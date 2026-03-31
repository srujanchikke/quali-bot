"""
flow_context.py — LLM context builder for flow_id JSON input
=============================================================
Given a FlowCoverageResult, builds a prompt that tells the LLM:
  1. What endpoints to test (from trigger_payloads)
  2. What setup is needed (from setup_payloads + prerequisites)
  3. What the success + failure cases look like (exact request bodies)
  4. What style to follow (from a similar existing spec)
  5. Where to put the new file (spec_to_create)

Two scenarios:

  SCENARIO A — MISSING: no spec covers these endpoints at all
    → LLM generates a brand new spec file from scratch

  SCENARIO B — PARTIAL/NEEDS_LLM_CHECK: candidate spec exists
    → LLM reads it and either confirms coverage or adds missing test cases

Output format:
  <file path="cypress/e2e/spec/Payment/00041-PaymentsUpdate.cy.js">
  ...complete spec file...
  </file>
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from flow_query import FlowCoverageResult, CoverageStatus, FlowQueryEngine


# ── compatibility aliases (replaces context.py + query.py) ───────────────────
# codegen.py imports ContextBundle from here instead of context.py

from enum import Enum
from dataclasses import dataclass as _dataclass

class Status(str, Enum):
    """Replaces query.Status — kept for codegen.py compatibility."""
    EXISTS           = "EXISTS"
    MISSING_CONFIG   = "MISSING_CONFIG"
    NOT_IN_ALLOWLIST = "NOT_IN_ALLOWLIST"
    MISSING_TEST     = "MISSING_TEST"

@_dataclass
class ContextBundle:
    """
    Replaces context.ContextBundle — the shape codegen.py expects.
    FlowContextBundle (from build_flow_context) is converted to this before
    passing to CodeGen.apply().
    """
    connector:     str
    flow:          str
    status:        Status
    prompt:        str
    system_prompt: str
    files_to_edit: list
    patch_meta:    dict = None

    def __post_init__(self):
        if self.patch_meta is None:
            self.patch_meta = {}


# ── endpoint group detection ─────────────────────────────────────────────────

ENDPOINT_GROUPS = {
    "/payment_methods/auth": "pm_auth",
    "/payment_methods":      "payment_method",
    "/payments":             "payment",
    "/payouts":              "payout",
    "/refunds":              "refund",
    "/routing":              "routing",
    "/customers":            "customer",
}

SPEC_DIR_MAP = {
    "payment":        "cypress/e2e/spec/Payment",
    "pm_auth":        "cypress/e2e/spec/Payment",
    "payment_method": "cypress/e2e/spec/Payment",
    "payout":         "cypress/e2e/spec/Payout",
    "refund":         "cypress/e2e/spec/Payment",
    "routing":        "cypress/e2e/spec/Routing",
    "customer":       "cypress/e2e/spec/Payment",
}

def detect_endpoint_group(trigger_paths: list) -> str:
    for path in trigger_paths:
        for prefix, group in sorted(ENDPOINT_GROUPS.items(), key=lambda x: -len(x[0])):
            if path.startswith(prefix):
                return group
    return "payment"


SYSTEM_PROMPT = (
    "You are a senior engineer writing Cypress tests for the Hyperswitch payment platform. "
    "You write precise, minimal JavaScript test specs. "
    "Follow the exact style of the reference spec provided. "
    "Output ONLY the <file path=...> tag — no explanation outside it."
)

# ── context bundle ────────────────────────────────────────────────────────────

@dataclass
class FlowContextBundle:
    flow_id:        int
    description:    str
    status:         str
    prompt:         str
    system_prompt:  str
    spec_to_create: Optional[str]       # path for the new file
    files_to_edit:  list[str] = field(default_factory=list)
    patch_meta:     dict      = field(default_factory=dict)


# ── helpers ───────────────────────────────────────────────────────────────────

def _format_payload(body: dict, indent: int = 4) -> str:
    return json.dumps(body, indent=indent)


def _format_trigger_cases(trigger_payloads: dict) -> str:
    """Format trigger_payloads into a readable test case description."""
    lines = []
    for handler, cases in trigger_payloads.items():
        lines.append(f"### Handler: {handler}")
        for case_name, case in cases.items():
            ep   = case.get("endpoint", "")
            body = case.get("body", {})
            exp  = case.get("expected_result", "")
            lines += [
                f"  {case_name}:",
                f"    endpoint: {ep}",
                f"    body: {json.dumps(body)}",
                f"    expected: {exp}",
            ]
        lines.append("")
    return "\n".join(lines)


def _format_setup(setup_payloads: list) -> str:
    lines = []
    for sp in setup_payloads:
        lines += [
            f"  name: {sp.get('name')}",
            f"  endpoint: {sp.get('endpoint')}",
            f"  body: {json.dumps(sp.get('body', {}))}",
            f"  purpose: {sp.get('purpose', '')}",
            "",
        ]
    return "\n".join(lines)


def _format_prerequisites(prerequisites: list) -> str:
    lines = []
    for p in prerequisites:
        kind  = p.get("kind", "")
        field = p.get("field", "")
        req   = p.get("required_value", p.get("required", ""))
        reason = p.get("reason", "")
        lines.append("  [" + kind + "] " + field + " = " + str(req) + "  — " + reason)
    return "\n".join(lines)


def _format_existing_it_blocks(it_blocks: dict) -> str:
    """
    Format the existing it() blocks found in Neo4j for the trigger endpoints.
    This is shown to the LLM so it mirrors the same naming pattern.
    """
    if not it_blocks:
        return "No existing it() blocks found for these endpoints."

    lines = ["Existing it() blocks for these endpoints (mirror this naming pattern):"]
    for spec_file, data in sorted(it_blocks.items()):
        spec_name = spec_file.split("/")[-1].replace(".cy.js", "")
        lines.append("")
        lines.append("  Spec: " + spec_name)
        lines.append("  Endpoints covered: " + str(data.get("endpoints_covered", [])))
        blocks = data.get("it_blocks", [])
        if blocks:
            lines.append("  it() blocks (" + str(len(blocks)) + " total):")
            for b in blocks[:15]:    # cap at 15 to avoid bloat
                skip = " [skipped]" if b.get("skipped") else ""
                lines.append("    - " + b["name"] + skip)
            if len(blocks) > 15:
                lines.append("    ... +" + str(len(blocks)-15) + " more")

    lines += [
        "",
        "IMPORTANT: Use the same it() naming convention as above.",
        "  - Use kebab-case names like 'create-payment-call-test', 'confirm-call-test'",
        "  - Add scenario-specific suffix for new cases:",
        "    e.g. 'confirm-call-test-same-customer' and 'confirm-call-test-different-customer'",
        "  - Do NOT invent completely new names if the pattern already exists",
    ]
    return "\n".join(lines)


# ── main builder ──────────────────────────────────────────────────────────────

def _get_style_reference(
    result: FlowCoverageResult, root: Path, q: Optional[FlowQueryEngine],
    repo_root: str, endpoint_group: str = "payment",
) -> tuple:
    """Get the best style reference spec content + path."""
    from flow_query import FlowType

    # pm_auth has no existing spec — LLM must use raw cy.request() pattern
    if endpoint_group == "pm_auth":
        return "", "none (no existing pm_auth spec — use raw cy.request() pattern)"

    # Strategy-specific reference choices for payment flows
    hint_map = {
        FlowType.CORE_ONLY:           "cypress/e2e/spec/Payment/00022-Variations.cy.js",
        FlowType.CONNECTOR_ONLY:      "cypress/e2e/spec/Payment/00006-NoThreeDSManualCapture.cy.js",
        FlowType.CORE_THEN_CONNECTOR: "cypress/e2e/spec/Payment/00006-NoThreeDSManualCapture.cy.js",
    }

    if result.core_spec_hint:
        hint = f"cypress/e2e/spec/Payment/{result.core_spec_hint}.cy.js"
        p = root / hint
        if p.exists():
            return p.read_text(encoding="utf-8", errors="replace"), hint

    if result.candidates:
        p = root / result.candidates[0].spec_file
        if p.exists():
            return p.read_text(encoding="utf-8", errors="replace"), result.candidates[0].spec_file

    default = hint_map.get(result.flow_type, "cypress/e2e/spec/Payment/00022-Variations.cy.js")
    p = root / default
    if p.exists():
        return p.read_text(encoding="utf-8", errors="replace"), default

    if q:
        similar = q.get_similar_spec(result.trigger_paths, repo_root)
        if similar:
            return similar, "(similar spec from Neo4j)"

    return "", ""


def _strategy_instructions(result: FlowCoverageResult, endpoint_group: str = "payment") -> str:
    """Return codegen instructions specific to flow type AND endpoint group."""
    from flow_query import FlowType

    # pm_auth endpoints need completely different instructions
    if endpoint_group == "pm_auth":
        return """## Codegen strategy: PM_AUTH
