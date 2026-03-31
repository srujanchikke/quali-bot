#!/usr/bin/env bash
set -euo pipefail

# ============================================================================
# run_flow_coverage_report.sh
# ============================================================================
# Integrates testing_agent flow pipeline with LLVM coverage reporting.
# 
# Usage:
#   bash report-generater/run_flow_coverage_report.sh
# 
# Env vars (optional):
#   HYPERSWITCH_ROOT   - Path to hyperswitch repo (default: ~/hyperswitch)
#   CYPRESS_TESTS_ROOT - Path to cypress-tests repo (default: ${HYPERSWITCH_ROOT}/cypress-tests)
#   CYPRESS_REPO       - If set, overrides CYPRESS_TESTS_ROOT for flow pipeline / Cypress repo path
#   FLOW_JSON          - Path to flow JSON file
#   SKIP_RUN           - If 1, skip cypress run (just generate coverage)
#   BASE_URL           - Router base URL (default: http://localhost:8080; use "localhost" for Cypress)
#   HEALTH_PATH        - Health check path; if unset, tries /health (v1) then /v2/health (v2-only)
#
# Cypress note: hyperswitch cypress-tests use the v1 admin API (/accounts, /account/...).
# The router must be a v1 build (e.g. `just run` / default features), not v2-only (`just run_v2`).
# v1 and v2 are mutually exclusive — build with: cargo build -p router --bin router
# ============================================================================

RG_ROOT="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${RG_ROOT}/.." && pwd)"

# Defaults
HYPERSWITCH_ROOT="${HYPERSWITCH_ROOT:-${HOME}/hyperswitch}"
CYPRESS_TESTS_ROOT="${CYPRESS_TESTS_ROOT:-${HYPERSWITCH_ROOT}/cypress-tests}"
# Prefer explicit CYPRESS_REPO when set (alternate clone path); else use CYPRESS_TESTS_ROOT.
CYPRESS_REPO_RESOLVED="${CYPRESS_REPO:-${CYPRESS_TESTS_ROOT}}"
FLOW_JSON="${FLOW_JSON:-${PROJECT_ROOT}/testing_agent/adyen_get_auth_header_output.json}"
SKIP_RUN="${SKIP_RUN:-0}"

# Output directories
OUT_BASE="${RG_ROOT}/output"
RUN_ID="${RUN_ID:-$(date +%Y%m%d_%H%M%S)}"
OUT_DIR="${OUT_BASE}/${RUN_ID}"
PROFRAW_DIR="${OUT_DIR}/profraw"
ROUTER_LOG="${OUT_DIR}/router_run.log"
TERMINAL_LOG="${OUT_DIR}/terminal_output.log"
LCOV_FILE="${OUT_DIR}/lcov.info"
HTML_DIR="${OUT_DIR}/coverage-html"
DIFF_JSON="${OUT_DIR}/coverage_run_report.json"
LINE_HITS_TXT="${OUT_DIR}/line_hits.txt"
FINAL_REPORT_JSON="${OUT_DIR}/final_report.json"
FLOW_RUN_LOG="${OUT_DIR}/flow_pipeline.log"
CYPRESS_PARSED_JSON="${OUT_DIR}/cypress_parsed.json"

mkdir -p "${OUT_DIR}" "${PROFRAW_DIR}"

# Persist the full script stdout/stderr so the UI can show the same terminal stream.
: > "${TERMINAL_LOG}"
exec > >(tee -a "${TERMINAL_LOG}") 2>&1

# ============================================================================
# Helper functions
# ============================================================================

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "Missing command: $1" >&2; exit 1; }
}

sep() {
  echo ""
  echo "==> $1"
}

# ============================================================================
# Validate environment
# ============================================================================

require_cmd curl
require_cmd python3
require_cmd grcov

# testing_agent dependency (used by flow_query.py)
if ! python3 -c "import neo4j" >/dev/null 2>&1; then
  sep "Installing missing Python dependency: neo4j"
  python3 -m pip install --user neo4j >/dev/null
fi

if command -v rustc >/dev/null 2>&1; then
  HOST="$(rustc -vV | sed -n 's/^host: //p')"
  SYSROOT="$(rustc --print sysroot)"
  export PATH="${SYSROOT}/lib/rustlib/${HOST}/bin:${HOME}/.cargo/bin:${PATH}"
fi

# Check for llvm-profdata (via xcrun on macOS or direct on Linux)
if command -v xcrun >/dev/null 2>&1 && xcrun llvm-profdata --version >/dev/null 2>&1; then
  LLVM_PROFDATA="xcrun llvm-profdata"
elif command -v llvm-profdata >/dev/null 2>&1; then
  LLVM_PROFDATA="llvm-profdata"
