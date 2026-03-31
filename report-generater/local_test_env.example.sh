#!/usr/bin/env bash
# Copy to local_test_env.sh and fill in values. local_test_env.sh is gitignored.
#
#   cp report-generater/local_test_env.example.sh report-generater/local_test_env.sh
#
# Then:
#   source report-generater/local_test_env.sh
#   bash report-generater/run_flow_coverage_report.sh

export GRID_API_KEY='YOUR_GRID_API_KEY'
export GRID_MODEL='glm-latest'
export GRID_BASE_URL='https://grid.ai.juspay.net'

export HYPERSWITCH_ROOT="${HYPERSWITCH_ROOT:-$HOME/Documents/Workspace/hyperswitch}"
export CYPRESS_REPO="${CYPRESS_REPO:-$HYPERSWITCH_ROOT/cypress-tests}"
export CYPRESS_CONNECTOR='wellsfargo'
export CYPRESS_ADMINAPIKEY='test_admin'
export CYPRESS_BASEURL='http://localhost:8080'
export CYPRESS_CONNECTOR_AUTH_FILE_PATH="${CYPRESS_CONNECTOR_AUTH_FILE_PATH:-$HOME/Downloads/creds.json}"

export NEO4J_PASSWORD="${NEO4J_PASSWORD:-Hyperswitch123}"

_RG_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
export ROUTER_CONFIG="${ROUTER_CONFIG:-$_RG_ROOT/config/development.toml}"
