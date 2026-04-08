"""
LLVM Coverage MCP Server

Tools (11):
  list_builds             — available build tags
  summarize_report        — overall line + function coverage for a build
  get_folder_coverage     — aggregate coverage for all files under a folder
  get_file_coverage       — per-file line + function coverage + uncovered detail
  get_uncovered_lines     — uncovered lines grouped by file
  get_uncovered_functions — uncovered functions grouped by file
  list_files              — files sorted by missed lines/functions
  get_zero_coverage_files — files never touched by any test
  search_function         — find a specific function and check if it's covered
  compare_builds          — coverage delta between two builds
  get_test_priority       — files ranked by impact score for test writing

Prompts (4):
  pr_coverage_review         — full PR coverage report (orchestrates git + MCP)
  write_test_plan            — structured test plan for a folder/file
  coverage_regression_report — what regressed between two builds
  onboarding_coverage_tour   — worst areas tour for new QA engineer

Data source:
  MONGO_URI set → queries MongoDB (populated by sync service from S3)
  Otherwise     → falls back to local file (COVERAGE_FILE_PATH / COVERAGE_DIR)
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

server = Server("coverage-mcp-server")


# ── Tool definitions ───────────────────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="list_builds",
            description="List available build tags with overall line and function coverage percentages.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="summarize_report",
            description="Overall line and function coverage summary for a build tag.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tag": {"type": "string", "description": "Build tag. Use list_builds to find tags. Use 'latest' for the most recent build."},
                },
                "required": ["tag"],
            },
        ),
        Tool(
            name="get_folder_coverage",
            description="Aggregate line and function coverage for all files under a folder path prefix.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tag":     {"type": "string"},
                    "folder":  {"type": "string", "description": "Folder path prefix (e.g. 'crates/router/src')."},
                    "sort_by": {"type": "string", "enum": ["missed_lines", "missed_funcs", "line_pct", "func_pct", "filename"], "default": "missed_lines"},
                    "top_n":   {"type": "integer", "default": 20},
                },
                "required": ["tag", "folder"],
            },
        ),
        Tool(
            name="get_file_coverage",
            description="Line and function coverage for a specific file. Matches on substring of the full path. Returns uncovered lines and functions.",
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
            description="List uncovered lines (execution count == 0) grouped by file. Filter by file or folder.",
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
            name="get_uncovered_functions",
            description="List unexecuted functions grouped by file with their start line numbers. Filter by file or folder.",
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
            description="List files in the report with line and function coverage stats.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tag":     {"type": "string"},
                    "prefix":  {"type": "string", "description": "Optional path prefix filter."},
                    "sort_by": {"type": "string", "enum": ["missed_lines", "missed_funcs", "line_pct", "func_pct", "filename"], "default": "missed_lines"},
                    "limit":   {"type": "integer", "default": 50},
                },
                "required": ["tag"],
            },
        ),
        Tool(
            name="get_zero_coverage_files",
            description="Files that have zero coverage — never executed by any test. These are the easiest wins to improve coverage.",
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
            description="Search for a specific function by name and check if it is covered. Useful for PR reviews to verify a specific function is tested.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tag":           {"type": "string"},
                    "function_name": {"type": "string", "description": "Function name or substring to search for (case-insensitive)."},
                },
                "required": ["tag", "function_name"],
            },
        ),
        Tool(
            name="compare_builds",
            description="Compare coverage between two builds. Shows regressions, improvements, and new/removed files. Use for PR reviews by passing the PR branch build and main branch build.",
            inputSchema={
                "type": "object",
                "properties": {
                    "base_tag":      {"type": "string", "description": "Base build tag (e.g. main branch build)."},
                    "head_tag":      {"type": "string", "description": "Head build tag (e.g. PR branch build)."},
                    "changed_files": {"type": "array", "items": {"type": "string"}, "description": "Optional list of file paths to restrict comparison to (e.g. files changed in a PR)."},
                },
                "required": ["base_tag", "head_tag"],
            },
        ),
        Tool(
            name="get_test_priority",
            description="Rank files by impact score (func_missed × 3 + line_missed). Higher score = more value to write tests for. Use to plan which files to tackle first.",
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
            description="Full PR coverage review. Checks changed files, finds uncovered functions, detects regressions vs base branch, and formats a PR comment.",
            arguments=[
                PromptArgument(name="head_tag",  description="Build tag for the PR branch.",   required=True),
                PromptArgument(name="base_tag",  description="Build tag for the base branch (e.g. main).", required=True),
                PromptArgument(name="pr_number", description="PR number for context.",         required=False),
            ],
        ),
        Prompt(
            name="write_test_plan",
            description="Generate a structured test plan for a folder or file, listing uncovered functions, suggested test types, and priority order.",
            arguments=[
                PromptArgument(name="tag",    description="Build tag to analyse.",               required=True),
                PromptArgument(name="target", description="File path or folder prefix to plan for.", required=True),
            ],
        ),
        Prompt(
            name="coverage_regression_report",
            description="Detailed report of what coverage regressed between two builds — which files, which functions, and by how much.",
            arguments=[
                PromptArgument(name="base_tag", description="Build tag before the change (e.g. last passing build).", required=True),
                PromptArgument(name="head_tag", description="Build tag after the change.",       required=True),
            ],
        ),
        Prompt(
            name="onboarding_coverage_tour",
            description="A tour of the worst-covered areas of the codebase for a new QA engineer. Shows where to focus, what's never been tested, and where quick wins are.",
            arguments=[
                PromptArgument(name="tag", description="Build tag to use (use 'latest' for current state).", required=True),
            ],
        ),
    ]


@server.get_prompt()
async def get_prompt(name: str, arguments: dict) -> GetPromptResult:
    match name:
        case "pr_coverage_review":       return _prompt_pr_review(arguments)
        case "write_test_plan":          return _prompt_test_plan(arguments)
        case "coverage_regression_report": return _prompt_regression(arguments)
        case "onboarding_coverage_tour": return _prompt_onboarding(arguments)
        case _:
            raise ValueError(f"Unknown prompt: {name}")


def _prompt_pr_review(args: dict) -> GetPromptResult:
    head_tag   = args.get("head_tag", "")
    base_tag   = args.get("base_tag", "")
    pr_number  = args.get("pr_number", "")
    pr_context = f"PR #{pr_number}" if pr_number else "this PR"

    text = f"""You are a QA engineer reviewing coverage for {pr_context}.

