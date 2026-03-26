"""
dead_end_functions.py
=====================
Single-pass reachability check: which functions in the call graph have
NO path (upward through callers) to any v1 API endpoint?

Algorithm:
  1. Load the full graph + tagged endpoints from Neo4j (same as find_impact).
  2. Multi-source BFS *forward* from every endpoint node through the FORWARD
     call graph (i.e. follow callee edges downward).  Every node visited is
     "reachable from an endpoint" — meaning changing it could affect that endpoint.
  3. Any node NOT visited = no endpoint ever calls it (directly or transitively).

Output: dead_end_functions.json
  {
    "reachable_count":  N,
    "dead_end_count":   M,
    "dead_ends": [
      {"name": "foo", "file": "...", "def_line": 42},
      ...
    ]
  }

Usage:
  SRC_ROOT=/path/to/hyperswitch .venv/bin/python3 dead_end_functions.py [--out dead_end_functions.json]
"""

import argparse
import json
import os
import sys
from collections import deque

from neo4j import GraphDatabase

NEO4J_URI  = "bolt://localhost:7687"
NEO4J_AUTH = ("neo4j", "Hyperswitch@123")

# ── same route parser as find_impact so we tag only v1 endpoints ──────────────
import re

def _parse_routes_app(filepath: str) -> list[dict]:
    try:
        with open(filepath, errors="replace") as f:
            content = f.read()
    except OSError:
        return []

    scope_re = re.compile(r'web::scope\(\s*"([^"]*)"\s*\)')
    res_re   = re.compile(r'web::resource\(\s*"([^"]*)"\s*\)')
    route_re = re.compile(r'web::([a-z]+)\(\)\s*\.to\(([a-zA-Z_][a-zA-Z0-9_:]*)\)')

    lines  = content.splitlines()
    tokens = []
    for i, line in enumerate(lines):
        for m in scope_re.finditer(line):
            tokens.append((i, "scope", m.group(1)))
        for m in res_re.finditer(line):
            tokens.append((i, "resource", m.group(1)))
        for m in route_re.finditer(line):
            tokens.append((i, "route", (m.group(1).upper(), m.group(2).split("::")[-1])))

    scope_stack: list = []
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
            # Skip v2 endpoints
            if "v2" in [p for p in full_path.split("/") if p]:
                continue
            routes.append({"method": method, "path": full_path, "handler": handler})

    return routes


def tag_endpoints(src_root: str, driver) -> int:
    routes_file = os.path.join(src_root, "crates", "router", "src", "routes", "app.rs")
    if not os.path.exists(routes_file):
        print(f"  Warning: routes/app.rs not found", file=sys.stderr)
        return 0

    all_routes = _parse_routes_app(routes_file)
    by_handler: dict = {}
    for r in all_routes:
        if r["handler"] not in by_handler:
            by_handler[r["handler"]] = r

    tagged = 0
    with driver.session() as s:
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

    print(f"  tagged {tagged} endpoint nodes", file=sys.stderr)
    return tagged


def load_graph(driver) -> tuple[dict, dict]:
    fn_info: dict = {}
    forward: dict = {}   # caller_sym → [callee_sym, ...]

    with driver.session() as s:
        rows = s.run("""
            MATCH (fn:Fn)
            RETURN fn.symbol    AS sym,
                   fn.name      AS name,
                   fn.file      AS file,
                   fn.def_line  AS def_line,
                   fn.is_endpoint AS is_endpoint
        """).data()
        for r in rows:
            fn_info[r["sym"]] = {
                "name":        r["name"],
                "file":        r["file"],
                "def_line":    r["def_line"],
                "is_endpoint": bool(r.get("is_endpoint")),
            }

        rows = s.run("""
            MATCH (a:Fn)-[:CALLS]->(b:Fn)
            RETURN a.symbol AS caller, b.symbol AS callee
        """).data()
        for r in rows:
            forward.setdefault(r["caller"], []).append(r["callee"])

    print(
        f"  {len(fn_info):,} nodes  |  {sum(len(v) for v in forward.values()):,} forward edges",
        file=sys.stderr,
    )
    return fn_info, forward


def compute_reachable(fn_info: dict, forward: dict) -> set[str]:
    """
    Multi-source BFS downward from every endpoint.
    Returns set of symbols reachable from at least one endpoint.
    """
    visited: set[str] = set()
    queue: deque = deque()

    for sym, info in fn_info.items():
        if info["is_endpoint"]:
            queue.append(sym)
            visited.add(sym)

    while queue:
        sym = queue.popleft()
        for callee in forward.get(sym, []):
            if callee not in visited:
                visited.add(callee)
                queue.append(callee)

    return visited


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="dead_end_functions.json")
    parser.add_argument("--src-root", default=os.environ.get("SRC_ROOT", ""))
    args = parser.parse_args()

    src_root = args.src_root
    if not src_root or not os.path.isdir(src_root):
        print("Error: set SRC_ROOT env var or --src-root to the hyperswitch repo root.", file=sys.stderr)
        sys.exit(1)

    driver = GraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)

    print("[1/3] Tagging v1 API endpoints …", file=sys.stderr)
    tag_endpoints(src_root, driver)

    print("[2/3] Loading call graph …", file=sys.stderr)
    fn_info, forward = load_graph(driver)
    driver.close()

    print("[3/3] Computing reachability from endpoints …", file=sys.stderr)
    reachable = compute_reachable(fn_info, forward)

    # Dead-end = not reachable from any endpoint, and not itself an endpoint,
    # and not a test/generated file
    dead_ends = []
    for sym, info in fn_info.items():
        if sym in reachable:
            continue
        if info.get("is_endpoint"):
            continue
        file = info.get("file") or ""
        if "test" in file or "openapi" in file:
            continue
        dead_ends.append({
            "name":     info["name"],
            "file":     file,
            "def_line": info["def_line"],
        })

    dead_ends.sort(key=lambda x: (x["file"], x["def_line"]))

    result = {
        "reachable_count": len(reachable),
        "dead_end_count":  len(dead_ends),
        "dead_ends":       dead_ends,
    }

    with open(args.out, "w") as f:
        json.dump(result, f, indent=2)

    print(f"\n  Reachable from endpoints : {len(reachable):,}", file=sys.stderr)
    print(f"  Dead-end functions       : {len(dead_ends):,}", file=sys.stderr)
    print(f"  Written to               : {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
