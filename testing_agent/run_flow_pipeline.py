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

def _config_blocks_matching_fields(required_fields: set, connector: str, repo: str) -> set:
    """
    Scan cypress/e2e/configs/Payment/{connector}.js for flow block names whose
    Request object contains ALL of the required fields.

    This is the general pattern-match: we derive what fields a flow needs from
    input.json prerequisites, then find config blocks already implementing them.
    """
    if not required_fields or not connector:
        return set()
    config_path = Path(repo) / f"cypress/e2e/configs/Payment/{connector}.js"
    if not config_path.exists():
        return set()
    import re as _re
    content = config_path.read_text(encoding="utf-8", errors="replace")
    # Match: BlockName: { ... Request: { ... } ... }
    # Capture block name + everything up to the closing brace of Request
    block_re = _re.compile(
        r'(\w+)\s*:\s*\{[^}]*?Request\s*:\s*\{([^}]*(?:\{[^}]*\}[^}]*)*?)\}',
        _re.DOTALL,
    )
    matching = set()
    for m in block_re.finditer(content):
        block_name   = m.group(1)
        request_body = m.group(2)
        if all(field in request_body for field in required_fields):
            matching.add(block_name)
    return matching


def _specs_referencing_blocks(block_names: set, candidates, repo: str):
    """
    Return the subset of candidates whose spec file references any of block_names.
    Preserves original candidate order so caller can re-sort.
    """
    if not block_names:
        return []
    matched = []
    for c in candidates:
        try:
            content = (Path(repo) / c.spec_file).read_text(encoding="utf-8", errors="replace")
            if any(b in content for b in block_names):
                matched.append(c)
        except OSError:
            pass
    return matched


