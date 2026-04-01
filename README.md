# quali-bot

`quali-bot` is a local automation workspace for:

- generating or extending Cypress connector test coverage with LLM assistance
- running a flow-based test pipeline from flow JSON inputs
- producing coverage and RCA-style reports for Hyperswitch API flows

The repo is split into two main parts:

- `testing_agent/`: flow analysis, coverage lookup, prompt building, code generation, and Cypress execution
- `report-generater/`: orchestration scripts that run the router with coverage enabled and produce report artifacts

## Repository Layout

```text
quali-bot/
├── testing_agent/
│   ├── run_flow_pipeline.py
│   ├── flow_query.py
│   ├── flow_context.py
│   ├── codegen.py
│   └── runner.py
├── report-generater/
│   ├── run_flow_coverage_report.sh
│   ├── run_organization_create_report.sh
│   ├── generate_final_report.py
│   ├── coverage_flow_gap.py
│   ├── parse_cypress_output.py
│   └── README.md
└── README.md
```

## What It Does

### 1. Testing Agent

The `testing_agent` pipeline takes a flow JSON file and tries to determine whether an existing Cypress test already covers the flow. If coverage is missing, it builds context, calls an LLM through a Grid-compatible API, writes the spec changes, and can run the generated Cypress spec.

High-level pipeline:

1. Read flow JSON input
2. Check coverage against the Cypress repo
3. Build context for the missing flow
4. Generate or patch test code using Grid / LLM
5. Run Cypress to validate the generated spec

### 2. Report Generator

The `report-generater` scripts run Hyperswitch with LLVM coverage enabled, execute a target flow, collect `.profraw` / `lcov.info`, and generate final report artifacts such as:

- `coverage_run_report.json`
- `final_report.json`
- `line_hits.txt`
- `coverage-html/`

The generated runtime output goes under:

`report-generater/output/<RUN_ID>/`

These generated artifacts are local-only and should not be committed.

## Prerequisites

Install the following before using the repo:

- `python3`
- `pip`
- `curl`
- `grcov`
- LLVM profiling tools:
  - macOS: Xcode command line tools / `xcrun llvm-profdata`
  - Linux: `llvm-profdata`
- Rust toolchain if you need to build or run the Hyperswitch router locally

Depending on the workflow, you may also need:

- a local `hyperswitch` checkout
- a local `cypress-tests` checkout
- a running Neo4j instance for flow coverage queries
- Grid-compatible LLM credentials for code generation

## Setup

### 1. Clone and enter the repo

```bash
git clone <your-repo-url>
cd quali-bot
```

### 2. Create a Python virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install neo4j
```

If additional Python dependencies are required in your environment, install them as your run flow demands.

### 3. Prepare external repos

Set up these local dependencies if you want to run the full pipeline:

- `HYPERSWITCH_ROOT`: path to your local Hyperswitch repo
- `CYPRESS_TESTS_ROOT`: path to your local `cypress-tests` repo

Example:

```bash
export HYPERSWITCH_ROOT="$HOME/hyperswitch"
export CYPRESS_TESTS_ROOT="$HYPERSWITCH_ROOT/cypress-tests"
export CYPRESS_REPO="$CYPRESS_TESTS_ROOT"
```

### 4. Optional: configure Grid / LLM access

`testing_agent/codegen.py` uses a Grid-compatible OpenAI-style endpoint.

Set:

```bash
export GRID_API_KEY="your_key"
export GRID_BASE_URL="https://your-grid-endpoint.com"
export GRID_MODEL="glm-latest"
```

Optional overrides:

```bash
export GRID_ENDPOINT_PATH="/v1/chat/completions"
export GRID_AUTH_HEADER="Authorization"
export GRID_AUTH_PREFIX="Bearer "
```

### 5. Optional: configure Neo4j

```bash
export NEO4J_URI="bolt://localhost:7687"
export NEO4J_USER="neo4j"
export NEO4J_PASSWORD="Hyperswitch123"
```

## Running The Main Workflows

### Run the flow pipeline

This uses `testing_agent/run_flow_pipeline.py`.

```bash
python3 testing_agent/run_flow_pipeline.py \
  --flow testing_agent/input.json
