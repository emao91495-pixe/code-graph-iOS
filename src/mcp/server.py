"""
server.py
FastAPI MCP Server providing:
  - 7 MCP tool endpoints (/api/mcp/*)
  - Watcher push endpoints (/api/watcher/*)
  - Dashboard (GET /dashboard)
  - Stats endpoints (/api/stats/health + /api/stats/risk)
"""

from __future__ import annotations
import os
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Code Intelligence Graph API", version="1.0.0")

# ── Fix imports when running via uvicorn directly ─────────────────────────────

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(_ROOT))


# ── Global instances (lazy-loaded) ───────────────────────────────────────────

_store = None
_engine = None
_pipeline = None
_watcher_heartbeats: dict[str, dict] = {}


def get_store():
    global _store
    if _store is None:
        from src.graph.store import Neo4jStore
        _store = Neo4jStore()
        _store.connect()
    return _store


def get_engine():
    global _engine
    if _engine is None:
        from src.query.engine import QueryEngine
        from src.search.bm25_index import BM25Index
        bm25 = BM25Index()
        bm25_path = os.getenv("BM25_INDEX_PATH", str(_ROOT / "bm25_index.pkl"))
        try:
            bm25.load(bm25_path)
        except Exception:
            pass  # BM25 not yet built; search will degrade gracefully
        emb_client = _make_embedding_client()
        _engine = QueryEngine(get_store(), bm25, embedding_client=emb_client)
    return _engine


def _make_embedding_client():
    """Create EmbeddingClient from config if API key is available, else None"""
    import yaml
    try:
        cfg = yaml.safe_load((_ROOT / "config.yaml").read_text())
    except Exception:
        cfg = {}
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


def get_pipeline():
    global _pipeline
    if _pipeline is None:
        from src.indexing.pipeline import Pipeline
        workspace = os.getenv("WORKSPACE")
        if not workspace:
            raise ValueError(
                "WORKSPACE environment variable is not set. "
                "Set it to the absolute path of your iOS project."
            )
        _pipeline = Pipeline(get_store(), workspace)
    return _pipeline


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    store = get_store()
    return {"status": "ok" if store.is_healthy() else "degraded",
            "neo4j": store.is_healthy()}


# ── MCP tool endpoints ────────────────────────────────────────────────────────

class CallChainRequest(BaseModel):
    function_name: str
    max_depth: int = 10
    branch: str = "master"

class ImpactRequest(BaseModel):
    function_name: str
    max_depth: int = 5
    branch: str = "master"

class ContextRequest(BaseModel):
    symbol_name: str
    branch: str = "master"

class SearchRequest(BaseModel):
    query: str
    top_k: int = 10
    branch: str = "master"

class ProcessRequest(BaseModel):
    process_name: str
    branch: str = "master"

class CypherRequest(BaseModel):
    query: str
    params: dict = {}

class DetectChangesRequest(BaseModel):
    diff_hunks: Optional[str] = None
    base_ref: str = "HEAD~1"
    branch: str = "master"


@app.post("/api/mcp/call-chain")
def mcp_call_chain(req: CallChainRequest):
    return get_engine().get_call_chain(req.function_name, req.max_depth, req.branch)


@app.post("/api/mcp/impact-scope")
def mcp_impact_scope(req: ImpactRequest):
    return get_engine().get_impact_scope(req.function_name, req.max_depth, req.branch)


@app.post("/api/mcp/context")
def mcp_context(req: ContextRequest):
    return get_engine().get_context(req.symbol_name, req.branch)


@app.post("/api/mcp/search")
def mcp_search(req: SearchRequest):
    return get_engine().search(req.query, req.top_k, req.branch)


@app.post("/api/mcp/process")
def mcp_process(req: ProcessRequest):
    return get_engine().get_process(req.process_name, req.branch)


@app.post("/api/mcp/cypher")
def mcp_cypher(req: CypherRequest):
    return get_store().query(req.query, req.params)


@app.post("/api/mcp/detect-changes")
def mcp_detect_changes(req: DetectChangesRequest, background: BackgroundTasks):
    from src.query.detect_changes import detect_from_diff, detect_from_git
    workspace = os.getenv("WORKSPACE", ".")
    if req.diff_hunks:
        return detect_from_diff(get_store(), req.diff_hunks, req.branch)
    return detect_from_git(get_store(), workspace, req.base_ref, req.branch)


