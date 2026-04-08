"""
Coverage sync service.

Exposes a single HTTP endpoint:
  POST /sync
  Body: {
    "build_id":    "build-142-abc1234",
    "branch":      "main",
    "commit":      "abc1234",
    "line_url":    "https://s3.amazonaws.com/...?X-Amz-Signature=...",
    "function_url": "https://s3.amazonaws.com/...?X-Amz-Signature=..."  (optional)
  }

Jenkins (which has S3 access) generates pre-signed URLs and POSTs them here.
This service downloads the files over plain HTTPS — no AWS credentials needed.

Auth: requests must include  Authorization: Bearer <SYNC_API_KEY>

Collections written:
  builds        — one doc per build (metadata + totals)
  file_coverage — one doc per (build_id, file_path)
"""

import hashlib
import hmac
import json
import logging
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import BulkWriteError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

MONGO_URI       = os.environ["MONGO_URI"]
MONGO_DB        = os.environ.get("MONGO_DB", "coverage")
SYNC_API_KEY_HASH = os.environ["SYNC_API_KEY_HASH"]  # SHA-256 hex of raw key
SYNC_PORT       = int(os.environ.get("SYNC_PORT", "8888"))

# ── DB setup ──────────────────────────────────────────────────────────────────

def get_db():
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10_000)
    db = client[MONGO_DB]
    db.file_coverage.create_index(
        [("build_id", ASCENDING), ("path", ASCENDING)], unique=True
    )
    db.file_coverage.create_index([("build_id", ASCENDING), ("line_missed", DESCENDING)])
    db.file_coverage.create_index([("build_id", ASCENDING), ("func_missed", DESCENDING)])
    db.builds.create_index([("build_id", ASCENDING)], unique=True)
    db.builds.create_index([("created_at", DESCENDING)])
    return db

_db = None

def db():
    global _db
    if _db is None:
        _db = get_db()
    return _db

# ── Auth ──────────────────────────────────────────────────────────────────────

def check_auth(auth_header: str) -> bool:
    if not auth_header.startswith("Bearer "):
        return False
    provided = auth_header.removeprefix("Bearer ").strip()
    return hmac.compare_digest(
        hashlib.sha256(provided.encode()).hexdigest(),
        SYNC_API_KEY_HASH,
    )

# ── Download helpers ──────────────────────────────────────────────────────────

def download_json(url: str) -> dict:
    """Download JSON from a pre-signed HTTPS URL. No AWS credentials needed."""
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    return resp.json()

# ── Parsers ───────────────────────────────────────────────────────────────────

def _line_stats(coverage: list) -> dict:
    """coverage array: None=not instrumented, 0=missed, >0=hit."""
    instrumented  = [x for x in coverage if x is not None]
    covered       = sum(1 for x in instrumented if x > 0)
    missed        = sum(1 for x in instrumented if x == 0)
    total         = len(instrumented)
    pct           = round(covered / total * 100, 2) if total else 0.0
    uncovered     = [i + 1 for i, x in enumerate(coverage) if x == 0]
    return {
        "line_covered":    covered,
        "line_missed":     missed,
        "line_total":      total,
        "line_pct":        pct,
        "uncovered_lines": uncovered,
    }


def parse_line_json(data: dict) -> tuple[dict, list[dict]]:
    """
    Custom tree format (line.json / index.json):
      { coveragePercent, linesCovered, linesMissed, linesTotal, children: {...} }
    """
    totals = {
        "line_covered": data.get("linesCovered", 0),
        "line_missed":  data.get("linesMissed", 0),
        "line_total":   data.get("linesTotal", 0),
        "line_pct":     data.get("coveragePercent", 0.0),
    }
    files = []
    _walk_line_tree(data.get("children", {}), prefix="", files=files)
    return totals, files


def _walk_line_tree(children: dict, prefix: str, files: list) -> None:
    for name, node in children.items():
        path = f"{prefix}/{name}" if prefix else name
        if "coverage" in node:
            files.append({"path": path, **_line_stats(node["coverage"])})
        else:
            _walk_line_tree(node.get("children", {}), path, files)


def parse_function_json(data: dict) -> dict[str, dict]:
    """
    Coveralls format (function.json / function_coverage.json):
      { source_files: [{ name, coverage, functions: [{name, start, exec}] }] }
    """
    result = {}
    for f in data.get("source_files", []):
        path  = f["name"]
        funcs = f.get("functions", [])
        cov   = f.get("coverage", [])

        func_covered = sum(1 for fn in funcs if fn.get("exec"))
        func_missed  = sum(1 for fn in funcs if not fn.get("exec"))
        func_total   = len(funcs)
        func_pct     = round(func_covered / func_total * 100, 2) if func_total else 0.0

        result[path] = {
            **_line_stats(cov),
            "func_covered":    func_covered,
            "func_missed":     func_missed,
            "func_total":      func_total,
            "func_pct":        func_pct,
            "uncovered_funcs": [
                {"name": fn["name"], "start": fn.get("start")}
                for fn in funcs if not fn.get("exec")
            ],
        }
    return result

