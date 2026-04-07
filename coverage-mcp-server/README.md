# coverage-mcp-server

An [MCP](https://modelcontextprotocol.io) server that exposes LLVM line coverage data to AI assistants (Claude Code, Claude Desktop). Ask Claude questions like "which files in `crates/router` have the lowest coverage?" and get instant answers backed by real coverage data.

## How it works

The server reads a coverage JSON file in a custom tree format (not `llvm-cov export`). It parses the nested `children` tree, extracts per-line coverage arrays, and serves the data through six MCP tools over SSE or stdio transport.

```
index.json  →  parser  →  in-memory cache  →  MCP tools  →  Claude
```

## Tools

| Tool | Description |
|------|-------------|
| `list_builds` | List available build tags |
| `summarize_report` | Overall line coverage % for a build |
| `get_folder_coverage` | Aggregate coverage for all files under a path prefix |
| `get_file_coverage` | Line coverage + uncovered line numbers for a specific file |
| `get_uncovered_lines` | Uncovered lines (count == 0) grouped by file, with optional file/folder filter |
| `list_files` | All files sorted by missed lines, coverage %, or filename |

All tools except `list_builds` require a `tag` argument. Use `list_builds` first to see what tags are loaded.

## Coverage JSON format

The server expects a custom tree-format JSON (not the standard `llvm-cov export --format=json`):

```json
{
  "name": "root",
  "coveragePercent": 8.08,
  "linesCovered": 29080,
  "linesMissed": 331018,
  "linesTotal": 360098,
  "children": {
    "crates": {
      "children": {
        "router": {
          "children": {
            "src": {
              "children": {
                "core.rs": {
                  "coverage": [-1, 0, 3, 0, 1, ...],
                  "linesCovered": 2,
                  "linesMissed": 2,
                  "linesTotal": 4
                }
              }
            }
          }
        }
      }
    }
  }
}
```

Values in the `coverage` array: `-1` = not instrumented, `0` = missed (not executed), `>0` = hit count.

> This format contains line-level coverage only — no function or region data.

## Setup

### Prerequisites

- Python 3.11+
- Docker (for containerized deployment)
- A coverage `index.json` file

### Local development (stdio)

```bash
cd coverage-mcp-server
python -m venv .venv && source .venv/bin/activate
pip install -e .

COVERAGE_FILE_PATH=~/Downloads/index.json coverage-mcp-server
```

### Generate an API key

Required for SSE transport (Docker / remote access):

```bash
python3 keygen.py
```

Output:
```
Raw key (client)      : abc123...   ← put this in Authorization: Bearer <key>
SHA-256 hash (server) : def456...   ← put this in MCP_API_KEY_HASH env var
```

The raw key is never stored on the server. Only the SHA-256 hash is held server-side; it cannot be reversed to recover the raw key.

### Docker (SSE transport)

**Build:**
```bash
docker build -t coverage-mcp-server .
```

**Run (single file):**
```bash
docker run -d \
  -e MCP_TRANSPORT=sse \
  -e MCP_API_KEY_HASH=<hash-from-keygen> \
  -e COVERAGE_FILE_PATH=/data/index.json \
  -v ~/Downloads:/data \
  -p 9090:8080 \
  coverage-mcp-server
```

**Run (directory of builds):**
```bash
docker run -d \
  -e MCP_TRANSPORT=sse \
  -e MCP_API_KEY_HASH=<hash-from-keygen> \
  -e COVERAGE_DIR=/data \
  -v /path/to/coverage/artifacts:/data \
  -p 9090:8080 \
  coverage-mcp-server
```

In directory mode, place files named `coverage_{tag}.json` (e.g. `coverage_build-42.json`) in the mounted path. Each tag becomes independently queryable.

## Configuration

All settings are environment variables (or `.env` file):

| Variable | Default | Description |
|----------|---------|-------------|
| `COVERAGE_FILE_PATH` | `~/Downloads/index.json` | Path to single coverage JSON file |
| `COVERAGE_DIR` | _(empty)_ | Directory of `coverage_{tag}.json` files. Takes precedence over `COVERAGE_FILE_PATH` when set. |
| `MCP_API_KEY_HASH` | _(empty)_ | SHA-256 hex digest of the raw API key. Required for SSE transport. |
| `MCP_TRANSPORT` | `stdio` | `stdio` or `sse` |
| `MCP_HOST` | `0.0.0.0` | Bind host (SSE only) |
| `MCP_PORT` | `8080` | Bind port (SSE only) |

Copy `.env.example` to `.env` and fill in values for local runs.

## Connecting Claude Code (VS Code extension)

Create `.mcp.json` in the project root (not `~/.claude/`):

```json
{
  "mcpServers": {
    "coverage": {
      "type": "sse",
      "url": "http://localhost:9090/sse",
      "headers": {
        "Authorization": "Bearer <your-raw-key>"
      }
    }
  }
}
```

Then add to `~/.claude/settings.json`:
```json
{
  "enabledMcpjsonServers": ["coverage"]
}
```

Reload the VS Code window (`Cmd+Shift+P` → Developer: Reload Window) after starting the container or changing config. The MCP connection is established at session start — a container restart requires a window reload.

## Example queries to ask Claude

```
Summarize the coverage report
Which folders have the lowest coverage?
What's the coverage for crates/router?
Show me uncovered lines in crates/router/src/core/payments.rs
List all files with less than 5% coverage in the connectors folder
Which files have the most missed lines?
```

## File structure

```
coverage-mcp-server/
├── src/coverage_mcp/
│   ├── server.py     # MCP server + tool handlers + ASGI SSE app
│   ├── parser.py     # Tree-format JSON parser → CoverageReport dataclasses
│   ├── fetcher.py    # File/directory resolver (single-file or multi-build)
│   └── config.py     # Environment variable configuration
├── keygen.py         # API key + hash generator
├── Dockerfile        # Python 3.13-slim, uv, non-root appuser
├── pyproject.toml
└── .env.example
```

## Notes

- Reports are cached in memory after first load. Restart the container to pick up a new coverage file.
- The SSE transport uses a plain ASGI dispatch function (not Starlette routing) to be compatible with `starlette>=1.0.0` and `sse-starlette>=3.3.4`.
- Auth uses `hmac.compare_digest` for constant-time comparison to prevent timing attacks.
