# report-generater

Coverage-based Root Cause Analysis (RCA) report generation for Hyperswitch API flows.

## Two Modes

### 1. Flow Coverage Report (New)

Runs a full cypress test flow via `testing_agent/run_flow_pipeline.py` and generates coverage analysis:

```bash
bash report-generater/run_flow_coverage_report.sh
```

#### Build the router with LLVM coverage first

Before running the flow coverage report, build the Hyperswitch router with LLVM
coverage instrumentation enabled:

```bash
cd "$HYPERSWITCH_ROOT"
RUSTFLAGS="-Cinstrument-coverage" cargo build -p router --bin router
```

Notes:

- Use the default v1 router build for Cypress compatibility.
- Do not use a v2-only build for this workflow, because the Cypress setup flow uses
  v1 admin routes such as `/accounts`.
- If the router is not built with `-Cinstrument-coverage`, no `.profraw` files will
  be generated, and the report will show coverage as unavailable.

This:
1. Starts hyperswitch router with LLVM coverage from `~/hyperswitch`
2. Runs `testing_agent/run_flow_pipeline.py` with the flow JSON
3. Parses cypress output for request IDs
4. Stops router to flush `.profraw`
5. Generates `lcov.info` and HTML coverage
6. Generates path-flow diff + line-hit report
7. Generates final log-correlated RCA report JSON

#### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HYPERSWITCH_ROOT` | `~/hyperswitch` | Path to hyperswitch repo |
| `CYPRESS_TESTS_ROOT` | `$HYPERSWITCH_ROOT/cypress-tests` | Path to cypress-tests repo |
| `FLOW_JSON` | `testing_agent/adyen_get_auth_header_output.json` | Flow JSON to run |
| `BASE_URL` | `http://127.0.0.1:8080` | Router base URL |
| `SKIP_RUN` | `0` | If `1`, skip cypress run (just generate coverage) |
| `BUILD_INSTRUMENTED_ROUTER` | `0` | Rebuild router with `-Cinstrument-coverage` before run |

#### Example

```bash
HYPERSWITCH_ROOT=/Users/chikke.srujan/hyperswitch \
CYPRESS_TESTS_ROOT=/Users/chikke.srujan/hyperswitch/cypress-tests \
FLOW_JSON=testing_agent/adyen_get_auth_header_output.json \
bash report-generater/run_flow_coverage_report.sh
```

### 2. Organization Create Report (Legacy)

Single API call test for `POST /v2/organizations`:

```bash
bash report-generater/run_organization_create_report.sh
```

See legacy documentation below.

---

## Output

Each run writes under:

`report-generater/output/<RUN_ID>/`

### Flow Coverage Report Artifacts

| File | Description |
|------|-------------|
| `terminal_output.log` | Full script terminal stdout/stderr for the run |
| `router_run.log` | Router stdout/stderr |
| `flow_pipeline.log` | Cypress test output |
| `cypress_parsed.json` | Parsed request IDs and test results |
| `lcov.info` | LCOV coverage data |
| `coverage-html/` | HTML coverage report |
| `coverage_run_report.json` | Coverage gap analysis |
| `line_hits.txt` | Per-line hit counts |
| `final_report.json` | RCA report with log correlation |
| `run_summary.txt` | Human-readable summary |

### Final Report Structure

```json
{
  "request_id": "abc123",
  "flow_info": {
    "flow_id": 1,
    "description": "...",
    "changed_function": "WellsFargo#ConnectorIntegration...",
    "target_leaf": "build_request"
  },
  "api_call": {
    "method": "POST",
    "endpoint": "/payments/{payment_id}/incremental_authorization",
    "http_status_code": 200
  },
  "test_results": {
    "cypress_passed": true,
    "passing_count": 5,
    "failing_count": 0
  },
  "coverage_diff": {
    "gap_status": "all_probed_lines_hit",
    "line_coverage_ratio": 1.0
  },
  "root_cause_analysis": {
    "status": "not_applicable_for_this_run",
    "reason": "Request succeeded and all probed leaf lines were hit."
  }
}
```

---

## Scripts

| Script | Purpose |
|--------|---------|
| `run_flow_coverage_report.sh` | Main orchestrator for flow coverage |
| `parse_cypress_output.py` | Extract request IDs from cypress output |
| `coverage_flow_gap.py` | Compute coverage gaps for leaf functions |
| `coverage_feedback_loop.py` | Generate coverage diff reports |
| `generate_final_report.py` | Build RCA JSON with log correlation |
| `build_source_index.py` | Index source files for LLM context |

---

## Legacy: Organization Create Report

Single-place POC flow for:

1. Start router with LLVM coverage instrumentation.
2. Reuse existing build (`SKIP_BUILD=1`) or build fresh automatically.
3. Hit `POST /v2/organizations` with `x-request-id`.
4. Stop router to flush `.profraw`.
5. Generate `lcov.info` and HTML coverage.
6. Generate path-flow diff + line-hit report.
7. Generate final log-correlated RCA report JSON.

### Run

Default run (fully from handoff package after one-time bundling):

```bash
bash report-generater/run_organization_create_report.sh
```

Optional env:

```bash
SKIP_BUILD=1 ORG_NAME="random_org_123" bash report-generater/run_organization_create_report.sh
```

Defaults:

- Start: bundled `report-generater/bin/router -f report-generater/config/development.toml`
- Logs: saved under each run output folder (`router_run.log`)

### One-time bundle step

If `report-generater/bin/router` is missing, build and bundle once from a hyperswitch checkout:

```bash
REPO_ROOT="/path/to/hyperswitch" SKIP_BUILD=0 bash report-generater/run_organization_create_report.sh
```

This compiles instrumented router and copies binary into `report-generater/bin/router`.

Config locality:

- Primary config is `report-generater/config/development.toml`.
- Runner requires this local config file and does not auto-copy from outside.

### One-time source index

Build once and keep it in this folder for shared LLM context:

```bash
python3 report-generater/build_source_index.py \
  --repo-root . \
  --chain-artifact report-generater/create_organization.json \
  --out report-generater/source_index.json
```
