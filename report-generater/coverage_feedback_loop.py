#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import coverage_flow_gap as cfg


def print_leaf_line_hits(
    leaf: dict[str, Any], profile: dict[str, dict[int, int]], repo_root: Path
) -> None:
    span = cfg._first_fn_body_span_lines(str(leaf.get("source", "")), int(leaf.get("def_line", 0)))
    if span is None:
        print("--- Per-line LCOV hits ---\n(could not derive leaf body span)", file=sys.stderr)
        return
    file_rel = str(leaf["file"]).replace("\\", "/")
    hits = profile.get(file_rel, {})
    src = (repo_root / file_rel).read_text(encoding="utf-8", errors="replace").splitlines()
    print("\n--- Per-line LCOV hits (leaf body span) ---", file=sys.stderr)
    print("  line         hits  source", file=sys.stderr)
    print("  " + "-" * 74, file=sys.stderr)
    for ln in range(span.start_line, span.end_line + 1):
        count = "(no DA)" if ln not in hits else str(hits[ln])
        text = src[ln - 1].rstrip() if 0 < ln <= len(src) else ""
        print(f"  {ln:5d}  {count:>12}  {text}", file=sys.stderr)
    print("  " + "-" * 74, file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description="Leaf vs lcov diff report.")
    ap.add_argument("--chain-artifact", type=Path, required=True)
    ap.add_argument("--lcov", type=Path, required=True)
    ap.add_argument("--repo-root", type=Path, default=Path("."))
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--print-line-hits", action="store_true")
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args()

    repo_root = args.repo_root.resolve()
    if not args.chain_artifact.is_file():
        print(f"Missing chain artifact: {args.chain_artifact}", file=sys.stderr)
        return 1
    if not args.lcov.is_file():
        print(f"Missing lcov: {args.lcov}", file=sys.stderr)
        return 1

    chain_doc = json.loads(args.chain_artifact.read_text(encoding="utf-8"))
    leaf = cfg.extract_leaf_from_chain_artifact(chain_doc)
    if leaf is None:
        print("No target leaf found in chain artifact.", file=sys.stderr)
        return 1

    profile = cfg.build_normalized_lcov(cfg.parse_lcov_records(args.lcov), repo_root)
    gap, err = cfg.compute_leaf_gap(leaf, profile)
    d = {
        "kind": "leaf_uncovered_lines",
        "leaf": {"name": leaf["name"], "file": leaf["file"], "def_line": leaf["def_line"]},
        "gaps": [gap] if gap else [],
        "error": err,
    }

    report: dict[str, Any] = {
        "path_flow_model": cfg.PATH_FLOW_MODEL,
        "pipeline": "diff_only_pl_empty",
        "pl": [],
        "run_records": [],
        "lcov_path": str(args.lcov.resolve()),
        "d": d,
        "context": {
            "path_flow_model": cfg.PATH_FLOW_MODEL,
            "leaf": {"name": leaf["name"], "file": leaf["file"], "def_line": leaf["def_line"]},
            "reachability": cfg.reachability_hints_from_artifact(chain_doc),
            "chain_artifact_path": str(args.chain_artifact.resolve()),
        },
        "audit_trail": None,
        "LEAF_public": {"name": leaf["name"], "file": leaf["file"], "def_line": leaf["def_line"]},
        "CHAIN_ARTIFACT": str(args.chain_artifact.resolve()),
    }

    if args.print_line_hits:
        print_leaf_line_hits(leaf, profile, repo_root)

    text = json.dumps(report, indent=2)
    print(text)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
