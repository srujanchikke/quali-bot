"""
Coverage MCP Server

File-format tools (tree JSON — line coverage):
  list_builds             — available build tags
  summarize_report        — overall line coverage
  get_folder_coverage     — aggregate line coverage for a folder prefix
  get_file_coverage       — line coverage + uncovered line numbers
  get_uncovered_lines     — uncovered lines grouped by file
  list_files              — all files sorted by missed lines

LLVM-format tools (llvm-cov export JSON — line + function + region):
  get_overall_coverage    — overall line/function/region coverage
  get_connector_coverage  — coverage for a specific connector
  is_function_tested      — check if a function is covered

MongoDB tools (require MONGO_URI — populated by sync service):
  get_uncovered_functions — unexecuted functions grouped by file
  get_zero_coverage_files — files never touched by any test
  search_function         — find a specific function and check coverage
  compare_builds          — coverage delta between two builds
  get_test_priority       — files ranked by impact score

Prompts (4):
  pr_coverage_review         — full PR coverage report
  write_test_plan            — structured test plan for a folder/file
  coverage_regression_report — what regressed between two builds
  onboarding_coverage_tour   — worst areas tour for new QA engineer
"""

import asyncio
import hashlib
import hmac
import logging
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent, Prompt, PromptMessage, PromptArgument, GetPromptResult

from .config import config
from .fetcher import fetch_coverage_json, list_available_tags
from .parser import parse_json, CoverageReport
from .llvm_parser import parse_llvm_json, is_llvm_format, LLVMCoverageReport

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

server = Server("coverage-mcp-server")

_report_cache: dict[str, CoverageReport] = {}
_llvm_cache: dict[str, LLVMCoverageReport] = {}


def _load_report(tag: str) -> CoverageReport:
    if tag not in _report_cache:
        data = fetch_coverage_json(tag)
        if is_llvm_format(data):
            _report_cache[tag] = _llvm_to_tree(parse_llvm_json(data, tag))
        else:
            _report_cache[tag] = parse_json(data, tag)
    return _report_cache[tag]


def _llvm_to_tree(llvm: LLVMCoverageReport) -> CoverageReport:
    """Adapt an LLVMCoverageReport into a CoverageReport for tree-format tool handlers."""
    from .parser import LineStat, FileCoverage
    totals = LineStat(
        covered=llvm.lines.covered,
        missed=llvm.lines.count - llvm.lines.covered,
        total=llvm.lines.count,
        percent=round(llvm.lines.percent, 2),
    )
    files = []
    for f in llvm.files:
        files.append(FileCoverage(
            filename=f.filename,
            lines=LineStat(
                covered=f.lines.covered,
                missed=f.lines.count - f.lines.covered,
                total=f.lines.count,
                percent=round(f.lines.percent, 2),
            ),
            uncovered_lines=[],  # not available in LLVM summary format
        ))
    from .parser import CoverageReport as CR
    return CR(tag=llvm.tag, totals=totals, files=files)


def _load_llvm_report(tag: str) -> LLVMCoverageReport:
    if tag not in _llvm_cache:
        data = fetch_coverage_json(tag)
        if not is_llvm_format(data):
            raise ValueError(
                f"Coverage file for tag '{tag}' is not in LLVM format. "
                "get_overall_coverage, get_connector_coverage, and is_function_tested "
                "require an LLVM coverage JSON (llvm-cov export output)."
            )
        _llvm_cache[tag] = parse_llvm_json(data, tag)
    return _llvm_cache[tag]