# ── Watcher push endpoints ────────────────────────────────────────────────────

class FileChangedRequest(BaseModel):
    filePath: str
    branch: str
    repo: str
    timestamp: Optional[str] = None


@app.post("/api/watcher/file-changed")
async def watcher_file_changed(req: FileChangedRequest, background: BackgroundTasks):
    """Receive file change notification and trigger incremental build in background."""
    background.add_task(_async_incremental_build, req.filePath, req.branch)
    return {"status": "queued", "file": req.filePath}


def _async_incremental_build(file_path: str, branch: str):
    try:
        get_pipeline().build_incremental(file_path, branch)
    except Exception as e:
        logger.error(f"Incremental build failed: {file_path}: {e}")


@app.post("/api/watcher/branch-switch")
def watcher_branch_switch(data: dict):
    branch = data.get("branch", "master")
    logger.info(f"Branch switch notified: {branch}")
    get_store().upsert_overlay_meta(branch, "unknown")
    return {"status": "ok", "branch": branch}


@app.post("/api/watcher/heartbeat")
def watcher_heartbeat(data: dict):
    repo = data.get("repo", "unknown")
    _watcher_heartbeats[repo] = {
        "branch": data.get("branch"),
        "lastSeen": datetime.now(timezone.utc).isoformat(),
    }
    return {"status": "ok"}


# ── Stats API (Dashboard data source) ────────────────────────────────────────

@app.get("/api/stats/health")
def stats_health():
    store = get_store()

    meta_rows = store.query("MATCH (m:Meta {id: 'meta:base'}) RETURN m")
    meta = dict(meta_rows[0]["m"]) if meta_rows else {}

    overlays = store.list_overlays()
    parse_errors = meta.get("parseErrors", [])

    watchers = [
        {"repo": repo, **info}
        for repo, info in _watcher_heartbeats.items()
    ]

    return {
        "lastUpdated": meta.get("lastUpdated"),
        "commitHash": meta.get("commitHash"),
        "totalNodes": {
            "functions": meta.get("totalFunctions", 0),
            "classes": meta.get("totalClasses", 0),
            "files": meta.get("totalFiles", 0),
        },
        "totalEdges": meta.get("totalEdges", 0),
        "coveragePercent": meta.get("coveragePercent", 0),
        "parseErrors": parse_errors[:20],
        "activeOverlays": overlays,
        "watchers": watchers,
        "neo4jHealthy": store.is_healthy(),
    }


@app.get("/api/stats/risk")
def stats_risk():
    store = get_store()

    top_fanin = store.query("""
        MATCH (f:Function)<-[:CALLS]-(caller)
        WHERE f.filePath IS NOT NULL
        WITH f, count(caller) AS fanIn
        WHERE fanIn > 2
        RETURN f.name AS name, f.qualifiedName AS qualifiedName,
               f.domain AS domain, f.filePath AS filePath,
               f.lineStart AS lineStart, fanIn
        ORDER BY fanIn DESC
        LIMIT 20
    """)

    top_coupling = store.query("""
        MATCH (src:Function)-[:CALLS]->(dst:Function)
        WHERE src.filePath <> dst.filePath
        WITH src.filePath AS file, count(*) AS crossModuleCalls
        RETURN file, crossModuleCalls
        ORDER BY crossModuleCalls DESC
        LIMIT 10
    """)

    low_confidence = store.query("""
        MATCH (src:Function)-[e:CALLS]->(dst:Function)
        WHERE e.confidence < 50
        RETURN src.name AS srcName, dst.name AS dstName,
               src.filePath AS srcFile, e.confidence AS confidence
        LIMIT 30
    """)

    communities = store.query("""
        MATCH (c:Community)
        RETURN c.id AS id, c.domain AS domain,
               c.memberCount AS memberCount, c.source AS source
        ORDER BY c.memberCount DESC
        LIMIT 20
    """)

    return {
        "topFanIn": top_fanin,
        "topCoupling": top_coupling,
        "lowConfidenceEdges": low_confidence,
        "communities": communities,
    }