def _pick_best_candidate(candidates, flow, connector: str = "", repo: str = ""):
    """
    Pick the most relevant existing spec for a regression run.

    Strategy (in priority order):
    1. Pattern match — extract required fields from flow prerequisites, find config
       blocks that implement those fields, boost specs that reference those blocks.
       This is data-driven from input.json; no flow-type-specific hardcoding.
    2. Handler keyword match — handler name signals the scenario type.
    3. Trigger body signals — capture_method, confirm, etc.
    4. Generic penalty — penalise unrelated spec types only when pattern match
       found no signal (i.e. we have no idea what the flow needs).
    """
    if not candidates: return None

    # Get trigger body signals
    body = {}
    for cases in flow.get("trigger_payloads", {}).values():
        for case in cases.values():
            body = case.get("body", {})
            break
        break

    handler = (flow.get("endpoints", [{}])[0].get("handler", "")
               or next(iter(flow.get("trigger_payloads", {})), ""))

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

    # ── Pattern match (general, driven by input.json prerequisites) ──────────
    # Only use fields that are actual API request body keys — not Rust expressions.
    # A valid JS field name contains no dots, spaces, parens, or angle brackets.
    import re as _re_field
    _is_api_field = lambda f: bool(f) and not _re_field.search(r'[\s.()\[\]<>]', f)
    required_fields = {
        p["field"] for p in flow.get("prerequisites", [])
        if p.get("field") and _is_api_field(p["field"])
    }

    matching_blocks = _config_blocks_matching_fields(required_fields, connector, repo)
    pattern_matched_specs = {
        c.spec_file for c in _specs_referencing_blocks(matching_blocks, candidates, repo)
    }
    has_pattern_signal = bool(pattern_matched_specs)

    def score(c):
        s = 0
        spec  = c.spec_file.split("/")[-1].lower()
        tests = " ".join(c.test_names).lower()
        h     = handler.lower()

        # Priority 1: config block pattern match — strongest signal
        if c.spec_file in pattern_matched_specs:
            s += 600

        # Priority 2: handler keyword match
        for h_kw, s_kw in HANDLER_SPEC_MAP.items():
            if h_kw in h and s_kw in spec:
                s += 500

        # Priority 3: trigger body signals
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

        # Priority 4: penalise unrelated specs — only when pattern match gave no signal
        if not has_pattern_signal and not body.get("customer_id") and not body.get("mandate_id"):
            if "incremental" not in h:
                if any(kw in spec for kw in ("mandate", "savecard", "zeroauth", "wallet",
                                              "banktransfer", "bankredirect", "upi", "crypto",
                                              "reward", "realtime", "variations")): s -= 300

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
            best = _pick_best_candidate(result.candidates, flow, connector, repo)
            print(f"  Candidates    : {n} existing spec(s)")
            print(f"  Best match    : {best.spec_file} ({len(best.test_names)} tests)")

        # COVERED: spec confirmed by graph — run regression directly.
        # NEEDS_LLM_CHECK: graph found specs covering the same endpoints but
        #   cannot confirm the specific guards/conditions are exercised.
        #   Ask the LLM to verify before deciding whether to run regression
        #   or fall through to new test generation.
        if result.status == CoverageStatus.NEEDS_LLM_CHECK and result.candidates:
            best = _pick_best_candidate(result.candidates, flow, connector, repo)
            sep("STEP 1b — LLM: verify existing spec covers this flow variant")
            try:
                from codegen import GridClient
                llm = GridClient(model=GRID_MODEL, api_key=GRID_KEY, base_url=GRID_URL)
                spec_path = Path(repo) / best.spec_file
                spec_content = spec_path.read_text(encoding="utf-8", errors="replace")[:6000]
                guards = flow.get("prerequisites", [])
                conditions = []
                for g in guards:
                    reason = g.get("reason", "")
                    field  = g.get("field", "")
                    if reason:
                        conditions.append(f"{field}: {reason}" if field else reason)
                    elif field:
                        conditions.append(f"field '{field}' must be present/valid")
                # Also pull guard text from chain hops
                for hop in flow.get("chain", []):
                    cond = hop.get("condition", {})
                    if isinstance(cond, dict) and cond.get("type") == "match":
                        conditions.append(f"match guard: {cond.get('text', '')}")
                conditions_text = "\n".join(f"  - {c}" for c in conditions) if conditions else f"  - {flow.get('description', '(see description)')}"
                desc = flow.get("description", "")

                # Build a summary of all candidates so the LLM can identify
                # if any other spec already covers the required conditions
                other_candidates_text = ""
                others = [c for c in result.candidates if c.spec_file != best.spec_file][:10]
                if others:
                    lines = []
                    for c in others:
                        spec_name = c.spec_file.split("/")[-1]
                        lines.append(f"  - {spec_name}: {', '.join(c.test_names[:4])}")
                    other_candidates_text = "\n## Other candidate specs (by name + test names)\n" + "\n".join(lines)

                verify_prompt = f"""You are reviewing Cypress test specs to determine if any already cover a specific payment flow variant.

## Flow variant to cover
Description: {desc}
Changed function: {flow.get('changed_function', '')}

## Required conditions that must be exercised
{conditions_text}

## Best candidate spec: {best.spec_file.split('/')[-1]} (first 6000 chars)
```javascript
{spec_content}
```
{other_candidates_text}

Does the best candidate spec (or any of the other candidates listed above) exercise ALL of the required conditions?
Answer on the first line: YES <spec_filename> or NO.
On the next line, briefly explain why (1-2 sentences).
"""
                answer = llm.chat(
                    system="You are a senior test engineer. Answer concisely.",
                    user=verify_prompt,
                    max_tokens=200,
                )
                first_line = answer.strip().splitlines()[0].strip().upper()
                print(f"  LLM verdict   : {answer.strip().splitlines()[0].strip()}")
                if len(answer.strip().splitlines()) > 1:
                    print(f"  Reason        : {answer.strip().splitlines()[1]}")

                # Check if LLM identified a different spec than the best candidate
                if first_line.startswith("YES"):
                    # Try to extract a specific filename from the answer
                    import re as _re2
                    fname_match = _re2.search(r'(\d{5}-[\w]+\.cy\.js)', answer, _re2.IGNORECASE)
                    if fname_match:
                        identified = fname_match.group(1)
                        alt = next((c for c in result.candidates
                                    if identified.lower() in c.spec_file.lower()), None)
                        if alt and alt.spec_file != best.spec_file:
                            print(f"  ↳ LLM identified better match: {alt.spec_file}")
                            best = alt
                            result.candidates = [alt] + [c for c in result.candidates if c != alt]
                    print(f"  ✅ LLM confirmed spec covers this variant — running for regression")
                    result.status = CoverageStatus.COVERED
                else:
                    print(f"  ⚠️  LLM says no existing spec covers this variant — generating new test")
                    result.status = CoverageStatus.MISSING
                    result.candidates = []
                    # Fall through to Strategy C below
            except Exception as llm_err:
                print(f"  ⚠️  LLM check failed ({llm_err}) — defaulting to regression")
                result.status = CoverageStatus.COVERED

        if result.status == CoverageStatus.COVERED and result.candidates:
            best = _pick_best_candidate(result.candidates, flow, connector, repo)

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
                            _run_cypress_spec(best.spec_file, connector, repo, verbose, flow)
                        return
                else:
                    print(f"  ✅ Existing spec covers these endpoints — run for regression")
                    if dry_run:
                        print(f"  (dry-run) would run: {best.spec_file} --env CONNECTOR={connector.lower()}")
                        return
                    if not skip_run:
                        _run_cypress_spec(best.spec_file, connector, repo, verbose, flow)
                    return
            else:
                print(f"  ✅ Existing spec covers these endpoints — run for regression")
                if dry_run:
                    print(f"  (dry-run) would run: {best.spec_file} --env CONNECTOR={connector.lower()}")
                    return
                if not skip_run:
                    _run_cypress_spec(best.spec_file, connector, repo, verbose, flow)
                return

        if run_only:
            if result.candidates:
                best = _pick_best_candidate(result.candidates, flow, connector, repo)
                _run_cypress_spec(best.spec_file, connector, repo, verbose, flow)
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
                best = _pick_best_candidate(result.candidates, flow, connector, repo) if result.candidates else None
                if best:
                    sep("STEP 3 — Cypress run")
                    _run_cypress_spec(best.spec_file, connector, repo, verbose, flow)
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
                best = _pick_best_candidate(candidates, flow, connector, repo) if candidates else None
                if best:
                    sep("STEP 4 — Cypress run")
                    _run_cypress_spec(best.spec_file, connector, repo, verbose, flow)
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