# ── Tool definitions ───────────────────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        # ── File-format tools (tree JSON) ──────────────────────────────────
        Tool(
            name="list_builds",
            description="List available build tags with coverage percentages.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
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
                    "tag":     {"type": "string"},
                    "folder":  {"type": "string", "description": "Folder path prefix to match."},
                    "sort_by": {"type": "string", "enum": ["missed_lines", "line_pct", "filename"], "default": "missed_lines"},
                    "top_n":   {"type": "integer", "default": 20},
                },
                "required": ["tag", "folder"],
            },
        ),
        Tool(
            name="get_file_coverage",
            description="Line coverage for a specific file. Matches on substring of the full path. Returns uncovered line numbers.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tag":  {"type": "string"},
                    "file": {"type": "string", "description": "Filename or path substring."},
                },
                "required": ["tag", "file"],
            },
        ),
        Tool(
            name="get_uncovered_lines",
            description="List uncovered lines (execution count == 0) grouped by file. Filter by file substring or folder prefix.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tag":    {"type": "string"},
                    "file":   {"type": "string", "description": "File path substring filter."},
                    "folder": {"type": "string", "description": "Folder path prefix filter."},
                    "limit":  {"type": "integer", "default": 50},
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
                    "tag":     {"type": "string"},
                    "prefix":  {"type": "string", "description": "Optional path prefix filter."},
                    "sort_by": {"type": "string", "enum": ["missed_lines", "line_pct", "filename"], "default": "missed_lines"},
                    "limit":   {"type": "integer", "default": 50},
                },
                "required": ["tag"],
            },
        ),
        # ── LLVM-format tools ───────────────────────────────────────────────
        Tool(
            name="get_overall_coverage",
            description="Overall line, function, and region coverage from an LLVM coverage report (llvm-cov export format).",
            inputSchema={
                "type": "object",
                "properties": {
                    "tag": {"type": "string", "description": "Build tag. Use list_builds to find tags."},
                },
                "required": ["tag"],
            },
        ),
        Tool(
            name="get_connector_coverage",
            description="Aggregate line, function, and region coverage for a specific connector (e.g. 'stripe', 'adyen'). Requires LLVM format.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tag":        {"type": "string"},
                    "connector":  {"type": "string", "description": "Connector name substring, e.g. 'stripe', 'adyen'."},
                    "show_files": {"type": "boolean", "default": True, "description": "Include per-file breakdown."},
                },
                "required": ["tag", "connector"],
            },
        ),
        Tool(
            name="is_function_tested",
            description="Check whether a function (by name substring) is covered. Requires LLVM format. Answers: 'Is create_payment tested?'",
            inputSchema={
                "type": "object",
                "properties": {
                    "tag":           {"type": "string"},
                    "function_name": {"type": "string", "description": "Function name substring. Examples: 'stripe', 'create_payment', 'authorize'."},
                    "limit":         {"type": "integer", "default": 20},
                },
                "required": ["tag", "function_name"],
            },
        ),
        # ── MongoDB tools ───────────────────────────────────────────────────
        Tool(
            name="get_uncovered_functions",
            description="List unexecuted functions with line numbers grouped by file. Filter by file or folder. Requires MongoDB.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tag":    {"type": "string"},
                    "file":   {"type": "string", "description": "File path substring filter."},
                    "folder": {"type": "string", "description": "Folder path prefix filter."},
                    "limit":  {"type": "integer", "default": 50},
                },
                "required": ["tag"],
            },
        ),
        Tool(
            name="get_zero_coverage_files",
            description="Files that have zero line coverage — never executed by any test. Easiest wins for coverage improvement. Requires MongoDB.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tag":    {"type": "string"},
                    "prefix": {"type": "string", "description": "Optional path prefix filter."},
                    "limit":  {"type": "integer", "default": 50},
                },
                "required": ["tag"],
            },
        ),
        Tool(
            name="search_function",
            description="Search for a specific function by name and check if it is covered. Useful for PR reviews. Requires MongoDB.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tag":           {"type": "string"},
                    "function_name": {"type": "string", "description": "Function name or substring (case-insensitive)."},
                },
                "required": ["tag", "function_name"],
            },
        ),
        Tool(
            name="compare_builds",
            description="Coverage delta between two builds — regressions, improvements, new/removed files. Pass changed_files to restrict to a PR diff. Requires MongoDB.",
            inputSchema={
                "type": "object",
                "properties": {
                    "base_tag":      {"type": "string", "description": "Base build tag (e.g. main branch build)."},
                    "head_tag":      {"type": "string", "description": "Head build tag (e.g. PR branch build)."},
                    "changed_files": {"type": "array", "items": {"type": "string"}, "description": "Optional file paths to restrict comparison to."},
                },
                "required": ["base_tag", "head_tag"],
            },
        ),
        Tool(
            name="get_test_priority",
            description="Rank files by impact score (func_missed × 3 + line_missed). Higher = more value to write tests for. Requires MongoDB.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tag":    {"type": "string"},
                    "prefix": {"type": "string", "description": "Optional path prefix to restrict to a module."},
                    "limit":  {"type": "integer", "default": 30},
                },
                "required": ["tag"],
            },
        ),
    ]


# ── Prompt definitions ─────────────────────────────────────────────────────────

