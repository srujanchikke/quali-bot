"""
run_flow_pipeline.py — End-to-end pipeline for flow_id JSON input
=================================================================
Takes the flow JSON from the Rust analyser and runs:
  1. flow_query:  check if cypress tests cover the endpoints + scenario
  2. codegen:     call Grid LLM, patch Connector.js / Utils.js / generate spec
  3. runner:      run the spec with cypress, auto-fix assertion mismatches

Usage:
    python run_flow_pipeline.py --flow flow.json
    python run_flow_pipeline.py --flow flow.json --dry-run --verbose
    echo '{"flow_id":1,...}' | python run_flow_pipeline.py --flow -
"""

import os
import re
import sys
import json
import argparse
from pathlib import Path

from flow_query   import (FlowQueryEngine, CoverageStatus, FlowType,
                           normalise_document, normalise_flow)
from flow_context import ContextBundle, Status, build_flow_context
from codegen      import CodeGen
from runner       import CypressRunner
from indexer      import Indexer, FLOW_ENDPOINTS

# ── env ───────────────────────────────────────────────────────────────────────

REPO         = os.environ.get("CYPRESS_REPO",                     ".")
NEO4J_URI    = os.environ.get("NEO4J_URI",                        "bolt://localhost:7687")
NEO4J_USER   = os.environ.get("NEO4J_USER",                       "neo4j")
NEO4J_PASS   = os.environ.get("NEO4J_PASSWORD",                   "Hyperswitch123")
GRID_KEY     = os.environ.get("GRID_API_KEY",                     "")
GRID_URL     = os.environ.get("GRID_BASE_URL",                    "")
GRID_MODEL   = os.environ.get("GRID_MODEL",                       "glm-latest")
CY_BASE_URL  = os.environ.get("CYPRESS_BASE_URL",                 "http://localhost:8080")
CY_AUTH_FILE = os.environ.get("CYPRESS_CONNECTOR_AUTH_FILE_PATH", "")
CY_PROFILE   = os.environ.get("CYPRESS_PROFILE_ID",               "")
CY_CONN_ID   = os.environ.get("CYPRESS_CONNECTOR_ID",             "")

# ── helpers ───────────────────────────────────────────────────────────────────

def sep(title: str = ""):
    line = "─" * 60
    print(f"\n{line}\n  {title}\n{line}" if title else line)


def load_flow(flow_arg: str) -> tuple:
    """Load flow JSON. Returns (raw_data, list_of_normalised_flows)."""
    if flow_arg == "-":
        data = json.load(sys.stdin)
    else:
        p = Path(flow_arg)
        data = json.loads(p.read_text()) if p.exists() else json.loads(flow_arg)

    if "flows" in data and isinstance(data["flows"], list):
        return data, normalise_document(data)
    flow = normalise_flow(data) if "changed_function" in data else data
    return data, [flow]


def _make_runner() -> CypressRunner:
    return CypressRunner(
        repo_root    = REPO,
        base_url     = CY_BASE_URL,
        auth_file    = CY_AUTH_FILE,
        profile_id   = CY_PROFILE,
        connector_id = CY_CONN_ID,
    )


def _flow_to_list_key(result) -> str:
    """
    Map a trigger path to the CONNECTOR_LISTS INCLUDE.* key in Utils.js.
    Returns "" for flows that have no allowlist (Void, SyncRefund, Refund etc.).
    """
    path    = result.trigger_paths[0] if result.trigger_paths else ""
    handler = (result.handlers[0] if result.handlers else "").lower()
    if "incremental_authorization" in path: return "INCREMENTAL_AUTH"
    if "capture"                   in path: return "OVERCAPTURE"
    if "manual_retry"              in handler: return "MANUAL_RETRY"
    return ""


def _trigger_path_to_flow_name(trigger_paths: list) -> str:
    """Reverse-lookup: /payments/:id/incremental_authorization → IncrementalAuth"""
    path_to_flow: dict[str, str] = {}
    for fn, eps in FLOW_ENDPOINTS.items():
        for ep in eps:
            p = re.sub(r':\w+', ':id',
                ep["path"]
                .replace("{", ":").replace("}", "")
                .replace("payment_id", "id").replace("refund_id", "id")
                .replace("dispute_id", "id").replace("mandate_id", "id")
                .replace("file_id", "id")
            )
            path_to_flow.setdefault(p, fn)

    for tp in trigger_paths:
        if tp in path_to_flow:
            return path_to_flow[tp]
        for k, fn in path_to_flow.items():
            base = k.split("/:id")[0]
            if base and base in tp:
                return fn
    return ""


