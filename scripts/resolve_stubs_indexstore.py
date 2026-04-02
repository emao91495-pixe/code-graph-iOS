#!/usr/bin/env python3
"""
resolve_stubs_indexstore.py
===========================
用 Xcode Index Store 解析 stub 调用关系，将 CALLS 边从 stub 节点重定向到真实 Function 节点。

原理
----
Xcode 在每次 build 时，编译器会把跨文件的符号引用写入 Index Store（位于 DerivedData）。
Index Store 包含：
  - 每个源文件的所有符号定义（DEFINITION），带 USR（Unified Symbol Reference）
  - 每个调用点（CALL role），带 callee USR 和调用行号

我们用这些信息：
  1. 构建 USR → (filePath, lineStart) 映射（从 DEFINITION occurrences）
  2. 构建 (callerFile, callLine) → [calleeUSR, ...] 映射（从 CALL occurrences）
  3. 从 Neo4j 读所有 stub CALLS 边，每条边有 srcFile + callSite（行号）
  4. 匹配：(srcFile, callSite) → calleeUSR → (defFile, defLine) → Function node
  5. 将 CALLS 边从 stub 重定向到真实 Function 节点

使用方法
--------
  python3 resolve_stubs_indexstore.py [--dry-run] [--store <path>]

  --dry-run   只统计匹配数，不写 Neo4j
  --store     Index Store DataStore 路径（默认自动找 DerivedData）

Index Store 路径结构
--------------------
  ~/Library/Developer/Xcode/DerivedData/<Project>-<hash>/Index.noindex/DataStore/
    v5/
      units/     # 每个编译单元一个文件（多目标会有重复）
      records/   # 按前缀分桶的符号记录文件（已去重）

ARM64 macOS ctypes 注意事项
---------------------------
- C 函数接收 StringRef（16 字节 struct）时，必须拆成 (c_char_p, c_size_t) 分开传
  因为 ARM64 上 ctypes 的 struct-by-value 传参有 ABI 问题
- 回调函数 接收 StringRef 时，可以用 StringRef Structure（ctypes 接收 struct 是正确的）
- 所有 dispose/argtypes 必须显式设置，否则 ctypes 用 32-bit int 截断指针

注意事项
--------
- 只覆盖 Xcode 已 build 过的文件（Swift + ObjC）
- Pods/ 里的文件如果被 build，也会被解析
- 如果 DerivedData 被清理，需要重新 build 才能解析
- 动态调用（performSelector:）依然是 stub，IndexStore 无法解析
"""

import ctypes
import os
import sys
import time
import argparse
import logging
from collections import defaultdict
from pathlib import Path

# Add project root (parent of scripts/) to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─── libIndexStore ctypes 绑定 ────────────────────────────────────────────────

LIBSTORE_PATH = (
    "/Applications/Xcode.app/Contents/Developer/Toolchains/"
    "XcodeDefault.xctoolchain/usr/lib/libIndexStore.dylib"
)

# 符号 Role 标志位（来自 LLVM IndexStore C API）
ROLE_DECLARATION = 1 << 0   # 1
ROLE_DEFINITION  = 1 << 1   # 2
ROLE_REFERENCE   = 1 << 2   # 4
ROLE_CALL        = 1 << 5   # 32
ROLE_DYNAMIC     = 1 << 6   # 64
ROLE_CALLEDBY    = 1 << 13  # 8192

# Relation role flags（用于 occurrence relations）
RELATION_OVERRIDE_OF = 1 << 11  # 2048：impl method overrides/implements proto method

# Unit dependency 类型
DEP_FILE   = 0
DEP_RECORD = 1
DEP_UNIT   = 2
DEP_MODULE = 3


class StringRef(ctypes.Structure):
    """
    indexstore_string_ref_t

    只用于 C→Python 回调参数（ctypes CFUNCTYPE 接收 struct 是正确的）。
    Python→C 调用时，必须拆成 (c_char_p, c_size_t) 分开传——见 _str_args()。
    """
    _fields_ = [("data", ctypes.c_void_p), ("length", ctypes.c_size_t)]

    def decode(self) -> str:
        if self.data and self.length:
            return ctypes.string_at(self.data, self.length).decode("utf-8", errors="replace")
        return ""


