"""
import_resolver.py - Phase 3
跨文件 import 解析：将未解析的 CALLS 边中的 callee_name 匹配到实际函数节点
unresolved func id → resolved func id，更新 resolved=True, confidence
"""

from __future__ import annotations
import logging
from ..graph.store import Neo4jStore

logger = logging.getLogger(__name__)

# Swift/ObjC 标准库、UIKit、CoreGraphics、常用类型 — 不需要解析
_STDLIB_SKIP = {
    # Swift 基础类型
    "Int", "Int8", "Int16", "Int32", "Int64",
    "UInt", "UInt8", "UInt16", "UInt32", "UInt64",
    "Float", "Double", "CGFloat", "Bool", "String",
    "Character", "Data", "Date", "URL", "UUID",
    "Array", "Dictionary", "Set", "Optional",
    "print", "debugPrint", "fatalError", "precondition", "assert",
    "abs", "min", "max", "sqrt", "floor", "ceil", "round",
    "integer", "string", "boolean", "number",
    # Foundation
    "NSObject", "NSString", "NSArray", "NSDictionary",
    "NSMutableArray", "NSMutableDictionary", "NSMutableString",
    "NSNumber", "NSDate", "NSURL", "NSData", "NSError",
    "NSIndexPath", "NSRange", "NSAttributedString", "NSMutableAttributedString",
    "NSNotification", "NSPredicate", "NSRegularExpression",
    # Dispatch
    "DispatchQueue", "DispatchGroup", "DispatchSemaphore",
    "DispatchWorkItem", "DispatchTime", "DispatchDeadline",
    # System
    "NotificationCenter", "UserDefaults", "Bundle",
    "FileManager", "JSONDecoder", "JSONEncoder",
    # UIKit 类型（不会在项目 Function 节点里）
    "UIView", "UIViewController", "UILabel", "UIButton", "UIImageView",
    "UITextField", "UITextView", "UISwitch", "UISlider",
    "UIScrollView", "UITableView", "UICollectionView", "UIStackView",
    "UITableViewCell", "UICollectionViewCell", "UICollectionReusableView",
    "UINavigationController", "UITabBarController", "UIPageViewController",
    "UIAlertController", "UIAlertAction",
    "UIGestureRecognizer", "UITapGestureRecognizer", "UIPanGestureRecognizer",
    "UILongPressGestureRecognizer", "UISwipeGestureRecognizer",
    "UIColor", "UIImage", "UIFont", "UIScreen", "UIWindow",
    "UIEdgeInsets", "UIOffset", "UIBezierPath",
    "UIBarButtonItem", "UINavigationItem", "UISearchBar",
    "UIActivityIndicatorView", "UIProgressView", "UIRefreshControl",
    "UISegmentedControl", "UIPickerView", "UIDatePicker",
    "UITableViewHeaderFooterView",
    # CoreGraphics
    "CGRect", "CGPoint", "CGSize", "CGColor", "CGFloat",
    "CGAffineTransform", "CGContext", "CGPath", "CGImage",
    "CGRectMake", "CGPointMake", "CGSizeMake",
    # Swift 关键字 / 属性被误解析为函数调用
    "selector", "escaping", "available", "discardableResult",
    "throws", "async", "await", "rethrows", "autoclosure",
    "noescape", "convention", "objc", "nonobjc",
    # 常见参数名被误解析
    "completion", "callback", "handler", "block",
    # 第三方布局库 (Masonry / PureLayout)
    "mas_equalTo", "mas_lessThanOrEqualTo", "mas_greaterThanOrEqualTo",
    "mas_makeConstraints", "mas_updateConstraints", "mas_remakeConstraints",
    "mas_left", "mas_right", "mas_top", "mas_bottom", "mas_width", "mas_height",
    "equalTo", "lessThanOrEqualTo", "greaterThanOrEqualTo",
    "autoPinEdge", "autoPinEdgesToSuperviewEdges", "autoSetDimension",
    "autoAlignAxis", "autoPinEdgeToSuperviewEdge", "autoSetDimensions",
    # Swift 自身关键字
    "super", "self", "init", "deinit",
    # SDWebImage / Kingfisher
    "sd_setImage", "sd_setImageWithURL",
}

_BATCH_SIZE = 2000


