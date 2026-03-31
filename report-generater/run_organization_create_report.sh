#!/usr/bin/env bash
set -euo pipefail

RG_ROOT="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${REPO_ROOT:-}"
SOURCE_ROOT="${SOURCE_ROOT:-${RG_ROOT}/source_snapshot}"
OUT_BASE="${RG_ROOT}/output"
RUN_ID="${RUN_ID:-$(date +%Y%m%d_%H%M%S)}"
OUT_DIR="${OUT_BASE}/${RUN_ID}"
LOCAL_CONFIG_DIR="${RG_ROOT}/config"
LOCAL_CONFIG_FILE="${LOCAL_CONFIG_DIR}/development.toml"

BASE_URL="${BASE_URL:-http://127.0.0.1:8080}"
HEALTH_PATH="${HEALTH_PATH:-/v2/health}"
HEALTH_TIMEOUT_S="${HEALTH_TIMEOUT_S:-120}"
SKIP_BUILD="${SKIP_BUILD:-1}"
CONFIG_FILE="${CONFIG_FILE:-${LOCAL_CONFIG_FILE}}"
ORG_NAME="${ORG_NAME:-random_org_$(date +%s)}"
AUTHORIZATION_HEADER="${AUTHORIZATION_HEADER:-}"

CHAIN_ARTIFACT="${RG_ROOT}/create_organization.json"
PROFRAW_DIR="${OUT_DIR}/profraw"
ROUTER_LOG="${OUT_DIR}/router_run.log"
RESP_BODY="${OUT_DIR}/organization_create_response.json"
RESP_HEADERS="${OUT_DIR}/organization_create_response_headers.txt"
RESP_META="${OUT_DIR}/organization_create_http.txt"
LCOV_FILE="${OUT_DIR}/lcov.info"
HTML_DIR="${OUT_DIR}/coverage-html"
DIFF_JSON="${OUT_DIR}/coverage_run_report.json"
LINE_HITS_TXT="${OUT_DIR}/line_hits.txt"
FINAL_REPORT_JSON="${OUT_DIR}/final_report.json"

mkdir -p "${OUT_DIR}" "${PROFRAW_DIR}"
mkdir -p "${LOCAL_CONFIG_DIR}"

if [[ ! -f "${LOCAL_CONFIG_FILE}" ]]; then
  echo "ERROR: missing local config ${LOCAL_CONFIG_FILE}" >&2
  echo "Keep config inside report-generater/config and re-run." >&2
  exit 1
fi

ADMIN_API_KEY="${ADMIN_API_KEY:-$(
  python3 - "${LOCAL_CONFIG_FILE}" <<'PY'
import re
import sys
from pathlib import Path
text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
m = re.search(r'(?m)^\s*admin_api_key\s*=\s*"([^"]+)"\s*$', text)
print(m.group(1) if m else "")
PY
)}"

if [[ -z "${ADMIN_API_KEY}" ]]; then
  echo "ERROR: could not resolve admin API key." >&2
  echo "Set ADMIN_API_KEY env var or define admin_api_key in ${LOCAL_CONFIG_FILE}." >&2
  exit 1
fi

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "Missing command: $1" >&2; exit 1; }
}

require_cmd curl
require_cmd python3
require_cmd grcov

if command -v rustc >/dev/null 2>&1; then
  HOST="$(rustc -vV | sed -n 's/^host: //p')"
  SYSROOT="$(rustc --print sysroot)"
  export PATH="${SYSROOT}/lib/rustlib/${HOST}/bin:${HOME}/.cargo/bin:${PATH}"
fi
require_cmd llvm-profdata

export RUSTFLAGS="-Cinstrument-coverage"
export LLVM_PROFILE_FILE="${PROFRAW_DIR}/router-%p-%m.profraw"

BUNDLED_BIN_DIR="${RG_ROOT}/bin"
BUNDLED_ROUTER_BIN="${BUNDLED_BIN_DIR}/router"
ROUTER_BIN="${BUNDLED_ROUTER_BIN}"