else
  echo "ERROR: llvm-profdata not found. Install LLVM tools:" >&2
  echo "  macOS: Xcode command line tools (xcode-select --install)" >&2
  echo "  Linux: rustup component add llvm-tools-preview" >&2
  exit 1
fi

if [[ ! -d "${HYPERSWITCH_ROOT}/crates" ]]; then
  echo "ERROR: HYPERSWITCH_ROOT must point to hyperswitch repo root" >&2
  echo "  Current: ${HYPERSWITCH_ROOT}" >&2
  echo "  Set HYPERSWITCH_ROOT env var or update the default in this script" >&2
  exit 1
fi

if [[ ! -f "${FLOW_JSON}" ]]; then
  echo "ERROR: Flow JSON not found: ${FLOW_JSON}" >&2
  exit 1
fi

# Extract flow info for logging
FLOW_ID="$(python3 - "${FLOW_JSON}" <<'PY'
import json
import sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
flows = data.get("flows", [])
if flows:
    print(flows[0].get("flow_id", "?"))
else:
    print("?")
PY
)"

FLOW_DESC="$(python3 - "${FLOW_JSON}" <<'PY'
import json
import sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
flows = data.get("flows", [])
if flows:
    print(flows[0].get("description", "")[:60])
else:
    print("unknown flow")
PY
)"

TARGET_LEAF="$(python3 - "${FLOW_JSON}" <<'PY'
import json
import sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
for flow in data.get("flows", []):
    for step in flow.get("chain", []):
        if step.get("role") == "target":
            print(step.get("function", "?"))
            sys.exit(0)
print("?")
PY
)"

CHANGED_FUNC="$(python3 - "${FLOW_JSON}" <<'PY'
import json
import sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
print(data.get("changed_function", "?"))
PY
)"

echo "============================================================"
echo "  Flow Coverage Report"
echo "============================================================"
echo "  Run ID          : ${RUN_ID}"
echo "  Flow ID         : ${FLOW_ID}"
echo "  Description     : ${FLOW_DESC}"
echo "  Changed function: ${CHANGED_FUNC}"
echo "  Target leaf     : ${TARGET_LEAF}"
echo "  Hyperswitch     : ${HYPERSWITCH_ROOT}"
echo "  Cypress tests   : ${CYPRESS_REPO_RESOLVED}"
echo "  Flow JSON       : ${FLOW_JSON}"
echo "  Output dir      : ${OUT_DIR}"
echo "============================================================"

# ============================================================================
# Step 1: Start hyperswitch router with LLVM coverage
# ============================================================================

BASE_URL="${BASE_URL:-http://localhost:8080}"
HEALTH_TIMEOUT_S="${HEALTH_TIMEOUT_S:-120}"
ROUTER_CONFIG="${ROUTER_CONFIG:-${HYPERSWITCH_ROOT}/config/development.toml}"
# HEALTH_PATH: leave unset to probe /health (v1) then /v2/health (v2-only binary).

export RUSTFLAGS="-Cinstrument-coverage"
# macOS compatibility: avoid %p (process id) which can cause profile writes to fail/land in unexpected locations.
export LLVM_PROFILE_FILE="${PROFRAW_DIR}/router-%m.profraw"

ROUTER_PID=""
cleanup_router() {
  if [[ -n "${ROUTER_PID}" ]] && kill -0 "${ROUTER_PID}" 2>/dev/null; then
    sep "Stopping router (PID: ${ROUTER_PID})"
    kill -TERM "${ROUTER_PID}" 2>/dev/null || true
    wait "${ROUTER_PID}" 2>/dev/null || true
    ROUTER_PID=""
  fi
}
trap cleanup_router EXIT