@server.list_prompts()
async def list_prompts() -> list[Prompt]:
    return [
        Prompt(
            name="pr_coverage_review",
            description="Full PR coverage review. Gets changed files from git, checks each file, finds uncovered functions, detects regressions, formats a PR comment.",
            arguments=[
                PromptArgument(name="head_tag",  description="Build tag for the PR branch.",                  required=True),
                PromptArgument(name="base_tag",  description="Build tag for the base branch (e.g. main).",    required=True),
                PromptArgument(name="pr_number", description="PR number for context.",                         required=False),
            ],
        ),
        Prompt(
            name="write_test_plan",
            description="Generate a structured test plan for a folder or file — uncovered functions, priority order, suggested test types.",
            arguments=[
                PromptArgument(name="tag",    description="Build tag to analyse.",                    required=True),
                PromptArgument(name="target", description="File path or folder prefix to plan for.",  required=True),
            ],
        ),
        Prompt(
            name="coverage_regression_report",
            description="Detailed report of what coverage regressed between two builds — files, functions, delta, root cause hypothesis.",
            arguments=[
                PromptArgument(name="base_tag", description="Build tag before the change.", required=True),
                PromptArgument(name="head_tag", description="Build tag after the change.",  required=True),
            ],
        ),
        Prompt(
            name="onboarding_coverage_tour",
            description="Tour of the worst-covered areas for a new QA engineer — where to focus, what's never been tested, quick wins.",
            arguments=[
                PromptArgument(name="tag", description="Build tag (use 'latest' for current state).", required=True),
            ],
        ),
    ]


@server.get_prompt()
async def get_prompt(name: str, arguments: dict) -> GetPromptResult:
    match name:
        case "pr_coverage_review":         return _prompt_pr_review(arguments)
        case "write_test_plan":            return _prompt_test_plan(arguments)
        case "coverage_regression_report": return _prompt_regression(arguments)
        case "onboarding_coverage_tour":   return _prompt_onboarding(arguments)
        case _:
            raise ValueError(f"Unknown prompt: {name}")


def _prompt_pr_review(args: dict) -> GetPromptResult:
    head_tag   = args.get("head_tag", "")
    base_tag   = args.get("base_tag", "")
    pr_number  = args.get("pr_number", "")
    pr_context = f"PR #{pr_number}" if pr_number else "this PR"
    text = f"""You are a QA engineer reviewing coverage for {pr_context}.
Head build: `{head_tag}` (PR branch)  |  Base build: `{base_tag}` (base branch)

Follow these steps:

## Step 1 — Get changed files
Run `git diff {base_tag}...HEAD --name-only` to get files changed in this PR.

## Step 2 — Compare coverage
Call `compare_builds(base_tag="{base_tag}", head_tag="{head_tag}", changed_files=[...])`.
Note regressions.

## Step 3 — Check each changed file
Call `get_file_coverage(tag="{head_tag}", file="<path>")` for each changed file.

## Step 4 — Find uncovered functions
For files with func_missed > 0, call `get_uncovered_functions(tag="{head_tag}", file="<path>")`.

## Step 5 — Zero-coverage files
Call `get_zero_coverage_files(tag="{head_tag}")` filtered to changed files.

## Step 6 — Format PR comment

### Coverage Report — {pr_context}

**Summary**
- Line%: base → head (delta) | Func%: base → head (delta)
- Files changed: N | Regressions: N | Zero-coverage: N

**Regressions** (if any)
| File | Line% Δ | Func% Δ |

**Uncovered functions in changed files**
| File | Function | Line |

**Zero-coverage files** (if any)

**Well-covered files** (no action needed)

Be specific — include file paths and function names."""
    return GetPromptResult(
        description=f"PR coverage review: {head_tag} vs {base_tag}",
        messages=[PromptMessage(role="user", content=TextContent(type="text", text=text))],
    )


def _prompt_test_plan(args: dict) -> GetPromptResult:
    tag    = args.get("tag", "latest")
    target = args.get("target", "")
    text = f"""You are a QA engineer creating a test plan for `{target}` (build: `{tag}`).

## Step 1 — Coverage overview
Call `get_folder_coverage(tag="{tag}", folder="{target}", sort_by="missed_funcs", top_n=20)`.

## Step 2 — Priority ranking
Call `get_test_priority(tag="{tag}", prefix="{target}", limit=20)`.

## Step 3 — Uncovered functions
For the top 10 files by impact score, call `get_uncovered_functions(tag="{tag}", file="<path>")`.

## Step 4 — Zero coverage files
Call `get_zero_coverage_files(tag="{tag}", prefix="{target}")`.

## Step 5 — Write the test plan

### Test Plan — `{target}` (build: `{tag}`)

**Coverage baseline**: Line X% | Functions X% | Zero-coverage files: N

**Priority 1 — Critical (0% coverage)**
For each file: path, suggested test type (unit/integration/e2e), functions to cover.

**Priority 2 — High impact (func_missed > 5)**
Same format.

**Priority 3 — Quick wins (func_missed 1-5)**
Same format.

**Estimated effort**: test files needed, complexity estimate."""
    return GetPromptResult(
        description=f"Test plan for {target} (build: {tag})",
        messages=[PromptMessage(role="user", content=TextContent(type="text", text=text))],
    )