def _str_args():
    """返回 StringRef 在 Python→C 调用时的参数类型（拆成两个）"""
    return [ctypes.c_char_p, ctypes.c_size_t]


def _load_lib() -> ctypes.CDLL:
    """加载 libIndexStore.dylib 并设置所有函数签名"""
    lib = ctypes.CDLL(LIBSTORE_PATH)

    # ── store ─────────────────────────────────────────────
    lib.indexstore_store_create.restype = ctypes.c_void_p
    lib.indexstore_store_create.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
    lib.indexstore_store_dispose.restype = None
    lib.indexstore_store_dispose.argtypes = [ctypes.c_void_p]

    # units_apply_f (function pointer 版，不是 ObjC block)
    # 回调签名：bool(*)(void *ctx, indexstore_string_ref_t unit_name)
    # ARM64 上 StringRef 在回调里用 Structure 接收是正确的
    UNIT_CB = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, StringRef)
    lib.indexstore_store_units_apply_f.restype = ctypes.c_bool
    lib.indexstore_store_units_apply_f.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, UNIT_CB
    ]
    lib._UNIT_CB = UNIT_CB  # 防止 GC

    # ── unit reader ───────────────────────────────────────
    # unit_reader_create 接收 StringRef by value
    # 必须拆成 (c_char_p, c_size_t)
    lib.indexstore_unit_reader_create.restype = ctypes.c_void_p
    lib.indexstore_unit_reader_create.argtypes = (
        [ctypes.c_void_p] + _str_args() + [ctypes.c_void_p]
    )
    lib.indexstore_unit_reader_dispose.restype = None
    lib.indexstore_unit_reader_dispose.argtypes = [ctypes.c_void_p]
    lib.indexstore_unit_reader_get_main_file.restype = StringRef
    lib.indexstore_unit_reader_get_main_file.argtypes = [ctypes.c_void_p]

    # unit dependencies_apply_f
    DEP_CB = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    lib.indexstore_unit_reader_dependencies_apply_f.restype = ctypes.c_bool
    lib.indexstore_unit_reader_dependencies_apply_f.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, DEP_CB
    ]
    lib._DEP_CB = DEP_CB

    lib.indexstore_unit_dependency_get_kind.restype = ctypes.c_int
    lib.indexstore_unit_dependency_get_kind.argtypes = [ctypes.c_void_p]
    lib.indexstore_unit_dependency_get_name.restype = StringRef
    lib.indexstore_unit_dependency_get_name.argtypes = [ctypes.c_void_p]
    lib.indexstore_unit_dependency_get_filepath.restype = StringRef
    lib.indexstore_unit_dependency_get_filepath.argtypes = [ctypes.c_void_p]

    # ── record reader ─────────────────────────────────────
    # record_reader_create 接收 StringRef by value → 拆成 (c_char_p, c_size_t)
    lib.indexstore_record_reader_create.restype = ctypes.c_void_p
    lib.indexstore_record_reader_create.argtypes = (
        [ctypes.c_void_p] + _str_args() + [ctypes.c_void_p]
    )
    lib.indexstore_record_reader_dispose.restype = None
    lib.indexstore_record_reader_dispose.argtypes = [ctypes.c_void_p]

    # occurrences_apply_f
    OCC_CB = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    lib.indexstore_record_reader_occurrences_apply_f.restype = ctypes.c_bool
    lib.indexstore_record_reader_occurrences_apply_f.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, OCC_CB
    ]
    lib._OCC_CB = OCC_CB

    # ── occurrence ────────────────────────────────────────
    lib.indexstore_occurrence_get_symbol.restype = ctypes.c_void_p
    lib.indexstore_occurrence_get_symbol.argtypes = [ctypes.c_void_p]
    lib.indexstore_occurrence_get_roles.restype = ctypes.c_uint64
    lib.indexstore_occurrence_get_roles.argtypes = [ctypes.c_void_p]
    lib.indexstore_occurrence_get_line_col.restype = None
    lib.indexstore_occurrence_get_line_col.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32)
    ]

    # relations_apply_f
    REL_CB = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    lib.indexstore_occurrence_relations_apply_f.restype = ctypes.c_bool
    lib.indexstore_occurrence_relations_apply_f.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, REL_CB
    ]
    lib._REL_CB = REL_CB

    # ── symbol ────────────────────────────────────────────
    lib.indexstore_symbol_get_name.restype = StringRef
    lib.indexstore_symbol_get_name.argtypes = [ctypes.c_void_p]
    lib.indexstore_symbol_get_usr.restype = StringRef
    lib.indexstore_symbol_get_usr.argtypes = [ctypes.c_void_p]
    lib.indexstore_symbol_get_kind.restype = ctypes.c_int
    lib.indexstore_symbol_get_kind.argtypes = [ctypes.c_void_p]

    # ── relation ─────────────────────────────────────────
    lib.indexstore_symbol_relation_get_roles.restype = ctypes.c_uint64
    lib.indexstore_symbol_relation_get_roles.argtypes = [ctypes.c_void_p]
    lib.indexstore_symbol_relation_get_symbol.restype = ctypes.c_void_p
    lib.indexstore_symbol_relation_get_symbol.argtypes = [ctypes.c_void_p]

    return lib


