#!/usr/bin/env python3
"""
MCP stdio server — for use with Claude Code
Implements MCP JSON-RPC 2.0 protocol (stdio transport)
Tools: search / context / impact / call-chain / graph-stats
"""

from __future__ import annotations
import sys
import json
import logging
import traceback
from pathlib import Path

# Add project root to sys.path
_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

(_ROOT / "logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [mcp] %(message)s",
    handlers=[logging.FileHandler(_ROOT / "logs" / "mcp_stdio.log")],
)
logger = logging.getLogger(__name__)

# ── Tool definitions ───────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "cig_search",
        "description": (
            "Search for functions in the code graph using natural language. "
            "Returns a ranked list of functions with qualifiedName, file path, and line number. "
            "Use this when you don't know the exact function name and want to find entry points by semantics."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language search query, e.g. 'route deviation handling'"},
                "top_k": {"type": "integer", "description": "Number of results to return (default 10)", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "cig_context",
        "description": (
            "Get 360° context for a function: incoming calls, outgoing calls, parent class, "
            "file location, and domain. "
            "Use this when you know the function name and want to understand its responsibilities and dependencies."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Function name or qualified name, e.g. 'MyClass.handleTap'"},
                "branch": {"type": "string", "description": "Branch name (default: master)", "default": "master"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "cig_impact",
        "description": (
            "Analyze the blast radius of modifying a function: returns all direct and indirect callers "
            "(reverse traversal), ordered by call depth. "
            "Use this to assess change risk or during code review."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Function name or qualified name to analyze"},
                "max_depth": {"type": "integer", "description": "Traversal depth (default: 5)", "default": 5},
                "branch": {"type": "string", "description": "Branch name (default: master)", "default": "master"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "cig_call_chain",
        "description": (
            "Get the downstream call chain of a function: returns the tree of functions it calls "
            "(forward traversal). "
            "Use this to understand a function's execution flow."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Function name or qualified name"},
                "max_depth": {"type": "integer", "description": "Traversal depth (default: 5)", "default": 5},
                "branch": {"type": "string", "description": "Branch name (default: master)", "default": "master"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "cig_graph_stats",
        "description": (
            "Show the current health of the code graph: function count, class count, file count, "
            "edge count, CALLS coverage, top 10 high-risk nodes, and community distribution. "
            "Use this to understand the overall architecture and risk profile of the codebase."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]

# ── Lazy-loaded engine ─────────────────────────────────────────────────────────

_engine = None
_store = None

def _get_engine():
    global _engine, _store
    if _engine is not None:
        return _engine

    import yaml
    from src.graph.store import Neo4jStore
    from src.search.bm25_index import BM25Index
    from src.query.engine import QueryEngine

    cfg_path = _ROOT / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())

    _store = Neo4jStore(cfg["neo4j"]["uri"], cfg["neo4j"]["user"], cfg["neo4j"]["password"])
    _store.connect()

    bm25 = BM25Index()
    idx_path = cfg.get("bm25_index_path", str(_ROOT / "bm25_index.pkl"))
    try:
        bm25.load(idx_path)
    except Exception:
        pass  # BM25 not yet built; search will degrade gracefully

    emb_client = _make_embedding_client(cfg)
    _engine = QueryEngine(_store, bm25, embedding_client=emb_client)
    logger.info("QueryEngine initialized")
    return _engine


def _make_embedding_client(cfg: dict):
    """Create EmbeddingClient from config if API key is available, else None"""
    import os
    emb_cfg = cfg.get("embedding", {})
    api_key = emb_cfg.get("api_key") or os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        return None
    from src.search.embedding_client import EmbeddingClient
    return EmbeddingClient(
        api_key=api_key,
        model=emb_cfg.get("model", "text-embedding-v3"),
        dims=emb_cfg.get("dims", 1024),
    )


# ── Tool execution ─────────────────────────────────────────────────────────────

def _run_tool(name: str, args: dict) -> str:
    engine = _get_engine()

    if name == "cig_search":
        results = engine.bm25.search(args["query"], top_k=args.get("top_k", 10))
        if not results:
            return "No matching functions found."
        ids = [r["id"] for r in results]
        nodes = _store.query(
            "MATCH (f:Function) WHERE f.id IN $ids RETURN f", {"ids": ids}
        )
        id_map = {n["f"]["id"]: n["f"] for n in nodes}
        lines = []
        for r in results:
            node = id_map.get(r["id"], {})
            qname = node.get("qualifiedName", r["id"])
            fpath = (node.get("filePath") or "").split("/")[-1]
            line = node.get("lineStart", 0)
            domain = node.get("domain", "unknown")
            lines.append(
                f"[{r['rank']}] score={r['score']:.1f} | {qname} | {domain} | {fpath}:{line}"
            )
        return "\n".join(lines)

    elif name == "cig_context":
        result = engine.get_context(args["symbol"], branch=args.get("branch", "master"))
        if "error" in result:
            return result["error"]
        func = result.get("node", {})
        out = [
            f"Function: {func.get('qualifiedName')}",
            f"File: {(func.get('filePath') or '').split('/')[-1]}:{func.get('lineStart')}",
            f"Domain: {func.get('domain')}",
            f"Signature: {func.get('signature', '')}",
        ]
        incoming = result.get("incoming_calls", [])
        if incoming:
            out.append(f"\nCalled by ({len(incoming)}):")
            for c in incoming[:8]:
                qname = c.get('qname') or c.get('name') or '?'
                fname = (c.get('filePath') or '').split('/')[-1]
                out.append(f"  <- {qname} [{fname}:{c.get('lineStart')}]")
        outgoing = result.get("outgoing_calls", [])
        if outgoing:
            out.append(f"\nCalls ({len(outgoing)}):")
            for c in outgoing[:8]:
                qname = c.get('qname') or c.get('name') or '?'
                fname = (c.get('filePath') or '').split('/')[-1]
                out.append(f"  -> {qname} [{fname}:{c.get('lineStart')}] (confidence={c.get('confidence')})")
        community = result.get("community")
        if community:
            out.append(f"\nCommunity: {community.get('name')} ({community.get('memberCount')} members)")
        return "\n".join(out)

    elif name == "cig_impact":
        result = engine.get_impact_scope(
            args["symbol"],
            max_depth=args.get("max_depth", 5),
            branch=args.get("branch", "master"),
        )
        if "error" in result:
            return result["error"]
        callers = result.get("callers", [])
        seen: dict = {}
        for c in callers:
            k = c["qualifiedName"]
            if k != result["function"].get("qualifiedName") and k not in seen:
                seen[k] = c
        unique = list(seen.values())
        if not unique:
            return f"No callers found for {args['symbol']} (may be a top-level entry point)."

        from collections import defaultdict
        by_depth: dict = defaultdict(list)
        for c in unique:
            by_depth[c["depth"]].append(c)

        out = [f"Impact analysis: {result['function'].get('qualifiedName')}",
               f"{len(unique)} unique callers across {len(set(c['filePath'].split('/')[-1] for c in unique))} files\n"]
        for d in sorted(by_depth.keys()):
            out.append(f"Depth {d}:")
            for c in by_depth[d][:6]:
                fname = (c.get("filePath") or "").split("/")[-1]
                out.append(f"  {c['qualifiedName']}  [{fname}:{c['lineStart']}]")
            if len(by_depth[d]) > 6:
                out.append(f"  ... +{len(by_depth[d]) - 6} more")
        return "\n".join(out)

    elif name == "cig_call_chain":
        result = engine.get_call_chain(
            args["symbol"],
            max_depth=args.get("max_depth", 5),
            branch=args.get("branch", "master"),
        )
        if "error" in result:
            return result["error"]
        chains = result.get("chains", [])
        if not chains:
            return f"{args['symbol']} has no downstream calls (may be a leaf function)."
        out = [f"Call chain: {result['function'].get('qualifiedName')}\n"]
        for i, chain in enumerate(chains[:10]):
            nodes = chain.get("chain_nodes", [])
            path = " -> ".join(n.get("qualifiedName", "?") for n in nodes)
            out.append(f"Chain {i+1}: {path}")
        return "\n".join(out)

    elif name == "cig_graph_stats":
        meta_rows = _store.query("MATCH (m:Meta {id: 'meta:base'}) RETURN m")
        meta = dict(meta_rows[0]["m"]) if meta_rows else {}
        top_fanin = _store.query("""
            MATCH (f:Function)<-[:CALLS]-(caller)
            WITH f, count(caller) AS fanIn
            WHERE fanIn > 5
            RETURN f.qualifiedName AS name, f.domain AS domain, fanIn
            ORDER BY fanIn DESC LIMIT 10
        """)
        out = [
            "=== Code Intelligence Graph Status ===",
            f"Functions: {meta.get('totalFunctions', 0):,}",
            f"Classes / Structs: {meta.get('totalClasses', 0):,}",
            f"Files: {meta.get('totalFiles', 0):,}",
            f"Edges: {meta.get('totalEdges', 0):,}",
            f"CALLS Coverage: {meta.get('coveragePercent', 0)}%",
            f"Last Updated: {meta.get('lastUpdated', 'N/A')}",
            "\nHigh-Risk Nodes Top 10 (most called):",
        ]
        for r in top_fanin:
            out.append(f"  {r['name']} | {r['domain']} | fanIn={r['fanIn']}")
        return "\n".join(out)

    return f"Unknown tool: {name}"


# ── MCP JSON-RPC protocol handler ─────────────────────────────────────────────

def _handle(request: dict) -> dict | None:
    method = request.get("method", "")
    req_id = request.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "code-intelligence-graph", "version": "1.0.0"},
            },
        }

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {"tools": TOOLS},
        }

    if method == "tools/call":
        params = request.get("params", {})
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        try:
            content = _run_tool(tool_name, arguments)
            return {
                "jsonrpc": "2.0", "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": content}],
                    "isError": False,
                },
            }
        except Exception as e:
            logger.error(traceback.format_exc())
            return {
                "jsonrpc": "2.0", "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"Tool execution error: {e}"}],
                    "isError": True,
                },
            }

    if req_id is not None:
        return {
            "jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    return None


def main():
    (_ROOT / "logs").mkdir(exist_ok=True)
    logger.info("MCP stdio server started")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue

        response = _handle(request)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
