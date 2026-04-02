#!/usr/bin/env python3
"""
cli.py
Command-line interface for Code Intelligence Graph.

Usage:
  python cli.py build --path /path/to/your/ios/project
  python cli.py query call-chain MyClass.myMethod
  python cli.py query impact MyClass.myMethod
  python cli.py query context MyClass
  python cli.py search "handle payment failure"
  python cli.py detect-changes --diff HEAD~1
  python cli.py stats
"""

import click
import json
import logging
import os
import yaml

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")


def _load_config_workspace() -> str:
    """Read workspace.path from config.yaml, if available."""
    try:
        with open("config.yaml") as f:
            cfg = yaml.safe_load(f)
        return cfg.get("workspace", {}).get("path", "")
    except Exception:
        return ""


def get_store():
    from src.graph.store import Neo4jStore
    store = Neo4jStore()
    store.connect()
    return store


def get_engine(store=None):
    from src.query.engine import QueryEngine
    from src.search.bm25_index import BM25Index
    if store is None:
        store = get_store()
    bm25 = BM25Index()
    bm25.load("bm25_index.pkl")
    return QueryEngine(store, bm25)


@click.group()
def cli():
    """Code Intelligence Graph CLI"""
    pass


# ── build ──────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--path", default=None, help="Workspace path (defaults to config.yaml)")
@click.option("--branch", default="master", help="Branch name")
@click.option("--file", "single_file", default=None,
              help="Process a single file only (incremental mode)")
def build(path, branch, single_file):
    """Build the code graph (full or incremental)."""
    from src.indexing.pipeline import Pipeline

    store = get_store()

    workspace = path or os.getenv("WORKSPACE") or _load_config_workspace()
    if not workspace:
        raise click.ClickException(
            "Workspace path not set. Use --path, set WORKSPACE env var, "
            "or configure workspace.path in config.yaml."
        )

    pipeline = Pipeline(store, workspace)

    if single_file:
        click.echo(f"Incremental build: {single_file}")
        ok = pipeline.build_incremental(single_file, branch)
        click.echo("done" if ok else "failed")
    else:
        click.echo(f"Full build: {workspace} (branch={branch})")
        stats = pipeline.build_full(branch)
        click.echo(json.dumps(stats, indent=2, ensure_ascii=False))

    store.close()


# ── query ──────────────────────────────────────────────────────────────────────

@cli.group()
def query():
    """Graph query commands"""
    pass


@query.command("call-chain")
@click.argument("function_name")
@click.option("--depth", default=10, help="Max traversal depth")
@click.option("--branch", default="master")
def call_chain(function_name, depth, branch):
    """Query the downstream call chain of a function."""
    engine = get_engine()
    result = engine.get_call_chain(function_name, max_depth=depth, branch=branch)
    _print_result(result)


@query.command("impact")
@click.argument("function_name")
@click.option("--depth", default=5, help="Max traversal depth")
@click.option("--branch", default="master")
def impact(function_name, depth, branch):
    """Query impact scope (who calls this function)."""
    engine = get_engine()
    result = engine.get_impact_scope(function_name, max_depth=depth, branch=branch)
    _print_result(result)


@query.command("context")
@click.argument("symbol_name")
@click.option("--branch", default="master")
def context(symbol_name, branch):
    """Get 360-degree context view of a symbol."""
    engine = get_engine()
    result = engine.get_context(symbol_name, branch=branch)
    _print_result(result)


# ── search ─────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("query_text")
@click.option("--top-k", default=10)
@click.option("--branch", default="master")
def search(query_text, top_k, branch):
    """Natural language search (BM25)."""
    engine = get_engine()
    results = engine.search(query_text, top_k=top_k, branch=branch)
    for r in results:
        score = r.get("score", 0)
        click.echo(f"[{score:.2f}] {r.get('qualifiedName', r.get('name'))} "
                   f"| {r.get('domain', '?')} | {r.get('filePath', '?')}:{r.get('lineStart', '?')}")


# ── detect-changes ─────────────────────────────────────────────────────────────

@cli.command("detect-changes")
@click.option("--diff", "base_ref", default="HEAD~1", help="Base git ref to diff against")
@click.option("--workspace", default=None)
@click.option("--branch", default="master")
def detect_changes_cmd(base_ref, workspace, branch):
    """Analyze git diff impact scope."""
    from src.query.detect_changes import detect_from_git
    store = get_store()
    ws = workspace or os.getenv("WORKSPACE") or _load_config_workspace()
    if not ws:
        raise click.ClickException(
            "Workspace path not set. Use --workspace, set WORKSPACE env var, "
            "or configure workspace.path in config.yaml."
        )
    result = detect_from_git(store, ws, base_ref=base_ref, branch=branch)
    click.echo(result.get("summary", ""))
    click.echo("\n--- Detail ---")
    _print_result(result)
    store.close()


# ── stats ──────────────────────────────────────────────────────────────────────

@cli.command()
def stats():
    """Show graph statistics."""
    store = get_store()
    meta = store.query("MATCH (m:Meta {id: 'meta:base'}) RETURN m")
    if meta:
        m = meta[0]["m"] if "m" in meta[0] else meta[0]
        click.echo(json.dumps(dict(m), indent=2, ensure_ascii=False))
    overlays = store.list_overlays()
    if overlays:
        click.echo(f"\nActive overlays ({len(overlays)}):")
        for o in overlays:
            click.echo(f"  {o['branch']} | {o.get('lastUpdated', '?')}")
    store.close()


# ── helpers ────────────────────────────────────────────────────────────────────

def _print_result(result: dict):
    click.echo(json.dumps(result, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    cli()
