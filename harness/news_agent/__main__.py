"""Unified CLI entry point for the news agent pipeline.

Usage:
    python -m harness.news_agent --step search --date 2026-05-27
    python -m harness.news_agent --step preprocess --date 2026-05-27
    python -m harness.news_agent --step push --date 2026-05-27
    python -m harness.news_agent --step all --date 2026-05-27
    python -m harness.news_agent --step diagnose
"""

import argparse
import sys

STEP_MAP = {
    "github":     ("harness.news_agent.github_trending", "fetch_github_trending"),
    "search":     ("harness.news_agent.search", "run_daily_search"),
    "arxiv":      ("harness.news_agent.arxiv", "fetch_arxiv"),
    "preprocess": ("harness.news_agent.preprocess", "generate_brief"),
    "cross_day":  ("harness.news_agent.cross_day", "search_cross_day"),
    "push":       ("harness.news_agent.push", "push_to_feishu"),
    "vectorize":  ("harness.news_agent.vectorize", "run_vectorizer"),
    "feedback":   ("harness.news_agent.feedback", "run_feedback_loop"),
    "diagnose":   ("harness.news_agent.diagnose", "run_all_checks"),
}

PIPELINE_ORDER = ["github", "search", "arxiv", "preprocess", "cross_day", "push", "vectorize", "feedback"]


def main():
    parser = argparse.ArgumentParser(
        description="News Agent — unified daily news pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(f"  {k:12s} {v[0]}.{v[1]}()" for k, v in STEP_MAP.items()),
    )
    parser.add_argument("--step", required=True,
                        choices=list(STEP_MAP) + ["all"],
                        help="Pipeline step to run, or 'all' for full pipeline")
    parser.add_argument("--date", help="Date YYYY-MM-DD (default: today)")
    parser.add_argument("--top", type=int, default=30, help="Top N snippets for preprocess")
    args = parser.parse_args()

    def _run_step(step, date_str, top_n):
        """Run a single pipeline step with correct argument handling."""
        mod_path, func_name = STEP_MAP[step]
        mod = __import__(mod_path, fromlist=[func_name])
        func = getattr(mod, func_name)

        if step == "cross_day":
            from ..indexer import HarnessDB
            db = HarnessDB()
            discoveries = func(date_str, db)
            from .cross_day import format_cross_day_results
            print(format_cross_day_results(discoveries))
            db.close()
        elif step == "vectorize":
            from . import OBSIDIAN_NEWS
            report_path = OBSIDIAN_NEWS / f"{date_str}.md"
            if not report_path.exists():
                print(f"[news_agent] Report not found: {report_path}", file=sys.stderr)
                return
            from ..indexer import HarnessDB
            db = HarnessDB()
            from .vectorize import parse_news_file, vectorize_snippets
            snippets = parse_news_file(report_path)
            print(f"[news_agent] Parsed {len(snippets)} snippets from {report_path.name}")
            vectorize_snippets(snippets, db)
            print(f"[news_agent] Embedded and stored {len(snippets)} snippets")
            db.close()
        elif step == "preprocess":
            func(date_str=date_str, top_n=top_n)
        elif step == "diagnose":
            func(date_str=date_str)
        else:
            func(date_str=date_str)

    if args.step == "all":
        for step in PIPELINE_ORDER:
            print(f"\n{'='*60}")
            print(f"  STEP: {step}")
            print(f"{'='*60}")
            try:
                _run_step(step, args.date, args.top)
            except Exception as e:
                print(f"[news_agent] Step '{step}' failed: {e}", file=sys.stderr)
        return

    if args.step not in STEP_MAP:
        print(f"Unknown step: {args.step}", file=sys.stderr)
        sys.exit(1)

    _run_step(args.step, args.date, args.top)


if __name__ == "__main__":
    main()
