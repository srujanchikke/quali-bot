#!/usr/bin/env python3
"""
generate_final_report.py — Build final RCA report JSON
======================================================

Generates a Root Cause Analysis report by correlating:
  - Router logs with request IDs
  - Coverage gap analysis
  - Cypress test results
  - Flow metadata from the flow JSON

Usage:
    python generate_final_report.py \
        --request-id "abc123" \
        --router-log router_run.log \
        --coverage-report coverage_run_report.json \
        --flow-json adyen_get_auth_header_output.json \
        --cypress-parsed cypress_parsed.json \
        --out final_report.json
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _read_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def collect_request_lines(log_text: str, request_id: str) -> list[dict[str, Any]]:
    """Collect log lines containing the request ID."""
    out = []
    for i, line in enumerate(log_text.splitlines(), start=1):
        if request_id in line:
            out.append({"line_no": i, "line": line})
    return out


def parse_status_and_error(lines: list[dict[str, Any]]) -> tuple[int | None, dict[str, Any] | None]:
    """Parse HTTP status code and error info from log lines."""
    status = None
    err_msg = None
    err_code = None
    for rec in lines:
        line = rec["line"]
        if "status_code" in line:
            m = re.search(r"status_code[^0-9]*(\d{3})", line)
            if m:
                status = int(m.group(1))
        if "Missing required param: Authorization" in line:
            err_msg = "Missing required param: Authorization"
        if "IR_04" in line:
            err_code = "IR_04"
    if err_msg or err_code:
        return status, {"type": "invalid_request", "code": err_code, "message": err_msg}
    return status, None


def parse_cypress_errors(cypress_parsed: dict[str, Any]) -> dict[str, Any] | None:
    """Extract error info from parsed cypress output."""
    if cypress_parsed.get("test_passed"):
        return None
    
    errors = cypress_parsed.get("errors", [])
    failed_tests = cypress_parsed.get("failed_test_names", [])
    
    if not errors and not failed_tests:
        return None
    
    return {
        "type": "cypress_test_failure",
        "failed_tests": failed_tests[:5],
        "errors": errors[:3],
        "passing_count": cypress_parsed.get("passing_count", 0),
        "failing_count": cypress_parsed.get("failing_count", 0),
    }


def extract_flow_info(flow_json: dict[str, Any]) -> dict[str, Any]:
    """Extract API call info from flow JSON document."""
    # Get endpoint info
    endpoints = flow_json.get("endpoints", [])
    first_endpoint = endpoints[0] if endpoints else {}
    
    # Get flow info
    flows = flow_json.get("flows", [])
    first_flow = flows[0] if flows else {}
    
    # Get changed function
    changed_func = flow_json.get("changed_function", "")
    
    # Get target leaf from chain
    target_leaf = None
    for step in first_flow.get("chain", []):
        if step.get("role") == "target":
            target_leaf = step.get("function", "")
            break
    
    return {
        "method": first_endpoint.get("method", "UNKNOWN"),
        "endpoint": first_endpoint.get("path", "UNKNOWN"),
        "handler": first_endpoint.get("handler", ""),
        "changed_function": changed_func,
        "target_leaf": target_leaf,
        "description": first_flow.get("description", ""),
        "flow_id": first_flow.get("flow_id", "?"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Build final RCA report JSON.")
    ap.add_argument("--request-id", required=True, help="Request ID to correlate")
    ap.add_argument("--router-log", type=Path, required=True, help="Router log file")
    ap.add_argument("--coverage-report", type=Path, required=True, help="Coverage diff JSON")
    ap.add_argument("--flow-json", type=Path, default=None, help="Flow JSON (e.g., adyen_get_auth_header_output.json)")
    ap.add_argument("--cypress-parsed", type=Path, default=None, help="Parsed cypress output JSON")
    ap.add_argument("--out", type=Path, required=True, help="Output report JSON path")
    args = ap.parse_args()

    # Load coverage report
    cov = _read_json(args.coverage_report)
    
    # Load router log
    log_text = _read_text(args.router_log)
    req_lines = collect_request_lines(log_text, args.request_id)
    status_code, log_err_obj = parse_status_and_error(req_lines)
    
    # Load flow JSON for API metadata
    flow_json = _read_json(args.flow_json) if args.flow_json else {}
    flow_info = extract_flow_info(flow_json)
    
    # Load cypress parsed output
    cypress_parsed = _read_json(args.cypress_parsed) if args.cypress_parsed else {}
    cypress_err_obj = parse_cypress_errors(cypress_parsed)
    
    # Extract coverage gap info
    d = cov.get("d", {})
    gap = (d.get("gaps") or [{}])[0]
    leaf = d.get("leaf", {})
    zero_lines = gap.get("zero_hit_lines") or []
    lcov_hit_lines = gap.get("lcov_hit_lines")

    # Determine RCA status and reason
    # Priority: log error > cypress error > coverage status
    err_obj = log_err_obj or cypress_err_obj
    
    if log_err_obj:
        rca_status = "failure_cause_found"
        rca_reason = f"The request was rejected before leaf execution: {log_err_obj.get('message')} ({log_err_obj.get('code')})."
    elif cypress_err_obj:
        rca_status = "test_failure"
        failed = cypress_err_obj.get("failing_count", 0)
        rca_reason = f"Cypress test failed: {failed} test(s) failed. Check cypress output for details."
    elif gap.get("status") == "all_probed_lines_hit":
        rca_status = "not_applicable_for_this_run"
        rca_reason = "Request succeeded and all probed leaf lines were hit."
    else:
        rca_status = "investigate_more"
        rca_reason = "Coverage gap exists but no explicit error was found for this request id in matched log lines."

    # Build final report
    final_report: dict[str, Any] = {
        "request_id": args.request_id,
        "flow_info": {
            "flow_id": flow_info.get("flow_id", "?"),
            "description": flow_info.get("description", ""),
            "changed_function": flow_info.get("changed_function", ""),
            "target_leaf": flow_info.get("target_leaf", ""),
        },
        "api_call": {
            "method": flow_info.get("method", "UNKNOWN"),
            "endpoint": flow_info.get("endpoint", "UNKNOWN"),
            "handler": flow_info.get("handler", ""),
            "http_status_code": status_code,
            "error": err_obj,
        },
        "test_results": {
            "cypress_passed": cypress_parsed.get("test_passed", None),
            "passing_count": cypress_parsed.get("passing_count", 0),
            "failing_count": cypress_parsed.get("failing_count", 0),
            "total_tests": cypress_parsed.get("total_tests", 0),
            "duration_ms": cypress_parsed.get("duration_ms", 0),
        },
        "router_log_correlation": {
            "router_log_path": str(args.router_log),
            "matches_in_log": len(req_lines),
            "sample_lines": req_lines[:8],
        },
        "coverage_diff": {
            "leaf": leaf,
            "gap_status": gap.get("status"),
            "body_span": gap.get("body_span"),
            "lines_in_span": gap.get("lines_in_span"),
            "lcov_hit_lines": lcov_hit_lines,
            "zero_hit_lines": zero_lines,
            "line_coverage_ratio": gap.get("line_coverage_ratio"),
        },
        "root_cause_analysis": {
            "status": rca_status,
            "reason": rca_reason,
            "why_coverage_is_zero": (
                "Request did not enter leaf due to pre-check/auth failure."
                if zero_lines and log_err_obj
                else None
            ),
        },
    }

    args.out.write_text(json.dumps(final_report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())