# ── Merge + insert ────────────────────────────────────────────────────────────

def sync_build(build_id: str, branch: str, commit: str, line_data: dict, func_data: dict | None) -> str:
    line_totals, line_files = parse_line_json(line_data)

    func_map = parse_function_json(func_data) if func_data else {}
    log.info(f"  Parsed {len(line_files)} files from line.json, {len(func_map)} from function.json")

    all_func_covered = sum(v["func_covered"] for v in func_map.values())
    all_func_missed  = sum(v["func_missed"]  for v in func_map.values())
    all_func_total   = sum(v["func_total"]   for v in func_map.values())
    func_pct = round(all_func_covered / all_func_total * 100, 2) if all_func_total else 0.0

    d = db()

    d.builds.update_one(
        {"build_id": build_id},
        {"$set": {
            "build_id":     build_id,
            "branch":       branch,
            "commit":       commit,
            "created_at":   datetime.now(timezone.utc).isoformat(),
            "synced_at":    datetime.now(timezone.utc).isoformat(),
            **line_totals,
            "func_covered": all_func_covered,
            "func_missed":  all_func_missed,
            "func_total":   all_func_total,
            "func_pct":     func_pct,
        }},
        upsert=True,
    )

    docs = []
    for lf in line_files:
        path = lf["path"]
        doc  = {"build_id": build_id, **lf}
        if path in func_map:
            ff = func_map[path]
            doc.update({
                "func_covered":    ff["func_covered"],
                "func_missed":     ff["func_missed"],
                "func_total":      ff["func_total"],
                "func_pct":        ff["func_pct"],
                "uncovered_funcs": ff["uncovered_funcs"],
            })
        else:
            doc.update({"func_covered": 0, "func_missed": 0,
                        "func_total": 0, "func_pct": 0.0, "uncovered_funcs": []})
        docs.append(doc)

    ops = [
        {"updateOne": {
            "filter": {"build_id": build_id, "path": d["path"]},
            "update": {"$set": d},
            "upsert": True,
        }}
        for d in docs
    ]
    try:
        result = d.file_coverage.bulk_write(ops, ordered=False)
        msg = f"Upserted {result.upserted_count} new, modified {result.modified_count} files"
    except BulkWriteError as e:
        msg = f"Partial write error: {e.details['writeErrors'][:2]}"

    log.info(f"  {msg}")
    return msg

# ── HTTP server ───────────────────────────────────────────────────────────────

class SyncHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        log.info(f"{self.address_string()} {format % args}")

    def _respond(self, status: int, body: dict):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/health":
            self._respond(200, {"status": "ok"})
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/sync":
            self._respond(404, {"error": "not found"})
            return

        # Auth
        auth = self.headers.get("Authorization", "")
        if not check_auth(auth):
            self._respond(401, {"error": "unauthorized"})
            return

        # Parse body
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length))
        except Exception:
            self._respond(400, {"error": "invalid JSON"})
            return

        build_id  = body.get("build_id", "").strip()
        branch    = body.get("branch", "")
        commit    = body.get("commit", "")
        line_url  = body.get("line_url", "").strip()
        func_url  = body.get("function_url", "").strip()

        if not build_id or not line_url:
            self._respond(400, {"error": "build_id and line_url are required"})
            return

        log.info(f"Sync triggered | build={build_id} branch={branch} commit={commit}")

        try:
            log.info("  Downloading line.json...")
            line_data = download_json(line_url)

            func_data = None
            if func_url:
                log.info("  Downloading function.json...")
                func_data = download_json(func_url)

            result_msg = sync_build(build_id, branch, commit, line_data, func_data)
            self._respond(200, {"status": "ok", "build_id": build_id, "detail": result_msg})

        except requests.HTTPError as e:
            log.error(f"  Download failed: {e}")
            self._respond(502, {"error": f"download failed: {e}"})
        except Exception as e:
            log.exception(f"  Sync failed for {build_id}")
            self._respond(500, {"error": str(e)})


def main():
    log.info(f"Sync service starting on port {SYNC_PORT}")
    db()  # connect + create indexes on startup
    server = HTTPServer(("0.0.0.0", SYNC_PORT), SyncHandler)
    log.info(f"Listening on 0.0.0.0:{SYNC_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
