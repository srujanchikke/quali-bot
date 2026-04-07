"""
annotate_guards.py  —  Part 2: Annotate CALLS edges with conditional guards
============================================================================

For every CALLS edge in Neo4j, read the source file at the call-site line,
parse it with Tree-sitter (Rust grammar), walk up the AST from the call node
to find the innermost conditional guard wrapping that call, and write the
result back onto the edge.

Properties written on each [:CALLS] edge:
  guard_type        : 'if' | 'if_let' | 'match' | 'while' | 'while_let' | 'for' | null
  guard_condition   : raw source text of the condition / scrutinee (first 300 chars)
  match_arm_pattern : (match only) raw text of the arm pattern containing the call,
                      e.g. "Connector::Stripe | Connector::Paypal" or "_"

A null guard_type means the call is unconditional at its immediate scope.

Algorithm per call-site:
  1. Parse the source file with Tree-sitter (cached — each file parsed once).
  2. Walk the node tree to find the deepest node whose span includes the
     call-site line.
  3. Walk up node.parent chain looking for a guard node
     (if_expression, match_expression, while_expression, for_expression).
  4. Stop the upward walk at any function_item or closure_expression
     boundary — we only care about guards *inside* the enclosing function.
  5. Extract the condition text from the guard node.

Usage:
  SRC_ROOT=/path/to/hyperswitch python annotate_guards.py
"""

import os
import sys

from tree_sitter import Language, Parser
import tree_sitter_rust

from hs_indexer.config import cfg
from hs_indexer.db import get_driver

# ── Config ─────────────────────────────────────────────────────────────────────

BATCH_SIZE = cfg.indexing.guards_batch_size

# ── Tree-sitter setup ──────────────────────────────────────────────────────────

RUST_LANG = Language(tree_sitter_rust.language())
_parser   = Parser(RUST_LANG)

# Guard node types we care about — these introduce a conditional branch
_GUARD_TYPES = frozenset({
    "if_expression",
    "match_expression",
    "while_expression",
    "for_expression",
})

# Stop walking up at these — they represent a function/closure boundary
_STOP_TYPES = frozenset({
    "function_item",
    "closure_expression",
    "source_file",
})


# ── File cache ─────────────────────────────────────────────────────────────────

_file_cache: dict = {}   # abs_path → (source_bytes, parsed_tree)


def _get_tree(abs_path: str):
    """Return (source_bytes, tree) for a file, cached."""
    if abs_path in _file_cache:
        return _file_cache[abs_path]
    try:
        with open(abs_path, "rb") as f:
            src = f.read()
    except (OSError, FileNotFoundError):
        _file_cache[abs_path] = (None, None)
        return (None, None)
    tree = _parser.parse(src)
    _file_cache[abs_path] = (src, tree)
    return (src, tree)


# ── AST helpers ────────────────────────────────────────────────────────────────

def _deepest_node_at_line(node, target_row: int):
    """
    Return the deepest (most specific) named node whose span covers target_row.
    target_row is 0-indexed (Tree-sitter convention).
    """
    if node.start_point[0] > target_row or node.end_point[0] < target_row:
        return None  # this node doesn't span the target line

    # Try children first — a child will be more specific
    for child in node.named_children:
        result = _deepest_node_at_line(child, target_row)
        if result is not None:
            return result

    return node  # this node spans the line and no named child does


def _condition_text(guard_node, src: bytes) -> tuple[str, str]:
    """
    Return (guard_type_str, condition_text) for a guard AST node.

    Tree-sitter Rust named fields used:
      if_expression   → .condition  (expression or let_condition)
      match_expression → .value      (the scrutinee)
      while_expression → .condition  (expression or let_condition)
      for_expression  → .pattern + .value
    """
    t = guard_node.type

    def _text(n) -> str:
        return src[n.start_byte:n.end_byte].decode("utf-8", errors="replace").strip()

    if t == "if_expression":
        cond = guard_node.child_by_field_name("condition")
        if cond is None:
            return ("if", "")
        # Distinguish plain `if` from `if let`
        if cond.type == "let_condition":
            return ("if_let", _text(cond))
        return ("if", _text(cond))

    if t == "match_expression":
        val = guard_node.child_by_field_name("value")
        return ("match", _text(val) if val else "")

    if t == "while_expression":
        cond = guard_node.child_by_field_name("condition")
        if cond is None:
            return ("while", "")
        if cond.type == "let_condition":
            return ("while_let", _text(cond))
        return ("while", _text(cond))

    if t == "for_expression":
        pattern = guard_node.child_by_field_name("pattern")
        value   = guard_node.child_by_field_name("value")
        pat_text = _text(pattern) if pattern else "?"
        val_text = _text(value)   if value   else "?"
        return ("for", f"{pat_text} in {val_text}")

    return (t, "")   # fallback — shouldn't happen


def _find_arm_pattern(match_node, call_node, src: bytes) -> str | None:
    """
    Given a match_expression node and a descendant call_node, find the
    match_arm that contains the call and return its pattern text.
    E.g. "Connector::Stripe | Connector::Paypal" or "_".
    Returns None if no match_arm ancestor is found between the two.
    """
    def _txt(n) -> str:
        return src[n.start_byte:n.end_byte].decode("utf-8", errors="replace").strip()

    node = call_node
    while node is not None and node.id != match_node.id:
        if node.type == "match_arm":
            # Verify this arm is a direct child of the match_block of our match_node
            parent = node.parent
            if parent is not None and parent.parent is not None and parent.parent.id == match_node.id:
                pattern = node.child_by_field_name("pattern")
                if pattern:
                    return _txt(pattern)
                return None
        node = node.parent
    return None


