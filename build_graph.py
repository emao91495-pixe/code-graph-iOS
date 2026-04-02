#!/usr/bin/env python3
"""
build_graph.py
Full / incremental build entry point (suitable for CI).

Usage:
  python build_graph.py --full --branch master
  python build_graph.py --files file1.swift file2.swift --branch feature/my-feature
  python build_graph.py --full --path /path/to/ios/project
"""

import argparse
import logging
import os
import sys
import yaml

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [build] %(message)s")


def _load_config_workspace() -> str:
    try:
        with open("config.yaml") as f:
            cfg = yaml.safe_load(f)
        return cfg.get("workspace", {}).get("path", "")
    except Exception:
        return ""


def main():
    parser = argparse.ArgumentParser(description="Code Intelligence Graph Builder")
    parser.add_argument("--full", action="store_true", help="Full build")
    parser.add_argument("--files", nargs="+", help="Incremental: specify file list")
    parser.add_argument("--branch", default="master", help="Branch name")
    parser.add_argument("--path", default=None, help="Workspace path")
    args = parser.parse_args()

    sys.path.insert(0, os.path.dirname(__file__))

    from src.graph.store import Neo4jStore
    from src.indexing.pipeline import Pipeline

    workspace = args.path or os.getenv("WORKSPACE") or _load_config_workspace()
    if not workspace:
        parser.error(
            "Workspace path not set. Use --path, set WORKSPACE env var, "
            "or configure workspace.path in config.yaml."
        )

    store = Neo4jStore()
    store.connect()
    pipeline = Pipeline(store, workspace)

    if args.full:
        stats = pipeline.build_full(args.branch)
        print(f"Full build done: {stats}")
    elif args.files:
        for f in args.files:
            ok = pipeline.build_incremental(f, args.branch)
            print(f"{'ok' if ok else 'failed'} {f}")
    else:
        parser.print_help()

    store.close()


if __name__ == "__main__":
    main()
