#!/usr/bin/env python3
"""
parse_cypress_output.py — Extract request IDs and test results from cypress output
======================================================================================

Parses the output from run_flow_pipeline.py to extract:
  - Request IDs (x-request-id headers)
  - HTTP status codes
  - Test pass/fail status
  - Response bodies

Usage:
    python parse_cypress_output.py --log flow_pipeline.log --out parsed.json
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


@dataclass
class RequestInfo:
    """Information about a single HTTP request made during testing."""
    request_id: str
    method: str = ""
    url: str = ""
    status_code: int | None = None
    response_body: dict[str, Any] | None = None
    timestamp: str = ""


@dataclass
class ParsedCypressOutput:
    """Parsed result from cypress test output."""
    request_ids: list[str] = field(default_factory=list)
    requests: list[RequestInfo] = field(default_factory=list)
    test_passed: bool = False
    passing_count: int = 0
    failing_count: int = 0
    total_tests: int = 0
    duration_ms: int = 0
    failed_test_names: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    raw_snippets: list[str] = field(default_factory=list)


def parse_request_ids(text: str) -> list[str]:
    """Extract x-request-id values from output.
    
    Looks for patterns like:
      - x-request-id: abc123
      - "x-request-id": "abc123"
      - request_id: abc123
      - X-Request-Id: abc123
    """
    ids = set()
    
    # Pattern 1: x-request-id: value (header style)
    for m in re.finditer(r'x-request-id\s*[:=]\s*([a-zA-Z0-9_-]+)', text, re.IGNORECASE):
        ids.add(m.group(1))
    
    # Pattern 2: "x-request-id": "value" (JSON style)
    for m in re.finditer(r'"x-request-id"\s*:\s*"([^"]+)"', text, re.IGNORECASE):
        ids.add(m.group(1))
    
    # Pattern 3: request_id in response body
    for m in re.finditer(r'"request_id"\s*:\s*"([^"]+)"', text):
        val = m.group(1)
        if val and val != "null":
            ids.add(val)
    
    return sorted(ids)


def parse_http_status(text: str) -> dict[str, int]:
    """Extract HTTP status codes with context."""
    statuses = {}
    for m in re.finditer(r'(GET|POST|PUT|DELETE|PATCH)\s+(\S+)\s+.*?(\d{3})', text):
        method, url, status = m.groups()
        key = f"{method} {url}"
        statuses[key] = int(status)
    return statuses


def parse_test_summary(text: str) -> tuple[int, int, int, int]:
    """Parse cypress test summary: passing, failing, pending, duration_ms."""
    passing_m = re.search(r'(\d+)\s+passing', text)
    failing_m = re.search(r'(\d+)\s+failing', text)
    pending_m = re.search(r'(\d+)\s+pending', text)
    duration_m = re.search(r'passing\s*\((\d+)(?:ms|s)\)', text)
    
    passing = int(passing_m.group(1)) if passing_m else 0
    failing = int(failing_m.group(1)) if failing_m else 0
    pending = int(pending_m.group(1)) if pending_m else 0
    duration = int(duration_m.group(1)) if duration_m else 0
    
    # Handle duration in seconds
    if duration_m and 's' in duration_m.group(0) and 'ms' not in duration_m.group(0):
        duration *= 1000
    
    return passing, failing, pending, duration


def parse_failed_tests(text: str) -> list[str]:
    """Extract names of failed tests."""
    failed = []
    
    # Look for failing test blocks
    # Cypress format: "  1) test name"
    in_failing = False
    for line in text.splitlines():
        if re.match(r'\s*\d+\s+failing', line):
            in_failing = True
            continue
        if in_failing and re.match(r'\s+\d+\)', line):
            # Extract test name after the number
            m = re.match(r'\s+\d+\)\s*(.+)', line)
            if m:
                failed.append(m.group(1).strip())
    
    return failed


def parse_errors(text: str) -> list[str]:
    """Extract error messages from cypress output."""
    errors = []
    
    # Look for assertion errors
    for m in re.finditer(r'AssertionError[:\s]+([^\n]+)', text):
        errors.append(m.group(1).strip())
    
    # Look for generic error lines
    for m in re.finditer(r'ERROR[:\s]+([^\n]+)', text, re.IGNORECASE):
        err = m.group(1).strip()
        if err and err not in errors:
            errors.append(err)
    
    # Look for Cypress error blocks
    for m in re.finditer(r'CypressError:[^\n]*\n([^\n]+)', text):
        errors.append(m.group(1).strip())
    
    return errors[:10]  # Limit to first 10


def parse_response_bodies(text: str) -> list[dict[str, Any]]:
    """Extract JSON response bodies from output."""
    bodies = []
    
    # Look for JSON objects in the output
    # Pattern: lines that look like JSON objects
    in_json = False
    json_lines = []
    brace_depth = 0
    
    for line in text.splitlines():
        if '{' in line:
            if not in_json:
                in_json = True
                json_lines = []
                brace_depth = 0
            brace_depth += line.count('{') - line.count('}')
            json_lines.append(line)
        elif in_json:
            brace_depth += line.count('{') - line.count('}')
            json_lines.append(line)
            if brace_depth <= 0:
                # Try to parse the JSON
                try:
                    json_text = '\n'.join(json_lines)
                    obj = json.loads(json_text)
                    if isinstance(obj, dict) and obj:
                        bodies.append(obj)
                except json.JSONDecodeError:
                    pass
                in_json = False
                json_lines = []
    
    return bodies


def parse_log_file(path: Path) -> ParsedCypressOutput:
    """Parse a cypress log file and extract structured data."""
    text = path.read_text(encoding="utf-8", errors="replace")
    
    request_ids = parse_request_ids(text)
    passing, failing, pending, duration = parse_test_summary(text)
    failed_tests = parse_failed_tests(text)
    errors = parse_errors(text)
    response_bodies = parse_response_bodies(text)
    
    # Build request info list
    requests = []
    for rid in request_ids:
        req = RequestInfo(request_id=rid)
        # Try to find associated status code
        for body in response_bodies:
            if isinstance(body, dict):
                req.response_body = body
                break
        requests.append(req)
    
    return ParsedCypressOutput(
        request_ids=request_ids,
        requests=[asdict(r) for r in requests],
        test_passed=(failing == 0 and passing > 0),
        passing_count=passing,
        failing_count=failing,
        total_tests=passing + failing + pending,
        duration_ms=duration,
        failed_test_names=failed_tests,
        errors=errors,
        raw_snippets=[],
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Parse cypress output for request IDs and test results.")
    ap.add_argument("--log", type=Path, required=True, help="Path to cypress log file")
    ap.add_argument("--out", type=Path, required=True, help="Output JSON path")
    args = ap.parse_args()

    if not args.log.is_file():
        print(f"ERROR: Log file not found: {args.log}", flush=True)
        return 1

    result = parse_log_file(args.log)
    
    output = {
        "source_log": str(args.log),
        "request_ids": result.request_ids,
        "requests": result.requests,
        "test_passed": result.test_passed,
        "passing_count": result.passing_count,
        "failing_count": result.failing_count,
        "total_tests": result.total_tests,
        "duration_ms": result.duration_ms,
        "failed_test_names": result.failed_test_names,
        "errors": result.errors,
    }
    
    args.out.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(f"Parsed {len(result.request_ids)} request IDs, {result.passing_count} passing tests", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())