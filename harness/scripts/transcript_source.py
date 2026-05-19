#!/usr/bin/env python3
"""Transcript Source — merge Claude Code sessions, history, and MCP memory into rich transcripts.

Sources (by signal density):
  1. MCP memory session_summary entities (highest signal — structured LLM summaries)
  2. .claude/history.jsonl (medium signal — user inputs with timestamps)
  3. .claude/sessions/*.json (low signal — session metadata)

Output: JSONL transcript with enriched entries for observer analysis.
"""

import json
import re
from datetime import datetime
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
SESSIONS_DIR = CLAUDE_DIR / "sessions"
HISTORY_PATH = CLAUDE_DIR / "history.jsonl"
MEMORY_PATH = Path("D:/Claude/memory.jsonl")


def _extract_num(name: str) -> int:
    m = re.search(r"Summary_(\d+)_", name)
    return int(m.group(1)) if m else 0


def read_session_metadata() -> dict:
    """Read all session metadata files. Returns {sessionId: metadata}."""
    sessions = {}
    if not SESSIONS_DIR.exists():
        return sessions
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            sid = data.get("sessionId", "")
            if sid:
                sessions[sid] = {
                    "pid": data.get("pid"),
                    "cwd": data.get("cwd"),
                    "started_at": data.get("startedAt"),
                    "updated_at": data.get("updatedAt"),
                    "status": data.get("status"),
                }
        except (json.JSONDecodeError, OSError):
            continue
    return sessions


def read_user_history(session_id: str = None) -> list:
    """Read user inputs from history.jsonl, optionally filtered by session."""
    if not HISTORY_PATH.exists():
        return []
    entries = []
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if session_id and entry.get("sessionId") != session_id:
                    continue
                display = entry.get("display", "")[:500]
                ts = entry.get("timestamp")
                entries.append({
                    "text": display,
                    "timestamp": ts,
                    "session_id": entry.get("sessionId", ""),
                })
    except OSError:
        pass
    return entries


def read_memory_summaries(memory_path: Path = None) -> dict:
    """Extract all session_summary entities from memory.jsonl."""
    path = memory_path or MEMORY_PATH
    if not path.exists():
        return {}

    summaries = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") == "entity" and entry.get("entityType") == "session_summary":
                    name = entry.get("name", "")
                    observations = entry.get("observations", [])
                    if observations:
                        summaries[name] = observations
    except OSError:
        pass
    return summaries


def merge_to_transcript(
    session_meta: dict,
    history: list,
    mcp_summaries: dict,
    window_hours: int = 24,
) -> list:
    """Merge all sources into a rich transcript.

    Priority: MCP summaries > history entries. Session metadata adds context.
    """
    entries = []

    # 1. Add session context header
    active_sessions = {k: v for k, v in session_meta.items() if v.get("status") == "busy"
                       or v.get("status") == "active"}
    recent_sessions = {k: v for k, v in session_meta.items()
                       if v.get("started_at") and v.get("started_at", 0) > 0}

    if active_sessions or recent_sessions:
        context_info = f"Sessions: {len(session_meta)} total, "
        context_info += f"{len(active_sessions)} active, "
        context_info += f"{len(history)} history entries"
        entries.append({"type": "context", "content": context_info})

    # 2. Add MCP session summaries as the primary signal source
    sorted_names = sorted(mcp_summaries.keys(), key=_extract_num)
    latest = sorted_names[-1:] if sorted_names else []  # Only most recent

    for name in latest:
        observations = mcp_summaries[name]
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

        if request:
            entries.append({"role": "user", "content": request[:500], "session": name})
        if investigated:
            entries.append({"type": "tool_use", "name": "investigate", "session": name})
            # Simulate tool result — check for error patterns in the investigation text
            if any(word in investigated.lower() for word in ["error", "failed", "bug", "issue", "wrong"]):
                entries.append({"type": "tool_result", "content": "Error: " + investigated[:200],
                                "session": name, "status": "error"})
        if learned or completed:
            combined = f"Learned: {learned[:300]}\nCompleted: {completed[:300]}"
            entries.append({"role": "assistant", "content": combined, "session": name})
            entries.append({"type": "tool_use", "name": "apply_learning", "session": name})

    # 3. Add recent user history with timestamps
    recent_history = [h for h in history if h.get("timestamp") and h["timestamp"] > 0]
    recent_history.sort(key=lambda h: h.get("timestamp", 0))

    for h in recent_history[-20:]:  # Last 20 user inputs
        text = h.get("text", "").strip()
        if len(text) > 5 and text not in [e.get("content", "") for e in entries if e.get("role") == "user"]:
            entries.append({
                "role": "user",
                "content": text[:300],
                "timestamp": h.get("timestamp"),
                "session_id": h.get("session_id", ""),
            })

    return entries


def build_latest_transcript(output_path: Path = None, memory_path: Path = None):
    """Main entry point: merge sources and write latest_session.jsonl."""
    if output_path is None:
        output_path = Path("D:/Claude/harness/latest_session.jsonl")

    print("[transcript_source] Reading session metadata...")
    session_meta = read_session_metadata()
    print(f"  Found {len(session_meta)} sessions")

    print("[transcript_source] Reading user history...")
    history = read_user_history()
    print(f"  Found {len(history)} history entries")

    print("[transcript_source] Reading MCP memory summaries...")
    mcp_summaries = read_memory_summaries(memory_path)
    print(f"  Found {len(mcp_summaries)} MCP session summaries")

    print("[transcript_source] Merging...")
    entries = merge_to_transcript(session_meta, history, mcp_summaries)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # Count signal richness
    tool_uses = sum(1 for e in entries if e.get("type") == "tool_use")
    errors = sum(1 for e in entries if e.get("status") == "error")
    user_msgs = sum(1 for e in entries if e.get("role") == "user")
    print(f"[transcript_source] Wrote {len(entries)} entries to {output_path}")
    print(f"  Signal: {user_msgs} user msgs, {tool_uses} tool_uses, {errors} errors")
    return entries


if __name__ == "__main__":
    build_latest_transcript()
