"""
LLVM Coverage MCP Server

Tools:
  summarize_report        — overall line coverage
  get_folder_coverage     — aggregate line coverage for a folder prefix
  get_file_coverage       — line coverage + uncovered line numbers for a file
  get_uncovered_lines     — uncovered lines grouped by file, filtered by file/folder
  list_files              — all files sorted by missed lines
  list_builds             — available build tags
"""

import asyncio
import hashlib
import hmac
import logging
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from .config import config
from .fetcher import fetch_coverage_json, list_available_tags
from .parser import parse_json, CoverageReport

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

server = Server("coverage-mcp-server")

_report_cache: dict[str, CoverageReport] = {}


def _load_report(tag: str) -> CoverageReport:
    if tag not in _report_cache:
        data = fetch_coverage_json(tag)
        _report_cache[tag] = parse_json(data, tag)
    return _report_cache[tag]


# ── Tool definitions ───────────────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="summarize_report",
            description="Overall line coverage for a build tag.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tag": {"type": "string", "description": "Build tag. Use list_builds to find tags."},
                },
                "required": ["tag"],
            },
        ),
        Tool(
            name="get_folder_coverage",
            description="Aggregate line coverage for all files under a folder path prefix.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tag": {"type": "string"},
                    "folder": {"type": "string", "description": "Folder path prefix to match."},
                    "sort_by": {
                        "type": "string",
                        "enum": ["missed_lines", "line_pct", "filename"],
                        "default": "missed_lines",
                    },
                    "top_n": {"type": "integer", "default": 20},
                },
                "required": ["tag", "folder"],
            },
        ),
        Tool(
            name="get_file_coverage",
            description=(
                "Line coverage for a specific file. "
                "Matches on substring of the full path. "
                "Also returns the uncovered line numbers."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "tag": {"type": "string"},
                    "file": {"type": "string", "description": "Filename or path substring."},
                },
                "required": ["tag", "file"],
            },
        ),
        Tool(
            name="get_uncovered_lines",
            description=(
                "List uncovered lines (execution count == 0) grouped by file. "
                "Filter by file substring or folder prefix."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "tag": {"type": "string"},
                    "file": {"type": "string", "description": "File path substring filter."},
                    "folder": {"type": "string", "description": "Folder path prefix filter."},
                    "limit": {"type": "integer", "default": 50, "description": "Max files to show."},
                },
                "required": ["tag"],
            },
        ),
        Tool(
            name="list_files",
            description="List files in the report with line coverage stats.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tag": {"type": "string"},
                    "prefix": {"type": "string", "description": "Optional path prefix filter."},
                    "sort_by": {
                        "type": "string",
                        "enum": ["missed_lines", "line_pct", "filename"],
                        "default": "missed_lines",
                    },
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["tag"],
            },
        ),
        Tool(
            name="list_builds",
            description="List build tags available.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
    ]


# ── Handlers ───────────────────────────────────────────────────────────────

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        match name:
            case "summarize_report":     return await _summarize_report(arguments)
            case "get_folder_coverage":  return await _get_folder_coverage(arguments)
            case "get_file_coverage":    return await _get_file_coverage(arguments)
            case "get_uncovered_lines":  return await _get_uncovered_lines(arguments)
            case "list_files":           return await _list_files(arguments)
            case "list_builds":          return await _list_builds(arguments)
            case _:
                return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except FileNotFoundError as e:
        return [TextContent(type="text", text=f"[Not Found] {e}")]
    except Exception as e:
        logger.exception(f"Error in tool '{name}'")
        return [TextContent(type="text", text=f"[Error] {type(e).__name__}: {e}")]


async def _summarize_report(args: dict) -> list[TextContent]:
    report = await asyncio.to_thread(_load_report, args["tag"])
    t = report.totals
    text = (
        f"## Coverage Summary — tag: {report.tag}\n\n"
        f"| Metric | Covered | Total   | Missed  | %      |\n"
        f"|--------|---------|---------|---------|--------|\n"
        f"| Lines  | {t.covered:>7,} | {t.total:>7,} | {t.missed:>7,} | {t.percent:>5.2f}% |\n\n"
        f"Files in report: {len(report.files):,}\n"
        f"Note: this report contains line-level coverage only (no function/region data)."
    )
    return [TextContent(type="text", text=text)]


