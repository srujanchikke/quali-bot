"""
filter_false_positives.py
=========================
Uses the Juspay Grid API (open-large) to remove false-positive endpoints from
find_impact.py output BEFORE flow enrichment.

For each changed function it:
  1. Reads the function's source code
  2. Reads the source of its 2 immediate callers in the chain
  3. Sends all endpoints in one batch to open-large
  4. Classifies each as TRUE_POSITIVE / FALSE_POSITIVE with a reason
  5. Drops false positives and writes the filtered JSON

Usage:
  JUSPAY_API_KEY=sk-... python filter_false_positives.py \\
      --input testing_agent/input.json \\
      --src-root /path/to/hyperswitch \\
      --out testing_agent/input_filtered.json

Env:
  JUSPAY_API_KEY   your Juspay Grid API key (required)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import textwrap
import urllib.request
import urllib.error
from pathlib import Path

from hs_indexer.config import cfg

GRID_BASE_URL          = cfg.llm.grid_api_url
GRID_MODEL             = cfg.llm.models.grid
GRID_FALLBACK_MODEL    = cfg.llm.models.grid_fallback
MAX_SOURCE_LINES  = cfg.filter.max_source_lines
MAX_CHAIN_NODES   = cfg.filter.max_chain_nodes
MAX_HANDLER_LINES = cfg.filter.max_handler_lines
MAX_HANDLERS      = cfg.filter.max_handlers
BATCH_SIZE        = cfg.filter.batch_size


# ── Source helpers ──────────────────────────────────────────────────────────────

def _read_lines(path: Path, start: int, count: int) -> str:
    """Read `count` lines from `path` starting at 1-based `start`."""
    try:
        lines = path.read_text(errors="replace").splitlines()
        chunk = lines[max(0, start - 1): start - 1 + count]
        return "\n".join(chunk)
    except Exception:
        return ""


def _find_fn_in_file(fn_name: str, file_path: Path) -> int | None:
    """Return 1-based line number of `fn {fn_name}` in file, or None."""
    try:
        for i, line in enumerate(file_path.read_text(errors="replace").splitlines(), 1):
            if re.search(rf"\bfn\s+{re.escape(fn_name)}\b", line):
                return i
    except Exception:
        pass
    return None


def _grep_fn(fn_name: str, src_root: Path) -> tuple[Path, int] | None:
    """Grep the source tree for the first definition of `fn fn_name`."""
    try:
        result = subprocess.run(
            ["grep", "-rn", "--include=*.rs", "-m", "1",
             f"fn {fn_name}", str(src_root / "crates")],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            first = result.stdout.strip().splitlines()[0]
            file_str, line_str, *_ = first.split(":", 2)
            return Path(file_str), int(line_str)
    except Exception:
        pass
    return None


def get_source_for_node(node_name: str, src_root: Path,
                        known_file: str | None = None,
                        known_line: int | None = None) -> str:
    """Return source snippet for a call chain node (plain function name or Struct#Trait#method)."""
    # Extract bare function name from Struct#Trait#method
    parts = node_name.split("#")
    fn_name = parts[-1]

    file_path: Path | None = None
    line: int | None = None

    if known_file and known_line:
        fp = src_root / known_file
        if fp.exists():
            file_path, line = fp, known_line
        else:
            file_path, line = src_root / known_file.lstrip("/"), known_line

    if file_path is None or not file_path.exists():
        found = _grep_fn(fn_name, src_root)
        if found:
            file_path, line = found

    if file_path and line:
        snippet = _read_lines(file_path, line, MAX_SOURCE_LINES)
        return f"// {file_path.name}:{line}\n{snippet}"

    return f"// source not found for {node_name}"


def get_extra_context(caller_sources: list[tuple[str, str]], src_root: Path) -> str:
    """
    For each caller, find function calls that look like boolean guards or
    Option-returning helpers and pull in their source so the model can see
    the full data-flow, not just the guard check site.
    """
    # Match patterns like `should_*`, `is_*`, `has_*`, `can_*`, `perform_*`
    # that typically gate whether the changed function is actually called.
    guard_fn_re = re.compile(
        r'\b(should_\w+|is_\w+|has_\w+|can_\w+|perform_\w+|check_\w+|get_\w+_for_\w+)\b'
    )
    extra: list[str] = []
    seen: set[str] = set()
    for _, src in caller_sources:
        for fn in guard_fn_re.findall(src):
            if fn in seen:
                continue
            seen.add(fn)
            found = _grep_fn(fn, src_root)
            if found:
                fpath, fline = found
                snippet = _read_lines(fpath, fline, MAX_SOURCE_LINES)
                extra.append(f"// {fpath.name}:{fline}  [{fn}]\n{snippet}")
            if len(extra) >= 4:   # cap to avoid huge prompts
                break
        if len(extra) >= 4:
            break
    return "\n\n".join(extra)


# ── JSON extraction ─────────────────────────────────────────────────────────────

def _extract_json(text: str) -> str:
    """
    Robustly extract the first JSON object from model output.
    Handles markdown fences, leading prose, and models that wrap JSON in text.
    """
    # Strip markdown code fences
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text.strip())
    text = text.strip()

    if text.startswith("{"):
        return text

    # Find the first '{' and attempt to extract a balanced JSON object
    start = text.find("{")
    if start != -1:
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start: i + 1]

    # Last resort: regex for {"results": [...]}
    match = re.search(r'\{.*?"results"\s*:\s*\[.*?\]\s*\}', text, re.DOTALL)
    if match:
        return match.group(0)

    return text  # return as-is and let json.loads raise a useful error