# ─── IndexStore 解析 ──────────────────────────────────────────────────────────

def parse_indexstore(store_path: str, collect_overrides: bool = False) -> tuple:
    """
    扫描 IndexStore，返回：
      usr_to_def:    {usr → {"name": str, "filePath": str, "line": int}}
      call_sites:    {(filePath, callLine) → [calleeUSR, ...]}

    当 collect_overrides=True 时额外返回：
      override_map:  {proto_method_usr → [impl_method_usr, ...]}
                     含义：impl 方法实现/覆盖了 proto 方法
    """
    lib = _load_lib()
    store = lib.indexstore_store_create(store_path.encode(), None)
    if not store:
        raise RuntimeError(f"无法打开 IndexStore: {store_path}")

    usr_to_def: dict[str, dict] = {}
    call_sites: dict[tuple, list] = defaultdict(list)
    override_map: dict[str, list] = defaultdict(list)  # proto_usr → [impl_usr, ...]
    processed_records: set[str] = set()
    unit_count = [0]
    record_count = [0]

    OCC_CB  = lib._OCC_CB
    REL_CB  = lib._REL_CB

    def make_occ_handler(file_path: str):
        def on_occurrence(ctx, occ):
            roles = lib.indexstore_occurrence_get_roles(occ)
            if not (roles & (ROLE_DEFINITION | ROLE_CALL)):
                return True

            sym = lib.indexstore_occurrence_get_symbol(occ)
            if not sym:
                return True

            usr = lib.indexstore_symbol_get_usr(sym).decode()
            if not usr:
                return True

            name = lib.indexstore_symbol_get_name(sym).decode()
            line = ctypes.c_uint32(0)
            col  = ctypes.c_uint32(0)
            lib.indexstore_occurrence_get_line_col(occ, ctypes.byref(line), ctypes.byref(col))

            if roles & ROLE_DEFINITION:
                if usr not in usr_to_def:
                    usr_to_def[usr] = {
                        "name": name,
                        "filePath": file_path,
                        "line": int(line.value)
                    }

                # 提取 overrideOf 关系（protocol dispatch 用）
                if collect_overrides:
                    _impl_usr = usr  # closure capture

                    def on_relation(ctx2, rel):
                        rel_roles = lib.indexstore_symbol_relation_get_roles(rel)
                        if rel_roles & RELATION_OVERRIDE_OF:
                            rel_sym = lib.indexstore_symbol_relation_get_symbol(rel)
                            if rel_sym:
                                proto_usr = lib.indexstore_symbol_get_usr(rel_sym).decode()
                                if proto_usr and _impl_usr not in override_map[proto_usr]:
                                    override_map[proto_usr].append(_impl_usr)
                        return True

                    rel_cb = REL_CB(on_relation)
                    lib.indexstore_occurrence_relations_apply_f(occ, None, rel_cb)

            if roles & ROLE_CALL:
                key = (file_path, int(line.value))
                if usr not in call_sites[key]:
                    call_sites[key].append(usr)

            return True
        return on_occurrence

    # store_path/v5/records/<last-2-chars>/<record-name>
    records_base = os.path.join(store_path, "v5", "records")

    def record_file_exists(record_name: str) -> bool:
        """indexstore_record_reader_create 在文件不存在时会 crash，必须先检查"""
        prefix = record_name[-2:] if len(record_name) >= 2 else ""
        return os.path.exists(os.path.join(records_base, prefix, record_name))

    def process_record(record_name: str, file_path: str):
        if record_name in processed_records:
            return
        if not record_file_exists(record_name):
            return  # SDK/外部 record，不在本地，跳过
        processed_records.add(record_name)
        record_count[0] += 1

        rbytes = record_name.encode()
        rec_reader = lib.indexstore_record_reader_create(store, rbytes, len(rbytes), None)
        if not rec_reader:
            return

        occ_cb = OCC_CB(make_occ_handler(file_path))
        lib.indexstore_record_reader_occurrences_apply_f(rec_reader, None, occ_cb)
        lib.indexstore_record_reader_dispose(rec_reader)

    DEP_CB = lib._DEP_CB

    def make_dep_handler():
        def on_dependency(ctx, dep):
            kind = lib.indexstore_unit_dependency_get_kind(dep)
            # kind=1 (DEP_RECORD): 外部模块 record (.swiftinterface, .pch 等)
            # kind=2 (DEP_UNIT):   Swift 源文件 unit — 同样存储为 record，可直接读取
            if kind in (DEP_RECORD, DEP_UNIT):
                record_name = lib.indexstore_unit_dependency_get_name(dep).decode()
                fp = lib.indexstore_unit_dependency_get_filepath(dep).decode()
                if record_name and fp:
                    process_record(record_name, fp)
            return True
        return on_dependency

    UNIT_CB = lib._UNIT_CB

    def on_unit(ctx, unit_name_ref):
        unit_count[0] += 1
        if unit_count[0] % 5000 == 0:
            log.info(
                f"  units: {unit_count[0]}/38105, records: {record_count[0]}, "
                f"defs: {len(usr_to_def)}, calls: {len(call_sites)}"
            )

        uname = unit_name_ref.decode()
        ubytes = uname.encode()
        unit_reader = lib.indexstore_unit_reader_create(store, ubytes, len(ubytes), None)
        if not unit_reader:
            return True

        dep_handler = make_dep_handler()
        dep_cb = DEP_CB(dep_handler)
        lib.indexstore_unit_reader_dependencies_apply_f(unit_reader, None, dep_cb)
        lib.indexstore_unit_reader_dispose(unit_reader)
        return True

    log.info("扫描 IndexStore units...")
    unit_cb = UNIT_CB(on_unit)
    lib.indexstore_store_units_apply_f(store, 0, None, unit_cb)
    lib.indexstore_store_dispose(store)

    log.info(f"IndexStore 扫描完成: {unit_count[0]} units, {record_count[0]} 唯一 records")
    log.info(f"  定义: {len(usr_to_def)}, 调用点: {len(call_sites)}")
    if collect_overrides:
        log.info(f"  protocol override 关系: {len(override_map)}")

    if collect_overrides:
        return usr_to_def, dict(call_sites), dict(override_map)
    return usr_to_def, dict(call_sites)


