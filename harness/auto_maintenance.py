"""Auto-maintenance — three automatic actions that close the feedback loop.

Runs at session end (cmd_observe). Pure local logic, zero LLM, zero token.

Actions:
  1. Auto-archive idle skills: >14 days unused → move to archive/
  2. Auto-approve low-risk skills: pending >3 days + sandbox risk "low" → deploy
  3. Auto-degrade low-effectiveness: confidence <0.3 → back to review queue
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from pathlib import Path

HARNESS_DIR = Path(__file__).resolve().parent
ACTIVE_SKILLS_DIR = Path.home() / ".claude" / "skills"
PENDING_DIR = HARNESS_DIR / "skills"
ARCHIVE_DIR = PENDING_DIR / "archive"
CLAUDE_MD_PATHS = [
    Path("D:/Claude/CLAUDE.md"),
    Path("D:/Claude/.claude/CLAUDE.md"),
]

IDLE_ARCHIVE_DAYS = 14
AUTO_APPROVE_PENDING_DAYS = 3
LOW_CONFIDENCE_THRESHOLD = 0.3


# ── Main entry point ─────────────────────────────────────────────────────

def run(db_path: Path):
    """Run all three auto-maintenance actions. Each independent, non-fatal.

    Called at end of cmd_observe(), after health check and SelfModel update.
    """
    conn = sqlite3.connect(str(db_path), timeout=2)
    conn.execute("PRAGMA journal_mode=WAL")

    archived = _auto_archive_idle(conn)
    approved = _auto_approve_low_risk(conn, db_path)
    degraded = _auto_degrade_low_confidence(conn)
    cleaned = _cleanup_stale_entries(conn)

    conn.close()

    # Update CLAUDE.md if anything changed
    if archived or approved or degraded or cleaned:
        _rebuild_claude_md_index()

    # Print summary
    if archived:
        print(f"[harness] Auto-archived {len(archived)} idle skill(s): "
              f"{', '.join(archived)}")
    if approved:
        print(f"[harness] Auto-approved {len(approved)} low-risk skill(s): "
              f"{', '.join(approved)}")
    if degraded:
        print(f"[harness] Auto-degraded {len(degraded)} low-confidence skill(s): "
              f"{', '.join(degraded)}")
    if cleaned:
        print(f"[harness] Cleaned {len(cleaned)} stale DB entries: "
              f"{', '.join(cleaned)}")


# ── Action 1: Auto-archive idle skills ──────────────────────────────────

def _auto_archive_idle(conn: sqlite3.Connection) -> list[str]:
    """Archive active skills unused for >IDLE_ARCHIVE_DAYS days.

    Moves from ~/.claude/skills/ to harness/skills/archive/.
    Principle: 知识不养闲人 — skills that prove useless degrade then archive.
    """
    archived: list[str] = []
    now = time.time()
    threshold = now - IDLE_ARCHIVE_DAYS * 86400

    cur = conn.execute(
        "SELECT name, last_used, created_at, usage_count FROM skill_index"
    )
    rows = cur.fetchall()

    for name, last_used, created_at, usage_count in rows:
        # Determine if idle
        is_idle = False
        if last_used and last_used < threshold:
            is_idle = True
        elif not last_used and created_at and created_at < threshold and (usage_count or 0) == 0:
            is_idle = True

        if not is_idle:
            continue

        # Find the active skill file
        skill_file = _find_skill_file(name, ACTIVE_SKILLS_DIR)
        if skill_file is None:
            continue

        # Move to archive
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        dest = ARCHIVE_DIR / skill_file.name
        shutil.move(str(skill_file), str(dest))

        # Update DB: set confidence low, log
        conn.execute(
            "UPDATE skill_index SET harness_confidence = 0.1, updated_at = unixepoch() "
            "WHERE name = ?",
            (name,),
        )
        conn.execute(
            "INSERT INTO evolution_log (skill_name, action, change_summary, timestamp) "
            "VALUES (?, 'auto_archive', ?, unixepoch())",
            (name, f"Auto-archived: idle >{IDLE_ARCHIVE_DAYS}d"),
        )
        conn.commit()

        archived.append(name)

    return archived


# ── Action 2: Auto-approve low-risk pending skills ───────────────────────

def _auto_approve_low_risk(conn: sqlite3.Connection, db_path: Path) -> list[str]:
    """Auto-approve pending skills that have been waiting >3 days AND
    pass sandbox verification with "low" risk.

    This breaks the human-review bottleneck for proven-safe skills.
    """
    approved: list[str] = []
    now = time.time()

    if not PENDING_DIR.exists():
        return approved

    for skill_path in sorted(PENDING_DIR.glob("harness_*.md")):
        # Check pending duration
        mtime = skill_path.stat().st_mtime
        days_pending = (now - mtime) / 86400
        if days_pending < AUTO_APPROVE_PENDING_DAYS:
            continue

        # Sandbox verification
        try:
            from sandbox_verifier import verify_skill
            sr = verify_skill(skill_path, db_path)
            if sr.risk_level != "low":
                continue
        except Exception:
            continue  # Can't verify → skip auto-approve

        # Auto-approve: copy to active dir, remove from pending
        ACTIVE_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        dest = ACTIVE_SKILLS_DIR / skill_path.name
        shutil.copy2(skill_path, dest)
        skill_path.unlink()

        # Update DB: mark as active
        name = skill_path.stem
        existing = conn.execute(
            "SELECT name FROM skill_index WHERE name = ?", (name,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE skill_index SET harness_confidence = 0.6, "
                "updated_at = unixepoch() WHERE name = ?",
                (name,),
            )
        else:
            conn.execute(
                "INSERT INTO skill_index (name, file_path, harness_confidence, "
                "created_at, updated_at) VALUES (?, ?, 0.6, unixepoch(), unixepoch())",
                (name, str(dest)),
            )

        conn.execute(
            "INSERT INTO evolution_log (skill_name, action, change_summary, timestamp) "
            "VALUES (?, 'auto_approve', ?, unixepoch())",
            (name, f"Auto-approved: pending {days_pending:.0f}d, sandbox risk={sr.risk_level}"),
        )
        conn.commit()

        approved.append(name)

    return approved


# ── Action 3: Auto-degrade low-confidence skills ────────────────────────

def _auto_degrade_low_confidence(conn: sqlite3.Connection) -> list[str]:
    """Degrade skills with harness_confidence < LOW_CONFIDENCE_THRESHOLD.

    Active skills → move back to review queue.
    Already-pending skills with very low confidence → archive.
    """
    degraded: list[str] = []
    cur = conn.execute(
        "SELECT name, harness_confidence FROM skill_index "
        "WHERE harness_confidence < ?",
        (LOW_CONFIDENCE_THRESHOLD,),
    )
    rows = cur.fetchall()

    for name, conf in rows:
        # Check if active
        active_file = _find_skill_file(name, ACTIVE_SKILLS_DIR)
        if active_file:
            # Move back to review queue
            dest = PENDING_DIR / active_file.name
            shutil.move(str(active_file), str(dest))
            conn.execute(
                "UPDATE skill_index SET harness_confidence = 0.4, updated_at = unixepoch() "
                "WHERE name = ?",
                (name,),
            )
            conn.execute(
                "INSERT INTO evolution_log (skill_name, action, change_summary, timestamp) "
                "VALUES (?, 'auto_degrade', ?, unixepoch())",
                (name, f"Degraded: confidence {conf:.2f} < {LOW_CONFIDENCE_THRESHOLD:.1f}"),
            )
            conn.commit()
            degraded.append(name)
            continue

        # Check if already pending (still <0.3 after re-review failure → archive)
        pending_file = _find_skill_file(name, PENDING_DIR)
        if pending_file:
            ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            dest = ARCHIVE_DIR / pending_file.name
            shutil.move(str(pending_file), str(dest))
            conn.execute(
                "UPDATE skill_index SET harness_confidence = 0.0, updated_at = unixepoch() "
                "WHERE name = ?",
                (name,),
            )
            conn.execute(
                "INSERT INTO evolution_log (skill_name, action, change_summary, timestamp) "
                "VALUES (?, 'auto_archive_low_conf', ?, unixepoch())",
                (name, f"Archived: pending + confidence {conf:.2f}"),
            )
            conn.commit()
            degraded.append(name)

    return degraded


# ── Action 4: Cleanup stale DB entries ──────────────────────────────────

def _cleanup_stale_entries(conn: sqlite3.Connection) -> list[str]:
    """Remove skill_index entries that have no matching .md file anywhere.

    These are orphaned records — files were deleted but DB entries remain.
    Without cleanup, SelfModel reports them as idle/degraded forever.
    """
    cleaned: list[str] = []
    cur = conn.execute("SELECT name FROM skill_index")
    db_names = [r[0] for r in cur.fetchall()]

    active = {f.stem for f in ACTIVE_SKILLS_DIR.glob("harness_*.md")} if ACTIVE_SKILLS_DIR.exists() else set()
    pending = {f.stem for f in PENDING_DIR.glob("harness_*.md")} if PENDING_DIR.exists() else set()
    all_files = active | pending

    for name in db_names:
        if name not in all_files:
            conn.execute("DELETE FROM skill_index WHERE name = ?", (name,))
            conn.execute(
                "INSERT INTO evolution_log (skill_name, action, change_summary, timestamp) "
                "VALUES (?, 'cleanup_stale', ?, unixepoch())",
                (name, "Orphaned DB entry — no matching file exists"),
            )
            cleaned.append(name)

    if cleaned:
        conn.commit()
    return cleaned


# ── Helpers ──────────────────────────────────────────────────────────────

def _find_skill_file(name: str, directory: Path) -> Path | None:
    """Find a skill .md file by name in a directory."""
    if not directory.exists():
        return None
    # Exact match
    exact = directory / f"{name}.md"
    if exact.exists():
        return exact
    # Fuzzy match (name might be stored without .md suffix)
    for f in directory.glob("*.md"):
        if f.stem == name:
            return f
    return None


def _rebuild_claude_md_index():
    """Update CLAUDE.md skill index after auto-actions."""
    try:
        from harness_daemon import _update_claude_md_skill_index
        for candidate in CLAUDE_MD_PATHS:
            if candidate.exists():
                _update_claude_md_skill_index(candidate)
    except Exception:
        pass