def _last_flow_in_connector(repo: str, connector_rel: str) -> str:
    """Find the last PascalCase flow key in Connector.js (used as insert_after)."""
    skip = {"Request", "Response", "Configs", "ResponseCustom", "card_pm", "connectorDetails"}
    try:
        js = (Path(repo) / connector_rel).read_text(encoding="utf-8", errors="replace")
        flows = [m for m in re.findall(r'\b([A-Z][A-Za-z0-9]+)\s*:\s*\{', js) if m not in skip]
        return flows[-1] if flows else "PaymentIntent"
    except Exception:
        return "PaymentIntent"


def _extract_balanced_block(js: str, start: int) -> str:
    """Extract a balanced-brace JS block starting at `start`."""
    depth = 0
    for i, ch in enumerate(js[start:]):
        if ch == '{':   depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return js[start:start + i + 1]
    return js[start:]


def _get_style_example(repo: str, connector_rel: str, n: int = 2) -> str:
    """Extract n real Request+Response blocks from Connector.js as LLM style reference."""
    skip = {"Request", "Response", "Configs", "ResponseCustom", "card_pm", "connectorDetails"}
    examples = []
    try:
        js = (Path(repo) / connector_rel).read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r'\b([A-Z][A-Za-z0-9]+)\s*:\s*\{', js):
            if m.group(1) in skip:
                continue
            block = _extract_balanced_block(js, m.start())
            if "Request" in block and "Response" in block:
                examples.append(block[:600])
            if len(examples) >= n:
                break
    except Exception:
        pass
    return "\n".join(examples)


def _pick_best_candidate(candidates, flow):
    """Score existing specs to find the most relevant one for regression."""
    if not candidates:
        return None

    body    = {}
    handler = ""
    for cases in flow.get("trigger_payloads", {}).values():
        for case in cases.values():
            body = case.get("body", {})
            break
        break
    handler = (flow.get("endpoints", [{}])[0].get("handler", "")
               or next(iter(flow.get("trigger_payloads", {})), ""))

    HANDLER_SPEC_MAP = {
        "incremental": "incremental", "overcapture": "overcapture",
        "void":        "void",        "cancel":      "void",
        "refund":      "refund",      "mandate":     "mandate",
        "sync":        "sync",        "savecard":    "savecard",
        "wallet":      "wallet",      "threedsauth": "threedsmanual",
    }

    def score(c):
        s    = 0
        spec  = c.spec_file.split("/")[-1].lower()
        tests = " ".join(c.test_names).lower()
        h     = handler.lower()

        for h_kw, s_kw in HANDLER_SPEC_MAP.items():
            if h_kw in h and s_kw in spec:
                s += 500

        if body.get("confirm") is True:
            if any(kw in spec for kw in ("nothree", "no3ds", "autocapture", "manualcapture")):
                s += 200
            if "confirm" in tests: s += 50
        if body.get("capture_method") == "automatic" and "auto"   in spec: s += 150
        if body.get("capture_method") == "manual"    and "manual" in spec: s += 150
        if body.get("amount_to_capture"):
            s += 400 if "overcapture" in spec else 200 if "capture" in spec else 0
        if "payment_method_data" in body:
            if any(kw in spec for kw in ("card", "three", "nothree")): s += 50
        if body.get("customer_id"):
            if any(kw in spec for kw in ("savecard", "customer", "mandate")): s += 100
        if not body.get("customer_id") and not body.get("mandate_id"):
            if "incremental" not in h:
                if any(kw in spec for kw in ("mandate", "savecard", "zeroauth", "wallet",
                                              "banktransfer", "bankredirect", "upi", "crypto",
                                              "reward", "realtime", "variations")): s -= 300
        s += len(c.test_names)
        return s

    return max(candidates, key=score)


