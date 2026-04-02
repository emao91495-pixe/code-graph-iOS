"""
store.py
GraphStore 抽象 + Neo4jStore 实现
支持 upsert（节点/边），branch overlay 隔离，批量写入
"""

from __future__ import annotations
import os
import logging
from abc import ABC, abstractmethod
from typing import Optional
from datetime import datetime, timezone

from neo4j import GraphDatabase, Driver
from neo4j.exceptions import ServiceUnavailable

from .schema import init_schema, update_meta
from ..parser.extractor import ExtractionResult, NodeRecord, EdgeRecord

logger = logging.getLogger(__name__)


class GraphStore(ABC):
    """图存储抽象接口"""

    @abstractmethod
    def upsert_nodes(self, nodes: list[NodeRecord]) -> None: ...

    @abstractmethod
    def upsert_edges(self, edges: list[EdgeRecord]) -> None: ...

    @abstractmethod
    def delete_file_nodes(self, file_path: str, branch: str) -> None: ...

    @abstractmethod
    def query(self, cypher: str, params: dict = None) -> list[dict]: ...

    def write_extraction(self, result: ExtractionResult) -> None:
        """写入一个文件的提取结果（先清旧节点再写新）"""
        self.delete_file_nodes(result.file_path,
                               result.nodes[0].props.get("branch", "master") if result.nodes else "master")
        self.upsert_nodes(result.nodes)
        self.upsert_edges(result.edges)