Head build: `{head_tag}` (PR branch)
Base build: `{base_tag}` (base branch)

Follow these steps in order:

## Step 1 — Get changed files
Run `git diff {base_tag}...HEAD --name-only` (or use your git tools) to get the list of files changed in this PR.

## Step 2 — Compare coverage
Call `compare_builds(base_tag="{base_tag}", head_tag="{head_tag}", changed_files=[...list from step 1...])`.
Note any regressions (files where coverage dropped).

## Step 3 — Check each changed file
For each changed file, call `get_file_coverage(tag="{head_tag}", file="<path>")`.
Note the line% and func% for each.

## Step 4 — Find uncovered functions in changed files
For files with func_missed > 0, call `get_uncovered_functions(tag="{head_tag}", file="<path>")`.
These are the functions added or changed in this PR that have no test coverage.

## Step 5 — Check for zero-coverage files
Call `get_zero_coverage_files(tag="{head_tag}", prefix="<common prefix of changed files>")`.
Any changed file at 0% should be flagged as critical.

## Step 6 — Format the PR comment
Write a markdown PR comment with these sections:

### Coverage Report — {pr_context}

**Summary**
- Overall: line% and func% for head vs base (delta)
- Files changed: N | Files with coverage drop: N | Files at 0%: N

**Regressions** (if any)
| File | Line% Before | Line% After | Func% Before | Func% After |
...

**Uncovered functions in changed files**
| File | Function | Line |
...

**Zero-coverage files** (if any)
...

**No action needed** (files that are well covered)
...

Be specific. Include file paths and function names. A developer should be able to act on this comment directly."""

    return GetPromptResult(
        description=f"PR coverage review: {head_tag} vs {base_tag}",
        messages=[PromptMessage(role="user", content=TextContent(type="text", text=text))],
    )


def _prompt_test_plan(args: dict) -> GetPromptResult:
    tag    = args.get("tag", "latest")
    target = args.get("target", "")

    text = f"""You are a QA engineer creating a test plan for `{target}` (build: `{tag}`).

Follow these steps:

## Step 1 — Coverage overview
Call `get_folder_coverage(tag="{tag}", folder="{target}", sort_by="missed_funcs", top_n=20)`.
Get a picture of which files need the most work.

## Step 2 — Priority ranking
Call `get_test_priority(tag="{tag}", prefix="{target}", limit=20)`.
This gives impact scores — use this to order the plan.

