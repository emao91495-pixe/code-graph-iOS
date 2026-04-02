"""
process_tracer.py - Phase 5
执行流预计算：找到入口函数，沿 CALLS 链追踪，将完整路径存入 Process 节点
查询时直接读取，不做 runtime 遍历
"""

from __future__ import annotations
import logging
from ..graph.store import Neo4jStore

logger = logging.getLogger(__name__)

# 典型入口函数模式（viewDidLoad / viewDidAppear / application:didFinish / etc.）
ENTRY_POINT_PATTERNS = [
    "viewDidLoad",
    "viewDidAppear",
    "viewWillAppear",
    "application:didFinishLaunchingWithOptions",
    "applicationDidBecomeActive",
    "scene:willConnectTo",
    "startNavigation",
    "beginTrip",
    "onDeviation",           # 偏航相关入口
    "getTrafficConditions",  # 摄像头相关
    "didReceiveRemoteNotification",
    "paymentSheet",
    "handleDeepLink",
]

MAX_DEPTH = 15          # 最大追踪深度，防止循环
MAX_PATH_NODES = 50     # 单条执行流最多节点数


def trace_processes(store: Neo4jStore, branch: str = "master") -> int:
    """
    预计算所有入口函数的执行流，写入 Process 节点和 PART_OF 边
    返回发现的执行流数量
    """
    # 找到入口函数（公开方法 + 符合入口模式）
    entry_funcs = _find_entry_points(store, branch)
    logger.info(f"Phase 5: Found {len(entry_funcs)} entry points")

    process_count = 0
    for func in entry_funcs:
        # 追踪调用链
        chain = _trace_call_chain(store, func["id"], branch, MAX_DEPTH)
        if len(chain) < 2:
            continue

        # 创建 Process 节点
        process_id = f"process:{func['id']}"
        domain = func.get("domain", "unknown")

        with store.driver.session() as session:
            session.run("""
                MERGE (p:Process {id: $id})
                SET p.name = $name,
                    p.entryPoint = $entry,
                    p.domain = $domain,
                    p.nodeCount = $count,
                    p.branch = $branch
            """, id=process_id,
                name=f"{domain}/{func['name']}",
                entry=func["qualifiedName"],
                domain=domain,
                count=len(chain),
                branch=branch)

            # 创建 PART_OF 边
            for node_id in chain:
                session.run("""
                    MATCH (f:Function {id: $fid})
                    MATCH (p:Process {id: $pid})
                    MERGE (f)-[:PART_OF]->(p)
                """, fid=node_id, pid=process_id)

        process_count += 1

    logger.info(f"Phase 5: Traced {process_count} execution flows")
    return process_count


def _find_entry_points(store: Neo4jStore, branch: str) -> list[dict]:
    """找到入口函数：isPublic=true 或名称匹配入口模式"""
    results = store.query("""
        MATCH (f:Function)
        WHERE f.branch = $branch OR f.branch = 'master'
        AND (f.isPublic = true OR any(pattern IN $patterns WHERE f.name CONTAINS pattern))
        RETURN f.id AS id, f.name AS name, f.qualifiedName AS qualifiedName,
               f.domain AS domain, f.filePath AS filePath
        LIMIT 200
    """, {"branch": branch, "patterns": ENTRY_POINT_PATTERNS})
    return results


def _trace_call_chain(store: Neo4jStore, start_id: str, branch: str,
                       max_depth: int) -> list[str]:
    """
    BFS 追踪 CALLS 链，返回所有涉及节点 id 列表
    使用 Cypher 的变长路径查询（高效）
    """
    result = store.query(f"""
        MATCH path = (start:Function {{id: $start_id}})-[:CALLS*1..{max_depth}]->(end:Function)
        WHERE (end.branch = $branch OR end.branch = 'master')
          AND end.domain <> 'unknown'
        WITH nodes(path) AS path_nodes
        UNWIND path_nodes AS n
        RETURN DISTINCT n.id AS node_id
        LIMIT {MAX_PATH_NODES}
    """, {"start_id": start_id, "branch": branch})

    ids = [start_id] + [r["node_id"] for r in result if r["node_id"] != start_id]
    return ids
