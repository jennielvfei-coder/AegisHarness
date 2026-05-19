#!/usr/bin/env python3
"""Harness hooks — lightweight fire-and-forget handlers for Claude Code hooks.

Design: every hook runs in <500ms. Never blocks. Never crashes the session.
All heavy work is deferred to observer (Stop hook, post-session).
"""

import json
import sqlite3
import sys
import time
from pathlib import Path

HARNESS_DIR = Path(__file__).resolve().parent
DB_PATH = HARNESS_DIR / "state.db"
TOOL_LOG_TABLE = "tool_call_log"
SIGNAL_BUFFER_TABLE = "signal_buffer"

# ── Schema init (idempotent, runs on first call) ──

def _ensure_tables():
    """Create lightweight tables if they don't exist. Fast — no migration logic."""
    conn = sqlite3.connect(str(DB_PATH), timeout=2)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tool_call_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tool_name TEXT NOT NULL,
            status TEXT NOT NULL,       -- 'success' | 'error'
            error_type TEXT,            -- 'timeout' | 'dns' | 'permission' | 'rate_limit' | NULL
            duration_ms INTEGER,
            session_id TEXT,
            timestamp REAL NOT NULL DEFAULT (unixepoch())
        );
        CREATE INDEX IF NOT EXISTS idx_tool_log_status ON tool_call_log(status);
        CREATE INDEX IF NOT EXISTS idx_tool_log_tool ON tool_call_log(tool_name);

        CREATE TABLE IF NOT EXISTS signal_buffer (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_type TEXT NOT NULL,   -- 'correction' | 'preference' | 'retry' | 'interrupt'
            content TEXT,
            session_id TEXT,
            timestamp REAL NOT NULL DEFAULT (unixepoch())
        );
    """)
    conn.commit()
    conn.close()


# ── PreToolUse: inject reminder before WebFetch ──

def pre_tool_use(tool_name: str, tool_input: str):
    """PreToolUse hook — inject hints before known-problematic tools.

    Called BEFORE WebFetch / Bash / etc.
    Outputs a reminder line that Claude Code reads as context.
    """
    if tool_name in ("WebFetch", "WebSearch"):
        print("[harness] WebFetch reminder: if blocked, use browser-use MCP or set skipWebFetchPreflight",
              file=sys.stderr)


# ── PostToolUse: log tool results ──

def post_tool_use(tool_name: str, tool_input: str, tool_output: str = ""):
    """PostToolUse hook — log success/failure to local SQLite.

    Detects error signals in tool output and logs them for observer analysis.
    """
    _ensure_tables()

    status = "success"
    error_type = None

    # Fast error detection — substring match, no regex overhead
    output_lower = tool_output.lower()[:500]
    if any(e in output_lower for e in ("error", "failed", "traceback", "exception",
                                         "timeout", "denied", "blocked", "not found",
                                         "refused", "cannot", "unable", "exit code: 1")):
        status = "error"
        if "timeout" in output_lower:
            error_type = "timeout"
        elif any(e in output_lower for e in ("dns", "resolve", "unreachable", "refused")):
            error_type = "dns"
        elif any(e in output_lower for e in ("permission", "denied", "blocked", "forbidden")):
            error_type = "permission"
        elif any(e in output_lower for e in ("rate limit", "too many", "429")):
            error_type = "rate_limit"

    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=2)
        conn.execute(
            f"INSERT INTO {TOOL_LOG_TABLE}(tool_name, status, error_type) VALUES(?,?,?)",
            (tool_name, status, error_type),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass  # Never block on DB failure


# ── UserPromptSubmit: real-time signal detection ──

def user_prompt_submit(message: str = ""):
    """Scan user message for correction/preference signals. Stores to signal_buffer.

    If message is empty, reads the last unprocessed entry from history.jsonl.
    """
    _ensure_tables()

    if not message:
        message = _read_last_history_line() or ""
        if not message:
            return

    signal_type = None
    msg = message[:200]

    if any(w in msg for w in ("不对", "错了", "不是这样", "应该是", "改一下",
                               "纠正", "重新", "那个不对", "你忘了")):
        signal_type = "correction"
    elif any(w in msg for w in ("以后都", "我总是", "帮我记住", "我习惯",
                                 "我的偏好", "记住", "默认", "下次", "永远")):
        signal_type = "preference"
    elif len(msg) < 80 and any(w in msg for w in ("再", "重试", "换", "试试")):
        signal_type = "retry"

    if signal_type:
        try:
            conn = sqlite3.connect(str(DB_PATH), timeout=2)
            conn.execute(
                f"INSERT INTO {SIGNAL_BUFFER_TABLE}(signal_type, content) VALUES(?,?)",
                (signal_type, msg),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass


def _read_last_history_line() -> str | None:
    """Read the last user message from history.jsonl."""
    history_path = Path.home() / ".claude" / "history.jsonl"
    if not history_path.exists():
        return None
    try:
        with open(history_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if lines:
            last = json.loads(lines[-1].strip())
            return last.get("display", "")[:300]
    except Exception:
        pass
    return None


# ── CLI dispatch ──

def main():
    if len(sys.argv) < 2:
        return

    action = sys.argv[1]

    try:
        if action == "pre-tool":
            tool = sys.argv[2] if len(sys.argv) > 2 else ""
            inp = sys.argv[3] if len(sys.argv) > 3 else ""
            pre_tool_use(tool, inp)
        elif action == "post-tool":
            tool = sys.argv[2] if len(sys.argv) > 2 else ""
            inp = sys.argv[3] if len(sys.argv) > 3 else ""
            out = sys.argv[4] if len(sys.argv) > 4 else ""
            post_tool_use(tool, inp, out)
        elif action == "user-msg":
            msg = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
            user_prompt_submit(msg)
    except Exception:
        pass  # Never let a hook crash the session


if __name__ == "__main__":
    main()