if [[ "${SKIP_BUILD}" != "1" ]]; then
  require_cmd cargo
  require_cmd jq
  if [[ -z "${REPO_ROOT}" || ! -d "${REPO_ROOT}/crates" ]]; then
    echo "ERROR: SKIP_BUILD=0 requires REPO_ROOT pointing to hyperswitch repo root." >&2
    exit 1
  fi
  FEATURES="$(cargo metadata --all-features --format-version 1 --no-deps --manifest-path "${REPO_ROOT}/Cargo.toml" | jq -r '
    [ .packages[] | select(.name == "router") | .features | keys[]
    | select(any(. ; test("(([a-z_]+)_)?v2"))) ] | join(",")
  ')"
  echo "==> Building instrumented router from REPO_ROOT=${REPO_ROOT}"
  cargo build --package router --bin router --no-default-features --features "${FEATURES}" --manifest-path "${REPO_ROOT}/Cargo.toml"
  mkdir -p "${BUNDLED_BIN_DIR}"
  cp "${REPO_ROOT}/target/debug/router" "${BUNDLED_ROUTER_BIN}"
  chmod +x "${BUNDLED_ROUTER_BIN}"
fi

if [[ ! -x "${ROUTER_BIN}" ]]; then
  echo "ERROR: bundled router binary not found at ${ROUTER_BIN}" >&2
  echo "Run once with SKIP_BUILD=0 and REPO_ROOT=/path/to/hyperswitch to bundle binary." >&2
  exit 1
fi
echo "==> Using bundled binary ${ROUTER_BIN}"

if [[ ! -d "${SOURCE_ROOT}/crates" ]]; then
  echo "ERROR: source snapshot missing at ${SOURCE_ROOT}" >&2
  echo "Bundle source files under report-generater/source_snapshot/crates/..." >&2
  exit 1
fi

