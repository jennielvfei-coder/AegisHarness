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
CONSTRAINT_CACHE_PATH = HARNESS_DIR / ".constraint_cache.json"
EXECUTION_FLAG_PATH = HARNESS_DIR / ".execution_flag.json"

# ── Schema init (idempotent, runs on first call) ──

def _ensure_tables():
    """Create lightweight tables if they don't exist. Fast — no migration logic."""
    conn = sqlite3.connect(str(DB_PATH), timeout=2)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tool_call_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tool_name TEXT NOT NULL,
            status TEXT NOT NULL,
            error_type TEXT,
            duration_ms INTEGER,
            session_id TEXT,
            timestamp REAL NOT NULL DEFAULT (unixepoch())
        );
        CREATE INDEX IF NOT EXISTS idx_tool_log_status ON tool_call_log(status);
        CREATE INDEX IF NOT EXISTS idx_tool_log_tool ON tool_call_log(tool_name);

        CREATE TABLE IF NOT EXISTS signal_buffer (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_type TEXT NOT NULL,
            content TEXT,
            session_id TEXT,
            timestamp REAL NOT NULL DEFAULT (unixepoch())
        );
    """)
    conn.commit()
    conn.close()


# ── Constraint cache ──────────────────────────────────────────────────

def _load_constraints() -> list:
    """Load active constraints from the injector-written cache file."""
    try:
        if CONSTRAINT_CACHE_PATH.exists():
            data = json.loads(CONSTRAINT_CACHE_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
    except Exception:
        pass
    return []


def _check_constraints(tool_name: str, tool_input: str) -> dict | None:
    """Check if this tool call matches any active constraint. Returns first match."""
    constraints = _load_constraints()
    if not constraints:
        return None
    inp = tool_input.lower() if tool_input else ""
    for c in constraints:
        c_tool = c.get("tool_name", "")
        if c_tool == tool_name or c_tool == "*":
            pattern = c.get("match_pattern", "").lower()
            if pattern and pattern in inp:
                return c
    return None


# ── PreToolUse: constraint block ──

def _read_execution_flag() -> dict | None:
    """Read the external-data-received flag. Returns dict or None."""
    try:
        if EXECUTION_FLAG_PATH.exists():
            return json.loads(EXECUTION_FLAG_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _clear_execution_flag():
    """Clear the execution flag (user has seen output and responded)."""
    try:
        EXECUTION_FLAG_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def _set_execution_flag(tool_name: str):
    """Set the execution flag when external data is received."""
    try:
        EXECUTION_FLAG_PATH.write_text(
            json.dumps({"tool": tool_name, "timestamp": time.time()}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


def pre_tool_use(tool_name: str, tool_input: str):
    """PreToolUse hook — constraint block + execution-pause check.

    Constraint block: match active constraint = inject hard block context.

    Execution pause: if an Edit/Write follows an external data result
    (WebFetch/WebSearch) in the same turn without a user message between,
    inject a soft reminder to confirm diagnosis before editing.
    This catches the "found something → immediately started coding" pattern.
    """
    # ── Constraint block (existing) ──
    matched = _check_constraints(tool_name, tool_input)
    if matched:
        violations = matched.get("violations", 0)
        max_v = matched.get("max_violations", 5)
        block_msg = (
            f"\n⛔ CONSTRAINT ACTIVE: {matched['name']}\n"
            f"{matched['message']}\n"
            f"当前违反次数: {violations}/{max_v}\n"
        )
        if violations >= max_v:
            block_msg += (
                "⚠️ 此约束已达到违反上限。这不是建议——是基于历史失败数据的硬阻断。\n"
            )
        print(block_msg)
        print(
            f"[harness] ⛔ BLOCKED: {tool_name} matched '{matched['name']}'",
            file=sys.stderr,
        )

    # ── Execution pause: external data → edit without user confirmation ──
    if tool_name in ("Edit", "Write"):
        flag = _read_execution_flag()
        if flag:
            elapsed = time.time() - flag.get("timestamp", 0)
            if elapsed < 300:  # within 5 minutes
                print(
                    f"\n⏸️  外部数据来自 {flag.get('tool', '外部源')} ({elapsed:.0f}s前)。\n"
                    f"你是否已向用户确认过当前的诊断方向？\n"
                    f"如果没有，先输出诊断文本，确认后再编辑。\n"
                )
                print(
                    f"[harness] ⏸️  Edit follows external data ({flag.get('tool')})"
                    f" — soft pause",
                    file=sys.stderr,
                )


# ── PostToolUse: log results + constraint violations ──

def post_tool_use(tool_name: str, tool_input: str, tool_output: str = ""):
    """PostToolUse hook — log success/failure and increment constraint violations.

    If a constrained tool was called anyway (Claude ignored the block),
    increment the violation counter. After max_violations, the constraint
    escalates to session-fatal in the next PreToolUse.
    """
    _ensure_tables()

    status = "success"
    error_type = None

    output_lower = tool_output.lower()[:500] if tool_output else ""
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

        # Constraint violation tracking
        matched = _check_constraints(tool_name, tool_input)
        if matched:
            conn.execute(
                "UPDATE constraints SET violation_count = violation_count + 1 "
                "WHERE name = ?",
                (matched["name"],),
            )
        conn.commit()
        conn.close()
    except Exception:
        pass

    # Within-session constraint propagation: detect failure → block same-session retries
    if status == "error":
        _propagate_within_session_constraint(tool_name, tool_input, tool_output)

    # Execution flag: external data received → mark for pre_tool_use check
    if tool_name in ("WebFetch", "WebSearch", "Agent"):
        _set_execution_flag(tool_name)


# ── UserPromptSubmit: real-time signal detection ──

def user_prompt_submit(message: str = ""):
    """Scan user message for correction/preference signals. Stores to signal_buffer.

    Also clears the execution flag — user has seen output and is responding.
    """
    _ensure_tables()

    # Clear execution pause flag — user is responding
    _clear_execution_flag()

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

    # News feedback: short messages with sentiment keywords
    if not signal_type:
        _sentiment_words = {"不错", "好", "很好", "很棒", "喜欢", "有意思", "精彩",
                           "详细", "深度", "太浅", "浅", "差", "不好", "无聊",
                           "没意思", "太长", "太短", "一般", "不详细"}
        if len(msg) < 120 and any(w in msg for w in _sentiment_words):
            signal_type = "news_feedback"

    # Judgment update: user revises a past judgment
    if not signal_type:
        _judgment_markers = {"不成立了", "推翻", "概率调", "概率改成", "更新概率",
                            "重新评估", "改为", "改成", "不再成立",
                            "H", "C", "P"}
        # Trigger when message contains label (H1/C2/P3) + revision action
        has_label = any(re.match(r'^[HCP]\d', w) for w in msg.split())
        has_revision = any(w in msg for w in _judgment_markers)
        if has_label and has_revision:
            signal_type = "judgment_update"

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

    # Also detect context_reference: did Claude use harness-injected context?
    _detect_context_reference(message)


def _propagate_within_session_constraint(tool_name: str, tool_input: str, tool_output: str):
    """Detect tool failure → write temporary constraint to cache for same-session blocking.

    Uses omega_predictor._analyze_tool_failure (shared, zero-LLM) to classify the error.
    Network/permission failures get a temporary 1-hour constraint written to the cache
    that PreToolUse reads. This closes the within-session loop:
      PostToolUse detects failure → constraint_cache updated → PreToolUse blocks retry.

    Constraint expires in 1 hour — long enough for the current session, short enough
    not to pollute future sessions (permanent constraints come from cmd_observe).
    """
    try:
        # Lazy import — hooks must stay <500ms
        from omega_predictor import _analyze_tool_failure

        # Build a minimal tool_result-like entry for the shared analyzer
        entry = {
            "type": "tool_result",
            "name": tool_name,
            "input": {"command": tool_input} if tool_name == "Bash" else {"url": tool_input},
            "content": tool_output[:500] if tool_output else "",
        }

        info = _analyze_tool_failure(entry)
        if info is None:
            return
        if info.error_category not in ("network", "permission"):
            return

        # Load existing cache
        constraints = []
        if CONSTRAINT_CACHE_PATH.exists():
            try:
                constraints = json.loads(CONSTRAINT_CACHE_PATH.read_text(encoding="utf-8"))
                if not isinstance(constraints, list):
                    constraints = []
            except Exception:
                pass

        # Check if a constraint for this tool+pattern already exists
        pattern_lower = info.match_pattern.lower()
        for c in constraints:
            c_pattern = c.get("match_pattern", "").lower()
            if c.get("tool_name") == tool_name and c_pattern == pattern_lower:
                return  # Already constrained

        # Create temporary constraint
        import time as _time
        temp_constraint = {
            "name": f"auto:within-session {info.error_category} → {info.match_pattern[:60]}",
            "tool_name": tool_name,
            "match_pattern": info.match_pattern,
            "action": "block",
            "message": f"本会话中 {tool_name}({info.match_pattern[:80]}) 失败。"
                       f"临时阻断同类调用 (有效期: 1小时)。",
            "violations": 0,
            "max_violations": 3,
            "expires_at": _time.time() + 3600,
            "source": "within-session",
        }

        constraints.append(temp_constraint)
        CONSTRAINT_CACHE_PATH.write_text(
            json.dumps(constraints, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass  # Never let propagation crash the hook


def _detect_context_reference(message: str = ""):
    """Detect if Claude's response references harness-injected context.

    Checks the last assistant message for harness markers.
    Stores as signal_type='context_reference' in signal_buffer.
    """
    history_path = Path.home() / ".claude" / "history.jsonl"
    if not history_path.exists():
        return
    try:
        with open(history_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if not lines:
            return
        last = json.loads(lines[-1].strip())
        content = last.get("display", "")[:500]
        markers = [
            "harness_", "技能", "约束", "inject",
            "Harness", "意图匹配", "工作流", "预检",
        ]
        if any(m in content for m in markers):
            conn = sqlite3.connect(str(DB_PATH), timeout=2)
            conn.execute(
                f"INSERT INTO {SIGNAL_BUFFER_TABLE}(signal_type, content) VALUES(?,?)",
                ("context_reference", content[:200]),
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