# ─── Neo4j Stub 解析 ──────────────────────────────────────────────────────────

def resolve_stubs(store_path: str, dry_run: bool = False):
    from src.graph.store import Neo4jStore

    t0 = time.time()
    log.info("=== Step 1: 解析 IndexStore ===")
    usr_to_def, call_sites = parse_indexstore(store_path)

    log.info("=== Step 2: 读取 Neo4j Function 节点 ===")
    neo4j = Neo4jStore()

    # 构建 (filePath, lineStart) → node_id 映射
    log.info("  加载 Function 节点...")
    funcs = neo4j.query("""
        MATCH (f:Function)
        WHERE f.filePath IS NOT NULL AND f.lineStart IS NOT NULL
        RETURN f.id AS id, f.filePath AS filePath, f.lineStart AS lineStart
    """)
    file_line_to_id: dict[tuple, str] = {}
    for f in funcs:
        key = (f["filePath"], int(f["lineStart"]))
        file_line_to_id[key] = f["id"]
    log.info(f"  已加载 {len(funcs)} 个 Function 节点")

    log.info("=== Step 3: 读取 Stub CALLS 边 ===")
    stub_edges = neo4j.query("""
        MATCH (src:Function)-[r:CALLS]->(dst)
        WHERE dst.id STARTS WITH 'func:unresolved'
          AND r.callSite IS NOT NULL
        RETURN src.filePath AS srcFile, r.callSite AS callSite,
               dst.id AS dstStubId, r.src AS edgeSrc, r.dst AS edgeDst,
               r.calleeRaw AS calleeRaw
    """)
    log.info(f"  待解析 stub 边: {len(stub_edges)}")

    log.info("=== Step 4: 匹配 ===")
    resolved = []    # [(edgeSrc, edgeDst, realFuncId)]
    unresolved_reasons = defaultdict(int)

    for edge in stub_edges:
        src_file = edge["srcFile"]
        call_line = int(edge["callSite"])
        callee_raw = edge.get("calleeRaw") or ""

        # ±1 容差查找调用点（IndexStore 行号 vs Neo4j 解析行号可能偏移1行）
        callee_usrs = []
        for delta in (0, 1, -1):
            callee_usrs = call_sites.get((src_file, call_line + delta), [])
            if callee_usrs:
                break

        if not callee_usrs:
            unresolved_reasons["no_call_site_in_indexstore"] += 1
            continue

        # 同一行可能有多个调用，用 calleeRaw 名称过滤，避免张冠李戴
        # 不做 fallback：calleeRaw 匹配不到就是 SDK/外部函数，直接跳过
        if callee_raw:
            callee_raw_lower = callee_raw.lower()
            filtered = [u for u in callee_usrs
                        if callee_raw_lower in usr_to_def.get(u, {}).get("name", "").lower()]
            callee_usrs = filtered  # 空列表会在后续被当成 sdk/external

        matched_id = None
        for usr in callee_usrs:
            defn = usr_to_def.get(usr)
            if not defn:
                continue
            # ±1 容差查找被调方定义（IndexStore defLine vs 我们的解析行号可能偏移1行）
            for d in (0, 1, -1):
                node_id = file_line_to_id.get((defn["filePath"], defn["line"] + d))
                if node_id:
                    matched_id = node_id
                    break
            if matched_id:
                break

        if matched_id:
            resolved.append((edge["edgeSrc"], edge["edgeDst"], matched_id))
        else:
            if callee_usrs and all(u in usr_to_def for u in callee_usrs):
                unresolved_reasons["callee_not_in_our_graph"] += 1
            else:
                unresolved_reasons["callee_in_sdk_or_external"] += 1

    total = len(stub_edges)
    log.info(f"  匹配成功: {len(resolved)} / {total} = {len(resolved)/total*100:.1f}%")
    log.info(f"  未匹配原因: {dict(unresolved_reasons)}")

    if dry_run:
        log.info("dry-run 模式，不写 Neo4j")
        for src, dst_stub, real_id in resolved[:5]:
            log.info(f"  示例: {dst_stub} → {real_id}")
        return

    log.info("=== Step 5: 写回 Neo4j ===")
    BATCH = 500
    written = 0
    for i in range(0, len(resolved), BATCH):
        batch = resolved[i:i + BATCH]
        records = [{"src": s, "dst_stub": d, "dst_real": r} for s, d, r in batch]
        neo4j.query("""
            UNWIND $records AS rec
            MATCH (src:Function {id: rec.src})-[old:CALLS]->(stub {id: rec.dst_stub})
            MATCH (real:Function {id: rec.dst_real})
            MERGE (src)-[new:CALLS]->(real)
            SET new.confidence = 99,
                new.callSite   = old.callSite,
                new.resolved   = true,
                new.src        = old.src,
                new.dst        = rec.dst_real
            DELETE old
        """, params={"records": records})
        written += len(batch)
        log.info(f"  写入进度: {written}/{len(resolved)}")

    elapsed = time.time() - t0
    log.info("=== 完成 ===")
    log.info(f"  解析 stub 总数: {len(stub_edges)}")
    log.info(f"  成功重定向:    {len(resolved)} ({len(resolved)/total*100:.1f}%)")
    log.info(f"  耗时: {elapsed:.1f}s")


