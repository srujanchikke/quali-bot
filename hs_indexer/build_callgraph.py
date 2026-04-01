"""
build_callgraph.py  —  Part 1: Call Graph Extraction
=====================================================

Reads index.scip and loads a CALLS graph into Neo4j.

Algorithm (per source file in the index):
  1. Collect every function DEFINITION in the file, sorted by start line.
     These are the potential "caller" nodes.
  2. For every REFERENCE occurrence that points to a callable symbol
     (SCIP convention: callable symbols end with  "()."):
       - Find the innermost function definition whose start line
         is before the reference line  → that is the CALLER.
       - Record the edge:  caller  --[CALLS]-->  callee  @ (file, line)
  3. Deduplicate edges (same caller→callee pair may appear many times
     if the callee is called in a loop or from multiple places in one fn).

Neo4j schema produced:
  Node   (:Fn  {symbol, name, file, def_line})
  Edge   [:CALLS {file, line}]       -- file/line of the call site

Usage:
  python build_callgraph.py [path/to/index.scip]
"""

import sys

try:
    from . import scip_pb2
except ImportError:
    try:
        from scip import scip_pb2
    except ImportError:
        import scip_pb2

from neo4j import GraphDatabase

# ── Config ─────────────────────────────────────────────────────────────────────

SCIP_PATH  = "index.scip"
NEO4J_URI  = "bolt://localhost:7687"
NEO4J_AUTH = ("neo4j", "Hyperswitch@123")

# SCIP symbol_roles bit flags
DEFINITION   = 1
WRITE_ACCESS = 4
READ_ACCESS  = 8

BATCH_SIZE = 5_000


# ── Step 1: Parse index.scip ───────────────────────────────────────────────────

def load_scip(path: str):
    """
    Parse a SCIP index file.

    SCIP files are a sequence of length-delimited protobuf records (one per
    field of the top-level Index message).  We merge them incrementally and
    skip any truncated trailing record gracefully.
    """
    from google.protobuf.internal.decoder import _DecodeVarint

    index = scip_pb2.Index()
    with open(path, "rb") as f:
        data = f.read()

    pos = 0
    while pos < len(data):
        try:
            tag, after_tag = _DecodeVarint(data, pos)
        except Exception:
            break

        if (tag & 0x7) != 2:          # expect length-delimited (wire type 2)
            break

        try:
            length, after_len = _DecodeVarint(data, after_tag)
        except Exception:
            break

        if after_len + length > len(data):
            break                      # truncated trailing record — stop cleanly

        index.MergeFromString(data[pos : after_len + length])
        pos = after_len + length

    return index


# ── Step 2: Extract call graph ─────────────────────────────────────────────────

def _display_name(symbol: str) -> str:
    """
    Derive a short human-readable name from a SCIP symbol string.

    SCIP symbols look like:
      cargo hyperswitch 0.1.0 crates/router/src/.../mod.rs/payments_core().
    We take the last path segment and strip trailing punctuation.
    """
    local = symbol.rsplit("/", 1)[-1]   # last segment after the final '/'
    return local.rstrip("#().")


def _enclosing_fn(ref_line: int, sorted_defs: list) -> tuple | None:
    """
    Return (def_line, symbol) of the innermost callable definition that
    starts before `ref_line` in the same file.

    `sorted_defs` is a list of (def_line, symbol) sorted by def_line asc.
    Only symbols ending in  "()."  are considered callable containers.
    """
    result = None
    for def_line, sym in sorted_defs:
        if def_line > ref_line:
            break
        if sym.endswith(")."):
            result = (def_line, sym)
    return result