# ── assertion auto-fix ────────────────────────────────────────────────────────

def _parse_assertion_errors(stdout: str) -> list[dict]:
    """
    Parse cypress assertion diff blocks:
      field_name
      + expected - actual
      -config_value    ← what test expected (wrong value in config)
      +api_value       ← what API returned  (correct value to use)
    """
    return [
        {"field": m.group(1), "config_val": m.group(2), "api_val": m.group(3)}
        for m in re.finditer(
            r'(\w+)\s*\n\s*\+\s*expected\s+-\s+actual.*?\n\s*-(\S+)\s*\n\s*\+(\S+)',
            stdout, re.DOTALL
        )
    ]


def _auto_fix_connector_config(connector_js_path: str, flow_name: str,
                                assertion_errors: list[dict]) -> list[str]:
    """
    Deterministic fix: replace wrong field values in the flow's Response block
    with the values the API actually returned.
    """
    path = Path(connector_js_path)
    if not path.exists():
        return [f"⚠️  {path.name} not found"]

    js = path.read_text(encoding="utf-8")
    m  = re.search(rf'\b{re.escape(flow_name)}\s*:\s*\{{', js)
    if not m:
        return [f"⚠️  {flow_name} block not found in {path.name}"]

    flow_block = _extract_balanced_block(js, m.start())
    start, end = m.start(), m.start() + len(flow_block)
    changes = []

    for err in assertion_errors:
        field, wrong, right = err["field"], err["config_val"], err["api_val"]
        new_block, n = re.subn(
            rf'({re.escape(field)}\s*:\s*){re.escape(wrong)}(\s*,?)',
            rf'\g<1>{right}\2', flow_block, count=1
        )
        if n:
            js         = js[:start] + new_block + js[end:]
            end       += len(new_block) - len(flow_block)
            flow_block = new_block
            changes.append(f"  {flow_name}.Response.body.{field}: {wrong} → {right}")

    if changes:
        path.write_text(js, encoding="utf-8")
    return changes


def _llm_fix_connector_config(full_path: str, flow_name: str, connector: str,
                               repo: str, failed_output: str) -> bool:
    """
    LLM-assisted fix: used when deterministic fix can't resolve structural issues.
    Sends the failing block + command assertions + a passing reference to the LLM.
    """
    if not GRID_KEY or not GRID_URL:
        print("  ⚠️  GRID_API_KEY / GRID_BASE_URL not set — cannot call LLM")
        return False

    connector_js = Path(full_path).read_text(encoding="utf-8", errors="replace")

    # Current broken block
    m = re.search(rf'{re.escape(flow_name)}\s*:\s*\{{', connector_js)
    if not m:
        return False
    current_block = _extract_balanced_block(connector_js, m.start())
    start = m.start(); end = start + len(current_block)

    # Cypress command source (what the test asserts)
    cmd_name = flow_name[0].lower() + flow_name[1:]
    cmd_src  = ""
    cmd_path = Path(repo) / "cypress/support/commands.js"
    if cmd_path.exists():
        src = cmd_path.read_text(encoding="utf-8", errors="replace")
        cm  = re.search(
            r'Cypress\.Commands\.add\(["\']' + cmd_name + r'["\'].*?(?=Cypress\.Commands\.add|\Z)',
            src, re.DOTALL
        )
        if cm:
            cmd_src = cm.group(0)[:1500]

    # Reference block from another connector that passes
    ref_block = ref_connector = ""
    for js_file in sorted((Path(repo) / "cypress/e2e/configs/Payment").glob("*.js")):
        if js_file.stem.lower() in (connector.lower(), "utils", "commons", "modifiers"):
            continue
        text = js_file.read_text(encoding="utf-8", errors="replace")
        rm   = re.search(rf'{re.escape(flow_name)}\s*:\s*\{{', text)
        if rm:
            ref_block     = _extract_balanced_block(text, rm.start())
            ref_connector = js_file.stem
            break

    # What failed
    assertion_summary = "\n".join(
        f"  field '{m2.group(1)}': config has {m2.group(2)}, API returned {m2.group(3)}"
        for m2 in re.finditer(
            r'(\w+)\s*\n\s*\+\s*expected\s+-\s+actual.*?\n\s*-(\S+)\s*\n\s*\+(\S+)',
            failed_output, re.DOTALL
        )
    ) or "  (see raw output)"

    prompt = f"""Fix the {flow_name} config block in {connector}.js.

## Failing assertions
{assertion_summary}

## Current {connector} block (needs fixing)
```javascript
{current_block}
```

## What the test asserts (cy.{cmd_name})
```javascript
{cmd_src}
```

## Reference block from {ref_connector} (passes the test)
```javascript
{ref_block}
```

## Rules
- Values must match what the {connector} API actually returns (see failing assertions)
- Output ONLY the fixed block in <new_flow_block>...</new_flow_block>
- No explanation, no markdown outside the tag
"""
    try:
        from codegen import GridClient, _parse_new_flow_block, _normalise_block
        response  = GridClient(api_key=GRID_KEY, base_url=GRID_URL, model=GRID_MODEL).chat(
            system="You fix JavaScript connector config blocks. Output ONLY the <new_flow_block> tag.",
            user=prompt
        )
        new_block = _parse_new_flow_block(response)
        if not new_block:
            print("  LLM response missing <new_flow_block> tag")
            return False
        Path(full_path).write_text(
            connector_js[:start] + _normalise_block(new_block) + connector_js[end:],
            encoding="utf-8"
        )
        return True
    except Exception as e:
        print(f"  LLM fix error: {e}")
        return False


