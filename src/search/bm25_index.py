"""
bm25_index.py
BM25 关键词搜索索引
Phase 1 阶段使用，无需 Embedding 模型
"""

from __future__ import annotations
import pickle
import logging
from pathlib import Path
from typing import Optional

try:
    from rank_bm25 import BM25Okapi
    BM25_AVAILABLE = True
except ImportError:
    BM25_AVAILABLE = False

logger = logging.getLogger(__name__)


class BM25Index:
    def __init__(self):
        self._bm25: Optional[object] = None
        self._doc_ids: list[str] = []
        self._docs: list[str] = []

    def build(self, docs: list[str], doc_ids: list[str]) -> None:
        """构建完整索引"""
        self._docs = docs
        self._doc_ids = doc_ids
        if BM25_AVAILABLE and docs:
            tokenized = [self._tokenize(d) for d in docs]
            self._bm25 = BM25Okapi(tokenized)
        logger.info(f"BM25 index built: {len(docs)} documents")

    def add(self, new_docs: list[str], new_ids: list[str]) -> None:
        """增量添加（重建索引，BM25 不支持在线更新）"""
        self._docs.extend(new_docs)
        self._doc_ids.extend(new_ids)
        if BM25_AVAILABLE and self._docs:
            tokenized = [self._tokenize(d) for d in self._docs]
            self._bm25 = BM25Okapi(tokenized)

    def search(self, query: str, top_k: int = 10) -> list[dict]:
        """
        BM25 搜索，返回 [{id, score, rank}] 列表
        """
        if not self._bm25 or not BM25_AVAILABLE:
            return self._fallback_search(query, top_k)

        tokens = self._tokenize(query)
        scores = self._bm25.get_scores(tokens)

        # 排序取 top_k
        ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)[:top_k]
        results = []
        for rank, (idx, score) in enumerate(ranked):
            if score > 0:
                results.append({
                    "id": self._doc_ids[idx],
                    "score": float(score),
                    "rank": rank + 1,
                })
        return results

    def __len__(self) -> int:
        return len(self._doc_ids)

    def save(self, path: str) -> None:
        with open(path, "wb") as f:
            pickle.dump({"docs": self._docs, "ids": self._doc_ids}, f)

    def load(self, path: str) -> bool:
        p = Path(path)
        if not p.exists():
            return False
        try:
            with open(path, "rb") as f:
                data = pickle.load(f)
            self.build(data["docs"], data["ids"])
            return True
        except Exception as e:
            logger.error(f"BM25 load failed: {e}")
            return False

    def _tokenize(self, text: str) -> list[str]:
        """分词：按驼峰、下划线、空格拆分"""
        import re
        # 驼峰拆分：filterCameraMarkers → filter Camera Markers
        text = re.sub(r'([A-Z])', r' \1', text)
        # 按非字母数字拆分
        tokens = re.split(r'[^a-zA-Z0-9\u4e00-\u9fff]+', text.lower())
        return [t for t in tokens if len(t) > 1]

    def _fallback_search(self, query: str, top_k: int) -> list[dict]:
        """rank_bm25 不可用时的简单关键词匹配"""
        query_lower = query.lower()
        results = []
        for i, doc in enumerate(self._docs):
            if query_lower in doc.lower():
                results.append({"id": self._doc_ids[i], "score": 1.0, "rank": len(results) + 1})
                if len(results) >= top_k:
                    break
        return results
