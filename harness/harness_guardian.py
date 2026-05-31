"""Harness Guardian — independent verification daemon.

ECC-inspired Instincts layer: verification is not a pipeline step, it's an
always-on daemon process. Survives pipeline crashes. Watches belief_traces
and auto-verifies without depending on any harness module.

Modes:
    daemon    Continuous polling loop (default interval: 60s)
    pulse     One-shot check, then exit
    status    Print guardian health and verification stats

Design:
    - Zero harness imports — self-contained verification logic
    - Own SQLite connection with WAL mode for concurrent access
    - Signal-driven: polls belief_traces, applies verification rules
    - Crash-resilient: if harness pipeline dies, guardian keeps verifying

Verification Rules (applied in order):
    1. STALE — belief > STALE_DAYS old, no recurrence → transient, resolved
    2. RECURRING — same modality+tool in >= RECUR_SESSIONS sessions → confirmed
    3. ORPHAN — tool never appeared in any session after ORPHAN_DAYS → false alarm

Usage:
    python harness_guardian.py daemon --interval 30     # poll every 30s
    python harness_guardian.py pulse                     # one-shot
    python harness_guardian.py status                    # health report
"""

from __future__ import annotations

import argparse
import signal
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────

STALE_DAYS = 3         # beliefs older than this with no recurrence → resolved
RECUR_SESSIONS = 2      # distinct sessions to confirm a recurring pattern
ORPHAN_DAYS = 7         # tool never seen again after this long → false alarm
DEFAULT_INTERVAL = 60   # seconds between daemon polls
DB_PATH = Path(__file__).resolve().parent / "state.db"

# ── Verification logic ─────────────────────────────────────────────────────


def _get_unverified_beliefs(conn: sqlite3.Connection) -> list[dict]:
    """Fetch all unverified belief traces."""
    cur = conn.execute(
        "SELECT id, session_id, belief_type, tool_name, match_pattern, "
        "confidence, evidence, created_at "
        "FROM belief_traces WHERE was_correct = 0 "
        "ORDER BY created_at ASC"
    )
    return [
        {
            "id": r[0], "session_id": r[1], "belief_type": r[2],
            "tool_name": r[3], "match_pattern": r[4],
            "confidence": r[5], "evidence": r[6], "created_at": r[7],
        }
        for r in cur.fetchall()
    ]


def _check_recurrence(conn: sqlite3.Connection, belief: dict) -> tuple[int, int]:
    """Check how many later sessions have the same belief_type.

    Returns (later_session_count, total_session_count).
    """
    cur = conn.execute(
        "SELECT COUNT(DISTINCT session_id) FROM belief_traces "
        "WHERE belief_type = ? AND session_id != ? AND created_at > ?",
        (belief["belief_type"], belief["session_id"], belief["created_at"]),
    )
    later = cur.fetchone()[0] or 0

    cur = conn.execute(
        "SELECT COUNT(DISTINCT session_id) FROM belief_traces "
        "WHERE belief_type = ?",
        (belief["belief_type"],),
    )
    total = cur.fetchone()[0] or 0

    return later, total


def _check_tool_vanished(conn: sqlite3.Connection, belief: dict) -> bool:
    """Check if this belief's tool has never appeared in any session after it.

    Returns True if the tool appears to have vanished (no later occurrences).
    """
    tool_name = belief.get("tool_name", "")
    if not tool_name:
        return False  # can't determine without tool name

    # Check tool_call_log for any occurrence after this belief
    try:
        cur = conn.execute(
            "SELECT COUNT(*) FROM tool_call_log "
            "WHERE tool_name = ? AND timestamp > ?",
            (tool_name, belief["created_at"]),
        )
        later = cur.fetchone()[0] or 0
        return later == 0
    except Exception:
        return False


def _check_tool_recurrence(conn: sqlite3.Connection, belief: dict) -> int:
    """Count distinct sessions where the same tool_name had errors after this belief.

    Returns count of later sessions with same-tool failures.
    """
    tool_name = belief.get("tool_name", "")
    if not tool_name:
        return 0

    try:
        # Check belief_traces for same tool_name in later sessions
        cur = conn.execute(
            "SELECT COUNT(DISTINCT session_id) FROM belief_traces "
            "WHERE tool_name = ? AND session_id != ? AND created_at > ?",
            (tool_name, belief["session_id"], belief["created_at"]),
        )
        return cur.fetchone()[0] or 0
    except Exception:
        return 0


