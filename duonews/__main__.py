"""Unified CLI entry point for the DuoNews pipeline.

Usage:
    python -m duonews --step search --date 2026-05-31
    python -m duonews --step preprocess --date 2026-05-31
    python -m duonews --step push --date 2026-05-31
    python -m duonews --step all --date 2026-05-31
    python -m duonews --step diagnose
"""

import argparse
import sys

STEP_MAP = {
    "github":       ("duonews.github_trending", "fetch_github_trending"),
    "search":       ("duonews.search", "run_daily_search"),
    "arxiv":        ("duonews.arxiv", "fetch_arxiv"),
    "preprocess":   ("duonews.preprocess", "generate_brief"),
    "cross_day":    ("duonews.cross_day", "search_cross_day"),
    "report_write": ("duonews.report_writer", "generate_report"),
    "push":         ("duonews.push", "push_to_feishu"),
    "vectorize":    ("duonews.vectorize", "run_vectorizer"),
    "feedback":     ("duonews.feedback", "run_feedback_loop"),
    "diagnose":     ("duonews.diagnose", "run_all_checks"),
}

PIPELINE_ORDER = [
    "github", "search", "arxiv",          # Phase 1: Data gathering
    "preprocess", "cross_day",             # Phase 2: Preprocess & analysis
    "report_write",                        # Phase 3: Report generation
    "push", "vectorize", "feedback",       # Phase 4: Distribution & feedback
]


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

    def _get_date(date_str=None):
        from datetime import date
        return date_str or date.today().isoformat()

    def _run_step(step, date_str, top_n):
        """Run a single pipeline step with correct argument handling."""
        mod_path, func_name = STEP_MAP[step]
        mod = __import__(mod_path, fromlist=[func_name])
        func = getattr(mod, func_name)

        if step == "cross_day":
            from harness.indexer import HarnessDB
            db = HarnessDB()
            discoveries = func(date_str, db)
            from .cross_day import format_cross_day_results
            print(format_cross_day_results(discoveries))
            db.close()
        elif step == "vectorize":
            from . import OBSIDIAN_NEWS
            report_path = OBSIDIAN_NEWS / f"{date_str}.md"
            if not report_path.exists():
                print(f"[duonews] Report not found: {report_path}", file=sys.stderr)
                return
            from harness.indexer import HarnessDB
            db = HarnessDB()
            from .vectorize import parse_news_file, vectorize_snippets
            snippets = parse_news_file(report_path)
            print(f"[duonews] Parsed {len(snippets)} snippets from {report_path.name}")
            vectorize_snippets(snippets, db)
            print(f"[duonews] Embedded and stored {len(snippets)} snippets")
            db.close()
        elif step == "preprocess":
            func(date_str=date_str, top_n=top_n)
        elif step == "report_write":
            func(date_str=date_str)
        elif step == "diagnose":
            func(date_str=date_str)
        else:
            func(date_str=date_str)

    if args.step == "all":
        import uuid
        import json as _json
        from datetime import datetime as _datetime, timezone as _timezone

        # Fault tolerance rules: which failures abort the pipeline
        ABORT_ON_FAILURE = {"search", "preprocess", "report_write"}
        RETRY_ONCE = {"search"}  # Steps that get one retry on failure

        pipeline_state = {
            "run_id": str(uuid.uuid4())[:8],
            "date": args.date or _get_date(),
            "command": "all",
            "started_at": _datetime.now(_timezone.utc).isoformat(),
            "steps": {},
        }

        def _save_pipeline_state():
            from . import DUONEWS_DIR
            state_path = DUONEWS_DIR / ".pipeline_state.json"
            state_path.write_text(
                _json.dumps(pipeline_state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        _save_pipeline_state()

        for step in PIPELINE_ORDER:
            print(f"\n{'='*60}")
            print(f"  STEP: {step}")
            print(f"{'='*60}")

            step_start = _datetime.now(_timezone.utc)
            pipeline_state["steps"][step] = {
                "status": "running",
                "started_at": step_start.isoformat(),
                "completed_at": None,
                "error": None,
                "rows_affected": 0,
            }
            _save_pipeline_state()

            try:
                _run_step(step, args.date, args.top)
                pipeline_state["steps"][step]["status"] = "succeeded"
            except Exception as e:
                error_msg = f"{type(e).__name__}: {e}"
                print(f"[duonews] Step '{step}' failed: {error_msg}", file=sys.stderr)

                # Retry logic for designated steps
                if step in RETRY_ONCE:
                    print(f"[duonews] Retrying '{step}' once...", file=sys.stderr)
                    try:
                        _run_step(step, args.date, args.top)
                        pipeline_state["steps"][step]["status"] = "succeeded"
                        pipeline_state["steps"][step]["error"] = f"Retry succeeded after: {error_msg}"
                    except Exception as e2:
                        pipeline_state["steps"][step]["error"] = f"Retry also failed: {type(e2).__name__}: {e2}"
                        pipeline_state["steps"][step]["status"] = "failed"
                else:
                    pipeline_state["steps"][step]["error"] = error_msg
                    pipeline_state["steps"][step]["status"] = "failed"

                # Check if this failure should abort the pipeline
                if step in ABORT_ON_FAILURE and pipeline_state["steps"][step]["status"] == "failed":
                    pipeline_state["completed_at"] = _datetime.now(_timezone.utc).isoformat()
                    pipeline_state["status"] = "failed"
                    _save_pipeline_state()
                    print(f"[duonews] Pipeline ABORTED at '{step}' (critical step failed)",
                          file=sys.stderr)
                    return

            pipeline_state["steps"][step]["completed_at"] = _datetime.now(_timezone.utc).isoformat()
            _save_pipeline_state()

        pipeline_state["completed_at"] = _datetime.now(_timezone.utc).isoformat()
        pipeline_state["status"] = "succeeded"
        _save_pipeline_state()
        print(f"\n[duonews] Pipeline complete. State saved to duonews/.pipeline_state.json")
        return

    if args.step not in STEP_MAP:
        print(f"Unknown step: {args.step}", file=sys.stderr)
        sys.exit(1)

    _run_step(args.step, args.date, args.top)


if __name__ == "__main__":
    main()
