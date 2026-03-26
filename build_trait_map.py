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

def _extract_impl_methods(impl_body: TSNode, src: bytes) -> list[str]:
    """Return list of method names defined inside an impl body."""
    methods = []
    for child in impl_body.named_children:
        if child.type == "function_item":
            name_node = child.child_by_field_name("name")
            if name_node:
                methods.append(_ts_text(name_node, src))
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
                    # Grab each type argument; keep only simple identifiers
                    for child in type_args.named_children:
                        if child.type in ("type_identifier", "scoped_type_identifier"):
                            type_name = _short_name(_ts_text(child, src))
                            if type_name:
                                calls.append({
                                    "callee":     callee,
                                    "type_param": type_name,
                                    "line":       fn_node.start_point[0] + 1,
                                })

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

    for dirpath, dirnames, filenames in os.walk(search_root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            if not fname.endswith(".rs"):
                continue
            abs_path = os.path.join(dirpath, fname)
            calls = _extract_generic_calls_from_file(abs_path)
            # Merge variable-binding inferred calls (no duplicates by line)
            vb_calls = _extract_variable_binding_calls_from_file(abs_path)
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

    for dirpath, dirnames, filenames in os.walk(search_root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            if not fname.endswith(".rs"):
                continue
            abs_path = os.path.join(dirpath, fname)
            calls = _extract_variable_binding_calls_from_file(abs_path)
            if calls:
                rel_path = os.path.relpath(abs_path, src_root)
                result[rel_path] = calls

    total = sum(len(v) for v in result.values())
    print(f"  {total} variable-binding generic call sites found across {len(result)} files", file=sys.stderr)
    return result


def _extract_variable_binding_calls_from_file(abs_path: str) -> list[dict]:
    """
    Find variable-binding generic call sites in one file.

    Per function body, builds a local bindings dict (var_name → ConcreteType) by
    walking let_declaration nodes:
      - If value is struct_expression → extract name field, short_name it
      - If value is call_expression and function text contains '::' →
        extract short_name(fn_text.rsplit('::', 1)[0])

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

                if concrete_type and var_name:
                    bindings[var_name] = concrete_type
                return  # don't recurse into let_declaration children

            for child in node.named_children:
                _collect_bindings(child)

        _collect_bindings(body)

        # Pass 2: walk call_expressions — if first arg is a bound identifier, emit
        def _collect_calls(node: TSNode):
            if node.type == "call_expression":
                fn_node_call = node.child_by_field_name("function")
                args_node = node.child_by_field_name("arguments")
                if fn_node_call and args_node:
                    callee = _short_name(_ts_text(fn_node_call, src))
                    first_arg = next(
                        (c for c in args_node.named_children if c.type != "comment"),
                        None,
                    )
                    if first_arg and first_arg.type == "identifier":
                        ident = _ts_text(first_arg, src)
                        if ident in bindings:
                            calls.append({
                                "callee":     callee,
                                "type_param": bindings[ident],
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


# ── Neo4j: write Layer 1 ──────────────────────────────────────────────────────

def _batches(lst, size=BATCH_SIZE):
    for i in range(0, len(lst), size):
        yield lst[i:i+size]


def write_impl_annotations(impl_records: list[dict], driver):
    """
    For each (trait, type, method) triple:
      1. Annotate the concrete :Fn node (name == "type#trait#method") with
         impl_trait and impl_type properties.
      2. Create [:IMPLEMENTS] edge from the concrete :Fn to the abstract :Fn
         (name == "trait#method") if both exist.
    """
    # Flatten to individual (trait, type, method) tuples — now with trait_args + spec_key
    triples = []
    for r in impl_records:
        trait_args = r.get("trait_args", ())
        args_str   = ",".join(trait_args)
        for method in r["methods"]:
            spec_key = _make_specialization_key(r["trait_name"], r["type_name"], trait_args, method)
            triples.append({
                "trait_name":    r["trait_name"],
                "trait_args_str": args_str,         # comma-joined for Neo4j string storage
                "type_name":     r["type_name"],
                "method_name":   method,
                "concrete_name": f"{r['type_name']}#{r['trait_name']}#{method}",
                "abstract_name": f"{r['trait_name']}#{method}",
                "spec_key":      spec_key,
            })

    print(f"  {len(triples):,} (trait, type, method) triples to annotate", file=sys.stderr)

    # Step A: annotate concrete :Fn nodes with full specialization properties
    q_annotate = """
        UNWIND $batch AS row
        MATCH (fn:Fn)
        WHERE fn.name = row.concrete_name
        SET fn.impl_trait      = row.trait_name,
            fn.impl_type       = row.type_name,
            fn.impl_trait_args = row.trait_args_str,
            fn.impl_method     = row.method_name,
            fn.impl_spec_key   = row.spec_key
    """
    # Step B: create [:IMPLEMENTS] edges with spec metadata on the edge too
    q_edge = """
        UNWIND $batch AS row
        MATCH (concrete:Fn {name: row.concrete_name})
        MATCH (abstract:Fn  {name: row.abstract_name})
        MERGE (concrete)-[e:IMPLEMENTS]->(abstract)
        SET e.impl_type      = row.type_name,
            e.trait_args     = row.trait_args_str,
            e.spec_key       = row.spec_key
    """

    with driver.session() as s:
        # Reset existing annotations first (idempotent)
        s.run("MATCH (fn:Fn) REMOVE fn.impl_trait, fn.impl_type, fn.impl_trait_args, fn.impl_method, fn.impl_spec_key")
        s.run("MATCH ()-[r:IMPLEMENTS]->() DELETE r")

        annotated = 0
        for batch in _batches(triples):
            result = s.run(q_annotate, batch=batch)
            annotated += result.consume().counters.properties_set // 5   # 5 props set per node
        print(f"  {annotated:,} :Fn nodes annotated", file=sys.stderr)

        edges_created = 0
        for batch in _batches(triples):
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

        top_traits = s.run("""
            MATCH (fn:Fn)
            WHERE fn.impl_trait IS NOT NULL
            RETURN fn.impl_trait AS trait, count(*) AS cnt
            ORDER BY cnt DESC LIMIT 10
        """).data()

    print(f"\n  :Fn nodes with impl_trait  : {impl_count:,}", file=sys.stderr)
    print(f"  [:IMPLEMENTS] edges        : {edge_count:,}", file=sys.stderr)
    print(f"  [:CALLS] edges with type   : {call_count:,}", file=sys.stderr)
    print(f"\n  Top traits by impl count:", file=sys.stderr)
    for r in top_traits:
        print(f"    {r['trait']:40s} {r['cnt']:4d} impls", file=sys.stderr)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    src_root = os.environ.get("SRC_ROOT", "")
    if not src_root or not os.path.isdir(src_root):
        print("Error: set SRC_ROOT to the hyperswitch repo root.", file=sys.stderr)
        sys.exit(1)

    driver = GraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)

    print("[1/4] Scanning impl blocks …", file=sys.stderr)
    impl_records = collect_all_impls(src_root)

    print("[2/4] Writing trait impl annotations to Neo4j …", file=sys.stderr)
    write_impl_annotations(impl_records, driver)

    print("[3/4] Scanning generic call sites …", file=sys.stderr)
    generic_calls = collect_all_generic_calls(src_root)

    print("[4/4] Writing generic call annotations to Neo4j …", file=sys.stderr)
    write_generic_call_annotations(generic_calls, driver)

    print_summary(driver)
    driver.close()

    print("\nDone. Neo4j now has impl_trait/impl_type on :Fn nodes,", file=sys.stderr)
    print("      [:IMPLEMENTS] edges, and type_param on [:CALLS] edges.", file=sys.stderr)
    print("Next: run find_impact.py — it will use this data automatically.", file=sys.stderr)
