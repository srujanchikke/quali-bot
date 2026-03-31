"""
codegen.py — LLM code generation + surgical patch application
=============================================================
Uses raw HTTP (urllib) — no SDK dependency.
Works with any OpenAI-compatible gateway including Grid.

Setup:
    export GRID_API_KEY=your_key
    export GRID_BASE_URL=https://your-grid-endpoint.com   # no trailing slash
    export GRID_MODEL=glm-latest                          # default

CLI:
    python codegen.py --repo . --connector Stripe --flow Overcapture
    python codegen.py --repo . --connector Stripe --flow Overcapture --dry-run --show-prompt
    python codegen.py --repo . --connector Stripe --flow Overcapture --model glm-latest

How Grid is called:
    POST {GRID_BASE_URL}/v1/chat/completions
    Authorization: Bearer {GRID_API_KEY}
    {"model": "glm-latest", "messages": [...], "max_tokens": 2048}

If your Grid uses a different path or auth header, set:
    GRID_ENDPOINT_PATH=/your/path         (default: /v1/chat/completions)
    GRID_AUTH_HEADER=X-API-Key            (default: Authorization)
    GRID_AUTH_PREFIX=                     (default: Bearer )
"""

from __future__ import annotations

import os, re, json, time, urllib.request, urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from flow_context import ContextBundle, Status


# ── result type ───────────────────────────────────────────────────────────────

@dataclass
class PatchResult:
    success:       bool
    files_changed: list[str]  = field(default_factory=list)
    llm_response:  str        = ""
    error:         str        = ""
    skipped_llm:   bool       = False

    def summary(self) -> str:
        if self.success:
            src = "(direct, no LLM)" if self.skipped_llm else "(LLM)"
            return f"✅ Patched {src}: {self.files_changed}"
        return f"❌ Failed: {self.error}"


# ── Grid / LLM client (pure urllib, no SDK) ───────────────────────────────────

class GridClient:
    """
    Thin HTTP wrapper around any OpenAI-compatible chat/completions endpoint.
    Reads all config from environment variables so nothing is hardcoded.
    """

    def __init__(
        self,
        api_key:   Optional[str] = None,
        base_url:  Optional[str] = None,
        model:     Optional[str] = None,
    ):
        self.api_key  = api_key  or os.environ.get("GRID_API_KEY")  or os.environ.get("GLM_API_KEY")
        self.base_url = (base_url or os.environ.get("GRID_BASE_URL") or "").rstrip("/")
        self.model    = model    or os.environ.get("GRID_MODEL") or "glm-latest"

        # Endpoint path — most gateways use /v1/chat/completions
        self.path = os.environ.get("GRID_ENDPOINT_PATH", "/v1/chat/completions")

        # Auth header customisation
        self.auth_header = os.environ.get("GRID_AUTH_HEADER", "Authorization")
        self.auth_prefix = os.environ.get("GRID_AUTH_PREFIX", "Bearer ")

        if not self.api_key:
            raise EnvironmentError(
                "GRID_API_KEY not set.\n"
                "Run:  export GRID_API_KEY=your_key"
            )
        if not self.base_url:
            raise EnvironmentError(
                "GRID_BASE_URL not set.\n"
                "Run:  export GRID_BASE_URL=https://your-grid-endpoint.com"
            )

    def chat(self, system: str, user: str, max_tokens: int = 2048) -> str:
        """
        Send a chat completion request. Returns the assistant message text.
        Raises on HTTP errors with the full response body for debugging.
        """
        url     = self.base_url + self.path
        payload = json.dumps({
            "model":      self.model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
        }).encode()

        headers = {
            "Content-Type":   "application/json",
            self.auth_header: f"{self.auth_prefix}{self.api_key}",
        }

        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            raise RuntimeError(
                f"Grid API error {e.code} {e.reason}\n"
                f"URL     : {url}\n"
                f"Response: {raw[:1000]}\n\n"
                f"Check GRID_BASE_URL, GRID_API_KEY, and GRID_MODEL."
            )
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Cannot reach Grid endpoint: {url}\n"
                f"Reason: {e.reason}\n"
                f"Check GRID_BASE_URL is correct and reachable."
            )

        # Parse OpenAI-compatible response
        # choices[0].message.content
        try:
            return body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            # Fallback: some gateways use different shapes
            # Try common alternatives
            for key in ("text", "output", "response", "content", "result"):
                if key in body:
                    return str(body[key])
            raise RuntimeError(
                f"Unexpected response shape from Grid.\n"
                f"Response: {json.dumps(body, indent=2)[:500]}\n\n"
                f"Expected: {{\"choices\": [{{\"message\": {{\"content\": \"...\"}}}}]}}\n"
                f"Set GRID_ENDPOINT_PATH / GRID_AUTH_HEADER if your Grid uses "
                f"a non-standard format."
            )

    @classmethod
    def probe(cls, **kwargs) -> dict:
        """
        Send a minimal test request and return the raw response.
        Use this to figure out what your Grid endpoint returns.
        """
        client = cls(**kwargs)
        url    = client.base_url + client.path
        payload = json.dumps({
            "model":      client.model,
            "max_tokens": 10,
            "messages":   [{"role": "user", "content": "Say: hello"}],
        }).encode()
        headers = {
            "Content-Type":   "application/json",
            client.auth_header: f"{client.auth_prefix}{client.api_key}",
        }
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return {"status": resp.status, "body": json.loads(resp.read())}
        except urllib.error.HTTPError as e:
            return {"status": e.code, "body": e.read().decode(errors="replace")}
        except Exception as e:
            return {"status": None, "error": str(e)}


