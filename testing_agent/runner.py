"""
runner.py — Cypress test runner for the Hyperswitch testing agent
=================================================================
Wraps `npx cypress run` as a subprocess, parses the output,
and returns a structured result the agent can act on.

Dependency chain
----------------
Cypress payment tests need a merchant account + connector already created.
There are two modes:

  MODE 1 — Setup first (--setup flag):
    Runs 00001-AccountCreate, 00002-CustomerCreate, 00003-ConnectorCreate
    before the actual test spec. Creates everything fresh.
    Requires: adminApiKey in env or cypress.env.json

  MODE 2 — Pre-configured (default, recommended for agent):
    Connector already exists. Pass profileId + connectorId.
    Faster — skips setup specs entirely.

One-time setup (do this once manually or via dashboard):
  1. Run: npx cypress run --spec "cypress/e2e/spec/Payment/00001-AccountCreate.cy.js,
                                   cypress/e2e/spec/Payment/00002-CustomerCreate.cy.js,
                                   cypress/e2e/spec/Payment/00003-ConnectorCreate.cy.js"
         --env CONNECTOR=stripe,adminApiKey=YOUR_KEY,baseUrl=http://localhost:8080
  2. Note the profileId and connectorId from the output / hyperswitch dashboard
  3. Set them in cypress.env.json or as env vars for all future runs

cypress.env.json (place in cypress-tests repo root):
  {
    "baseUrl": "http://localhost:8080",
    "adminApiKey": "admin_api_key_...",
    "connector_1": { "profileId": "pro_xxx", "connectorId": "mca_xxx" }
  }

Environment variables:
    CYPRESS_BASE_URL          Hyperswitch server URL (default: http://localhost:8080)
    CYPRESS_ADMIN_API_KEY     Admin API key (needed for --setup mode)
    CYPRESS_PROFILE_ID        Pre-configured profile ID  (MODE 2)
    CYPRESS_CONNECTOR_ID      Pre-configured connector ID (MODE 2)
    CYPRESS_BROWSER           Browser (default: chrome)
    CYPRESS_TIMEOUT_S         Timeout seconds (default: 300)

Usage:
    runner = CypressRunner(repo_root="/path/to/cypress-tests")
    result = runner.run("Stripe", "Overcapture")

CLI:
    # MODE 2 (pre-configured, fast):
    python runner.py --repo /path/to/cypress-tests --connector stripe --flow Overcapture

    # MODE 1 (run setup first):
    python runner.py --repo . --connector stripe --flow Overcapture --setup
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Spec file for each flow — from our domain knowledge map
from indexer import SPEC_FLOWS

# Build reverse map: flow → spec name
FLOW_TO_SPEC: dict[str, str] = {}
for spec_name, flows in SPEC_FLOWS.items():
    for flow in flows:
        if flow not in FLOW_TO_SPEC:   # first spec wins
            FLOW_TO_SPEC[flow] = spec_name


# ── result types ──────────────────────────────────────────────────────────────

@dataclass
class FailedTest:
    name:  str
    error: str


@dataclass
class RunResult:
    connector:     str
    flow:          str
    spec_file:     str
    command:       str

    passed:        bool
    passing:       int   = 0
    failing:       int   = 0
    total:         int   = 0
    duration_ms:   int   = 0

    passing_tests: list[str]        = field(default_factory=list)
    failed_tests:  list[FailedTest] = field(default_factory=list)

    exit_code:     int  = 0
    raw_stdout:    str  = ""
    raw_stderr:    str  = ""
    timed_out:     bool = False
    error:         str  = ""    # runner-level error (not test failure)

    def summary(self) -> str:
        if self.error:
            return f"❌ Runner error: {self.error}"
        if self.timed_out:
            return f"⏱  Timed out: {self.spec_file}"
        icon = "✅" if self.passed else "❌"
        return (
            f"{icon} {self.connector}/{self.flow} — "
            f"{self.passing}/{self.total} passing "
            f"({self.duration_ms}ms)"
        )

    def failure_summary(self) -> str:
        """
        Structured failure context to pass back to the LLM for retry.
        Contains exactly what broke and the assertion error.
        """
        if self.passed:
            return "All tests passed."
        lines = [
            f"Test run FAILED for {self.connector}/{self.flow}",
            f"Spec: {self.spec_file}",
            f"Result: {self.passing} passing, {self.failing} failing",
            "",
        ]
        for ft in self.failed_tests:
            lines += [
                f"FAILED: {ft.name}",
                f"  Error: {ft.error}",
                "",
            ]
        lines += [
            "Raw output (last 50 lines):",
            *self.raw_stdout.splitlines()[-50:],
        ]
        return "\n".join(lines)


# ── output parser ──────────────────────────────────────────────────────────────

def _parse_duration(s: str) -> int:
    """Convert '8s', '1m 23s', '1234ms' to milliseconds."""
    s = s.strip()
    if 'm' in s:
        mm = re.match(r'(\d+)m\s*(\d+)s', s)
        if mm:
            return (int(mm.group(1)) * 60 + int(mm.group(2))) * 1000
    if 'ms' in s:
        mm = re.match(r'(\d+)ms', s)
        return int(mm.group(1)) if mm else 0
    mm = re.match(r'([\d.]+)s', s)
    return int(float(mm.group(1)) * 1000) if mm else 0


def _parse_failed_tests(stdout: str) -> list[FailedTest]:
    """Parse numbered failure blocks from cypress terminal output."""
    failed = []
    # Everything after "N failing"
    m = re.search(r'\d+\s+failing\s*\n(.*)', stdout, re.DOTALL)
    if not m:
        return []

    section = m.group(1)
    # Split on block headers like "  1) " "  2) "
    blocks = re.split(r'\n\s{2,}\d+\)', '\n' + section)

    for block in blocks[1:]:
        lines = [l for l in block.splitlines() if l.strip()]
        if not lines:
            continue

        test_name   = ""
        error_lines = []
        in_error    = False

        for line in lines:
            s = line.strip()
            if not in_error and s.endswith(':') and not s.startswith('at '):
                test_name = s.rstrip(':')
                in_error  = True
            elif in_error:
                if s.startswith('at ') and '(' in s:
                    break   # stack trace — stop
                if s and not s.startswith('+ expected') and not s.startswith('- actual'):
                    error_lines.append(s)

        if test_name:
            failed.append(FailedTest(
                name  = test_name,
                error = " | ".join(error_lines[:3]),
            ))

    return failed


def parse_cypress_output(stdout: str, returncode: int) -> dict:
    """Parse cypress terminal output into a structured dict."""
    passing_m   = re.search(r'(\d+)\s+passing', stdout)
    failing_m   = re.search(r'(\d+)\s+failing', stdout)
    dur_m       = re.search(r'passing \((.+?)\)', stdout)
    passing_tests = re.findall(r'[✓✔]\s+(.+?)\s+\(\d+(?:ms|s)\)', stdout)

    passing     = int(passing_m.group(1)) if passing_m else 0
    failing     = int(failing_m.group(1)) if failing_m else 0
    duration_ms = _parse_duration(dur_m.group(1)) if dur_m else 0
    failed      = _parse_failed_tests(stdout)

    return {
        "passed":        returncode == 0 and failing == 0,
        "passing":       passing,
        "failing":       failing,
        "total":         passing + failing,
        "duration_ms":   duration_ms,
        "passing_tests": passing_tests,
        "failed_tests":  failed,
    }


# ── runner ────────────────────────────────────────────────────────────────────

class CypressRunner:
    def __init__(
        self,
        repo_root:    str,
        base_url:     Optional[str] = None,
        admin_api_key:Optional[str] = None,
        profile_id:   Optional[str] = None,
        connector_id: Optional[str] = None,
        auth_file:    Optional[str] = None,
        browser:      str           = "chrome",
        timeout_s:    int           = 300,
        headed:       bool          = False,
    ):
        self.root          = Path(repo_root)
        self.base_url      = (base_url
                              or os.environ.get("CYPRESS_BASE_URL")
                              or os.environ.get("CYPRESS_BASEURL", "http://localhost:8080"))
        self.admin_api_key = (admin_api_key
                              or os.environ.get("CYPRESS_ADMIN_API_KEY")
                              or os.environ.get("CYPRESS_ADMINAPIKEY", ""))
        self.profile_id    = (profile_id
                              or os.environ.get("CYPRESS_PROFILE_ID", ""))
        self.connector_id  = (connector_id
                              or os.environ.get("CYPRESS_CONNECTOR_ID", ""))
        self.auth_file     = (auth_file
                              or os.environ.get("CYPRESS_CONNECTOR_AUTH_FILE_PATH", ""))
        self.browser       = browser or os.environ.get("CYPRESS_BROWSER", "chrome")
        self.timeout_s     = timeout_s or int(os.environ.get("CYPRESS_TIMEOUT_S", "300"))
        self.headed        = headed

    def _spec_for_flow(self, flow: str) -> Optional[str]:
        """Return relative spec path for a flow."""
        spec_name = FLOW_TO_SPEC.get(flow)
        if not spec_name:
            return None
        return f"cypress/e2e/spec/Payment/{spec_name}.cy.js"

    def _build_command(
        self,
        connector: str,
        spec_file: str,
        extra_env: Optional[dict] = None,
    ) -> list[str]:
        """Build the npx cypress run command.

        NOTE: baseUrl is a cypress CONFIG option, not an env var.
        It must be passed via --config, not --env.
        Cypress also auto-reads CYPRESS_BASE_URL env var if set.
        """
        # --env vars: runtime values the tests read via Cypress.env()
        env_vars = {
            "CONNECTOR": connector.lower(),
        }
        if self.profile_id:
            env_vars["connector_1_profile_id"]   = self.profile_id
        if self.connector_id:
            env_vars["connector_1_connector_id"] = self.connector_id
        if self.admin_api_key:
            env_vars["adminApiKey"]              = self.admin_api_key
        # Connector auth file — how hyperswitch tests load API keys
        if self.auth_file:
            env_vars["CONNECTOR_AUTH_FILE_PATH"] = self.auth_file
        if extra_env:
            env_vars.update(extra_env)

        env_str = ",".join(f"{k}={v}" for k, v in env_vars.items())

        cmd = [
            "npx", "cypress", "run",
            "--spec",    spec_file,
            "--env",     env_str,
            "--browser", self.browser,
        ]

        # baseUrl is a config option — use --config, not --env
        if self.base_url:
            cmd += ["--config", "baseUrl=" + self.base_url]

        if not self.headed:
            cmd.append("--headless")

        return cmd

    # Flows that need a prior payment in globalState (in addition to account/customer/connector).
    # These flows operate on an existing payment, so a payment must be created and confirmed
    # before the test spec runs.
    #
    # MANUAL_CAPTURE_FLOWS: need a payment in `requires_capture` state
    #   → prepend 00006-NoThreeDSManualCapture (create + confirm, not auto-captured)
    # AUTO_CAPTURE_FLOWS: need a captured payment (e.g. for refund)
    #   → prepend 00004-NoThreeDSAutoCapture (create + confirm, auto-captured)
    MANUAL_CAPTURE_FLOWS: set[str] = {
        "IncrementalAuth",
        "VoidAfterConfirm",
        "Capture",
        "SyncPayment",
    }
    AUTO_CAPTURE_FLOWS: set[str] = {
        "Refund",
        "SyncRefund",
    }

    def _setup_specs(self, flow_name: str = "") -> list[str]:
        """Return the setup spec files that must run before payment tests.

        For flows that operate on an existing payment (void, capture,
        incremental auth, refund) a payment creation spec is prepended so
        that globalState carries a valid paymentId and any saved-card body
        when the target spec starts.
        """
        base = [
            "cypress/e2e/spec/Payment/00001-AccountCreate.cy.js",
            "cypress/e2e/spec/Payment/00002-CustomerCreate.cy.js",
            "cypress/e2e/spec/Payment/00003-ConnectorCreate.cy.js",
        ]
        if flow_name in self.MANUAL_CAPTURE_FLOWS:
            base.append("cypress/e2e/spec/Payment/00006-NoThreeDSManualCapture.cy.js")
        elif flow_name in self.AUTO_CAPTURE_FLOWS:
            base.append("cypress/e2e/spec/Payment/00004-NoThreeDSAutoCapture.cy.js")
        return base

    def run(
        self,
        connector:  str,
        flow:       str,
        spec_file:  Optional[str]  = None,
        extra_env:  Optional[dict] = None,
        setup:      bool           = False,
    ) -> RunResult:
        """
        Run cypress tests for (connector, flow).

        Args:
            connector:  Connector name e.g. "Stripe" or "stripe"
            flow:       Flow name e.g. "Overcapture"
            spec_file:  Override spec file (optional)
            extra_env:  Extra cypress env vars
            setup:      If True, run 00001-00003 setup specs first (MODE 1).
                        Flow-specific prerequisite specs (e.g. a payment creation
                        spec) are automatically added when `flow` matches a known
                        pattern in MANUAL_CAPTURE_FLOWS / AUTO_CAPTURE_FLOWS.
        """
        # Resolve spec file
        if not spec_file:
            spec_file = self._spec_for_flow(flow)
        if not spec_file:
            return RunResult(
                connector=connector, flow=flow,
                spec_file="", command="",
                passed=False,
                error=f"No spec file found for flow '{flow}'. "
                      f"Add it to SPEC_FLOWS in indexer.py.",
            )

        # Build spec list — setup specs must be in the SAME cypress run
        # as the test spec so globalState persists across specs
        if setup:
            setup_specs = self._setup_specs(flow_name=flow)
            all_specs = ",".join(setup_specs + [spec_file])
            print("  Running setup + test in one cypress process (globalState must persist):")
            print(f"    setup: {[s.split('/')[-1] for s in setup_specs]}")
            print(f"    test:  {spec_file.split('/')[-1]}")
        else:
            all_specs = spec_file

        cmd = self._build_command(connector, all_specs, extra_env)
        cmd_str = " ".join(cmd)

        print(f"  Running: {cmd_str}")

        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd,
                cwd        = str(self.root),
                capture_output = True,
                text       = True,
                timeout    = self.timeout_s,
            )
        except subprocess.TimeoutExpired as e:
            stdout = e.stdout or ""
            return RunResult(
                connector=connector, flow=flow,
                spec_file=spec_file, command=cmd_str,
                passed=False, timed_out=True,
                duration_ms=int((time.time() - t0) * 1000),
                raw_stdout=stdout,
                error=f"Cypress timed out after {self.timeout_s}s",
            )
        except FileNotFoundError:
            return RunResult(
                connector=connector, flow=flow,
                spec_file=spec_file, command=cmd_str,
                passed=False,
                error="'npx' not found. Is Node.js installed? Run: npm install",
            )

        duration_ms = int((time.time() - t0) * 1000)
        parsed      = parse_cypress_output(proc.stdout, proc.returncode)

        return RunResult(
            connector     = connector,
            flow          = flow,
            spec_file     = spec_file,
            command       = cmd_str,
            passed        = parsed["passed"],
            passing       = parsed["passing"],
            failing       = parsed["failing"],
            total         = parsed["total"],
            duration_ms   = duration_ms,
            passing_tests = parsed["passing_tests"],
            failed_tests  = parsed["failed_tests"],
            exit_code     = proc.returncode,
            raw_stdout    = proc.stdout,
            raw_stderr    = proc.stderr,
        )

    def run_spec(self, spec_file: str, connector: str) -> RunResult:
        """Run an entire spec file regardless of flow."""
        return self.run(connector, flow="", spec_file=spec_file)


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Run Hyperswitch cypress tests")
    ap.add_argument("--repo",       required=True,
                    help="Path to cypress-tests root")
    ap.add_argument("--connector",  required=True,
                    help="Connector name e.g. stripe, adyen")
    ap.add_argument("--flow",       required=True,
                    help="Flow name e.g. Overcapture, Capture")
    ap.add_argument("--spec",       default=None,
                    help="Override spec file path")
    ap.add_argument("--base-url",    default=None,
                    help="Hyperswitch URL (default: CYPRESS_BASE_URL or http://localhost:8080)")
    ap.add_argument("--admin-key",   default=None,
                    help="Admin API key (needed for --setup mode)")
    ap.add_argument("--profile-id",  default=None,
                    help="Pre-configured profileId (MODE 2, skips setup)")
    ap.add_argument("--connector-id",default=None,
                    help="Pre-configured connectorId (MODE 2, skips setup)")
    ap.add_argument("--auth-file",   default=None,
                    help="Path to connector credentials JSON file "
                         "(default: CYPRESS_CONNECTOR_AUTH_FILE_PATH env var)")
    ap.add_argument("--setup",       action="store_true",
                    help="Run 00001-00003 setup specs before the test (MODE 1)")
    ap.add_argument("--browser",     default="chrome")
    ap.add_argument("--headed",      action="store_true")
    ap.add_argument("--timeout",     type=int, default=300)
    ap.add_argument("--show-output", action="store_true",
                    help="Print full cypress stdout")
    args = ap.parse_args()

    runner = CypressRunner(
        repo_root     = args.repo,
        base_url      = args.base_url,
        admin_api_key = args.admin_key,
        profile_id    = args.profile_id,
        connector_id  = args.connector_id,
        auth_file     = args.auth_file,
        browser       = args.browser,
        timeout_s     = args.timeout,
        headed        = args.headed,
    )

    result = runner.run(args.connector, args.flow,
                        spec_file=args.spec, setup=args.setup)

    print(result.summary())
    print(f"  Spec    : {result.spec_file}")
    print(f"  Command : {result.command}")

    if result.failed_tests:
        print("\nFailed tests:")
        for ft in result.failed_tests:
            print(f"  ✗ {ft.name}")
            print(f"    {ft.error}")

    if args.show_output:
        print("\n" + "─"*60)
        print(result.raw_stdout)

    if not result.passed and not result.error:
        print("\nFailure context for LLM retry:")
        print("─"*60)
        print(result.failure_summary())