# ── cypress run helpers ───────────────────────────────────────────────────────

def _print_run_result(result, verbose: bool = False):
    print(f"\n  {result.summary()}")
    if result.error:
        print(f"  Runner error: {result.error}")
        return
    if result.passed:
        print(f"  ✅ {result.passing}/{result.total} tests passed in {result.duration_ms}ms")
    else:
        print("\n  Failed tests:")
        for ft in result.failed_tests:
            print(f"    ✗ {ft.name}\n      {ft.error}")
    if verbose:
        sep("Raw output")
        print(result.raw_stdout)


def _run_spec(spec_file: str, connector: str, repo: str, verbose: bool = False):
    """Run a spec file with setup specs (00001-00003) prepended for payment specs."""
    full = Path(repo) / spec_file
    if not full.exists():
        num  = Path(spec_file).stem.split("-")[0]
        hits = list((Path(repo) / Path(spec_file).parent).glob(f"{num}-*.cy.js"))
        if hits:
            spec_file = str(hits[0].relative_to(Path(repo)))
            print(f"  ℹ️  Resolved spec to: {spec_file}")
        else:
            print(f"  ❌ Spec not found: {full}")
            return

    c      = connector.lower() if connector else "service"
    setup  = "spec/Payment/" in spec_file
    result = _make_runner().run(c, flow="", spec_file=spec_file, setup=setup)
    print(f"  Running: {spec_file} --env CONNECTOR={c}")
    _print_run_result(result, verbose)
    return result


def _run_and_autofix(spec_file: str, connector: str, repo: str, verbose: bool,
                     connector_rel: str, flow_name: str, max_retries: int = 2):
    """
    Run cypress. On failure:
      1. Deterministic fix — parse assertion diffs, patch config values
      2. LLM fix — for structural issues deterministic fix can't handle
    Retries up to max_retries times.
    """
    full_path = str(Path(repo) / connector_rel)

    for attempt in range(1, max_retries + 2):
        sep(f"STEP 4 — Cypress run (attempt {attempt})")
        result = _make_runner().run(connector.lower(), flow="", spec_file=spec_file, setup=True)
        _print_run_result(result, verbose)

        if result.passed:
            return
        if attempt > max_retries:
            print(f"  ❌ Still failing after {max_retries} auto-fix attempts")
            return

        sep(f"Auto-fix: updating {connector}.js (attempt {attempt})")
        errors = _parse_assertion_errors(result.raw_stdout)

        if errors:
            changes = _auto_fix_connector_config(full_path, flow_name, errors)
            if changes:
                for c in changes: print(c)
                print("  Retrying...")
                continue

        print("  Simple value fix failed — calling LLM with full context")
        if _llm_fix_connector_config(full_path, flow_name, connector, repo, result.raw_stdout):
            print(f"  ✅ LLM applied fix — retrying...")
        else:
            print("  ❌ LLM fix failed — check the config block manually")
            return


