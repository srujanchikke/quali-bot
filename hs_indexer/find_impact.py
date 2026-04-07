"""
find_impact.py  —  Part 3: BFS Reachability → Impact Report
=============================================================

Given a function name, walks the CALLS graph *upward* (callers → callers of
callers → …) until it reaches API endpoint handlers, then reports which
endpoints can reach the function and what conditional guards lie along each
path.

Three steps on first run (idempotent — can be re-run safely):
  1. Tag endpoint handlers in Neo4j (:Fn nodes get  is_endpoint / http_method /
     http_path  properties) by parsing routes/app.rs.
  2. Load the full CALLS graph (with guard annotations from Part 2) into memory.
  3. BFS upward from the target function.  For every endpoint reached, record
     the call chain and every guard along that path.

Output (stdout + optional JSON file):
  {
    "function":   "get_connector_with_networks",
    "file":       "crates/router/src/core/payments.rs",
    "def_line":   2129,
    "endpoint_count": 4,
    "endpoints": [
      {
        "method":     "POST",
        "path":       "/payments/{payment_id}/confirm",
        "handler":    "payments_confirm",
        "call_chain": ["payments_confirm", ..., "get_connector_with_networks"],
        "guards": [
          {
            "at_hop":      "get_connector_data_with_routing_decision → get_connector_with_networks",
            "guard_type":  "if_let",
            "condition":   "let Some((data, card_network)) = get_connector_with_networks(connectors)"
          }
        ]
      }
    ]
  }

Usage:
  SRC_ROOT=/path/to/hyperswitch python find_impact.py <function_name> [--depth N] [--out result.json]
"""

import argparse
import json
import os
import re
import sys
from collections import deque
from dataclasses import dataclass

from tree_sitter import Language, Parser, Node as TSNode
import tree_sitter_rust as _tsrust

from hs_indexer.db import get_driver


# ── Specialization constraint ─────────────────────────────────────────────────

@dataclass(frozen=True)
class SpecConstraint:
    """
    Immutable, hashable BFS constraint representing the full specialization
    identity of a concrete trait impl method.

    Example — impl ConnectorIntegration<Authorize, X, Y> for Stripe :: build_request:
        impl_type  = "Stripe"
        trait_name = "ConnectorIntegration"
        trait_args = ("Authorize", "X", "Y")
        method     = "build_request"
        spec_key   = "ConnectorIntegration|Stripe|Authorize,X,Y|build_request"

    Unknown fields are None / empty tuple — treated as wildcards during matching.
    This distinguishes Stripe+Authorize+build_request from Stripe+PSync+build_request.
    """
    impl_type:  str | None      = None
    trait_name: str | None      = None
    trait_args: tuple           = ()       # tuple[str, ...]
    method:     str | None      = None

    def spec_key(self) -> str | None:
        """Stable string identity; None when insufficient info."""
        if self.trait_name and self.impl_type:
            args = ",".join(self.trait_args)
            return f"{self.trait_name}|{self.impl_type}|{args}|{self.method or ''}"
        return self.impl_type   # backward-compat: bare type name

    def is_compatible_with(self, other: "SpecConstraint") -> bool:
        """True if both constraints could represent the same specialization."""
        def _clash(a, b):
            return a and b and a != b
        if _clash(self.impl_type,  other.impl_type):  return False
        if _clash(self.trait_name, other.trait_name): return False
        if _clash(self.method,     other.method):     return False
        if self.trait_args and other.trait_args and self.trait_args != other.trait_args:
            return False
        return True

    def merge(self, other: "SpecConstraint") -> "SpecConstraint":
        """Produce a more specific constraint from two compatible ones."""
        return SpecConstraint(
            impl_type  = self.impl_type  or other.impl_type,
            trait_name = self.trait_name or other.trait_name,
            trait_args = self.trait_args or other.trait_args,
            method     = self.method     or other.method,
        )

    def to_dict(self, known_connectors: frozenset = frozenset()) -> dict:
        # Use "connector" key only when impl_type is a known real connector.
        # Falls back to trait name check if known_connectors is not provided.
        # This prevents framework structs like "PaymentResponse" from being
        # labelled as connectors and causing downstream tools to look for
        # non-existent PaymentResponse.js Cypress config files.
        if known_connectors:
            is_connector = self.impl_type in known_connectors
        else:
            is_connector = self.trait_name == "ConnectorIntegration"
        return {
            "connector" if is_connector else "impl_type": self.impl_type,
            "trait":              self.trait_name,
            "trait_args":         list(self.trait_args),
            "method":             self.method,
            "specialization_key": self.spec_key(),
        }

# ── Step 0: should_call_connector gate analysis ───────────────────────────────

def parse_should_call_connector(src_root: str) -> dict:
    """
    Parse should_call_connector in payments.rs.
    Returns {op_name: "always_true" | "always_false" | "conditional"}
    with "_default" for the wildcard arm result.
    """
    payments_file = os.path.join(src_root, "crates", "router", "src", "core", "payments.rs")
    try:
        with open(payments_file, errors="replace") as f:
            content = f.read()
    except OSError:
        return {}

    fn_start = content.find("pub fn should_call_connector<")
    if fn_start == -1:
        return {}
    match_start = content.find('match format!("{operation:?}").as_str()', fn_start)
    if match_start == -1:
        return {}

    # The match line is: match format!("{operation:?}").as_str() {
    # The format string itself contains '{', so skip past .as_str() before looking for the opening brace.
    as_str_pos = content.find(".as_str()", match_start)
    if as_str_pos == -1:
        return {}
    brace_start = content.index("{", as_str_pos + len(".as_str()"))
    depth, pos = 0, brace_start
    while pos < len(content):
        if content[pos] == "{":
            depth += 1
        elif content[pos] == "}":
            depth -= 1
            if depth == 0:
                break
        pos += 1
    match_body = content[brace_start + 1 : pos]

    result: dict = {}

    # Classify the wildcard/default arm
    default_m = re.search(r"_\s*=>\s*(true|false)", match_body)
    result["_default"] = "always_true" if (default_m and default_m.group(1) == "true") else "always_false"

    # Find every named arm start
    arm_positions = [(m.start(), m.group(1)) for m in re.finditer(r'"(\w+)"\s*=>', match_body)]

    for i, (start, op_name) in enumerate(arm_positions):
        # Body of this arm runs until the next arm or the default arm
        next_boundary = (
            arm_positions[i + 1][0] if i + 1 < len(arm_positions)
            else (default_m.start() if default_m else len(match_body))
        )
        arrow_pos = match_body.index("=>", start) + 2
        arm_expr = match_body[arrow_pos:next_boundary].strip().rstrip(",").strip()

        if arm_expr == "true":
            result[op_name] = "always_true"
        elif arm_expr == "false":
            result[op_name] = "always_false"
        else:
            result[op_name] = "conditional"

    return result


_DISPATCH_FN_NAMES = frozenset({
    "payments_operation_core",
    "proxy_for_payments_operation_core",
    "payments_core",
    "authorize_verify_select",  # passes op type as first positional arg (not turbofish)
})

def _ts_find_handler_op_type(
    abs_path: str,
    def_line: int,
    payment_op_types: frozenset | None,
) -> str | None:
    """
    Tree-sitter primary pass for extract_handler_op_type.

    1. Parse the file and find the function_item node whose start line is within
       3 rows of def_line - 1.
    2. Walk its body for call_expression nodes where the function text ends with
       any of: payments_operation_core, proxy_for_payments_operation_core, payments_core.
    3. For each such call inspect the first argument:
       - struct_expression  → short_name of its name field
       - call_expression with '::' in function → short_name(fn.rsplit('::', 1)[0])
       - identifier         → skip (would need binding lookup)
    4. Return first type found that is in payment_op_types (or any type if None).
    """
    # _ts_parse_file and _ts_text are defined later in this module; resolved at runtime.
    tree, src = _ts_parse_file(abs_path)
    if tree is None:
        return None

    target_row = def_line - 1  # 0-indexed

    # Find the function_item whose start row is within 3 of target_row
    fn_node = None

    def _find_fn(node):
        nonlocal fn_node
        if fn_node is not None:
            return
        if node.type == "function_item":
            if abs(node.start_point[0] - target_row) <= 3:
                fn_node = node
                return
        for child in node.named_children:
            _find_fn(child)

    _find_fn(tree.root_node)
    if fn_node is None:
        return None

    body = fn_node.child_by_field_name("body")
    if body is None:
        return None

    def _short(qualified: str) -> str:
        base = qualified.split("<")[0]
        base = base.rsplit("::", 1)[-1]
        return base.strip().rstrip(">()")

    def _walk_for_dispatch(node) -> str | None:
        if node.type == "call_expression":
            fn_n = node.child_by_field_name("function")
            if fn_n is not None:
                candidate: str | None = None

                # Pattern 1: turbofish — payments_core::<PaymentCreate, R>(state, ...)
                # fn_n is a generic_function node whose "function" child is the base name
                # and whose "type_arguments" child contains the op type as first type arg.
                if fn_n.type == "generic_function":
                    base_fn_n = fn_n.child_by_field_name("function")
                    base_fn_text = _ts_text(base_fn_n, src) if base_fn_n else ""
                    base_short = base_fn_text.rsplit("::", 1)[-1].strip()
                    if base_short in _DISPATCH_FN_NAMES:
                        type_args_n = fn_n.child_by_field_name("type_arguments")
                        if type_args_n:
                            first_type = next(
                                (c for c in type_args_n.named_children
                                 if c.type not in ("lifetime", "comment")),
                                None,
                            )
                            if first_type is not None:
                                t = _short(_ts_text(first_type, src))
                                if t != "_":
                                    # Only accept as op type if it looks like a payment op;
                                    # the first turbofish arg is often the flow generic (e.g.
                                    # api_types::Capture), NOT the op type.  If it doesn't
                                    # match, fall through to Pattern 2 which scans positional args.
                                    looks_like_op = (
                                        (payment_op_types and t in payment_op_types)
                                        or bool(re.match(r"^(Payment|Complete)", t))
                                    )
                                    if looks_like_op:
                                        candidate = t

                # Pattern 2: scan ALL positional args for a Payment op type.
                # payments_core passes the operation struct as a later positional arg:
                #   payments_core::<api_types::Capture(=F), ..., PaymentData<_>>(
                #       state, req_state, platform, profile, payments::PaymentCapture, ...)
                # The first turbofish arg is the flow generic F (e.g. api_types::Capture),
                # NOT the op type. The op type (e.g. PaymentCapture) is a positional arg.
                # Scan all args and prefer ones that look like op types.
                if candidate is None:
                    if fn_n.type == "generic_function":
                        base_fn2 = fn_n.child_by_field_name("function")
                        base_text = _ts_text(base_fn2, src) if base_fn2 else ""
                        short_fn = base_text.rsplit("::", 1)[-1].strip()
                    else:
                        fn_text = _ts_text(fn_n, src)
                        short_fn = fn_text.rsplit("::", 1)[-1].strip()
                    if short_fn in _DISPATCH_FN_NAMES:
                        args_n = node.child_by_field_name("arguments")
                        if args_n:
                            _data_suffixes = ("Data", "Response", "Request", "Types")
                            for arg in args_n.named_children:
                                if arg.type == "comment":
                                    continue
                                arg_cand: str | None = None
                                if arg.type == "struct_expression":
                                    type_n = arg.child_by_field_name("name")
                                    if type_n:
                                        arg_cand = _short(_ts_text(type_n, src))
                                elif arg.type == "call_expression":
                                    inner_fn = arg.child_by_field_name("function")
                                    if inner_fn:
                                        inner_text = _ts_text(inner_fn, src)
                                        if "::" in inner_text:
                                            arg_cand = _short(inner_text.rsplit("::", 1)[0])
                                elif arg.type in ("identifier", "scoped_identifier"):
                                    arg_cand = _short(_ts_text(arg, src))
                                if arg_cand and not any(arg_cand.endswith(s) for s in _data_suffixes):
                                    is_op = (
                                        (payment_op_types and arg_cand in payment_op_types)
                                        or re.match(r"^(Payment|Complete)", arg_cand)
                                    )
                                    if is_op:
                                        candidate = arg_cand
                                        break

                if candidate:
                    if payment_op_types is None or candidate in payment_op_types:
                        return candidate
                    if re.match(r"^(Payment|Complete)", candidate):
                        return candidate

        for child in node.named_children:
            result = _walk_for_dispatch(child)
            if result is not None:
                return result
        return None

    return _walk_for_dispatch(body)