def _prompt_regression(args: dict) -> GetPromptResult:
    base_tag = args.get("base_tag", "")
    head_tag = args.get("head_tag", "")
    text = f"""You are a QA engineer investigating a coverage regression.
Base: `{base_tag}`  →  Head: `{head_tag}`

## Step 1 — Overall delta
Call `summarize_report(tag="{base_tag}")` and `summarize_report(tag="{head_tag}")`.

## Step 2 — File comparison
Call `compare_builds(base_tag="{base_tag}", head_tag="{head_tag}")`.

## Step 3 — Drill into top regressions
For the top 5 regressed files, call `get_uncovered_functions(tag="{head_tag}", file="<path>")`.

## Step 4 — Write the report

### Coverage Regression Report
**Builds:** `{base_tag}` → `{head_tag}`

**Overall delta**
| Metric | Before | After | Delta |

**Regressed files** (worst first)
For each: path, line delta, func delta, functions that lost coverage.

**Improved files** (acknowledge wins)

**Root cause hypothesis**
What likely caused the regression?

**Recommended actions**
Specific steps to restore coverage."""
    return GetPromptResult(
        description=f"Regression report: {base_tag} → {head_tag}",
        messages=[PromptMessage(role="user", content=TextContent(type="text", text=text))],
    )


def _prompt_onboarding(args: dict) -> GetPromptResult:
    tag = args.get("tag", "latest")
    text = f"""You are giving a new QA engineer a tour of coverage (build: `{tag}`).

## Step 1 — Big picture
Call `summarize_report(tag="{tag}")`.

## Step 2 — Top-level breakdown
Call `get_folder_coverage(tag="{tag}", folder="crates", sort_by="missed_funcs", top_n=15)`.
Identify the 3-5 modules with biggest gaps.

## Step 3 — Never-tested files
Call `get_zero_coverage_files(tag="{tag}", limit=30)`.

## Step 4 — Highest impact targets
Call `get_test_priority(tag="{tag}", limit=20)`.

## Step 5 — Sample deep dive
Pick the top file from Step 4 and call `get_uncovered_functions(tag="{tag}", file="<path>")`.

## Step 6 — Write the tour

### Welcome to Coverage — Build `{tag}`

**The big picture**: plain English summary — interpret the numbers, don't just repeat them.

**Where the gaps are** (top 5 modules): what each module does, coverage%, why it matters.

**Files never touched** (top 10): path + line count as proxy for complexity.

**Where to start** (top 5 high-impact files): path, impact score, uncovered functions, suggested first test.

**How to use the tools**: brief guide with example queries."""
    return GetPromptResult(
        description=f"Onboarding coverage tour (build: {tag})",
        messages=[PromptMessage(role="user", content=TextContent(type="text", text=text))],
    )


# ── Tool handlers ──────────────────────────────────────────────────────────────

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        match name:
            # File-format tools
            case "list_builds":            return await _list_builds(arguments)
            case "summarize_report":        return await _summarize_report(arguments)
            case "get_folder_coverage":     return await _get_folder_coverage(arguments)
            case "get_file_coverage":       return await _get_file_coverage(arguments)
            case "get_uncovered_lines":     return await _get_uncovered_lines(arguments)
            case "list_files":              return await _list_files(arguments)
            # LLVM-format tools
            case "get_overall_coverage":    return await _get_overall_coverage(arguments)
            case "get_connector_coverage":  return await _get_connector_coverage(arguments)
            case "is_function_tested":      return await _is_function_tested(arguments)
            # MongoDB tools
            case "get_uncovered_functions": return await _get_uncovered_functions(arguments)
            case "get_zero_coverage_files": return await _get_zero_coverage_files(arguments)
            case "search_function":         return await _search_function(arguments)
            case "compare_builds":          return await _compare_builds(arguments)
            case "get_test_priority":       return await _get_test_priority(arguments)
            case _:
                return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except FileNotFoundError as e:
        return [TextContent(type="text", text=f"[Not Found] {e}")]
    except Exception as e:
        logger.exception(f"Error in tool '{name}'")
        return [TextContent(type="text", text=f"[Error] {type(e).__name__}: {e}")]


# ── File-format tool implementations ──────────────────────────────────────────

async def _list_builds(args: dict) -> list[TextContent]:
    if config.use_mongo():
        from . import db as mdb
        builds = await asyncio.to_thread(mdb.list_builds)
        if not builds:
            return [TextContent(type="text", text="No builds in database.")]
        lines = ["## Available Builds", "",
                 f"{'Tag':<40} {'Branch':<20} {'Line%':>6}  {'Func%':>6}  {'Created'}"]
        lines.append("-" * 100)
        for b in builds:
            lines.append(
                f"{b['build_id']:<40} {b.get('branch',''):<20} "
                f"{b.get('line_pct',0):>5.1f}%  {b.get('func_pct',0):>5.1f}%  "
                f"{b.get('created_at','')}"
            )
        return [TextContent(type="text", text="\n".join(lines))]
    tags = await asyncio.to_thread(list_available_tags)
    if not tags:
        return [TextContent(type="text", text="No builds found.")]
    return [TextContent(type="text", text="## Available Tags\n" + "\n".join(f"  {t}" for t in tags))]


