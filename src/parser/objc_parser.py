"""
Objective-C AST 解析器
使用 libclang（Clang C API Python bindings）解析 .m / .h 文件，提取：
- @interface / @implementation / @protocol 定义
- 方法声明与定义（含 selector、行号、是否 static）
- 消息发送（[receiver selector]）

回退链：libclang → regex（无 libclang 时）
"""

from __future__ import annotations
import re
from pathlib import Path
from typing import Optional

from .swift_parser import SwiftClass, SwiftFunction, SwiftCall, SwiftImport, ParseResult

# ── libclang 初始化 ─────────────────────────────────────────────────────────
_LIBCLANG_PATH = (
    "/Applications/Xcode.app/Contents/Developer/Toolchains/"
    "XcodeDefault.xctoolchain/usr/lib/libclang.dylib"
)

try:
    import clang.cindex as _cx
    if Path(_LIBCLANG_PATH).exists():
        _cx.Config.set_library_file(_LIBCLANG_PATH)
    _index = _cx.Index.create()
    LIBCLANG_AVAILABLE = True
except Exception:
    LIBCLANG_AVAILABLE = False
    _cx = None
    _index = None


def parse_file(file_path: str) -> ParseResult:
    path = Path(file_path)
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return ParseResult(file_path, [], [], [], [], [str(e)])

    if LIBCLANG_AVAILABLE:
        return _parse_with_libclang(file_path, source)
    return _parse_with_regex(file_path, source)


# ── libclang 解析 ────────────────────────────────────────────────────────────