async def _get_folder_coverage(args: dict) -> list[TextContent]:
    report = await asyncio.to_thread(_load_report, args["tag"])
    folder = args["folder"].rstrip("/")
    sort_by = args.get("sort_by", "missed_lines")
    top_n = int(args.get("top_n", 20))

    matched = [f for f in report.files if folder in f.filename]
    if not matched:
        return [TextContent(type="text", text=f"No files found matching folder: '{folder}'")]

    total   = sum(f.lines.total   for f in matched)
    covered = sum(f.lines.covered for f in matched)
    missed  = sum(f.lines.missed  for f in matched)
    pct     = round(covered / total * 100, 2) if total else 0.0

    key_fn = {
        "missed_lines": lambda f: -f.lines.missed,
        "line_pct":     lambda f: f.lines.percent,
        "filename":     lambda f: f.filename,
    }.get(sort_by, lambda f: -f.lines.missed)

    lines = [
        f"## Folder Coverage — '{folder}' (tag: {args['tag']})",
        f"Files matched: {len(matched)}",
        "",
        f"| Metric | Covered | Total   | Missed  | %      |",
        f"|--------|---------|---------|---------|--------|",
        f"| Lines  | {covered:>7,} | {total:>7,} | {missed:>7,} | {pct:>5.2f}% |",
        "",
        f"### Top {top_n} Files (sorted by {sort_by})",
    ]
    for f in sorted(matched, key=key_fn)[:top_n]:
        lines.append(
            f"  {f.filename}\n"
            f"    lines: {f.lines.covered}/{f.lines.total} ({f.lines.percent:.1f}%)  missed: {f.lines.missed}"
        )
    return [TextContent(type="text", text="\n".join(lines))]


async def _get_file_coverage(args: dict) -> list[TextContent]:
    report = await asyncio.to_thread(_load_report, args["tag"])
    query = args["file"]

    matched = [f for f in report.files if query in f.filename]
    if not matched:
        return [TextContent(type="text", text=f"No files found matching: '{query}'")]

    lines = [f"## File Coverage — '{query}' (tag: {args['tag']})", f"Matches: {len(matched)}", ""]
    for f in matched:
        uncov_preview = f.uncovered_lines[:30]
        more = len(f.uncovered_lines) - len(uncov_preview)
        lines += [
            f"### {f.filename}",
            f"| Metric | Covered | Total | Missed | %      |",
            f"|--------|---------|-------|--------|--------|",
            f"| Lines  | {f.lines.covered:>7,} | {f.lines.total:>5,} | {f.lines.missed:>6,} | {f.lines.percent:>5.2f}% |",
        ]
        if uncov_preview:
            suffix = f"  (+{more} more)" if more else ""
            lines.append(f"  Uncovered lines: {uncov_preview}{suffix}")
        lines.append("")
    return [TextContent(type="text", text="\n".join(lines))]


async def _get_uncovered_lines(args: dict) -> list[TextContent]:
    report = await asyncio.to_thread(_load_report, args["tag"])
    file_filter   = args.get("file", "").strip()
    folder_filter = args.get("folder", "").rstrip("/").strip()
    limit = int(args.get("limit", 50))

    def matches(f) -> bool:
        if file_filter and file_filter not in f.filename:
            return False
        if folder_filter and not f.filename.startswith(folder_filter):
            return False
        return True

    matched = [f for f in report.files if matches(f) and f.lines.missed > 0]
    matched = sorted(matched, key=lambda f: -f.lines.missed)

    if not matched:
        return [TextContent(type="text", text="No uncovered lines found for the given filter.")]

    scope_parts = []
    if file_filter:   scope_parts.append(f"file: '{file_filter}'")
    if folder_filter: scope_parts.append(f"folder: '{folder_filter}'")
    scope = "  |  ".join(scope_parts) if scope_parts else "entire build"

    lines = [
        f"## Uncovered Lines — tag: {args['tag']}",
        f"Scope: {scope}",
        f"Showing {min(limit, len(matched))} of {len(matched)} files with missed lines",
        "",
    ]
    for f in matched[:limit]:
        uncov = f.uncovered_lines[:20]
        more = len(f.uncovered_lines) - len(uncov)
        suffix = f"  (+{more} more)" if more else ""
        lines.append(f"### {f.filename}  ({f.lines.missed} missed lines)")
        lines.append(f"  Lines: {uncov}{suffix}")
        lines.append("")

    return [TextContent(type="text", text="\n".join(lines))]


