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

GRID_BASE_URL  = "https://grid.ai.juspay.net"
GRID_MODEL     = "claude-sonnet-4-6"   # strong reasoning model via Grid
MAX_SOURCE_LINES = 70   # lines to read around a function definition
MAX_CHAIN_NODES  = 4    # how many chain nodes to include source for


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
    For each caller whose source mentions a function call that produces a key
    guarded value (e.g. perform_debit_routing), also include THAT function's source.
    This lets the model see the full data-flow, not just the guard check.
    """
    # Functions worth pulling in if they appear in a caller's body
    WORTH_EXPANDING = [
        "perform_debit_routing",
        "should_perform_debit_routing_for_the_flow",
        "should_execute_debit_routing",
        "should_call_connector",
        "should_add_task_to_process_tracker",
    ]
    extra: list[str] = []
    seen: set[str] = set()
    for _, src in caller_sources:
        for fn in WORTH_EXPANDING:
            if fn in src and fn not in seen:
                seen.add(fn)
                found = _grep_fn(fn, src_root)
                if found:
                    fpath, fline = found
                    snippet = _read_lines(fpath, fline, MAX_SOURCE_LINES)
                    extra.append(f"// {fpath.name}:{fline}  [{fn}]\n{snippet}")
    return "\n\n".join(extra)


# ── Grid API call ───────────────────────────────────────────────────────────────

def _call_grid(prompt: str) -> str:
    """Call claude-sonnet-4-6 via Juspay Grid (OpenAI-compatible endpoint)."""
    api_key = os.environ.get("JUSPAY_API_KEY", "")
    if not api_key:
        raise RuntimeError("JUSPAY_API_KEY environment variable is not set.")

    payload = json.dumps({
        "model":           GRID_MODEL,
        "max_tokens":      4096,
        "messages":        [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature":     0.1,
    }).encode()

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
        text = data["choices"][0]["message"]["content"]
        # claude-sonnet may wrap JSON in markdown fences — strip them
        text = text.strip()
        # Strip markdown fences if present
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text.strip())
        # If model returned analysis text with embedded JSON, extract the JSON object
        if not text.startswith("{"):
            match = re.search(r'\{[^{}]*"results"\s*:\s*\[.*?\]\s*\}', text, re.DOTALL)
            if match:
                text = match.group(0)
        return text
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"Grid API error {e.code}: {body}") from e


# ── Prompt builder ──────────────────────────────────────────────────────────────

def build_prompt(changed_fn_name: str,
                 target_source: str,
                 caller_sources: list[tuple[str, str]],
                 endpoints: list[dict],
                 extra_context: str = "") -> str:

    callers_block = ""
    for cname, csrc in caller_sources:
        callers_block += f"\n### Caller: {cname}\n```rust\n{csrc}\n```\n"

    if extra_context:
        callers_block += f"\n### Key upstream functions (produce the guarded values)\n```rust\n{extra_context}\n```\n"

    ep_lines = []
    for i, ep in enumerate(endpoints):
        method = ep.get("method", "?")
        path   = ep.get("endpoint", ep.get("path", "?"))
        sk     = (ep.get("specialization_key")
                  or (ep.get("specialization") or {}).get("specialization_key", ""))
        chain  = " → ".join(str(n) for n in ep.get("call_chain", []))
        ep_lines.append(f'{i}. [{method} {path}] spec={sk!r}\n   chain: {chain}')

    endpoints_block = "\n".join(ep_lines)

    return textwrap.dedent(f"""\
        You are a Rust static-analysis expert reviewing call graph reachability.

        A BFS call graph analyzer found that the following function is structurally
        reachable from several API endpoints. Your job is to determine which endpoints
        can ACTUALLY trigger this function at runtime, and which are false positives
        caused by guard conditions, data-flow gates, or operation-type mismatches
        that the BFS cannot see.

        ## Changed function: `{changed_fn_name}`
        ```rust
        {target_source}
        ```

        ## Callers (source context)
        {callers_block}

        ## Endpoints to classify ({len(endpoints)} total)
        {endpoints_block}

        ## CRITICAL: spec key vs Rust operation name
        The `spec=` field in each endpoint is the PAYMENT ROUTING FLOW TYPE (e.g.
        "Authorize", "Capture", "PSync"). This is NOT the same as the Rust operation
        struct name used in guards like `should_perform_debit_routing_for_the_flow`.

        To determine the Rust operation name, look at the HANDLER function:
        - `payments_confirm`   handler → creates `PaymentConfirm` operation  ← matches "PaymentConfirm"
        - `payments_capture`   handler → creates `PaymentCapture` operation
        - `payments_retrieve`  handler → creates `PaymentStatus` operation
        - `payments_cancel`    handler → creates `PaymentCancel` operation
        - Any redirect/3DS handler  → creates `CompleteAuthorize` or `PaymentSync`

        So `spec=Authorize` on the `payments_confirm` handler means the Rust operation
        IS `PaymentConfirm`, which DOES satisfy `should_perform_debit_routing_for_the_flow`.

        ## Instructions
        For each endpoint (by its 0-based index above):
        - Identify the HANDLER function (first node in the chain)
        - Map it to its Rust operation name using the table above
        - Check whether that operation satisfies the guard conditions in the callers
        - Common patterns to detect:
            * `Option` parameter always `None` for this op type
            * `match format!("{{operation:?}}").as_str() {{ "X" => ..., _ => false }}` gating the call
            * Re-entrant call from a post-task/FRM hook using a different op type
            * Feature flag / config flag always false for this flow

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

def filter_endpoints(raw: list[dict], src_root: Path) -> list[dict]:
    if not raw:
        return raw

    # Group by changed function (file + line identify it uniquely)
    groups: dict[tuple, list[int]] = {}
    for i, ep in enumerate(raw):
        key = (ep.get("file", ""), ep.get("line", 0), ep.get("modified_function", ""))
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
        group_eps = [raw[i] for i in indices]
        prompt = build_prompt(fn_name, target_source, caller_sources, group_eps, extra_ctx)

        try:
            raw_resp = _call_grid(prompt)
            parsed   = json.loads(raw_resp)
            for item in parsed.get("results", []):
                idx     = item.get("index")
                verdict = item.get("verdict", "TRUE_POSITIVE")
                reason  = item.get("reason", "")
                if idx is not None and 0 <= idx < len(indices):
                    results[indices[idx]] = (verdict, reason)
        except Exception as exc:
            print(f"  [filter] ERROR: Grid API call failed — keeping all endpoints.",
                  file=sys.stderr)
            print(f"  [filter] Reason: {exc}", file=sys.stderr)
            for i in indices:
                results[i] = ("TRUE_POSITIVE", f"filter skipped ({exc})")

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

    filtered = filter_endpoints(raw_list, src_root)

    if wrapper is not None:
        key = "endpoints" if "endpoints" in wrapper else "matrix"
        output = {**wrapper, key: filtered}
    else:
        output = filtered

    out_path.write_text(json.dumps(output, indent=2))
    print(f"  [filter] Written to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