if [[ "${SKIP_RUN}" != "1" ]]; then
  sep "Starting hyperswitch router with LLVM coverage"
  echo "  Config: ${ROUTER_CONFIG}"
  echo "  Log: ${ROUTER_LOG}"
  echo "  Tip: cypress-tests expect a v1 API router at BASE_URL (see script header). Build: cargo build -p router --bin router"
  
  : > "${ROUTER_LOG}"
  
  # Use pre-built instrumented binary instead of cargo run
  ROUTER_BIN="${HYPERSWITCH_ROOT}/target/debug/router"
  
  if [[ ! -x "${ROUTER_BIN}" ]]; then
    echo "ERROR: Instrumented router binary not found at ${ROUTER_BIN}" >&2
    echo "Build it first with: RUSTFLAGS=\"-Cinstrument-coverage\" cargo build --bin router" >&2
    exit 1
  fi
  
  cd "${HYPERSWITCH_ROOT}"
  "${ROUTER_BIN}" -f "${ROUTER_CONFIG}" >> "${ROUTER_LOG}" 2>&1 &
  ROUTER_PID=$!
  cd "${PROJECT_ROOT}"
  
  echo "  Router binary: ${ROUTER_BIN}"
  echo "  Router PID: ${ROUTER_PID}"

  # Wait for health (/health for v1, /v2/health for v2-only — probe if HEALTH_PATH unset)
  sep "Waiting for router health (${BASE_URL})"
  ok=0
  for _ in $(seq 1 "${HEALTH_TIMEOUT_S}"); do
    if [[ -n "${HEALTH_PATH:-}" ]]; then
      HEALTH_URL="${BASE_URL%/}${HEALTH_PATH}"
      if curl -sS -o /dev/null -f "${HEALTH_URL}" 2>/dev/null; then
        ok=1
        break
      fi
    else
      for CAND in /health /v2/health; do
        HEALTH_URL="${BASE_URL%/}${CAND}"
        if curl -sS -o /dev/null -f "${HEALTH_URL}" 2>/dev/null; then
          HEALTH_PATH="$CAND"
          ok=1
          break 2
        fi
      done
    fi
    if ! kill -0 "${ROUTER_PID}" 2>/dev/null; then
      echo "ERROR: Router exited before health became ready" >&2
      echo "Last 50 lines of router log:" >&2
      tail -50 "${ROUTER_LOG}" >&2
      exit 1
    fi
    sleep 1
  done
  
  if [[ "${ok}" != "1" ]]; then
    echo "ERROR: Router health timeout (${HEALTH_TIMEOUT_S}s); tried ${HEALTH_PATH:-/health and /v2/health}" >&2
    exit 1
  fi
  echo "  Router is healthy at ${HEALTH_URL:-${BASE_URL%/}${HEALTH_PATH}}"

  # ============================================================================
  # Step 2: Run testing_agent flow pipeline
  # ============================================================================

  sep "Running flow pipeline"
  echo "  Flow: ${FLOW_JSON}"
  echo "  Repo: ${CYPRESS_REPO_RESOLVED}"
  
  # Set env vars for cypress
  export CYPRESS_BASE_URL="${BASE_URL}"
  export CYPRESS_REPO="${CYPRESS_REPO_RESOLVED}"
  
  # Run the flow pipeline and capture output
  python3 "${PROJECT_ROOT}/testing_agent/run_flow_pipeline.py" \
    --flow "${FLOW_JSON}" \
    --repo "${CYPRESS_REPO_RESOLVED}" \
    --verbose \
    2>&1 | tee "${FLOW_RUN_LOG}" || true
  
  echo "  Flow pipeline completed"

  # ============================================================================
  # Step 3: Parse cypress output for request IDs
  # ============================================================================

  sep "Parsing cypress output"
  python3 "${RG_ROOT}/parse_cypress_output.py" \
    --log "${FLOW_RUN_LOG}" \
    --out "${CYPRESS_PARSED_JSON}"
  
  REQUEST_IDS="$(python3 - "${CYPRESS_PARSED_JSON}" <<'PY'
import json
import sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
ids = data.get("request_ids", [])
print(",".join(ids) if ids else "")
PY
)"

  echo "  Request IDs found: ${REQUEST_IDS:-none}"

  # ============================================================================
  # Step 4: Stop router to flush .profraw
  # ============================================================================

  sep "Stopping router to flush .profraw"
  cleanup_router
  trap - EXIT
fi

# ============================================================================
# Step 5: Verify profraw files exist
# ============================================================================

PROFRAW_COUNT="$(python3 - "${PROFRAW_DIR}" <<'PY'
import sys
from pathlib import Path
p = Path(sys.argv[1])
print(len(list(p.glob("*.profraw"))))
PY
)"

if [[ "${PROFRAW_COUNT}" == "0" ]]; then
  # Fallback: some LLVM/rust setups write default_*.profraw to repo root.
  ROOT_PROFRAW_FOUND="$(python3 - "${HYPERSWITCH_ROOT}" "${PROFRAW_DIR}" <<'PY'
import shutil
import sys
from pathlib import Path
root = Path(sys.argv[1])
dest = Path(sys.argv[2])
dest.mkdir(parents=True, exist_ok=True)
files = sorted(root.glob("*.profraw"))
count = 0
for f in files:
    shutil.move(str(f), str(dest / f.name))
    count += 1
print(count)
PY
)"
  if [[ "${ROOT_PROFRAW_FOUND}" != "0" ]]; then
    PROFRAW_COUNT="${ROOT_PROFRAW_FOUND}"
  else
    echo "WARN: no .profraw files were generated under ${PROFRAW_DIR}" >&2
    echo "  Continuing without coverage generation; final report will be partial." >&2
    COVERAGE_AVAILABLE=0
  fi