# ─── Protocol Dispatch 解析 ───────────────────────────────────────────────────

def resolve_protocol_dispatch(store_path: str, dry_run: bool = False):
    """
    通过 IndexStore overrideOf 关系补全 protocol/delegate 调用边。

    原理
    ----
    IndexStore 为每个实现了协议方法的函数记录 RELATION_OVERRIDE_OF 关系：
      impl_method_USR --overrideOf--> proto_method_USR

    同时，调用协议方法的 call site 记录了 proto_method_USR 作为 callee。
    通过这两者可以构建：
      caller_function → protocol_method → impl_function
    即为每个 caller 添加指向所有 impl 的 CALLS 边（confidence=75, via='protocol_dispatch'）。
    """
    from src.graph.store import Neo4jStore

    t0 = time.time()
    log.info("=== Step 1: 解析 IndexStore（含 override 关系）===")
    usr_to_def, call_sites, override_map = parse_indexstore(store_path, collect_overrides=True)

    if not override_map:
        log.info("未发现任何 override 关系，可能 Index Store 未更新或分支不含协议实现")
        return

    log.info(f"  发现 {len(override_map)} 个 protocol 方法有实现覆盖")

    # 构建反向索引：proto_usr → [(callerFilePath, callLine), ...]
    proto_to_callsites: dict[str, list[tuple]] = defaultdict(list)
    for (fp, line), usrs in call_sites.items():
        for usr in usrs:
            if usr in override_map:
                proto_to_callsites[usr].append((fp, line))

    total_proto_calls = sum(len(v) for v in proto_to_callsites.values())
    log.info(f"  有调用的 protocol 方法: {len(proto_to_callsites)} 个, 调用点: {total_proto_calls}")

    log.info("=== Step 2: 读取 Neo4j Function 节点 ===")
    neo4j = Neo4jStore()

    funcs = neo4j.query("""
        MATCH (f:Function)
        WHERE f.filePath IS NOT NULL AND f.lineStart IS NOT NULL
        RETURN f.id AS id, f.filePath AS filePath, f.lineStart AS lineStart
    """)
    file_line_to_id: dict[tuple, str] = {}
    for f in funcs:
        key = (f["filePath"], int(f["lineStart"]))
        file_line_to_id[key] = f["id"]
    log.info(f"  已加载 {len(funcs)} 个 Function 节点")

    log.info("=== Step 3: 匹配 caller → proto → impl ===")
    edges_to_add = []  # [(caller_id, impl_id, call_line)]

    for proto_usr, callsite_list in proto_to_callsites.items():
        impl_usrs = override_map[proto_usr]

        # 解析每个 impl_usr 对应的 Function 节点
        impl_ids = []
        for impl_usr in impl_usrs:
            defn = usr_to_def.get(impl_usr)
            if not defn:
                continue
            for delta in (0, 1, -1):
                node_id = file_line_to_id.get((defn["filePath"], defn["line"] + delta))
                if node_id:
                    impl_ids.append(node_id)
                    break

        if not impl_ids:
            continue

        # 解析每个 call site 对应的 caller Function 节点
        for (caller_file, call_line) in callsite_list:
            # caller 是包含这个 call site 的函数，需要在 Neo4j 里找：
            # 该文件中 lineStart <= call_line <= lineEnd 的函数
            # （为避免逐行查 Neo4j，先构建 filePath → [func] 的内存索引）
            pass  # 用下面的批量查询代替

    # 批量查 Neo4j：给定 (filePath, callLine)，找 caller Function
    if proto_to_callsites:
        callsite_params = []
        for proto_usr, callsite_list in proto_to_callsites.items():
            impl_usrs = override_map[proto_usr]
            impl_ids = []
            for impl_usr in impl_usrs:
                defn = usr_to_def.get(impl_usr)
                if defn:
                    for delta in (0, 1, -1):
                        node_id = file_line_to_id.get((defn["filePath"], defn["line"] + delta))
                        if node_id:
                            impl_ids.append(node_id)
                            break
            if not impl_ids:
                continue
            for (caller_file, call_line) in callsite_list:
                callsite_params.append({
                    "filePath": caller_file,
                    "callLine": call_line,
                    "implIds": impl_ids,
                })

        log.info(f"  有效调用点（含 impl 节点）: {len(callsite_params)}")

        if not callsite_params:
            log.info("  没有可解析的 protocol dispatch 调用，退出")
            return

        if dry_run:
            log.info("dry-run 模式，不写 Neo4j")
            for p in callsite_params[:5]:
                log.info(f"  示例: {p['filePath']}:{p['callLine']} → impl={p['implIds']}")
            return

        log.info("=== Step 4: 写回 Neo4j（protocol dispatch 边）===")
        BATCH = 200
        written = 0
        for i in range(0, len(callsite_params), BATCH):
            batch = callsite_params[i:i + BATCH]
            result = neo4j.query("""
                UNWIND $items AS item
                MATCH (caller:Function)
                WHERE caller.filePath = item.filePath
                  AND caller.lineStart <= item.callLine
                  AND caller.lineEnd   >= item.callLine
                WITH caller, item
                UNWIND item.implIds AS implId
                MATCH (impl:Function {id: implId})
                MERGE (caller)-[e:CALLS]->(impl)
                ON CREATE SET
                    e.confidence    = 75,
                    e.via           = 'protocol_dispatch',
                    e.callSite      = item.callLine,
                    e.resolved      = true
                ON MATCH SET
                    e.confidence    = CASE WHEN e.confidence < 75 THEN 75 ELSE e.confidence END,
                    e.via           = coalesce(e.via, 'protocol_dispatch')
                RETURN count(e) AS cnt
            """, params={"items": batch})
            cnt = sum(r["cnt"] for r in result) if result else 0
            written += cnt
            log.info(f"  写入进度: batch {i//BATCH+1}, 累计新增/更新边: {written}")

    elapsed = time.time() - t0
    log.info("=== 完成 ===")
    log.info(f"  处理 protocol override 关系: {len(override_map)}")
    log.info(f"  有效调用点: {len(callsite_params)}")
    log.info(f"  写入/更新边: {written}")
    log.info(f"  耗时: {elapsed:.1f}s")


