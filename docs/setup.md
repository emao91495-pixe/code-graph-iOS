# Setup Guide (Non-Docker)

This guide covers running Code Intelligence Graph directly on macOS without Docker.

## Prerequisites

- macOS 13+ (Ventura or later)
- Python 3.12+
- Xcode 15+ (for libclang and SwiftSyntax)
- Neo4j 5.x Community Edition

## 1. Install Neo4j

```bash
# Via Homebrew
brew install neo4j

# Start Neo4j
neo4j start

# Set password (first run)
# Open http://localhost:7474 → login with neo4j/neo4j → set new password
# Update config.yaml with the new password
```

Or download from https://neo4j.com/download-center/#community

## 2. Install Python dependencies

```bash
python3 -m venv venv
source venv/bin/activate

pip install -r requirements.txt
```

**Note on libclang**: The `libclang==18.1.1` package requires the Xcode command-line tools.
If installation fails, ensure Xcode is installed and run:

```bash
xcode-select --install
```

## 3. Build the SwiftSyntax extractor

The Swift parser uses a compiled CLI tool. Build it once:

```bash
cd src/parser/swift_syntax_extractor
swift build -c release
cd ../../..
```

The binary will be at `src/parser/swift_syntax_extractor/.build/release/swift-syntax-extractor`.

## 4. Configure

```bash
cp config.yaml config.local.yaml   # optional: keep a local copy
```

Edit `config.yaml`:
- Set `workspace.path` to your iOS project root
- Set `neo4j.password` to match your Neo4j password

## 5. Generate domain mapping (recommended)

```bash
python scripts/generate_domain_mapping.py --workspace /path/to/your/ios/project
```

Review and edit `domain_mapping.yaml`.

## 6. Build the graph

```bash
python cli.py build
```

This runs all 7 pipeline phases. For a large project (5,000+ files), expect 5-15 minutes.
Phase 7 (embedding) is skipped automatically if no `DASHSCOPE_API_KEY` is set.

## 7. Start the API server

```bash
uvicorn src.mcp.server:app --host 0.0.0.0 --port 8080 --reload
```

The dashboard is available at http://localhost:8080/dashboard.

## 8. (Optional) Start the file watcher

```bash
python watcher.py --path /path/to/your/ios/project
```

The watcher monitors file changes and triggers incremental rebuilds automatically.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `WORKSPACE` | (from config.yaml) | Absolute path to iOS project |
| `NEO4J_URI` | `bolt://localhost:7687` | Neo4j connection URI |
| `NEO4J_USER` | `neo4j` | Neo4j username |
| `NEO4J_PASSWORD` | `codegraph123` | Neo4j password |
| `GRAPH_API_URL` | `http://localhost:8080` | API URL (used by watcher) |
| `BM25_INDEX_PATH` | `bm25_index.pkl` | Path to BM25 index file |
| `DASHSCOPE_API_KEY` | (none) | DashScope API key for hybrid search (optional) |

## Linux Notes

On Linux, Swift parsing requires installing Swift from https://swift.org/download/.
`libclang` for ObjC parsing requires `clang` to be installed:

```bash
# Ubuntu / Debian
apt-get install clang python3-clang
```

The `libclang` Python package path may need to be configured manually:

```python
import clang.cindex
clang.cindex.Config.set_library_path("/usr/lib/llvm-18/lib")
```