fi
if [[ "${COVERAGE_AVAILABLE:-1}" == "1" ]]; then
  echo "  Found ${PROFRAW_COUNT} .profraw files"
fi

# ============================================================================
# Step 6: Generate coverage HTML + lcov
# ============================================================================

# Determine llvm-path for grcov
if command -v xcrun >/dev/null 2>&1; then
  LLVM_PATH="$(xcrun --find llvm-profdata | xargs dirname)"
elif [[ -n "${SYSROOT}" ]]; then
  LLVM_PATH="${SYSROOT}/lib/rustlib/${HOST}/bin"
else
  LLVM_PATH=""
fi

GRCOV_LLVM_ARGS=""
if [[ -n "${LLVM_PATH}" ]]; then
  GRCOV_LLVM_ARGS="--llvm-path ${LLVM_PATH}"
fi

if [[ "${COVERAGE_AVAILABLE:-1}" == "1" ]]; then
  sep "Generating coverage HTML + lcov"
  grcov "${PROFRAW_DIR}" \
    --source-dir "${HYPERSWITCH_ROOT}" \
    --binary-path "${HYPERSWITCH_ROOT}/target/debug" \
    --output-type html \
    --output-path "${HTML_DIR}" \
    --keep-only "crates/*" \
    --ignore-not-existing \
    ${GRCOV_LLVM_ARGS}

  grcov "${PROFRAW_DIR}" \
    --source-dir "${HYPERSWITCH_ROOT}" \
    --binary-path "${HYPERSWITCH_ROOT}/target/debug" \
    --output-type lcov \
    --output-path "${LCOV_FILE}" \
    --keep-only "crates/*" \
    --ignore-not-existing \
    ${GRCOV_LLVM_ARGS}

  echo "  LCOV: ${LCOV_FILE}"
  echo "  HTML: ${HTML_DIR}"

  # ============================================================================
  # Step 7: Generate path-flow diff + line hits
  # ============================================================================
  sep "Generating path-flow diff + line hits"
  python3 "${RG_ROOT}/coverage_feedback_loop.py" \
    --chain-artifact "${FLOW_JSON}" \
    --lcov "${LCOV_FILE}" \
    --repo-root "${HYPERSWITCH_ROOT}" \
    --print-line-hits \
    --out "${DIFF_JSON}" \
    > "${OUT_DIR}/coverage_feedback_stdout.json" \
    2> "${LINE_HITS_TXT}"
else
  # Produce minimal artifacts so final_report generation still works.
  printf '{}' > "${OUT_DIR}/coverage_feedback_stdout.json"
  printf 'Coverage unavailable: no profraw generated\n' > "${LINE_HITS_TXT}"
  cat > "${DIFF_JSON}" <<'EOF'
{
  "d": {
    "kind": "coverage_unavailable",
    "leaf": {},
    "gaps": [],
    "error": "no profraw generated"
  }
}
EOF
fi

# ============================================================================
# Step 8: Generate final RCA report
# ============================================================================

sep "Generating final RCA report"
if [[ -n "${REQUEST_IDS:-}" ]]; then
  FIRST_REQUEST_ID="$(echo "${REQUEST_IDS}" | cut -d',' -f1)"
else
  FIRST_REQUEST_ID="unknown"
fi

python3 "${RG_ROOT}/generate_final_report.py" \
  --request-id "${FIRST_REQUEST_ID}" \
  --router-log "${ROUTER_LOG}" \
  --coverage-report "${DIFF_JSON}" \
  --flow-json "${FLOW_JSON}" \
  --cypress-parsed "${CYPRESS_PARSED_JSON}" \
  --out "${FINAL_REPORT_JSON}"

# ============================================================================
# Step 9: Generate run summary
# ============================================================================

cat > "${OUT_DIR}/run_summary.txt" <<EOF
Run ID: ${RUN_ID}
Flow ID: ${FLOW_ID}
Description: ${FLOW_DESC}
Changed function: ${CHANGED_FUNC}
Target leaf: ${TARGET_LEAF}
Request IDs: ${REQUEST_IDS:-none}

Artifacts:
- Terminal log: ${TERMINAL_LOG}
- Router log: ${ROUTER_LOG}
- Flow pipeline log: ${FLOW_RUN_LOG}
- Cypress parsed: ${CYPRESS_PARSED_JSON}
- LCOV: ${LCOV_FILE}
- Coverage HTML: ${HTML_DIR}
- Diff summary JSON: ${DIFF_JSON}
- Line hits text: ${LINE_HITS_TXT}
- Final RCA report JSON: ${FINAL_REPORT_JSON}
EOF

sep "Done"
echo "  Output folder: ${OUT_DIR}"
echo "  Summary: ${OUT_DIR}/run_summary.txt"
echo "  Final report: ${FINAL_REPORT_JSON}"