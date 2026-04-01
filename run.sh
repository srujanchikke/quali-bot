#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Load local environment config
source "${SCRIPT_DIR}/report-generater/local_test_env.sh"

# Step 1: Index the cypress-tests repo
echo "==> Indexing cypress-tests repo..."
cd "${SCRIPT_DIR}/testing_agent"
python3 indexer.py --repo "${CYPRESS_REPO}" --full

# Step 2: Run full flow coverage report
echo "==> Running flow coverage report..."
cd "${SCRIPT_DIR}"
bash report-generater/run_flow_coverage_report.sh