# ── Call graph subgraph API (D3.js data source) ───────────────────────────────

@app.get("/api/graph/subgraph")
def graph_subgraph(name: str, depth: int = 2):
    """
    Return the subgraph centered on the given function within `depth` hops (D3-compatible JSON).
    nodes: [{id, name, domain, filePath, lineStart, fanIn, isCenter}]
    links: [{source, target, confidence, via}]
    """
    store = get_store()

    center_rows = store.query(
        "MATCH (f:Function) WHERE f.name = $n OR f.qualifiedName = $n "
        "RETURN f LIMIT 1",
        params={"n": name},
    )
    if not center_rows:
        return JSONResponse({"nodes": [], "links": [], "error": f"Function '{name}' not found"})

    center = center_rows[0]["f"]
    center_id = center["id"]

    _d = max(1, min(int(depth), 6))
    neighbor_rows = store.query(
        f"""
        MATCH (start:Function {{id: $cid}})
        OPTIONAL MATCH (start)-[:CALLS*1..{_d}]->(out:Function)
          WHERE out.filePath IS NOT NULL
        OPTIONAL MATCH (inc:Function)-[:CALLS*1..2]->(start)
          WHERE inc.filePath IS NOT NULL
        WITH start,
             collect(DISTINCT out) AS outs,
             collect(DISTINCT inc) AS incs
        WITH [start] + outs + incs AS all_nodes
        UNWIND all_nodes AS n
        WITH DISTINCT n
        OPTIONAL MATCH (n)<-[:CALLS]-(inbound:Function)
        RETURN n AS func, count(inbound) AS fanIn
        LIMIT 80
        """,
        params={"cid": center_id},
    )

    nodes = []
    node_ids = set()
    for row in neighbor_rows:
        f = row["func"]
        if not f or not f.get("id"):
            continue
        nodes.append({
            "id":            f["id"],
            "name":          f.get("name", ""),
            "qualifiedName": f.get("qualifiedName", ""),
            "domain":        f.get("domain") or "unknown",
            "filePath":      f.get("filePath", ""),
            "lineStart":     f.get("lineStart", 0),
            "fanIn":         row["fanIn"] or 0,
            "isCenter":      f["id"] == center_id,
        })
        node_ids.add(f["id"])

    if node_ids:
        edge_rows = store.query(
            """
            MATCH (a:Function)-[e:CALLS]->(b:Function)
            WHERE a.id IN $ids AND b.id IN $ids
            RETURN a.id AS source, b.id AS target,
                   e.confidence AS confidence, e.via AS via
            """,
            params={"ids": list(node_ids)},
        )
    else:
        edge_rows = []

    links = [
        {
            "source":     r["source"],
            "target":     r["target"],
            "confidence": r.get("confidence") or 90,
            "via":        r.get("via"),
        }
        for r in edge_rows
    ]

    return {"nodes": nodes, "links": links, "center": center_id}


