# Code Intelligence Graph — Operations Guide

> Your iOS codebase is a massive call graph — tens of thousands of functions calling each other across thousands of files. This tool parses it all into a Neo4j graph database, then lets Claude "see" your architecture through 5 MCP tools: search functions by natural language, trace call chains, analyze blast radius, and more.

---

## Table of Contents

- [Quick Start (5 minutes, Docker)](#quick-start-5-minutes-docker)
- [1. System Requirements](#1-system-requirements)
- [2. Installation](#2-installation)
  - [Option A: Docker (recommended)](#option-a-docker-recommended)
  - [Option B: Local (no Docker)](#option-b-local-no-docker)
- [3. Build the Code Graph](#3-build-the-code-graph)
- [4. Verify Everything Works](#4-verify-everything-works)
- [5. Connect to Claude Code](#5-connect-to-claude-code)
- [6. How to Use the 5 MCP Tools](#6-how-to-use-the-5-mcp-tools)
- [7. Daily Development Workflow](#7-daily-development-workflow)
- [8. Enable Hybrid Search (Optional)](#8-enable-hybrid-search-optional)
- [9. Customize Domain Mapping](#9-customize-domain-mapping)
- [10. Advanced: IndexStore Stub Resolution](#10-advanced-indexstore-stub-resolution)
- [11. CLI Command Reference](#11-cli-command-reference)
- [12. Troubleshooting](#12-troubleshooting)
- [13. Environment Variables Reference](#13-environment-variables-reference)
- [14. Uninstall / Clean Up](#14-uninstall--clean-up)

---

## Quick Start (5 minutes, Docker)

If you just want to get running as fast as possible:

```bash
git clone <this-repo-url>
cd code-intelligence-graph

# Point to your iOS project
cp .env.example .env
# Edit .env → set WORKSPACE_PATH=/path/to/your/ios/project

# Start Neo4j + API + Watcher
docker compose up -d

# Wait ~60s for Neo4j to initialize, then build the graph
docker compose exec graph-api python cli.py build

# Verify
docker compose exec graph-api python cli.py stats
```

Then configure Claude Code (in `~/.claude/mcp_settings.json`):

```json
{
  "mcpServers": {
    "code-graph": {
      "command": "python",
      "args": ["<absolute-path-to>/code-intelligence-graph/src/mcp/mcp_stdio.py"],
      "env": {
        "NEO4J_URI": "bolt://localhost:7687",
        "NEO4J_USER": "neo4j",
        "NEO4J_PASSWORD": "codegraph123"
      }
    }
  }
}
```

Done. Ask Claude: *"Use cig_graph_stats to show the code graph status"* — if it returns function counts, you're all set.

Read on for the detailed walkthrough.

---

## 1. System Requirements

| Requirement | Version | Notes |
|---|---|---|
| macOS | 13+ (Ventura) | Linux works for the graph/API side, but Swift parsing needs macOS |
| Xcode | 15+ | Provides `libclang` for ObjC parsing |
| Python | 3.12+ | `python3 --version` to check |
| Docker + Docker Compose | latest | Only for Docker install path |
| Neo4j | 5.x Community | Only for local install (Docker includes it) |

**Your iOS project must have been built at least once in Xcode.** This ensures Swift Package dependencies are resolved and `.swift` files are valid.

---

## 2. Installation

### Option A: Docker (recommended)

Docker handles Neo4j, the API server, and the file watcher — you don't need to install anything else.

**Step 1. Clone the repo**

```bash
git clone <this-repo-url>
cd code-intelligence-graph
```

**Step 2. Configure your workspace**

```bash
cp .env.example .env
```

Open `.env` and set `WORKSPACE_PATH` to the **absolute path** of your iOS project root:

```bash
# .env
WORKSPACE_PATH=/Users/yourname/Projects/MyiOSApp
NEO4J_PASSWORD=codegraph123
```

**Step 3. Start the services**

```bash
docker compose up -d
```

This starts 3 containers:

| Container | Port | What it does |
|---|---|---|
| `code-graph-neo4j` | 7474 (browser), 7687 (bolt) | Graph database |
| `code-graph-api` | 8080 | FastAPI MCP server + dashboard |
| `code-graph-watcher` | — | Watches your project for file changes |

**Step 4. Wait for Neo4j to become healthy**

```bash
docker compose ps
```

Wait until `code-graph-neo4j` shows `healthy` (usually 30-60 seconds). You'll see something like:

```
NAME                  STATUS
code-graph-neo4j      running (healthy)
code-graph-api        running (healthy)
code-graph-watcher    running
```

If `neo4j` stays `unhealthy`, check `docker compose logs neo4j`.

---

### Option B: Local (no Docker)

Use this if you prefer running everything natively.

**Step 1. Install Neo4j**

```bash
brew install neo4j
neo4j start
```

Open http://localhost:7474 in your browser. Log in with `neo4j` / `neo4j`, then set a new password. Remember this password — you'll need it in `config.yaml`.

**Step 2. Clone and set up Python**

```bash
git clone <this-repo-url>
cd code-intelligence-graph

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

> If `pip install` fails on `libclang`, run `xcode-select --install` first.

**Step 3. Build the SwiftSyntax CLI parser**

This is a one-time step. The Swift parser is a small CLI tool that uses Apple's official SwiftSyntax library:

```bash
cd src/parser/swift_syntax_extractor
swift build -c release
cd ../../..
```

You should see `Build complete!`. The binary is at `.build/release/swift-graph-extractor`.

> If this fails, ensure Xcode 15+ is installed and `swift --version` works.

**Step 4. Configure**

Open `config.yaml` and set your workspace path and Neo4j password:

```yaml
workspace:
  path: /Users/yourname/Projects/MyiOSApp   # <- your iOS project
  exclude:
    - Pods/
    - .git/
    - build/
    - DerivedData/

neo4j:
  uri: bolt://localhost:7687
  user: neo4j
  password: your-neo4j-password              # <- the password you set in Step 1
```

---

## 3. Build the Code Graph

This is the key step — it parses your entire iOS project and loads the call graph into Neo4j.

```bash
# Docker
docker compose exec graph-api python cli.py build

# Local
python cli.py build
```

The build runs 7 phases:

```
Phase 1-2: Parsing 5231 files                        ← parses every .swift / .m file
Phase 3: Resolving imports...                         ← matches cross-file function calls
Phase 4: Community detection...                       ← clusters functions by domain
Phase 5: Process tracing...                           ← traces execution flows
Phase 6: Building BM25 index...                       ← builds keyword search index
Phase 7: Building embedding index...                  ← vector embeddings (optional)
```

**Expected duration** (varies by project size):

| Project size | Time |
|---|---|
| 500 files | 1-2 min |
| 2,000 files | 3-5 min |
| 5,000 files | 5-15 min |
| 10,000+ files | 15-30 min |

When done, you'll see a JSON summary:

```json
{
  "total_files": 5231,
  "parse_errors": 12,
  "resolved_calls": 48210,
  "communities": 18,
  "processes": 342,
  "bm25_indexed": 24500
}
```

A few `parse_errors` is normal (generated code, unusual syntax, etc.).

> **Note:** Phase 7 (embedding) is automatically skipped if you haven't set a `DASHSCOPE_API_KEY`. This is fine — BM25 search works without it. See [Section 8](#8-enable-hybrid-search-optional) to enable it later.

---

## 4. Verify Everything Works

Run these checks to confirm the graph is built correctly:

```bash
# 1. Check graph stats — should show non-zero function counts
python cli.py stats

# 2. Try a search — should return results
python cli.py search "viewDidLoad"

# 3. Try a call chain (use any function from your project)
python cli.py query call-chain AppDelegate.applicationDidFinishLaunching

# 4. Open the dashboard (Local or Docker)
open http://localhost:8080/dashboard

# 5. Health check (should return {"status": "ok"})
curl http://localhost:8080/health
```

If `python cli.py stats` shows functions > 0 and `search` returns results, the graph is healthy.

---

## 5. Connect to Claude Code

There are two ways to connect. **stdio MCP** is recommended (simpler, no HTTP server dependency).

### Option A: stdio MCP (recommended)

Create or edit `~/.claude/mcp_settings.json`:

```json
{
  "mcpServers": {
    "code-graph": {
      "command": "python",
      "args": ["/absolute/path/to/code-intelligence-graph/src/mcp/mcp_stdio.py"],
      "env": {
        "NEO4J_URI": "bolt://localhost:7687",
        "NEO4J_USER": "neo4j",
        "NEO4J_PASSWORD": "codegraph123"
      }
    }
  }
}
```

> **Important:**
> - All paths must be **absolute** (not `~/...` or `./...`).
> - If you used a Python venv, use the full venv Python path:
>   `"command": "/path/to/code-intelligence-graph/venv/bin/python"`

### Option B: HTTP API

If the API server is already running (via Docker or `uvicorn`):

```json
{
  "mcpServers": {
    "code-graph": {
      "url": "http://localhost:8080"
    }
  }
}
```

### Verify the connection

Open Claude Code and type:

> Show code graph stats using cig_graph_stats

If Claude returns function counts, class counts, and CALLS coverage, the connection is working.

### Test the tools

Try these prompts:

> Search the code graph for "network request"

> What is the full context of `AppDelegate.applicationDidFinishLaunching`?

> What is the impact scope of modifying `NetworkClient.request`?

---

## 6. How to Use the 5 MCP Tools

### cig_search — "I don't know the function name"

Find functions by natural language. Good for exploring unfamiliar code.

| Ask Claude | What happens |
|---|---|
| *"Search the code graph for payment handling"* | Returns ranked list of matching functions |
| *"Find all functions related to route calculation"* | BM25 keyword match (+ vector semantic if enabled) |

### cig_context — "Tell me everything about this function"

360-degree view: who calls it, what it calls, which file, which domain.

| Ask Claude | What happens |
|---|---|
| *"Show context for `AuthManager.refreshToken`"* | Returns callers, callees, file, domain, community |
| *"What does `NetworkClient.request` depend on?"* | Same — shows incoming and outgoing relationships |

### cig_impact — "What breaks if I change this?"

Reverse traversal: finds all functions that directly or indirectly call the target.

| Ask Claude | What happens |
|---|---|
| *"Impact scope of `DatabaseHelper.save`"* | Lists all callers by depth, counts affected files |
| *"If I modify `Logger.log`, what's the blast radius?"* | Shows how many functions and domains are affected |

### cig_call_chain — "What does this function do under the hood?"

Forward traversal: traces the downstream call tree.

| Ask Claude | What happens |
|---|---|
| *"Show call chain from `AppDelegate.applicationDidFinishLaunching`"* | Shows full execution flow from app launch |
| *"What functions does `CheckoutVC.submitPayment` call?"* | Shows the call tree with file locations |

### cig_graph_stats — "How healthy is the graph?"

Dashboard: function/class/file counts, CALLS coverage, top fan-in nodes.

| Ask Claude | What happens |
|---|---|
| *"Show code graph stats"* | Returns overall graph metrics |

### Tips

- **Use qualified names** when possible: `MyClass.myMethod` is more precise than just `myMethod`.
- **Combine tools**: `cig_search` to find the entry point → `cig_call_chain` to explore it → `cig_impact` to assess risk.
- **Check CALLS coverage** in `cig_graph_stats`. Below 30% means many cross-file calls are unresolved — run IndexStore resolution (Section 10) to improve it.

---

## 7. Daily Development Workflow

### Automatic updates: File Watcher

The watcher monitors your project directory and triggers **incremental rebuilds** whenever a `.swift` or `.m` file changes. It also detects **git branch switches**.

```bash
# Docker — already running via docker compose, nothing to do

# Local
python watcher.py --path /path/to/your/ios/project
```

With the watcher running, the graph stays up-to-date automatically as you code.

### When to manually rebuild

| Scenario | Action |
|---|---|
| Normal coding | Watcher handles it automatically |
| Large rebase / merge | `python cli.py build` (full rebuild) |
| Switched to a new branch | Watcher detects this; or `python cli.py build --branch my-branch` |
| Changed `domain_mapping.yaml` | `python cli.py build` (domains are assigned at build time) |
| First time after clone | `python cli.py build` |

### Code review workflow

Before submitting or reviewing a PR, check the impact:

```bash
python cli.py detect-changes --diff origin/main
```

This outputs:
- Which functions were changed
- What downstream functions are affected (blast radius)
- Which domains are impacted
- Risk assessment

You can also ask Claude directly:

> Analyze the impact of my recent changes using detect-changes against origin/main

### Branch overlays

The graph supports multiple branches simultaneously. Each branch gets its own overlay on top of the `master` graph:

```bash
# Build graph for your feature branch
python cli.py build --branch feature/my-feature

# Query against it
python cli.py query call-chain NewClass.newMethod --branch feature/my-feature
```

---

## 8. Enable Hybrid Search (Optional)

By default, `cig_search` uses BM25 (keyword matching). You can optionally enable **hybrid search** which adds vector embeddings for better semantic matching. This makes search more accurate, especially for natural language queries.

### How it works

```
Query: "handle payment failure"
    ├── BM25 path (keyword match)  →  Top 30 results
    ├── Vector path (semantic)     →  Top 30 results
    └── RRF Fusion                 →  Top 10 merged results
```

### Setup

1. **Get a DashScope API key** at https://dashscope.console.aliyun.com/ (Alibaba Cloud)

2. **Set the key** (pick one):

   ```bash
   # In .env file
   echo "DASHSCOPE_API_KEY=sk-your-key" >> .env

   # Or as environment variable
   export DASHSCOPE_API_KEY=sk-your-key

   # Or in config.yaml (under embedding.api_key)
   ```

3. **Rebuild** to generate embeddings:

   ```bash
   python cli.py build
   ```

   You should see:
   ```
   Phase 7: embedding 24500 functions...
   Phase 7: done. 24500 embeddings stored.
   ```

### Without API key

Everything works fine — Phase 7 is skipped and search uses BM25 only. All other tools (`context`, `impact`, `call-chain`, `stats`) are unaffected.

---

## 9. Customize Domain Mapping

Domains are logical feature areas (e.g. `networking`, `auth`, `payments`) assigned to every function. They appear in search results, context views, and impact analysis.

### Generate a starter mapping

```bash
python scripts/generate_domain_mapping.py --workspace /path/to/your/ios/project
```

This creates `domain_mapping.yaml` using directory-name heuristics.

### Customize it

```yaml
# domain_mapping.yaml
mappings:
  MyApp/Features/Auth/:       auth
  MyApp/Features/Payments/:   payments
  MyApp/Network/:             networking
  MyApp/UI/:                  ui
  MyApp/Utils/:               utilities
```

Rules:
- **Longest prefix wins**: `MyApp/UI/Components/` takes priority over `MyApp/UI/`
- **Fallback**: if no prefix matches, the top-level directory name is used
- **After editing**: rebuild with `python cli.py build`

See [domain-mapping-guide.md](domain-mapping-guide.md) for more examples.

---

## 10. Advanced: IndexStore Stub Resolution

After a normal build, ~60-70% of CALLS edges point to unresolved "stubs" (cross-file calls where the parser can't determine the target). The IndexStore resolver uses **Xcode's compiled index** to fix this.

### Prerequisites

- Your project must be **successfully built in Xcode** (Product → Build)
- `DerivedData` must not have been cleaned

### Run

```bash
# Preview first (no writes)
python scripts/resolve_stubs_indexstore.py --dry-run

# Apply
python scripts/resolve_stubs_indexstore.py
```

This typically resolves 20-25% of remaining stubs, bringing total CALLS coverage from ~33% to ~47%.

See [stub-resolution.md](stub-resolution.md) for how it works.

---

## 11. CLI Command Reference

### Build

```bash
python cli.py build                                # Full build (workspace from config.yaml)
python cli.py build --path /path/to/project        # Explicit workspace path
python cli.py build --branch feature/x             # Build for a specific branch
python cli.py build --file path/to/File.swift      # Incremental: single file only
```

### Query

```bash
python cli.py query call-chain <FunctionName>             # Forward: what does it call?
python cli.py query call-chain <Name> --depth 15          # Custom depth (default: 10)
python cli.py query impact <FunctionName>                  # Reverse: who calls it?
python cli.py query impact <Name> --depth 8                # Custom depth (default: 5)
python cli.py query context <SymbolName>                   # 360-degree view
```

### Search & Analysis

```bash
python cli.py search "natural language query"              # Search (BM25 or hybrid)
python cli.py search "payment handling" --top-k 20         # More results
python cli.py detect-changes --diff HEAD~1                 # Impact of last commit
python cli.py detect-changes --diff origin/main            # Impact vs main branch
python cli.py stats                                        # Graph statistics
```

---

## 12. Troubleshooting

### Neo4j won't start / connection refused

```bash
# Docker
docker compose logs neo4j                        # check for error messages
docker compose restart neo4j                     # restart it

# Local
neo4j status                                     # is it running?
lsof -i :7687                                    # is the port in use?
```

Common cause: port 7687 already in use by another Neo4j instance.

### Build shows many parse errors

```bash
# Check Xcode is installed
xcode-select -p
# Should print: /Applications/Xcode.app/Contents/Developer

# Rebuild the SwiftSyntax extractor
cd src/parser/swift_syntax_extractor
swift build -c release
cd ../../..
```

A few parse errors (< 5% of files) is normal. Large numbers usually mean the SwiftSyntax CLI wasn't built or Xcode is missing.

### `python cli.py search` returns nothing

1. Check the graph has data: `python cli.py stats` — functions should be > 0
2. Check `bm25_index.pkl` exists: `ls -la bm25_index.pkl`
3. If missing, rebuild: `python cli.py build`

### `Function not found` in query commands

- Names are **case-sensitive**: `MyClass.myMethod`, not `myclass.mymethod`
- Use `cig_search` first to find the exact qualified name
- Check if the file is excluded by `workspace.exclude` in `config.yaml`

### Claude Code can't see the MCP tools

1. Check `mcp_settings.json` has correct **absolute paths**
2. Test manually:
   ```bash
   echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python src/mcp/mcp_stdio.py
   ```
   You should see a JSON response listing 5 tools.
3. Check logs: `cat logs/mcp_stdio.log`

### Docker services show "unhealthy"

```bash
docker compose down
docker compose up -d                             # fresh start
docker compose logs graph-api                    # check API errors
```

If `graph-api` fails, it's usually because Neo4j isn't ready yet. Wait for Neo4j to become healthy first.

### Memory issues on large projects

- Reduce parallel workers: set `pipeline.max_workers: 2` in `config.yaml`
- Increase Neo4j heap in `docker-compose.yml`:
  ```yaml
  NEO4J_server_memory_heap_max__size=2G
  ```

### Phase 7 shows "skipped"

This is expected if you haven't set `DASHSCOPE_API_KEY`. Search works fine without it (BM25 only). See [Section 8](#8-enable-hybrid-search-optional) to enable it.

---

## 13. Environment Variables Reference

| Variable | Default | Required | Description |
|---|---|---|---|
| `WORKSPACE` / `WORKSPACE_PATH` | — | Yes (or use config.yaml) | Absolute path to your iOS project |
| `NEO4J_URI` | `bolt://localhost:7687` | No | Neo4j connection URI |
| `NEO4J_USER` | `neo4j` | No | Neo4j username |
| `NEO4J_PASSWORD` | `codegraph123` | No | Neo4j password |
| `DASHSCOPE_API_KEY` | — | No | DashScope API key (enables hybrid search) |
| `GRAPH_API_URL` | `http://localhost:8080` | No | API server URL (used by watcher) |
| `BM25_INDEX_PATH` | `bm25_index.pkl` | No | Path to BM25 index file |

---

## 14. Uninstall / Clean Up

### Docker

```bash
# Stop and remove containers + volumes
docker compose down -v

# Remove images
docker rmi code-intelligence-graph-graph-api code-intelligence-graph-watcher
```

### Local

```bash
# Stop Neo4j
neo4j stop

# Remove the graph data (optional — only if you want a clean slate)
# Neo4j data is at: /usr/local/var/neo4j/data/

# Remove the repo
rm -rf code-intelligence-graph
```

### Reset graph data (keep installation)

```bash
# Docker: drop and recreate volumes
docker compose down -v
docker compose up -d
# Then rebuild: docker compose exec graph-api python cli.py build

# Local: clear Neo4j database
python -c "
from src.graph.store import Neo4jStore
store = Neo4jStore(); store.connect()
store.query('MATCH (n) DETACH DELETE n')
print('Database cleared.')
"
# Then rebuild: python cli.py build
```
