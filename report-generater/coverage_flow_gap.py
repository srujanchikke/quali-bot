#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PATH_FLOW_MODEL: dict[str, str] = {
    "scored_for_coverage": "leaf_function_body_only",
    "leaf": "Terminal function in the chain with role='target'.",
    "chain": "Earlier chain steps are reachability context only.",
    "objective": "Maximize distinct leaf body lines hit at least once.",
}


@dataclass
class BodySpan:
    start_line: int
    end_line: int


def parse_lcov_records(path: Path) -> dict[str, dict[int, int]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    by_file: dict[str, dict[int, int]] = {}
    current_sf: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("SF:"):
            current_sf = line[3:].strip()
            by_file.setdefault(current_sf, {})
            continue
        if line.startswith("DA:") and current_sf:
            m = re.match(r"DA:(\d+),(\d+)", line)
            if m:
                ln = int(m.group(1))
                hits = int(m.group(2))
                by_file[current_sf][ln] = by_file[current_sf].get(ln, 0) + hits
            continue
        if line == "end_of_record":
            current_sf = None
    return by_file


def normalize_sf_key(sf: str, repo_root: Path) -> str:
    p = Path(sf)
    try:
        rel = p.resolve().relative_to(repo_root.resolve())
        return rel.as_posix()
    except ValueError:
        return sf.replace("\\", "/")


def build_normalized_lcov(
    by_file: dict[str, dict[int, int]], repo_root: Path
) -> dict[str, dict[int, int]]:
    out: dict[str, dict[int, int]] = {}
    for sf, lines in by_file.items():
        out[normalize_sf_key(sf, repo_root)] = lines
    return out


def _iter_chain_steps(flow_doc: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for flow in flow_doc.get("flows") or []:
        for step in flow.get("chain") or []:
            steps.append(step)
    return steps


def extract_leaf_from_chain_artifact(flow_doc: dict[str, Any]) -> dict[str, Any] | None:
    """Extract the target leaf function from a flow JSON document.
    
    Handles two formats:
    1. Old format: step has 'source' field with full source code
    2. New format: step has 'full_source' field (may be truncated)
    
    Falls back to reading source from file if source not embedded.
    """
    for step in _iter_chain_steps(flow_doc):
        if step.get("role") == "target":
            source = step.get("source") or step.get("full_source") or ""
            
            # If no embedded source, try to read from file
            if not source and step.get("file"):
                try:
                    file_path = Path(step.get("file", ""))
                    if file_path.is_file():
                        source = file_path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    pass
            
            return {
                "name": step.get("function"),
                "file": step.get("file"),
                "def_line": int(step.get("def_line", 0) or 0),
                "source": source,
            }
    return None


def _first_fn_body_span_lines(source: str, def_line: int) -> BodySpan | None:
    if not source or def_line < 1:
        return None
    m = re.search(r"\bfn\s+[A-Za-z0-9_]+", source)
    if not m:
        return None
    brace0 = source.find("{", m.end())
    if brace0 < 0:
        return None
    depth = 0
    close_pos = -1
    for i, c in enumerate(source[brace0:], start=brace0):
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                close_pos = i
                break
    if close_pos < 0:
        return None
    rel_start = source[:brace0].count("\n")
    rel_end = source[: close_pos + 1].count("\n")
    return BodySpan(start_line=def_line + rel_start, end_line=def_line + rel_end)


def reachability_hints_from_artifact(flow_doc: dict[str, Any]) -> dict[str, Any]:
    endpoints = []
    for e in flow_doc.get("endpoints") or []:
        if isinstance(e, dict):
            endpoints.append(
                {
                    "method": e.get("method"),
                    "path": e.get("path"),
                    "handler": e.get("handler"),
                    "chain": e.get("chain"),
                }
            )
    return {"endpoint_count": len(endpoints), "endpoints": endpoints}


def compute_leaf_gap(
    leaf: dict[str, Any], lcov: dict[str, dict[int, int]]
) -> tuple[dict[str, Any] | None, str | None]:
    file_rel = str(leaf.get("file", "")).replace("\\", "/")
    def_line = int(leaf.get("def_line", 0))
    source = str(leaf.get("source", ""))
    span = _first_fn_body_span_lines(source, def_line)
    if span is None:
        return None, "could_not_parse_body_span"

    hits = lcov.get(file_rel)
    if not hits:
        return None, "no_lcov_for_file"

    lines_in_span = span.end_line - span.start_line + 1
    probed = 0
    hit = 0
    no_da = 0
    zeros: list[int] = []
    for ln in range(span.start_line, span.end_line + 1):
        if ln not in hits:
            no_da += 1
            continue
        probed += 1
        if hits[ln] > 0:
            hit += 1
        else:
            zeros.append(ln)

    if probed == 0:
        ratio = None
        status = "no_lcov_da_in_span"
    elif hit == probed:
        ratio = 1.0
        status = "all_probed_lines_hit"
    elif hit == 0:
        ratio = 0.0
        status = "all_probed_lines_zero"
    else:
        ratio = hit / probed
        status = "partial"

    return (
        {
            "function": leaf.get("name"),
            "file": file_rel,
            "role": "target",
            "def_line": def_line,
            "body_span": {"start": span.start_line, "end": span.end_line},
            "lines_in_span": lines_in_span,
            "lines_without_lcov_da": no_da,
            "lcov_probed_lines": probed,
            "lcov_hit_lines": hit,
            "line_coverage_ratio": ratio,
            "zero_hit_lines": zeros,
            "status": status,
            "note": None,
        },
        None,
    )


def dump_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")
