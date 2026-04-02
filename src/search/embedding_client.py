"""
embedding_client.py
DashScope (Alibaba Cloud) embedding client

Supports two modes (auto-selected):
  - OpenAI compatible mode: text-embedding-v3 etc. (via compatible-mode/v1/embeddings)
  - Native DashScope API: qwen3-vl-embedding etc. (via api/v1/services/embeddings/multimodal-embedding)

Supports batch embed + exponential backoff retry
"""

from __future__ import annotations
import time
import logging
import requests

logger = logging.getLogger(__name__)

DASHSCOPE_OPENAI_URL  = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DASHSCOPE_NATIVE_URL  = ("https://dashscope.aliyuncs.com/api/v1/services/"
                         "embeddings/multimodal-embedding/multimodal-embedding")

# Native API models (not using OpenAI compatible layer)
NATIVE_MODELS = {"qwen3-vl-embedding"}

DEFAULT_MODEL = "qwen3-vl-embedding"
DEFAULT_DIMS  = 2560   # qwen3-vl-embedding output dimensions
MAX_BATCH     = 10     # DashScope max 10 per request
MAX_RETRIES   = 3


class EmbeddingClient:
    def __init__(self, api_key: str,
                 model: str = DEFAULT_MODEL,
                 dims: int = DEFAULT_DIMS):
        self._api_key = api_key
        self._model   = model
        self._dims    = dims
        self._native  = model in NATIVE_MODELS

    def embed(self, text: str) -> list[float]:
        """Embed a single text, used for queries"""
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str],
                    batch_size: int = MAX_BATCH) -> list[list[float]]:
        """Batch embed with auto-chunking + exponential backoff retry"""
        results: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            for attempt in range(MAX_RETRIES):
                try:
                    if self._native:
                        embs = self._call_native(batch)
                    else:
                        embs = self._call_openai(batch)
                    results.extend(embs)
                    time.sleep(0.3)   # avoid rate limiting
                    break
                except Exception as e:
                    if attempt == MAX_RETRIES - 1:
                        raise
                    wait = 2 ** attempt
                    logger.warning(
                        f"Embedding batch {i} failed (attempt {attempt + 1}): "
                        f"{e}, retry in {wait}s"
                    )
                    time.sleep(wait)
        return results

    # ── Native DashScope API (qwen3-vl-embedding etc.) ────────────────────────

    def _call_native(self, texts: list[str]) -> list[list[float]]:
        payload = {
            "model": self._model,
            "input": {"contents": [{"text": t} for t in texts]},
        }
        resp = requests.post(
            DASHSCOPE_NATIVE_URL,
            json=payload,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        if "output" not in data:
            raise ValueError(f"DashScope native API error: {data}")
        # Sort by text_index to preserve order
        embeddings = sorted(data["output"]["embeddings"],
                            key=lambda x: x.get("text_index", 0))
        return [e["embedding"] for e in embeddings]

    # ── OpenAI compatible mode (text-embedding-v3 etc.) ───────────────────────

    def _call_openai(self, texts: list[str]) -> list[list[float]]:
        from openai import OpenAI
        if not hasattr(self, "_openai_client"):
            self._openai_client = OpenAI(
                api_key=self._api_key,
                base_url=DASHSCOPE_OPENAI_URL,
            )
        resp = self._openai_client.embeddings.create(
            model=self._model,
            input=texts,
            dimensions=self._dims,
            encoding_format="float",
        )
        return [item.embedding for item in resp.data]