def _verify_one(conn: sqlite3.Connection, belief: dict) -> tuple[int, str]:
    """Apply verification rules to a single belief.

    Returns (was_correct, reason).
        was_correct: 1 = verified correct, -1 = verified wrong (false alarm)
    """
    now = time.time()
    age_days = (now - belief["created_at"]) / 86400 if belief["created_at"] else 0

    later_sessions, total_sessions = _check_recurrence(conn, belief)
    tool_later = _check_tool_recurrence(conn, belief)
    tool_vanished = _check_tool_vanished(conn, belief)

    # Rule 1: STALE — old belief with no recurrence anywhere → transient, resolved
    if age_days >= STALE_DAYS and later_sessions == 0:
        return (1, f"stale_resolved:{age_days:.0f}d_old_no_recurrence")

    # Rule 2: RECURRING — same pattern across multiple sessions → confirmed real
    if total_sessions >= RECUR_SESSIONS and later_sessions >= 1:
        return (1, f"recurring_confirmed:{total_sessions}sessions_{later_sessions}later")

    # Rule 2b: TOOL_RECURRING — same tool failing in later sessions → confirmed
    if tool_later >= RECUR_SESSIONS:
        return (1, f"tool_recurring:{tool_later}sessions_same_tool")

    # Rule 3: ORPHAN — tool vanished completely → likely false alarm
    if age_days >= ORPHAN_DAYS and tool_vanished:
        return (-1, f"orphan_false_alarm:tool_vanished_{age_days:.0f}d")

    # No rule matched — leave unverified
    return (0, "")


def _mark_verified(
    conn: sqlite3.Connection, belief_id: int, was_correct: int, reason: str
) -> None:
    """Update a belief trace as verified."""
    conn.execute(
        "UPDATE belief_traces SET was_correct = ?, "
        "recommended_action = CASE "
        "  WHEN recommended_action IS NULL OR recommended_action = '' THEN ? "
        "  ELSE recommended_action || ' [guardian: ' || ? || ']' "
        "END "
        "WHERE id = ?",
        (was_correct, f"guardian_verified:{reason}",
         reason[:80], belief_id),
    )
    conn.commit()


def run_pulse(db_path: Path | None = None, verbose: bool = True) -> dict:
    """One-shot verification check. Returns summary dict."""
    if db_path is None:
        db_path = DB_PATH

    if not db_path.exists():
        if verbose:
            print("[guardian] No database found. Skipping pulse.")
        return {"status": "no_db", "verified": 0, "unverified": 0}

    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")

    try:
        beliefs = _get_unverified_beliefs(conn)
        if not beliefs:
            if verbose:
                print("[guardian] pulse: all beliefs verified ✓")
            return {"status": "clean", "verified": 0, "unverified": 0}

        verified_count = 0
        false_alarm_count = 0
        still_unverified = 0

        for belief in beliefs:
            was_correct, reason = _verify_one(conn, belief)

            if was_correct == 1:
                _mark_verified(conn, belief["id"], 1, reason)
                verified_count += 1
                if verbose:
                    print(f"[guardian] ✓ {belief['belief_type']} "
                          f"(id={belief['id']}, session={belief['session_id'][:25]}...) "
                          f"— {reason}")
            elif was_correct == -1:
                _mark_verified(conn, belief["id"], -1, reason)
                false_alarm_count += 1
                if verbose:
                    print(f"[guardian] ✗ FALSE ALARM {belief['belief_type']} "
                          f"(id={belief['id']}) — {reason}")
            else:
                still_unverified += 1

        if verbose:
            print(f"[guardian] pulse done: {verified_count} verified, "
                  f"{false_alarm_count} false alarms, "
                  f"{still_unverified} still unverified")

        return {
            "status": "ok",
            "verified": verified_count,
            "false_alarms": false_alarm_count,
            "unverified": still_unverified,
            "total": len(beliefs),
        }
    finally:
        conn.close()


