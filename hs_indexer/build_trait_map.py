"""
build_trait_map.py — Part 1.5: Trait Implementation & Generic Call-Site Map
============================================================================

Scans every Rust source file with Tree-sitter and builds two data layers:

Layer 1 — Trait Impl Map
  For every `impl Trait for T { fn method … }` block:
    • Annotates the matching :Fn node in Neo4j with:
        impl_trait  = "GetTrackers"      (short trait name)
        impl_type   = "PaymentConfirm"   (short concrete type name)
    • Creates a [:IMPLEMENTS] edge:
        (:Fn {name:"PaymentConfirm#GetTrackers#get_trackers"})
          -[:IMPLEMENTS]->
        (:Fn {name:"GetTrackers#get_trackers"})

Layer 2 — Generic Call-Site Map
  For every turbofish call  `fn_name::<ConcreteType>(…)`  or
  struct-as-first-arg call  `fn_name(ConcreteType { … }, …)`:
    • Annotates the relevant [:CALLS] edge with:
        type_param = "ConcreteType"

This data lets find_impact.py:
  • Discover concrete implementing types dynamically (no hardcoded lists).
  • Know exactly which concrete type flows into a generic function at each
    call site — enabling precise type-constrained BFS.

Usage:
  SRC_ROOT=/path/to/hyperswitch .venv/bin/python3 build_trait_map.py
"""

import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

from neo4j import GraphDatabase
from tree_sitter import Language, Parser, Node as TSNode
import tree_sitter_rust as _tsrust

# ── Config ─────────────────────────────────────────────────────────────────────

NEO4J_URI  = "bolt://localhost:7687"
NEO4J_AUTH = ("neo4j", "Hyperswitch@123")
BATCH_SIZE = 1_000

_TS_RUST_LANG = Language(_tsrust.language())
_ts_parser    = Parser(_TS_RUST_LANG)

# Directories to skip entirely
_SKIP_DIRS = {"target", ".git", "node_modules"}

# ── Tree-sitter helpers ────────────────────────────────────────────────────────