# ── Grid API call ───────────────────────────────────────────────────────────────

def _call_grid_model(prompt: str, model: str) -> str:
    """Call a single Grid model. Raises RuntimeError on any HTTP error."""
    api_key = os.environ.get("JUSPAY_API_KEY", "")
    if not api_key:
        raise RuntimeError("JUSPAY_API_KEY environment variable is not set.")

    # response_format: json_object is only supported by OpenAI-compatible Claude/GPT
    # models. Free Grid models like kimi-latest ignore or reject it, returning empty
    # responses. Omit it for non-Claude models and rely on the prompt instruction.
    _supports_json_format = any(x in model.lower() for x in ("claude", "gpt-4", "gpt-3"))
    body: dict = {
        "model":       model,
        "max_tokens":  4096,
        "messages":    [{"role": "user", "content": prompt}],
        "temperature": 0.1,
    }
    if _supports_json_format:
        body["response_format"] = {"type": "json_object"}

    payload = json.dumps(body).encode()

    req = urllib.request.Request(
        f"{GRID_BASE_URL}/v1/chat/completions",
        data=payload,
        headers={
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read())
        text = data["choices"][0]["message"]["content"].strip()
        if not text:
            raise RuntimeError(f"Model {model!r} returned empty content (may not support JSON mode)")
        text = _extract_json(text)
        return text
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"Grid API error {e.code}: {body}") from e


def _is_budget_error(err: RuntimeError) -> bool:
    return "budget_exceeded" in str(err) or "daily limit" in str(err).lower()


def _call_grid(prompt: str, model: str | None = None) -> str:
    """
    Call the Grid API.  Falls back to GRID_FALLBACK_MODEL automatically when
    the primary model returns a budget-exceeded error.

    Pass ``model`` explicitly to override both primary and fallback for a
    single call (e.g. when the user sets --model on the CLI).
    """
    primary  = model or GRID_MODEL
    fallback = GRID_FALLBACK_MODEL

    try:
        return _call_grid_model(prompt, primary)
    except RuntimeError as exc:
        if fallback and fallback != primary and _is_budget_error(exc):
            print(
                f"  [filter] Primary model ({primary}) hit budget limit — "
                f"retrying with fallback model ({fallback}) …",
                file=sys.stderr,
            )
            return _call_grid_model(prompt, fallback)
        raise


