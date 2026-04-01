"""
run_flow_pipeline.py — End-to-end pipeline for flow_id JSON input
=================================================================
Takes the new flow JSON format and runs:
  1. flow_query:   check if cypress tests cover the endpoints + scenario
  2. flow_context: build LLM prompt if coverage missing
  3. codegen:      call Grid LLM, write new spec file to disk
  4. runner:       run the new spec with cypress

Usage:
    python run_flow_pipeline.py --flow flow.json
    python run_flow_pipeline.py --flow flow.json --dry-run --verbose
    echo '{"flow_id":1,...}' | python run_flow_pipeline.py --flow -

Flow JSON can be:
  - A file path:  --flow /path/to/flow.json
  - Stdin:        --flow -
  - Inline JSON:  --flow '{"flow_id":1,...}'
"""

import os
import sys
import json
import argparse
from pathlib import Path

from flow_query   import FlowQueryEngine, CoverageStatus, FlowType, normalise_document, normalise_flow
from flow_context import build_flow_context
from codegen      import CodeGen
from runner       import CypressRunner

# ── env defaults ─────────────────────────────────────────────────────────────

REPO         = os.environ.get("CYPRESS_REPO",                     ".")
NEO4J_URI    = os.environ.get("NEO4J_URI",                        "bolt://localhost:7687")
NEO4J_USER   = os.environ.get("NEO4J_USER",                       "neo4j")
NEO4J_PASS   = os.environ.get("NEO4J_PASSWORD",                   "Hyperswitch123")
GRID_KEY     = os.environ.get("GRID_API_KEY",                     "")
GRID_URL     = os.environ.get("GRID_BASE_URL",                    "")
GRID_MODEL   = os.environ.get("GRID_MODEL",                       "glm-latest")
CY_BASE_URL  = os.environ.get("CYPRESS_BASE_URL") or os.environ.get(
    "CYPRESS_BASEURL", "http://localhost:8080"
)
CY_AUTH_FILE = os.environ.get("CYPRESS_CONNECTOR_AUTH_FILE_PATH", "")
CY_PROFILE   = os.environ.get("CYPRESS_PROFILE_ID",               "")
CY_CONN_ID   = os.environ.get("CYPRESS_CONNECTOR_ID",             "")


def sep(title: str = ""):
    line = "─" * 60
    if title:
        print(f"\n{line}\n  {title}\n{line}")
    else:
        print(line)


def load_flow(flow_arg: str) -> tuple:
    """
    Load flow JSON. Returns (data, flows) where:
      data  = raw parsed JSON (may be document or single flow)
      flows = list of normalised flows ready for check_coverage()

    Handles three input shapes:
      1. Full document with data['flows'] list (new format from Rust analyser)
      2. Single flow dict                      (old format)
      3. Inline JSON string
    """
    if flow_arg == "-":
        data = json.load(sys.stdin)
    else:
        p = Path(flow_arg)
        if p.exists():
            data = json.loads(p.read_text())
        else:
            try:
                data = json.loads(flow_arg)
            except json.JSONDecodeError:
                raise ValueError(f"Cannot read flow: '{flow_arg}'")

    # Detect document vs single flow
    if "flows" in data and isinstance(data["flows"], list):
        # Full document — extract and normalise all flows
        flows = normalise_document(data)
        return data, flows
    else:
        # Single flow — normalise it
        flow = normalise_flow(data) if "changed_function" in data else data
        return data, [flow]


import re as _re

