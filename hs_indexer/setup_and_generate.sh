#!/usr/bin/env bash
set -euo pipefail

# ============================================================================
# setup_and_generate.sh
# ============================================================================
# Two subcommands:
#
#   index  — Generate index.scip from source, then load the call graph,
#             trait map, and guard annotations into Neo4j. Run once per
#             source change. Prints a ready message when done.
#
#   query  — Query the Neo4j graph by file + line to find impacted API
#             flows and write testing_agent/input.json.
#
# Usage:
#   bash hs_indexer/setup_and_generate.sh index
#   bash hs_indexer/setup_and_generate.sh query <function_name>
#   bash hs_indexer/setup_and_generate.sh query --file <relative/path.rs> --line <n>
#   bash hs_indexer/setup_and_generate.sh query <function_name> --file <path.rs> --line <n>
#
# When to regenerate index.scip:
#   - Hyperswitch Rust source changed  → run index normally (SKIP_SCIP=0, the default)
#   - Only hs_indexer Python files changed (build_trait_map.py, annotate_guards.py, etc.)
#                                      → SKIP_SCIP=1  (reuse existing index.scip, re-load Neo4j)
#   - Neither changed, just re-querying → skip the index subcommand entirely, run query directly
#
# Env vars (index):
#   HYPERSWITCH_ROOT  - Path to hyperswitch repo root (default: ~/hyperswitch)
#   SCIP_FILE         - Where to write/read index.scip (default: ${HYPERSWITCH_ROOT}/index.scip)
#   SKIP_SCIP         - If 1, skip rust-analyzer scip and reuse existing index.scip
#
# Env vars (query):
#   HYPERSWITCH_ROOT  - Path to hyperswitch repo root (default: ~/hyperswitch)
#   OUT_JSON          - Output path for generated JSON (default: testing_agent/input.json)
#   DEPTH             - BFS depth (default: 8)
#   BACKEND           - LLM backend: auto|anthropic|groq|gemini|ollama (default: auto)
# ============================================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

HYPERSWITCH_ROOT="${HYPERSWITCH_ROOT:-${HOME}/hyperswitch}"
SCIP_FILE="${SCIP_FILE:-${HYPERSWITCH_ROOT}/index.scip}"
OUT_JSON="${OUT_JSON:-${PROJECT_ROOT}/testing_agent/input.json}"
SKIP_SCIP="${SKIP_SCIP:-0}"
DEPTH="${DEPTH:-8}"
BACKEND="${BACKEND:-auto}"

# ── Helpers ───────────────────────────────────────────────────────────────────
sep() { echo ""; echo "==> $*"; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "ERROR: Missing required command: $1" >&2; exit 1; }
}

usage() {
  echo "Usage:"
  echo "  bash hs_indexer/setup_and_generate.sh index"
  echo "  bash hs_indexer/setup_and_generate.sh query <function_name>"
  echo "  bash hs_indexer/setup_and_generate.sh query --file <relative/path.rs> --line <n>"
  echo "  bash hs_indexer/setup_and_generate.sh query <function_name> --file <path.rs> --line <n>"
  exit 1
}

# ── Subcommand dispatch ───────────────────────────────────────────────────────
SUBCOMMAND="${1:-}"
shift || true

case "${SUBCOMMAND}" in
  index) ;;
  query) ;;
  *) usage ;;
esac

# ── Shared validation ─────────────────────────────────────────────────────────
if [[ ! -d "${HYPERSWITCH_ROOT}/crates" ]]; then
  echo "ERROR: HYPERSWITCH_ROOT does not look like a hyperswitch repo: ${HYPERSWITCH_ROOT}" >&2
  exit 1
fi

require_cmd python3

if ! python3 -c "import neo4j" >/dev/null 2>&1; then
  sep "Installing missing Python dependency: neo4j"
  python3 -m pip install --user neo4j >/dev/null
fi