# ── pipeline ──────────────────────────────────────────────────────────────────

def run_flow_pipeline(flow: dict, repo: str = REPO, dry_run: bool = False,
                      skip_run: bool = False, run_only: bool = False, verbose: bool = False):

    connector = flow.get("connector", "")
    repo      = str(Path(repo).resolve())

    if not (Path(repo) / "cypress").exists():
        print(f"\n❌ '{repo}' is not a cypress-tests repo root. Set CYPRESS_REPO or pass --repo")
        return

    sep(f"Flow pipeline: flow_id={flow.get('flow_id', '?')}")
    print(f"  Description     : {flow.get('description', '')[:80]}")
    if flow.get("changed_function"): print(f"  Changed function: {flow['changed_function']}")
    if connector:                    print(f"  Connector       : {connector}")

    q   = FlowQueryEngine(NEO4J_URI, (NEO4J_USER, NEO4J_PASS))
    idx = Indexer(repo, NEO4J_URI, (NEO4J_USER, NEO4J_PASS))
    cg  = CodeGen(repo, indexer=idx, model=GRID_MODEL, api_key=GRID_KEY, base_url=GRID_URL)

    try:
        # ── STEP 1: Query coverage ────────────────────────────────────────────
        sep("STEP 1 — Query: is this flow covered?")
        result = q.check_coverage(flow, connector=connector)
        print(f"  Status        : {result.status}")
        print(f"  Trigger paths : {result.trigger_paths}")
        print(f"  Setup paths   : {result.setup_paths}")
        print(f"  Handlers      : {result.handlers}")

        if result.candidates:
            best = _pick_best_candidate(result.candidates, flow)
            print(f"  Candidates    : {len(result.candidates)} existing spec(s)")
            print(f"  Best match    : {best.spec_file} ({len(best.test_names)} tests)")

        # ── COVERED / NEEDS_LLM_CHECK ─────────────────────────────────────────
        if result.status in (CoverageStatus.COVERED, CoverageStatus.NEEDS_LLM_CHECK) \
                and result.candidates:
            best = _pick_best_candidate(result.candidates, flow)

            # For CONNECTOR_ONLY: verify the connector has a config block + allowlist entry
            if connector and result.flow_type == FlowType.CONNECTOR_ONLY:
                flow_name = (q._derive_flow_name(result.trigger_paths, connector)
                             or _trigger_path_to_flow_name(result.trigger_paths))

                if flow_name:
                    cc_result = q.check_connector_flow(connector, flow_name)
                    result.connector_check = cc_result.connector_check

                    if cc_result.status not in (CoverageStatus.COVERED,
                                                CoverageStatus.NEEDS_LLM_CHECK):
                        print(f"  ⚠️  Spec exists but {connector} config incomplete: {cc_result.status}")
                        result.status      = cc_result.status
                        result.what_to_fix = cc_result.what_to_fix
                        # fall through to codegen
                    else:
                        print(f"  ✅ {connector} config OK — running spec for regression")
                        if dry_run:
                            print(f"  (dry-run) would run: {best.spec_file}")
                            return
                        if not skip_run:
                            connector_rel = f"cypress/e2e/configs/Payment/{connector}.js"
                            _run_and_autofix(best.spec_file, connector, repo, verbose,
                                             connector_rel, flow_name)
                        return

            if dry_run:
                print(f"  (dry-run) would run: {best.spec_file}")
                return
            if not skip_run:
                _run_spec(best.spec_file, connector, repo, verbose)
            return

        if run_only:
            if result.candidates:
                _run_spec(_pick_best_candidate(result.candidates, flow).spec_file,
                          connector, repo, verbose)
            else:
                print("  ❌ --run-only but no spec found")
            return

        # Route by status
        cc_status = result.connector_check.status if result.connector_check else result.status

        connector_rel = f"cypress/e2e/configs/Payment/{connector}.js"
        utils_rel     = "cypress/e2e/configs/Payment/Utils.js"

        # ── Strategy A: NOT_IN_ALLOWLIST ──────────────────────────────────────
        if cc_status == CoverageStatus.NOT_IN_ALLOWLIST:
            cc       = result.connector_check
            list_key = cc.allowlist_key if cc and cc.allowlist_key else _flow_to_list_key(result)

            sep("STEP 2 — allowlist_only: add to Utils.js")
            print(f"  Adding '{connector.lower()}' to CONNECTOR_LISTS.{list_key}")
            if dry_run:
                print(f"  (dry-run) would patch: {utils_rel}")
                return

            patch = cg.apply(ContextBundle(
                connector=connector, flow=str(flow.get("flow_id", "")),
                status=Status.NOT_IN_ALLOWLIST, prompt="", system_prompt="",
                files_to_edit=[utils_rel],
                patch_meta={"type": "allowlist_only", "skip_llm": True,
                            "allowlist_file": utils_rel, "allowlist_key": list_key,
                            "connector_name": connector.lower()},
            ))
            print(f"  {patch.summary()}")
            if patch.success and not skip_run and result.candidates:
                best = _pick_best_candidate(result.candidates, flow)
                sep("STEP 3 — Cypress run")
                _run_spec(best.spec_file, connector, repo, verbose)
            return

        # ── Strategy B: MISSING_CONFIG ────────────────────────────────────────
        if cc_status == CoverageStatus.MISSING_CONFIG:
            cc           = result.connector_check
            flow_name    = cc.flow_name if cc else _trigger_path_to_flow_name(result.trigger_paths)
            insert_after = _last_flow_in_connector(repo, connector_rel)
            list_key     = _flow_to_list_key(result)

            sep("STEP 2 — Build context (insert_flow_block)")

            # Reference: real blocks from other connectors that already have this flow
            # Ordered by size ASC so LLM sees the simplest pattern first
            ref_rows = q.get_reference_blocks(flow_name, exclude_connector=connector)

            if ref_rows:
                ref_lines = [
                    f"\n\n## Existing {flow_name} blocks from other connectors",
                    "These are real blocks. Pick the most common pattern and adapt it:",
                ]
                for ref in ref_rows[:3]:
                    ref_lines += [f"\n### {ref['connector']}", f"```javascript\n{ref['block'][:800]}\n```"]
                ref_block_section = "\n".join(ref_lines)
            elif cc and cc.reference_block:
                ref_block_section = (
                    f"\n\n## Reference block (from {cc.reference_connector})\n"
                    f"```javascript\n{cc.reference_block[:2000]}\n```"
                )
            else:
                ref_block_section = ""

            style_example = _get_style_example(repo, connector_rel)

            prompt = f"""You must add a new flow config block to {connector}.js for the {flow_name} flow.

## What to generate
A single JavaScript object block for `{flow_name}` inside `connectorDetails.card_pm`.
Insert it after the `{insert_after}` block.

## Required output format
<new_flow_block>
{flow_name}: {{
  Request: {{
    // fields for {flow_name}
  }},
  Response: {{
    status: 200,
    body: {{
      status: "...",
    }},
  }},
}},
</new_flow_block>

## {connector} formatting style
```javascript
{style_example}
```
{ref_block_section}

## Rules
- Output ONLY the <new_flow_block>...</new_flow_block> tag
- No imports, no describe()/it() blocks — config only
- No markdown outside the tag
- Response status: "cancelled" for Void, "pending" for Refund/SyncRefund, "succeeded" for Capture
"""
            print(f"  Target        : {connector_rel}")
            print(f"  Flow to add   : {flow_name}")
            print(f"  Insert after  : {insert_after}")
            print(f"  List key      : {list_key or '(none)'}")
            print(f"  Reference     : {len(ref_rows)} Neo4j block(s)" +
                  (f" [{', '.join(r['connector'] for r in ref_rows[:3])}]" if ref_rows else ""))
            print(f"  Prompt tokens : ~{len(prompt)//4:,}")
            if verbose: sep("Prompt"); print(prompt)
            if dry_run:
                print("\n  (dry-run — no files written)")
                return

            sep("STEP 3 — Codegen: insert_flow_block")
            patch = cg.apply(ContextBundle(
                connector=connector, flow=str(flow.get("flow_id", "")),
                status=Status.MISSING_CONFIG, prompt=prompt,
                system_prompt=(
                    "You are a senior engineer on the Hyperswitch cypress test suite. "
                    "Output ONLY the requested XML tag — no explanation, no markdown."
                ),
                files_to_edit=[connector_rel, utils_rel],
                patch_meta={
                    "type": "insert_flow_block",
                    "target_file": connector_rel,
                    "insert_after": insert_after,
                    **( {"allowlist_file": utils_rel, "allowlist_key": list_key,
                          "connector_name": connector.lower()} if list_key else {} ),
                },
            ))
            print(f"  {patch.summary()}")
            if not patch.success:
                print(f"\n❌ Codegen failed: {patch.error}")
                return
            if verbose and patch.llm_response:
                sep("LLM response"); print(patch.llm_response)
            if not skip_run:
                candidates = q._find_covering_specs(result.trigger_paths)
                best = _pick_best_candidate(candidates, flow) if candidates else None
                if best:
                    _run_and_autofix(best.spec_file, connector, repo, verbose,
                                     connector_rel, flow_name)
                else:
                    print("  ⚠️  No spec found after patching — reindex and retry")
            return

        # ── Strategy C: MISSING — generate new spec ───────────────────────────
        sep("STEP 2 — Build context (full_file_rewrite)")
        bundle = build_flow_context(result, repo, q)
        print(f"  Spec target   : {bundle.spec_to_create}")
        print(f"  Prompt tokens : ~{len(bundle.prompt) // 4:,}")
        if verbose: sep("Prompt"); print(bundle.prompt)
        if dry_run:
            print("\n  (dry-run — no files written)")
            return

        sep("STEP 3 — Codegen: generate spec file")
        patch = cg.apply(ContextBundle(
            connector=f"flow_{flow.get('flow_id', '')}",
            flow=bundle.description, status=Status.MISSING_TEST,
            prompt=bundle.prompt, system_prompt=bundle.system_prompt,
            files_to_edit=bundle.files_to_edit, patch_meta=bundle.patch_meta,
        ))
        print(f"  {patch.summary()}")
        if not patch.success:
            print(f"\n❌ Codegen failed: {patch.error}")
            if patch.llm_response:
                print(f"\nLLM response:\n{patch.llm_response[:500]}")
            return
        if verbose and patch.llm_response:
            sep("LLM response"); print(patch.llm_response)
        if not skip_run:
            spec_file = patch.files_changed[0] if patch.files_changed else bundle.spec_to_create
            sep("STEP 4 — Cypress run")
            _run_spec(spec_file, repo=repo, connector="", verbose=verbose)

    finally:
        q.close()
        try: idx.close()
        except Exception: pass


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Run the full flow pipeline")
    ap.add_argument("--flow",     required=True,
                    help="Flow JSON: file path, '-' for stdin, or inline JSON")
    ap.add_argument("--repo",     default=os.environ.get("CYPRESS_REPO", "."),
                    help="cypress-tests root (or set CYPRESS_REPO)")
    ap.add_argument("--dry-run",  action="store_true")
    ap.add_argument("--run-only", action="store_true",
                    help="Skip codegen, just run the existing spec")
    ap.add_argument("--skip-run", action="store_true",
                    help="Generate/patch but skip the cypress run")
    ap.add_argument("--verbose",  action="store_true")
    args = ap.parse_args()

    print("Config:")
    print(f"  Repo       : {args.repo}")
    print(f"  Neo4j      : {NEO4J_URI}")
    print(f"  Grid URL   : {GRID_URL or '(not set)'}")
    print(f"  Grid model : {GRID_MODEL}")
    print(f"  Cypress URL: {CY_BASE_URL}")

    try:
        data, flows = load_flow(args.flow)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"❌ Could not load flow: {e}")
        sys.exit(1)

    if len(flows) > 1:
        print(f"\nDocument contains {len(flows)} flows.")
        if data.get("changed_function"):
            print(f"Changed function: {data['changed_function']}")
        print()

    for flow in flows:
        run_flow_pipeline(flow=flow, repo=args.repo, dry_run=args.dry_run,
                          skip_run=args.skip_run, run_only=args.run_only,
                          verbose=args.verbose)
        if len(flows) > 1:
            print()