# ── Core: find guard for one call site ────────────────────────────────────────

MAX_CONDITION_LEN = 300   # truncate very long conditions


def find_guard(src_root: str, rel_file: str, call_line: int) -> dict | None:
    """
    Return guard info for a call at `call_line` (1-indexed) in `rel_file`,
    or None if the call is not inside any conditional in its enclosing function.

    Returned dict: {guard_type, guard_condition}
    """
    abs_path = os.path.join(src_root, rel_file)
    src, tree = _get_tree(abs_path)
    if src is None or tree is None:
        return None

    target_row = call_line - 1   # convert to 0-indexed

    # Step 1: find the deepest AST node at the call-site line
    start = _deepest_node_at_line(tree.root_node, target_row)
    if start is None:
        return None

    # Step 2: walk up the parent chain looking for an enclosing guard
    node = start.parent
    while node is not None:
        if node.type in _STOP_TYPES:
            break                      # crossed a function boundary — stop

        if node.type in _GUARD_TYPES:
            guard_type, condition = _condition_text(node, src)
            result = {
                "guard_type":      guard_type,
                "guard_condition": condition[:MAX_CONDITION_LEN],
            }
            if node.type == "match_expression":
                arm_pat = _find_arm_pattern(node, start, src)
                if arm_pat:
                    result["match_arm_pattern"] = arm_pat[:MAX_CONDITION_LEN]
            return result

        node = node.parent

    return None   # unconditional call


# ── Neo4j: fetch edges, annotate, write back ──────────────────────────────────

def _batches(lst: list, size: int = BATCH_SIZE):
    for i in range(0, len(lst), size):
        yield lst[i : i + size]


def annotate_all(src_root: str):
    """
    Fetch every CALLS edge from Neo4j that has a file+line, annotate it with
    guard info, and write the result back in batches.
    """
    driver = get_driver()

    with driver.session() as s:
        # Fetch all CALLS edges that have a call-site location
        print("  fetching CALLS edges from Neo4j …")
        rows = s.run("""
            MATCH (a:Fn)-[r:CALLS]->(b:Fn)
            WHERE r.file IS NOT NULL AND r.line IS NOT NULL
            RETURN
                elementId(r)  AS eid,
                r.file        AS file,
                r.line        AS line
        """).data()
        print(f"  {len(rows):,} CALLS edges to annotate")

    # ── Annotate ──────────────────────────────────────────────────────────────
    print("  annotating …  (this walks the source files)")
    annotated    = []   # edges that are inside a guard
    unconditional = 0
    file_errors  = 0
    files_seen   = set()

    for row in rows:
        eid   = row["eid"]
        file  = row["file"]
        line  = row["line"]

        if file not in files_seen:
            files_seen.add(file)

        info = find_guard(src_root, file, line)

        if info is None:
            unconditional += 1
        else:
            annotated.append({"eid": eid, **info})

    print(f"  {len(annotated):,} edges inside a guard")
    print(f"  {unconditional:,} unconditional edges")
    print(f"  {len(files_seen):,} unique source files visited")

    # ── Write back to Neo4j ────────────────────────────────────────────────────
    print("  writing guard annotations to Neo4j …")

    # First, initialise all edges to null (so re-runs are idempotent)
    with driver.session() as s:
        s.run("""
            MATCH ()-[r:CALLS]->()
            SET r.guard_type        = null,
                r.guard_condition   = null,
                r.match_arm_pattern = null
        """)

    # Then set the guarded ones
    q_update = """
        UNWIND $batch AS row
        MATCH ()-[r:CALLS]->()
        WHERE elementId(r) = row.eid
        SET r.guard_type        = row.guard_type,
            r.guard_condition   = row.guard_condition,
            r.match_arm_pattern = row.match_arm_pattern
    """
    with driver.session() as s:
        for batch in _batches(annotated):
            s.run(q_update, batch=batch)

    driver.close()


# ── Guard distribution summary ─────────────────────────────────────────────────

def print_summary(src_root: str):
    """Print a breakdown of guard types found in the annotated graph."""
    driver = get_driver()
    with driver.session() as s:
        rows = s.run("""
            MATCH ()-[r:CALLS]->()
            RETURN
                coalesce(r.guard_type, 'unconditional') AS guard_type,
                count(*) AS cnt
            ORDER BY cnt DESC
        """).data()
    driver.close()

    print("\nGuard type distribution:")
    total = sum(r["cnt"] for r in rows)
    for r in rows:
        pct = 100 * r["cnt"] / total if total else 0
        print(f"  {r['guard_type']:20s}  {r['cnt']:6,}  ({pct:4.1f}%)")


# ── Entry point ────────────────────────────────────────────────────────────────

def main(src_root: str | None = None) -> None:
    root = src_root or os.environ.get("SRC_ROOT", "")
    if not root:
        print("Error: set SRC_ROOT environment variable to the hyperswitch repo root.")
        sys.exit(1)
    if not os.path.isdir(root):
        print(f"Error: SRC_ROOT={root!r} is not a directory.")
        sys.exit(1)

    print(f"[1/2] Annotating CALLS edges with guard info …")
    print(f"      SRC_ROOT = {root}")
    annotate_all(root)

    print("\n[2/2] Summary")
    print_summary(root)

    print("\nDone. CALLS edges now have guard_type / guard_condition properties.")


if __name__ == "__main__":
    main()