def extract_call_graph(index) -> tuple[dict, list]:
    """
    Walk every document in the SCIP index and extract:
      nodes : {symbol → {symbol, name, file, def_line}}
      edges : [(caller_sym, callee_sym, call_site_file, call_site_line)]

    Only CALLS edges are produced (references to callable symbols, i.e.,
    symbols whose SCIP descriptor ends with  "(). ").
    Type references, field accesses, imports etc. are ignored.
    """
    nodes: dict = {}    # symbol → node dict
    edges: list = []    # raw (caller, callee, file, line) tuples

    for doc in index.documents:
        file_path = doc.relative_path

        # ── Collect function DEFINITION occurrences in this file ──────────────
        # These are sorted by start line so _enclosing_fn() can binary-search-style scan.
        defs: list = []     # [(line_0indexed, symbol)]

        for occ in doc.occurrences:
            sym = occ.symbol
            if not sym or sym.startswith("local "):
                continue
            if not (occ.symbol_roles & DEFINITION):
                continue
            if not sym.endswith(")."):          # only callables
                continue

            line = occ.range[0] if occ.range else 0
            defs.append((line, sym))

            # Register node (1-indexed line for display)
            if sym not in nodes:
                nodes[sym] = {
                    "symbol":   sym,
                    "name":     _display_name(sym),
                    "file":     file_path,
                    "def_line": line + 1,
                }

        defs.sort(key=lambda x: x[0])

        if not defs:
            continue    # file has no callable definitions → no edges to produce

        # ── Scan reference occurrences → record CALLS edges ───────────────────
        for occ in doc.occurrences:
            sym = occ.symbol
            if not sym or sym.startswith("local "):
                continue
            if occ.symbol_roles & DEFINITION:
                continue                        # skip self-definition
            if not sym.endswith(")."):          # only calls to callables
                continue

            ref_line = occ.range[0] if occ.range else 0
            caller = _enclosing_fn(ref_line, defs)
            if not caller:
                continue                        # reference outside any function

            _, caller_sym = caller
            edges.append((caller_sym, sym, file_path, ref_line + 1))

    return nodes, edges


# ── Step 3: Load into Neo4j ────────────────────────────────────────────────────

def _batches(lst: list, size: int = BATCH_SIZE):
    for i in range(0, len(lst), size):
        yield lst[i : i + size]


def load_into_neo4j(nodes: dict, raw_edges: list):
    """
    Write the call graph to Neo4j.

    Nodes  →  :Fn {symbol, name, file, def_line}
    Edges  →  [:CALLS {file, line}]

    Edges are deduplicated at the Python level before loading:
    multiple call sites between the same pair (caller, callee) are collapsed
    into one edge (keeping the earliest call-site line number).
    """

    # ── Deduplicate edges: one edge per (caller, callee), earliest line ────────
    seen: dict = {}   # (caller, callee) → {caller, callee, file, line}
    for caller, callee, file, line in raw_edges:
        key = (caller, callee)
        if key not in seen or line < seen[key]["line"]:
            seen[key] = {"caller": caller, "callee": callee, "file": file, "line": line}
    unique_edges = list(seen.values())

    # ── Load ──────────────────────────────────────────────────────────────────
    driver = GraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)

    with driver.session() as s:
        print("  clearing existing graph …")
        s.run("MATCH (n) DETACH DELETE n")

        print("  creating indexes …")
        for stmt in [
            "CREATE INDEX fn_symbol IF NOT EXISTS FOR (n:Fn) ON (n.symbol)",
            "CREATE INDEX fn_name   IF NOT EXISTS FOR (n:Fn) ON (n.name)",
            "CREATE INDEX fn_file   IF NOT EXISTS FOR (n:Fn) ON (n.file)",
        ]:
            s.run(stmt)

        # Nodes
        node_list = list(nodes.values())
        print(f"  loading {len(node_list):,} function/method nodes …")
        q_node = """
            UNWIND $batch AS n
            MERGE (fn:Fn {symbol: n.symbol})
            SET fn.name     = n.name,
                fn.file     = n.file,
                fn.def_line = n.def_line
        """
        for batch in _batches(node_list):
            s.run(q_node, batch=batch)

        # Edges — MATCH both endpoints; if either is external (stdlib/external crate)
        # it won't exist as a node and the MATCH fails silently → edge is skipped.
        # This is intentional: we only want edges within the project.
        print(f"  loading {len(unique_edges):,} CALLS edges …")
        q_edge = """
            UNWIND $batch AS e
            MATCH (a:Fn {symbol: e.caller})
            MATCH (b:Fn {symbol: e.callee})
            MERGE (a)-[r:CALLS]->(b)
            SET r.file = e.file, r.line = e.line
        """
        for batch in _batches(unique_edges):
            s.run(q_edge, batch=batch)

    driver.close()


# ── Entry point ────────────────────────────────────────────────────────────────

def main(scip_path: str | None = None) -> None:
    path = scip_path or SCIP_PATH
    print(f"[1/3] Parsing {path} …")
    index = load_scip(path)
    print(f"      {len(index.documents):,} documents in index")

    print("[2/3] Extracting call graph …")
    nodes, edges = extract_call_graph(index)
    print(f"      {len(nodes):,} function/method nodes")
    print(f"      {len(edges):,} raw CALLS edges  (before dedup)")

    print("[3/3] Loading into Neo4j …")
    load_into_neo4j(nodes, edges)

    print("\nDone. Call graph is in Neo4j.")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