def _ts_text(node: TSNode, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace").strip()


def _short_name(qualified: str) -> str:
    """Strip generic params and module path — return last identifier segment."""
    base = qualified.split("<")[0]          # drop <T, U, ...>
    base = base.rsplit("::", 1)[-1]        # drop module path
    return base.strip().rstrip(">()")


def _extract_trait_info(trait_node: TSNode, src: bytes) -> tuple[str, tuple[str, ...]]:
    """
    Parse a trait reference in an impl_item and return (base_trait_name, trait_args).

    Handles both:
      Simple:  GetTrackers
               → ("GetTrackers", ())
      Generic: ConnectorIntegration<Authorize, PaymentsAuthorizeData, PaymentsResponseData>
               → ("ConnectorIntegration", ("Authorize", "PaymentsAuthorizeData", "PaymentsResponseData"))
    """
    if trait_node.type == "generic_type":
        inner = trait_node.child_by_field_name("type")
        args  = trait_node.child_by_field_name("type_arguments")
        base  = _short_name(_ts_text(inner, src)) if inner else ""
        arg_names: list[str] = []
        if args:
            for child in args.named_children:
                # Accept type identifiers and scoped paths; skip lifetime params
                if child.type in ("type_identifier", "scoped_type_identifier"):
                    t = _short_name(_ts_text(child, src))
                    if t:
                        arg_names.append(t)
                elif child.type == "generic_type":
                    # e.g. Box<dyn Error> as a type arg — take the outer name only
                    inner2 = child.child_by_field_name("type")
                    t = _short_name(_ts_text(inner2, src)) if inner2 else ""
                    if t:
                        arg_names.append(t)
        return (base, tuple(arg_names))
    else:
        return (_short_name(_ts_text(trait_node, src)), ())


def _make_specialization_key(
    trait_name: str, impl_type: str, trait_args: tuple[str, ...], method: str
) -> str:
    """
    Build a stable string identity for one concrete trait impl method.
    Format: TraitName|ImplType|Arg1,Arg2,Arg3|method_name
    Example: ConnectorIntegration|Stripe|Authorize,PaymentsAuthorizeData,PaymentsResponseData|build_request
    """
    return f"{trait_name}|{impl_type}|{','.join(trait_args)}|{method}"


def _parse_file(abs_path: str):
    try:
        src = open(abs_path, "rb").read()
    except OSError:
        return None, None
    return _ts_parser.parse(src), src


# ── Layer 1: Trait Impl Extraction ────────────────────────────────────────────

def _extract_impl_methods(impl_body: TSNode, src: bytes) -> list[tuple[str, int]]:
    """Return list of (method_name, def_line_1indexed) for each fn in an impl body."""
    methods = []
    for child in impl_body.named_children:
        if child.type == "function_item":
            name_node = child.child_by_field_name("name")
            if name_node:
                def_line = child.start_point[0] + 1   # 1-indexed to match build_callgraph.py
                methods.append((_ts_text(name_node, src), def_line))
    return methods


def _extract_impls_from_file(abs_path: str) -> list[dict]:
    """
    Parse one .rs file and return a list of impl records:
      {trait_name, type_name, methods: [str], file: rel_path}
    Only `impl Trait for T` blocks are returned (not bare `impl T`).
    """
    tree, src = _parse_file(abs_path)
    if tree is None:
        return []

    records = []

    def _walk(node: TSNode):
        if node.type == "impl_item":
            trait_node = node.child_by_field_name("trait")
            type_node  = node.child_by_field_name("type")
            body_node  = node.child_by_field_name("body")

            if trait_node and type_node and body_node:
                # Use _extract_trait_info to capture generic args too
                trait_name, trait_args = _extract_trait_info(trait_node, src)
                type_name  = _short_name(_ts_text(type_node,  src))
                methods    = _extract_impl_methods(body_node, src)

                if trait_name and type_name and methods:
                    records.append({
                        "trait_name": trait_name,
                        "trait_args": trait_args,   # tuple[str, ...], e.g. ("Authorize", "X", "Y")
                        "type_name":  type_name,
                        "methods":    methods,
                    })
            # Don't recurse into nested impl items (associated types etc.)
            return

        for child in node.named_children:
            _walk(child)

    _walk(tree.root_node)
    return records


def collect_all_impls(src_root: str) -> list[dict]:
    """Walk the entire codebase and collect all `impl Trait for T` records."""
    records = []
    src_path = Path(src_root)
    crates_path = src_path / "crates"
    search_root = crates_path if crates_path.is_dir() else src_path

    total_files = 0
    for dirpath, dirnames, filenames in os.walk(search_root):
        # Prune skipped dirs in-place so os.walk won't descend into them
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            if not fname.endswith(".rs"):
                continue
            abs_path = os.path.join(dirpath, fname)
            rel_path = os.path.relpath(abs_path, src_root)
            file_records = _extract_impls_from_file(abs_path)
            for r in file_records:
                r["file"] = rel_path
            records.extend(file_records)
            total_files += 1
            if total_files % 500 == 0:
                print(f"  … scanned {total_files} files, {len(records)} impls so far", file=sys.stderr)

    print(f"  Scanned {total_files} files  →  {len(records)} impl blocks found", file=sys.stderr)
    return records


# ── Layer 2: Generic Call-Site Extraction ─────────────────────────────────────

def _extract_generic_calls_from_file(abs_path: str) -> list[dict]:
    """
    Find generic call sites in one file.

    Patterns detected:
      1. Turbofish:       some_fn::<ConcreteType>(...)
      2. Struct-as-arg:   some_fn(ConcreteType { ... }, ...)  (first arg only)

    Returns list of {caller_line (1-indexed), callee_name, type_param}.
    """
    tree, src = _parse_file(abs_path)
    if tree is None:
        return []

    calls = []

    def _walk(node: TSNode):
        # Pattern 1: turbofish  fn::<Type>(...)
        if node.type == "call_expression":
            fn_node = node.child_by_field_name("function")
            if fn_node and fn_node.type == "generic_function":
                inner_fn = fn_node.child_by_field_name("function")
                type_args = fn_node.child_by_field_name("type_arguments")
                if inner_fn and type_args:
                    callee = _short_name(_ts_text(inner_fn, src))
                    # Take only the FIRST concrete (non-wildcard) type argument.
                    # Emitting all args causes the last Neo4j write to win, which
                    # may be a Rust `_` wildcard overwriting the real operation type.
                    for child in type_args.named_children:
                        if child.type in ("type_identifier", "scoped_type_identifier"):
                            type_name = _short_name(_ts_text(child, src))
                            if type_name and type_name != "_":
                                calls.append({
                                    "callee":     callee,
                                    "type_param": type_name,
                                    "line":       fn_node.start_point[0] + 1,
                                })
                                break  # first concrete type arg is the operation type T

            # Pattern 2: first arg is a struct literal  fn(TypeName { ... }, ...)
            if fn_node and fn_node.type in ("identifier", "scoped_identifier", "field_expression"):
                callee = _short_name(_ts_text(fn_node, src))
                args_node = node.child_by_field_name("arguments")
                if args_node:
                    first_arg = next(
                        (c for c in args_node.named_children if c.type != "comment"),
                        None,
                    )
                    if first_arg and first_arg.type == "struct_expression":
                        type_node = first_arg.child_by_field_name("name")
                        if type_node:
                            type_name = _short_name(_ts_text(type_node, src))
                            if type_name:
                                calls.append({
                                    "callee":     callee,
                                    "type_param": type_name,
                                    "line":       node.start_point[0] + 1,
                                })

        for child in node.named_children:
            _walk(child)

    _walk(tree.root_node)
    return calls


def collect_all_generic_calls(src_root: str) -> dict[str, list[dict]]:
    """
    Walk codebase and collect generic call sites.
    Returns {rel_file: [{callee, type_param, line}, ...]}
    """
    result: dict = {}
    src_path = Path(src_root)
    crates_path = src_path / "crates"
    search_root = crates_path if crates_path.is_dir() else src_path

    alias_map = _build_type_alias_map(src_root)
    print(f"  {len(alias_map)} type aliases resolved", file=sys.stderr)

    for dirpath, dirnames, filenames in os.walk(search_root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            if not fname.endswith(".rs"):
                continue
            abs_path = os.path.join(dirpath, fname)
            calls = _extract_generic_calls_from_file(abs_path)
            # Merge variable-binding inferred calls (no duplicates by line)
            vb_calls = _extract_variable_binding_calls_from_file(abs_path, type_alias_map=alias_map)
            if vb_calls:
                existing_lines = {c["line"] for c in calls}
                for c in vb_calls:
                    if c["line"] not in existing_lines:
                        calls.append(c)
                        existing_lines.add(c["line"])
            if calls:
                rel_path = os.path.relpath(abs_path, src_root)
                result[rel_path] = calls

    total = sum(len(v) for v in result.values())
    print(f"  {total} generic call sites found across {len(result)} files", file=sys.stderr)
    return result


def collect_variable_binding_calls(src_root: str) -> dict[str, list[dict]]:
    """
    Walk codebase and collect variable-binding generic call sites.
    Finds patterns like:
        let op = PaymentCreate { ... };
        payments_operation_core(op, ...)
    Returns {rel_file: [{callee, type_param, line}, ...]}
    """
    result: dict = {}
    src_path = Path(src_root)
    crates_path = src_path / "crates"
    search_root = crates_path if crates_path.is_dir() else src_path

    alias_map = _build_type_alias_map(src_root)
    print(f"  {len(alias_map)} type aliases resolved", file=sys.stderr)

    for dirpath, dirnames, filenames in os.walk(search_root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            if not fname.endswith(".rs"):
                continue
            abs_path = os.path.join(dirpath, fname)
            calls = _extract_variable_binding_calls_from_file(abs_path, type_alias_map=alias_map)
            if calls:
                rel_path = os.path.relpath(abs_path, src_root)
                result[rel_path] = calls

    total = sum(len(v) for v in result.values())
    print(f"  {total} variable-binding generic call sites found across {len(result)} files", file=sys.stderr)
    return result


def _build_type_alias_map(src_root: str) -> dict[str, str]:
    """
    Scan every .rs file for type alias definitions of the form:
        type Alias = Generic<FirstTypeArg, ...>;
        type Alias = dyn Generic<FirstTypeArg, ...>;

    Returns {alias_name: first_concrete_type_arg}.

    Example:
        pub type UasPreAuthenticationRouterData =
            RouterData<PreAuthenticate, UasPreAuthenticationRequestData, ...>;
        → {"UasPreAuthenticationRouterData": "PreAuthenticate"}

    This lets _collect_bindings resolve plain type_identifier annotations:
        let router_data: UasPreAuthenticationRouterData = ...
                         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                         type_identifier (not generic_type) → look up in alias map
                         → concrete_type = "PreAuthenticate"
    """
    alias_map: dict[str, str] = {}
    src_path = Path(src_root)
    crates_path = src_path / "crates"
    search_root = crates_path if crates_path.is_dir() else src_path

    def _extract_from_file(abs_path: str):
        tree, src = _parse_file(abs_path)
        if tree is None:
            return
        def _walk(node: TSNode):
            if node.type == "type_item":
                name_node = node.child_by_field_name("name")
                type_node = node.child_by_field_name("type")
                if name_node and type_node:
                    inner = type_node
                    # Unwrap `dyn Trait<T>` → `Trait<T>`
                    if inner.type == "dynamic_type":
                        inner = next(
                            (c for c in inner.named_children
                             if c.type == "generic_type"), None,
                        ) or inner
                    if inner is not None and inner.type == "generic_type":
                        args_node = inner.child_by_field_name("type_arguments")
                        if args_node:
                            first_arg = next(
                                (c for c in args_node.named_children
                                 if c.type in ("type_identifier", "scoped_type_identifier")),
                                None,
                            )
                            if first_arg:
                                candidate = _short_name(_ts_text(first_arg, src))
                                if len(candidate) > 2 and candidate[0].isupper():
                                    alias_map[_ts_text(name_node, src)] = candidate
            for child in node.named_children:
                _walk(child)
        _walk(tree.root_node)

    for dirpath, dirnames, filenames in os.walk(search_root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            if fname.endswith(".rs"):
                _extract_from_file(os.path.join(dirpath, fname))

    print(f"  {len(alias_map):,} type aliases resolved (alias → first generic arg)", file=sys.stderr)
    return alias_map


def _extract_variable_binding_calls_from_file(abs_path: str, type_alias_map: dict | None = None) -> list[dict]:
    """
    Find variable-binding generic call sites in one file.

    Per function body, builds a local bindings dict (var_name → ConcreteType) by
    walking let_declaration nodes:
      - If value is struct_expression → extract name field, short_name it
      - If value is call_expression and function text contains '::' →
        extract short_name(fn_text.rsplit('::', 1)[0])
      - If type annotation is generic_type → extract first type arg
      - If type annotation is type_identifier → look up in type_alias_map

    Then walks call_expression nodes: if the first argument is an identifier
    that is in bindings, emits {callee, type_param, line}.

    Scoped per function_item — bindings reset when entering a new function.
    """
    tree, src = _parse_file(abs_path)
    if tree is None:
        return []

    calls = []

    def _extract_from_function(fn_node: TSNode):
        """Extract variable-binding call sites from a single function_item node."""
        body = fn_node.child_by_field_name("body")
        if body is None:
            return

        # Pass 1: collect let-binding → concrete type mappings
        bindings: dict[str, str] = {}

        def _collect_bindings(node: TSNode):
            if node.type == "let_declaration":
                pat_node = node.child_by_field_name("pattern")
                val_node = node.child_by_field_name("value")
                if pat_node is None or val_node is None:
                    return
                var_name = _ts_text(pat_node, src)
                # Strip any type annotation (e.g. `mut op` → `op`)
                var_name = var_name.lstrip("mut").strip()

                concrete_type: str | None = None
                if val_node.type == "struct_expression":
                    type_node = val_node.child_by_field_name("name")
                    if type_node:
                        concrete_type = _short_name(_ts_text(type_node, src))
                elif val_node.type == "call_expression":
                    fn_node_inner = val_node.child_by_field_name("function")
                    if fn_node_inner:
                        fn_text = _ts_text(fn_node_inner, src)
                        if "::" in fn_text:
                            concrete_type = _short_name(fn_text.rsplit("::", 1)[0])

                # Fallback: read explicit type annotation on the let binding.
                # Handles patterns like:
                #   let router_data: RouterData<IncrementalAuthorization, ...> = ...
                # Extract the first generic type argument as the operation type T.
                if concrete_type is None:
                    type_ann_node = node.child_by_field_name("type")
                    if type_ann_node:
                        # Unwrap reference: &RouterData<T> → RouterData<T>
                        inner = type_ann_node
                        if inner.type == "reference_type":
                            inner = next(
                                (c for c in inner.named_children if c.type == "generic_type"),
                                None,
                            ) or inner
                        if inner is not None and inner.type == "generic_type":
                            args_node = inner.child_by_field_name("type_arguments")
                            if args_node:
                                first_arg = next(
                                    (c for c in args_node.named_children
                                     if c.type in ("type_identifier", "scoped_type_identifier")),
                                    None,
                                )
                                if first_arg:
                                    candidate = _short_name(_ts_text(first_arg, src))
                                    # Only use concrete type names (multi-char CamelCase).
                                    # Skip bare generic params like F, T, Req (≤2 chars or
                                    # single uppercase used as type-variable convention).
                                    # _short_name strips module prefix (api::PoFulfill → PoFulfill)
                                    # so the isupper() check always sees just the type name.
                                    if len(candidate) > 2 and candidate[0].isupper():
                                        concrete_type = candidate
                        # Type alias resolution: let router_data: UasPreAuthenticationRouterData = ...
                        #   type_ann is a plain type_identifier (not generic_type), but the
                        #   alias expands to RouterData<PreAuthenticate, ...> — look it up.
                        elif (inner is not None
                              and inner.type in ("type_identifier", "scoped_type_identifier")
                              and type_alias_map):
                            alias_name = _short_name(_ts_text(inner, src))
                            resolved = type_alias_map.get(alias_name)
                            if resolved:
                                concrete_type = resolved

                if concrete_type and var_name:
                    bindings[var_name] = concrete_type
                # Still recurse into the value expression — it may contain nested
                # match arms / blocks with their own inner let bindings, e.g.:
                #   let resp = match cond { true => { let ci: Type = ...; call(ci) } }
                if val_node is not None:
                    _collect_bindings(val_node)
                return  # pattern/type nodes never contain nested let declarations

            for child in node.named_children:
                _collect_bindings(child)

        _collect_bindings(body)

        # Pass 2: walk call_expressions — if ANY arg is a bound identifier, emit.
        # Checks all argument positions (not just first) so patterns like:
        #   execute_connector_processing_step(state, connector_integration, router_data)
        # are captured even when the typed variable is arg 2 or 3.
        # If multiple bound args carry different types, skip (ambiguous).
        def _collect_calls(node: TSNode):
            if node.type == "call_expression":
                fn_node_call = node.child_by_field_name("function")
                args_node = node.child_by_field_name("arguments")
                if fn_node_call and args_node:
                    callee = _short_name(_ts_text(fn_node_call, src))
                    # Collect type_params from all bound identifier arguments
                    found_types: list[str] = []
                    for arg in args_node.named_children:
                        if arg.type == "comment":
                            continue
                        # Strip reference operators: &x, &mut x → x
                        inner_arg = arg
                        if arg.type in ("reference_expression", "unary_expression"):
                            inner_arg = next(
                                (c for c in arg.named_children if c.type == "identifier"),
                                None,
                            ) or arg
                        if inner_arg.type == "identifier":
                            ident = _ts_text(inner_arg, src)
                            if ident in bindings:
                                found_types.append(bindings[ident])
                    # Only emit when all bound args agree on a single concrete type
                    unique = list(dict.fromkeys(found_types))  # deduplicate, preserve order
                    if len(unique) == 1:
                        calls.append({
                            "callee":     callee,
                            "type_param": unique[0],
                            "line":       node.start_point[0] + 1,
                        })

            for child in node.named_children:
                _collect_calls(child)

        _collect_calls(body)

    def _walk_for_functions(node: TSNode):
        if node.type == "function_item":
            _extract_from_function(node)
            # Don't recurse into nested function items (closures handled separately)
            return
        for child in node.named_children:
            _walk_for_functions(child)

    _walk_for_functions(tree.root_node)
    return calls


# ── Layer 3: Synthetic CALLS edges for async_trait impl bodies ────────────────
#
# Problem: SCIP does not record call occurrences inside `#[async_trait]` macro
# expansions.  The concrete impl method nodes exist in Neo4j (created by Layer 1
# above) but have no outgoing CALLS edges to the helpers they invoke.
#
# Solution: re-parse every impl method body with Tree-sitter, extract all call
# expressions by name, then MERGE synthetic CALLS edges in Neo4j for any callee
# that exists in the graph.  MERGE ensures we never duplicate an edge SCIP did
# record; ON CREATE marks the synthetic ones with `synthetic: true` for tracing.
#
# No manual stub functions are required when new ops or trait methods are added.

# Rust stdlib / common method names that flood every function body — matching
# these against Fn nodes would create massive false-positive edges.
_NOISE_CALLEES: frozenset[str] = frozenset({
    "map", "ok", "err", "into", "from", "unwrap", "expect", "clone",
    "len", "push", "pop", "get", "set", "new", "default", "iter",
    "collect", "flatten", "filter", "and_then", "or_else", "ok_or",
    "ok_or_else", "map_err", "is_some", "is_none", "is_empty",
    "to_string", "to_owned", "as_ref", "as_mut", "as_str",
    "try_from", "try_into", "into_iter", "next", "await",
    "lock", "read", "write", "send", "recv", "spawn",
    "box_err", "change_context", "attach_printable",
})


def _extract_calls_from_fn_body(fn_node: TSNode, src: bytes) -> list[dict]:
    """
    Walk a function_item node and collect every call expression.

    Returns list of {callee, line} where:
      callee  — short unqualified name of the function/method being called
      line    — 1-indexed source line of the call expression
    """
    results: list[dict] = []

    def _walk(node: TSNode) -> None:
        if node.type == "call_expression":
            fn_part = node.child_by_field_name("function")
            if fn_part is not None:
                name: str | None = None
                if fn_part.type == "field_expression":
                    # method call: receiver.method(args)  →  capture "method"
                    field = fn_part.child_by_field_name("field")
                    if field is not None:
                        name = _ts_text(field, src)
                elif fn_part.type == "generic_function":
                    # turbofish: fn_name::<T>(args)  →  capture "fn_name"
                    inner = fn_part.child_by_field_name("function")
                    if inner is not None:
                        name = _short_name(_ts_text(inner, src))
                elif fn_part.type in ("identifier", "scoped_identifier"):
                    name = _short_name(_ts_text(fn_part, src))

                if name and len(name) >= 4 and name not in _NOISE_CALLEES:
                    results.append({
                        "callee": name,
                        "line":   fn_part.start_point[0] + 1,
                    })

        for child in node.named_children:
            _walk(child)

    body = fn_node.child_by_field_name("body")
    if body is not None:
        _walk(body)
    return results


def collect_async_trait_call_edges(
    impl_records: list[dict], src_root: str
) -> list[dict]:
    """
    Re-parse every impl method body collected in Layer 1 and synthesize CALLS
    edge records for all function calls found inside.

    Returns list of:
      {caller_file, caller_def_line, callee, call_line}
    """
    # Group by file so each file is parsed only once
    by_file: dict[str, list[dict]] = {}
    for r in impl_records:
        by_file.setdefault(r["file"], []).append(r)

    all_edges: list[dict] = []

    for rel_file, records in by_file.items():
        abs_path = os.path.join(src_root, rel_file)
        tree, src = _parse_file(abs_path)
        if tree is None:
            continue

        # Map def_line → method record for the methods in this file
        def_line_to_rec: dict[int, dict] = {}
        for r in records:
            for method_name, def_line in r["methods"]:
                def_line_to_rec[def_line] = {
                    "method_name": method_name,
                    "def_line":    def_line,
                    "file":        rel_file,
                }

        def _walk(node: TSNode) -> None:
            if node.type == "function_item":
                def_line = node.start_point[0] + 1
                if def_line in def_line_to_rec:
                    rec = def_line_to_rec[def_line]
                    for call in _extract_calls_from_fn_body(node, src):
                        all_edges.append({
                            "caller_file":     rec["file"],
                            "caller_def_line": rec["def_line"],
                            "callee":          call["callee"],
                            "call_line":       call["line"],
                        })
                # Still recurse — nested fns (closures) may be inside
            for child in node.named_children:
                _walk(child)

        _walk(tree.root_node)

    print(
        f"  {len(all_edges):,} synthetic call edges extracted from impl bodies",
        file=sys.stderr,
    )
    return all_edges


def write_synthetic_calls(call_edges: list[dict], driver) -> None:
    """
    Write CALLS edges synthesized from impl method bodies.

    Uses MERGE so existing SCIP-recorded edges are never duplicated.
    Only edges SCIP missed (ON CREATE branch) are marked with synthetic=true.
    """
    # Drop old synthetic edges so re-runs stay clean
    with driver.session() as s:
        deleted = s.run(
            "MATCH ()-[r:CALLS {synthetic: true}]->() DELETE r RETURN count(r) AS n"
        ).single()["n"]
    if deleted:
        print(f"  Removed {deleted:,} stale synthetic CALLS edges", file=sys.stderr)

    # Pre-filter: only allow callees whose short name is UNIQUE in the graph.
    # If two Fn nodes share the same name, the edge is ambiguous — we'd connect
    # to the wrong one. Unique names are safe; duplicate names are skipped.
    # Also excludes abstract trait nodes (name contains '#').
    callee_names = {e["callee"] for e in call_edges}
    unique_names: set[str] = set()
    with driver.session() as s:
        rows = s.run("""
            UNWIND $names AS n
            MATCH (fn:Fn)
            WHERE fn.name = n AND NOT fn.name CONTAINS '#'
            WITH n, count(fn) AS cnt
            WHERE cnt = 1
            RETURN n AS name
        """, names=list(callee_names)).data()
        unique_names = {r["name"] for r in rows}

    filtered = [e for e in call_edges if e["callee"] in unique_names]
    print(
        f"  {len(unique_names):,} unique callee names (of {len(callee_names):,}) — "
        f"{len(filtered):,} edges after uniqueness filter",
        file=sys.stderr,
    )

    # Only create an edge when the callee exists AND:
    #   1. Is not an abstract trait node (those have '#' — handled by IMPLEMENTS bridge)
    #   2. Has an EXACT name match (not ENDS WITH — that was too broad)
    #   3. Is unique by name (pre-filtered above)
    q = """
        UNWIND $batch AS row
        MATCH (caller:Fn)
        WHERE caller.file     = row.caller_file
          AND caller.def_line = row.caller_def_line
        MATCH (callee:Fn {name: row.callee})
        MERGE (caller)-[r:CALLS]->(callee)
        ON CREATE SET r.file      = row.caller_file,
                      r.line      = row.call_line,
                      r.synthetic = true
    """
    call_edges = filtered

    import time
    created = 0
    for batch in _batches(call_edges, size=200):
        retries = 5
        while retries:
            try:
                with driver.session() as s:
                    result = s.run(q, batch=batch)
                    created += result.consume().counters.relationships_created
                break
            except Exception as exc:
                if "Deadlock" in str(exc) and retries > 1:
                    retries -= 1
                    time.sleep(0.3)
                else:
                    raise

    print(f"  {created:,} synthetic CALLS edges created", file=sys.stderr)


# ── Neo4j: write Layer 1 ──────────────────────────────────────────────────────

def _batches(lst, size=BATCH_SIZE):
    for i in range(0, len(lst), size):
        yield lst[i:i+size]


def write_impl_annotations(impl_records: list[dict], driver):
    """
    For each (trait, type, method, def_line) quad:
      0. CREATE the :Fn node if SCIP missed it (happens for every specialization
         of a generic trait beyond the first — e.g. all ConnectorIntegration<X>
         impls beyond the one SCIP indexed).  Node is created with a synthetic
         symbol keyed by file+def_line so it is unique and stable across re-runs.
      1. Annotate the concrete :Fn node matched by (file, def_line) — not by name,
         which is ambiguous when the same method appears in multiple specializations.
      2. Create [:IMPLEMENTS] edge from the concrete :Fn to the abstract :Fn
         (matched by name == "trait#method").
    """
    # Flatten to individual (trait, type, method, def_line, file) quads
    triples = []
    for r in impl_records:
        trait_args = r.get("trait_args", ())
        args_str   = ",".join(trait_args)
        file_      = r.get("file", "")
        for method_name, def_line in r["methods"]:
            spec_key = _make_specialization_key(r["trait_name"], r["type_name"], trait_args, method_name)
            # Name encodes the full specialization — unique per impl block
            args_tag      = f"<{args_str}>" if args_str else ""
            concrete_name = f"{r['type_name']}#{r['trait_name']}{args_tag}#{method_name}"
            triples.append({
                "trait_name":     r["trait_name"],
                "trait_args_str": args_str,
                "type_name":      r["type_name"],
                "method_name":    method_name,
                "def_line":       def_line,
                "file":           file_,
                "concrete_name":  concrete_name,
                "abstract_name":  f"{r['trait_name']}#{method_name}",
                "spec_key":       spec_key,
                # Synthetic symbol — stable key for nodes SCIP didn't create
                "synthetic_sym":  f"synthetic://{file_}::{def_line}",
            })

    print(f"  {len(triples):,} (trait, type, method) triples to annotate", file=sys.stderr)

    # ── Ensure a (file, def_line) index exists for fast matching ──────────────
    with driver.session() as s:
        s.run("CREATE INDEX fn_def_line IF NOT EXISTS FOR (n:Fn) ON (n.def_line)")

    # ── Reset existing annotations (idempotent re-runs) ───────────────────────
    with driver.session() as s:
        s.run("MATCH (fn:Fn) REMOVE fn.impl_trait, fn.impl_type, fn.impl_trait_args, fn.impl_method, fn.impl_spec_key")
        s.run("MATCH ()-[r:IMPLEMENTS]->() DELETE r")

    # ── Step 0: Create missing :Fn nodes SCIP did not index ───────────────────
    # MERGE on synthetic_sym so re-runs are idempotent.
    # Only nodes where SCIP already created a node at (file, def_line) will be
    # skipped — they keep their SCIP symbol; the ON CREATE branch is a no-op.
    q_create = """
        UNWIND $batch AS row
        MERGE (fn:Fn {symbol: row.synthetic_sym})
        ON CREATE SET fn.name     = row.concrete_name,
                      fn.file     = row.file,
                      fn.def_line = row.def_line
    """
    # But only create synthetic nodes when no SCIP node already exists at that location.
    # We first collect (file, def_line) pairs that already have a node, then skip those.
    existing_locations: set = set()
    with driver.session() as s:
        rows = s.run("""
            MATCH (fn:Fn)
            WHERE NOT fn.symbol STARTS WITH 'synthetic://'
            RETURN fn.file AS file, fn.def_line AS def_line
        """).data()
        for row in rows:
            if row["file"] and row["def_line"] is not None:
                existing_locations.add((row["file"], row["def_line"]))

    missing = [t for t in triples if (t["file"], t["def_line"]) not in existing_locations]
    print(f"  {len(missing):,} nodes missing from SCIP — will be created", file=sys.stderr)

    created = 0
    for batch in _batches(missing):
        with driver.session() as s:
            result = s.run(q_create, batch=batch)
            created += result.consume().counters.nodes_created
    print(f"  {created:,} new :Fn nodes created", file=sys.stderr)

    # ── Step A: Annotate concrete :Fn nodes matched by (file, def_line) ───────
    q_annotate = """
        UNWIND $batch AS row
        MATCH (fn:Fn)
        WHERE fn.file = row.file AND fn.def_line = row.def_line
        SET fn.impl_trait      = row.trait_name,
            fn.impl_type       = row.type_name,
            fn.impl_trait_args = row.trait_args_str,
            fn.impl_method     = row.method_name,
            fn.impl_spec_key   = row.spec_key
    """
    annotated = 0
    for batch in _batches(triples):
        with driver.session() as s:
            result = s.run(q_annotate, batch=batch)
            annotated += result.consume().counters.properties_set // 5
    print(f"  {annotated:,} :Fn nodes annotated", file=sys.stderr)

    # ── Step B: Create [:IMPLEMENTS] edges ────────────────────────────────────
    # Concrete node matched by (file, def_line); abstract node matched by symbol.
    # When multiple abstract nodes share the same name (trait defined in multiple
    # modules, e.g. GetTracker in payments/operations.rs AND fraud_check/operation.rs),
    # pick the one whose file shares the longest common path prefix with the concrete
    # node's file — computed in Python using os.path.commonprefix.

    # Fetch all abstract :Fn candidate nodes keyed by name
    abstract_by_name: dict[str, list[dict]] = {}
    with driver.session() as s:
        abstract_names = list({t["abstract_name"] for t in triples})
        rows = s.run("""
            UNWIND $names AS n
            MATCH (fn:Fn {name: n})
            RETURN fn.name AS name, fn.symbol AS sym, fn.file AS file
        """, names=abstract_names).data()
        for r in rows:
            abstract_by_name.setdefault(r["name"], []).append(r)

    def _best_abstract_sym(concrete_file: str, abstract_name: str) -> str | None:
        candidates = abstract_by_name.get(abstract_name, [])
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]["sym"]
        # Prefer the candidate whose file shares the most path characters with concrete_file
        best = max(candidates, key=lambda c: len(os.path.commonprefix([concrete_file, c["file"] or ""])))
        return best["sym"]

    for t in triples:
        t["abstract_sym"] = _best_abstract_sym(t["file"], t["abstract_name"])

    valid_triples = [t for t in triples if t.get("abstract_sym")]

    q_edge = """
        UNWIND $batch AS row
        MATCH (concrete:Fn)
        WHERE concrete.file = row.file AND concrete.def_line = row.def_line
        MATCH (abstract:Fn {symbol: row.abstract_sym})
        MERGE (concrete)-[e:IMPLEMENTS]->(abstract)
        SET e.impl_type  = row.type_name,
            e.trait_args = row.trait_args_str,
            e.spec_key   = row.spec_key
    """
    edges_created = 0
    for batch in _batches(valid_triples):
        with driver.session() as s:
            result = s.run(q_edge, batch=batch)
            edges_created += result.consume().counters.relationships_created
    print(f"  {edges_created:,} [:IMPLEMENTS] edges created", file=sys.stderr)


# ── Neo4j: write Layer 2 ──────────────────────────────────────────────────────

def write_generic_call_annotations(generic_calls: dict[str, list[dict]], driver):
    """
    Annotate [:CALLS] edges with type_param where a turbofish or struct-as-arg
    pattern reveals the concrete type being passed to a generic function.

    Matches by: edge file == rel_file AND edge line == call_line AND
                callee node name ends with callee_short_name.
    """
    flat = []
    for rel_file, calls in generic_calls.items():
        for c in calls:
            flat.append({
                "file":       rel_file,
                "line":       c["line"],
                "callee":     c["callee"],
                "type_param": c["type_param"],
            })

    print(f"  Writing {len(flat):,} generic call annotations …", file=sys.stderr)

    q = """
        UNWIND $batch AS row
        MATCH ()-[r:CALLS]->(b:Fn)
        WHERE r.file = row.file
          AND r.line = row.line
          AND b.name ENDS WITH row.callee
        SET r.type_param = row.type_param
    """

    # Clear existing annotations first (own transaction, fully committed before writes)
    with driver.session() as s:
        s.run("MATCH ()-[r:CALLS]->() REMOVE r.type_param")

    # Write in small independent transactions to avoid deadlocks.
    # Each batch is its own committed transaction — no overlapping locks.
    import time
    annotated = 0
    for batch in _batches(flat, size=500):   # smaller batches → fewer lock conflicts
        retries = 5
        while retries:
            try:
                with driver.session() as s:
                    result = s.run(q, batch=batch)
                    annotated += result.consume().counters.properties_set
                break
            except Exception as e:
                if "Deadlock" in str(e) and retries > 1:
                    retries -= 1
                    time.sleep(0.3)
                else:
                    raise
    print(f"  {annotated:,} [:CALLS] edges annotated with type_param", file=sys.stderr)


# ── Summary ───────────────────────────────────────────────────────────────────

def print_summary(driver):
    with driver.session() as s:
        impl_count = s.run(
            "MATCH (fn:Fn) WHERE fn.impl_trait IS NOT NULL RETURN count(fn) AS n"
        ).single()["n"]
        edge_count = s.run(
            "MATCH ()-[r:IMPLEMENTS]->() RETURN count(r) AS n"
        ).single()["n"]
        call_count = s.run(
            "MATCH ()-[r:CALLS]->() WHERE r.type_param IS NOT NULL RETURN count(r) AS n"
        ).single()["n"]
        synthetic_count = s.run(
            "MATCH ()-[r:CALLS {synthetic: true}]->() RETURN count(r) AS n"
        ).single()["n"]

        top_traits = s.run("""
            MATCH (fn:Fn)
            WHERE fn.impl_trait IS NOT NULL
            RETURN fn.impl_trait AS trait, count(*) AS cnt
            ORDER BY cnt DESC LIMIT 10
        """).data()

    print(f"\n  :Fn nodes with impl_trait  : {impl_count:,}", file=sys.stderr)
    print(f"  [:IMPLEMENTS] edges        : {edge_count:,}", file=sys.stderr)
    print(f"  [:CALLS] edges with type   : {call_count:,}", file=sys.stderr)
    print(f"  [:CALLS] synthetic edges   : {synthetic_count:,}", file=sys.stderr)
    print(f"\n  Top traits by impl count:", file=sys.stderr)
    for r in top_traits:
        print(f"    {r['trait']:40s} {r['cnt']:4d} impls", file=sys.stderr)


# ── Entry point ───────────────────────────────────────────────────────────────

def main(src_root: str | None = None) -> None:
    root = src_root or os.environ.get("SRC_ROOT", "")
    if not root or not os.path.isdir(root):
        print("Error: set SRC_ROOT to the hyperswitch repo root.", file=sys.stderr)
        sys.exit(1)

    driver = GraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)

    print("[1/5] Scanning impl blocks …", file=sys.stderr)
    impl_records = collect_all_impls(root)

    print("[2/5] Writing trait impl annotations to Neo4j …", file=sys.stderr)
    write_impl_annotations(impl_records, driver)

    print("[3/5] Scanning generic call sites …", file=sys.stderr)
    generic_calls = collect_all_generic_calls(root)

    print("[4/5] Writing generic call annotations to Neo4j …", file=sys.stderr)
    write_generic_call_annotations(generic_calls, driver)

    print("[5/5] Synthesizing CALLS edges for async_trait impl bodies …", file=sys.stderr)
    synthetic_edges = collect_async_trait_call_edges(impl_records, root)
    write_synthetic_calls(synthetic_edges, driver)

    print_summary(driver)
    driver.close()

    print("\nDone. Neo4j now has impl_trait/impl_type on :Fn nodes,", file=sys.stderr)
    print("      [:IMPLEMENTS] edges, type_param on [:CALLS] edges,", file=sys.stderr)
    print("      and synthetic CALLS edges for async_trait impl bodies.", file=sys.stderr)


if __name__ == "__main__":
    main()