async def _summarize_report(args: dict) -> list[TextContent]:
    report = await asyncio.to_thread(_load_report, args["tag"])
    t = report.totals
    text = (
        f"## Coverage Summary — tag: {report.tag}\n\n"
        f"| Metric | Covered | Total   | Missed  | %      |\n"
        f"|--------|---------|---------|---------|--------|\n"
        f"| Lines  | {t.covered:>7,} | {t.total:>7,} | {t.missed:>7,} | {t.percent:>5.2f}% |\n\n"
        f"Files in report: {len(report.files):,}\n"
        f"Note: line-level only. Use get_overall_coverage for function/region data."
    )
    return [TextContent(type="text", text=text)]


async def _get_folder_coverage(args: dict) -> list[TextContent]:
    report  = await asyncio.to_thread(_load_report, args["tag"])
    folder  = args["folder"].rstrip("/")
    sort_by = args.get("sort_by", "missed_lines")
    top_n   = int(args.get("top_n", 20))
    matched = [f for f in report.files if folder in f.filename]
    if not matched:
        return [TextContent(type="text", text=f"No files found matching folder: '{folder}'")]
    total   = sum(f.lines.total   for f in matched)
    covered = sum(f.lines.covered for f in matched)
    missed  = sum(f.lines.missed  for f in matched)
    pct     = round(covered / total * 100, 2) if total else 0.0
    key_fn  = {"missed_lines": lambda f: -f.lines.missed, "line_pct": lambda f: f.lines.percent, "filename": lambda f: f.filename}.get(sort_by, lambda f: -f.lines.missed)
    lines   = [
        f"## Folder Coverage — '{folder}' (tag: {args['tag']})", f"Files matched: {len(matched)}", "",
        f"| Metric | Covered | Total   | Missed  | %      |", f"|--------|---------|---------|---------|--------|",
        f"| Lines  | {covered:>7,} | {total:>7,} | {missed:>7,} | {pct:>5.2f}% |", "", f"### Top {top_n} Files (sorted by {sort_by})",
    ]
    for f in sorted(matched, key=key_fn)[:top_n]:
        lines.append(f"  {f.filename}\n    lines: {f.lines.covered}/{f.lines.total} ({f.lines.percent:.1f}%)  missed: {f.lines.missed}")
    return [TextContent(type="text", text="\n".join(lines))]


async def _get_file_coverage(args: dict) -> list[TextContent]:
    report  = await asyncio.to_thread(_load_report, args["tag"])
    matched = [f for f in report.files if args["file"] in f.filename]
    if not matched:
        return [TextContent(type="text", text=f"No files found matching: '{args['file']}'")]
    lines = [f"## File Coverage — '{args['file']}' (tag: {args['tag']})", f"Matches: {len(matched)}", ""]
    for f in matched:
        uncov = f.uncovered_lines[:30]
        more  = len(f.uncovered_lines) - len(uncov)
        lines += [
            f"### {f.filename}",
            f"| Metric | Covered | Total | Missed | %      |",
            f"|--------|---------|-------|--------|--------|",
            f"| Lines  | {f.lines.covered:>7,} | {f.lines.total:>5,} | {f.lines.missed:>6,} | {f.lines.percent:>5.2f}% |",
        ]
        if uncov:
            lines.append(f"  Uncovered lines: {uncov}{f'  (+{more} more)' if more else ''}")
        lines.append("")
    return [TextContent(type="text", text="\n".join(lines))]


async def _get_uncovered_lines(args: dict) -> list[TextContent]:
    report    = await asyncio.to_thread(_load_report, args["tag"])
    file_f    = args.get("file", "").strip()
    fold_f    = args.get("folder", "").rstrip("/").strip()
    limit     = int(args.get("limit", 50))
    matched   = [
        f for f in report.files
        if (not file_f or file_f in f.filename)
        and (not fold_f or f.filename.startswith(fold_f))
        and f.lines.missed > 0
    ]
    matched = sorted(matched, key=lambda f: -f.lines.missed)
    if not matched:
        return [TextContent(type="text", text="No uncovered lines found for the given filter.")]
    parts = []
    if file_f: parts.append(f"file: '{file_f}'")
    if fold_f: parts.append(f"folder: '{fold_f}'")
    lines = [f"## Uncovered Lines — tag: {args['tag']}", f"Scope: {'  |  '.join(parts) or 'entire build'}", f"Showing {min(limit, len(matched))} of {len(matched)} files", ""]
    for f in matched[:limit]:
        uncov  = f.uncovered_lines[:20]
        more   = len(f.uncovered_lines) - len(uncov)
        lines.append(f"### {f.filename}  ({f.lines.missed} missed)")
        lines.append(f"  Lines: {uncov}{f'  (+{more} more)' if more else ''}")
        lines.append("")
    return [TextContent(type="text", text="\n".join(lines))]


