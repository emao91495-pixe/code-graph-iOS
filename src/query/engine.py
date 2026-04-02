"""
engine.py
核心查询引擎：实现 7 个 MCP 工具的底层查询逻辑
"""

from __future__ import annotations
import logging
from typing import Optional
from ..graph.store import Neo4jStore
from ..search.bm25_index import BM25Index

logger = logging.getLogger(__name__)


class QueryEngine:
    def __init__(self, store: Neo4jStore, bm25: BM25Index,
                 embedding_client=None):
        self.store = store
        self.bm25 = bm25
        self._embedding_client = embedding_client
        self._hybrid: Optional[object] = None

    def get_call_chain(self, function_name: str, max_depth: int = 10,
                       branch: str = "master") -> dict:
        """
        调用链：函数调用了什么（向下追踪）
        返回树状结构
        """
        # 找到函数节点
        start = self._find_function(function_name, branch)
        if not start:
            return {"error": f"Function not found: {function_name}"}

        # 变长路径查询
        chain = self.store.query(f"""
            MATCH path = (start:Function {{id: $start_id}})-[:CALLS*1..{max_depth}]->(end:Function)
            WHERE NOT (end)-[:CALLS]->(:Function)
                  OR length(path) = {max_depth}
            WITH path, [n IN nodes(path) | {{
                id: n.id,
                name: n.name,
                qualifiedName: n.qualifiedName,
                domain: n.domain,
                filePath: n.filePath,
                lineStart: n.lineStart
            }}] AS chain_nodes,
            [r IN relationships(path) | {{
                confidence: r.confidence,
                callSite: r.callSite
            }}] AS chain_edges
            RETURN chain_nodes, chain_edges
            LIMIT 20
        """, {"start_id": start["id"]})

        return {
            "function": start,
            "chains": chain[:20],
            "depth": max_depth,
        }

    def get_impact_scope(self, function_name: str, max_depth: int = 5,
                          branch: str = "master") -> dict:
        """
        影响面：谁调用了这个函数（反向追踪）
        用于评估修改风险
        """
        target = self._find_function(function_name, branch)
        if not target:
            return {"error": f"Function not found: {function_name}"}

        callers = self.store.query(f"""
            MATCH path = (caller:Function)-[:CALLS*1..{max_depth}]->(target:Function {{id: $target_id}})
            WITH caller, length(path) AS depth,
                 [n IN nodes(path) | n.qualifiedName] AS call_path
            RETURN caller.id AS id,
                   caller.name AS name,
                   caller.qualifiedName AS qualifiedName,
                   caller.domain AS domain,
                   caller.filePath AS filePath,
                   caller.lineStart AS lineStart,
                   depth,
                   call_path
            ORDER BY depth ASC
            LIMIT 50
        """, {"target_id": target["id"]})

        # 汇总影响的执行流
        processes = self.store.query("""
            MATCH (target:Function {id: $target_id})-[:PART_OF]->(p:Process)
            RETURN p.id AS id, p.name AS name, p.domain AS domain
        """, {"target_id": target["id"]})

        return {
            "function": target,
            "callers": callers,
            "affected_processes": processes,
            "caller_count": len(callers),
        }

    def get_context(self, symbol_name: str, branch: str = "master") -> dict:
        """
        360° 视图：入边 + 出边 + 所属执行流 + 社区
        """
        node = self._find_function(symbol_name, branch) or self._find_class(symbol_name)
        if not node:
            return {"error": f"Symbol not found: {symbol_name}"}

        node_id = node["id"]

        outgoing = self.store.query("""
            MATCH (n {id: $id})-[e:CALLS]->(target)
            WHERE target.name IS NOT NULL
            RETURN target.name AS name, target.qualifiedName AS qname,
                   target.domain AS domain, target.filePath AS filePath,
                   target.lineStart AS lineStart, e.confidence AS confidence,
                   e.callSite AS callSite
            ORDER BY e.confidence DESC, e.callSite
            LIMIT 200
        """, {"id": node_id})

        incoming = self.store.query("""
            MATCH (caller)-[e:CALLS]->(n {id: $id})
            WHERE caller.id <> $id
            RETURN caller.name AS name, caller.qualifiedName AS qname,
                   caller.domain AS domain, caller.filePath AS filePath,
                   caller.lineStart AS lineStart, e.confidence AS confidence
            LIMIT 20
        """, {"id": node_id})

        processes = self.store.query("""
            MATCH (n {id: $id})-[:PART_OF]->(p:Process)
            RETURN p.name AS name, p.domain AS domain
        """, {"id": node_id})

        community = self.store.query("""
            MATCH (n {id: $id})-[:BELONGS_TO]->(c:Community)
            RETURN c.name AS name, c.domain AS domain, c.memberCount AS memberCount
        """, {"id": node_id})

        return {
            "node": node,
            "outgoing_calls": outgoing,
            "incoming_calls": incoming,
            "processes": processes,
            "community": community[0] if community else None,
        }

    def search(self, query: str, top_k: int = 10, branch: str = "master") -> list[dict]:
        """
        Natural language search: BM25 + vector semantic hybrid (HybridSearch + RRF)
        Falls back to pure BM25 if no embedding_client is configured
        """
        return self._get_hybrid().search(query, top_k)

    def _get_hybrid(self):
        """Lazy-load HybridSearch instance"""
        if self._hybrid is None:
            from ..search.hybrid_search import HybridSearch
            self._hybrid = HybridSearch(
                self.bm25, self.store, self._embedding_client
            )
        return self._hybrid

    def get_process(self, process_name: str, branch: str = "master") -> dict:
        """
        执行流查询：返回某业务流程的完整路径
        """
        process = self.store.query("""
            MATCH (p:Process)
            WHERE p.name CONTAINS $name OR p.domain CONTAINS $name
            RETURN p.id AS id, p.name AS name, p.entryPoint AS entryPoint,
                   p.domain AS domain, p.nodeCount AS nodeCount
            LIMIT 5
        """, {"name": process_name})

        if not process:
            return {"error": f"Process not found: {process_name}"}

        # 获取第一个匹配的流程成员
        members = self.store.query("""
            MATCH (f:Function)-[:PART_OF]->(p:Process {id: $pid})
            RETURN f.id AS id, f.name AS name, f.qualifiedName AS qname,
                   f.filePath AS filePath, f.lineStart AS lineStart,
                   f.domain AS domain
            LIMIT 50
        """, {"pid": process[0]["id"]})

        return {
            "process": process[0],
            "members": members,
            "all_matches": process,
        }

    def raw_cypher(self, query: str, params: dict = None) -> list[dict]:
        """原始 Cypher 查询（高级用户）"""
        return self.store.query(query, params or {})

    # ── 工具方法 ──────────────────────────────────────────────────────────────

    def _find_function(self, name: str, branch: str) -> Optional[dict]:
        """按名称（精确或 qualifiedName）查找函数"""
        results = self.store.query("""
            MATCH (f:Function)
            WHERE f.name = $name OR f.qualifiedName = $name
            RETURN f.id AS id, f.name AS name,
                   f.qualifiedName AS qualifiedName,
                   f.domain AS domain, f.filePath AS filePath,
                   f.lineStart AS lineStart, f.signature AS signature
            ORDER BY
                CASE WHEN f.branch = $branch THEN 0 ELSE 1 END
            LIMIT 1
        """, {"name": name, "branch": branch})
        return results[0] if results else None

    def _find_class(self, name: str) -> Optional[dict]:
        results = self.store.query("""
            MATCH (c:Class {name: $name})
            RETURN c.id AS id, c.name AS name, c.kind AS kind,
                   c.domain AS domain, c.filePath AS filePath
            LIMIT 1
        """, {"name": name})
        return results[0] if results else None