# ── surgical patch helpers ─────────────────────────────────────────────────────

def _insert_after_flow(js: str, insert_after: str, new_block: str) -> tuple[str, bool]:
    """Insert new_block after the `insert_after: { ... },` block."""
    m = re.search(rf'\b{re.escape(insert_after)}\s*:\s*\{{', js)
    if not m:
        # Fallback: insert before closing } of card_pm
        card_m = re.search(r'\bcard_pm\s*:\s*\{', js)
        if not card_m:
            return js, False
        start = js.index('{', card_m.start())
        depth = 0
        for i, ch in enumerate(js[start:]):
            if ch == '{':   depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return js[:start+i] + new_block + '\n  ' + js[start+i:], True
        return js, False

    start = m.end() - 1
    depth = 0
    for i, ch in enumerate(js[start:]):
        if ch == '{':   depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end_pos = start + i + 1
                comma_m = re.match(r'(\s*,?)', js[end_pos:])
                cut = end_pos + (comma_m.end() if comma_m else 0)
                return js[:cut] + '\n' + new_block + js[cut:], True
    return js, False


def _append_to_allowlist(js: str, list_key: str, connector: str) -> tuple[str, bool]:
    """Add connector to CONNECTOR_LISTS.INCLUDE.<KEY> alphabetically."""
    feature = list_key.split(".")[-1]
    m = re.search(rf'\b{re.escape(feature)}\s*:\s*\[([^\]]*)\]', js, re.DOTALL)
    if not m:
        return js, False
    raw     = re.sub(r'//[^\n]*', '', m.group(1))
    members = [v.strip().strip("\"'") for v in raw.split(",") if v.strip().strip("\"'")]
    if connector in members:
        return js, True   # already present, idempotent
    members = sorted(set(members + [connector]))
    new_arr = ", ".join(f'"{c}"' for c in members)
    return js[:m.start(1)] + new_arr + js[m.end(1):], True


def _parse_new_flow_block(text: str) -> Optional[str]:
    m = re.search(r'<new_flow_block>\s*(.*?)\s*</new_flow_block>', text, re.DOTALL)
    return m.group(1).strip() if m else None


def _parse_file_tag(text: str) -> Optional[tuple[str, str]]:
    m = re.search(r'<file\s+path=["\']([^"\']+)["\']\s*>(.*?)</file>', text, re.DOTALL)
    return (m.group(1).strip(), m.group(2)) if m else None


def _parse_allowlist_update(text: str) -> Optional[tuple[str, str]]:
    m = re.search(
        r'<allowlist_update\s+connector=["\']([^"\']+)["\']\s+list=["\']([^"\']+)["\']',
        text,
    )
    return (m.group(1), m.group(2)) if m else None


