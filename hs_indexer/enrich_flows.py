"""
enrich_flows.py  —  LLM Enrichment Layer for find_impact.py Output
====================================================================

Reads the JSON produced by find_impact.py and, for every flow, asks
an LLM to produce human-readable test specifications:

  • summary             — what this flow does in plain English
  • prerequisites       — setup / runtime / dispatch requirements
  • setup_payloads      — API requests to configure prerequisites
  • trigger_payloads    — requests that enter the flow (coverage) and
                          prove the changed function matters (assertion)
  • negative_payloads   — requests that must NOT reach this flow
  • expected_outcomes   — what a passing test looks like
  • testing_instructions — step-by-step guide

Supported backends (--backend flag):
  groq      Llama 3.3 70B via Groq  — free tier, fast (recommended)
            GROQ_API_KEY  from console.groq.com
  gemini    Gemini 1.5 Flash        — free tier, 1M tokens/day
            GEMINI_API_KEY from aistudio.google.com
  ollama    Local model via Ollama  — fully free, no key needed
            --ollama-model  (default: llama3.1)
  anthropic Claude Opus 4.6         — best quality, paid
            ANTHROPIC_API_KEY

Usage:
  GROQ_API_KEY=gsk_... python enrich_flows.py --input result.json --backend groq
  GEMINI_API_KEY=...  python enrich_flows.py --input result.json --backend gemini
                      python enrich_flows.py --input result.json --backend ollama
  ANTHROPIC_API_KEY=. python enrich_flows.py --input result.json --backend anthropic

Optional:
  --out <path>          output file  (default: <input>_enriched.json)
  --flow <id>           enrich only this flow_id
  --ollama-model <name> model for Ollama backend  (default: llama3.1)
  --ollama-url <url>    Ollama base URL  (default: http://localhost:11434)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap

# ── Config ─────────────────────────────────────────────────────────────────────

MAX_SNIPPET_LEN  = 800    # chars of source snippet per intermediate chain node
MAX_SOURCE_LEN   = 2_000  # chars of full_source / source for direct_caller / target

# Default models per backend
_DEFAULT_MODELS = {
    "groq":      "llama-3.3-70b-versatile",
    "gemini":    "gemini-2.0-flash",
    "ollama":    "llama3.1",
    "anthropic": "claude-opus-4-6",
    "grid":      "claude-sonnet-4-6",
}


# ── Shared output schema (as a prompt fragment) ────────────────────────────────
# Backends that don't support native JSON-schema enforcement receive this as text.

SCHEMA_DESCRIPTION = """\
Return ONLY a JSON object with exactly these fields:

