#!/usr/bin/env python3
"""MCP Bridge — convert MCP memory session summaries to observer-compatible JSONL.

Usage:
  python mcp_bridge.py                    # Read from memory.jsonl, output to memory_transcripts.jsonl
  python mcp_bridge.py --input <path>     # Custom input
  python mcp_bridge.py --output <path>    # Custom output
"""

import json
import argparse
from pathlib import Path


def extract_session_content(memory_path: Path) -> list:
    """Extract session summary observations from memory.jsonl and convert to transcript-like entries."""
    if not memory_path.exists():
        print(f"[bridge] {memory_path} not found.")
        return []

    # Read all memory lines
    lines = []
    with open(memory_path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped:
                try:
                    lines.append(json.loads(stripped))
                except json.JSONDecodeError:
                    continue

    # Find session_summary entities
    summaries = {}
    for entry in lines:
        if entry.get("type") == "entity" and entry.get("entityType") == "session_summary":
            name = entry.get("name", "")
            observations = entry.get("observations", [])
            if observations:
                summaries[name] = observations

    # Convert each summary to a transcript entry
    transcripts = []
    for name, observations in summaries.items():
        # Each observation is a structured summary line
        request = ""
        learned = ""
        completed = ""

        for obs in observations:
            if obs.startswith("❓ Request:"):
                request = obs.replace("❓ Request:", "").strip()
            elif obs.startswith("💡 Learned:"):
                learned = obs.replace("💡 Learned:", "").strip()
            elif obs.startswith("✅ Completed:"):
                completed = obs.replace("✅ Completed:", "").strip()

        if request:
            transcripts.append({
                "role": "user",
                "content": request[:500],
                "session": name
            })
        if learned or completed:
            combined = f"Learned: {learned[:300]}\nCompleted: {completed[:300]}"
            transcripts.append({
                "role": "assistant",
                "content": combined,
                "session": name
            })
            # Simulate tool calls for complex sessions
            if len(learned) > 200:
                transcripts.append({"type": "tool_use", "name": "skill_manage", "session": name})

    return transcripts


def main():
    parser = argparse.ArgumentParser(description="MCP Memory Bridge")
    parser.add_argument("--input", default="D:/Claude/memory.jsonl")
    parser.add_argument("--output", default="D:/Claude/harness/mcp_transcripts.jsonl")
    args = parser.parse_args()

    transcripts = extract_session_content(Path(args.input))

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        for entry in transcripts:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    print(f"[bridge] Wrote {len(transcripts)} transcript entries from "
          f"{len(set(t.get('session','') for t in transcripts))} sessions to {output_path}")


if __name__ == "__main__":
    main()