def _parse_with_libclang(file_path: str, source: str) -> ParseResult:
    """使用 libclang 精确解析 ObjC 文件"""
    tu = _index.parse(
        file_path,
        args=["-ObjC", "-fobjc-arc", "-x", "objective-c"],
        unsaved_files=[(file_path, source)],
        options=_cx.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD,
    )

    classes: list[SwiftClass] = []
    functions: list[SwiftFunction] = []
    calls: list[SwiftCall] = []
    imports_list: list[SwiftImport] = []
    errors: list[str] = [d.spelling for d in tu.diagnostics if d.severity >= _cx.Diagnostic.Error]

    # 已登记的 (qualified_name, line_no) 避免重复 call 边
    _seen_calls: set[tuple[str, str, int]] = set()

    # 用于把 message_send 归属到当前函数
    _func_stack: list[SwiftFunction] = []

    CursorKind = _cx.CursorKind

    def line_of(cursor) -> int:
        return cursor.location.line or 0

    def end_line_of(cursor) -> int:
        return cursor.extent.end.line or 0

    def walk(cursor, current_class: Optional[str] = None):
        # 只处理主文件中的节点
        if cursor.location.file and cursor.location.file.name != file_path:
            return

        kind = cursor.kind

        # ── import ──────────────────────────────────────────────────────────
        if kind == CursorKind.OBJC_CLASS_REF and current_class is None:
            pass  # 交由 #import 正则处理（libclang 不直接暴露 preproc include）

        # ── @interface ──────────────────────────────────────────────────────
        elif kind == CursorKind.OBJC_INTERFACE_DECL:
            name = cursor.spelling
            if not name:
                return
            super_cls = next(
                (c.spelling for c in cursor.get_children()
                 if c.kind == CursorKind.OBJC_SUPER_CLASS_REF),
                None
            )
            protocols = [
                c.spelling for c in cursor.get_children()
                if c.kind == CursorKind.OBJC_PROTOCOL_REF
            ]
            classes.append(SwiftClass(
                name=name, kind="class", file_path=file_path,
                line_start=line_of(cursor), line_end=end_line_of(cursor),
                is_public=True,
                inherits=[super_cls] if super_cls else [],
                implements=protocols,
            ))
            for child in cursor.get_children():
                walk(child, current_class=name)
            return

        # ── @protocol ───────────────────────────────────────────────────────
        elif kind == CursorKind.OBJC_PROTOCOL_DECL:
            name = cursor.spelling
            if not name:
                return
            classes.append(SwiftClass(
                name=name, kind="protocol", file_path=file_path,
                line_start=line_of(cursor), line_end=end_line_of(cursor),
                is_public=True, inherits=[], implements=[],
            ))
            for child in cursor.get_children():
                walk(child, current_class=name)
            return

        # ── @implementation ─────────────────────────────────────────────────
        elif kind == CursorKind.OBJC_IMPLEMENTATION_DECL:
            name = cursor.spelling
            if not name:
                return
            for child in cursor.get_children():
                walk(child, current_class=name)
            return

        # ── category implementation ─────────────────────────────────────────
        elif kind == CursorKind.OBJC_CATEGORY_IMPL_DECL:
            # "ClassName (CategoryName)" → use class name
            name = cursor.spelling  # e.g. "TPNetwork"
            for child in cursor.get_children():
                walk(child, current_class=name)
            return

        # ── method definition ───────────────────────────────────────────────
        elif kind in (CursorKind.OBJC_INSTANCE_METHOD_DECL,
                      CursorKind.OBJC_CLASS_METHOD_DECL):
            if not cursor.is_definition():
                # 只处理有方法体的定义（@implementation 中的）
                for child in cursor.get_children():
                    walk(child, current_class=current_class)
                return

            selector = cursor.spelling  # e.g. "doSomething:withParam:"
            is_static = (kind == CursorKind.OBJC_CLASS_METHOD_DECL)
            qualified = f"{current_class}.{selector}" if current_class else selector
            # 取返回类型 + selector 作为 signature
            ret_type = cursor.result_type.spelling if hasattr(cursor, 'result_type') else ""
            prefix = "+" if is_static else "-"
            sig = f"{prefix} ({ret_type}){selector}"[:200]

            func = SwiftFunction(
                name=selector,
                qualified_name=qualified,
                file_path=file_path,
                line_start=line_of(cursor),
                line_end=end_line_of(cursor),
                signature=sig,
                is_public=True,
                is_static=is_static,
                parent_class=current_class,
            )
            functions.append(func)

            # 进入方法体提取调用
            _func_stack.append(func)
            for child in cursor.get_children():
                walk(child, current_class=current_class)
            _func_stack.pop()
            return

        # ── message send ─────────────────────────────────────────────────────
        elif kind == CursorKind.OBJC_MESSAGE_EXPR:
            if _func_stack:
                caller = _func_stack[-1].qualified_name
                selector = cursor.spelling  # 完整 selector，如 "setTitle:forState:"
                if selector and selector not in _OBJC_SKIP:
                    # receiver：第一个子节点
                    receiver = None
                    children = list(cursor.get_children())
                    if children:
                        recv_cursor = children[0]
                        recv_text = recv_cursor.spelling
                        if recv_text and recv_text not in ("self", "super"):
                            receiver = recv_text

                    key = (caller, selector, line_of(cursor))
                    if key not in _seen_calls:
                        _seen_calls.add(key)
                        calls.append(SwiftCall(
                            caller_qualified=caller,
                            callee_name=selector,
                            callee_receiver=receiver,
                            line_no=line_of(cursor),
                            confidence=75,
                        ))

        # ── 递归 ────────────────────────────────────────────────────────────
        for child in cursor.get_children():
            walk(child, current_class=current_class)

    walk(tu.cursor)

    # #import 行用正则补充（libclang 不暴露 preproc_include 节点内容）
    for m in _IMPORT_RE.finditer(source):
        raw = m.group(1)
        module = raw.split("/")[0].replace(".h", "")
        line = source[:m.start()].count("\n") + 1
        imports_list.append(SwiftImport(module, file_path, line))

    return ParseResult(file_path, classes, functions, calls, imports_list, errors)


# ── Regex fallback ──────────────────────────────────────────────────────────

_INTERFACE_RE = re.compile(r'@(?:interface|protocol)\s+(\w+)(?:\s*:\s*(\w+))?', re.MULTILINE)
_IMPL_RE = re.compile(r'@implementation\s+(\w+)', re.MULTILINE)
_IMPORT_RE = re.compile(r'#import\s+[<"]([^>"]+)[>"]', re.MULTILINE)
_METHOD_START_RE = re.compile(r'^([-+])\s*\([^)]+\)\s*(\w+)', re.MULTILINE)
_KEYWORD_PART_RE = re.compile(r'(\w+)\s*:\s*\([^)]+\)\s*\w+')
_MSG_RE = re.compile(r'\[(\w[\w.*]*)\s+(\w+)', re.MULTILINE)

