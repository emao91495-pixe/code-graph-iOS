"""
community_detector.py - Phase 4
使用 Louvain 社区检测算法自动发现功能聚类
依赖 Neo4j Graph Data Science (GDS) 插件
"""

from __future__ import annotations
import logging
from collections import Counter
from ..graph.store import Neo4jStore

logger = logging.getLogger(__name__)

GDS_AVAILABLE_QUERY = "RETURN gds.version() AS version"


def detect_communities(store: Neo4jStore, branch: str = "master") -> int:
    """
    运行社区检测，将结果写入 Community 节点和 BELONGS_TO 边
    返回发现的社区数量
    """
    # 检查是否有 CALLS 边（没有就直接走 fallback，GDS 会报错）
    calls_count = store.query("MATCH ()-[r:CALLS]->() RETURN count(r) AS cnt")
    if not calls_count or calls_count[0].get("cnt", 0) == 0:
        logger.info("Phase 4: No CALLS edges found, using domain-based fallback")
        return _fallback_community_detection(store, branch)

    # 检查 GDS 是否可用
    if not _gds_available(store):
        logger.warning("Phase 4: GDS plugin not available, using fallback community detection")
        return _fallback_community_detection(store, branch)

    return _gds_louvain(store, branch)


def _gds_available(store: Neo4jStore) -> bool:
    try:
        store.query(GDS_AVAILABLE_QUERY)
        return True
    except Exception:
        return False


def _gds_louvain(store: Neo4jStore, branch: str) -> int:
    """使用 GDS Louvain 算法（Neo4j GDS 插件）"""
    logger.info("Phase 4: Running Louvain community detection via GDS")

    with store.driver.session() as session:
        # 创建临时图投影
        session.run("""
            CALL gds.graph.project(
                'cig-graph',
                'Function',
                {
                    CALLS: {
                        orientation: 'UNDIRECTED',
                        properties: 'confidence'
                    }
                }
            )
        """)

        # 运行 Louvain
        result = session.run("""
            CALL gds.louvain.write('cig-graph', {
                writeProperty: 'communityId',
                relationshipWeightProperty: 'confidence'
            })
            YIELD communityCount, modularity
            RETURN communityCount, modularity
        """).single()

        community_count = result["communityCount"] if result else 0
        modularity = result["modularity"] if result else 0.0
        logger.info(f"Louvain: {community_count} communities, modularity={modularity:.3f}")

        # 清理投影
        session.run("CALL gds.graph.drop('cig-graph')")

    # 将 communityId 转化为 Community 节点
    _materialize_communities(store, branch)
    return community_count


def _fallback_community_detection(store: Neo4jStore, branch: str) -> int:
    """
    GDS 不可用时的 fallback：基于 domain 分组作为社区
    不如 Louvain 精细，但不需要 GDS 插件
    """
    logger.info("Phase 4: Fallback - using domain-based community grouping")

    domains = store.query("""
        MATCH (f:Function)
        WHERE f.domain IS NOT NULL AND f.domain <> 'unknown'
        RETURN f.domain AS domain, count(f) AS cnt
        ORDER BY cnt DESC
    """)

    community_count = 0
    for d in domains:
        domain = d["domain"]
        cnt = d["cnt"]
        community_id = f"community:{domain}"

        # 创建 Community 节点
        store.upsert_nodes([])  # 通过直接 query 写入
        with store.driver.session() as session:
            session.run("""
                MERGE (c:Community {id: $id})
                SET c.name = $name,
                    c.domain = $domain,
                    c.memberCount = $cnt,
                    c.branch = $branch,
                    c.source = 'domain_fallback'
            """, id=community_id, name=domain, domain=domain, cnt=cnt, branch=branch)

            # 创建 BELONGS_TO 边
            session.run("""
                MATCH (f:Function {domain: $domain, branch: $branch})
                MATCH (c:Community {id: $cid})
                MERGE (f)-[:BELONGS_TO]->(c)
            """, domain=domain, branch=branch, cid=community_id)

        community_count += 1

    logger.info(f"Phase 4 (fallback): {community_count} domain-based communities")
    return community_count


def _materialize_communities(store: Neo4jStore, branch: str) -> None:
    """将 Function.communityId 物化为 Community 节点和 BELONGS_TO 边"""
    # 获取所有社区 ID 和 domain 分布
    members = store.query("""
        MATCH (f:Function)
        WHERE f.communityId IS NOT NULL AND f.branch = $branch
        RETURN f.communityId AS cid, f.domain AS domain
    """, {"branch": branch})

    if not members:
        return

    # 统计每个社区的主要 domain
    cid_domains: dict[int, list[str]] = {}
    for m in members:
        cid_domains.setdefault(m["cid"], []).append(m["domain"] or "unknown")

    with store.driver.session() as session:
        for cid, domain_list in cid_domains.items():
            # 取出现最多的 domain
            top_domain = Counter(domain_list).most_common(1)[0][0]
            community_node_id = f"community:{branch}:{cid}"

            session.run("""
                MERGE (c:Community {id: $id})
                SET c.communityId = $cid,
                    c.domain = $domain,
                    c.memberCount = $count,
                    c.branch = $branch,
                    c.source = 'louvain'
            """, id=community_node_id, cid=cid,
                domain=top_domain, count=len(domain_list), branch=branch)

            # BELONGS_TO 边
            session.run("""
                MATCH (f:Function {communityId: $cid, branch: $branch})
                MATCH (c:Community {id: $cid_node})
                MERGE (f)-[:BELONGS_TO]->(c)
            """, cid=cid, branch=branch, cid_node=community_node_id)
