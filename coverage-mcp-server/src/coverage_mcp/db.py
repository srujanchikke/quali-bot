"""
MongoDB query layer for the MCP server.

All reads go through this module. The MCP server never touches raw JSON files
when MONGO_URI is configured — the sync service has already parsed and stored
everything in a query-friendly shape.

Collections:
  builds        — one doc per build, holds totals + metadata
  file_coverage — one doc per (build_id, path), holds line + function stats
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from pymongo import MongoClient, ASCENDING, DESCENDING

from .config import config


@lru_cache(maxsize=1)
def _client() -> MongoClient:
    return MongoClient(config.MONGO_URI, serverSelectionTimeoutMS=10_000)


def db():
    return _client()[config.MONGO_DB]


# ── Builds ────────────────────────────────────────────────────────────────────

def list_builds() -> list[dict]:
    """All builds sorted newest first."""
    return list(
        db().builds.find(
            {},
            {"_id": 0, "build_id": 1, "branch": 1, "commit": 1,
             "created_at": 1, "line_pct": 1, "func_pct": 1},
            sort=[("created_at", DESCENDING)],
        )
    )


def get_build(build_id: str) -> dict | None:
    if build_id == "latest":
        build_id = _resolve_latest()
    return db().builds.find_one({"build_id": build_id}, {"_id": 0})


def _resolve_latest() -> str:
    doc = db().builds.find_one({}, {"build_id": 1}, sort=[("created_at", DESCENDING)])
    if not doc:
        raise FileNotFoundError("No builds in database.")
    return doc["build_id"]


def resolve_tag(tag: str) -> str:
    return _resolve_latest() if tag == "latest" else tag


# ── File coverage ─────────────────────────────────────────────────────────────

_FILE_PROJ = {
    "_id": 0, "path": 1,
    "line_covered": 1, "line_missed": 1, "line_total": 1, "line_pct": 1,
    "func_covered": 1, "func_missed": 1, "func_total": 1, "func_pct": 1,
    "uncovered_lines": 1, "uncovered_funcs": 1,
}

_SORT_KEYS: dict[str, tuple] = {
    "missed_lines":   ("line_missed",  DESCENDING),
    "missed_funcs":   ("func_missed",  DESCENDING),
    "line_pct":       ("line_pct",     ASCENDING),
    "func_pct":       ("func_pct",     ASCENDING),
    "filename":       ("path",         ASCENDING),
}


def get_file(build_id: str, path_substring: str) -> list[dict]:
    build_id = resolve_tag(build_id)
    return list(
        db().file_coverage.find(
            {"build_id": build_id, "path": {"$regex": path_substring}},
            _FILE_PROJ,
        )
    )


def get_folder(
    build_id: str,
    folder: str,
    sort_by: str = "missed_lines",
    top_n: int = 20,
) -> list[dict]:
    build_id = resolve_tag(build_id)
    sort_field, sort_dir = _SORT_KEYS.get(sort_by, ("line_missed", DESCENDING))
    return list(
        db().file_coverage.find(
            {"build_id": build_id, "path": {"$regex": f"^{folder}"}},
            _FILE_PROJ,
            sort=[(sort_field, sort_dir)],
            limit=top_n,
        )
    )


def get_uncovered(
    build_id: str,
    file_filter: str = "",
    folder_filter: str = "",
    limit: int = 50,
) -> list[dict]:
    build_id = resolve_tag(build_id)
    q: dict[str, Any] = {"build_id": build_id, "line_missed": {"$gt": 0}}
    if file_filter:
        q["path"] = {"$regex": file_filter}
    if folder_filter:
        q["path"] = {"$regex": f"^{folder_filter}"}
    return list(
        db().file_coverage.find(
            q,
            {**_FILE_PROJ, "uncovered_lines": 1, "uncovered_funcs": 1},
            sort=[("line_missed", DESCENDING)],
            limit=limit,
        )
    )


def list_files(
    build_id: str,
    prefix: str = "",
    sort_by: str = "missed_lines",
    limit: int = 50,
) -> list[dict]:
    build_id = resolve_tag(build_id)
    q: dict[str, Any] = {"build_id": build_id}
    if prefix:
        q["path"] = {"$regex": f"^{prefix}"}
    sort_field, sort_dir = _SORT_KEYS.get(sort_by, ("line_missed", DESCENDING))
    return list(
        db().file_coverage.find(
            q,
            {"_id": 0, "path": 1, "line_pct": 1, "line_missed": 1,
             "func_pct": 1, "func_missed": 1},
            sort=[(sort_field, sort_dir)],
            limit=limit,
        )
    )


def get_zero_coverage_files(build_id: str, prefix: str = "", limit: int = 50) -> list[dict]:
    """Files where line_pct == 0 (never touched by any test)."""
    build_id = resolve_tag(build_id)
    q: dict[str, Any] = {"build_id": build_id, "line_pct": 0, "line_total": {"$gt": 0}}
    if prefix:
        q["path"] = {"$regex": f"^{prefix}"}
    return list(
        db().file_coverage.find(
            q,
            {"_id": 0, "path": 1, "line_total": 1, "line_missed": 1,
             "func_total": 1, "func_missed": 1},
            sort=[("line_missed", DESCENDING)],
            limit=limit,
        )
    )


def search_function(build_id: str, function_name: str) -> list[dict]:
    """Find a function by name substring across all files."""
    build_id = resolve_tag(build_id)
    pipeline = [
        {"$match": {"build_id": build_id}},
        {"$project": {
            "path": 1,
            "line_pct": 1,
            "func_pct": 1,
            "uncovered_funcs": 1,
            "all_funcs": {"$concatArrays": [
                {"$ifNull": ["$uncovered_funcs", []]},
            ]},
        }},
        {"$unwind": "$all_funcs"},
        {"$match": {"all_funcs.name": {"$regex": function_name, "$options": "i"}}},
        {"$project": {
            "_id": 0,
            "path": 1,
            "func_name": "$all_funcs.name",
            "start_line": "$all_funcs.start",
            "covered": {"$literal": False},
        }},
    ]
    missed = list(db().file_coverage.aggregate(pipeline))

    # Also search covered functions — stored differently, need $where or separate index
    # For now return missed functions with covered=False, covered ones need schema change
    return missed


def compare_builds(base_id: str, head_id: str, paths: list[str] | None = None) -> dict:
    """
    Compare coverage between two builds.
    If paths provided, only compare those files (for PR diff use case).
    Returns { regressions, improvements, new_files, removed_files, summary }
    """
    base_id = resolve_tag(base_id)
    head_id = resolve_tag(head_id)

    q_base: dict[str, Any] = {"build_id": base_id}
    q_head: dict[str, Any] = {"build_id": head_id}
    if paths:
        regex = "|".join(paths)
        q_base["path"] = {"$regex": regex}
        q_head["path"] = {"$regex": regex}

    proj = {"_id": 0, "path": 1, "line_pct": 1, "func_pct": 1,
            "line_missed": 1, "func_missed": 1}

    base_files = {f["path"]: f for f in db().file_coverage.find(q_base, proj)}
    head_files = {f["path"]: f for f in db().file_coverage.find(q_head, proj)}

    regressions  = []
    improvements = []
    new_files    = []
    removed_files = []

    all_paths = set(base_files) | set(head_files)
    for path in all_paths:
        if path not in base_files:
            new_files.append({"path": path, **head_files[path]})
            continue
        if path not in head_files:
            removed_files.append({"path": path, **base_files[path]})
            continue

        b = base_files[path]
        h = head_files[path]
        line_delta = round(h["line_pct"] - b["line_pct"], 2)
        func_delta = round(h.get("func_pct", 0) - b.get("func_pct", 0), 2)

        if line_delta < -1 or func_delta < -1:
            regressions.append({
                "path": path,
                "line_pct_before": b["line_pct"], "line_pct_after": h["line_pct"], "line_delta": line_delta,
                "func_pct_before": b.get("func_pct", 0), "func_pct_after": h.get("func_pct", 0), "func_delta": func_delta,
            })
        elif line_delta > 1 or func_delta > 1:
            improvements.append({
                "path": path,
                "line_pct_before": b["line_pct"], "line_pct_after": h["line_pct"], "line_delta": line_delta,
                "func_pct_before": b.get("func_pct", 0), "func_pct_after": h.get("func_pct", 0), "func_delta": func_delta,
            })

    regressions.sort(key=lambda x: x["line_delta"])
    improvements.sort(key=lambda x: -x["line_delta"])

    base_build = get_build(base_id) or {}
    head_build = get_build(head_id) or {}

    return {
        "base":         {"build_id": base_id, "line_pct": base_build.get("line_pct", 0), "func_pct": base_build.get("func_pct", 0)},
        "head":         {"build_id": head_id, "line_pct": head_build.get("line_pct", 0), "func_pct": head_build.get("func_pct", 0)},
        "regressions":  regressions,
        "improvements": improvements,
        "new_files":    new_files,
        "removed_files": removed_files,
    }


def get_test_priority(build_id: str, prefix: str = "", limit: int = 30) -> list[dict]:
    """
    Rank files by impact score = (func_missed * 3) + line_missed.
    Higher score = more bang for the buck when writing tests.
    """
    build_id = resolve_tag(build_id)
    pipeline = [
        {"$match": {
            "build_id": build_id,
            **({"path": {"$regex": f"^{prefix}"}} if prefix else {}),
            "$or": [{"line_missed": {"$gt": 0}}, {"func_missed": {"$gt": 0}}],
        }},
        {"$addFields": {
            "impact_score": {
                "$add": [
                    {"$multiply": ["$func_missed", 3]},
                    "$line_missed",
                ]
            }
        }},
        {"$sort": {"impact_score": -1}},
        {"$limit": limit},
        {"$project": {
            "_id": 0, "path": 1,
            "line_pct": 1, "line_missed": 1,
            "func_pct": 1, "func_missed": 1,
            "impact_score": 1,
        }},
    ]
    return list(db().file_coverage.aggregate(pipeline))


def folder_totals(build_id: str, folder: str) -> dict:
    build_id = resolve_tag(build_id)
    pipeline = [
        {"$match": {"build_id": build_id, "path": {"$regex": f"^{folder}"}}},
        {"$group": {
            "_id": None,
            "files":        {"$sum": 1},
            "line_covered": {"$sum": "$line_covered"},
            "line_missed":  {"$sum": "$line_missed"},
            "line_total":   {"$sum": "$line_total"},
            "func_covered": {"$sum": "$func_covered"},
            "func_missed":  {"$sum": "$func_missed"},
            "func_total":   {"$sum": "$func_total"},
        }},
    ]
    result = list(db().file_coverage.aggregate(pipeline))
    if not result:
        return {}
    r = result[0]
    r["line_pct"] = round(r["line_covered"] / r["line_total"] * 100, 2) if r["line_total"] else 0.0
    r["func_pct"] = round(r["func_covered"] / r["func_total"] * 100, 2) if r["func_total"] else 0.0
    return r
