#!/usr/bin/env python3
"""MCP Bridge — convert MCP memory session summaries to observer-compatible JSONL.

Usage:
  python mcp_bridge.py                           # Process all sessions, observer reads latest
  python mcp_bridge.py --all                      # Output ALL sessions (for batch analysis)
  python mcp_bridge.py --session Summary_267_xxx  # Output specific session by name
"""

import json
import argparse
import re
from pathlib import Path

HARNESS_DIR = Path(__file__).resolve().parent.parent


def extract_sessions(memory_path: Path) -> dict:
    """Extract all session_summary entities from memory.jsonl, keyed by session name."""
    if not memory_path.exists():
        print(f"[bridge] {memory_path} not found.")
        return {}

    lines = []
    with open(memory_path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped:
                try:
                    lines.append(json.loads(stripped))
                except json.JSONDecodeError:
                    continue

    summaries = {}
    for entry in lines:
        if entry.get("type") == "entity" and entry.get("entityType") == "session_summary":
            name = entry.get("name", "")
            observations = entry.get("observations", [])
            if observations:
                summaries[name] = observations
    return summaries


def _extract_num(name: str) -> int:
    m = re.search(r"Summary_(\d+)_", name)
    return int(m.group(1)) if m else 0


def session_to_transcript(name: str, observations: list) -> list:
    """Convert one session's observations to transcript entries."""
    request = ""
    learned = ""
    completed = ""
    investigated = ""

    for obs in observations:
        if obs.startswith("❓ Request:"):
            request = obs.replace("❓ Request:", "").strip()
        elif obs.startswith("💡 Learned:"):
            learned = obs.replace("💡 Learned:", "").strip()
        elif obs.startswith("✅ Completed:"):
            completed = obs.replace("✅ Completed:", "").strip()
        elif obs.startswith("🔍 Investigated:"):
            investigated = obs.replace("🔍 Investigated:", "").strip()

    entries = []
    if request:
        entries.append({"role": "user", "content": request[:500], "session": name})
    if investigated:
        entries.append({"type": "tool_use", "name": "investigate", "session": name})
    if learned or completed:
        combined = f"Learned: {learned[:300]}\nCompleted: {completed[:300]}"
        entries.append({"role": "assistant", "content": combined, "session": name})
        # Complex sessions: learned a lot → mark with tool calls
        if len(learned) > 200:
            entries.append({"type": "tool_use", "name": "apply_learning", "session": name})

    return entries


def write_session_file(name: str, entries: list, output_dir: Path):
    """Write one session's transcript to a JSONL file."""
    output_dir.mkdir(parents=True, exist_ok=True)
    filepath = output_dir / f"{name}.jsonl"
    with open(filepath, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return filepath


def main():
    parser = argparse.ArgumentParser(description="MCP Memory Bridge")
    parser.add_argument("--input", default="D:/Claude/memory.jsonl")
    parser.add_argument("--session", default=None, help="Process specific session by name")
    parser.add_argument("--all", action="store_true", help="Write ALL sessions as separate files")
    args = parser.parse_args()

    summaries = extract_sessions(Path(args.input))
    if not summaries:
        print("[bridge] No session summaries found.")
        return

    # Sort sessions by number
    sorted_names = sorted(summaries.keys(), key=_extract_num)

    sessions_dir = HARNESS_DIR / "sessions"

    if args.session:
        # Output specific session
        if args.session not in summaries:
            print(f"[bridge] Session {args.session} not found. Available: {sorted_names[-5:]}")
            return
        entries = session_to_transcript(args.session, summaries[args.session])
        path = write_session_file(args.session, entries, sessions_dir)
        print(f"[bridge] Wrote {len(entries)} entries to {path}")
    elif args.all:
        # Output ALL sessions
        count = 0
        for name in sorted_names:
            entries = session_to_transcript(name, summaries[name])
            write_session_file(name, entries, sessions_dir)
            count += 1
        print(f"[bridge] Wrote {count} session files to {sessions_dir}")
    else:
        # Default: only the LATEST session → harness reads this
        latest_name = sorted_names[-1]
        entries = session_to_transcript(latest_name, summaries[latest_name])

        # Write to latest_session.jsonl (single file, observer input)
        latest_path = HARNESS_DIR / "latest_session.jsonl"
        with open(latest_path, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        # Also archive to sessions dir
        write_session_file(latest_name, entries, sessions_dir)

        print(f"[bridge] Latest session: {latest_name}")
        print(f"[bridge] Wrote {len(entries)} entries to {latest_path}")
        print(f"[bridge] Archived to {sessions_dir / (latest_name + '.jsonl')}")


if __name__ == "__main__":
    main()