class Neo4jStore(GraphStore):
    """Neo4j Community 实现"""

    BATCH_SIZE = 500   # 每次批量写入的节点/边数量

    def __init__(self, uri: str = None, user: str = None, password: str = None):
        self.uri = uri or os.getenv("NEO4J_URI", "bolt://localhost:7687")
        self.user = user or os.getenv("NEO4J_USER", "neo4j")
        self.password = password or os.getenv("NEO4J_PASSWORD", "codegraph123")
        self._driver: Optional[Driver] = None

    def connect(self) -> None:
        self._driver = GraphDatabase.driver(
            self.uri, auth=(self.user, self.password),
            max_connection_pool_size=1,
        )
        self._driver.verify_connectivity()
        init_schema(self._driver)
        logger.info(f"Connected to Neo4j: {self.uri}")

    def close(self) -> None:
        if self._driver:
            self._driver.close()

    @property
    def driver(self) -> Driver:
        if not self._driver:
            self.connect()
        return self._driver

    # ── 节点写入 ─────────────────────────────────────────────────────────────

    def upsert_nodes(self, nodes: list[NodeRecord]) -> None:
        for i in range(0, len(nodes), self.BATCH_SIZE):
            batch = nodes[i:i + self.BATCH_SIZE]
            self._upsert_node_batch(batch)

    def _upsert_node_batch(self, batch: list[NodeRecord]) -> None:
        # 按 label 分组
        by_label: dict[str, list] = {}
        for n in batch:
            by_label.setdefault(n.label, []).append({"id": n.id, **n.props})

        with self.driver.session() as session:
            for label, records in by_label.items():
                session.run(f"""
                    UNWIND $records AS r
                    MERGE (n:{label} {{id: r.id}})
                    SET n += r
                """, records=records)

    # ── 边写入 ───────────────────────────────────────────────────────────────

    def upsert_edges(self, edges: list[EdgeRecord]) -> None:
        for i in range(0, len(edges), self.BATCH_SIZE):
            batch = edges[i:i + self.BATCH_SIZE]
            self._upsert_edge_batch(batch)

    # id 前缀 → Neo4j label 映射
    _PREFIX_LABEL = {"file": "File", "class": "Class", "func": "Function"}

    @staticmethod
    def _label_from_id(node_id: str) -> str:
        prefix = node_id.split(":")[0]
        return Neo4jStore._PREFIX_LABEL.get(prefix, "")

    def _upsert_edge_batch(self, batch: list[EdgeRecord]) -> None:
        # 按 (rel, src_label, dst_label) 分组，确保 MATCH 走索引
        groups: dict[tuple, list] = {}
        for e in batch:
            src_lbl = self._label_from_id(e.src_id)
            dst_lbl = self._label_from_id(e.dst_id)
            key = (e.rel, src_lbl, dst_lbl)
            groups.setdefault(key, []).append({
                "src": e.src_id, "dst": e.dst_id, **e.props
            })

        with self.driver.session() as session:
            for (rel, src_lbl, dst_lbl), records in groups.items():
                src_match = f"(src:{src_lbl} {{id: r.src}})" if src_lbl else "(src {id: r.src})"
                if rel == "CALLS":
                    # dst 可能是未解析占位符，stub 不加标签，避免污染 :Function 节点
                    dst_merge = "(dst {id: r.dst})"
                    session.run(f"""
                        UNWIND $records AS r
                        MATCH {src_match}
                        MERGE {dst_merge}
                        MERGE (src)-[e:{rel}]->(dst)
                        SET e += r
                    """, records=records)
                else:
                    dst_match = f"(dst:{dst_lbl} {{id: r.dst}})" if dst_lbl else "(dst {id: r.dst})"
                    session.run(f"""
                        UNWIND $records AS r
                        MATCH {src_match}
                        MATCH {dst_match}
                        MERGE (src)-[e:{rel}]->(dst)
                        SET e += r
                    """, records=records)

    # ── 删除（增量更新时先清旧数据）──────────────────────────────────────────

    def delete_file_nodes(self, file_path: str, branch: str) -> None:
        """删除某文件在某分支下的所有节点和关联边"""
        with self.driver.session() as session:
            session.run("""
                MATCH (n {filePath: $fp, branch: $branch})
                DETACH DELETE n
            """, fp=file_path, branch=branch)
            # 删除 File 节点本身
            session.run("""
                MATCH (f:File {path: $fp, branch: $branch})
                DETACH DELETE f
            """, fp=file_path, branch=branch)

    # ── 查询 ─────────────────────────────────────────────────────────────────

    def query(self, cypher: str, params: dict = None) -> list[dict]:
        with self.driver.session() as session:
            result = session.run(cypher, **(params or {}))
            return [dict(r) for r in result]

    # ── 元数据更新 ────────────────────────────────────────────────────────────

    def refresh_meta(self) -> None:
        """重新统计并更新 Meta 节点"""
        with self.driver.session() as session:
            counts = session.run("""
                MATCH (f:Function) WITH count(f) AS funcs
                MATCH (c:Class) WITH funcs, count(c) AS classes
                MATCH (fi:File) WITH funcs, classes, count(fi) AS files
                MATCH ()-[e]->() WITH funcs, classes, files, count(e) AS edges
                RETURN funcs, classes, files, edges
            """).single()

            # 计算 CALLS 覆盖率：有出向 CALLS 边的函数 / 总函数数
            cov_row = session.run("""
                MATCH (f:Function)
                OPTIONAL MATCH (f)-[r:CALLS]->()
                WITH f, count(r) AS callCount
                RETURN
                    count(f) AS total,
                    sum(CASE WHEN callCount > 0 THEN 1 ELSE 0 END) AS covered
            """).single()

        total = cov_row["total"] if cov_row else 0
        covered = cov_row["covered"] if cov_row else 0
        coverage = round((covered / total * 100), 1) if total > 0 else 0.0

        update_meta(
            self.driver,
            lastUpdated=datetime.now(timezone.utc).isoformat(),
            totalFunctions=counts["funcs"] if counts else 0,
            totalClasses=counts["classes"] if counts else 0,
            totalFiles=counts["files"] if counts else 0,
            totalEdges=counts["edges"] if counts else 0,
            coveragePercent=coverage,
        )

    # ── Overlay 管理 ──────────────────────────────────────────────────────────

    def upsert_overlay_meta(self, branch: str, commit_hash: str) -> None:
        """记录 branch overlay 的元数据"""
        with self.driver.session() as session:
            session.run("""
                MERGE (o:OverlayMeta {branch: $branch})
                SET o.commitHash = $hash,
                    o.lastUpdated = $ts
            """, branch=branch, hash=commit_hash,
                ts=datetime.now(timezone.utc).isoformat())

    def delete_overlay(self, branch: str) -> None:
        """PR 合并后清理 branch overlay 的所有节点"""
        with self.driver.session() as session:
            session.run("""
                MATCH (n {branch: $branch})
                WHERE NOT $branch = 'master'
                DETACH DELETE n
            """, branch=branch)
            session.run("""
                MATCH (o:OverlayMeta {branch: $branch})
                DELETE o
            """, branch=branch)
        logger.info(f"Deleted overlay for branch: {branch}")

    def list_overlays(self) -> list[dict]:
        """列出所有活跃 overlay"""
        return self.query("""
            MATCH (o:OverlayMeta)
            RETURN o.branch AS branch, o.commitHash AS commitHash,
                   o.lastUpdated AS lastUpdated
            ORDER BY o.lastUpdated DESC
        """)

    # ── Vector search ────────────────────────────────────────────────────────

    def vector_search(self, query_embedding: list[float],
                      top_k: int = 30) -> list[dict]:
        """Vector index nearest neighbor search, returns [{id, score}]"""
        rows = self.query(
            """
            CALL db.index.vector.queryNodes('func_embedding', $top_k, $emb)
            YIELD node AS f, score
            RETURN f.id AS id, score
            ORDER BY score DESC
            """,
            {"top_k": top_k, "emb": query_embedding},
        )
        return [{"id": r["id"], "score": r["score"]}
                for r in rows if r.get("id")]

    def store_embeddings(self, items: list[dict]) -> None:
        """Batch write embeddings to Function nodes.
        items: [{id: str, embedding: list[float]}]
        """
        batch_size = 500
        for i in range(0, len(items), batch_size):
            self.query(
                """
                UNWIND $items AS item
                MATCH (f:Function {id: item.id})
                SET f.embedding = item.embedding
                """,
                {"items": items[i:i + batch_size]},
            )

    # ── 健康检查 ──────────────────────────────────────────────────────────────

    def is_healthy(self) -> bool:
        try:
            self.driver.verify_connectivity()
            return True
        except Exception:
            return False