```

Useful options:

```bash
python3 testing_agent/run_flow_pipeline.py \
  --flow testing_agent/input.json \
  --dry-run \
  --verbose
```

What it expects:

- a valid flow JSON input
- access to the Cypress repo
- Neo4j if coverage lookup is needed
- Grid credentials if code generation is needed

### Run the flow coverage report

This is the main reporting workflow.

Before running it, make sure your local Hyperswitch router binary is already built
with LLVM coverage instrumentation. The script reuses the existing binary at
`target/debug/router` by default.

Build it from your local `hyperswitch` checkout:

```bash
cd "$HYPERSWITCH_ROOT"
RUSTFLAGS="-Cinstrument-coverage" cargo build -p router --bin router
```

Important:

- Build the default v1 router for Cypress compatibility. Do not use a v2-only build.
- The Cypress setup specs in `cypress-tests` call v1 admin routes such as `/accounts`.
- If you rebuild the router without `RUSTFLAGS="-Cinstrument-coverage"`, `.profraw`
  files will not be generated and coverage output will be partial.
- If you do want the script to rebuild the binary for you, run it with
  `BUILD_INSTRUMENTED_ROUTER=1`.

```bash
bash report-generater/run_flow_coverage_report.sh
```

Common environment variables:

```bash
export HYPERSWITCH_ROOT="$HOME/hyperswitch"
export CYPRESS_TESTS_ROOT="$HYPERSWITCH_ROOT/cypress-tests"
export FLOW_JSON="$PWD/testing_agent/input.json"
export BASE_URL="http://localhost:8080"
```

Example:

```bash
HYPERSWITCH_ROOT="$HOME/hyperswitch" \
CYPRESS_TESTS_ROOT="$HOME/hyperswitch/cypress-tests" \
FLOW_JSON="$PWD/testing_agent/input.json" \
bash report-generater/run_flow_coverage_report.sh
```

### Run the organization create report

This is a more focused legacy workflow for `POST /v2/organizations`.

```bash
bash report-generater/run_organization_create_report.sh
```

If you need to bundle a router binary from a local Hyperswitch checkout:

```bash
REPO_ROOT="$HOME/hyperswitch" \
SKIP_BUILD=0 \
bash report-generater/run_organization_create_report.sh
```

### Run the UI

The report viewer UI lives in `report-generater/ui` and is built with React, TypeScript, and Vite.

Install dependencies:

```bash
cd report-generater/ui
npm install
```

Start the local development server:

```bash
cd report-generater/ui
npm run dev
```

Vite will print a local URL in the terminal, usually `http://localhost:5173`.

Build the UI for production:

```bash
cd report-generater/ui
npm run build
```

Preview the production build locally:

```bash
cd report-generater/ui
npm run preview
```

Run the UI linter:

```bash
cd report-generater/ui
npm run lint
```

## Important Notes

- `report-generater/output/` contains generated runtime artifacts and can become very large.
- `testing_agent/scip/` is a nested git repository / local dependency and should not be added casually.
- `report-generater/local_test_env.sh` contains local-only config and is intentionally ignored.
- The coverage report script expects a compatible local Hyperswitch router build and matching Cypress tests setup.

## Generated Output

Typical output from report runs includes:

- `router_run.log`
- `flow_pipeline.log`
- `cypress_parsed.json`
- `lcov.info`
- `coverage-html/`
- `coverage_run_report.json`
- `final_report.json`
- `run_summary.txt`

All of these are generated during execution and should stay out of version control.

## Where To Look Next

- `report-generater/README.md` for deeper details on reporting modes
- `testing_agent/codegen.py` for Grid / LLM integration
- `testing_agent/run_flow_pipeline.py` for the end-to-end flow runner
