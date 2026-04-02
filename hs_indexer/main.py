"""
hs_indexer/main.py — Hyperswitch impact analysis pipeline orchestrator.

Two phases:

  index   Build the Neo4j call graph from index.scip (run once per source change).
            1. build_callgraph   — parse SCIP → :Fn nodes + :CALLS edges
            2. build_trait_map   — annotate trait impls + generic type params
            3. annotate_guards   — annotate :CALLS edges with conditional guards

  query   Find which API endpoints are impacted by a changed function.

Usage:
    # Index phase (once after rust-analyzer scip . generates index.scip):
    python -m hs_indexer index \\
        --scip /path/to/hyperswitch/index.scip \\
        --src-root /path/to/hyperswitch

    # Query phase (any number of times):
    python -m hs_indexer query validate_customer_access \\
        --src-root /path/to/hyperswitch \\
        --out result.json

    # Query by file + line (when function name is ambiguous):
    python -m hs_indexer query \\
        --src-root /path/to/hyperswitch \\
        --file crates/router/src/core/payments/helpers.rs \\
        --line 5897 \\
        --out result.json
"""
import argparse
import os
import sys

# first index the code, then run queries repeatedly without re-indexing
def cmd_index(args: argparse.Namespace) -> None:
    from hs_indexer import build_callgraph, build_trait_map, annotate_guards

    print("=" * 60)
    print("Step 1/3  build_callgraph — SCIP → Neo4j call graph")
    print("=" * 60)
    build_callgraph.main(args.scip)

    print()
    print("=" * 60)
    print("Step 2/3  build_trait_map — trait impl + generic type annotations")
    print("=" * 60)
    build_trait_map.main(args.src_root)

    print()
    print("=" * 60)
    print("Step 3/3  annotate_guards — conditional guard annotations")
    print("=" * 60)
    annotate_guards.main(args.src_root)

    print()
    print("Indexing complete. Neo4j is ready for queries.")
    print("  Run:  python -m hs_indexer query <fn_name> --src-root <path> --out result.json")


def cmd_query(args: argparse.Namespace) -> None:
    from hs_indexer.find_impact import find_impact
    from hs_indexer.enrich_flows import enrich_flow, _DEFAULT_MODELS

    src_root = args.src_root or os.environ.get("SRC_ROOT", "")
    if not src_root:
        print("Error: provide --src-root or set SRC_ROOT env var.", file=sys.stderr)
        sys.exit(1)

    if not args.function and not (args.file_hint and args.line_hint):
        print("Error: provide a function name, or both --file and --line.", file=sys.stderr)
        sys.exit(1)

    os.environ["SRC_ROOT"] = src_root

    # Step 1: BFS impact analysis — always writes raw JSON to --out
    result = find_impact(
        args.function,
        src_root,
        max_depth=args.depth,
        out_path=args.out,
        file_hint=args.file_hint,
        line_hint=args.line_hint,
    )

    # Step 2: LLM enrichment — skipped when --no-enrich is set.
    if getattr(args, "no_enrich", False):
        print(f"\n[query] --no-enrich set — skipping LLM enrichment.", file=sys.stderr)
        return

    if not result or not result.get("flows"):
        return

    backend = args.backend
    if backend == "auto":
        if os.environ.get("JUSPAY_API_KEY"):
            backend = "grid"
        elif os.environ.get("ANTHROPIC_API_KEY"):
            backend = "anthropic"
        elif os.environ.get("GROQ_API_KEY"):
            backend = "groq"
        elif os.environ.get("GEMINI_API_KEY"):
            backend = "gemini"
        else:
            print(
                "\n  [enrich] No API key found (JUSPAY_API_KEY / ANTHROPIC_API_KEY / GROQ_API_KEY / GEMINI_API_KEY)."
                " Skipping LLM enrichment. Set a key or pass --backend ollama to enrich.",
                file=sys.stderr,
            )
            return

    model = _DEFAULT_MODELS[backend]
    flows = result.get("flows", [])
    fn_name = result.get("function", "?")
    print(f"\n[enrich] Backend: {backend} ({model}) — enriching {len(flows)} flow(s) for {fn_name} …",
          file=sys.stderr)

    enriched_flows = []
    for fl in flows:
        flow_id = fl.get("flow_id", "?")
        desc    = fl.get("description", "")[:80]
        print(f"  [flow {flow_id}] {desc} … ", file=sys.stderr, end="", flush=True)
        try:
            spec = enrich_flow(result, fl, backend, model)
            print("✓", file=sys.stderr)
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            spec = {"error": str(exc)}
        enriched_flows.append({**fl, "llm_spec": spec})

    enriched = {**{k: v for k, v in result.items() if k != "flows"}, "flows": enriched_flows}

    out_path = args.out or (f"{fn_name}_enriched.json" if fn_name != "?" else "impact_enriched.json")
    with open(out_path, "w") as f:
        import json
        json.dump(enriched, f, indent=2)
    print(f"\n[enrich] Written to: {out_path}", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="python -m hs_indexer",
        description="Hyperswitch call-graph indexer and impact analysis tool.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument(
        "--src-root",
        default=os.environ.get("SRC_ROOT", ""),
        metavar="PATH",
        help="Path to hyperswitch repo root (or set SRC_ROOT env var).",
    )

    sub = ap.add_subparsers(dest="command", required=True)

    # ── index subcommand ──────────────────────────────────────────────────────
    p_index = sub.add_parser(
        "index",
        help="Build Neo4j graph from index.scip  (run once per source change)",
        description="Runs build_callgraph → build_trait_map → annotate_guards in sequence.",
    )
    p_index.add_argument(
        "--scip",
        default="index.scip",
        metavar="PATH",
        help="Path to index.scip produced by `rust-analyzer scip .`  (default: ./index.scip)",
    )

    # ── query subcommand ──────────────────────────────────────────────────────
    p_query = sub.add_parser(
        "query",
        help="Find impacted API endpoints for a changed function",
        description="BFS upward from the changed function through the call graph to API endpoints.",
    )
    p_query.add_argument(
        "function",
        nargs="?",
        default=None,
        help="Function name (e.g. validate_customer_access). Optional when --file + --line are given.",
    )
    p_query.add_argument(
        "--file",
        dest="file_hint",
        metavar="PATH",
        help="Relative path to the changed file (e.g. crates/router/src/core/payments/helpers.rs).",
    )
    p_query.add_argument(
        "--line",
        dest="line_hint",
        type=int,
        metavar="N",
        help="Line number of the changed function within --file.",
    )
    p_query.add_argument(
        "--depth",
        type=int,
        default=12,
        metavar="N",
        help="Max BFS depth (default: 8).",
    )
    p_query.add_argument(
        "--out",
        metavar="FILE",
        help="Write JSON result to this file (also used as the enriched output path).",
    )
    p_query.add_argument(
        "--no-enrich",
        action="store_true",
        default=False,
        help="Skip LLM enrichment — write raw BFS JSON only (useful before running filter_false_positives).",
    )
    p_query.add_argument(
        "--backend",
        default="auto",
        choices=["auto", "grid", "anthropic", "groq", "gemini", "ollama"],
        help=(
            "LLM backend for enrichment (default: auto — picks from available API keys). "
            "Set JUSPAY_API_KEY for Grid/open-large, ANTHROPIC_API_KEY / GROQ_API_KEY / "
            "GEMINI_API_KEY, or use --backend ollama for a local model."
        ),
    )

    args = ap.parse_args()

    if args.command == "index":
        cmd_index(args)
    elif args.command == "query":
        cmd_query(args)


if __name__ == "__main__":
    main()