# ── Prompt builder ──────────────────────────────────────────────────────────────

def build_prompt(changed_fn_name: str,
                 target_source: str,
                 caller_sources: list[tuple[str, str]],
                 endpoints: list[dict],
                 extra_context: str = "",
                 fn_file: str = "",
                 fn_line: int = 0,
                 handler_sources: dict | None = None) -> str:

    callers_block = ""
    for cname, csrc in caller_sources:
        callers_block += f"\n### Caller: {cname}\n```rust\n{csrc}\n```\n"

    if extra_context:
        callers_block += f"\n### Guard / helper functions referenced in callers\n```rust\n{extra_context}\n```\n"

    handlers_block = ""
    if handler_sources:
        for hname, hsrc in handler_sources.items():
            handlers_block += f"\n### Handler: `{hname}`\n```rust\n{hsrc}\n```\n"

    ep_lines = []
    for i, ep in enumerate(endpoints):
        method  = ep.get("method", "?")
        path    = ep.get("endpoint", ep.get("path", "?"))
        sk      = (ep.get("specialization_key")
                   or (ep.get("specialization") or {}).get("specialization_key", ""))
        chain   = ep.get("call_chain", [])
        handler = chain[0] if chain else "?"
        chain_str = " → ".join(str(n) for n in chain)
        ep_lines.append(
            f'{i}. [{method} {path}]  handler={handler!r}  spec={sk!r}\n'
            f'   chain: {chain_str}'
        )

    endpoints_block = "\n".join(ep_lines)

    handlers_section = (
        f"\n## API handler source code\n"
        f"Each handler below is the ENTRY POINT for one or more endpoints above.\n"
        f"Read each handler to understand what payment operation type it creates\n"
        f"and what data it populates — this determines whether guards are satisfied.\n"
        f"{handlers_block}"
    ) if handlers_block else ""

    return textwrap.dedent(f"""\
        You are a Rust static-analysis expert reviewing call graph reachability.

        A BFS call graph analyzer found that the following function is structurally
        reachable from several API endpoints. Your job is to determine which endpoints
        can ACTUALLY trigger this function at runtime, and which are false positives
        caused by guard conditions, data-flow gates, feature flags, or type mismatches
        that the static BFS cannot see.

        ## Changed function: `{changed_fn_name}`
        Location: `{fn_file}` line {fn_line}
        (Ignore any other function with the same name in the codebase — only this one changed.)
        ```rust
        {target_source}
        ```

        ## Call chain callers (bottom-up from changed function)
        {callers_block}
        {handlers_section}

        ## Endpoints to classify ({len(endpoints)} total)
        Each line shows: index. [METHOD path] handler='entry handler' spec='flow type'
        followed by the full call chain from handler down to the changed function.
        {endpoints_block}

        ## How to classify
        For each endpoint:
        1. Identify its HANDLER (entry point shown in the chain).
        2. Read that handler's source above to understand what operation/data it creates.
        3. Trace the call chain through the CALLER sources above, looking for guards:
           - `if let Some(x) = field` where `field` is None for this operation type
           - `match format!("{{op:?}}") {{ "X" => ..., _ => false }}` gating on op name
           - Feature flag always disabled for this flow
           - Wrong connector / trait dispatch type for this endpoint
        4. If a guard DEFINITIVELY prevents this endpoint from reaching the changed
           function → FALSE_POSITIVE. Otherwise → TRUE_POSITIVE.

        Apply the same guard logic consistently across all endpoints. If a guard
        eliminates N endpoints but allows 1, you must return N FALSE_POSITIVEs and
        1 TRUE_POSITIVE — do not collapse everything to the same verdict.

        YOUR RESPONSE MUST BE VALID JSON ONLY. Start your response with `{{` and end
        with `}}`. Do not include any explanation, analysis, preamble, or markdown.
        The JSON must have exactly this shape:
        {{
          "results": [
            {{
              "index": 0,
              "verdict": "TRUE_POSITIVE",
              "reason": "one concise sentence"
            }},
            ...
          ]
        }}

        verdict must be exactly "TRUE_POSITIVE" or "FALSE_POSITIVE".
        Include one entry per endpoint index (0 to {len(endpoints)-1}).
        RESPOND WITH JSON ONLY. NO OTHER TEXT.
    """)