def resolve_imports(store: Neo4jStore, branch: str = "master") -> int:
    """
    对 resolved=False 的 CALLS 边，批量匹配真实 Function 节点。

    修复：使用两阶段方式避免 SKIP/LIMIT 分页漂移 bug：
      Phase A — 先快照所有未解析边的 elementId（只读，不修改）
      Phase B — 按 elementId 批量查询详情并更新

    策略优先级（高→低）：
      1. 同文件内匹配（confidence +20）
      2. 同 domain + 同 branch
      3. 同 branch 任意
      4. master 任意
    """
    # 构建全量函数索引（一次性加载，内存操作）
    logger.info("Phase 3: Building function index...")
    all_funcs = store.query("""
        MATCH (f:Function)
        WHERE f.branch = $branch OR f.branch = 'master'
        RETURN f.id AS id, f.name AS name, f.qualifiedName AS qname,
               f.filePath AS filePath, f.domain AS domain, f.branch AS branch
    """, {"branch": branch})

    # 索引：简单名 → 候选列表
    name_index: dict[str, list[dict]] = {}
    # 索引：filePath → {简单名 → 函数id}（同文件优先）
    file_index: dict[str, dict[str, str]] = {}

    for f in all_funcs:
        fname = f["name"]
        qname = f["qname"]
        fpath = f["filePath"] or ""

        for key in (fname, qname):
            if key:
                name_index.setdefault(key, []).append(f)

        if fname and fpath:
            file_index.setdefault(fpath, {})[fname] = f["id"]

    logger.info(f"Phase 3: Index built — {len(name_index)} names, {len(file_index)} files")

    # Phase A：快照所有未解析边的 elementId（只读）
    logger.info("Phase 3: Snapshotting unresolved edge IDs...")
    snapshot = store.query("""
        MATCH (src:Function)-[e:CALLS]->(dst)
        WHERE e.resolved = false AND e.calleeRaw IS NOT NULL
        RETURN elementId(e) AS eid
    """)
    all_eids = [r["eid"] for r in snapshot]
    total_edges = len(all_eids)
    logger.info(f"Phase 3: {total_edges} unresolved edges to process")

    total_resolved = 0

    # Phase B：按 elementId 分批处理
    for batch_start in range(0, total_edges, _BATCH_SIZE):
        batch_eids = all_eids[batch_start: batch_start + _BATCH_SIZE]

        # 按 elementId 拉取边详情
        edges = store.query("""
            MATCH (src:Function)-[e:CALLS]->(dst)
            WHERE elementId(e) IN $eids
            RETURN elementId(e) AS edge_eid,
                   src.filePath AS src_file,
                   src.domain AS src_domain,
                   e.calleeRaw AS callee_raw,
                   e.confidence AS confidence
        """, {"eids": batch_eids})

        updates: list[dict] = []

        for edge in edges:
            callee_raw = edge["callee_raw"]
            if not callee_raw or callee_raw in _STDLIB_SKIP:
                continue

            src_file = edge.get("src_file") or ""
            src_domain = edge.get("src_domain") or ""

            # 策略1：同文件内匹配（最高置信度）
            if src_file and src_file in file_index:
                func_id = file_index[src_file].get(callee_raw)
                if func_id:
                    updates.append({
                        "edge_eid": edge["edge_eid"],
                        "new_dst": func_id,
                        "confidence": min(100, (edge["confidence"] or 70) + 20),
                    })
                    continue

            # 策略2：全局名字索引匹配
            candidates = name_index.get(callee_raw, [])
            if not candidates:
                continue

            best = _pick_best_candidate(candidates, src_domain, src_file, branch)
            if best:
                conf_bonus = 10 if best["domain"] == src_domain else 0
                updates.append({
                    "edge_eid": edge["edge_eid"],
                    "new_dst": best["id"],
                    "confidence": min(100, (edge["confidence"] or 70) + conf_bonus),
                })

        if updates:
            _apply_resolved_updates(store, updates)
            total_resolved += len(updates)

        logger.info(
            f"Phase 3: batch {batch_start // _BATCH_SIZE + 1}/"
            f"{(total_edges + _BATCH_SIZE - 1) // _BATCH_SIZE}, "
            f"resolved {len(updates)}/{len(edges)}, total={total_resolved}"
        )

    logger.info(f"Phase 3: Done — resolved {total_resolved}/{total_edges} CALLS edges")
    return total_resolved


def _pick_best_candidate(candidates: list[dict], src_domain: str,
                         src_file: str, branch: str) -> dict | None:
    """选最佳候选：同文件 > 同domain同branch > 同branch > 任意"""
    for c in candidates:
        if (c.get("filePath") or "") == src_file:
            return c
    for c in candidates:
        if c["branch"] == branch and c["domain"] == src_domain:
            return c
    for c in candidates:
        if c["branch"] == branch:
            return c
    for c in candidates:
        if c["branch"] == "master" and c["domain"] == src_domain:
            return c
    return candidates[0] if candidates else None


def _apply_resolved_updates(store: Neo4jStore, updates: list[dict]) -> None:
    """批量将 unresolved 边重定向到真实 Function 节点（用 elementId 避免 id() 漂移）"""
    with store.driver.session() as session:
        for upd in updates:
            session.run("""
                MATCH (src:Function)-[e:CALLS]->(old)
                WHERE elementId(e) = $eid
                MATCH (newDst:Function {id: $new_dst})
                MERGE (src)-[ne:CALLS]->(newDst)
                SET ne = properties(e),
                    ne.resolved = true,
                    ne.confidence = $conf
                DELETE e
            """, eid=upd["edge_eid"], new_dst=upd["new_dst"],
                conf=upd["confidence"])
