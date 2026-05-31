#!/usr/bin/env python3
"""Find and normalize the latest Claude Code raw transcript.

Output: normalized JSONL (one entry per message/tool-call/tool-result)
ready for observer analysis.

Usage:
  python get_last_session.py [--project D--Claude] [--output <path>]
"""

import json
import sys
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"


def find_latest_transcript(project: str = "D--Claude") -> Path | None:
    """Find the most recently modified transcript JSONL in the project directory."""
    project_dir = PROJECTS_DIR / project
    if not project_dir.exists():
        print(f"[get_session] Project dir not found: {project_dir}", file=sys.stderr)
        return None

    candidates = sorted(
        project_dir.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    # Skip non-session files (UUID format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx.jsonl)
    for path in candidates:
        stem = path.stem
        if len(stem) >= 32 and stem.count("-") >= 4:
            return path
    return None


def normalize_transcript(raw_path: Path) -> list[dict]:
    """Convert raw Claude Code transcript to normalized entries.

    Each output entry is one of:
      {"role": "user", "content": "..."}
      {"role": "assistant", "content": "...", "stop_reason": "..."}
      {"type": "tool_use", "name": "...", "input": {...}}
      {"type": "tool_result", "content": "...", "is_error": bool}
    """
    entries = []
    seen_ids = set()

    with open(raw_path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entry = json.loads(stripped)
            except json.JSONDecodeError:
                continue

            msg = entry.get("message")
            if not msg:
                continue

            msg_id = msg.get("id", "")
            role = msg.get("role", "")
            content = msg.get("content", [])
            stop_reason = msg.get("stop_reason", "")
            usage = msg.get("usage", {})

            # Content may be string (text-only) or list of blocks
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            if not isinstance(content, list):
                continue

            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type", "")
                block_id = f"{msg_id}:{block_type}"

                if block_type == "text" and role == "user":
                    # Skip dedup (same message repeated across lines)
                    if msg_id not in seen_ids:
                        seen_ids.add(msg_id)
                        entries.append({
                            "role": "user",
                            "content": block.get("text", "")[:500],
                        })
                elif block_type == "text" and role == "assistant":
                    if block_id not in seen_ids:
                        seen_ids.add(block_id)
                        entries.append({
                            "role": "assistant",
                            "content": block.get("text", "")[:500],
                            "stop_reason": stop_reason,
                        })
                elif block_type == "tool_use":
                    if block_id not in seen_ids:
                        seen_ids.add(block_id)
                        entries.append({
                            "type": "tool_use",
                            "name": block.get("name", ""),
                            "input": block.get("input", {}),
                        })
                elif block_type == "tool_result":
                    if block_id not in seen_ids:
                        seen_ids.add(block_id)
                        result_content = block.get("content", "")
                        if isinstance(result_content, list):
                            result_content = " ".join(
                                c.get("text", "") for c in result_content
                                if isinstance(c, dict)
                            )
                        is_error = block.get("is_error", False)
                        entries.append({
                            "type": "tool_result",
                            "content": str(result_content)[:1000],
                            "is_error": bool(is_error),
                        })

    return entries


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="D--Claude")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    path = find_latest_transcript(args.project)
    if not path:
        print("[get_session] No transcript found.", file=sys.stderr)
        sys.exit(0)

    entries = normalize_transcript(path)
    if not entries:
        print("[get_session] Empty transcript.", file=sys.stderr)
        sys.exit(0)

    output = Path(args.output) if args.output else (
        Path("D:/Claude/harness/latest_session.jsonl")
    )
    output.parent.mkdir(parents=True, exist_ok=True)

    with open(output, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    tool_count = sum(1 for e in entries if e.get("type") == "tool_use")
    error_count = sum(1 for e in entries if e.get("is_error"))
    user_count = sum(1 for e in entries if e.get("role") == "user")
    print(f"[get_session] {path.name} → {output}")
    print(f"  {len(entries)} entries: {user_count} user msgs, {tool_count} tool_uses, {error_count} errors")
    print(f"  Size: {path.stat().st_size / 1024 / 1024:.1f}MB")


if __name__ == "__main__":
    main()