# ============================================================================
# index subcommand
# ============================================================================
if [[ "${SUBCOMMAND}" == "index" ]]; then
  require_cmd rust-analyzer

  echo "============================================================"
  echo "  Hyperswitch Indexer — Index Phase"
  echo "============================================================"
  echo "  Hyperswitch root : ${HYPERSWITCH_ROOT}"
  echo "  SCIP file        : ${SCIP_FILE}"
  echo "  Skip SCIP gen    : ${SKIP_SCIP}"
  echo "============================================================"

  # Step 1: Generate index.scip
  if [[ "${SKIP_SCIP}" != "1" ]]; then
    sep "Step 1/2: Generating index.scip (rust-analyzer scip .)"
    echo "  Indexing the full Hyperswitch source tree."
    echo "  First run: ~5–15 min. Subsequent runs are faster (incremental)."
    (cd "${HYPERSWITCH_ROOT}" && rust-analyzer scip .)
    echo "  SCIP file written: ${SCIP_FILE}"
  else
    sep "Step 1/2: Skipping SCIP generation (SKIP_SCIP=1)"
    if [[ ! -f "${SCIP_FILE}" ]]; then
      echo "ERROR: index.scip not found at ${SCIP_FILE}" >&2
      echo "  Run without SKIP_SCIP=1 to generate it." >&2
      exit 1
    fi
    echo "  Reusing existing SCIP: ${SCIP_FILE}"
  fi

  # Step 2: Load into Neo4j (callgraph + trait map + guards)
  sep "Step 2/2: Loading call graph into Neo4j"
  echo "  Phase 1/3 — build_callgraph : SCIP → :Fn nodes + :CALLS edges"
  echo "  Phase 2/3 — build_trait_map : trait impl + generic type annotations"
  echo "  Phase 3/3 — annotate_guards : conditional guard annotations"
  (cd "${PROJECT_ROOT}" && python3 -m hs_indexer --src-root "${HYPERSWITCH_ROOT}" index --scip "${SCIP_FILE}")

  echo ""
  echo "============================================================"
  echo "  Indexing complete. Neo4j is ready for queries."
  echo ""
  echo "  Run the query step next (pick one form):"
  echo "    bash hs_indexer/setup_and_generate.sh query <function_name>"
  echo "    bash hs_indexer/setup_and_generate.sh query --file <crates/.../file.rs> --line <n>"
  echo "    bash hs_indexer/setup_and_generate.sh query <function_name> --file <crates/.../file.rs> --line <n>"
  echo "============================================================"
  exit 0
fi

# ============================================================================
# query subcommand
# ============================================================================
FUNCTION_NAME=""
FILE=""
LINE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --file)     FILE="$2";          shift 2 ;;
    --line)     LINE="$2";          shift 2 ;;
    --*)        echo "ERROR: Unknown argument: $1" >&2; usage ;;
    *)          FUNCTION_NAME="$1"; shift   ;;
  esac
done

if [[ -z "${FUNCTION_NAME}" && ( -z "${FILE}" || -z "${LINE}" ) ]]; then
  echo "ERROR: Provide a function name, or both --file and --line." >&2
  echo "  Examples:" >&2
  echo "    bash hs_indexer/setup_and_generate.sh query get_auth_header" >&2
  echo "    bash hs_indexer/setup_and_generate.sh query --file crates/router/src/core/payments/helpers.rs --line 5897" >&2
  exit 1
fi

echo "============================================================"
echo "  Hyperswitch Indexer — Query Phase"
echo "============================================================"
echo "  Hyperswitch root : ${HYPERSWITCH_ROOT}"
if [[ -n "${FUNCTION_NAME}" ]]; then
echo "  Function         : ${FUNCTION_NAME}"
fi
if [[ -n "${FILE}" ]]; then
echo "  File             : ${FILE}:${LINE}"
fi
echo "  BFS depth        : ${DEPTH}"
echo "  LLM backend      : ${BACKEND}"
echo "  Output JSON      : ${OUT_JSON}"
echo "============================================================"

QUERY_ARGS=()
[[ -n "${FUNCTION_NAME}" ]] && QUERY_ARGS+=("${FUNCTION_NAME}")
[[ -n "${FILE}" ]]          && QUERY_ARGS+=(--file "${FILE}")
[[ -n "${LINE}" ]]          && QUERY_ARGS+=(--line "${LINE}")

# ── Resolve LLM backend ───────────────────────────────────────────────────────
sep "Checking LLM API keys for flow enrichment"
HAS_KEY=0
RESOLVED_BACKEND="${BACKEND}"