{
  "summary": "<string: one-paragraph description>",
  "prerequisites": {
    "setup":    [{"description":"","endpoint":"","payload":{},"notes":""}],
    "runtime":  [{"field":"","constraint":"","notes":""}],
    "dispatch": [{"guard":"","must_be":"","rationale":""}]
  },
  "setup_payloads": [
    {"label":"","method":"","endpoint":"","headers":{},"body":{},"notes":""}
  ],
  "trigger_payloads": [
    {
      "label":"","class":"coverage|assertion",
      "method":"","endpoint":"","body":{},
      "expected_status":200,
      "assertions":["<string>"],
      "notes":""
    }
  ],
  "negative_payloads": [
    {"label":"","method":"","endpoint":"","body":{},"expected_status":200,"rationale":""}
  ],
  "expected_outcomes": [
    {"outcome":"","how_to_verify":""}
  ],
  "testing_instructions": ["<step 1>","<step 2>"],
  "confidence": "high|medium|low",
  "confidence_notes": "<string>"
}"""

# Full JSON schema for backends that support native enforcement (Anthropic)
OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "prerequisites": {
            "type": "object",
            "properties": {
                "setup": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string"},
                            "endpoint":    {"type": "string"},
                            "payload":     {"type": "object"},
                            "notes":       {"type": "string"},
                        },
                        "required": ["description", "endpoint"],
                        "additionalProperties": False,
                    },
                },
                "runtime": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "field":      {"type": "string"},
                            "constraint": {"type": "string"},
                            "notes":      {"type": "string"},
                        },
                        "required": ["field", "constraint"],
                        "additionalProperties": False,
                    },
                },
                "dispatch": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "guard":     {"type": "string"},
                            "must_be":   {"type": "string"},
                            "rationale": {"type": "string"},
                        },
                        "required": ["guard", "must_be"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["setup", "runtime", "dispatch"],
            "additionalProperties": False,
        },
        "setup_payloads": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label":    {"type": "string"},
                    "method":   {"type": "string"},
                    "endpoint": {"type": "string"},
                    "headers":  {"type": "object"},
                    "body":     {"type": "object"},
                    "notes":    {"type": "string"},
                },
                "required": ["label", "method", "endpoint", "body"],
                "additionalProperties": False,
            },
        },
        "trigger_payloads": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label":           {"type": "string"},
                    "class":           {"type": "string", "enum": ["coverage", "assertion"]},
                    "method":          {"type": "string"},
                    "endpoint":        {"type": "string"},
                    "body":            {"type": "object"},
                    "expected_status": {"type": "integer"},
                    "assertions":      {"type": "array", "items": {"type": "string"}},
                    "notes":           {"type": "string"},
                },
                "required": ["label", "class", "method", "endpoint", "body",
                             "expected_status", "assertions"],
                "additionalProperties": False,
            },
        },
        "negative_payloads": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label":           {"type": "string"},
                    "method":          {"type": "string"},
                    "endpoint":        {"type": "string"},
                    "body":            {"type": "object"},
                    "expected_status": {"type": "integer"},
                    "rationale":       {"type": "string"},
                },
                "required": ["label", "method", "endpoint", "body",
                             "expected_status", "rationale"],
                "additionalProperties": False,
            },
        },
        "expected_outcomes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "outcome":        {"type": "string"},
                    "how_to_verify":  {"type": "string"},
                },
                "required": ["outcome", "how_to_verify"],
                "additionalProperties": False,
            },
        },
        "testing_instructions": {"type": "array", "items": {"type": "string"}},
        "confidence":       {"type": "string", "enum": ["high", "medium", "low"]},
        "confidence_notes": {"type": "string"},
    },
    "required": [
        "summary", "prerequisites", "setup_payloads", "trigger_payloads",
        "negative_payloads", "expected_outcomes", "testing_instructions",
        "confidence", "confidence_notes",
    ],
    "additionalProperties": False,
}


# ── Prompt construction ────────────────────────────────────────────────────────

def _trim(text: str | None, max_len: int) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[:max_len] + "\n…(truncated)"


def _format_condition(cond: dict) -> str:
    if not cond:
        return "unconditional"
    ctype = cond.get("type", "unknown")
    text  = cond.get("text", "")
    if ctype == "unconditional":
        return "unconditional"
    return f"[{ctype}] {text}" if text else ctype


def _format_chain_node(node: dict, idx: int, total: int) -> str:
    fn   = node.get("function", "?")
    file = node.get("file",     "?")
    line = node.get("def_line", "?")
    role = node.get("role",     "intermediate")
    cond = _format_condition(node.get("condition") or {})

    src_raw = node.get("full_source") or node.get("source") or ""
    if not src_raw and isinstance(node.get("condition"), dict):
        src_raw = node["condition"].get("snippet", "")
    max_len = MAX_SOURCE_LEN if role in ("target", "direct_caller") else MAX_SNIPPET_LEN
    snippet = _trim(src_raw, max_len)

    lines = [
        f"  [{idx+1}/{total}] {fn}  ({role})",
        f"      file:      {file}:{line}",
        f"      condition: {cond}",
    ]
    if snippet:
        lines.append("      snippet:")
        for sl in snippet.splitlines():
            lines.append("        " + sl)
    return "\n".join(lines)


def build_prompt(impact: dict, flow: dict, include_schema: bool = True) -> str:
    fn_name  = impact.get("function", "?")
    fn_file  = impact.get("file",     "?")
    fn_line  = impact.get("def_line", "?")

    flow_id   = flow.get("flow_id",      "?")
    flow_desc = flow.get("description",  "")
    endpoints = flow.get("endpoints",    [])
    chain     = flow.get("chain",        [])
    prereqs   = flow.get("prerequisites", [])
    connectors = flow.get("connectors",  [])
    cond_high     = flow.get("conditions_high",     0)
    cond_inferred = flow.get("conditions_inferred", 0)
    cond_missing  = flow.get("conditions_missing",  0)

    endpoint_lines = "\n".join(
        f"  {e.get('method','?')} {e.get('path','?')}  (handler: {e.get('handler','?')})"
        for e in endpoints
    )
    chain_lines = "\n".join(
        _format_chain_node(node, i, len(chain)) for i, node in enumerate(chain)
    )

    if prereqs:
        parts = []
        for p in prereqs:
            field   = p.get("field", "?")
            cond    = p.get("condition", "")
            cfg_ep  = p.get("config_endpoint", "")
            cfg_val = json.dumps(p.get("config_value", {}), separators=(",", ":"))
            parts.append(
                f"  - {field}: {cond}\n    config via: {cfg_ep}  value: {cfg_val}"
            )
        prereq_lines = "\n".join(parts)
    else:
        prereq_lines = "  (none detected statically)"

    connector_sample = ", ".join(connectors[:10]) + (
        f"  … and {len(connectors)-10} more" if len(connectors) > 10 else ""
    )

    schema_block = f"\n## Required output format\n{SCHEMA_DESCRIPTION}\n" if include_schema else ""

    return textwrap.dedent(f"""\
        You are a senior Rust/Hyperswitch payment platform test engineer.

        ## Changed Function
        Name : {fn_name}
        File : {fn_file}:{fn_line}

        ## Flow {flow_id}
        Guard description : {flow_desc}

        ## Endpoints that reach the changed function via this flow
        {endpoint_lines}

        ## Call chain (entry point → changed function)
        {chain_lines}

        ## Statically detected prerequisites
        {prereq_lines}

        ## Condition coverage
          High-confidence conditions   : {cond_high}
          Inferred conditions          : {cond_inferred}
          Conditions with missing info : {cond_missing}

        ## Connectors involved
        {connector_sample if connector_sample else "any / not restricted"}
        {schema_block}
        Based on the static analysis above, produce a complete test specification for
        this flow.

        Rules:
        1. summary — explain what `{fn_name}` does in this flow and why a test matters.
        2. prerequisites.setup — API calls needed BEFORE triggering the flow.
        3. prerequisites.runtime — request-body fields / headers required by the guards.
        4. prerequisites.dispatch — each conditional guard and what value makes it TRUE.
        5. setup_payloads — concrete JSON request bodies for each setup step.
           Use realistic placeholder values (uuid4 for IDs, "sk_test_XXX" for keys).
        6. trigger_payloads — at least one "coverage" (reaches the fn) and one
           "assertion" (exercises the specific logic of `{fn_name}`).
        7. negative_payloads — at least one payload per major guard that skips this flow.
        8. expected_outcomes — observable HTTP / DB / event outcomes for a passing test.
        9. testing_instructions — numbered steps in plain English.
        10. confidence — "high" if all guards clearly understood, "medium" if some
            inferred, "low" if major preconditions unknown.

        Respond with ONLY the JSON object — no prose, no markdown fences.
    """)


# ── Backend implementations ────────────────────────────────────────────────────

def _call_groq(prompt: str, model: str) -> str:
    from groq import Groq
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY environment variable is not set.")
    client = Groq(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.2,
    )
    return response.choices[0].message.content


def _call_gemini(prompt: str, model: str) -> str:
    from google import genai
    from google.genai import types
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY environment variable is not set.")
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.2,
        ),
    )
    return response.text


def _call_ollama(prompt: str, model: str, base_url: str) -> str:
    import urllib.request
    payload = json.dumps({
        "model":  model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.2},
    }).encode()
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read())
    return data.get("response", "")


def _call_grid(prompt: str, model: str) -> str:
    import urllib.request as _urlreq
    import urllib.error   as _urlerr
    api_key = os.environ.get("JUSPAY_API_KEY", "")
    if not api_key:
        raise RuntimeError("JUSPAY_API_KEY environment variable is not set.")
    payload = json.dumps({
        "model":       model,
        "max_tokens":  16000,
        "messages":    [{"role": "user", "content": prompt}],
        "temperature": 0.2,
    }).encode()
    req = _urlreq.Request(
        "https://grid.ai.juspay.net/v1/chat/completions",
        data=payload,
        headers={
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with _urlreq.urlopen(req, timeout=600) as resp:
            data = json.loads(resp.read())
        import re as _re
        text = data["choices"][0]["message"]["content"]
        text = _re.sub(r"^```(?:json)?\s*", "", text.strip())
        text = _re.sub(r"\s*```$", "", text.strip())
        return text
    except _urlerr.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"Grid API error {e.code}: {body}") from e


def _call_anthropic(prompt: str, model: str) -> str:
    import anthropic as _anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set.")
    client = _anthropic.Anthropic(api_key=api_key)
    with client.messages.stream(
        model=model,
        max_tokens=16_000,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": prompt}],
        output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
    ) as stream:
        final = stream.get_final_message()
    return next(b.text for b in final.content if b.type == "text")


def enrich_flow(impact: dict, flow: dict, backend: str, model: str,
                ollama_url: str = "http://localhost:11434") -> dict:
    # Anthropic enforces the schema natively; others get it embedded in the prompt
    include_schema = backend != "anthropic"
    prompt = build_prompt(impact, flow, include_schema=include_schema)

    if backend == "groq":
        raw = _call_groq(prompt, model)
    elif backend == "gemini":
        raw = _call_gemini(prompt, model)
    elif backend == "ollama":
        raw = _call_ollama(prompt, model, ollama_url)
    elif backend == "anthropic":
        raw = _call_anthropic(prompt, model)
    elif backend == "grid":
        raw = _call_grid(prompt, model)
    else:
        raise ValueError(f"Unknown backend: {backend!r}")

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM returned invalid JSON: {e}\n---\n{raw[:500]}") from e


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Enrich find_impact.py output with LLM test specs."
    )
    ap.add_argument("--input",   required=True, help="Path to find_impact.py JSON output")
    ap.add_argument("--out",     default=None,  help="Output path (default: <input>_enriched.json)")
    ap.add_argument("--flow",    type=int, default=None, help="Enrich only this flow_id")
    ap.add_argument(
        "--backend", default="groq",
        choices=["groq", "gemini", "ollama", "anthropic", "grid"],
        help="LLM backend to use (default: groq)",
    )
    ap.add_argument("--model",        default=None,
                    help="Override model name for the selected backend")
    ap.add_argument("--ollama-model", default="llama3.1",
                    help="Model name for Ollama backend (default: llama3.1)")
    ap.add_argument("--ollama-url",   default="http://localhost:11434",
                    help="Ollama API base URL")
    args = ap.parse_args()

    backend = args.backend
    model   = args.model or (args.ollama_model if backend == "ollama"
                             else _DEFAULT_MODELS[backend])

    # ── Load input ─────────────────────────────────────────────────────────────
    with open(args.input) as f:
        impact: dict = json.load(f)

    flows: list[dict] = impact.get("flows", [])
    if not flows:
        print("No flows found in input. Nothing to enrich.", file=sys.stderr)
        sys.exit(0)

    if args.flow is not None:
        flows = [fl for fl in flows if fl.get("flow_id") == args.flow]
        if not flows:
            print(f"flow_id {args.flow} not found.", file=sys.stderr)
            sys.exit(1)

    # ── Enrich each flow ───────────────────────────────────────────────────────
    fn_name = impact.get("function", "?")
    print(f"\nBackend : {backend}  ({model})", file=sys.stderr)
    print(f"Enriching {len(flows)} flow(s) for  {fn_name} …", file=sys.stderr)

    enriched_flows: list[dict] = []
    for fl in flows:
        flow_id = fl.get("flow_id", "?")
        desc    = fl.get("description", "")[:80]
        print(f"\n  [flow {flow_id}] {desc}", file=sys.stderr)
        print( "            calling LLM …", file=sys.stderr, end=" ", flush=True)

        try:
            spec = enrich_flow(impact, fl, backend, model,
                               ollama_url=args.ollama_url)
            print("✓", file=sys.stderr)
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            spec = {"error": str(exc)}

        enriched_flows.append({**fl, "llm_spec": spec})

    # ── Write output ───────────────────────────────────────────────────────────
    out_path = args.out or (os.path.splitext(args.input)[0] + "_enriched.json")
    result   = {
        **{k: v for k, v in impact.items() if k != "flows"},
        "flows": enriched_flows,
    }
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)

    print(f"\n  Written to : {out_path}", file=sys.stderr)
    print(f"  Flows enriched : {len(enriched_flows)}", file=sys.stderr)


if __name__ == "__main__":
    main()
