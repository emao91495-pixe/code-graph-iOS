"""
pipeline.py
6 阶段构建管道：串联 Phase 1-6
支持全量构建和增量构建（仅处理变更文件）
"""

from __future__ import annotations
import os
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Callable, Optional

from tqdm import tqdm

from ..parser import swift_parser, objc_parser
from ..parser.extractor import extract, load_domain_mapping
from ..graph.store import Neo4jStore
from ..graph.schema import update_meta
from ..search.bm25_index import BM25Index
from .import_resolver import resolve_imports
from .community_detector import detect_communities
from .process_tracer import trace_processes

logger = logging.getLogger(__name__)


class Pipeline:
    """6 阶段构建管道"""

    def __init__(self, store: Neo4jStore, workspace: str,
                 domain_mapping_path: str = "domain_mapping.yaml",
                 max_workers: int = 4):
        self.store = store
        self.workspace = Path(workspace).expanduser()
        self.domain_mapping = load_domain_mapping(domain_mapping_path)
        self.max_workers = max_workers
        self.bm25 = BM25Index()

    # ── 全量构建 ──────────────────────────────────────────────────────────────

    def build_full(self, branch: str = "master",
                   on_progress: Optional[Callable[[int, int], None]] = None) -> dict:
        """
        全量构建：扫描所有 Swift/ObjC 文件，运行 6 阶段
        返回统计信息
        """
        logger.info(f"=== Full build started | branch={branch} ===")
        stats = {"branch": branch, "start": datetime.now(timezone.utc).isoformat()}

        # 收集所有文件
        files = self._collect_files()
        total = len(files)
        logger.info(f"Phase 1-2: Parsing {total} files")
        stats["total_files"] = total

        # Phase 1-2: 解析 + 提取
        parse_errors: list[str] = []
        processed = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(self._parse_and_extract, fp, branch): fp
                       for fp in files}
            for future in tqdm(as_completed(futures), total=total, desc="Parsing"):
                fp = futures[future]
                try:
                    result = future.result()
                    if result:
                        self.store.write_extraction(result)
                except Exception as e:
                    parse_errors.append(f"{fp}: {e}")
                    logger.warning(f"Parse failed: {fp}: {e}")
                processed += 1
                if on_progress:
                    on_progress(processed, total)

        stats["parse_errors"] = len(parse_errors)
        logger.info(f"Phase 1-2 done: {processed} files, {len(parse_errors)} errors")

        # Phase 3: 跨文件 import 解析
        logger.info("Phase 3: Resolving imports...")
        resolved = resolve_imports(self.store, branch)
        stats["resolved_calls"] = resolved

        # Phase 4: 社区检测
        logger.info("Phase 4: Community detection...")
        communities = detect_communities(self.store, branch)
        stats["communities"] = communities

        # Phase 5: 执行流预计算
        logger.info("Phase 5: Process tracing...")
        processes = trace_processes(self.store, branch)
        stats["processes"] = processes

        # Phase 6: BM25 索引
        logger.info("Phase 6: Building BM25 index...")
        self._build_bm25_index(branch)
        stats["bm25_indexed"] = len(self.bm25)

        # Phase 7: Embedding index (requires DASHSCOPE_API_KEY)
        logger.info("Phase 7: Building embedding index...")
        embedded = self._build_embedding_index(branch)
        stats["embedded"] = embedded

        # 更新 Meta
        self.store.refresh_meta()
        stats["end"] = datetime.now(timezone.utc).isoformat()
        logger.info(f"=== Full build done: {stats} ===")
        return stats

    # ── 增量构建（单文件）──────────────────────────────────────────────────────

    def build_incremental(self, file_path: str, branch: str) -> bool:
        """
        增量构建：处理单个变更文件
        Phase 1-2 → 写图 → Phase 3 仅对新边解析
        """
        result = self._parse_and_extract(file_path, branch)
        if not result:
            return False

        self.store.write_extraction(result)

        # 只对新写入的未解析边做 import 解析
        resolve_imports(self.store, branch)

        # 更新 BM25 索引（增量）
        self._index_file_bm25(result, branch)

        logger.info(f"Incremental build done: {file_path}")
        return True

    # ── 私有方法 ──────────────────────────────────────────────────────────────

    def _collect_files(self) -> list[str]:
        """收集工作区内所有 Swift/ObjC 文件（排除配置的目录）"""
        from ..parser.extractor import load_domain_mapping
        import yaml

        # 读取排除规则
        try:
            with open("config.yaml") as f:
                config = yaml.safe_load(f)
            excludes = config.get("workspace", {}).get("exclude", [])
        except Exception:
            excludes = ["Pods/", ".git/", ".build/", "build/", "DerivedData/"]

        files = []
        for ext in ("*.swift", "*.m", "*.h"):
            for path in self.workspace.rglob(ext):
                path_str = str(path)
                if not any(excl.rstrip("/") in path_str for excl in excludes):
                    files.append(path_str)
        return files

    def _parse_and_extract(self, file_path: str, branch: str):
        """Phase 1+2：解析单个文件并提取节点/边"""
        try:
            if file_path.endswith(".swift"):
                parse_result = swift_parser.parse_file(file_path)
            elif file_path.endswith((".m", ".h")):
                parse_result = objc_parser.parse_file(file_path)
            else:
                return None

            return extract(parse_result, self.domain_mapping, branch)
        except Exception as e:
            logger.error(f"Parse error {file_path}: {e}")
            return None

    def _build_bm25_index(self, branch: str) -> None:
        """从 Neo4j 读取所有函数，构建 BM25 索引"""
        funcs = self.store.query("""
            MATCH (f:Function)
            WHERE f.branch = $branch OR f.branch = 'master'
            RETURN f.id AS id, f.name AS name, f.qualifiedName AS qname,
                   f.domain AS domain, f.signature AS sig,
                   f.cigTerms AS cig
        """, {"branch": branch})

        docs = []
        doc_ids = []
        for f in funcs:
            # 将函数信息拼成可搜索文本
            terms = [
                f.get("name", ""),
                f.get("qname", ""),
                f.get("domain", ""),
                f.get("sig", ""),
                " ".join(f.get("cig") or []),
            ]
            docs.append(" ".join(t for t in terms if t))
            doc_ids.append(f["id"])

        self.bm25.build(docs, doc_ids)
        self.bm25.save("bm25_index.pkl")
        logger.info(f"BM25 index built: {len(docs)} functions")

    def _build_embedding_index(self, branch: str) -> int:
        """Phase 7: Batch generate embeddings and write to Neo4j Vector Index.
        Only processes nodes without embeddings (supports incremental). Returns count.
        """
        client = self._get_embedding_client()
        if not client:
            logger.info("Phase 7: skipped (no DASHSCOPE_API_KEY configured)")
            return 0

        funcs = self.store.query(
            """
            MATCH (f:Function)
            WHERE (f.branch = $branch OR f.branch = 'master')
              AND f.embedding IS NULL
            RETURN f.id AS id, f.name AS name, f.qualifiedName AS qname,
                   f.domain AS domain, f.signature AS sig
            """,
            {"branch": branch},
        )
        if not funcs:
            logger.info("Phase 7: all functions already have embeddings")
            return 0

        texts = [
            " ".join(filter(None, [
                f.get("name"), f.get("qname"),
                f.get("domain"), f.get("sig"),
            ]))
            for f in funcs
        ]
        ids = [f["id"] for f in funcs]

        logger.info(
            f"Phase 7: embedding {len(funcs)} functions..."
        )
        embeddings = client.embed_batch(texts)
        items = [{"id": id_, "embedding": emb}
                 for id_, emb in zip(ids, embeddings)]
        self.store.store_embeddings(items)
        logger.info(f"Phase 7: done. {len(items)} embeddings stored.")
        return len(items)

    def _get_embedding_client(self):
        """Read API key from env or config, return EmbeddingClient or None"""
        import yaml
        try:
            with open("config.yaml") as f:
                cfg = yaml.safe_load(f)
        except Exception:
            cfg = {}

        emb_cfg = cfg.get("embedding", {})
        api_key = (emb_cfg.get("api_key")
                   or os.environ.get("DASHSCOPE_API_KEY"))
        if not api_key:
            return None

        from ..search.embedding_client import EmbeddingClient
        return EmbeddingClient(
            api_key=api_key,
            model=emb_cfg.get("model", "text-embedding-v3"),
            dims=emb_cfg.get("dims", 1024),
        )

    def _index_file_bm25(self, extraction_result, branch: str) -> None:
        """增量更新 BM25：加入新文件的函数"""
        new_docs = []
        new_ids = []
        for node in extraction_result.nodes:
            if node.label == "Function":
                p = node.props
                terms = [
                    p.get("name", ""),
                    p.get("qualifiedName", ""),
                    p.get("domain", ""),
                    " ".join(p.get("cigTerms") or []),
                ]
                new_docs.append(" ".join(t for t in terms if t))
                new_ids.append(node.id)

        if new_docs:
            self.bm25.add(new_docs, new_ids)
            self.bm25.save("bm25_index.pkl")