# ── Main filtering logic ────────────────────────────────────────────────────────

def filter_endpoints(raw: list[dict], src_root: Path,
                     wrapper: dict | None = None,
                     model: str | None = None) -> list[dict]:
    if not raw:
        return raw

    # Top-level fallback location (BFS stores function info at wrapper level,
    # not per-endpoint when there is only one changed function)
    top_fn_name = (wrapper or {}).get("function", "")
    top_fn_file = (wrapper or {}).get("file", "") or (wrapper or {}).get("changed_file", "")
    top_fn_line = (wrapper or {}).get("def_line", 0) or (wrapper or {}).get("changed_line", 0)

    # Group by changed function (file + line identify it uniquely)
    groups: dict[tuple, list[int]] = {}
    for i, ep in enumerate(raw):
        key = (
            ep.get("file", "") or top_fn_file,
            ep.get("line", 0) or top_fn_line,
            ep.get("modified_function", "") or top_fn_name,
        )
        groups.setdefault(key, []).append(i)

    results: dict[int, tuple[str, str]] = {}  # index → (verdict, reason)

    for (fn_file, fn_line, fn_name), indices in groups.items():
        print(f"  [filter] {fn_name}  ({len(indices)} endpoints) …", file=sys.stderr)

        # Read the changed function's source
        target_source = get_source_for_node(
            fn_name, src_root, known_file=fn_file, known_line=fn_line
        )

        # Collect sources for the 2 immediate callers (consistent across the group)
        sample_ep = raw[indices[0]]
        chain = sample_ep.get("call_chain", [])
        # chain is ordered: endpoint handler → ... → changed fn
        # callers of the target are at positions [-2] and [-3]
        caller_sources: list[tuple[str, str]] = []
        for node in reversed(chain[:-1]):  # skip the last (changed fn itself)
            if len(caller_sources) >= MAX_CHAIN_NODES - 1:
                break
            src = get_source_for_node(node, src_root)
            caller_sources.append((node, src))

        extra_ctx = get_extra_context(caller_sources, src_root)

        # Collect source for each unique handler (capped to avoid prompt bloat)
        handler_sources: dict[str, str] = {}
        group_eps = [raw[i] for i in indices]
        for ep in group_eps:
            if len(handler_sources) >= 8:   # cap: 8 unique handlers max
                break
            chain = ep.get("call_chain", [])
            handler = chain[0] if chain else None
            if handler and handler not in handler_sources:
                parts = handler.split("#")
                h_fn  = parts[-1]
                found = _grep_fn(h_fn, src_root)
                if found:
                    fpath, fline = found
                    snippet = _read_lines(fpath, fline, 40)   # 40 lines, not 70
                    handler_sources[handler] = f"// {fpath.name}:{fline}\n{snippet}"
                else:
                    handler_sources[handler] = get_source_for_node(handler, src_root)

        # Batch into groups of 15 so each API response stays well within limits
        BATCH = 15
        for b_start in range(0, len(group_eps), BATCH):
            batch     = group_eps[b_start: b_start + BATCH]
            b_indices = indices[b_start: b_start + BATCH]
            prompt    = build_prompt(fn_name, target_source, caller_sources, batch, extra_ctx,
                                     fn_file=fn_file, fn_line=fn_line,
                                     handler_sources=handler_sources)
            try:
                raw_resp = _call_grid(prompt, model=model)
                parsed   = json.loads(raw_resp)
                for item in parsed.get("results", []):
                    idx     = item.get("index")
                    verdict = item.get("verdict", "TRUE_POSITIVE")
                    reason  = item.get("reason", "")
                    if idx is not None and 0 <= idx < len(batch):
                        results[b_indices[idx]] = (verdict, reason)
            except Exception as exc:
                batch_num = b_start // BATCH + 1
                print(f"  [filter] ERROR batch {batch_num}: {exc}", file=sys.stderr)
                for gi in b_indices:
                    results[gi] = ("TRUE_POSITIVE", f"filter skipped ({exc})")

    # Annotate and drop false positives
    kept = []
    n_fp = 0
    for i, ep in enumerate(raw):
        verdict, reason = results.get(i, ("TRUE_POSITIVE", "not evaluated"))
        if verdict == "FALSE_POSITIVE":
            n_fp += 1
            print(f"  [filter] DROP  {ep.get('method','?')} "
                  f"{ep.get('endpoint', ep.get('path','?'))}  "
                  f"[{(ep.get('specialization_key') or {}).get('specialization_key','')}]"
                  f"\n           reason: {reason}", file=sys.stderr)
        else:
            ep = dict(ep)
            ep["filter_verdict"] = verdict
            ep["filter_reason"]  = reason
            kept.append(ep)

    print(f"  [filter] {len(raw)} → {len(kept)} kept  ({n_fp} false positives removed)",
          file=sys.stderr)
    return kept