# ── Dashboard HTML ────────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Code Intelligence Graph Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script src="https://cdn.jsdelivr.net/npm/d3@7"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #0f1117; color: #e1e4e8; }
  .header { padding: 16px 30px; border-bottom: 1px solid #21262d;
            display: flex; align-items: center; gap: 12px; }
  .header h1 { font-size: 20px; font-weight: 600; }
  .badge { background: #238636; color: #fff; padding: 2px 8px;
           border-radius: 12px; font-size: 12px; }
  .badge.error { background: #da3633; }
  .tabs { display: flex; padding: 0 30px; border-bottom: 1px solid #21262d; gap: 4px; }
  .tab-btn { background: none; border: none; color: #8b949e; padding: 10px 18px;
             font-size: 14px; cursor: pointer; border-bottom: 2px solid transparent;
             transition: color .15s; }
  .tab-btn:hover { color: #c9d1d9; }
  .tab-btn.active { color: #58a6ff; border-bottom-color: #58a6ff; }
  .tab-pane { display: none; }
  .tab-pane.active { display: block; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
          gap: 16px; padding: 24px 30px; }
  .card { background: #161b22; border: 1px solid #21262d; border-radius: 8px; padding: 20px; }
  .card h3 { font-size: 12px; color: #8b949e; text-transform: uppercase;
             letter-spacing: 0.5px; margin-bottom: 8px; }
  .card .value { font-size: 32px; font-weight: 700; color: #58a6ff; }
  .card .sub { font-size: 12px; color: #8b949e; margin-top: 4px; }
  .section { padding: 0 30px 24px; }
  .section h2 { font-size: 16px; margin-bottom: 14px; color: #c9d1d9; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; padding: 8px 12px; color: #8b949e;
       border-bottom: 1px solid #21262d; font-weight: 500; }
  td { padding: 8px 12px; border-bottom: 1px solid #21262d; }
  tr:hover td { background: #21262d; }
  .domain-tag { background: #1f3a5f; color: #79c0ff; padding: 1px 6px;
                border-radius: 4px; font-size: 11px; }
  .conf-low { color: #f85149; }
  .conf-med { color: #e3b341; }
  .status-dot { width: 8px; height: 8px; border-radius: 50%;
                display: inline-block; margin-right: 6px; }
  .status-dot.ok { background: #3fb950; }
  .status-dot.warn { background: #e3b341; }
  .refresh-btn { margin-left: auto; background: #21262d; border: 1px solid #30363d;
                 color: #c9d1d9; padding: 6px 14px; border-radius: 6px;
                 cursor: pointer; font-size: 13px; }
  .refresh-btn:hover { background: #30363d; }
  .chart-wrap { width: 100%; max-width: 420px; margin: 0 auto; }
  .graph-toolbar { display: flex; gap: 10px; align-items: center;
                   padding: 16px 30px 8px; flex-wrap: wrap; }
  .graph-toolbar input { background: #161b22; border: 1px solid #30363d; color: #e1e4e8;
                          padding: 7px 12px; border-radius: 6px; font-size: 13px; width: 280px; }
  .graph-toolbar input:focus { outline: none; border-color: #58a6ff; }
  .graph-toolbar select { background: #161b22; border: 1px solid #30363d; color: #e1e4e8;
                           padding: 7px 10px; border-radius: 6px; font-size: 13px; }
  .btn-primary { background: #1f6feb; border: 1px solid #388bfd; color: #fff;
                 padding: 7px 16px; border-radius: 6px; cursor: pointer; font-size: 13px; }
  .btn-primary:hover { background: #388bfd; }
  #graph-svg { display: block; width: 100%; height: 540px; background: #0d1117;
               border: 1px solid #21262d; border-radius: 8px; margin: 0 30px;
               width: calc(100% - 60px); }
  #graph-info { margin: 10px 30px; padding: 12px 16px; background: #161b22;
                border: 1px solid #21262d; border-radius: 6px; font-size: 13px;
                min-height: 42px; display: none; }
  #graph-legend { display: flex; flex-wrap: wrap; gap: 10px; padding: 8px 30px 16px;
                  font-size: 12px; }
  .legend-item { display: flex; align-items: center; gap: 5px; }
  .legend-dot { width: 10px; height: 10px; border-radius: 50%; }
  .blast-toolbar { display: flex; gap: 10px; align-items: center; padding: 16px 30px 8px; }
  .blast-toolbar input { background: #161b22; border: 1px solid #30363d; color: #e1e4e8;
                          padding: 7px 12px; border-radius: 6px; font-size: 13px; width: 320px; }
  .blast-toolbar input:focus { outline: none; border-color: #58a6ff; }
  .depth-badge { background: #21262d; color: #8b949e; padding: 1px 7px;
                 border-radius: 10px; font-size: 11px; }
  .depth-0 { color: #f85149; font-weight: 600; }
  .depth-1 { color: #e3b341; }
  .depth-2 { color: #79c0ff; }
</style>
</head>
<body>

<div class="header">
  <h1>Code Intelligence Graph</h1>
  <span class="badge" id="neo4j-badge">Neo4j &#9679;</span>
  <button class="refresh-btn" onclick="loadAll()">&#8635; Refresh</button>
</div>

<div class="tabs">
  <button class="tab-btn active" onclick="switchTab('stats', this)">&#128202; Stats</button>
  <button class="tab-btn" onclick="switchTab('graph', this)">&#128279; Call Graph</button>
  <button class="tab-btn" onclick="switchTab('blast', this)">&#128165; Impact Analysis</button>
</div>

<!-- Tab 1: Stats -->
<div id="tab-stats" class="tab-pane active">

<div class="grid">
  <div class="card"><h3>Functions</h3><div class="value" id="s-funcs">-</div></div>
  <div class="card"><h3>Classes / Structs</h3><div class="value" id="s-classes">-</div></div>
  <div class="card"><h3>Files</h3><div class="value" id="s-files">-</div></div>
  <div class="card"><h3>Edges</h3><div class="value" id="s-edges">-</div></div>
  <div class="card"><h3>CALLS Coverage</h3><div class="value" id="s-coverage">-</div></div>
  <div class="card"><h3>Active Overlays</h3><div class="value" id="s-overlays">-</div></div>
  <div class="card"><h3>Parse Errors</h3><div class="value" id="s-errors">-</div>
    <div class="sub" id="s-updated">-</div></div>
</div>

<div class="section">
  <h2>Watcher Status</h2>
  <table>
    <thead><tr><th>Repo</th><th>Branch</th><th>Last Heartbeat</th><th>Status</th></tr></thead>
    <tbody id="watcher-table"></tbody>
  </table>
</div>

<div class="section">
  <h2>High-Risk Nodes Top 20 (most called)</h2>
  <table>
    <thead><tr><th>Function</th><th>Domain</th><th>Fan-In</th><th>Location</th></tr></thead>
    <tbody id="fanin-table"></tbody>
  </table>
</div>

<div class="section">
  <h2>Community Distribution</h2>
  <div class="chart-wrap"><canvas id="community-chart"></canvas></div>
</div>

<div class="section">
  <h2>Low-Confidence Edges (Swift &#8596; ObjC, confidence &lt; 50)</h2>
  <table>
    <thead><tr><th>Caller</th><th>Callee</th><th>Confidence</th><th>Source File</th></tr></thead>
    <tbody id="lowconf-table"></tbody>
  </table>
</div>

</div><!-- end tab-stats -->

<!-- Tab 2: Call Graph -->
<div id="tab-graph" class="tab-pane">

<div class="graph-toolbar">
  <input id="graph-input" type="text" placeholder="Enter function name, e.g. viewDidLoad"
         onkeydown="if(event.key==='Enter') renderGraph()">
  <select id="graph-depth">
    <option value="1">Depth 1</option>
    <option value="2" selected>Depth 2</option>
    <option value="3">Depth 3</option>
  </select>
  <button class="btn-primary" onclick="renderGraph()">Render</button>
  <span style="font-size:12px;color:#8b949e;margin-left:8px">
    Node size = fan-in &nbsp;|&nbsp; Edge thickness = confidence &nbsp;|&nbsp; Drag / scroll to zoom
  </span>
</div>

<svg id="graph-svg"></svg>
<div id="graph-info"></div>
<div id="graph-legend"></div>

</div><!-- end tab-graph -->

<!-- Tab 3: Impact Analysis -->
<div id="tab-blast" class="tab-pane">

<div class="blast-toolbar">
  <input id="blast-input" type="text" placeholder="Enter function name, e.g. processPayment"
         onkeydown="if(event.key==='Enter') runBlast()">
  <button class="btn-primary" onclick="runBlast()">Analyze Impact</button>
  <span id="blast-summary" style="font-size:13px;color:#8b949e;margin-left:10px"></span>
</div>

<div class="section" style="padding-top:16px">
  <h2>Affected Functions (reverse call chain)</h2>
  <table>
    <thead>
      <tr>
        <th>Depth</th><th>Function</th><th>Domain</th><th>Call Path</th><th>Location</th>
      </tr>
    </thead>
    <tbody id="blast-table"></tbody>
  </table>
</div>

<div class="section">
  <h2>Affected Processes</h2>
  <table>
    <thead><tr><th>Process Name</th><th>Domain</th><th>Entry Point</th></tr></thead>
    <tbody id="blast-process-table"></tbody>
  </table>
</div>

</div><!-- end tab-blast -->

<script>
function switchTab(name, btn) {
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  btn.classList.add('active');
}

// ── Tab 1: Stats ──────────────────────────────────────────────────────────────
let communityChart = null;

async function loadHealth() {
  const r = await fetch('/api/stats/health').then(r => r.json()).catch(() => null);
  if (!r) return;
  document.getElementById('s-funcs').textContent = (r.totalNodes?.functions || 0).toLocaleString();
  document.getElementById('s-classes').textContent = (r.totalNodes?.classes || 0).toLocaleString();
  document.getElementById('s-files').textContent = (r.totalNodes?.files || 0).toLocaleString();
  document.getElementById('s-edges').textContent = (r.totalEdges || 0).toLocaleString();
  document.getElementById('s-coverage').textContent = (r.coveragePercent || 0) + '%';
  document.getElementById('s-overlays').textContent = (r.activeOverlays?.length || 0);
  document.getElementById('s-errors').textContent = (r.parseErrors?.length || 0);
  document.getElementById('s-updated').textContent = r.lastUpdated
    ? 'Updated ' + new Date(r.lastUpdated).toLocaleString() : '';
  const badge = document.getElementById('neo4j-badge');
  badge.textContent = r.neo4jHealthy ? 'Neo4j \u25cf' : 'Neo4j \u2717';
  badge.className = 'badge' + (r.neo4jHealthy ? '' : ' error');
  const wtb = document.getElementById('watcher-table');
  wtb.innerHTML = (r.watchers || []).map(w => {
    const age = w.lastSeen ? Math.floor((Date.now() - new Date(w.lastSeen)) / 1000) : 9999;
    const ok = age < 60;
    return `<tr><td>${w.repo||'-'}</td><td>${w.branch||'-'}</td>
      <td>${w.lastSeen ? new Date(w.lastSeen).toLocaleTimeString() : '-'}</td>
      <td><span class="status-dot ${ok?'ok':'warn'}"></span>${ok?'Online':'Offline'}</td></tr>`;
  }).join('') || '<tr><td colspan="4" style="color:#8b949e">No watcher connected</td></tr>';
}

async function loadRisk() {
  const r = await fetch('/api/stats/risk').then(r => r.json()).catch(() => null);
  if (!r) return;
  document.getElementById('fanin-table').innerHTML = (r.topFanIn || []).map(f => {
    const file = f.filePath ? f.filePath.split('/').pop() : '-';
    return `<tr><td>${f.qualifiedName||f.name}</td>
      <td><span class="domain-tag">${f.domain||'?'}</span></td>
      <td><strong>${f.fanIn}</strong></td><td>${file}:${f.lineStart||'?'}</td></tr>`;
  }).join('');
  document.getElementById('lowconf-table').innerHTML = (r.lowConfidenceEdges || []).map(e => {
    const cls = e.confidence < 30 ? 'conf-low' : 'conf-med';
    return `<tr><td>${e.srcName}</td><td>${e.dstName}</td>
      <td class="${cls}">${e.confidence}</td>
      <td>${e.srcFile?e.srcFile.split('/').pop():'-'}</td></tr>`;
  }).join('');
  const communities = (r.communities||[]).filter(c => c.source !== 'domain_fallback').slice(0,15);
  const labels = communities.map(c => {
    const p = (c.id||'').split(':'); return p.length >= 3 ? '#'+p[p.length-1] : c.id;
  });
  const ctx = document.getElementById('community-chart').getContext('2d');
  if (communityChart) communityChart.destroy();
  communityChart = new Chart(ctx, {
    type: 'bar',
    data: { labels, datasets: [{ label: 'Functions', data: communities.map(c=>c.memberCount),
      backgroundColor: 'rgba(88,166,255,0.7)', borderColor: 'rgba(88,166,255,1)', borderWidth: 1 }] },
    options: { responsive: true, plugins: { legend: { display: false },
      tooltip: { callbacks: { title: (i) => communities[i[0].dataIndex].domain || communities[i[0].dataIndex].id } } },
      scales: { x: { ticks: { color:'#8b949e', maxRotation:45 }, grid: { color:'#21262d' } },
                y: { ticks: { color:'#8b949e' }, grid: { color:'#21262d' } } } }
  });
}

async function loadAll() { await Promise.all([loadHealth(), loadRisk()]); }
loadAll();
setInterval(loadAll, 60000);

// ── Tab 2: D3.js Call Graph ────────────────────────────────────────────────────
// Dynamic color palette — assigned per domain as nodes are encountered
const _domainColorPalette = [
  '#58a6ff','#3fb950','#d2a8ff','#e3b341','#f78166',
  '#79c0ff','#56d364','#ffa657','#ff7b72','#a5d6ff',
];
const _domainColorMap = {};
let _paletteIdx = 0;

function domainColor(d) {
  if (!d || d === 'unknown') return '#6e7681';
  if (!_domainColorMap[d]) {
    _domainColorMap[d] = _domainColorPalette[_paletteIdx % _domainColorPalette.length];
    _paletteIdx++;
  }
  return _domainColorMap[d];
}

let simulation = null;

async function renderGraph() {
  const name = document.getElementById('graph-input').value.trim();
  const depth = parseInt(document.getElementById('graph-depth').value);
  if (!name) return;

  const infoEl = document.getElementById('graph-info');
  infoEl.style.display = 'block';
  infoEl.textContent = 'Loading...';

  const data = await fetch(`/api/graph/subgraph?name=${encodeURIComponent(name)}&depth=${depth}`)
    .then(r => r.json()).catch(e => ({ error: e.message }));

  if (data.error || !data.nodes?.length) {
    infoEl.textContent = data.error || 'Function not found';
    return;
  }

  infoEl.textContent = `Nodes: ${data.nodes.length}  |  Edges: ${data.links.length}`;

  const svg = d3.select('#graph-svg');
  svg.selectAll('*').remove();
  if (simulation) simulation.stop();

  const rect = document.getElementById('graph-svg').getBoundingClientRect();
  const W = rect.width, H = rect.height || 540;

  const g = svg.append('g');
  svg.call(d3.zoom().scaleExtent([0.2, 4])
    .on('zoom', e => g.attr('transform', e.transform)));

  svg.append('defs').append('marker')
    .attr('id', 'arrow').attr('viewBox', '0 -4 8 8')
    .attr('refX', 14).attr('refY', 0)
    .attr('markerWidth', 6).attr('markerHeight', 6)
    .attr('orient', 'auto')
    .append('path').attr('d', 'M0,-4L8,0L0,4').attr('fill', '#444c56');

  const nodeMap = {};
  data.nodes.forEach(n => { nodeMap[n.id] = n; });

  const link = g.append('g').selectAll('line')
    .data(data.links).join('line')
    .attr('stroke', l => l.via === 'protocol_dispatch' ? '#d2a8ff' : '#444c56')
    .attr('stroke-width', l => l.confidence >= 90 ? 2 : l.confidence >= 75 ? 1.5 : 1)
    .attr('stroke-dasharray', l => l.confidence < 75 ? '4,3' : null)
    .attr('marker-end', 'url(#arrow)');

  const nodeRadius = n => Math.max(8, Math.min(26, 8 + (n.fanIn || 0) * 0.7));

  const node = g.append('g').selectAll('g')
    .data(data.nodes).join('g')
    .attr('cursor', 'pointer')
    .call(d3.drag()
      .on('start', (e, d) => { if (!e.active) simulation.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
      .on('drag',  (e, d) => { d.fx = e.x; d.fy = e.y; })
      .on('end',   (e, d) => { if (!e.active) simulation.alphaTarget(0); d.fx = null; d.fy = null; }))
    .on('click', (e, d) => {
      const file = d.filePath ? d.filePath.split('/').pop() : '-';
      infoEl.style.display = 'block';
      infoEl.innerHTML = `<strong>${d.qualifiedName || d.name}</strong>
        &nbsp;<span class="domain-tag">${d.domain}</span>
        &nbsp; called <strong>${d.fanIn}</strong> times
        &nbsp;&nbsp; ${file}:${d.lineStart || '?'}`;
    });

  node.append('circle')
    .attr('r', nodeRadius)
    .attr('fill', n => domainColor(n.domain))
    .attr('fill-opacity', 0.85)
    .attr('stroke', n => n.isCenter ? '#ffa657' : '#21262d')
    .attr('stroke-width', n => n.isCenter ? 3 : 1);

  node.append('text')
    .attr('dy', n => nodeRadius(n) + 12)
    .attr('text-anchor', 'middle')
    .attr('fill', '#c9d1d9')
    .attr('font-size', '10px')
    .text(n => n.name.length > 20 ? n.name.slice(0, 18) + '\u2026' : n.name);

  simulation = d3.forceSimulation(data.nodes)
    .force('link', d3.forceLink(data.links).id(d => d.id).distance(90).strength(0.5))
    .force('charge', d3.forceManyBody().strength(-220))
    .force('center', d3.forceCenter(W / 2, H / 2))
    .force('collision', d3.forceCollide().radius(n => nodeRadius(n) + 12))
    .on('tick', () => {
      link.attr('x1', l => l.source.x).attr('y1', l => l.source.y)
          .attr('x2', l => l.target.x).attr('y2', l => l.target.y);
      node.attr('transform', n => `translate(${n.x},${n.y})`);
    });

  const seenDomains = [...new Set(data.nodes.map(n => n.domain || 'unknown'))];
  document.getElementById('graph-legend').innerHTML = seenDomains.map(d =>
    `<span class="legend-item">
      <span class="legend-dot" style="background:${domainColor(d)}"></span>${d}
    </span>`
  ).join('') + `<span class="legend-item" style="margin-left:16px">
    <svg width="22" height="8"><line x1="0" y1="4" x2="22" y2="4"
      stroke="#d2a8ff" stroke-width="1.5"/></svg>
    &nbsp;protocol dispatch
  </span>`;
}

// ── Tab 3: Impact Analysis ─────────────────────────────────────────────────────
async function runBlast() {
  const name = document.getElementById('blast-input').value.trim();
  if (!name) return;
  document.getElementById('blast-summary').textContent = 'Analyzing...';
  document.getElementById('blast-table').innerHTML = '';
  document.getElementById('blast-process-table').innerHTML = '';

  const data = await fetch('/api/mcp/impact-scope', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ function_name: name, max_depth: 5 })
  }).then(r => r.json()).catch(e => ({ error: e.message }));

  if (data.error || !data.callers) {
    document.getElementById('blast-summary').textContent = data.error || 'Not found or no callers';
    return;
  }

  const callers = data.callers || [];
  document.getElementById('blast-summary').textContent =
    `${callers.length} functions affected`;

  document.getElementById('blast-table').innerHTML = callers.map(c => {
    const file = c.filePath ? c.filePath.split('/').pop() : '-';
    const depthCls = c.depth === 1 ? 'depth-0' : c.depth === 2 ? 'depth-1' : 'depth-2';
    const path = (c.callPath || []).join(' \u2192 ');
    return `<tr>
      <td><span class="depth-badge ${depthCls}">depth ${c.depth}</span></td>
      <td>${c.qualifiedName || c.name}</td>
      <td><span class="domain-tag">${c.domain || '?'}</span></td>
      <td style="color:#8b949e;font-size:11px;max-width:300px;overflow:hidden;
                 text-overflow:ellipsis;white-space:nowrap" title="${path}">${path||'-'}</td>
      <td>${file}:${c.lineStart||'?'}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="5" style="color:#8b949e">No callers found (may be an entry point)</td></tr>';

  const procs = data.affectedProcesses || [];
  document.getElementById('blast-process-table').innerHTML = procs.map(p =>
    `<tr><td>${p.name}</td><td><span class="domain-tag">${p.domain||'?'}</span></td>
      <td style="color:#8b949e">${p.entryPoint||'-'}</td></tr>`
  ).join('') || '<tr><td colspan="3" style="color:#8b949e">No associated processes</td></tr>';
}
</script>
</body>
</html>
"""


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(content=DASHBOARD_HTML)


# ── App startup ───────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    logger.info("graph-api starting up...")
    try:
        get_store()
        logger.info("Neo4j connected")
    except Exception as e:
        logger.warning(f"Neo4j connection failed at startup: {e}")
    try:
        get_engine()
        logger.info("BM25 index loaded")
    except Exception as e:
        logger.warning(f"BM25 load failed: {e}")