## Step 3 — Uncovered functions
For the top 10 files by impact score, call `get_uncovered_functions(tag="{tag}", file="<path>")`.
Collect all unexecuted function names and their start lines.

## Step 4 — Zero coverage files
Call `get_zero_coverage_files(tag="{tag}", prefix="{target}")`.
These need at minimum a smoke test.

## Step 5 — Write the test plan

Format the output as:

### Test Plan — `{target}` (build: `{tag}`)

**Coverage baseline**
- Line: X% | Functions: X%
- Files with 0% coverage: N

**Priority 1 — Critical (0% coverage)**
For each zero-coverage file:
- File: `path`
- Suggested test type: unit / integration / e2e
- Functions to cover: list
- Notes: any context about what this module does

**Priority 2 — High impact (func_missed > 5)**
Same format.

**Priority 3 — Quick wins (func_missed 1-5)**
Same format.

**Estimated effort**
Rough estimate of test files needed and complexity.

Be actionable. A QA engineer should be able to pick up this plan and start writing tests immediately."""

    return GetPromptResult(
        description=f"Test plan for {target} (build: {tag})",
        messages=[PromptMessage(role="user", content=TextContent(type="text", text=text))],
    )


def _prompt_regression(args: dict) -> GetPromptResult:
    base_tag = args.get("base_tag", "")
    head_tag = args.get("head_tag", "")

    text = f"""You are a QA engineer investigating a coverage regression.

Base build: `{base_tag}`
Head build: `{head_tag}`

Follow these steps:

## Step 1 — Overall delta
Call `summarize_report(tag="{base_tag}")` and `summarize_report(tag="{head_tag}")`.
Calculate the overall line% and func% delta.

## Step 2 — File-level comparison
Call `compare_builds(base_tag="{base_tag}", head_tag="{head_tag}")`.
Get the full list of regressions and improvements.

## Step 3 — Drill into regressions
For the top 5 regressed files, call `get_uncovered_functions(tag="{head_tag}", file="<path>")`.
Find which specific functions lost coverage.

## Step 4 — Write the regression report

### Coverage Regression Report
**Builds:** `{base_tag}` → `{head_tag}`

**Overall**
| Metric | Before | After | Delta |
| Lines  | X%     | X%    | -X%   |
| Funcs  | X%     | X%    | -X%   |

**Regressed files** (sorted by worst delta)
For each file:
- Path, line delta, func delta
- Functions that lost coverage (were they deleted, refactored, or just untested?)

**Improved files** (acknowledge the wins)

**Root cause hypothesis**
Based on the patterns, what likely caused the regression?
(e.g. new code added without tests, refactor removed test hooks, test deleted)

**Recommended actions**
Specific steps to restore coverage."""

    return GetPromptResult(
        description=f"Regression report: {base_tag} → {head_tag}",
        messages=[PromptMessage(role="user", content=TextContent(type="text", text=text))],
    )


def _prompt_onboarding(args: dict) -> GetPromptResult:
    tag = args.get("tag", "latest")

    text = f"""You are giving a new QA engineer a tour of the codebase coverage (build: `{tag}`).

Your goal: help them understand where the testing gaps are and where to focus first.

Follow these steps:

## Step 1 — Big picture
Call `summarize_report(tag="{tag}")`.
Describe what the overall coverage means in plain terms.

## Step 2 — Top-level breakdown
Call `get_folder_coverage(tag="{tag}", folder="crates", sort_by="missed_funcs", top_n=15)`.
(Adjust the folder to match the project structure if needed.)
Identify the 3-5 modules with the biggest gaps.

## Step 3 — Never-tested files
Call `get_zero_coverage_files(tag="{tag}", limit=30)`.
These are files no test has ever touched.

## Step 4 — Highest impact targets
Call `get_test_priority(tag="{tag}", limit=20)`.
These are the files where writing tests will improve coverage the most.

## Step 5 — A sample deep dive
Pick the single most impactful file from Step 4.
Call `get_uncovered_functions(tag="{tag}", file="<path>")` on it.
Show the new engineer what "working on a specific file" looks like.

## Step 6 — Write the tour

### Welcome to Coverage — Build `{tag}`

**The big picture**
Plain English summary of current state. Don't just repeat numbers — interpret them.

**Where the gaps are** (top 5 modules)
For each: what the module does, coverage%, why it matters to test.