This flow tests /payment_methods/auth/* endpoints — bank account credential linking.
These are NOT /payments endpoints. Do NOT use createPaymentIntentTest, confirmCallTest,
captureCallTest, or any card payment cy.task() wrappers.

What to generate:
  - Use cy.request() directly — no cy.task() wrappers exist for pm_auth endpoints
  - baseUrl = Cypress.env("baseUrl") or globalState.get("baseUrl")
  - Single it() block that calls the trigger endpoint directly (no setup step needed)
  - Assert response.status === 200 and response.body contains expected fields
  - The trigger_payload body is the exact request body to send

Correct structure:
  it("link-token-create-call-test", () => {
    cy.request({
      method: "POST",
      url: `${globalState.get("baseUrl")}/payment_methods/auth/link`,
      headers: { "api-key": globalState.get("apiKey") },
      body: { connector: "adyen", ... },   // use exact trigger_payload body
      failOnStatusCode: false,
    }).then(resp => {
      expect(resp.status).to.equal(200)
      expect(resp.body).to.have.property("link_token")
    })
  })"""

    if result.flow_type == FlowType.CORE_ONLY:
        return """## Codegen strategy: CORE_ONLY
This flow tests pure API behaviour — no connector processing involved.
The payment is created with confirm=false (not routed to a connector).
Tests exercise Hyperswitch's own validation/auth/business logic.

What to generate:
  - One describe() block for the flow
  - Setup: create payment intent with the setup_payloads body (inline card data, confirm=false)
  - Test cases: one it() per case in trigger_payloads (success + error cases)
  - For error cases: assert response.body.error.code or error.message
  - For success cases: assert the expected fields are present in response
  - No connector config needed — all inline request bodies provided in setup_payloads"""

    elif result.flow_type == FlowType.CONNECTOR_ONLY:
        return """## Codegen strategy: CONNECTOR_ONLY
This flow tests connector integration — needs connector config from connectorDetails.
The payment is routed to and processed by the connector.

What to generate:
  - Import getConnectorDetails, getValueByKey from Utils.js
  - Use connectorData = getConnectorDetails(globalState.get("connectorId"))["card_pm"]
  - Use connectorData["FlowName"] to get Request/Response shapes
  - Tests should read expected values from connectorDetails, not hardcode them
  - Setup: use the setup_payloads as the base, merge with connector config
  - Connector name comes from Cypress.env("CONNECTOR") at runtime"""

    else:  # CORE_THEN_CONNECTOR
        core_spec = result.core_spec_hint or "an existing core spec"
        return f"""## Codegen strategy: CORE_THEN_CONNECTOR
This flow needs core setup (API-level) THEN connector processing.

Step 1 — Core setup (does {core_spec} already exist?):
  - If yes: the setup_payloads steps are handled by that existing spec
  - If no: include setup inline using the setup_payloads bodies

Step 2 — Connector trigger:
  - The trigger_payloads send requests that route to the connector
  - Import connector config as in CONNECTOR_ONLY strategy
  - State (paymentId etc.) flows from setup to trigger via globalState

What to generate:
  - before() block that runs setup_payloads (creates payment with customer_id etc.)
  - it() blocks that run trigger_payloads (update/confirm with connector)
  - Both success and error cases from trigger_payloads"""


def build_flow_context(
    result:    FlowCoverageResult,
    repo_root: str,
    q:         Optional[FlowQueryEngine] = None,
) -> FlowContextBundle:
    """
    Build the LLM prompt for creating/updating a cypress spec.
    Strategy is determined by result.flow_type.
    """
    from flow_query import FlowType, CoverageStatus

    flow           = result.flow
    root           = Path(repo_root)
    endpoint_group = detect_endpoint_group(result.trigger_paths)
    spec_dir       = SPEC_DIR_MAP.get(endpoint_group, "cypress/e2e/spec/Payment")
    spec_target    = result.spec_to_create or f"{spec_dir}/00041-NewFlow.cy.js"

    # Get style reference — endpoint-group aware
    similar_spec, similar_path = _get_style_reference(result, root, q, repo_root, endpoint_group)

    # Format flow sections
    trigger_section = _format_trigger_cases(flow.get("trigger_payloads", {}))
    setup_section   = _format_setup(flow.get("setup_payloads", []))
    prereq_section  = _format_prerequisites(flow.get("prerequisites", []))
    strategy_instr  = _strategy_instructions(result, endpoint_group)

    # Task description
    if result.status == CoverageStatus.MISSING:
        task = "Create a NEW cypress spec file from scratch for this flow."
    else:
        task = (
            "A candidate spec was found covering the same endpoints. "
            "Check if it covers this exact scenario. "
            "If not, add the missing test cases."
        )

    # Existing it() blocks from Neo4j — what test names already exist for these endpoints
    existing_it_blocks = result.classification.get("existing_it_blocks", {})
    it_blocks_section  = _format_existing_it_blocks(existing_it_blocks)

    # Build prompt
    parts = [
        "Generate a Cypress spec for the following Hyperswitch API flow.",
        "",
        "## Flow",
        "flow_id: " + str(result.flow_id),
        result.description,
        "",
        strategy_instr,
        "",
        "## Prerequisites",
        prereq_section,
        "",
        "## Existing it() blocks for these endpoints (from index)",
        it_blocks_section,
        "",
        "## Setup payloads (run in before() block)",
        setup_section,
        "## Trigger test cases",
        trigger_section,
        "## Task",
        task,
        "",
        "## Style reference (" + (similar_path or "none") + ")",
        ("Follow this spec structure EXACTLY:" if similar_spec else
         "Infer style from standard Hyperswitch cypress patterns."),
        ("```javascript\n" + similar_spec[:4000] + "\n```") if similar_spec else "",
        "",
        "## Required patterns",
        "- let globalState; at top, populated in before()",
        "- cy.task() for all API calls (not cy.request() directly)",
        "- State flows through globalState between it() blocks",
        "- Error cases: assert response.body.error.code or error.message",
        "- Success cases: assert expected fields from trigger_payloads.expected_result",
        "",
        "## Output — return ONLY this:",
        '<file path="' + spec_target + '">',
        "...complete spec...",
        "</file>",
    ]

    prompt = "\n".join(p for p in parts if p is not None)

    return FlowContextBundle(
        flow_id        = result.flow_id,
        description    = result.description,
        status         = result.status,
        prompt         = prompt,
        system_prompt  = SYSTEM_PROMPT,
        spec_to_create = spec_target,
        files_to_edit  = [spec_target],
        patch_meta     = {
            "type":        "full_file_rewrite",
            "target_file": spec_target,
            "flow_type":   result.flow_type,
        },
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, sys, os

    ap = argparse.ArgumentParser()
    ap.add_argument("--flow",     required=True, help="Path to flow JSON or '-' for stdin")
    ap.add_argument("--repo",     default=os.environ.get("CYPRESS_REPO", "."))
    ap.add_argument("--uri",      default="bolt://localhost:7687")
    ap.add_argument("--user",     default="neo4j")
    ap.add_argument("--password", default="Hyperswitch123")
    ap.add_argument("--show-prompt", action="store_true")
    args = ap.parse_args()

    if args.flow == "-":
        flow = json.load(sys.stdin)
    else:
        with open(args.flow) as f:
            flow = json.load(f)

    q = FlowQueryEngine(args.uri, (args.user, args.password))
    try:
        result  = q.check_coverage(flow)
        bundle  = build_flow_context(result, args.repo, q)

        print(f"flow_id       : {bundle.flow_id}")
        print(f"Status        : {bundle.status}")
        print(f"Spec target   : {bundle.spec_to_create}")
        print(f"Prompt tokens : ~{len(bundle.prompt) // 4:,}")

        if args.show_prompt:
            print("\n" + "─" * 60)
            print(bundle.prompt)
    finally:
        q.close()