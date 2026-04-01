#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


def infer_symbol_name(function_name: str) -> str:
    if "#" in function_name:
        return function_name.split("#")[-1].strip()
    return function_name.strip()


def locate_rust_definition(lines: list[str], function_name: str, def_line: int) -> int:
    symbol = infer_symbol_name(function_name)
    pattern = re.compile(rf"\bfn\s+{re.escape(symbol)}\b")
    start = max(0, def_line - 120)
    end = min(len(lines), def_line + 240)

    for index in range(start, end):
        if pattern.search(lines[index]):
            return index

    return max(def_line - 1, 0)


def extract_rust_snippet(
    source_text: str,
    function_name: str,
    def_line: int,
    max_lines: int = 220,
) -> str:
    lines = source_text.replace("\r\n", "\n").split("\n")
    if not lines:
        return ""

    start_index = locate_rust_definition(lines, function_name, def_line)
    snippet_lines: list[str] = []
    brace_depth = 0
    started = False

    for index in range(start_index, min(len(lines), start_index + max_lines)):
        line = lines[index]
        snippet_lines.append(line)

        for char in line:
            if char == "{":
                brace_depth += 1
                started = True
            elif char == "}":
                brace_depth = max(brace_depth - 1, 0)

        if started and brace_depth == 0 and index > start_index:
            break

    return "\n".join(snippet_lines).rstrip() + "\n"


def enrich_chain_sources(doc: dict[str, Any], repo_root: Path) -> bool:
    changed = False
    for flow in doc.get("flows", []):
        for step in flow.get("chain", []):
            file_path = step.get("file")
            def_line = step.get("def_line")
            if not file_path or not isinstance(def_line, int):
                continue

            abs_path = repo_root / file_path
            if not abs_path.is_file():
                continue

            try:
                source_text = abs_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            snippet = extract_rust_snippet(
                source_text,
                str(step.get("function", "")),
                def_line,
            )
            if snippet and step.get("source") != snippet:
                step["source"] = snippet
                changed = True

    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill chain[].source into flow JSON files.")
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("json_files", nargs="+", type=Path)
    args = parser.parse_args()

    for json_file in args.json_files:
      data = json.loads(json_file.read_text(encoding="utf-8"))
      if enrich_chain_sources(data, args.repo_root):
          json_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