def _derive_setup_specs(flow: dict, repo: str) -> list[str]:
    """
    Derive which setup specs must run before the test spec, based on what
    the flow actually needs (from llm_spec.setup_payloads endpoints).

    This replaces the hardcoded "always run Account+Customer+Connector create"
    approach. Each setup spec is only included when the flow's prerequisites
    actually require it.

    Endpoint → setup spec mapping:
      /accounts or /account/*          → 00001-AccountCreate
      /customers                       → 00002-CustomerCreate
      /account/*/connectors            → 00003-ConnectorCreate
      /user/signup or /user/signin     → (user login — no separate setup spec needed,
                                          handled by the spec's beforeEach)
      (anything else / empty)          → no setup
    """
    ENDPOINT_TO_SETUP = {
        "/accounts":   "cypress/e2e/spec/Payment/00001-AccountCreate.cy.js",
        "/account/":   "cypress/e2e/spec/Payment/00001-AccountCreate.cy.js",
        "/customers":  "cypress/e2e/spec/Payment/00002-CustomerCreate.cy.js",
        "/connectors": "cypress/e2e/spec/Payment/00003-ConnectorCreate.cy.js",
    }

    setup_payloads = flow.get("llm_spec", {}).get("setup_payloads", [])
    if not setup_payloads:
        # No llm_spec setup_payloads — fall back to checking top-level setup_payloads
        setup_payloads = flow.get("setup_payloads", [])

    needed = []
    seen   = set()
    for step in setup_payloads:
        endpoint = step.get("endpoint", "") or step.get("url", "")
        for pattern, spec in ENDPOINT_TO_SETUP.items():
            if pattern in endpoint and spec not in seen:
                # Verify the setup spec actually exists before adding it
                if (Path(repo) / spec).exists():
                    needed.append(spec)
                    seen.add(spec)
                break

    return needed


def _run_cypress_spec(spec_file: str, connector: str, repo: str, verbose: bool = False,
                      flow: dict = None):
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

    # Derive setup specs from the flow's actual prerequisites.
    # Only the specs the flow truly needs are included — no hardcoded list.
    setup_specs = _derive_setup_specs(flow, repo) if flow else []
    result = runner.run(c, flow="", spec_file=spec_file, setup_specs=setup_specs)
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