async def _list_files(args: dict) -> list[TextContent]:
    report  = await asyncio.to_thread(_load_report, args["tag"])
    prefix  = args.get("prefix", "")
    sort_by = args.get("sort_by", "missed_lines")
    limit   = int(args.get("limit", 50))
    files   = [f for f in report.files if not prefix or f.filename.startswith(prefix)]
    key_fn  = {"missed_lines": lambda f: -f.lines.missed, "line_pct": lambda f: f.lines.percent, "filename": lambda f: f.filename}.get(sort_by, lambda f: -f.lines.missed)
    files   = sorted(files, key=key_fn)
    lines   = [f"## File List — tag: {args['tag']}", f"Filter: '{prefix or '(none)'}' | {len(files)} files | sorted by {sort_by}", f"Showing top {min(limit, len(files))}", "", f"{'File':<80} {'Line%':>6}  {'Missed':>7}", "-" * 97]
    for f in files[:limit]:
        lines.append(f"{f.filename:<80} {f.lines.percent:>5.1f}%  {f.lines.missed:>7,}")
    return [TextContent(type="text", text="\n".join(lines))]


# ── LLVM tool implementations ──────────────────────────────────────────────────

async def _get_overall_coverage(args: dict) -> list[TextContent]:
    report = await asyncio.to_thread(_load_llvm_report, args["tag"])
    l, f, r = report.lines, report.functions, report.regions
    text = (
        f"## Overall Coverage — tag: {report.tag}\n\n"
        f"| Metric    | Covered | Total   | Missed  | %       |\n"
        f"|-----------|---------|---------|---------|----------|\n"
        f"| Lines     | {l.covered:>7,} | {l.count:>7,} | {l.count - l.covered:>7,} | {l.percent:>6.2f}% |\n"
        f"| Functions | {f.covered:>7,} | {f.count:>7,} | {f.count - f.covered:>7,} | {f.percent:>6.2f}% |\n"
        f"| Regions   | {r.covered:>7,} | {r.count:>7,} | {r.count - r.covered:>7,} | {r.percent:>6.2f}% |\n\n"
        f"Files in report: {len(report.files):,}\n"
        f"Functions tracked: {len(report._function_index):,}"
    )
    return [TextContent(type="text", text=text)]


async def _get_connector_coverage(args: dict) -> list[TextContent]:
    report    = await asyncio.to_thread(_load_llvm_report, args["tag"])
    connector = args["connector"].lower()
    show_files = bool(args.get("show_files", True))
    matched   = [f for f in report.files if connector in f.filename.lower()]
    if not matched:
        return [TextContent(type="text", text=f"No files found matching connector: '{connector}'")]
    l_count   = sum(f.lines.count       for f in matched)
    l_covered = sum(f.lines.covered     for f in matched)
    f_count   = sum(f.functions.count   for f in matched)
    f_covered = sum(f.functions.covered for f in matched)
    r_count   = sum(f.regions.count     for f in matched)
    r_covered = sum(f.regions.covered   for f in matched)
    l_pct = round(l_covered / l_count * 100, 2) if l_count else 0.0
    f_pct = round(f_covered / f_count * 100, 2) if f_count else 0.0
    r_pct = round(r_covered / r_count * 100, 2) if r_count else 0.0
    lines = [
        f"## Connector Coverage — '{connector}' (tag: {args['tag']})", f"Files matched: {len(matched)}", "",
        f"| Metric    | Covered | Total | Missed | %       |", f"|-----------|---------|-------|--------|---------|",
        f"| Lines     | {l_covered:>7,} | {l_count:>5,} | {l_count - l_covered:>6,} | {l_pct:>6.2f}% |",
        f"| Functions | {f_covered:>7,} | {f_count:>5,} | {f_count - f_covered:>6,} | {f_pct:>6.2f}% |",
        f"| Regions   | {r_covered:>7,} | {r_count:>5,} | {r_count - r_covered:>6,} | {r_pct:>6.2f}% |",
    ]
    if show_files:
        lines += ["", "### Per-file breakdown (sorted by line coverage %)"]
        for f in sorted(matched, key=lambda f: f.lines.percent):
            lines.append(f"  {f.filename}\n    lines: {f.lines.covered}/{f.lines.count} ({f.lines.percent:.1f}%)  functions: {f.functions.covered}/{f.functions.count} ({f.functions.percent:.1f}%)")
    return [TextContent(type="text", text="\n".join(lines))]