# ─── CLI ─────────────────────────────────────────────────────────────────────

def find_default_store() -> str:
    """
    Auto-discover the most recently modified Xcode Index Store under DerivedData.
    Picks the project with the most recently modified DataStore directory.
    """
    derived = Path.home() / "Library/Developer/Xcode/DerivedData"
    if not derived.exists():
        raise FileNotFoundError(f"DerivedData not found at {derived}")

    candidates = []
    for p in derived.iterdir():
        candidate = p / "Index.noindex/DataStore"
        if candidate.exists():
            mtime = candidate.stat().st_mtime
            candidates.append((mtime, candidate))

    if not candidates:
        raise FileNotFoundError(
            "No Index Store found in DerivedData. "
            "Make sure you have built the project in Xcode at least once."
        )

    # Use the most recently modified store
    candidates.sort(reverse=True)
    chosen = candidates[0][1]
    log.info(f"Auto-selected Index Store: {chosen}")
    return str(chosen)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Resolve stub CALLS edges using Xcode Index Store."
    )
    parser.add_argument("--store", default=None,
                        help="Path to Index Store DataStore directory (auto-detected if omitted)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Count matches only, do not write to Neo4j")
    parser.add_argument(
        "--protocol-dispatch",
        action="store_true",
        help="Also resolve protocol/delegate dispatch edges"
    )
    args = parser.parse_args()

    store_path = args.store or find_default_store()
    log.info(f"IndexStore: {store_path}")

    if args.protocol_dispatch:
        resolve_protocol_dispatch(store_path, dry_run=args.dry_run)
    else:
        resolve_stubs(store_path, dry_run=args.dry_run)