def _pick_best_candidate(candidates, flow):
    """
    Pick the most relevant existing spec for a regression run.
    Scores by matching the trigger body's payment characteristics to spec names/test names.
    """
    if not candidates: return None

    # Get trigger body signals
    body = {}
    for cases in flow.get("trigger_payloads", {}).values():
        for case in cases.values():
            body = case.get("body", {})
            break
        break

    # Also score by handler name keywords
    handler = (flow.get("endpoints", [{}])[0].get("handler", "")
               or next(iter(flow.get("trigger_payloads", {})), ""))

    # Map handler keywords → spec keywords
    HANDLER_SPEC_MAP = {
        "incremental": "incremental",
        "overcapture": "overcapture",
        "void":        "void",
        "cancel":      "void",
        "refund":      "refund",
        "mandate":     "mandate",
        "sync":        "sync",
        "savecard":    "savecard",
        "wallet":      "wallet",
        "threedsauth": "threedsmanual",
    }

    def score(c):
        s = 0
        spec = c.spec_file.split("/")[-1].lower()
        tests = " ".join(c.test_names).lower()

        # Handler-based boost — highest priority
        h = handler.lower()
        for h_kw, s_kw in HANDLER_SPEC_MAP.items():
            if h_kw in h and s_kw in spec:
                s += 500

        # Scenario-specific boosts based on trigger body
        if body.get("confirm") is True:
            if any(kw in spec for kw in ("nothree", "no3ds", "autocapture", "manualcapture")):
                s += 200
            if "confirm" in tests:
                s += 50
        if body.get("capture_method") == "automatic":
            if "auto" in spec: s += 150
        if body.get("capture_method") == "manual":
            if "manual" in spec: s += 150
        if body.get("amount_to_capture"):
            if "overcapture" in spec: s += 400
            elif "capture" in spec: s += 200
        if "payment_method_data" in body:
            if "card" in spec or "three" in spec or "nothree" in spec: s += 50
        if body.get("customer_id"):
            if any(kw in spec for kw in ("savecard", "customer", "mandate")): s += 100

        # Penalise unrelated spec types for generic payment flows
        if not body.get("customer_id") and not body.get("mandate_id"):
            if "incremental" not in h:  # don't penalise if we're looking for incremental
                if any(kw in spec for kw in ("mandate", "savecard", "zeroauth", "wallet",
                                              "banktransfer", "bankredirect", "upi", "crypto",
                                              "reward", "realtime", "variations")): s -= 300

        # Prefer specs with more test coverage
        s += len(c.test_names)
        return s

    return max(candidates, key=score)



def _flow_to_list_key(result) -> str:
    """Map a flow's trigger path to the CONNECTOR_LISTS key in Utils.js."""
    path = result.trigger_paths[0] if result.trigger_paths else ""
    if "incremental_authorization" in path: return "INCREMENTAL_AUTH"
    if "capture" in path:                   return "OVERCAPTURE"
    if "cancel" in path:                    return "VOID"
    # Fallback: derive from handler name
    handler = result.handlers[0].upper() if result.handlers else ""
    return handler


def _last_flow_in_connector(repo: str, connector_rel: str) -> str:
    """Find the last PascalCase flow key in a Connector.js to use as insert_after."""
    import re
    try:
        js = (Path(repo) / connector_rel).read_text(encoding="utf-8", errors="replace")
        matches = re.findall(r'\b([A-Z][A-Za-z0-9]+)\s*:\s*\{', js)
        skip = {"Request","Response","Configs","ResponseCustom"}
        flows = [m for m in matches if m not in skip]
        return flows[-1] if flows else "PaymentIntent"
    except Exception:
        return "PaymentIntent"