async def _is_function_tested(args: dict) -> list[TextContent]:
    report   = await asyncio.to_thread(_load_llvm_report, args["tag"])
    query    = args["function_name"].lower()
    limit    = int(args.get("limit", 20))
    matched  = [fn for fn in report._function_index if query in fn.name.lower()]
    if not matched:
        return [TextContent(type="text", text=f"No functions found matching: '{args['function_name']}'")]
    tested   = [fn for fn in matched if fn.count > 0]
    untested = [fn for fn in matched if fn.count == 0]
    lines = [f"## Function Coverage — '{args['function_name']}' (tag: {args['tag']})", f"Matched {len(matched)}: {len(tested)} tested ✓  {len(untested)} untested ✗", ""]
    if tested:
        lines.append(f"### ✓ Tested ({len(tested)})")
        for fn in tested[:limit]:
            lines.append(f"  [count={fn.count}] {fn.name}")
            lines.append(f"      {fn.filenames[0] if fn.filenames else 'unknown'}")
        if len(tested) > limit:
            lines.append(f"  ... and {len(tested) - limit} more")
        lines.append("")
    if untested:
        lines.append(f"### ✗ Untested ({len(untested)})")
        for fn in untested[:limit]:
            lines.append(f"  {fn.name}")
            lines.append(f"      {fn.filenames[0] if fn.filenames else 'unknown'}")
        if len(untested) > limit:
            lines.append(f"  ... and {len(untested) - limit} more")
    return [TextContent(type="text", text="\n".join(lines))]


# ── MongoDB tool implementations ───────────────────────────────────────────────

def _require_mongo() -> "mdb":
    if not config.use_mongo():
        raise RuntimeError("This tool requires MongoDB. Set MONGO_URI.")
    from . import db as mdb
    return mdb


async def _get_uncovered_functions(args: dict) -> list[TextContent]:
    mdb    = _require_mongo()
    tag    = args["tag"]
    file_f = args.get("file", "").strip()
    fold_f = args.get("folder", "").rstrip("/").strip()
    limit  = int(args.get("limit", 50))
    matches = await asyncio.to_thread(mdb.get_uncovered, tag, file_f, fold_f, limit)
    matches = [f for f in matches if f.get("func_missed", 0) > 0]
    if not matches:
        return [TextContent(type="text", text="No uncovered functions found for the given filter.")]
    parts = []
    if file_f: parts.append(f"file: '{file_f}'")
    if fold_f: parts.append(f"folder: '{fold_f}'")
    lines = [f"## Uncovered Functions — tag: {tag}", f"Scope: {' | '.join(parts) or 'entire build'}", f"Showing {len(matches)} files", ""]
    for f in matches:
        funcs = f.get("uncovered_funcs", [])[:15]
        more  = len(f.get("uncovered_funcs", [])) - len(funcs)
        lines.append(f"### {f['path']}  ({f['func_missed']} unexecuted functions)")
        for fn in funcs:
            lines.append(f"  line {fn.get('start','?'):>4}: {fn['name'].split('::')[-1][:80]}")
        if more:
            lines.append(f"  ... (+{more} more)")
        lines.append("")
    return [TextContent(type="text", text="\n".join(lines))]


async def _get_zero_coverage_files(args: dict) -> list[TextContent]:
    mdb    = _require_mongo()
    tag    = args["tag"]
    prefix = args.get("prefix", "")
    limit  = int(args.get("limit", 50))
    files  = await asyncio.to_thread(mdb.get_zero_coverage_files, tag, prefix, limit)
    if not files:
        return [TextContent(type="text", text=f"No zero-coverage files found{f' under {prefix}' if prefix else ''}.")]
    lines = [f"## Zero Coverage Files — tag: {tag}", f"Filter: '{prefix or '(none)'}' | {len(files)} files never executed", "", f"{'File':<75} {'Lines':>6}  {'Funcs':>6}", "-" * 92]
    for f in files:
        lines.append(f"{f['path']:<75} {f['line_total']:>6,}  {f['func_total']:>6,}")
    return [TextContent(type="text", text="\n".join(lines))]


async def _search_function(args: dict) -> list[TextContent]:
    mdb           = _require_mongo()
    tag           = args["tag"]
    function_name = args["function_name"]
    results = await asyncio.to_thread(mdb.search_function, tag, function_name)
    if not results:
        return [TextContent(type="text", text=f"No uncovered functions matching '{function_name}' found in build '{tag}'.\nThe function may be covered or may not exist.")]
    lines = [f"## Function Search — '{function_name}' (tag: {tag})", f"Found {len(results)} uncovered match(es)", ""]
    for r in results:
        lines.append(f"  ❌ MISSED  line {r.get('start_line','?'):>4}  {r['path']}")
        lines.append(f"             {r['func_name']}")
        lines.append("")
    return [TextContent(type="text", text="\n".join(lines))]