_OBJC_SKIP = frozenset([
    'if', 'else', 'for', 'while', 'do', 'switch', 'case', 'return',
    'self', 'super', 'nil', 'NULL', 'YES', 'NO', 'true', 'false',
    'void', 'int', 'long', 'float', 'double', 'char', 'BOOL',
    'NSObject', 'NSString', 'NSArray', 'NSDictionary', 'NSNumber',
    'NSMutableArray', 'NSMutableDictionary',
    'alloc', 'init', 'new', 'copy', 'dealloc', 'retain', 'release',
    'autorelease', 'description', 'class', 'superclass',
])


def _line_no(source: str, pos: int) -> int:
    return source[:pos].count("\n") + 1


def _build_impl_blocks(source: str) -> list[tuple[str, int, int]]:
    matches = list(_IMPL_RE.finditer(source))
    total_lines = source.count("\n") + 1
    blocks = []
    for i, m in enumerate(matches):
        line_start = _line_no(source, m.start())
        line_end = (_line_no(source, matches[i + 1].start()) - 1
                    if i + 1 < len(matches) else total_lines)
        blocks.append((m.group(1), line_start, line_end))
    return blocks


def _get_class_for_line(impl_blocks: list[tuple[str, int, int]], line_no: int) -> Optional[str]:
    for class_name, start, end in impl_blocks:
        if start <= line_no <= end:
            return class_name
    return None


def _parse_with_regex(file_path: str, source: str) -> ParseResult:
    """正则回退解析（无 libclang 时使用）"""
    classes: list[SwiftClass] = []
    functions: list[SwiftFunction] = []
    calls: list[SwiftCall] = []
    imports_list: list[SwiftImport] = []

    for m in _IMPORT_RE.finditer(source):
        line = _line_no(source, m.start())
        module = m.group(1).split("/")[0].replace(".h", "")
        imports_list.append(SwiftImport(module, file_path, line))

    total_lines = source.count("\n") + 1
    for i, m in enumerate(list(_INTERFACE_RE.finditer(source))):
        ms = list(_INTERFACE_RE.finditer(source))
        line_start = _line_no(source, m.start())
        line_end = (_line_no(source, ms[i + 1].start()) - 1
                    if i + 1 < len(ms) else total_lines)
        inherits = [m.group(2)] if m.group(2) else []
        classes.append(SwiftClass(
            name=m.group(1), kind="class", file_path=file_path,
            line_start=line_start, line_end=line_end,
            is_public=True, inherits=inherits,
        ))

    impl_blocks = _build_impl_blocks(source)
    method_matches = list(_METHOD_START_RE.finditer(source))

    for i, m in enumerate(method_matches):
        line_start = _line_no(source, m.start())
        line_end = (_line_no(source, method_matches[i + 1].start()) - 1
                    if i + 1 < len(method_matches) else total_lines)

        current_class = _get_class_for_line(impl_blocks, line_start)
        if current_class is None:
            continue

        is_static = m.group(1) == "+"
        source_from_method = source[m.start():]
        keywords = _KEYWORD_PART_RE.findall(source_from_method.split("{")[0].split("\n")[0])
        selector = (":".join(keywords) + ":") if keywords else m.group(2)
        qualified = f"{current_class}.{selector}"
        sig_end = source.find("{", m.start())
        signature = (source[m.start():sig_end].strip()[:200]
                     if sig_end != -1 else m.group(0).strip()[:200])

        functions.append(SwiftFunction(
            name=selector, qualified_name=qualified,
            file_path=file_path, line_start=line_start, line_end=line_end,
            signature=signature, is_public=True, is_static=is_static,
            parent_class=current_class,
        ))

    msg_by_line: dict[int, list[tuple[str, str]]] = {}
    for m in _MSG_RE.finditer(source):
        line = _line_no(source, m.start())
        if m.group(2) not in _OBJC_SKIP:
            msg_by_line.setdefault(line, []).append((m.group(1), m.group(2)))

    for func in functions:
        for line, msgs in msg_by_line.items():
            if func.line_start <= line <= func.line_end:
                for receiver, selector in msgs:
                    calls.append(SwiftCall(
                        caller_qualified=func.qualified_name,
                        callee_name=selector,
                        callee_receiver=receiver if receiver not in ('self', 'super') else None,
                        line_no=line,
                        confidence=50,
                    ))

    return ParseResult(file_path, classes, functions, calls, imports_list, [])