**Files never touched by tests** (top 10)
Quick list with file size (line_total) as a proxy for complexity.

**Where to start — top 5 high-impact files**
For each: path, impact score, uncovered functions, suggested first test to write.

**How to use the coverage tools**
Brief guide: "To check a specific file, ask: get_file_coverage for <path>"
"To find what to work on next, ask: get_test_priority for <module>"

Make it welcoming and practical. The new engineer should finish reading this and know exactly what to work on first."""

    return GetPromptResult(
        description=f"Onboarding coverage tour (build: {tag})",
        messages=[PromptMessage(role="user", content=TextContent(type="text", text=text))],
    )


# ── Tool handlers ──────────────────────────────────────────────────────────────

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        match name:
            case "list_builds":            return await _list_builds(arguments)
            case "summarize_report":        return await _summarize_report(arguments)
            case "get_folder_coverage":     return await _get_folder_coverage(arguments)
            case "get_file_coverage":       return await _get_file_coverage(arguments)
            case "get_uncovered_lines":     return await _get_uncovered_lines(arguments)
            case "get_uncovered_functions": return await _get_uncovered_functions(arguments)
            case "list_files":              return await _list_files(arguments)
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


# ── Tool implementations ───────────────────────────────────────────────────────

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
    else:
        from .fetcher import list_available_tags
        tags = await asyncio.to_thread(list_available_tags)
        return [TextContent(type="text", text="## Available Tags\n" + "\n".join(f"  {t}" for t in tags))]


async def _summarize_report(args: dict) -> list[TextContent]:
    tag = args["tag"]
    if config.use_mongo():
        from . import db as mdb
        build = await asyncio.to_thread(mdb.get_build, tag)
        if not build:
            return [TextContent(type="text", text=f"Build '{tag}' not found.")]
        lc = build.get("line_covered", 0)
        lm = build.get("line_missed", 0)
        lt = build.get("line_total", 0)
        lp = build.get("line_pct", 0.0)
        fc = build.get("func_covered", 0)
        fm = build.get("func_missed", 0)
        ft = build.get("func_total", 0)
        fp = build.get("func_pct", 0.0)
        text = (
            f"## Coverage Summary — {build['build_id']}\n"
            f"Branch: {build.get('branch','')}  Commit: {build.get('commit','')}\n\n"
            f"| Metric    | Covered | Total   | Missed  | %      |\n"
            f"|-----------|---------|---------|---------|--------|\n"
            f"| Lines     | {lc:>7,} | {lt:>7,} | {lm:>7,} | {lp:>5.2f}% |\n"
            f"| Functions | {fc:>7,} | {ft:>7,} | {fm:>7,} | {fp:>5.2f}% |"
        )
        return [TextContent(type="text", text=text)]
    else:
        return await _summarize_report_file(args)


async def _get_folder_coverage(args: dict) -> list[TextContent]:
    tag     = args["tag"]
    folder  = args["folder"].rstrip("/")
    sort_by = args.get("sort_by", "missed_lines")
    top_n   = int(args.get("top_n", 20))
    if config.use_mongo():
        from . import db as mdb
        totals = await asyncio.to_thread(mdb.folder_totals, tag, folder)
        files  = await asyncio.to_thread(mdb.get_folder, tag, folder, sort_by, top_n)
        if not totals:
            return [TextContent(type="text", text=f"No files found matching folder: '{folder}'")]
        lines = [
            f"## Folder Coverage — '{folder}' (tag: {tag})",
            f"Files matched: {totals['files']}",
            "",
            f"| Metric    | Covered | Total   | Missed  | %      |",
            f"|-----------|---------|---------|---------|--------|",
            f"| Lines     | {totals['line_covered']:>7,} | {totals['line_total']:>7,} | {totals['line_missed']:>7,} | {totals['line_pct']:>5.2f}% |",
            f"| Functions | {totals['func_covered']:>7,} | {totals['func_total']:>7,} | {totals['func_missed']:>7,} | {totals['func_pct']:>5.2f}% |",
            "",
            f"### Top {top_n} Files (sorted by {sort_by})",
        ]
        for f in files:
            lines.append(
                f"  {f['path']}\n"
                f"    lines: {f['line_covered']}/{f['line_total']} ({f['line_pct']:.1f}%)  missed: {f['line_missed']}"
                f"  |  funcs: {f['func_covered']}/{f['func_total']} ({f['func_pct']:.1f}%)  missed: {f['func_missed']}"
            )
        return [TextContent(type="text", text="\n".join(lines))]
    else:
        return await _get_folder_coverage_file(args)


async def _get_file_coverage(args: dict) -> list[TextContent]:
    tag  = args["tag"]
    file = args["file"]
    if config.use_mongo():
        from . import db as mdb
        matches = await asyncio.to_thread(mdb.get_file, tag, file)
        if not matches:
            return [TextContent(type="text", text=f"No files found matching: '{file}'")]
        lines = [f"## File Coverage — '{file}' (tag: {tag})", f"Matches: {len(matches)}", ""]
        for f in matches:
            uncov_lines = f.get("uncovered_lines", [])[:30]
            uncov_funcs = f.get("uncovered_funcs", [])[:10]
            more_lines  = len(f.get("uncovered_lines", [])) - len(uncov_lines)
            more_funcs  = len(f.get("uncovered_funcs", [])) - len(uncov_funcs)
            lines += [
                f"### {f['path']}",
                f"| Metric    | Covered | Total | Missed | %      |",
                f"|-----------|---------|-------|--------|--------|",
                f"| Lines     | {f['line_covered']:>7,} | {f['line_total']:>5,} | {f['line_missed']:>6,} | {f['line_pct']:>5.2f}% |",
                f"| Functions | {f['func_covered']:>7,} | {f['func_total']:>5,} | {f['func_missed']:>6,} | {f['func_pct']:>5.2f}% |",
            ]
            if uncov_lines:
                lines.append(f"  Uncovered lines: {uncov_lines}{f'  (+{more_lines} more)' if more_lines else ''}")
            if uncov_funcs:
                names = [fn["name"].split("::")[-1] for fn in uncov_funcs]
                lines.append(f"  Uncovered functions: {names}{f'  (+{more_funcs} more)' if more_funcs else ''}")
            lines.append("")
        return [TextContent(type="text", text="\n".join(lines))]
    else:
        return await _get_file_coverage_file(args)


async def _get_uncovered_lines(args: dict) -> list[TextContent]:
    tag    = args["tag"]
    file_f = args.get("file", "").strip()
    fold_f = args.get("folder", "").rstrip("/").strip()
    limit  = int(args.get("limit", 50))
    if config.use_mongo():
        from . import db as mdb
        matches = await asyncio.to_thread(mdb.get_uncovered, tag, file_f, fold_f, limit)
        if not matches:
            return [TextContent(type="text", text="No uncovered lines found for the given filter.")]
        parts = []
        if file_f: parts.append(f"file: '{file_f}'")
        if fold_f: parts.append(f"folder: '{fold_f}'")
        scope = " | ".join(parts) or "entire build"
        lines = [f"## Uncovered Lines — tag: {tag}", f"Scope: {scope}", f"Showing {len(matches)} files", ""]
        for f in matches:
            uncov  = f.get("uncovered_lines", [])[:20]
            more   = len(f.get("uncovered_lines", [])) - len(uncov)
            lines.append(f"### {f['path']}  ({f['line_missed']} missed)")
            lines.append(f"  Lines: {uncov}{f'  (+{more} more)' if more else ''}")
            lines.append("")
        return [TextContent(type="text", text="\n".join(lines))]
    else:
        return await _get_uncovered_lines_file(args)


async def _get_uncovered_functions(args: dict) -> list[TextContent]:
    tag    = args["tag"]
    file_f = args.get("file", "").strip()
    fold_f = args.get("folder", "").rstrip("/").strip()
    limit  = int(args.get("limit", 50))
    if config.use_mongo():
        from . import db as mdb
        matches = await asyncio.to_thread(mdb.get_uncovered, tag, file_f, fold_f, limit)
        matches = [f for f in matches if f.get("func_missed", 0) > 0]
        if not matches:
            return [TextContent(type="text", text="No uncovered functions found for the given filter.")]
        parts = []
        if file_f: parts.append(f"file: '{file_f}'")
        if fold_f: parts.append(f"folder: '{fold_f}'")
        scope = " | ".join(parts) or "entire build"
        lines = [f"## Uncovered Functions — tag: {tag}", f"Scope: {scope}", f"Showing {len(matches)} files", ""]
        for f in matches:
            funcs = f.get("uncovered_funcs", [])[:15]
            more  = len(f.get("uncovered_funcs", [])) - len(funcs)
            lines.append(f"### {f['path']}  ({f['func_missed']} unexecuted functions)")
            for fn in funcs:
                short_name = fn["name"].split("::")[-1][:80]
                lines.append(f"  line {fn.get('start','?'):>4}: {short_name}")
            if more:
                lines.append(f"  ... (+{more} more)")
            lines.append("")
        return [TextContent(type="text", text="\n".join(lines))]
    else:
        return [TextContent(type="text", text="Function data requires MongoDB mode. Set MONGO_URI.")]


async def _list_files(args: dict) -> list[TextContent]:
    tag     = args["tag"]
    prefix  = args.get("prefix", "")
    sort_by = args.get("sort_by", "missed_lines")
    limit   = int(args.get("limit", 50))
    if config.use_mongo():
        from . import db as mdb
        files = await asyncio.to_thread(mdb.list_files, tag, prefix, sort_by, limit)
        lines = [
            f"## File List — tag: {tag}",
            f"Filter: '{prefix or '(none)'}' | sorted by {sort_by} | showing {len(files)}",
            "",
            f"{'File':<75} {'Line%':>6}  {'L.Miss':>7}  {'Func%':>6}  {'F.Miss':>7}",
            "-" * 110,
        ]
        for f in files:
            lines.append(
                f"{f['path']:<75} {f['line_pct']:>5.1f}%  {f['line_missed']:>7,}"
                f"  {f.get('func_pct', 0):>5.1f}%  {f.get('func_missed', 0):>7,}"
            )
        return [TextContent(type="text", text="\n".join(lines))]
    else:
        return await _list_files_file(args)


async def _get_zero_coverage_files(args: dict) -> list[TextContent]:
    tag    = args["tag"]
    prefix = args.get("prefix", "")
    limit  = int(args.get("limit", 50))
    if not config.use_mongo():
        return [TextContent(type="text", text="Requires MongoDB mode. Set MONGO_URI.")]
    from . import db as mdb
    files = await asyncio.to_thread(mdb.get_zero_coverage_files, tag, prefix, limit)
    if not files:
        return [TextContent(type="text", text=f"No zero-coverage files found{f' under {prefix}' if prefix else ''}.")]
    lines = [
        f"## Zero Coverage Files — tag: {tag}",
        f"Filter: '{prefix or '(none)'}' | {len(files)} files never executed",
        "",
        f"{'File':<75} {'Lines':>6}  {'Funcs':>6}",
        "-" * 92,
    ]
    for f in files:
        lines.append(f"{f['path']:<75} {f['line_total']:>6,}  {f['func_total']:>6,}")
    return [TextContent(type="text", text="\n".join(lines))]


async def _search_function(args: dict) -> list[TextContent]:
    tag           = args["tag"]
    function_name = args["function_name"]
    if not config.use_mongo():
        return [TextContent(type="text", text="Requires MongoDB mode. Set MONGO_URI.")]
    from . import db as mdb
    results = await asyncio.to_thread(mdb.search_function, tag, function_name)
    if not results:
        return [TextContent(type="text", text=f"No uncovered functions matching '{function_name}' found in build '{tag}'.\nThe function may be covered (executed) or may not exist.")]
    lines = [
        f"## Function Search — '{function_name}' (tag: {tag})",
        f"Found {len(results)} uncovered match(es)",
        "",
    ]
    for r in results:
        lines.append(f"  {'❌ MISSED':<12} line {r.get('start_line','?'):>4}  {r['path']}")
        lines.append(f"             {r['func_name']}")
        lines.append("")
    return [TextContent(type="text", text="\n".join(lines))]


async def _compare_builds(args: dict) -> list[TextContent]:
    base_tag = args["base_tag"]
    head_tag = args["head_tag"]
    changed_files = args.get("changed_files", None)
    if not config.use_mongo():
        return [TextContent(type="text", text="Requires MongoDB mode. Set MONGO_URI.")]
    from . import db as mdb
    result = await asyncio.to_thread(mdb.compare_builds, base_tag, head_tag, changed_files)

    base = result["base"]
    head = result["head"]
    line_delta = round(head["line_pct"] - base["line_pct"], 2)
    func_delta = round(head["func_pct"] - base["func_pct"], 2)
    line_sign  = "+" if line_delta >= 0 else ""
    func_sign  = "+" if func_delta >= 0 else ""

    lines = [
        f"## Build Comparison — `{base_tag}` → `{head_tag}`",
        "",
        f"| Metric    | Base   | Head   | Delta  |",
        f"|-----------|--------|--------|--------|",
        f"| Lines     | {base['line_pct']:>5.1f}% | {head['line_pct']:>5.1f}% | {line_sign}{line_delta:>4.1f}% |",
        f"| Functions | {base['func_pct']:>5.1f}% | {head['func_pct']:>5.1f}% | {func_sign}{func_delta:>4.1f}% |",
        "",
    ]

    if result["regressions"]:
        lines.append(f"### ⚠️  Regressions ({len(result['regressions'])} files)")
        for r in result["regressions"][:20]:
            lines.append(
                f"  {r['path']}\n"
                f"    line: {r['line_pct_before']:.1f}% → {r['line_pct_after']:.1f}% ({r['line_delta']:+.1f}%)  "
                f"func: {r['func_pct_before']:.1f}% → {r['func_pct_after']:.1f}% ({r['func_delta']:+.1f}%)"
            )
        lines.append("")

    if result["improvements"]:
        lines.append(f"### ✅ Improvements ({len(result['improvements'])} files)")
        for r in result["improvements"][:10]:
            lines.append(
                f"  {r['path']}\n"
                f"    line: {r['line_pct_before']:.1f}% → {r['line_pct_after']:.1f}% ({r['line_delta']:+.1f}%)  "
                f"func: {r['func_pct_before']:.1f}% → {r['func_pct_after']:.1f}% ({r['func_delta']:+.1f}%)"
            )
        lines.append("")

    if result["new_files"]:
        lines.append(f"### 🆕 New files ({len(result['new_files'])})")
        for f in result["new_files"][:10]:
            lines.append(f"  {f['path']}  (line: {f['line_pct']:.1f}%  func: {f.get('func_pct',0):.1f}%)")
        lines.append("")

    if result["removed_files"]:
        lines.append(f"### 🗑️  Removed files ({len(result['removed_files'])})")
        for f in result["removed_files"][:10]:
            lines.append(f"  {f['path']}")

    if not any([result["regressions"], result["improvements"], result["new_files"], result["removed_files"]]):
        lines.append("No significant coverage changes detected.")

    return [TextContent(type="text", text="\n".join(lines))]


async def _get_test_priority(args: dict) -> list[TextContent]:
    tag    = args["tag"]
    prefix = args.get("prefix", "")
    limit  = int(args.get("limit", 30))
    if not config.use_mongo():
        return [TextContent(type="text", text="Requires MongoDB mode. Set MONGO_URI.")]
    from . import db as mdb
    files = await asyncio.to_thread(mdb.get_test_priority, tag, prefix, limit)
    if not files:
        return [TextContent(type="text", text="No files with missed coverage found.")]
    lines = [
        f"## Test Priority — tag: {tag}",
        f"Filter: '{prefix or '(none)'}' | Impact = (func_missed × 3) + line_missed",
        "",
        f"{'#':<4} {'File':<70} {'Score':>6}  {'Line%':>6}  {'L.Miss':>7}  {'Func%':>6}  {'F.Miss':>7}",
        "-" * 115,
    ]
    for i, f in enumerate(files, 1):
        lines.append(
            f"{i:<4} {f['path']:<70} {f['impact_score']:>6,}"
            f"  {f['line_pct']:>5.1f}%  {f['line_missed']:>7,}"
            f"  {f['func_pct']:>5.1f}%  {f['func_missed']:>7,}"
        )
    return [TextContent(type="text", text="\n".join(lines))]


# ── File-based fallback implementations ───────────────────────────────────────

_report_cache: dict = {}


def _get_report(tag: str):
    if tag not in _report_cache:
        from .fetcher import fetch_coverage_json
        from .parser import parse_json
        _report_cache[tag] = parse_json(fetch_coverage_json(tag), tag)
    return _report_cache[tag]


async def _summarize_report_file(args: dict) -> list[TextContent]:
    report = await asyncio.to_thread(_get_report, args["tag"])
    t = report.totals
    text = (
        f"## Coverage Summary — tag: {report.tag}\n\n"
        f"| Metric | Covered | Total   | Missed  | %      |\n"
        f"|--------|---------|---------|---------|--------|\n"
        f"| Lines  | {t.covered:>7,} | {t.total:>7,} | {t.missed:>7,} | {t.percent:>5.2f}% |\n\n"
        f"Files in report: {len(report.files):,}\n"
        f"Note: function data not available in file mode."
    )
    return [TextContent(type="text", text=text)]


async def _get_folder_coverage_file(args: dict) -> list[TextContent]:
    report  = await asyncio.to_thread(_get_report, args["tag"])
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
        f"| Lines | {covered}/{total} ({pct:.2f}%) | missed: {missed} |", "", f"### Top {top_n} Files",
    ]
    for f in sorted(matched, key=key_fn)[:top_n]:
        lines.append(f"  {f.filename}\n    lines: {f.lines.covered}/{f.lines.total} ({f.lines.percent:.1f}%)  missed: {f.lines.missed}")
    return [TextContent(type="text", text="\n".join(lines))]


async def _get_file_coverage_file(args: dict) -> list[TextContent]:
    report  = await asyncio.to_thread(_get_report, args["tag"])
    matched = [f for f in report.files if args["file"] in f.filename]
    if not matched:
        return [TextContent(type="text", text=f"No files found matching: '{args['file']}'")]
    lines = [f"## File Coverage — '{args['file']}' (tag: {args['tag']})", f"Matches: {len(matched)}", ""]
    for f in matched:
        uncov = f.uncovered_lines[:30]
        more  = len(f.uncovered_lines) - len(uncov)
        lines += [f"### {f.filename}", f"| Lines | {f.lines.covered}/{f.lines.total} ({f.lines.percent:.2f}%) | missed: {f.lines.missed} |"]
        if uncov:
            lines.append(f"  Uncovered lines: {uncov}{f'  (+{more} more)' if more else ''}")
        lines.append("")
    return [TextContent(type="text", text="\n".join(lines))]


async def _get_uncovered_lines_file(args: dict) -> list[TextContent]:
    report  = await asyncio.to_thread(_get_report, args["tag"])
    file_f  = args.get("file", "").strip()
    fold_f  = args.get("folder", "").rstrip("/").strip()
    limit   = int(args.get("limit", 50))
    matched = [
        f for f in report.files
        if (not file_f or file_f in f.filename)
        and (not fold_f or f.filename.startswith(fold_f))
        and f.lines.missed > 0
    ]
    matched = sorted(matched, key=lambda f: -f.lines.missed)
    if not matched:
        return [TextContent(type="text", text="No uncovered lines found.")]
    lines = [f"## Uncovered Lines — tag: {args['tag']}", f"Showing {min(limit, len(matched))} of {len(matched)} files", ""]
    for f in matched[:limit]:
        uncov = f.uncovered_lines[:20]
        more  = len(f.uncovered_lines) - len(uncov)
        lines.append(f"### {f.filename}  ({f.lines.missed} missed)")
        lines.append(f"  Lines: {uncov}{f'  (+{more} more)' if more else ''}")
        lines.append("")
    return [TextContent(type="text", text="\n".join(lines))]


async def _list_files_file(args: dict) -> list[TextContent]:
    report  = await asyncio.to_thread(_get_report, args["tag"])
    prefix  = args.get("prefix", "")
    sort_by = args.get("sort_by", "missed_lines")
    limit   = int(args.get("limit", 50))
    files   = [f for f in report.files if not prefix or f.filename.startswith(prefix)]
    key_fn  = {"missed_lines": lambda f: -f.lines.missed, "line_pct": lambda f: f.lines.percent, "filename": lambda f: f.filename}.get(sort_by, lambda f: -f.lines.missed)
    files   = sorted(files, key=key_fn)
    lines   = [f"## File List — tag: {args['tag']}", f"Filter: '{prefix or '(none)'}' | {len(files)} files", "", f"{'File':<80} {'Line%':>6}  {'Missed':>7}", "-" * 97]
    for f in files[:limit]:
        lines.append(f"{f.filename:<80} {f.lines.percent:>5.1f}%  {f.lines.missed:>7,}")
    return [TextContent(type="text", text="\n".join(lines))]


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    try:
        config.validate()
    except RuntimeError as e:
        print(f"[coverage-mcp-server] {e}", file=sys.stderr)
        sys.exit(1)

    logger.info(f"Starting | transport={config.MCP_TRANSPORT} | mongo={'yes' if config.use_mongo() else 'no (file mode)'}")

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
                        "headers": [(b"content-type", b"application/json"),
                                    (b"www-authenticate", b"Bearer")]})
            await send({"type": "http.response.body",
                        "body": b'{"error":"Unauthorized"}', "more_body": False})

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