def run_flow_pipeline(
    flow:       dict,
    repo:       str  = REPO,
    dry_run:    bool = False,
    skip_run:   bool = False,
    run_only:   bool = False,
    verbose:    bool = False,
):
    # Connector comes from the flow JSON — not from user input
    connector = flow.get("connector", "")
    changed   = flow.get("changed_function", "")
    n_affected = flow.get("connector_count", 0)
    # Validate repo
    repo_path = Path(repo).resolve()
    if not (repo_path / "cypress").exists():
        print(f"\n❌ '{repo_path}' is not a cypress-tests repo root.")
        print(f"   Set CYPRESS_REPO or pass --repo")
        return

    repo = str(repo_path)
    flow_id = flow.get("flow_id", "?")

    sep(f"Flow pipeline: flow_id={flow_id}")
    print(f"  Description     : {flow.get('description', '')[:80]}")
    if changed:
        print(f"  Changed function: {changed}")
    if connector:
        print(f"  Connector       : {connector}")
    if n_affected:
        print(f"  Connectors affected: {n_affected} (run for each or at minimum for {connector})")

    q = FlowQueryEngine(NEO4J_URI, (NEO4J_USER, NEO4J_PASS))

    try:
        # ── STEP 1: Query ─────────────────────────────────────────────────────
        sep("STEP 1 — Query: is this flow covered?")
        result = q.check_coverage(flow, connector=connector)
        print(f"  Status        : {result.status}")
        print(f"  Trigger paths : {result.trigger_paths}")
        print(f"  Setup paths   : {result.setup_paths}")
        print(f"  Handlers      : {result.handlers}")

        n = len(result.candidates)
        if n:
            best = _pick_best_candidate(result.candidates, flow)
            print(f"  Candidates    : {n} existing spec(s)")
            print(f"  Best match    : {best.spec_file} ({len(best.test_names)} tests)")

        # COVERED or NEEDS_LLM_CHECK with candidates:
        # For CONNECTOR_ONLY flows, first verify the connector has:
        #   A) a config block in Connector.js  (MISSING_CONFIG → codegen)
        #   B) an entry in the allowlist        (NOT_IN_ALLOWLIST → codegen)
        # Only then run the spec.
        if result.status in (CoverageStatus.COVERED, CoverageStatus.NEEDS_LLM_CHECK) and result.candidates:
            best = _pick_best_candidate(result.candidates, flow)

            # For CONNECTOR_ONLY: check connector-level config before running
            if connector and result.flow_type == FlowType.CONNECTOR_ONLY:
                flow_name = q._derive_flow_name(result.trigger_paths, connector)
                if not flow_name:
                    # Derive from trigger path directly
                    tp = result.trigger_paths[0] if result.trigger_paths else ""
                    if "incremental_authorization" in tp: flow_name = "IncrementalAuth"
                    elif "capture" in tp:                 flow_name = "Capture"
                    elif "cancel" in tp:                  flow_name = "VoidAfterConfirm"
                    elif "refund" in tp:                  flow_name = "Refund"

                if flow_name:
                    cc_result = q.check_connector_flow(connector, flow_name)
                    result.connector_check = cc_result.connector_check
                    if cc_result.status not in (CoverageStatus.COVERED, CoverageStatus.NEEDS_LLM_CHECK):
                        print(f"  ⚠️  Spec exists but {connector} config is incomplete: {cc_result.status}")
                        print(f"  → Need to patch config before running spec")
                        result.status = cc_result.status
                        result.what_to_fix = cc_result.what_to_fix
                        # Fall through to codegen steps below
                    else:
                        print(f"  ✅ {connector} config OK — running spec for regression")
                        if dry_run:
                            print(f"  (dry-run) would run: {best.spec_file} --env CONNECTOR={connector.lower()}")
                            return
                        if not skip_run:
                            _run_cypress_spec(best.spec_file, connector, repo, verbose)
                        return
                else:
                    print(f"  ✅ Existing spec covers these endpoints — run for regression")
                    if dry_run:
                        print(f"  (dry-run) would run: {best.spec_file} --env CONNECTOR={connector.lower()}")
                        return
                    if not skip_run:
                        _run_cypress_spec(best.spec_file, connector, repo, verbose)
                    return
            else:
                print(f"  ✅ Existing spec covers these endpoints — run for regression")
                if dry_run:
                    print(f"  (dry-run) would run: {best.spec_file} --env CONNECTOR={connector.lower()}")
                    return
                if not skip_run:
                    _run_cypress_spec(best.spec_file, connector, repo, verbose)
                return

        if run_only:
            if result.candidates:
                best = _pick_best_candidate(result.candidates, flow)
                _run_cypress_spec(best.spec_file, connector, repo, verbose)
            else:
                print("  ❌ --run-only but no spec found")
            return

        from flow_context import ContextBundle, Status
        from flow_query import CoverageStatus as CS
        from indexer import Indexer

        # Route by connector_check status — three different strategies:
        #   MISSING_CONFIG   → insert_flow_block  (add block to Connector.js + Utils.js)
        #   NOT_IN_ALLOWLIST → allowlist_only      (add connector to Utils.js list only)
        #   MISSING          → full_file_rewrite   (generate brand new spec file)
        cc_status = (result.connector_check.status
                     if result.connector_check else result.status)

        idx = Indexer(repo, NEO4J_URI, (NEO4J_USER, NEO4J_PASS))
        cg  = CodeGen(repo, indexer=idx, model=GRID_MODEL,
                      api_key=GRID_KEY, base_url=GRID_URL)

        # ── Strategy A: NOT_IN_ALLOWLIST ──────────────────────────────────────
        if cc_status == CoverageStatus.NOT_IN_ALLOWLIST:
            cc        = result.connector_check
            utils_rel = "cypress/e2e/configs/Payment/Utils.js"
            list_key  = (cc.allowlist_key if cc and cc.allowlist_key
                         else _flow_to_list_key(result))
            sep("STEP 2 — allowlist_only: add to Utils.js")
            print(f"  Adding '{connector.lower()}' to CONNECTOR_LISTS.{list_key}")
            if dry_run:
                print(f"  (dry-run) would patch: {utils_rel}")
                idx.close(); return
            ctx_bundle = ContextBundle(
                connector=connector, flow=str(flow_id),
                status=Status.NOT_IN_ALLOWLIST,
                prompt="", system_prompt="",
                files_to_edit=[utils_rel],
                patch_meta={
                    "type":           "allowlist_only",
                    "skip_llm":       True,
                    "allowlist_file": utils_rel,
                    "allowlist_key":  list_key,
                    "connector_name": connector.lower(),
                },
            )
            patch = cg.apply(ctx_bundle)
            print(f"  {patch.summary()}")
            idx.close()
            if patch.success and not skip_run:
                best = _pick_best_candidate(result.candidates, flow) if result.candidates else None
                if best:
                    sep("STEP 3 — Cypress run")
                    _run_cypress_spec(best.spec_file, connector, repo, verbose)
            return

        # ── Strategy B: MISSING_CONFIG ────────────────────────────────────────
        if cc_status == CoverageStatus.MISSING_CONFIG:
            cc            = result.connector_check
            connector_rel = f"cypress/e2e/configs/Payment/{connector}.js"
            utils_rel     = "cypress/e2e/configs/Payment/Utils.js"
            list_key      = _flow_to_list_key(result)
            flow_name     = cc.flow_name if cc else "UnknownFlow"
            insert_after  = _last_flow_in_connector(repo, connector_rel)

            sep("STEP 2 — Build context (insert_flow_block)")

            # Read connector's existing blocks as style reference
            connector_js = (Path(repo) / connector_rel).read_text(encoding="utf-8", errors="replace")

            # Build a focused prompt — NOT a spec prompt.
            # The LLM must output ONLY a <new_flow_block> tag.
            system_prompt = (
                "You are a senior engineer working on the Hyperswitch payment platform cypress test suite. "
                "You generate minimal, precise JavaScript config additions for connector files. "
                "Output ONLY the requested XML tag — no explanation, no markdown, no other text."
            )

            ref_block_section = ""
            if cc and cc.reference_block:
                ref_block_section = (
                    f"\n\n## Reference block (from {cc.reference_connector})"
                    f"\nThis connector already has {flow_name}. Use it as the content template:\n"
                    f"```javascript\n{cc.reference_block[:2000]}\n```"
                )

            # Extract a few existing blocks from this connector as style reference
            import re as _re
            existing_blocks = _re.findall(
                r'([A-Z][A-Za-z0-9]+)\s*:\s*\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}',
                connector_js
            )[:3]
            style_example = "\n".join(existing_blocks) if existing_blocks else ""

            prompt = f"""You must add a new flow config block to {connector}.js for the {flow_name} flow.

## What to generate
A single JavaScript object block for `{flow_name}` inside `connectorDetails.card_pm`.
Insert it after the `{insert_after}` block.

## Required output format
Wrap the block in exactly this tag — nothing else:
<new_flow_block>
{flow_name}: {{
  Request: {{
    // fields needed to trigger this flow
  }},
  Response: {{
    status: 200,
    body: {{
      status: "...", // expected payment status
    }},
  }},
}},
</new_flow_block>

## {connector} style (copy this formatting exactly)
The block must match the style of existing blocks in {connector}.js:
```javascript
{style_example}
```
{ref_block_section}

## Rules
- Output ONLY the <new_flow_block>...</new_flow_block> tag
- No imports, no describe(), no it() blocks — this is a config block not a spec
- No markdown outside the tag
- Match the Request/Response structure of the reference block above
- Use {connector}-appropriate card details and expected statuses
"""

            print(f"  Target        : {connector_rel}")
            print(f"  Flow to add   : {flow_name}")
            print(f"  Insert after  : {insert_after}")
            print(f"  List key      : {list_key}")
            print(f"  Reference from: {cc.reference_connector if cc else 'none'}")
            print(f"  Prompt tokens : ~{len(prompt)//4:,}")
            if verbose: sep("Prompt"); print(prompt)
            if dry_run:
                print("\n  (dry-run — no files written)")
                idx.close(); return

            sep("STEP 3 — Codegen: insert_flow_block")
            ctx_bundle = ContextBundle(
                connector=connector, flow=str(flow_id),
                status=Status.MISSING_CONFIG,
                prompt=prompt, system_prompt=system_prompt,
                files_to_edit=[connector_rel, utils_rel],
                patch_meta={
                    "type":           "insert_flow_block",
                    "target_file":    connector_rel,
                    "insert_after":   insert_after,
                    "allowlist_file": utils_rel,
                    "allowlist_key":  list_key,
                    "connector_name": connector.lower(),
                },
            )
            patch = cg.apply(ctx_bundle)
            print(f"  {patch.summary()}")
            idx.close()
            if not patch.success:
                print(f"\n❌ Codegen failed: {patch.error}")
                return
            if verbose and patch.llm_response:
                sep("LLM response"); print(patch.llm_response)
            if not skip_run:
                # result.candidates is empty (check_coverage returned early for MISSING_CONFIG)
                # Re-query now that files are patched and reindexed
                candidates = q._find_covering_specs(result.trigger_paths)
                best = _pick_best_candidate(candidates, flow) if candidates else None
                if best:
                    sep("STEP 4 — Cypress run")
                    _run_cypress_spec(best.spec_file, connector, repo, verbose)
                else:
                    print("  ⚠️  No spec found to run after patching — reindex and retry")
            return

        # ── Strategy C: MISSING — generate new spec file ─────────────────────
        sep("STEP 2 — Build context (full_file_rewrite)")
        bundle = build_flow_context(result, repo, q)
        print(f"  Spec target   : {bundle.spec_to_create}")
        print(f"  Prompt tokens : ~{len(bundle.prompt) // 4:,}")
        if verbose: sep("Prompt"); print(bundle.prompt)
        if dry_run:
            print("\n  (dry-run — no files written, no LLM called)")
            idx.close(); return

        sep("STEP 3 — Codegen: generate spec file")
        ctx_bundle = ContextBundle(
            connector     = f"flow_{flow_id}",
            flow          = bundle.description,
            status        = Status.MISSING_TEST,
            prompt        = bundle.prompt,
            system_prompt = bundle.system_prompt,
            files_to_edit = bundle.files_to_edit,
            patch_meta    = bundle.patch_meta,
        )
        patch = cg.apply(ctx_bundle)
        print(f"  {patch.summary()}")
        idx.close()

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
            _run_cypress(spec_file, repo, verbose)

    except Exception as err:
        # Keep pipeline logs machine-readable and avoid hard crashes when
        # infrastructure dependencies (Neo4j, Grid LLM, etc.) are missing or misconfigured.
        print(f"\n❌ Flow pipeline failed before Cypress execution: {err}")
        hint = "Fix the issue above and re-run; continuing without Cypress."
        es = str(err)
        if "GRID_API_KEY" in es or "GLM_API_KEY" in es:
            hint = "Set GRID_API_KEY (and optionally GRID_BASE_URL, GRID_MODEL), then re-run; continuing without Cypress."
        elif "7687" in es or "Neo4j" in es or "bolt://" in es:
            hint = "Ensure Neo4j is running and NEO4J_URI / NEO4J_PASSWORD match; continuing without Cypress."
        print(f"   {hint}")
        return
    finally:
        q.close()


