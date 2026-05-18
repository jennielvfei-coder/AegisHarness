#!/usr/bin/env python3
"""Harness daemon — Claude Code self-evolving harness framework.

Usage:
  python harness_daemon.py observe   # Called by StopSession hook
  python harness_daemon.py inject    # Called by StartSession hook (Phase 3)
"""

import argparse
import sys
from pathlib import Path

HARNESS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HARNESS_DIR))


def cmd_observe():
    """Phase 1: Analyze the latest session transcript and record observations."""
    from observer import analyze_session
    from indexer import HarnessDB

    config_path = HARNESS_DIR / "harness_config.yaml"
    transcript_path = HARNESS_DIR.parent / "memory.jsonl"

    report = analyze_session(transcript_path, config_path)
    if report is None:
        print("[harness] No transcript found or nothing to analyze.")
        return

    db = HarnessDB(HARNESS_DIR / "state.db")
    db.save_observation(report)
    print(f"[harness] Observation saved: action={report.action}, "
          f"confidence={report.confidence:.2f}")


def cmd_inject():
    """Phase 3: Search for relevant fragments and output injection text."""
    print("[harness] injector not yet implemented (Phase 3).")


def main():
    parser = argparse.ArgumentParser(description="Harness daemon")
    parser.add_argument("command", choices=["observe", "inject"])
    args = parser.parse_args()

    if args.command == "observe":
        cmd_observe()
    elif args.command == "inject":
        cmd_inject()


if __name__ == "__main__":
    main()