def extract_handler_op_type(
    handler_file: str,
    handler_def_line: int,
    src_root: str,
    known_types: frozenset | None = None,
) -> str | None:
    """
    Return the payment operation type dispatched by the handler (e.g. "PaymentCreate").

    Primary pass: Tree-sitter structural scan (_ts_find_handler_op_type).
    Fallback: regex scan of up to 200 lines from def_line.

    Step B is specifically about the payments dispatch layer — handlers explicitly
    name their Op type as `payments::PaymentXxx` in their source body.  Using all
    2696 known codebase types here would cause false mismatches (e.g. constraint is
    "Stripe" but handler body names "PaymentCreate").  So we always use the
    payments-specific regex, and only validate against known_types when provided.
    """
    if not handler_file:
        return None
    abs_path = os.path.join(src_root, handler_file)

    # Primary: Tree-sitter structural scan
    ts_result = _ts_find_handler_op_type(abs_path, handler_def_line, known_types)
    if ts_result is not None:
        return ts_result

    # Fallback: regex text scan
    try:
        with open(abs_path, errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return None

    start = max(0, handler_def_line - 1)
    body = "".join(lines[start : start + 200])

    # Handlers always name their Op type via the payments module path.
    # The regex sees both turbofish types (PaymentData) and positional args
    # (PaymentCapture). Prefer the positional arg op type by skipping data/response
    # struct names that appear in the turbofish but are never the operation type.
    _data_suffixes = ("Data", "Response", "Request", "Types")
    first_fallback: str | None = None
    for m in re.finditer(r"payments::(?:operations::)?(\w+)", body):
        name = m.group(1)
        is_match = (
            (known_types and name in known_types)
            or (not known_types and bool(re.match(r"^(Payment|Complete)", name)))
        )
        if not is_match:
            continue
        if not any(name.endswith(s) for s in _data_suffixes):
            return name                    # clean op type — return immediately
        if first_fallback is None:
            first_fallback = name          # data struct — keep as last resort
    return first_fallback

# ── Step 1: Tag endpoint handlers in Neo4j ────────────────────────────────────

def _parse_routes_app(filepath: str) -> list[dict]:
    """
    Parse routes/app.rs → list of {method, path, handler}.

    Actix registration pattern:
      web::scope("/payments")
        .service(
          web::resource("/{id}/confirm")
            .route(web::post().to(payments_confirm))
        )
    """
    try:
        with open(filepath, errors="replace") as f:
            content = f.read()
    except OSError:
        return []

    scope_re  = re.compile(r'web::scope\(\s*"([^"]*)"\s*\)')
    res_re    = re.compile(r'web::resource\(\s*"([^"]*)"\s*\)')
    route_re  = re.compile(r'web::([a-z]+)\(\)\s*\.to\(([a-zA-Z_][a-zA-Z0-9_:]*)\)')

    lines  = content.splitlines()
    tokens = []
    for i, line in enumerate(lines):
        for m in scope_re.finditer(line):
            tokens.append((i, "scope", m.group(1)))
        for m in res_re.finditer(line):
            tokens.append((i, "resource", m.group(1)))
        for m in route_re.finditer(line):
            tokens.append((i, "route", (m.group(1).upper(), m.group(2).split("::")[-1])))

    scope_stack:    list = []   # [(indent, path_prefix)]
    pending_resource = None
    routes: list = []

    for _, (line_idx, ttype, tval) in enumerate(tokens):
        indent = len(lines[line_idx]) - len(lines[line_idx].lstrip())
        scope_stack = [(ind, p) for ind, p in scope_stack if ind < indent]

        if ttype == "scope":
            scope_stack.append((indent, tval))
            pending_resource = None
        elif ttype == "resource":
            pending_resource = tval
        elif ttype == "route":
            method, handler = tval
            prefix        = "".join(p for _, p in scope_stack)
            resource_path = pending_resource or ""
            full_path     = re.sub(r"/+", "/", prefix.rstrip("/") + "/" + resource_path.lstrip("/"))
            if not full_path.startswith("/"):
                full_path = "/" + full_path
            full_path = full_path.rstrip("/") or "/"
            # Skip v2 endpoints — any path segment equal to "v2" marks a v2-only route.
            # This covers both /v2/payments/... (scope-level) and
            # /payments/v2/filter (resource-level).
            path_parts = [p for p in full_path.split("/") if p]
            if "v2" in path_parts:
                continue
            routes.append({"method": method, "path": full_path, "handler": handler})

    return routes


def tag_endpoints(src_root: str, driver) -> int:
    """
    Parse routes/app.rs and set  is_endpoint / http_method / http_path
    on the matching :Fn nodes.  Safe to call multiple times (idempotent).
    """
    routes_file = os.path.join(src_root, "crates", "router", "src", "routes", "app.rs")
    if not os.path.exists(routes_file):
        print(f"  Warning: routes/app.rs not found at {routes_file}", file=sys.stderr)
        return 0

    all_routes = _parse_routes_app(routes_file)

    # Deduplicate: keep first registration per handler name
    by_handler: dict = {}
    for r in all_routes:
        if r["handler"] not in by_handler:
            by_handler[r["handler"]] = r

    print(f"  {len(all_routes)} route entries → {len(by_handler)} unique handlers", file=sys.stderr)

    tagged = 0
    with driver.session() as s:
        # Clear stale endpoint tags from previous runs so v2-only handlers
        # that no longer appear in v1 routes don't carry over.
        s.run("""
            MATCH (fn:Fn)
            WHERE fn.is_endpoint = true
            REMOVE fn.is_endpoint, fn.http_method, fn.http_path
        """)
        for handler, info in by_handler.items():
            row = s.run("""
                MATCH (fn:Fn {name: $name})
                SET fn.is_endpoint = true,
                    fn.http_method = $method,
                    fn.http_path   = $path
                RETURN count(fn) AS cnt
            """, name=handler, method=info["method"], path=info["path"]).single()
            if row:
                tagged += row["cnt"]

    print(f"  tagged {tagged} endpoint handler nodes", file=sys.stderr)
    return tagged


# ── Step 2: Load full call graph into memory ──────────────────────────────────

def load_graph(driver) -> tuple[dict, dict, dict]:
    """
    Pull every :Fn node and every [:CALLS] edge (with guard annotations) from
    Neo4j into Python dicts for fast in-memory BFS.

    Also loads [:IMPLEMENTS] edges built by build_trait_map.py.

    Returns:
      fn_info    : {symbol → {name, file, def_line, is_endpoint, ..., impl_spec_key}}
      reverse    : {callee_sym → [(caller_sym, guard_type, guard_condition, type_param, match_arm_pattern)]}
      implements : {concrete_sym → (abstract_sym, SpecConstraint)}
    """
    fn_info: dict = {}
    reverse: dict = {}
    implements: dict = {}

    with driver.session() as s:
        print("  loading function nodes …", file=sys.stderr)
        rows = s.run("""
            MATCH (fn:Fn)
            RETURN fn.symbol          AS sym,
                   fn.name            AS name,
                   fn.file            AS file,
                   fn.def_line        AS def_line,
                   fn.is_endpoint     AS is_endpoint,
                   fn.http_method     AS http_method,
                   fn.http_path       AS http_path,
                   fn.impl_trait      AS impl_trait,
                   fn.impl_type       AS impl_type,
                   fn.impl_trait_args AS impl_trait_args,
                   fn.impl_method     AS impl_method,
                   fn.impl_spec_key   AS impl_spec_key
        """).data()
        for r in rows:
            fn_info[r["sym"]] = {
                "name":            r["name"],
                "file":            r["file"],
                "def_line":        r["def_line"],
                "is_endpoint":     bool(r.get("is_endpoint")),
                "http_method":     r.get("http_method"),
                "http_path":       r.get("http_path"),
                "impl_trait":      r.get("impl_trait"),
                "impl_type":       r.get("impl_type"),
                "impl_trait_args": r.get("impl_trait_args"),
                "impl_method":     r.get("impl_method"),
                "impl_spec_key":   r.get("impl_spec_key"),
            }

        print("  loading CALLS edges …", file=sys.stderr)
        rows = s.run("""
            MATCH (a:Fn)-[r:CALLS]->(b:Fn)
            RETURN a.symbol              AS caller,
                   b.symbol              AS callee,
                   r.guard_type          AS guard_type,
                   r.guard_condition     AS guard_condition,
                   r.match_arm_pattern   AS match_arm_pattern,
                   r.type_param          AS type_param
        """).data()
        for r in rows:
            callee = r["callee"]
            if callee not in reverse:
                reverse[callee] = []
            reverse[callee].append((
                r["caller"],
                r.get("guard_type"),
                r.get("guard_condition"),
                r.get("type_param"),          # concrete type from turbofish/struct-as-arg
                r.get("match_arm_pattern"),   # specific match arm pattern (e.g. "Connector::Adyen")
            ))

        print("  loading IMPLEMENTS edges …", file=sys.stderr)
        try:
            rows = s.run("""
                MATCH (c:Fn)-[:IMPLEMENTS]->(a:Fn)
                RETURN c.symbol          AS csym,
                       a.symbol          AS asym,
                       c.impl_type       AS itype,
                       c.impl_trait      AS itrait,
                       c.impl_trait_args AS itrait_args,
                       c.impl_method     AS imethod
            """).data()
            for r in rows:
                if not (r.get("csym") and r.get("asym")):
                    continue
                raw_args   = r.get("itrait_args") or ""
                args_tuple = tuple(a for a in raw_args.split(",") if a)
                spec = SpecConstraint(
                    impl_type  = r.get("itype"),
                    trait_name = r.get("itrait"),
                    trait_args = args_tuple,
                    method     = r.get("imethod"),
                )
                implements[r["csym"]] = (r["asym"], spec)
            print(f"  {len(implements):,} IMPLEMENTS edges loaded (specialization-aware)", file=sys.stderr)
        except Exception as e:
            print(
                f"  IMPLEMENTS edges not found ({e}) — run build_trait_map.py",
                file=sys.stderr,
            )

    print(
        f"  {len(fn_info):,} nodes  |  {sum(len(v) for v in reverse.values()):,} reverse edges",
        file=sys.stderr,
    )
    return fn_info, reverse, implements


def load_trait_map(driver) -> dict:
    """
    Load the trait impl map written by build_trait_map.py.

    Returns:
      trait_map : {trait_name → frozenset of concrete type names}
      e.g. {"GetTrackers": frozenset({"PaymentConfirm", "PaymentUpdate", ...})}

    Also returns a flat set of ALL concrete types (for handler op-type scanning).
    Falls back gracefully if build_trait_map.py has not been run yet.
    """
    trait_map: dict = {}
    try:
        with driver.session() as s:
            rows = s.run("""
                MATCH (fn:Fn)
                WHERE fn.impl_trait IS NOT NULL AND fn.impl_type IS NOT NULL
                RETURN fn.impl_trait AS trait, fn.impl_type AS type
            """).data()
        for r in rows:
            trait_map.setdefault(r["trait"], set()).add(r["type"])
        # Convert inner sets to frozensets for safe sharing
        trait_map = {k: frozenset(v) for k, v in trait_map.items()}
        all_types = frozenset(t for types in trait_map.values() for t in types)
        print(
            f"  trait map: {len(trait_map)} traits  |  {len(all_types)} unique concrete types",
            file=sys.stderr,
        )
    except Exception:
        all_types = frozenset()
        print(
            "  trait map not found — run build_trait_map.py for better type inference",
            file=sys.stderr,
        )
    return trait_map, all_types


# ── Step 3: Trait dispatch — structural analysis (Tree-sitter) ────────────────

_TS_RUST_LANG = Language(_tsrust.language())
_ts_parser    = Parser(_TS_RUST_LANG)
_ts_cache: dict = {}  # abs_path → (mtime, tree, src_bytes)


def _ts_parse_file(abs_path: str):
    """Return (tree, src_bytes) for a Rust file, cached by mtime."""
    try:
        mtime = os.path.getmtime(abs_path)
    except OSError:
        return None, None
    if abs_path in _ts_cache and _ts_cache[abs_path][0] == mtime:
        return _ts_cache[abs_path][1], _ts_cache[abs_path][2]
    try:
        src = open(abs_path, "rb").read()
    except OSError:
        return None, None
    tree = _ts_parser.parse(src)
    _ts_cache[abs_path] = (mtime, tree, src)
    return tree, src


def _ts_text(node: TSNode, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", errors="replace").strip()


def _ts_impl_type_name(impl_node: TSNode, src: bytes) -> str | None:
    """Extract the concrete type name from an impl_item node (the T in impl Trait for T)."""
    type_node = impl_node.child_by_field_name("type")
    if type_node is None:
        return None
    text = _ts_text(type_node, src)
    # Strip generic parameters and take the last path segment
    base = text.split("<")[0].rsplit("::", 1)[-1].strip()
    return base or None


def _ts_impl_has_call(node: TSNode, src: bytes, fn_name: str) -> bool:
    """
    Return True if the subtree rooted at `node` contains a call to `fn_name`.
    Matches call_expression where the function name ends with fn_name,
    and method_call_expression where the method name == fn_name.
    """
    fn_bytes = fn_name.encode()
    # Quick pre-check: byte scan before the full AST walk
    if fn_bytes not in src[node.start_byte : node.end_byte]:
        return False

    def _walk(n: TSNode) -> bool:
        if n.type == "call_expression":
            fn_node = n.child_by_field_name("function")
            if fn_node is not None:
                fn_text = _ts_text(fn_node, src).rsplit("::", 1)[-1]
                if fn_text == fn_name:
                    return True
        elif n.type == "method_call_expression":
            m_node = n.child_by_field_name("method")
            if m_node is not None and _ts_text(m_node, src) == fn_name:
                return True
        for child in n.named_children:
            if _walk(child):
                return True
        return False

    return _walk(node)


def _ts_find_impl_items(node: TSNode):
    """Yield all impl_item nodes in the tree."""
    if node.type == "impl_item":
        yield node
    for child in node.named_children:
        yield from _ts_find_impl_items(child)


def _collect_relevant_files(target_syms: list[str], fn_info: dict, reverse: dict) -> set[str]:
    """
    Return relative file paths likely to contain concrete trait impl blocks
    that are direct callers of the target function.
    """
    files: set[str] = set()
    for tsym in target_syms:
        for entry in reverse.get(tsym, []):
            caller_sym = entry[0]
            name = fn_info.get(caller_sym, {}).get("name", "")
            if len(name.split("#")) >= 3:   # concrete impl: T#Trait#method
                f = fn_info.get(caller_sym, {}).get("file")
                if f:
                    files.add(f)
    # Fallback: include any caller file if no impl callers found
    if not files:
        for tsym in target_syms:
            for entry in reverse.get(tsym, []):
                f = fn_info.get(entry[0], {}).get("file")
                if f:
                    files.add(f)
    return files


def step_a_scan_impl_blocks(
    fn_name: str,
    relevant_files: set[str],
    src_root: str,
    known_types: frozenset | None = None,
) -> set[str]:
    """
    Step A (Structural) — Tree-sitter scan.

    For each file in relevant_files, parse with Tree-sitter and find all
    `impl Trait for T` blocks whose method body calls fn_name.

    If known_types is provided (from the trait map in Neo4j), only concrete
    types in that set are returned.  When None, all discovered types are
    returned — no hardcoded filtering.

    Returns: set of concrete type names (e.g. {"PaymentConfirm", "PaymentUpdate"}).
    """
    concrete_types: set[str] = set()
    for rel_file in relevant_files:
        abs_path = os.path.join(src_root, rel_file)
        tree, src = _ts_parse_file(abs_path)
        if tree is None:
            continue
        for impl_node in _ts_find_impl_items(tree.root_node):
            # Only consider `impl Trait for T` (has a trait field), not bare `impl T`
            if impl_node.child_by_field_name("trait") is None:
                continue
            body = impl_node.child_by_field_name("body")
            if body is None:
                continue
            if _ts_impl_has_call(body, src, fn_name):
                t = _ts_impl_type_name(impl_node, src)
                if t and (known_types is None or t in known_types):
                    concrete_types.add(t)
    return concrete_types


# ── Step 4: BFS upward from target function ────────────────────────────────────

def _find_symbols(fn_name: str, fn_info: dict) -> list[str]:
    """
    Return all symbols whose display name matches fn_name.

    Match order (first non-empty set wins):
      1. Exact match           — "get_connector_data_with_routing_decision"
      2. Abstract trait method — name == "Trait#fn_name" (e.g. "GetTracker#get_trackers")
      3. All concrete impls    — name ends with "#fn_name"  (e.g. "T#GetTracker#get_trackers")

    This lets callers pass bare method names like "get_trackers" and still resolve
    to the right set of symbols.
    """
    # 1. exact
    exact = [sym for sym, info in fn_info.items() if info["name"] == fn_name]

    # 2. abstract trait method (two-segment name ending with #fn_name)
    abstract = [
        sym for sym, info in fn_info.items()
        if info["name"].endswith(f"#{fn_name}") and info["name"].count("#") == 1
    ]

    # If both exist, return combined — e.g. get_or_create_customer_details exists
    # as a standalone payouts function AND as a trait method Domain#get_or_create_customer_details.
    # BFS must start from both so all paths are explored.
    if exact and abstract:
        return exact + abstract
    if exact:
        return exact
    if abstract:
        return abstract

    # 3. all concrete impls (three-segment name ending with #fn_name)
    return [
        sym for sym, info in fn_info.items()
        if info["name"].endswith(f"#{fn_name}") and info["name"].count("#") == 2
    ]


def check_path_feasibility(guards: list[dict]) -> tuple[bool, str | None]:
    """
    Return (is_feasible, reason_or_None).

    Detects obviously contradictory guards on the same path:

    Rule 1 — match contradiction:
      If the same match scrutinee text appears in two match guards on this path,
      the path requires the same value to satisfy two separate match arms
      simultaneously. Flag as infeasible with a reason string.
      (This catches cases like `req.payment_type` being matched twice on the path.)

    Rule 2 — if contradiction:
      If a condition text C appears in an 'if' guard AND the same text prefixed
      with '!' appears in another 'if' guard (or vice versa), it's contradictory.

    Returns (True, None) if no contradiction found.
    """
    # Rule 1: group match guards by their scrutinee (condition text)
    match_scrutinees: dict[str, int] = {}
    for g in guards:
        if g.get("guard_type") == "match":
            scrutinee = g.get("condition", "").strip()
            if scrutinee:
                match_scrutinees[scrutinee] = match_scrutinees.get(scrutinee, 0) + 1
                if match_scrutinees[scrutinee] >= 2:
                    return False, f"match contradiction: scrutinee '{scrutinee}' appears in multiple match arms on this path"

    # Rule 2: if/if_let condition contradiction
    if_conditions: set[str] = set()
    for g in guards:
        if g.get("guard_type") in ("if", "if_let"):
            cond = g.get("condition", "").strip()
            if cond:
                if_conditions.add(cond)

    for cond in if_conditions:
        negated_forms = (f"!({cond})", f"! {cond}", f"!{cond}")
        for neg in negated_forms:
            if neg in if_conditions:
                return False, f"if contradiction: '{cond}' and its negation both appear on this path"
        # Also check if this condition IS a negation of something else present
        for other in if_conditions:
            if other == cond:
                continue
            if cond in (f"!({other})", f"! {other}", f"!{other}"):
                return False, f"if contradiction: '{other}' and its negation both appear on this path"

    return True, None


def bfs_upward(
    target_syms: list[str],
    fn_info: dict,
    reverse: dict,
    concrete_types_step_a: set[str],
    src_root: str,
    known_types: frozenset | None = None,
    max_depth: int = 8,
    implements: dict | None = None,
    seed_spec: SpecConstraint | None = None
) -> list[dict]:
    """
    BFS upward through the reverse CALLS graph with type-constrained trait dispatch.

    State per queue entry:
      chain           : [endpoint_sym, …, intermediate_sym, target_sym]
      guards          : conditional guards accumulated along the path
      depth           : hops from target
      spec : SpecConstraint | None
               None   = no concrete impl crossed yet on this path
               set    = full (or partial) specialization identity of the concrete impl
                        that introduced this path, e.g.
                        SpecConstraint(impl_type="Stripe",
                                       trait_name="ConnectorIntegration",
                                       trait_args=("Authorize","X","Y"),
                                       method="build_request")

    Visited key: (sym, spec) — same node reachable with different specializations
    represents genuinely distinct paths (Stripe+Authorize != Stripe+PSync).

    Trait dispatch bridge (inline):
      When BFS hits a concrete impl node (in `implements` dict or 3-part SCIP name),
      it creates a SpecConstraint from the impl metadata and bridges to callers of the
      abstract trait method, carrying the full specialization forward.

    Step C intersection filter (at endpoint discovery):
      Uses spec.impl_type for backward-compat payment-op filtering when
      concrete_types_step_a is non-empty.
    """
    # Pre-build: abstract method name → [sym, …] for fast bridge lookup
    abstract_name_to_syms: dict = {}
    for sym, info in fn_info.items():
        name = info["name"]
        if len(name.split("#")) == 2:   # Trait#method (abstract dispatch target)
            abstract_name_to_syms.setdefault(name, []).append(sym)

    # Build the set of known concrete connector impl_types from IMPLEMENTS edges.
    # Only types present here are real connector names (e.g. "Adyen", "Stripe").
    # Framework wrappers like "ConnectorIntegrationEnum" or "RouterData" are NOT
    # in this set, so connector-consistency checks are not applied to them.
    # We restrict to impl_trait == "ConnectorIntegration" — the core connector
    # trait — which excludes dispatcher enums (ConnectorIntegrationEnum implements
    # ConnectorIntegrationInterface, not ConnectorIntegration directly).
    _known_connector_types: frozenset = frozenset(
        spec.impl_type
        for _sym, (_abs, spec) in (implements or {}).items()
        if spec.impl_type and spec.trait_name == "ConnectorIntegration"
    )

    # Memoised Op-type lookup (avoids re-reading files)
    _op_cache: dict = {}

    def _get_op_type(h_name: str, h_file: str, h_line: int) -> str | None:
        key = (h_file, h_line)
        if key not in _op_cache:
            _op_cache[key] = extract_handler_op_type(h_file, h_line, src_root, known_types)
        return _op_cache[key]

    # visited: set of (sym, spec) pairs — SpecConstraint is frozen/hashable
    visited: set = set()
    for sym in target_syms:
        visited.add((sym, seed_spec))

    # queue entries: (symbol, chain_so_far, guards_so_far, depth, spec: SpecConstraint|None)
    queue: deque = deque()
    for sym in target_syms:
        queue.append((sym, [sym], [], 0, seed_spec))

    endpoints_found: list[dict] = []

    while queue:
        current, chain, guards, depth, spec = queue.popleft()

        if depth >= max_depth:
            continue

        current_name = fn_info.get(current, {}).get("name", "")
        parts = current_name.split("#")

        # ── Collect callers: direct SCIP edges + optional trait-dispatch bridge ──
        callers_to_process: list = list(reverse.get(current, []))

        if len(parts) == 3 or (implements and current in implements):
            # Concrete impl node: bridge to callers of the abstract method.
            bridged_callers: set = set()
            # NEW — use Neo4j [:IMPLEMENTS] edges (built by build_trait_map.py)
            # Fall back to SCIP name heuristic if not found
            if implements and current in implements:
                abstract_sym, impl_spec = implements[current]
                # Connector consistency: if this impl's connector conflicts with the
                # connector already locked for this path, discard the branch entirely.
                # Only enforce when impl_spec.impl_type is a known concrete connector
                # (not a framework wrapper like ConnectorIntegrationEnum).
                if (spec is not None and spec.impl_type and impl_spec.impl_type
                        and spec.impl_type != impl_spec.impl_type
                        and impl_spec.impl_type in _known_connector_types):
                    continue
                # Reinforce: merge incoming spec with the impl's known specialization
                spec = impl_spec if spec is None else spec
                for item in reverse.get(abstract_sym, []):
                    if item[0] not in bridged_callers:
                        bridged_callers.add(item[0])
                        callers_to_process.append(item)
            elif len(parts) == 3:
                # Connector consistency: if this path already has a locked connector
                # and this concrete node belongs to a different one, discard the branch.
                # Only enforce when parts[0] is a known concrete connector type.
                if (spec is not None and spec.impl_type and spec.impl_type != parts[0]
                        and parts[0] in _known_connector_types):
                    continue
                # fallback: SCIP name heuristic — build SpecConstraint from name
                spec = SpecConstraint(
                    impl_type  = parts[0],
                    trait_name = parts[1],
                    method     = parts[2],
                )
                abstract_name = f"{parts[1]}#{parts[2]}"
                for abs_sym in abstract_name_to_syms.get(abstract_name, []):
                    for item in reverse.get(abs_sym, []):
                        if item[0] not in bridged_callers:
                            bridged_callers.add(item[0])
                            callers_to_process.append(item)

        for caller_sym, guard_type, guard_condition, *rest in callers_to_process:
            edge_type_param    = rest[0] if len(rest) > 0 else None
            match_arm_pattern  = rest[1] if len(rest) > 1 else None

            # Type-param mismatch prune: the CALLS edge records the concrete operation
            # type T that was flowing at this call site (extracted by build_trait_map.py
            # from struct literals, constructor calls, or explicit type annotations on
            # let bindings, e.g. `let rd: RouterData<IncrementalAuthorization, ..>`).
            # If T is known here and conflicts with the operation type we are tracing
            # (spec.trait_args[0]), this caller could never reach the changed function
            # via the operation in question — prune the path.
            # "_" is a Rust wildcard type inference placeholder — treat as unknown,
            # never prune on it (avoids false negatives on turbofish with `_` args).
                       # Skip the prune when spec.trait_args[0] is a short generic placeholder
            # like "F", "T", "Op", "D" (length ≤ 2) — these are formal type parameters
            # in the trait definition (e.g. GetTracker<F, PaymentData, ...>), not
            # concrete types.  Comparing a concrete edge type_param ("Authorize") with
            # a placeholder ("F") is always false and would incorrectly prune every
            # caller of functions whose spec was derived from such a generic impl.

            if (edge_type_param
                    and edge_type_param != "_"
                    and spec is not None
                    and spec.trait_args
                    and len(spec.trait_args[0]) > 2
                    and edge_type_param != spec.trait_args[0]):
                continue

            # If the edge carries a concrete type_param (from turbofish/struct-as-arg
            # recorded by build_trait_map.py), use it to build a minimal SpecConstraint
            # when none has been established yet on this path.
            if edge_type_param and spec is None:
                effective_spec: SpecConstraint | None = SpecConstraint(impl_type=edge_type_param)
            else:
                effective_spec = spec

            # Match-arm connector prune:
            # If the call is inside a match arm whose pattern is an explicit list of
            # Connector::Xyz variants (e.g. "Connector::Stripe | Connector::Paypal"),
            # and we have a locked connector for this path (spec.impl_type), check
            # whether our connector is among those variants.
            # This works for ANY match scrutinee (connector_name, connector_type, etc.)
            # as long as the arm uses Connector::Xyz enum variants.
            # Wildcard arms ("_") are not pruned — reachable by any connector.
            if (match_arm_pattern
                    and guard_type == "match"
                    and effective_spec is not None
                    and effective_spec.impl_type):
                arm_text = match_arm_pattern.lower().strip()
                # Only prune if the arm uses Connector:: variants (not a wildcard or guard)
                if arm_text not in ("_", "..") and "connector::" in arm_text:
                    locked = effective_spec.impl_type.lower()
                    arm_variants = {
                        v.strip().split("::")[-1].lower()
                        for v in re.split(r"[|,]", arm_text)
                        if "::" in v
                    }
                    if arm_variants and locked not in arm_variants:
                        continue

            # Connector consistency: if this caller is a concrete connector-specific node
            # (3-part name like Zen#Trait#method) and the path is already locked to a
            # different connector, reject this edge before it enters the queue.
            # Only enforce when caller's type is a known connector (not a framework
            # wrapper like ConnectorIntegrationEnum or RouterData).
            _caller_name = fn_info.get(caller_sym, {}).get("name", "")
            _caller_parts = _caller_name.split("#")
            if (len(_caller_parts) == 3
                    and effective_spec is not None
                    and effective_spec.impl_type
                    and _caller_parts[0] != effective_spec.impl_type
                    and _caller_parts[0] in _known_connector_types):
                continue

            visit_key = (caller_sym, effective_spec)
            if visit_key in visited:
                continue
            # Don't mark endpoints as visited — they are never added to the queue
            # so there's no loop risk.  Keeping them out of visited lets step 5
            # discover the same endpoint via multiple paths and pick the best chain
            # (shortest / no false SCIP edge).  Non-endpoints must still be marked
            # to avoid exponential revisits through the call graph.
            info = fn_info.get(caller_sym, {})
            if not info.get("is_endpoint"):
                visited.add(visit_key)

            # Accumulate guard if the edge to this caller is conditional
            new_guards = guards
            if guard_type:
                callee_name = fn_info.get(current, {}).get("name", current.rsplit("/", 1)[-1])
                caller_name = fn_info.get(caller_sym, {}).get("name", caller_sym.rsplit("/", 1)[-1])
                new_guards = guards + [{
                    "at_hop":      f"{caller_name} → {callee_name}",
                    "guard_type":  guard_type,
                    "condition":   guard_condition or "",
                }]

            new_chain = [caller_sym] + chain

            if info.get("is_endpoint"):
                # ── Step C: intersection filter ───────────────────────────────
                constraint_type = effective_spec.impl_type if effective_spec else None
                ep_name = info.get("name", "?")
                if constraint_type is not None and concrete_types_step_a:
                    # (a) Step A check: was this concrete type confirmed by Tree-sitter?
                    if constraint_type not in concrete_types_step_a:
                        continue
                    # (b) Step B check: does this handler actually use that Op type?
                    # Only applies for known connector types (Adyen, Stripe, etc.).
                    # Non-connector concrete types like "PaymentResponse" are framework
                    # structs — extract_handler_op_type returns payment operation type
                    # names (PaymentsAuthorize, PaymentConfirm, …) not struct names,
                    # so comparing against a non-connector type always mismatches and
                    # incorrectly drops every endpoint.
                    if constraint_type in _known_connector_types:
                        op_type = _get_op_type(
                            info.get("name", ""),
                            info.get("file", ""),
                            info.get("def_line", 1),
                        )
                        if op_type is not None and op_type != constraint_type:
                            continue

                ep_dict = {
                    "method":     info.get("http_method", "?"),
                    "path":       info.get("http_path",   "?"),
                    "handler":    info.get("name", ""),
                    "call_chain": [fn_info.get(s, {}).get("name", s) for s in new_chain],
                    "guards":     new_guards,
                    "depth":      depth + 1,
                    "_type_constraint": constraint_type,
                }
                # Attach full specialization context when available
                if effective_spec and effective_spec.spec_key():
                    ep_dict["specialization"] = effective_spec.to_dict(_known_connector_types)
                feasible, reason = check_path_feasibility(new_guards)
                if not feasible:
                    ep_dict["infeasible_reason"] = reason
                    ep_dict["likely_infeasible"] = True
                # Always add — don't discard, just annotate
                endpoints_found.append(ep_dict)
                continue

            queue.append((caller_sym, new_chain, new_guards, depth + 1, effective_spec))

    return endpoints_found


# ── Location-anchored seed resolution ─────────────────────────────────────────

def resolve_seed_from_location(
    src_root: str,
    rel_file: str,
    line: int,
    fn_info: dict,
) -> tuple[list[str], SpecConstraint | None]:
    """
    Given a file path + line number, find the enclosing :Fn node in fn_info
    and extract its full specialization (if it's a concrete trait impl method).

    Returns:
      (target_syms, spec_constraint)
      where target_syms are the symbols for the function at that location,
      and spec_constraint is a SpecConstraint built from impl metadata (or None).
    """
    # Normalize: strip leading slash if present
    rel_file = rel_file.lstrip("/")

    # Find all :Fn nodes in the same file — pick the one whose def_line is
    # the closest line <= the requested line (innermost enclosing function).
    candidates = []
    for sym, info in fn_info.items():
        f = (info.get("file") or "").lstrip("/")
        if f != rel_file:
            continue
        dl = info.get("def_line") or 0
        if dl <= line:
            candidates.append((dl, sym))

    if not candidates:
        return [], None

    # Sort by def_line descending — the largest def_line <= line is the
    # innermost enclosing function.
    candidates.sort(key=lambda x: -x[0])
    _, best_sym = candidates[0]
    best_info = fn_info[best_sym]

    target_syms = [best_sym]

    # Build SpecConstraint from impl metadata stored on the node
    itype  = best_info.get("impl_type")
    itrait = best_info.get("impl_trait")
    iargs  = best_info.get("impl_trait_args") or ""
    imethod = best_info.get("impl_method")

    if itype or itrait:
        args_tuple = tuple(a.strip() for a in iargs.split(",") if a.strip())
        spec = SpecConstraint(
            impl_type  = itype,
            trait_name = itrait,
            trait_args = args_tuple,
            method     = imethod,
        )
    else:
        spec = None

    return target_syms, spec


# ══════════════════════════════════════════════════════════════════════════════
# FLOW BUILDER LAYER  ── new output shape built on top of BFS results
# ══════════════════════════════════════════════════════════════════════════════

# ── Source reading helpers ────────────────────────────────────────────────────

def _read_file(src_root: str, filepath: str) -> list[str]:
    """Read a source file relative to src_root and return its lines."""
    if not src_root or not filepath:
        return []
    try:
        with open(os.path.join(src_root, filepath.lstrip("/")), errors="replace") as f:
            return f.readlines()
    except OSError:
        return []


def _find_fn_start(lines: list[str], def_line: int | None, fn_name: str) -> int:
    """Return the 0-based line index where `fn fn_name` is defined."""
    hint = max(0, (def_line or 1) - 1)

    def _matches(l: str) -> bool:
        return f"fn {fn_name}(" in l or f"fn {fn_name}<" in l

    for i in range(max(0, hint - 50), min(len(lines), hint + 50)):
        if _matches(lines[i]):
            return i
    for i in range(len(lines)):
        if _matches(lines[i]):
            return i
    return hint


# ── Name → fn_info index ──────────────────────────────────────────────────────

def _build_name_index(fn_info: dict) -> dict:
    """name → best fn_info entry (prefer crates/router over test/openapi)."""
    idx: dict = {}
    for info in fn_info.values():
        name = info.get("name", "")
        if not name:
            continue
        file = info.get("file") or ""
        score = 0 if ("test" in file or "openapi" in file) else 1
        existing = idx.get(name)
        if existing is None:
            idx[name] = info
        else:
            ex_score = 0 if ("test" in (existing.get("file") or "") or "openapi" in (existing.get("file") or "")) else 1
            if score > ex_score:
                idx[name] = info
    return idx


# ── Chain node builder ────────────────────────────────────────────────────────

def _build_chain_nodes(ep: dict, name_idx: dict, src_root: str) -> list[dict]:
    """
    Build structured chain nodes from a BFS endpoint result.

    chain[0]  = handler (entry point / route handler)
    chain[-1] = target (the changed function)
    chain[-2] = direct caller of the target

    Each node gets: function, file, def_line, role, condition (non-target).
    Direct caller and handler also get full_source when src_root is available.
    """
    chain_names = ep.get("call_chain", [])
    guards      = ep.get("guards", [])
    n           = len(chain_names)

    # (caller_name, callee_name) → guard dict  for fast lookup
    guard_map: dict = {}
    for g in guards:
        hop_str = g.get("at_hop", "")
        if " → " in hop_str:
            caller_n, callee_n = hop_str.split(" → ", 1)
            guard_map[(caller_n.strip(), callee_n.strip())] = g

    nodes: list[dict] = []

    for i, fn_name in enumerate(chain_names):
        info      = name_idx.get(fn_name, {})
        is_target = (i == n - 1)
        is_direct = (i == n - 2)
        is_entry  = (i == 0)

        role = (
            "target"        if is_target else
            "direct_caller" if is_direct else
            "handler"       if is_entry  else
            "intermediate"
        )

        node: dict = {
            "function": fn_name,
            "file":     info.get("file"),
            "def_line": info.get("def_line"),
            "role":     role,
        }

        if not is_target:
            next_name = chain_names[i + 1] if i + 1 < n else ""
            guard     = guard_map.get((fn_name, next_name))

            node["condition"] = (
                {
                    "type":           guard["guard_type"],
                    "text":           guard["condition"],
                    "condition_line": None,
                    "confidence":     "high",
                }
                if guard else
                {
                    "type":           "unconditional",
                    "text":           "unconditional",
                    "condition_line": None,
                    "confidence":     "high",
                }
            )

            # Include full source for the handler and direct caller
            if (is_direct or is_entry) and src_root and info.get("file"):
                try:
                    lines = _read_file(src_root, info["file"])
                    if lines:
                        bs = _find_fn_start(lines, info.get("def_line"), fn_name)
                        depth_c, end = 0, bs
                        for j in range(bs, min(len(lines), bs + 300)):
                            depth_c += lines[j].count("{") - lines[j].count("}")
                            end = j
                            if depth_c <= 0 and j > bs:
                                break
                        node["full_source"] = "".join(lines[bs:end + 1])
                except Exception:
                    pass

        nodes.append(node)
    return nodes


# ── Prerequisite extraction ───────────────────────────────────────────────────

_PROFILE_FIELD_RE = re.compile(
    r'\b(?:business_profile|profile|merchant_account|connector_config)'
    r'\.([a-z][a-z_0-9]+)'
)
_REQ_FIELD_RE = re.compile(
    r'(?:req|request)\s*\.\s*(?:get_)?([a-z][a-z_0-9]+)'
)


def _extract_prereq_fields(chain_nodes: list[dict]) -> dict:
    """Extract struct.field references from condition texts in chain nodes."""
    fields: dict = {}
    for node in chain_nodes:
        if node.get("role") == "target":
            continue
        text = node.get("condition", {}).get("text", "")
        for m in _PROFILE_FIELD_RE.finditer(text):
            fields[m.group(1)] = text
    return fields


def _rule1_find_toggle_endpoint(fn_info: dict, field_name: str) -> dict | None:
    """Rule 1: profile field → look for a toggle endpoint in fn_info."""
    fragment = field_name
    for prefix in ("is_", "enable_", "enabled_"):
        if fragment.startswith(prefix):
            fragment = fragment[len(prefix):]
    for suffix in ("_enabled", "_enable"):
        if fragment.endswith(suffix):
            fragment = fragment[:-len(suffix)]
    frag_dash = fragment.replace("_", "-")

    for info in fn_info.values():
        if not info.get("is_endpoint"):
            continue
        path   = info.get("http_path") or ""
        method = info.get("http_method") or ""
        if method not in ("POST", "PUT", "PATCH"):
            continue
        if ("profile" in path or "account" in path) and (fragment in path or frag_dash in path):
            return {
                "kind":            "profile_config",
                "field":           field_name,
                "config_endpoint": f"{method} {path}",
                "config_value":    {field_name: True},
                "confidence":      "high",
                "rule":            "toggle_endpoint",
            }
    return None


def _rule2_find_profile_update(fn_info: dict, field_name: str, src_root: str) -> dict | None:
    """Rule 2: field in a Profile*/Update* struct → find a profile update endpoint."""
    if not src_root:
        return None
    fpath = os.path.join(src_root, "crates", "api_models", "src", "admin.rs")
    if not os.path.exists(fpath):
        return None
    struct_re = re.compile(r"\s*(?:pub\s+)?struct\s+(\w+)")
    current_struct = None
    try:
        for line in open(fpath, errors="replace"):
            m = struct_re.match(line)
            if m:
                current_struct = m.group(1)
            if field_name in line and current_struct and any(
                k in current_struct for k in ("Update", "Create", "Request")
            ):
                # Find best profile endpoint from fn_info
                best = None
                for info in fn_info.values():
                    if not info.get("is_endpoint"):
                        continue
                    path   = info.get("http_path") or ""
                    method = info.get("http_method") or ""
                    if method in ("POST", "PUT", "PATCH") and "profile" in path:
                        if best is None or len(path) > len(best[1]):
                            best = (method, path)
                if best:
                    return {
                        "kind":            "profile_config",
                        "field":           field_name,
                        "config_endpoint": f"{best[0]} {best[1]}",
                        "config_value":    {field_name: True},
                        "confidence":      "high",
                        "rule":            "profile_update_struct",
                        "struct":          current_struct,
                    }
    except OSError:
        pass
    return None


def _extract_request_prereqs(chain_nodes: list[dict]) -> list[dict]:
    """Rule 4 (request-field): extract runtime prerequisites from request.field guards."""
    prereqs: list = []
    seen: set     = set()
    for node in chain_nodes:
        if node.get("role") == "target":
            continue
        text = node.get("condition", {}).get("text", "")
        for m in _REQ_FIELD_RE.finditer(text):
            field = (m.group(1) or "").strip()
            if not field or field in seen or len(field) <= 2:
                continue
            seen.add(field)
            required = ".is_some()" in text or f"get_{field}" in text
            prereqs.append({
                "kind":       "request_field",
                "field":      field,
                "required":   required,
                "reason":     text[:120],
                "confidence": "high" if required else "low",
            })
    return prereqs


def extract_prerequisites_fi(
    chain_nodes: list[dict],
    fn_info: dict,
    src_root: str,
    spec: "SpecConstraint | None" = None,
) -> list[dict]:
    """
    Derive prerequisites from chain conditions.

    Rule 1: profile/config field → find toggle endpoint
    Rule 2: profile update struct field → find update endpoint
    Rule 3: concrete type/specialization → op-type prerequisite
    Rule 4: request field guard → runtime prerequisite
    """
    prereqs:  list = []
    seen_eps: set  = set()

    # Rules 1 & 2
    for field_name, condition_text in _extract_prereq_fields(chain_nodes).items():
        r1 = _rule1_find_toggle_endpoint(fn_info, field_name)
        if r1:
            key = r1["config_endpoint"]
            if key not in seen_eps:
                seen_eps.add(key)
                r1["condition"] = condition_text[:100]
                prereqs.append(r1)
            continue
        r2 = _rule2_find_profile_update(fn_info, field_name, src_root)
        if r2:
            key = r2["config_endpoint"]
            if key not in seen_eps:
                seen_eps.add(key)
                r2["condition"] = condition_text[:100]
                prereqs.append(r2)

    # Rule 3: concrete type / specialization
    if spec and spec.impl_type:
        field_label = "connector" if spec.trait_name == "ConnectorIntegration" else "operation_type"
        prereqs.append({
            "kind":           "concrete_type",
            "field":          field_label,
            "required_value": spec.impl_type,
            "trait":          spec.trait_name,
            "trait_args":     list(spec.trait_args),
            "reason":         f"path only entered via {spec.spec_key() or spec.impl_type}",
            "confidence":     "high",
        })

    # Rule 4: request-field guards
    prereqs.extend(_extract_request_prereqs(chain_nodes))
    return prereqs


# ── Payload generator ─────────────────────────────────────────────────────────

_CARD_DUMMY = {
    "number":    "4111111111111111",
    "exp_month": "12",
    "exp_year":  "2030",
    "cvc":       "123",
}


def generate_flow_payload(flow: dict, src_root: str = "") -> dict:  # noqa: ARG001
    """
    Generate setup_payloads and trigger_payload for a flow.

    setup_payloads : derived from profile/config prerequisites
    trigger_payload: derived from endpoint path + guard conditions
    """
    endpoints   = flow.get("endpoints", [])
    ep          = endpoints[0] if endpoints else {}
    method      = ep.get("method", "POST")
    path        = ep.get("path", "")
    chain_nodes = flow.get("chain", [])
    prereqs     = flow.get("prerequisites", [])
    spec_info   = flow.get("specialization")

    # ── Setup payloads from profile/config prerequisites ──────────────────────
    setup_payloads = []
    for p in prereqs:
        if p.get("kind") == "profile_config" and p.get("config_endpoint"):
            parts     = p["config_endpoint"].split(" ", 1)
            ep_method = parts[0] if parts else "POST"
            ep_path   = parts[1] if len(parts) > 1 else ""
            setup_payloads.append({
                "endpoint": f"{ep_method} {ep_path}",
                "body":     p.get("config_value", {}),
                "note":     f"Enable '{p['field']}' before triggering flow",
            })

    # ── Trigger payload ───────────────────────────────────────────────────────
    body: dict = {}

    # Fields implied by guard conditions
    for node in chain_nodes:
        text = node.get("condition", {}).get("text", "")
        if "customer_id" in text and ("is_some" in text or "get_customer" in text):
            body["customer_id"] = "cust_test_001"
        if "amount" in text and "amount" not in body:
            body["amount"]   = 1000
            body["currency"] = "USD"
        if "mandate" in text:
            body["setup_future_usage"] = "off_session"

    # Path-shape defaults
    if "/payments" in path:
        body.setdefault("amount", 1000)
        body.setdefault("currency", "USD")
        body.setdefault("payment_method", "card")
        body.setdefault("payment_method_data", {"card": _CARD_DUMMY})
    elif "/refunds" in path:
        body.setdefault("payment_id", "pay_test_001")
        body.setdefault("amount", 500)
        body.setdefault("reason", "customer_request")
    elif "/payouts" in path:
        body.setdefault("amount", 1000)
        body.setdefault("currency", "USD")
        body.setdefault("payout_type", "bank")

    # Connector from specialization
    if spec_info and spec_info.get("connector") and spec_info.get("connector") != "None":
        body["connector"] = spec_info["connector"].lower()

    return {
        "setup_payloads":  setup_payloads,
        "trigger_payload": {"endpoint": f"{method} {path}", "body": body},
    }


# ── Flow grouper ──────────────────────────────────────────────────────────────

def build_flows(
    endpoints: list[dict],
    fn_info: dict,
    src_root: str,
    changed_function: str,
    changed_file: str | None,
    changed_line: int | None,
) -> list[dict]:
    """
    Group BFS endpoint results into flows.

    Flow signature: (chain[1:], guard structure)
    Two endpoints belong to the same flow only when they share the same
    intermediate call chain AND the same guard conditions.
    """
    name_idx = _build_name_index(fn_info)

    # ── Group by flow signature ───────────────────────────────────────────────
    sig_map: dict = {}   # sig → (representative_ep, [all_eps])
    for ep in endpoints:
        chain  = ep.get("call_chain", [])
        guards = ep.get("guards", [])
        sig = (
            tuple(chain[1:]),
            tuple((g["at_hop"], g["guard_type"], g["condition"]) for g in guards),
        )
        if sig not in sig_map:
            sig_map[sig] = (ep, [])
        sig_map[sig][1].append(ep)

    flows: list[dict] = []

    for flow_idx, (rep_ep, sharing_eps) in enumerate(sig_map.values(), 1):
        chain_nodes = _build_chain_nodes(rep_ep, name_idx, src_root)

        # Reconstruct SpecConstraint from endpoint specialization dict
        spec_dict = rep_ep.get("specialization")
        spec_obj: SpecConstraint | None = None
        if spec_dict:
            spec_obj = SpecConstraint(
                impl_type  = spec_dict.get("connector"),
                trait_name = spec_dict.get("trait"),
                trait_args = tuple(spec_dict.get("trait_args") or []),
                method     = spec_dict.get("method"),
            )

        prereqs = extract_prerequisites_fi(chain_nodes, fn_info, src_root, spec_obj)

        # Description — derived mechanically from conditions
        high_cond_texts = [
            n["condition"]["text"] for n in chain_nodes
            if n.get("condition", {}).get("confidence") == "high"
            and "unconditional" not in n.get("condition", {}).get("text", "")
        ]
        if high_cond_texts:
            desc_parts = [c.replace("if ", "").replace("if let ", "").strip()[:80]
                          for c in high_cond_texts[:2]]
            description = " + ".join(desc_parts)
        elif spec_obj and spec_obj.spec_key():
            description = f"via {spec_obj.spec_key()}"
        else:
            chain_preview = " → ".join(rep_ep.get("call_chain", [])[:3])
            description   = f"path through {chain_preview}"

        connectors: list[str] = []

        # Deduplicated sharing endpoints
        sharing_out: list[dict] = []
        seen_keys: set = set()
        for e in sharing_eps:
            key = f"{e.get('method')} {e.get('path')}"
            if key not in seen_keys:
                seen_keys.add(key)
                sharing_out.append({
                    "method":  e.get("method", "?"),
                    "path":    e.get("path", "?"),
                    "handler": e.get("handler", ""),
                })

        flow: dict = {
            "flow_id":          flow_idx,
            "description":      description,
            "changed_function": changed_function,
            "changed_file":     changed_file,
            "changed_line":     changed_line,
            "endpoints":        sharing_out,
            "prerequisites":    prereqs,
            "chain":            chain_nodes,
            "connectors":       connectors,
            "connector_count":  len(connectors),
            "specialization":   spec_dict,
            "conditions_high":  sum(
                1 for n in chain_nodes
                if n.get("condition", {}).get("confidence") == "high"
                and "unconditional" not in n.get("condition", {}).get("text", "")
            ),
            "conditions_missing": sum(
                1 for n in chain_nodes
                if n.get("condition", {}).get("confidence") in ("none", "low", None)
                and n.get("role") != "target"
            ),
        }

        payloads = generate_flow_payload(flow, src_root)
        flow["setup_payloads"]  = payloads["setup_payloads"]
        flow["trigger_payload"] = payloads["trigger_payload"]
        flows.append(flow)

    return flows


# ── Reachability matrix ───────────────────────────────────────────────────────

def build_reachability_matrix(
    endpoints: list[dict],
    flows: list[dict],
    changed_function: str,
    changed_file: str | None,
    changed_line: int | None,
) -> list[dict]:
    """One row per endpoint: guard conditions + impact_status."""
    matrix: list[dict] = []

    for ep in endpoints:
        ep_key       = f"{ep.get('method')} {ep.get('path')}"
        guard_conds: list = []
        required_ops: set = set()
        spec_key_out      = None

        for flow in flows:
            if not any(f"{e['method']} {e['path']}" == ep_key for e in flow.get("endpoints", [])):
                continue
            for node in flow.get("chain", []):
                if node.get("role") == "target":
                    continue
                cond = node.get("condition", {})
                text = cond.get("text", "")
                if text and "unconditional" not in text.lower():
                    guard_conds.append({
                        "function":   node["function"],
                        "type":       cond.get("type", "unknown"),
                        "condition":  text[:200],
                        "confidence": cond.get("confidence", "none"),
                    })
            spec_info = flow.get("specialization")
            if spec_info and spec_info.get("connector"):
                spec_key_out = spec_info.get("specialization_key")
                required_ops.add(spec_info["connector"])
            break   # only need first matching flow for guard conditions

        if not guard_conds:
            impact_status = "Impacted"
        elif required_ops:
            impact_status = f"Conditionally Impacted (via {', '.join(sorted(required_ops))})"
        else:
            impact_status = "Conditionally Impacted"

        matrix.append({
            "modified_function":   changed_function,
            "file":                changed_file,
            "line":                changed_line,
            "endpoint":            ep_key,
            "handler":             ep.get("handler", ""),
            "call_chain":          ep.get("call_chain", []),
            "guard_conditions":    guard_conds,
            "impact_status":       impact_status,
            "specialization_key":  spec_key_out,
            "likely_infeasible":   ep.get("likely_infeasible", False),
        })

    return matrix


# ── Main ───────────────────��───────────────────────────────────────────────────

def find_impact(fn_name: str | None, src_root: str, max_depth: int = 8, out_path: str | None = None,
                file_hint: str | None = None, line_hint: int | None = None):

    driver = get_driver()

    # ── 1. Tag endpoints (idempotent) ─────────────────────────────────────────
    print("[1/4] Tagging API endpoint handlers …", file=sys.stderr)
    tag_endpoints(src_root, driver)

    # ── 2. Load graph into memory ─────────────────────────────────────────────
    print("[2/4] Loading call graph into memory …", file=sys.stderr)
    fn_info, reverse, implements = load_graph(driver)
    trait_map, all_concrete_types = load_trait_map(driver)
    driver.close()

    # ── Resolve target symbols ────────────────────────────────────────────────
    seed_spec: SpecConstraint | None = None

    if file_hint and line_hint:
        # Location-anchored: use file+line to pin the exact specialization
        print(f"  resolving from {file_hint}:{line_hint} …", file=sys.stderr)
        loc_syms, seed_spec = resolve_seed_from_location(src_root, file_hint, line_hint, fn_info)
        if loc_syms:
            target_syms = loc_syms
            found_name = fn_info[target_syms[0]].get("name", "?")
            # If no explicit name was given, derive it from the located function
            if not fn_name:
                fn_name = found_name
            print(f"  anchored to: {found_name}  (spec: {seed_spec})", file=sys.stderr)
        else:
            if not fn_name:
                print(f"  ERROR: could not find a function at {file_hint}:{line_hint}.", file=sys.stderr)
                sys.exit(1)
            # Fallback to name-based lookup
            target_syms = _find_symbols(fn_name, fn_info)
    else:
        if not fn_name:
            print("  ERROR: provide a function name or both --file and --line.", file=sys.stderr)
            sys.exit(1)
        target_syms = _find_symbols(fn_name, fn_info)

    if not target_syms:
        print(f"  ERROR: no function named '{fn_name}' found in the graph.", file=sys.stderr)
        sys.exit(1)

    # Pick the best symbol (prefer project-local, not test/generated files)
    def _sym_priority(sym):
        info = fn_info[sym]
        file = info.get("file") or ""
        if "test" in file or "openapi" in file:
            return 1
        return 0

    target_syms.sort(key=_sym_priority)
    primary = target_syms[0]
    target_info = fn_info[primary]

    # ── 3. Step A: Tree-sitter structural scan for trait impl blocks ──────────
    print(f"[3/4] Step A — scanning impl blocks for '{fn_name}' …", file=sys.stderr)
    relevant_files = _collect_relevant_files(target_syms, fn_info, reverse)
    # Step A uses ALL known concrete types (discovers any impl that calls the target).
    step_a_types = all_concrete_types if all_concrete_types else None
    concrete_types_step_a = step_a_scan_impl_blocks(fn_name, relevant_files, src_root, step_a_types)
    if concrete_types_step_a:
        print(f"  Step A: {fn_name} reached via concrete types: {sorted(concrete_types_step_a)}", file=sys.stderr)
    else:
        print(f"  Step A: no impl-block dispatch detected — BFS runs unconstrained", file=sys.stderr)

    # Step B uses only types that appear in the payments dispatch layer — the ones
    # handlers explicitly name as `payments::PaymentXxx`.  Using all_concrete_types
    # here would cause connector types (Stripe, Adyen…) to match in handler bodies
    # instead of payment op types, producing false mismatches in Step C.
    payment_op_types = frozenset(
        t for t in all_concrete_types
        if re.match(r"^(Payment|Complete)", t)
    ) if all_concrete_types else None

    # ── 4. BFS with type-constrained trait dispatch ───────────────────────────
    print(
        f"[4/4] BFS from '{fn_name}' (max depth {max_depth}) …",
        file=sys.stderr,
    )
    print(
        f"  target: {target_info['name']}  "
        f"({target_info.get('file', '?')} L{target_info.get('def_line', '?')})",
        file=sys.stderr,
    )

    endpoints = bfs_upward(
        target_syms, fn_info, reverse,
        concrete_types_step_a=concrete_types_step_a,
        src_root=src_root,
        known_types=payment_op_types,
        max_depth=max_depth,
        implements=implements,
        seed_spec=seed_spec
    )
    # Remove internal BFS metadata before output
    for ep in endpoints:
        ep.pop("_type_constraint", None)
    endpoints.sort(key=lambda e: (e.get("path", ""), e.get("method", "")))

    # ── 5. Dedup + connector-gate filter ─────────────────────────────────────
    print("[5/5] Deduplicating and filtering connector-gated endpoints …", file=sys.stderr)

    # Deduplicate: same (method, path, handler, specialization_key) may appear via
    # multiple paths; keep the entry with the shortest call chain.
    # Different specialization keys (Stripe+Authorize vs Stripe+PSync) are kept separately.
    seen_ep: dict = {}
    for ep in endpoints:
        spec_key = ep.get("specialization", {}).get("specialization_key") if ep.get("specialization") else None
        key = (ep.get("method"), ep.get("path"), ep.get("handler"), spec_key)
        existing = seen_ep.get(key)
        if existing is None or len(ep["call_chain"]) < len(existing["call_chain"]):
            seen_ep[key] = ep
    endpoints = list(seen_ep.values())
    endpoints.sort(key=lambda e: (e.get("path", ""), e.get("method", ""), str(e.get("specialization", ""))))

    # should_call_connector gate: only applies when the path goes through a
    # function that is itself inside the connector-execution gate.  Checking for
    # `get_connector_data_with_routing_decision` (or similar connector-dispatch
    # functions) in the chain is a reliable proxy.
    _CONNECTOR_GATED_FUNS = frozenset({
        "get_connector_data_with_routing_decision",
        "call_connector_service",
        "complete_connector_service",
        "call_multiple_connectors_service",
    })

    gate_map = parse_should_call_connector(src_root)
    if gate_map:
        print(
            f"  gate map: {len(gate_map)-1} named ops  default={gate_map.get('_default')}",
            file=sys.stderr,
        )
        filtered = []
        for ep in endpoints:
            chain_names = ep.get("call_chain", [])

            # Verify first hop: if the handler source doesn't actually call the second
            # function in the chain, the SCIP edge is a false positive caused by
            # cfg-gated v2 functions being absent from the index (their calls get
            # attributed to the last v1 function in the file).
            if len(chain_names) >= 2:
                h_sym_check = next(
                    (s for s, info in fn_info.items() if info["name"] == chain_names[0]
                     and "crates/router" in (info.get("file") or "")),
                    None,
                )
                if h_sym_check:
                    h_info_check = fn_info[h_sym_check]
                    abs_path_check = os.path.join(src_root, h_info_check.get("file", ""))
                    tree_check, src_check = _ts_parse_file(abs_path_check)
                    if tree_check is not None and src_check is not None:
                        # Check if second hop name appears as a call in the handler body.
                        # SCIP names for trait methods use "Trait#method"; strip to bare name.
                        second_fn = chain_names[1]
                        search_name = second_fn.rsplit("#", 1)[-1]
                        # Find the function node for this handler
                        target_row = h_info_check.get("def_line", 1) - 1
                        fn_node_chk = None
                        def _find_fn_chk(node):
                            nonlocal fn_node_chk
                            if fn_node_chk is not None:
                                return
                            if node.type == "function_item":
                                if abs(node.start_point[0] - target_row) <= 3:
                                    fn_node_chk = node
                                    return
                            for c in node.named_children:
                                _find_fn_chk(c)
                        _find_fn_chk(tree_check.root_node)
                        if fn_node_chk is not None:
                            fn_body_text = src_check[
                                fn_node_chk.start_byte:fn_node_chk.end_byte
                            ].decode("utf-8", errors="replace")
                            # If the second hop name doesn't appear in the handler body at all,
                            # the SCIP edge is almost certainly a false positive.
                            if search_name not in fn_body_text:
                                print(
                                    f"  [pruned-false-edge] {ep['method']} {ep['path']} — "
                                    f"'{chain_names[0]}' has no call to '{second_fn}' in source "
                                    f"(SCIP cfg-feature false edge)"
                                    if search_name == second_fn else
                                    f"(SCIP cfg-feature false edge — searched for '{search_name}')",
                                    file=sys.stderr,
                                )
                                continue

            # Only apply the gate when the path goes through the connector-execution code
            if not any(fn in chain_names for fn in _CONNECTOR_GATED_FUNS):
                filtered.append(ep)
                continue

            # Identify the Op type for this handler.
            # Multiple nodes can share a name (router + openapi crates); prefer router.
            handler = ep["handler"]
            handler_candidates = [
                sym for sym, info in fn_info.items() if info["name"] == handler
            ]
            handler_candidates.sort(
                key=lambda s: 0 if ("crates/router" in (fn_info[s].get("file") or "")) else 1
            )
            handler_sym = handler_candidates[0] if handler_candidates else None
            h_info = fn_info.get(handler_sym, {}) if handler_sym else {}
            h_file   = h_info.get("file", "")
            h_line   = h_info.get("def_line", 1)

            # Structural (Tree-sitter) op type — precise: only matches actual dispatch calls.
            ts_op = _ts_find_handler_op_type(
                os.path.join(src_root, h_file) if h_file else "",
                h_line,
                payment_op_types,
            )
            # Full op type (Tree-sitter + regex fallback) — may match data struct names.
            op_type = extract_handler_op_type(h_file, h_line, src_root, payment_op_types)

            # Chain-walk fallback: when the handler body doesn't contain the op type
            # (e.g. it calls get_payment_action or payments_core as an intermediary),
            # walk the chain toward payments_operation_core and try each function.
            if ts_op is None and op_type is None:
                for chain_fn_name in chain_names[1:]:
                    if chain_fn_name in {
                        "payments_operation_core",
                        "get_connector_data_with_routing_decision",
                    }:
                        break
                    c_candidates = [
                        sym for sym, info in fn_info.items()
                        if info["name"] == chain_fn_name
                        and "crates/router" in (info.get("file") or "")
                    ]
                    if not c_candidates:
                        continue
                    c_info = fn_info[c_candidates[0]]
                    c_file = c_info.get("file", "")
                    c_line = c_info.get("def_line", 1)
                    if not c_file:
                        continue
                    c_ts = _ts_find_handler_op_type(
                        os.path.join(src_root, c_file),
                        c_line,
                        payment_op_types,
                    )
                    if c_ts is not None:
                        ts_op = c_ts
                        break
                    c_reg = extract_handler_op_type(c_file, c_line, src_root, payment_op_types)
                    if c_reg is not None:
                        op_type = c_reg
                        break

            # Apply _default only when the structural scan found the op type.
            # Regex finds data struct names (PaymentsRedirectResponseData) which are
            # NOT payment op types and must not be filtered by the default gate.
            if ts_op is not None:
                gate = gate_map.get(ts_op, gate_map.get("_default", "conditional"))
                op_type = ts_op  # use the precise type for the log message
            elif op_type is not None:
                gate = gate_map.get(op_type, "conditional")
            else:
                gate = "conditional"

            if gate == "always_false":
                print(
                    f"  [filtered] {ep['method']} {ep['path']} — "
                    f"Op={op_type} is always_false in should_call_connector",
                    file=sys.stderr,
                )
                continue

            if gate == "conditional":
                ep["connector_gate"] = f"conditional — op={op_type}"

            filtered.append(ep)

        endpoints = filtered
    else:
        print("  Warning: could not parse should_call_connector — gate filter skipped", file=sys.stderr)

    # ── Split feasible vs infeasible endpoints ───────────────────────────────
    feasible_endpoints   = [ep for ep in endpoints if not ep.get("likely_infeasible")]
    infeasible_endpoints = [ep for ep in endpoints if ep.get("likely_infeasible")]

    changed_function = target_info["name"]
    changed_file     = target_info.get("file")
    changed_line     = target_info.get("def_line")

    # ── 6. Build flows + reachability matrix ──────────────────────────────────
    print("[6/6] Building flows and reachability matrix …", file=sys.stderr)
    flows = build_flows(
        feasible_endpoints, fn_info, src_root,
        changed_function, changed_file, changed_line,
    )
    reachability_matrix = build_reachability_matrix(
        feasible_endpoints, flows,
        changed_function, changed_file, changed_line,
    )

    # ── Assemble result ───────────────────────────────────────────────────────
    result = {
        "changed_function": changed_function,
        "changed_file":     changed_file,
        "changed_line":     changed_line,
        # legacy keys kept for backward compat
        "function":         changed_function,
        "file":             changed_file,
        "def_line":         changed_line,
        "endpoint_count":   len(feasible_endpoints),
        "flow_count":       len(flows),
        "endpoints":        feasible_endpoints,
        "flows":            flows,
        "reachability_matrix": reachability_matrix,
        "infeasible_paths": infeasible_endpoints,
    }

    # Pretty-print to stdout
    print(f"\n{'─'*60}")
    print(f"  Changed  : {changed_function}")
    print(f"  File     : {changed_file}  L{changed_line}")
    print(f"  Impacted : {len(feasible_endpoints)} endpoint(s)  |  {len(flows)} flow(s)")
    print(f"{'─'*60}")
    for ep in feasible_endpoints:
        guards_label = (
            f"  [guarded: {', '.join(g['guard_type'] for g in ep['guards'])}]"
            if ep["guards"] else "  [unconditional]"
        )
        print(f"  {ep['method']:6s} {ep['path']}")
        print(f"         handler : {ep['handler']}")
        if ep.get("specialization"):
            s = ep["specialization"]
            args = ", ".join(s.get("trait_args") or [])
            spec_str = f"{s.get('connector')} via {s.get('trait')}[{args}]::{s.get('method')}"
            print(f"         spec    : {spec_str}")
        print(f"         chain   : {' → '.join(ep['call_chain'])}")
        print(f"         guards  :{guards_label}")
        for g in ep["guards"]:
            print(f"           • [{g['guard_type']}] at {g['at_hop']}")
            print(f"             condition: {g['condition'][:120]}")
        print()

    if infeasible_endpoints:
        print(f"{'─'*60}")
        print(f"  [LIKELY INFEASIBLE] paths: {len(infeasible_endpoints)}")
        print(f"{'─'*60}")
        for ep in infeasible_endpoints:
            print(f"  {ep['method']:6s} {ep['path']}  [LIKELY INFEASIBLE]")
            print(f"         handler : {ep['handler']}")
            print(f"         reason  : {ep.get('infeasible_reason', '')}")
            print(f"         chain   : {' → '.join(ep['call_chain'])}")
            print()

    if out_path:
        with open(out_path, "w") as f:
            json.dump(result, f, indent=2)
        print(f"  Wrote {out_path}")

    return result


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Find which API endpoints are impacted by a function change.\n\n"
                    "Identify the function by name, or by --file + --line (or both).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("function", nargs="?", default=None,
                    help="Function name (e.g. get_connector_with_networks). "
                         "Optional when --file and --line are provided.")
    ap.add_argument("--depth", type=int,  default=8, help="Max BFS depth (default 8)")
    ap.add_argument("--out",              help="Write JSON result to this file")
    ap.add_argument("--file",             help="Relative file path to the changed function "
                                               "(e.g. crates/router/src/core/payments.rs)",
                    dest="file_hint")
    ap.add_argument("--line", type=int,   help="Line number of the changed function within --file",
                    dest="line_hint")
    args = ap.parse_args()

    if not args.function and not (args.file_hint and args.line_hint):
        ap.error("Provide a function name, or both --file and --line to identify the function.")

    src_root = os.environ.get("SRC_ROOT", "")
    if not src_root:
        print("Error: set SRC_ROOT to the hyperswitch repo root.", file=sys.stderr)
        sys.exit(1)

    find_impact(
        args.function, src_root,
        max_depth=args.depth,
        out_path=args.out,
        file_hint=args.file_hint,
        line_hint=args.line_hint,
    )