def _normalise_block(block: str, indent: str = "    ") -> str:
    """Ensure 4-space base indentation and trailing comma."""
    lines = block.splitlines()
    if not lines:
        return block
    first_content = next((l for l in lines if l.strip()), lines[0])
    current_indent = len(first_content) - len(first_content.lstrip())
    if current_indent == 0:
        block = '\n'.join(indent + l if l.strip() else l for l in lines)
    if not block.rstrip().endswith(','):
        block = block.rstrip() + ','
    return block


# ── CodeGen ───────────────────────────────────────────────────────────────────

class CodeGen:
    def __init__(
        self,
        repo_root: str,
        indexer=None,
        model:    Optional[str] = None,
        dry_run:  bool = False,
        api_key:  Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        self.root    = Path(repo_root)
        self.indexer = indexer
        self.dry_run = dry_run
        self._client_kwargs = dict(
            api_key=api_key, base_url=base_url, model=model
        )
        self._client: Optional[GridClient] = None

    @property
    def client(self) -> GridClient:
        if self._client is None:
            self._client = GridClient(**{k: v for k, v in self._client_kwargs.items() if v})
        return self._client

    def _call(self, bundle: ContextBundle) -> str:
        """Call Grid LLM. Override in tests."""
        return self.client.chat(bundle.system_prompt, bundle.prompt)

    # ── dispatch ───────────────────────────────────────────────────────────────

    def apply(self, bundle: ContextBundle) -> PatchResult:
        ptype = bundle.patch_meta.get("type", "noop")

        if ptype == "noop":
            return PatchResult(success=True, skipped_llm=True,
                               error="Test already exists — nothing to do")

        if ptype == "allowlist_only" and bundle.patch_meta.get("skip_llm"):
            return self._apply_allowlist_only(bundle.patch_meta)

        if ptype == "insert_flow_block":
            return self._apply_insert_flow_block(bundle, bundle.patch_meta)

        if ptype == "full_file_rewrite":
            return self._apply_full_file_rewrite(bundle, bundle.patch_meta)

        return PatchResult(success=False, error=f"Unknown patch type: {ptype}")

    # ── strategies ─────────────────────────────────────────────────────────────

    def _apply_allowlist_only(self, meta: dict) -> PatchResult:
        utils_path = meta["allowlist_file"]
        list_key   = meta["allowlist_key"]
        connector  = meta["connector_name"]

        js = (self.root / utils_path).read_text(encoding="utf-8")
        modified, ok = _append_to_allowlist(js, list_key, connector)
        if not ok:
            return PatchResult(success=False,
                               error=f"{list_key} array not found in {utils_path}")
        if not self.dry_run:
            (self.root / utils_path).write_text(modified, encoding="utf-8")
            self._reindex([utils_path])
        return PatchResult(success=True, files_changed=[utils_path], skipped_llm=True)

    def _apply_insert_flow_block(self, bundle: ContextBundle, meta: dict) -> PatchResult:
        llm_text = self._call(bundle)

        new_block = _parse_new_flow_block(llm_text)
        if not new_block:
            return PatchResult(success=False, llm_response=llm_text,
                               error="LLM response missing <new_flow_block> tag.\n"
                                     f"Got:\n{llm_text[:500]}")

        new_block     = _normalise_block(new_block)
        target_path   = meta["target_file"]
        insert_after  = meta["insert_after"]
        files_changed = []

        # Patch connector config
        target_js      = (self.root / target_path).read_text(encoding="utf-8")
        patched, ok    = _insert_after_flow(target_js, insert_after, new_block)
        if not ok:
            return PatchResult(success=False, llm_response=llm_text,
                               error=f"Insertion point '{insert_after}' not found in {target_path}")
        if not self.dry_run:
            (self.root / target_path).write_text(patched, encoding="utf-8")
        files_changed.append(target_path)

        # Patch allowlist
        allowlist_file = meta.get("allowlist_file")
        allowlist_key  = meta.get("allowlist_key")
        connector      = meta.get("connector_name")
        if allowlist_file and allowlist_key and connector:
            al = _parse_allowlist_update(llm_text)
            if al:
                connector, allowlist_key = al
            utils_js       = (self.root / allowlist_file).read_text(encoding="utf-8")
            utils_patched, ok2 = _append_to_allowlist(utils_js, allowlist_key, connector)
            if not ok2:
                return PatchResult(success=False, llm_response=llm_text,
                                   files_changed=files_changed,
                                   error=f"Allowlist {allowlist_key} not found in {allowlist_file}")
            if not self.dry_run:
                (self.root / allowlist_file).write_text(utils_patched, encoding="utf-8")
            files_changed.append(allowlist_file)

        if not self.dry_run:
            self._reindex(files_changed)
        return PatchResult(success=True, files_changed=files_changed, llm_response=llm_text)

    def _apply_full_file_rewrite(self, bundle: ContextBundle, meta: dict) -> PatchResult:
        llm_text = self._call(bundle)
        parsed   = _parse_file_tag(llm_text)
        if not parsed:
            return PatchResult(success=False, llm_response=llm_text,
                               error="LLM response missing <file path=...> tag")
        rel_path, content = parsed
        if rel_path.startswith('/') or '..' in rel_path:
            return PatchResult(success=False, llm_response=llm_text,
                               error=f"Unsafe path: {rel_path}")
        if not self.dry_run:
            target = self.root / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            self._reindex([rel_path])
        return PatchResult(success=True, files_changed=[rel_path], llm_response=llm_text)

    def _reindex(self, paths: list[str]):
        if self.indexer:
            self.indexer.reindex_files(paths)


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    from flow_query import FlowQueryEngine as QueryEngine
    # build_context is replaced by flow_context.build_flow_context
    from indexer import Indexer

    ap = argparse.ArgumentParser(description="Generate and apply a Hyperswitch test patch")
    ap.add_argument("--repo",        required=True,  help="Path to cypress-tests root")
    ap.add_argument("--connector",   required=True)
    ap.add_argument("--flow",        required=True)
    ap.add_argument("--model",       default=None,
                    help="Model name (default: GRID_MODEL env var or glm-latest)")
    ap.add_argument("--base-url",    default=None,
                    help="Grid base URL (default: GRID_BASE_URL env var)")
    ap.add_argument("--api-key",     default=None,
                    help="Grid API key (default: GRID_API_KEY env var)")
    ap.add_argument("--uri",         default="bolt://localhost:7687")
    ap.add_argument("--user",        default="neo4j")
    ap.add_argument("--password",    default="Hyperswitch123")
    ap.add_argument("--dry-run",     action="store_true",
                    help="Show what would change without writing files")
    ap.add_argument("--show-prompt", action="store_true",
                    help="Print the LLM prompt")
    ap.add_argument("--probe",       action="store_true",
                    help="Send a test request to verify Grid connectivity")
    args = ap.parse_args()

    # Probe mode — test Grid connection before doing anything else
    if args.probe:
        print("Probing Grid endpoint...")
        result = GridClient.probe(
            api_key=args.api_key, base_url=args.base_url, model=args.model
        )
        print(json.dumps(result, indent=2))
        raise SystemExit(0)

    q   = QueryEngine(args.uri, (args.user, args.password))
    idx = Indexer(args.repo, args.uri, (args.user, args.password))
    cg  = CodeGen(
        args.repo, indexer=idx, model=args.model,
        dry_run=args.dry_run, api_key=args.api_key, base_url=args.base_url,
    )

    try:
        result = q.test_exists(args.connector, args.flow, "")
        if result.exists:
            print(f"✅ Test already exists for {args.connector}/{args.flow}")
            print(result.summary())
            raise SystemExit(0)

        llm_ctx = q.get_llm_context(args.connector, args.flow)
        bundle  = build_context(args.repo, llm_ctx)

        print(f"Status        : {bundle.status.value}")
        print(f"Files to edit : {bundle.files_to_edit}")
        print(f"Prompt tokens : ~{len(bundle.prompt)//4:,}")
        print(f"Skip LLM      : {bundle.patch_meta.get('skip_llm', False)}")
        print(f"Model         : {cg._client_kwargs.get('model') or os.environ.get('GRID_MODEL','glm-latest')}")
        print(f"Grid URL      : {args.base_url or os.environ.get('GRID_BASE_URL','(GRID_BASE_URL not set)')}")
        if args.dry_run:
            print("(dry-run — no files written)")
        if args.show_prompt:
            print("\n" + "─"*60)
            print(bundle.prompt)
            print("─"*60 + "\n")

        patch = cg.apply(bundle)
        print(patch.summary())
        if patch.llm_response and args.show_prompt:
            print("\nLLM response:")
            print(patch.llm_response)

    finally:
        q.close()
        idx.close()