async def _compare_builds(args: dict) -> list[TextContent]:
    mdb           = _require_mongo()
    base_tag      = args["base_tag"]
    head_tag      = args["head_tag"]
    changed_files = args.get("changed_files", None)
    result = await asyncio.to_thread(mdb.compare_builds, base_tag, head_tag, changed_files)
    base = result["base"]
    head = result["head"]
    line_delta = round(head["line_pct"] - base["line_pct"], 2)
    func_delta = round(head["func_pct"] - base["func_pct"], 2)
    lines = [
        f"## Build Comparison — `{base_tag}` → `{head_tag}`", "",
        f"| Metric    | Base   | Head   | Delta  |", f"|-----------|--------|--------|--------|",
        f"| Lines     | {base['line_pct']:>5.1f}% | {head['line_pct']:>5.1f}% | {line_delta:>+5.1f}% |",
        f"| Functions | {base['func_pct']:>5.1f}% | {head['func_pct']:>5.1f}% | {func_delta:>+5.1f}% |", "",
    ]
    if result["regressions"]:
        lines.append(f"### ⚠️  Regressions ({len(result['regressions'])} files)")
        for r in result["regressions"][:20]:
            lines.append(f"  {r['path']}\n    line: {r['line_pct_before']:.1f}% → {r['line_pct_after']:.1f}% ({r['line_delta']:+.1f}%)  func: {r['func_pct_before']:.1f}% → {r['func_pct_after']:.1f}% ({r['func_delta']:+.1f}%)")
        lines.append("")
    if result["improvements"]:
        lines.append(f"### ✅ Improvements ({len(result['improvements'])} files)")
        for r in result["improvements"][:10]:
            lines.append(f"  {r['path']}\n    line: {r['line_pct_before']:.1f}% → {r['line_pct_after']:.1f}% ({r['line_delta']:+.1f}%)")
        lines.append("")
    if result["new_files"]:
        lines.append(f"### 🆕 New files ({len(result['new_files'])})")
        for f in result["new_files"][:10]:
            lines.append(f"  {f['path']}  (line: {f['line_pct']:.1f}%  func: {f.get('func_pct',0):.1f}%)")
        lines.append("")
    if not any([result["regressions"], result["improvements"], result["new_files"]]):
        lines.append("No significant coverage changes detected.")
    return [TextContent(type="text", text="\n".join(lines))]


async def _get_test_priority(args: dict) -> list[TextContent]:
    mdb    = _require_mongo()
    tag    = args["tag"]
    prefix = args.get("prefix", "")
    limit  = int(args.get("limit", 30))
    files  = await asyncio.to_thread(mdb.get_test_priority, tag, prefix, limit)
    if not files:
        return [TextContent(type="text", text="No files with missed coverage found.")]
    lines = [
        f"## Test Priority — tag: {tag}",
        f"Filter: '{prefix or '(none)'}' | Impact = (func_missed × 3) + line_missed", "",
        f"{'#':<4} {'File':<70} {'Score':>6}  {'Line%':>6}  {'L.Miss':>7}  {'Func%':>6}  {'F.Miss':>7}", "-" * 115,
    ]
    for i, f in enumerate(files, 1):
        lines.append(f"{i:<4} {f['path']:<70} {f['impact_score']:>6,}  {f['line_pct']:>5.1f}%  {f['line_missed']:>7,}  {f['func_pct']:>5.1f}%  {f['func_missed']:>7,}")
    return [TextContent(type="text", text="\n".join(lines))]


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    try:
        config.validate()
    except RuntimeError as e:
        print(f"[coverage-mcp-server] {e}", file=sys.stderr)
        sys.exit(1)

    logger.info(f"Starting | transport={config.MCP_TRANSPORT} | mongo={'yes' if config.use_mongo() else 'no'}")

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
            headers  = dict(scope.get("headers", []))
            auth     = headers.get(b"authorization", b"").decode()
            if not auth.startswith("Bearer "):
                return False
            provided = auth.removeprefix("Bearer ").strip()
            return hmac.compare_digest(
                hashlib.sha256(provided.encode()).hexdigest(),
                stored_hash,
            )

        async def _send_401(send):
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body",
                        "body": b'{"error":"Unauthorized"}', "more_body": False})

        async def _send_404(send):
            await send({"type": "http.response.start", "status": 404,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body",
                        "body": b'{"error":"not_found"}', "more_body": False})

        _OAUTH_DISCOVERY_PATHS = {
            "/.well-known/oauth-authorization-server",
            "/.well-known/openid-configuration",
            "/.well-known/oauth-protected-resource",
        }

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

            if path in _OAUTH_DISCOVERY_PATHS or path == "/register":
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