if [[ "${BACKEND}" == "auto" ]]; then
  if   [[ -n "${JUSPAY_API_KEY:-}"    ]]; then RESOLVED_BACKEND="grid";     HAS_KEY=1; echo "  JUSPAY_API_KEY    : set → backend=grid"
  elif [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then RESOLVED_BACKEND="anthropic"; HAS_KEY=1; echo "  ANTHROPIC_API_KEY : set → backend=anthropic"
  elif [[ -n "${GROQ_API_KEY:-}"      ]]; then RESOLVED_BACKEND="groq";      HAS_KEY=1; echo "  GROQ_API_KEY      : set → backend=groq"
  elif [[ -n "${GEMINI_API_KEY:-}"    ]]; then RESOLVED_BACKEND="gemini";    HAS_KEY=1; echo "  GEMINI_API_KEY    : set → backend=gemini"
  fi
else
  HAS_KEY=1
  echo "  Backend explicitly set: ${BACKEND}"
fi

if [[ "${HAS_KEY}" == "0" ]]; then
  echo ""
  echo "  WARNING: No LLM API key found. Flow enrichment will be skipped."
  echo "  Set one of the following before running:"
  echo ""
  echo "    export JUSPAY_API_KEY=sk-...      # Juspay Grid (recommended)"
  echo "    export ANTHROPIC_API_KEY=sk-ant-..."
  echo "    export GROQ_API_KEY=gsk_..."
  echo "    export GEMINI_API_KEY=AIza..."
  echo ""
  echo "  Or pass BACKEND=ollama to use a local model (no key needed)."
  echo "  Continuing without enrichment..."
fi

RAW_JSON="${OUT_JSON%.json}_raw.json"
FILTERED_JSON="${OUT_JSON%.json}_filtered_tmp.json"

# ── Step 1: BFS only (no enrichment) ─────────────────────────────────────────
sep "Step 1/3: BFS impact analysis (no enrichment)"
(cd "${PROJECT_ROOT}" && python3 -m hs_indexer \
  --src-root "${HYPERSWITCH_ROOT}" \
  query \
  "${QUERY_ARGS[@]}" \
  --depth "${DEPTH}" \
  --no-enrich \
  --out "${RAW_JSON}")

if [[ ! -f "${RAW_JSON}" ]]; then
  echo "ERROR: hs_indexer did not produce output at ${RAW_JSON}" >&2
  exit 1
fi

# ── Step 2: False-positive filter ─────────────────────────────────────────────
sep "Step 2/3: Filtering false positives (endpoints + flows)"
if [[ -n "${JUSPAY_API_KEY:-}" ]]; then
  echo "  Using Grid API (claude-sonnet-4-6) to classify endpoints …"
  (cd "${PROJECT_ROOT}" && python3 -m hs_indexer.filter_false_positives \
    --input    "${RAW_JSON}" \
    --src-root "${HYPERSWITCH_ROOT}" \
    --out      "${FILTERED_JSON}")
  if [[ ! -f "${FILTERED_JSON}" ]]; then
    echo "  WARNING: filter step produced no output — using raw" >&2
    cp "${RAW_JSON}" "${FILTERED_JSON}"
  fi
else
  echo "  JUSPAY_API_KEY not set — skipping false-positive filter, using raw output"
  cp "${RAW_JSON}" "${FILTERED_JSON}"
fi

# ── Step 3: LLM enrichment of surviving flows only ────────────────────────────
sep "Step 3/3: LLM enrichment of surviving flows"
if [[ "${HAS_KEY}" == "1" ]]; then
  (cd "${PROJECT_ROOT}" && python3 -m hs_indexer.enrich_flows \
    --input   "${FILTERED_JSON}" \
    --backend "${RESOLVED_BACKEND}" \
    --out     "${OUT_JSON}") || true
fi

# If enrichment didn't produce output (no key, or error), use filtered as final
if [[ ! -f "${OUT_JSON}" ]]; then
  echo "  WARNING: enrichment produced no output — using filtered result as final" >&2
  cp "${FILTERED_JSON}" "${OUT_JSON}"
fi

# Clean up temp filtered file
rm -f "${FILTERED_JSON}"

echo ""
echo "============================================================"
echo "  Final output (enriched) : ${OUT_JSON}"
echo "  Raw BFS (unfiltered)    : ${RAW_JSON}"
echo ""
echo "  Run the full coverage report next:"
echo "    bash report-generater/run_flow_coverage_report.sh"
echo "============================================================"