# ── CLI ─────────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Filter BFS output false positives via Grid API")
    ap.add_argument("--input",    required=True, help="Raw find_impact JSON path")
    ap.add_argument("--src-root", required=True, help="Hyperswitch source root")
    ap.add_argument("--out",      default=None,  help="Output path (default: <input>_filtered.json)")
    ap.add_argument(
        "--model", default=None,
        help=(
            f"Grid model to use (default: {GRID_MODEL}). "
            f"On budget errors falls back to {GRID_FALLBACK_MODEL}. "
            "Free Grid models: kimi-latest, glm-latest, hosted_vllm/kimi-k2-5, hosted_vllm/deepseek"
        ),
    )
    args = ap.parse_args()

    in_path  = Path(args.input)
    src_root = Path(args.src_root)
    out_path = Path(args.out) if args.out else in_path.with_suffix("").with_name(
        in_path.stem + "_filtered.json"
    )

    if not os.environ.get("JUSPAY_API_KEY"):
        print("ERROR: JUSPAY_API_KEY is not set. Skipping filter — output unchanged.",
              file=sys.stderr)
        import shutil
        shutil.copy(in_path, out_path)
        sys.exit(0)

    raw_data = json.loads(in_path.read_text())
    # Support both list and {endpoints:[...]} shapes
    if isinstance(raw_data, list):
        raw_list = raw_data
        wrapper  = None
    else:
        raw_list = raw_data.get("endpoints", raw_data.get("matrix", []))
        wrapper  = raw_data

    filtered = filter_endpoints(raw_list, src_root, wrapper=wrapper, model=args.model)

    if wrapper is not None:
        key = "endpoints" if "endpoints" in wrapper else "matrix"
        # Build set of surviving (method, path) pairs to prune flows too
        surviving = {(ep.get("method"), ep.get("path", ep.get("endpoint")))
                     for ep in filtered}
        # Prune flows: keep only flows that have ≥1 surviving endpoint
        pruned_flows = []
        for fl in wrapper.get("flows", []):
            kept_eps = [e for e in fl.get("endpoints", [])
                        if (e.get("method"), e.get("path", e.get("endpoint"))) in surviving]
            if kept_eps:
                pruned_flows.append({**fl, "endpoints": kept_eps})
        n_flows_dropped = len(wrapper.get("flows", [])) - len(pruned_flows)
        if n_flows_dropped:
            print(f"  [filter] Dropped {n_flows_dropped} flow(s) with no surviving endpoints",
                  file=sys.stderr)
        output = {**wrapper, key: filtered, "flows": pruned_flows,
                  "endpoint_count": len(filtered), "flow_count": len(pruned_flows)}
    else:
        output = filtered

    out_path.write_text(json.dumps(output, indent=2))
    print(f"  [filter] Written to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
