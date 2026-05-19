#!/usr/bin/env python3
"""Harness daemon — Claude Code self-evolving harness framework.

Usage:
  python harness_daemon.py observe   # Called by StopSession hook
  python harness_daemon.py inject    # Called by StartSession hook (Phase 3)
"""

import argparse
import sys
from pathlib import Path

import yaml

HARNESS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HARNESS_DIR))


def load_config(config_path):
    """Load the harness YAML configuration file."""
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def cmd_observe():
    """Phase 1-2: Analyze the latest session transcript, record observations, and invoke refiner."""
    config_path = HARNESS_DIR / "harness_config.yaml"
    config = load_config(config_path)

    # Resolve transcript path from YAML config (not hardcoded)
    transcript_dir = Path(config["harness"]["transcript_dir"])
    transcript_file = config["harness"]["transcript_file"]
    transcript_path = transcript_dir / transcript_file

    # Resolve db path from YAML config (not hardcoded)
    db_path = Path(config["harness"]["db_path"])

    # Lazy imports — deferred because modules may not exist yet in Phase 1
    from observer import analyze_session  # deferred: Phase 1 observer module may not exist yet
    from indexer import HarnessDB         # deferred: Phase 1 indexer module may not exist yet

    report = analyze_session(transcript_path, config_path)
    if report is None:
        print("[harness] No transcript found or nothing to analyze.")
        return

    # Read raw transcript content for refiner
    session_content = ""
    if transcript_path.exists():
        try:
            session_content = transcript_path.read_text(encoding="utf-8")
        except Exception:
            session_content = ""

    db = HarnessDB(db_path)
    db.save_observation(report)
    print(f"[harness] Observation saved: action={report.action}, "
          f"confidence={report.confidence:.2f}")

    # Phase 2: If observation is actionable, call refiner
    if report.action in ("patch_skill", "create_skill"):
        print("[harness] Actionable observation — invoking refiner...")
        config = load_config(config_path)
        if config.get("refiner", {}).get("enabled", False):
            from refiner import refine
            skill_path = refine(report, session_content, config_path)
            if skill_path:
                print(f"[harness] New skill generated: {skill_path}")
    elif report.action == "update_preference":
        print("[harness] Preference detected — generating memory update...")
        from refiner import generate_preference
        pref = generate_preference(report, session_content)
        if pref:
            print(f"[harness] Preference: {pref}")

    db.close()


def cmd_inject():
    """Phase 3: Search for relevant fragments and output injection text."""
    # Read config following the same pattern as cmd_observe
    config_path = HARNESS_DIR / "harness_config.yaml"
    config = load_config(config_path)

    # Resolve db path from YAML config (consistent with cmd_observe)
    db_path = Path(config["harness"]["db_path"])

    print("[harness] injector not yet implemented (Phase 3).")


def main():
    parser = argparse.ArgumentParser(description="Harness daemon")
    parser.add_argument("command", choices=["observe", "inject"])
    args = parser.parse_args()

    try:
        if args.command == "observe":
            cmd_observe()
        elif args.command == "inject":
            cmd_inject()
    except Exception as e:
        print(f"[harness] Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
