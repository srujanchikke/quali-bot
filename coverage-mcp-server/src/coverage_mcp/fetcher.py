"""
Coverage report fetcher — local filesystem.

Current mode: reads a single JSON file from a configured path.
  COVERAGE_FILE_PATH=/Users/you/Downloads/index.json

Deployment path (to be wired up):
  When Jenkins mounts coverage artifacts into the container, point
  COVERAGE_DIR at that mount and files will be resolved as:
    {COVERAGE_DIR}/coverage_{tag}.json
  No code changes needed — just switch from COVERAGE_FILE_PATH to COVERAGE_DIR.
"""

import json
import os
from pathlib import Path

from .config import config


def fetch_coverage_json(tag: str) -> dict:
    path = _resolve_path(tag)
    if not path.exists():
        raise FileNotFoundError(f"Coverage file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def list_available_tags() -> list[str]:
    """
    Single-file mode: return the configured tag (or 'local') so tools
    that call list_builds still return something useful.
    If COVERAGE_DIR is set, list all coverage_{tag}.json files found there.
    """
    if config.COVERAGE_DIR:
        base = Path(config.COVERAGE_DIR)
        tags = []
        for f in sorted(base.glob("coverage_*.json"), reverse=True):
            tag = f.stem.removeprefix("coverage_")
            if tag:
                tags.append(tag)
        return tags

    # Single-file mode — derive a display tag from the filename
    if config.COVERAGE_FILE_PATH:
        stem = Path(config.COVERAGE_FILE_PATH).stem   # e.g. "index" or "coverage_42"
        tag = stem.removeprefix("coverage_") if stem != "index" else "local"
        return [tag]

    return []


def _resolve_path(tag: str) -> Path:
    # Directory mode: multiple builds, each as coverage_{tag}.json
    if config.COVERAGE_DIR:
        return Path(config.COVERAGE_DIR) / f"coverage_{tag}.json"

    # Single-file mode: always the same file regardless of tag
    return Path(config.COVERAGE_FILE_PATH).expanduser()