ROUTER_CMD=( "${ROUTER_BIN}" )
if [[ -n "${CONFIG_FILE}" ]]; then
  if [[ "${CONFIG_FILE}" = /* ]]; then
    ROUTER_CMD+=( -f "${CONFIG_FILE}" )
  else
    ROUTER_CMD+=( -f "${RG_ROOT}/${CONFIG_FILE}" )
  fi
fi

ROUTER_PID=""
cleanup() {
  if [[ -n "${ROUTER_PID}" ]] && kill -0 "${ROUTER_PID}" 2>/dev/null; then
    kill -TERM "${ROUTER_PID}" 2>/dev/null || true
    wait "${ROUTER_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

echo "==> Starting router (log: ${ROUTER_LOG})"
: > "${ROUTER_LOG}"
"${ROUTER_CMD[@]}" >> "${ROUTER_LOG}" 2>&1 &
ROUTER_PID=$!

HEALTH_URL="${BASE_URL%/}${HEALTH_PATH}"
echo "==> Waiting for ${HEALTH_URL}"
ok=0
for _ in $(seq 1 "${HEALTH_TIMEOUT_S}"); do
  if curl -sS -o /dev/null -f "${HEALTH_URL}" 2>/dev/null; then
    ok=1
    break
  fi
  if ! kill -0 "${ROUTER_PID}" 2>/dev/null; then
    echo "Router exited before health became ready" >&2
    exit 1
  fi
  sleep 1
done
if [[ "${ok}" != "1" ]]; then
  echo "Router health timeout (${HEALTH_TIMEOUT_S}s)" >&2
  exit 1
fi

echo "==> Hitting organization_create"
curl_args=(
  --location "${BASE_URL%/}/v2/organizations"
  --header "Content-Type: application/json"
  --header "api-key: ${ADMIN_API_KEY}"
  --dump-header "${RESP_HEADERS}"
  --data "{\"organization_name\":\"${ORG_NAME}\"}"
  --silent --show-error
  --write-out "\nHTTP_CODE=%{http_code}\n"
  --output "${RESP_BODY}"
)
if [[ -n "${AUTHORIZATION_HEADER}" ]]; then
  curl_args+=( --header "Authorization: ${AUTHORIZATION_HEADER}" )
fi
curl "${curl_args[@]}" > "${RESP_META}" || true

REQUEST_ID="$(
  python3 - "${RESP_HEADERS}" <<'PY'
import re
import sys
from pathlib import Path

headers = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
m = re.search(r"(?im)^x-request-id:\s*([^\r\n]+)\s*$", headers)
print(m.group(1).strip() if m else "")
PY
)"

if [[ -z "${REQUEST_ID}" ]]; then
  echo "ERROR: Could not detect x-request-id from response headers at ${RESP_HEADERS}" >&2
  echo "Please check whether router is returning x-request-id in response headers." >&2
  exit 1
fi
echo "==> Captured request_id=${REQUEST_ID}"

HTTP_CODE="$(
  python3 - "${RESP_META}" <<'PY'
import re
import sys
from pathlib import Path
text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
m = re.search(r'HTTP_CODE=(\d{3})', text)
print(m.group(1) if m else "")
PY
)"
if [[ "${HTTP_CODE}" != "200" ]]; then
  echo "ERROR: create_organization call failed with HTTP_CODE=${HTTP_CODE}" >&2
  echo "Response body:" >&2
  cat "${RESP_BODY}" >&2 || true
  exit 1
fi

echo "==> Stopping router to flush .profraw"
cleanup
trap - EXIT
ROUTER_PID=""

PROFRAW_COUNT="$(python3 - "${PROFRAW_DIR}" <<'PY'
import sys
from pathlib import Path
p = Path(sys.argv[1])
print(len(list(p.glob("*.profraw"))))
PY
)"
if [[ "${PROFRAW_COUNT}" == "0" ]]; then
  echo "ERROR: no .profraw files were generated under ${PROFRAW_DIR}" >&2
  echo "This usually means the bundled router binary is not LLVM-instrumented." >&2
  echo "Rebuild+bundle once with: REPO_ROOT=/path/to/hyperswitch SKIP_BUILD=0 bash run_organization_create_report.sh" >&2
  exit 1
fi

echo "==> Generating coverage HTML + lcov"
grcov "${PROFRAW_DIR}" \
  --source-dir "${SOURCE_ROOT}" \
  --binary-path "${BUNDLED_BIN_DIR}" \
  --output-type html \
  --output-path "${HTML_DIR}" \
  --keep-only "crates/*" \
  --ignore-not-existing

grcov "${PROFRAW_DIR}" \
  --source-dir "${SOURCE_ROOT}" \
  --binary-path "${BUNDLED_BIN_DIR}" \
  --output-type lcov \
  --output-path "${LCOV_FILE}" \
  --keep-only "crates/*" \
  --ignore-not-existing

echo "==> Generating path-flow diff + line hits"
python3 "${RG_ROOT}/coverage_feedback_loop.py" \
  --chain-artifact "${CHAIN_ARTIFACT}" \
  --lcov "${LCOV_FILE}" \
  --repo-root "${SOURCE_ROOT}" \
  --print-line-hits \
  --out "${DIFF_JSON}" \
  > "${OUT_DIR}/coverage_feedback_stdout.json" \
  2> "${LINE_HITS_TXT}"

echo "==> Generating final RCA report"
python3 "${RG_ROOT}/generate_final_report.py" \
  --request-id "${REQUEST_ID}" \
  --router-log "${ROUTER_LOG}" \
  --coverage-report "${DIFF_JSON}" \
  --out "${FINAL_REPORT_JSON}"

cat > "${OUT_DIR}/run_summary.txt" <<EOF
Run ID: ${RUN_ID}
Request ID: ${REQUEST_ID}
Organization Name: ${ORG_NAME}

Artifacts:
- Router log: ${ROUTER_LOG}
- Curl response body: ${RESP_BODY}
- Curl response headers: ${RESP_HEADERS}
- Curl response meta: ${RESP_META}
- LCOV: ${LCOV_FILE}
- Coverage HTML: ${HTML_DIR}
- Diff summary JSON: ${DIFF_JSON}
- Line hits text: ${LINE_HITS_TXT}
- Final RCA report JSON: ${FINAL_REPORT_JSON}
EOF

echo "==> Done. Output folder: ${OUT_DIR}"