def run_daemon(db_path: Path | None = None, interval: int = DEFAULT_INTERVAL):
    """Continuous polling daemon. Runs until SIGTERM/SIGINT."""
    if db_path is None:
        db_path = DB_PATH

    print(f"[guardian] daemon started (interval={interval}s, db={db_path})")
    print(f"[guardian] pid={__import__('os').getpid()}")

    running = True

    def _shutdown(signum, frame):
        nonlocal running
        print(f"\n[guardian] received signal {signum}, shutting down...")
        running = False

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    pulse_count = 0
    total_verified = 0

    while running:
        pulse_count += 1
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if not db_path.exists():
            print(f"[guardian] [{timestamp}] db not found, waiting...")
            time.sleep(interval)
            continue

        result = run_pulse(db_path, verbose=False)
        total_verified += result.get("verified", 0)

        if result.get("total", 0) > 0 or pulse_count % 10 == 0:
            # Print summary every 10 pulses or when work was done
            print(f"[guardian] [{timestamp}] pulse #{pulse_count}: "
                  f"verified={result.get('verified', 0)}, "
                  f"false_alarms={result.get('false_alarms', 0)}, "
                  f"unverified={result.get('unverified', 0)}, "
                  f"total_verified={total_verified}")

        time.sleep(interval)

    print(f"[guardian] daemon stopped. {pulse_count} pulses, "
          f"{total_verified} total verifications.")


def run_status(db_path: Path | None = None):
    """Print guardian health and verification statistics."""
    if db_path is None:
        db_path = DB_PATH

    if not db_path.exists():
        print("[guardian] No database found.")
        return

    conn = sqlite3.connect(str(db_path), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")

    try:
        # Overall stats
        cur = conn.execute(
            "SELECT was_correct, COUNT(*) FROM belief_traces GROUP BY was_correct"
        )
        stats = {row[0]: row[1] for row in cur.fetchall()}
        total = sum(stats.values())
        verified = stats.get(1, 0)
        false_alarms = stats.get(-1, 0)
        unverified = stats.get(0, 0)

        print("=== Guardian Status ===")
        print(f"  DB path: {db_path}")
        print(f"  Belief traces: {total} total")
        print(f"    Verified correct: {verified} ({verified/total*100:.0f}%)" if total else "")
        print(f"    False alarms:     {false_alarms} ({false_alarms/total*100:.0f}%)" if total else "")
        print(f"    Unverified:       {unverified} ({unverified/total*100:.0f}%)" if total else "")
        print()

        # Unverified breakdown
        cur = conn.execute(
            "SELECT belief_type, COUNT(*) FROM belief_traces "
            "WHERE was_correct = 0 GROUP BY belief_type"
        )
        unverified_rows = cur.fetchall()
        if unverified_rows:
            print("  Unverified breakdown:")
            for btype, cnt in unverified_rows:
                print(f"    {btype}: {cnt}")

        # Recent guardian activity
        cur = conn.execute(
            "SELECT COUNT(*) FROM belief_traces "
            "WHERE recommended_action LIKE '%guardian:%'"
        )
        guardian_marked = cur.fetchone()[0] or 0
        print(f"\n  Guardian-marked: {guardian_marked}")

    finally:
        conn.close()


# ── CLI ─────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Harness Guardian — independent verification daemon",
    )
    sub = parser.add_subparsers(dest="command")

    daemon_p = sub.add_parser("daemon", help="Continuous polling daemon")
    daemon_p.add_argument(
        "--interval", type=int, default=DEFAULT_INTERVAL,
        help=f"Polling interval in seconds (default: {DEFAULT_INTERVAL})",
    )
    daemon_p.add_argument(
        "--db", type=str, default=None,
        help="Path to state.db",
    )

    sub.add_parser("pulse", help="One-shot verification check")
    sub.add_parser("status", help="Print verification statistics")

    args = parser.parse_args()

    db_path = Path(getattr(args, "db", None) or DB_PATH)

    if args.command == "daemon":
        run_daemon(db_path, args.interval)
    elif args.command == "pulse":
        run_pulse(db_path)
    elif args.command == "status":
        run_status(db_path)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