async def _list_files(args: dict) -> list[TextContent]:
    report = await asyncio.to_thread(_load_report, args["tag"])
    prefix  = args.get("prefix", "")
    sort_by = args.get("sort_by", "missed_lines")
    limit   = int(args.get("limit", 50))

    files = [f for f in report.files if not prefix or f.filename.startswith(prefix)]
    key_fn = {
        "missed_lines": lambda f: -f.lines.missed,
        "line_pct":     lambda f: f.lines.percent,
        "filename":     lambda f: f.filename,
    }.get(sort_by, lambda f: -f.lines.missed)
    files = sorted(files, key=key_fn)

    lines = [
        f"## File List — tag: {args['tag']}",
        f"Filter: '{prefix or '(none)'}' | {len(files)} files | sorted by {sort_by}",
        f"Showing top {min(limit, len(files))}",
        "",
        f"{'File':<80} {'Line%':>6}  {'Missed':>7}",
        "-" * 97,
    ]
    for f in files[:limit]:
        lines.append(f"{f.filename:<80} {f.lines.percent:>5.1f}%  {f.lines.missed:>7,}")
    return [TextContent(type="text", text="\n".join(lines))]


async def _list_builds(args: dict) -> list[TextContent]:
    tags = await asyncio.to_thread(list_available_tags)
    if not tags:
        return [TextContent(type="text", text="No builds found.")]
    return [TextContent(type="text", text="## Available Tags\n" + "\n".join(f"  {t}" for t in tags))]


# ── Entry point ────────────────────────────────────────────────────────────

def main():
    try:
        config.validate()
    except RuntimeError as e:
        print(f"[coverage-mcp-server] {e}", file=sys.stderr)
        sys.exit(1)

    logger.info(f"Starting | transport={config.MCP_TRANSPORT}")

    if config.MCP_TRANSPORT == "stdio":
        asyncio.run(_run_stdio())
    elif config.MCP_TRANSPORT == "sse":
        _run_sse()
    else:
        print(f"Unknown MCP_TRANSPORT: {config.MCP_TRANSPORT}", file=sys.stderr)
        sys.exit(1)


async def _run_stdio():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def _run_sse():
    try:
        from mcp.server.sse import SseServerTransport
        import uvicorn

        stored_hash = config.MCP_API_KEY_HASH
        if not stored_hash:
            print("[coverage-mcp-server] MCP_API_KEY_HASH must be set for SSE transport.", file=sys.stderr)
            sys.exit(1)

        sse = SseServerTransport("/messages")

        def _check_auth(scope) -> bool:
            headers = dict(scope.get("headers", []))
            auth = headers.get(b"authorization", b"").decode()
            if not auth.startswith("Bearer "):
                return False
            provided = auth.removeprefix("Bearer ").strip()
            return hmac.compare_digest(
                hashlib.sha256(provided.encode()).hexdigest(),
                stored_hash,
            )

        async def _send_401(send):
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"www-authenticate", b"Bearer")]})
            await send({"type": "http.response.body",
                        "body": b'{"error":"Unauthorized"}', "more_body": False})

        _OAUTH_DISCOVERY_PATHS = {
            "/.well-known/oauth-authorization-server",
            "/.well-known/openid-configuration",
            "/.well-known/oauth-protected-resource",
        }

        async def _send_404(send):
            await send({"type": "http.response.start", "status": 404,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body",
                        "body": b'{"error":"not_found"}', "more_body": False})

        async def app(scope, receive, send):
            if scope["type"] == "lifespan":
                while True:
                    event = await receive()
                    if event["type"] == "lifespan.startup":
                        await send({"type": "lifespan.startup.complete"})
                    elif event["type"] == "lifespan.shutdown":
                        await send({"type": "lifespan.shutdown.complete"})
                        return
                return

            if scope["type"] != "http":
                return

            path   = scope.get("path", "")
            method = scope.get("method", "")
            client = scope.get("client", ("unknown",))[0]

            # Return 404 for OAuth discovery paths without auth check.
            # This signals to MCP clients that OAuth is not supported,
            # causing them to fall back to Bearer token auth directly.
            if any(path.startswith(p) for p in _OAUTH_DISCOVERY_PATHS) or path == "/register":
                await _send_404(send)
                return

            if not _check_auth(scope):
                logger.warning(f"Auth failed | path={path} client={client}")
                await _send_401(send)
                return

            if path == "/sse" and method == "GET":
                async with sse.connect_sse(scope, receive, send) as (r, w):
                    await server.run(r, w, server.create_initialization_options())
            elif path.startswith("/messages") and method == "POST":
                await sse.handle_post_message(scope, receive, send)
            else:
                await send({"type": "http.response.start", "status": 404,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"Not found", "more_body": False})

        uvicorn.run(app, host=config.MCP_HOST, port=config.MCP_PORT)
    except ImportError:
        print("SSE needs: pip install uvicorn", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