def _run_cypress_spec(spec_file: str, connector: str, repo: str, verbose: bool = False):
    """Run a specific spec file for a given connector, with setup specs first."""

    # Verify the spec file actually exists on disk
    full_path = Path(repo) / spec_file
    if not full_path.exists():
        # Try finding by spec name prefix (e.g. "00028" matches any file starting with that)
        spec_name = Path(spec_file).stem          # "00028-IncrementalAuth"
        spec_num  = spec_name.split("-")[0]       # "00028"
        spec_dir  = Path(repo) / Path(spec_file).parent
        candidates = list(spec_dir.glob(f"{spec_num}-*.cy.js")) if spec_dir.exists() else []
        if candidates:
            full_path = candidates[0]
            spec_file = str(full_path.relative_to(Path(repo)))
            print(f"  ℹ️  Resolved spec to: {spec_file}")
        else:
            print(f"  ❌ Spec file not found: {full_path}")
            print(f"     Files in {spec_dir}:")
            if spec_dir.exists():
                for f in sorted(spec_dir.iterdir())[:10]:
                    print(f"       {f.name}")
            print(f"     Run: git pull in your cypress-tests repo")
            return

    runner = CypressRunner(
        repo_root    = repo,
        base_url     = CY_BASE_URL,
        auth_file    = CY_AUTH_FILE,
        profile_id   = CY_PROFILE,
        connector_id = CY_CONN_ID,
    )
    c = connector.lower() if connector else "service"
    print(f"  Running: {spec_file} --env CONNECTOR={c}")

    # Payment specs need 00001-AccountCreate, 00002-CustomerCreate, 00003-ConnectorCreate
    # to run first so globalState has connectorId, customerId etc.
    is_payment_spec = "spec/Payment/" in spec_file
    result = runner.run(c, flow="", spec_file=spec_file, setup=is_payment_spec)
    _print_run_result(result, verbose)


