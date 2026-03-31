#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def extract_chain_files(chain_artifact: dict[str, Any]) -> list[str]:
    files: list[str] = []
    for flow in chain_artifact.get("flows") or []:
        for step in flow.get("chain") or []:
            f = step.get("file")
            if isinstance(f, str) and f not in files:
                files.append(f)
    return files


def index_rust_file(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    fns: list[dict[str, Any]] = []
    fn_re = re.compile(r"^\s*pub\s+(?:async\s+)?fn\s+([A-Za-z0-9_]+)\s*\(")
    for idx, line in enumerate(lines, start=1):
        m = fn_re.search(line)
        if m:
            fns.append({"name": m.group(1), "line": idx})
    return {"path": str(path), "line_count": len(lines), "functions": fns}


def main() -> int:
    ap = argparse.ArgumentParser(description="Build lightweight source index for chain files.")
    ap.add_argument("--repo-root", type=Path, required=True)
    ap.add_argument("--chain-artifact", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    repo_root = args.repo_root.resolve()
    chain = read_json(args.chain_artifact)
    rel_files = extract_chain_files(chain)

    indexed_files: list[dict[str, Any]] = []
    missing_files: list[str] = []
    for rel in rel_files:
        p = (repo_root / rel).resolve()
        if not p.is_file():
            missing_files.append(rel)
            continue
        if p.suffix == ".rs":
            indexed_files.append(index_rust_file(p))
        else:
            indexed_files.append({"path": str(p), "line_count": None, "functions": []})

    out: dict[str, Any] = {
        "chain_artifact": str(args.chain_artifact.resolve()),
        "repo_root": str(repo_root),
        "indexed_file_count": len(indexed_files),
        "missing_files": missing_files,
        "files": indexed_files,
    }
    args.out.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
