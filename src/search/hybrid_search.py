"""
hybrid_search.py
BM25 + vector semantic RRF hybrid search
- BM25 excels at exact function name matching
- Vector search excels at natural language / cross-language semantic matching
- RRF (Reciprocal Rank Fusion) merges both result sets
"""

from __future__ import annotations
import logging
from typing import Optional

from .bm25_index import BM25Index

logger = logging.getLogger(__name__)

RRF_K = 60  # RRF smoothing constant, standard value is 60


class HybridSearch:
    def __init__(self, bm25: BM25Index, store,
                 embedding_client=None):
        """
        Args:
            bm25: BM25 index
            store: Neo4jStore instance (for vector search and node detail queries)
            embedding_client: EmbeddingClient instance, None means pure BM25 mode
        """
        self.bm25 = bm25
        self.store = store
        self.embedding_client = embedding_client

    def search(self, query: str, top_k: int = 10) -> list[dict]:
        """
        Hybrid search: BM25 + vector, RRF fusion
        Returns [{id, name, qualifiedName, domain, filePath, lineStart,
                  score, bm25_rank, vec_rank}, ...]
        """
        expand = top_k * 3  # candidate count per path

        # ── BM25 ─────────────────────────────────────────────────────────────
        bm25_hits = self.bm25.search(query, top_k=expand)
        bm25_ranks: dict[str, int] = {
            h["id"]: i + 1 for i, h in enumerate(bm25_hits)
        }

        # ── Vector search ────────────────────────────────────────────────────
        vec_ranks: dict[str, int] = {}
        if self.embedding_client:
            try:
                q_emb = self.embedding_client.embed(query)
                vec_hits = self.store.vector_search(q_emb, top_k=expand)
                vec_ranks = {h["id"]: i + 1 for i, h in enumerate(vec_hits)}
            except Exception as e:
                logger.warning(f"Vector search failed, falling back to BM25: {e}")

        # ── RRF fusion ───────────────────────────────────────────────────────
        all_ids = set(bm25_ranks) | set(vec_ranks)
        if not all_ids:
            # Both paths returned nothing, do CONTAINS fallback
            return self._fallback_search(query, top_k)

        rrf_scores: dict[str, float] = {
            id_: (1.0 / (RRF_K + bm25_ranks[id_]) if id_ in bm25_ranks else 0.0)
               + (1.0 / (RRF_K + vec_ranks[id_])  if id_ in vec_ranks  else 0.0)
            for id_ in all_ids
        }
        top_ids = sorted(rrf_scores, key=rrf_scores.__getitem__, reverse=True)[:top_k]

        # ── Fetch node details from Neo4j ────────────────────────────────────
        nodes = self.store.query(
            "MATCH (f:Function) WHERE f.id IN $ids "
            "RETURN f.id AS id, f.name AS name, "
            "f.qualifiedName AS qualifiedName, "
            "f.domain AS domain, f.filePath AS filePath, "
            "f.lineStart AS lineStart",
            {"ids": top_ids},
        )
        for n in nodes:
            n["score"]     = rrf_scores.get(n["id"], 0.0)
            n["bm25_rank"] = bm25_ranks.get(n["id"])
            n["vec_rank"]  = vec_ranks.get(n["id"])

        return sorted(nodes, key=lambda x: x["score"], reverse=True)

    def _fallback_search(self, query: str, top_k: int) -> list[dict]:
        """When both paths return nothing, use Neo4j CONTAINS as last resort"""
        return self.store.query(
            """
            MATCH (f:Function)
            WHERE toLower(f.name) CONTAINS toLower($q)
               OR toLower(f.qualifiedName) CONTAINS toLower($q)
            RETURN f.id AS id, f.name AS name,
                   f.qualifiedName AS qualifiedName,
                   f.domain AS domain, f.filePath AS filePath,
                   f.lineStart AS lineStart, 1.0 AS score
            LIMIT $top_k
            """,
            {"q": query, "top_k": top_k},
        )
