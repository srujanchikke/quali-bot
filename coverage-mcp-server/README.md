# coverage-mcp-server

An [MCP](https://modelcontextprotocol.io) server that exposes LLVM line and function coverage data to Claude (VS Code extension or Claude Desktop).

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  K8s cluster (AWS)                                              │
│                                                                 │
│  ┌─────────┐   upload files    ┌─────────────────────────────┐ │
│  │ Jenkins │ ─────────────────►│  S3 bucket                  │ │
│  │         │                   │  builds/                    │ │
│  │         │   aws s3 presign  │    build-142-abc/           │ │
│  │         │ ◄──────────────── │      line.json              │ │
│  │         │                   │      function.json          │ │
│  └────┬────┘                   └─────────────────────────────┘ │
│       │ POST /sync                                              │
│       │ { build_id, line_url, function_url }                   │
│       │ Authorization: Bearer <SYNC_API_KEY>                   │
└───────┼─────────────────────────────────────────────────────────┘
        │ HTTPS (pre-signed URLs, no AWS creds needed)
        ▼
┌─────────────────────────────────────────────────────────────────┐
│  Local machine                                                  │
│                                                                 │
│  ┌──────────────────────┐    downloads files via HTTPS         │
│  │  sync service :8888  │ ──────────────────────────────────►  │
│  │  POST /sync          │    (pre-signed URLs, no AWS creds)   │
│  │  GET  /health        │                                       │
│  └──────────┬───────────┘                                       │
│             │ upsert                                            │
│             ▼                                                   │
│  ┌──────────────────────┐                                       │
│  │  MongoDB             │                                       │
│  │  builds              │                                       │
│  │  file_coverage       │                                       │
│  └──────────┬───────────┘                                       │
│             │ query                                             │
│             ▼                                                   │
│  ┌──────────────────────┐    MCP over SSE                      │
│  │  coverage-mcp :9090  │ ◄────────────────────── Claude       │
│  │  GET  /sse           │    Authorization: Bearer <MCP_KEY>   │
│  │  POST /messages      │                                       │
│  └──────────────────────┘                                       │
└─────────────────────────────────────────────────────────────────┘
```

**No AWS credentials on the local machine.** Jenkins generates pre-signed S3 URLs (HTTPS links with auth baked in, valid 1 hour) and POSTs them to the sync service. The sync service downloads using plain HTTPS.

---

## Credentials

| Key | Held by | Protects |
|-----|---------|----------|
| AWS credentials | Jenkins only | S3 upload + pre-signed URL generation |
| `SYNC_API_KEY` (raw) | Jenkins credential store | Calling `POST /sync` on local machine |
| `SYNC_API_KEY_HASH` | Local `.env` | Verifying incoming `/sync` requests |
| `MCP_API_KEY` (raw) | `.mcp.json` on dev machine | Calling the MCP server |
| `MCP_API_KEY_HASH` | Local `.env` | Verifying Claude's MCP requests |

---

## Coverage file formats

### line.json — custom tree format

```json
{
  "coveragePercent": 8.08,
  "linesCovered": 29080,
  "linesMissed": 331018,
  "linesTotal": 360098,
  "children": {
    "crates": {
      "children": {
        "router": {
          "children": {
            "payments.rs": {
              "coverage": [-1, 0, 3, 0, 1],
              "linesCovered": 2, "linesMissed": 2, "linesTotal": 4
            }
          }
        }
      }
    }
  }
}
```

`coverage` values: `-1` = not instrumented, `0` = missed, `>0` = hit count.

### function.json — Coveralls format

```json
{
  "source_files": [
    {
      "name": "crates/router/src/payments.rs",
      "coverage": [null, null, 3, 0, 1],
      "functions": [
        { "name": "handle_payment", "start": 45, "exec": true },
        { "name": "refund",         "start": 90, "exec": false }
      ]
    }
  ]
}
```

---

## MCP Tools (11)

All tools except `list_builds` accept a `tag` argument. Use `latest` for the most recent build.
Tools marked ★ require MongoDB mode (`MONGO_URI` set).

| Tool | ★ | Description |
|------|---|-------------|
| `list_builds` | | Available build tags with line% and func% |
| `summarize_report` | | Overall line + function coverage for a build |
| `get_folder_coverage` | | Aggregate coverage for all files under a path prefix |
| `get_file_coverage` | | Per-file line + function coverage + uncovered detail |
| `get_uncovered_lines` | | Uncovered lines grouped by file (filterable by file/folder) |
| `get_uncovered_functions` | ★ | Unexecuted functions with line numbers grouped by file |
| `list_files` | | Files sorted by missed lines/functions |
| `get_zero_coverage_files` | ★ | Files never touched by any test — easiest wins |
| `search_function` | ★ | Look up a specific function and check if it is covered |
| `compare_builds` | ★ | Coverage delta between two builds — regressions, improvements, new files |
| `get_test_priority` | ★ | Files ranked by impact score `(func_missed × 3) + line_missed` |

## MCP Prompts (4)

Prompts are reusable instruction templates. Ask Claude to use them by name.

| Prompt | Arguments | What it does |
|--------|-----------|-------------|
| `pr_coverage_review` | `head_tag`, `base_tag`, `pr_number` | Full PR coverage report — gets changed files via git, checks each file, finds uncovered functions, formats a PR comment |
| `write_test_plan` | `tag`, `target` | Structured test plan for a folder/file — priority order, suggested test types, uncovered functions |
| `coverage_regression_report` | `base_tag`, `head_tag` | What regressed between two builds, root cause hypothesis, recommended actions |
| `onboarding_coverage_tour` | `tag` | Tour of worst-covered areas for a new QA engineer — where to focus, quick wins, how to use the tools |

---

## Setup

### 1. Generate API keys

```bash
# Key for the sync service HTTP endpoint (Jenkins → sync)
python3 sync/keygen.py

# Key for the MCP server (Claude → MCP)
python3 coverage-mcp-server/keygen.py
```

Each script prints a raw key and its SHA-256 hash. The hash goes in `.env`, the raw key goes to the caller.

### 2. Configure environment

```bash
cp .env.example .env
# Fill in: MONGO_PASSWORD, SYNC_API_KEY_HASH, MCP_API_KEY_HASH
```

### 3. Start services

```bash
docker compose up -d
```

Three services start:
- **mongo** — internal only, not exposed outside Docker network
- **sync** — `localhost:8888`, receives build triggers from Jenkins
- **coverage-mcp** — `localhost:9090`, serves MCP tools to Claude

### 4. Configure Claude Code (VS Code extension)

Create `.mcp.json` in the project root:

```json
{
  "mcpServers": {
    "coverage": {
      "type": "sse",
      "url": "http://localhost:9090/sse",
      "headers": {
        "Authorization": "Bearer <raw-mcp-key>"
      }
    }
  }
}
```

Add to `~/.claude/settings.json`:
```json
{ "enabledMcpjsonServers": ["coverage"] }
```

Reload VS Code (`Cmd+Shift+P` → Developer: Reload Window).

### 5. Configure Jenkins

Add these as Jenkins credentials:
- `COVERAGE_S3_BUCKET` — your S3 bucket name
- `SYNC_SERVICE_URL` — `http://<your-local-ip>:8888/sync`
- `SYNC_API_KEY` — raw key from `sync/keygen.py`
- `aws-credentials` — AWS key pair with S3 read/write on your bucket

Add the pipeline stage from `jenkins_pipeline_snippet.groovy` to your `Jenkinsfile`.

---

## Sync service API

### `POST /sync`

Trigger a coverage sync for a build.

```
Authorization: Bearer <SYNC_API_KEY>
Content-Type: application/json

{
  "build_id":     "build-142-abc1234",   // required
  "branch":       "main",
  "commit":       "abc1234",
  "line_url":     "https://s3.amazonaws.com/...?X-Amz-Signature=...",  // required
  "function_url": "https://s3.amazonaws.com/...?X-Amz-Signature=..."   // optional
}
```

Response:
```json
{ "status": "ok", "build_id": "build-142-abc1234", "detail": "Upserted 1299 new, modified 0 files" }
```

### `GET /health`

```json
{ "status": "ok" }
```

---

## MongoDB schema

```
Collection: builds
  build_id, branch, commit, created_at, synced_at
  line_covered, line_missed, line_total, line_pct
  func_covered, func_missed, func_total, func_pct

Collection: file_coverage
  build_id, path
  line_covered, line_missed, line_total, line_pct
  uncovered_lines: [int]
  func_covered, func_missed, func_total, func_pct
  uncovered_funcs: [{name, start}]

Indexes:
  file_coverage: { build_id, path }         unique
  file_coverage: { build_id, line_missed }
  file_coverage: { build_id, func_missed }
  builds:        { created_at }
```

---

## File structure

```
quali-bot/
├── docker-compose.yml
├── .env.example
├── sync/
│   ├── sync.py        — HTTP server: POST /sync, GET /health
│   ├── keygen.py      — API key generator for sync endpoint
│   ├── requirements.txt
│   └── Dockerfile
└── coverage-mcp-server/
    ├── src/coverage_mcp/
    │   ├── server.py  — MCP tools + SSE ASGI app
    │   ├── db.py      — MongoDB query layer
    │   ├── parser.py  — local file parser (fallback mode)
    │   ├── fetcher.py — local file reader (fallback mode)
    │   └── config.py  — environment config
    ├── keygen.py      — API key generator for MCP endpoint
    ├── jenkins_pipeline_snippet.groovy
    ├── Dockerfile
    └── .env.example   — MCP server env vars
```

---

## Local dev (no MongoDB)

The MCP server falls back to reading a local JSON file when `MONGO_URI` is not set:

```bash
cd coverage-mcp-server
pip install -e .
COVERAGE_FILE_PATH=~/Downloads/index.json MCP_TRANSPORT=stdio coverage-mcp-server
```

Line coverage only in this mode — no function data.

---

## Example queries

```
List available builds
Summarize the latest build
Which folders have the lowest function coverage?
Get coverage for crates/router/src/core
Show uncovered functions in crates/router/src/core/payments.rs
List files with less than 5% coverage in the connectors folder
Which files have the most missed lines?
```