def _run_cypress(spec_file: str, repo: str, verbose: bool = False):
    """Run a spec file without a specific connector (service-level)."""
    _run_cypress_spec(spec_file, "service", repo, verbose)


def _print_run_result(result, verbose: bool = False):
    print(f"\n  {result.summary()}")
    if result.error:
        print(f"  Runner error: {result.error}")
        return
    if result.passed:
        print(f"  ✅ {result.passing}/{result.total} tests passed in {result.duration_ms}ms")
    else:
        print(f"\n  Failed tests:")
        for ft in result.failed_tests:
            print(f"    ✗ {ft.name}")
            print(f"      {ft.error}")
    if verbose:
        sep("Raw output")
        print(result.raw_stdout)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Run the full pipeline for a flow_id JSON"
    )
    ap.add_argument("--flow",      required=True,
                    help="Flow JSON: file path, '-' for stdin, or inline JSON string")
    ap.add_argument("--repo",      default=os.environ.get("CYPRESS_REPO", "."),
                    help="Path to cypress-tests root (or set CYPRESS_REPO env var)")
    ap.add_argument("--dry-run",   action="store_true",
                    help="Show what would happen without writing files or running cypress")
    ap.add_argument("--run-only",  action="store_true",
                    help="Skip codegen, just run the cypress spec if it exists")
    ap.add_argument("--skip-run",  action="store_true",
                    help="Generate the spec but skip the cypress run")
    ap.add_argument("--verbose",   action="store_true",
                    help="Print the full LLM prompt and cypress output")
    args = ap.parse_args()

    print("Config:")
    print(f"  Repo       : {args.repo}")
    print(f"  Neo4j      : {NEO4J_URI}")
    print(f"  Grid URL   : {GRID_URL or '(GRID_BASE_URL not set)'}")
    print(f"  Grid model : {GRID_MODEL}")
    print(f"  Cypress URL: {CY_BASE_URL}")

    try:
        data, flows = load_flow(args.flow)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"❌ Could not load flow: {e}")
        sys.exit(1)

    if len(flows) > 1:
        print(f"Document contains {len(flows)} flows.")
        changed = data.get("changed_function", "")
        if changed:
            print(f"Changed function: {changed}")
        print()

    for flow in flows:
        run_flow_pipeline(
            flow     = flow,
            repo     = args.repo,
            dry_run  = args.dry_run,
            skip_run = args.skip_run,
            run_only = args.run_only,
            verbose  = args.verbose,
        )
        if len(flows) > 1:
            print()  # spacing between flows