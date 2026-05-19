#!/usr/bin/env python3
"""Harness daemon — Claude Code self-evolving harness framework.

Usage:
  python harness_daemon.py observe   # StopSession hook: analyze → refine → queue skills
  python harness_daemon.py inject    # StartSession hook: notify reviews + inject fragments
  python harness_daemon.py review    # List/approve/reject pending skill reviews
"""

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

import yaml

HARNESS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HARNESS_DIR))


def load_config(config_path):
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ─── observe ──────────────────────────────────────────────────────────

def cmd_observe():
    """Phase 1-2: Analyze latest session transcript, save observation, invoke refiner."""
    config_path = HARNESS_DIR / "harness_config.yaml"
    config = load_config(config_path)

    transcript_dir = Path(config["harness"]["transcript_dir"])
    transcript_file = config["harness"]["transcript_file"]
    transcript_path = transcript_dir / transcript_file

    db_path = Path(config["harness"]["db_path"])

    from observer import analyze_session
    from indexer import HarnessDB

    report = analyze_session(transcript_path, config_path)
    if report is None:
        print("[harness] No transcript found or nothing to analyze.")
        return

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

    # Phase 2: Refiner — generate skill files to review queue (harness/skills/)
    if report.action in ("patch_skill", "create_skill"):
        print("[harness] Actionable observation — invoking refiner...")
        config = load_config(config_path)
        if config.get("refiner", {}).get("enabled", False):
            from refiner import refine
            skill_path = refine(report, session_content, config_path)
            if skill_path:
                print(f"[harness] Skill queued for review: {skill_path}")
                print("[harness] Run 'python harness_daemon.py review' to approve/reject.")
    elif report.action == "update_preference":
        print("[harness] Preference detected — generating memory update...")
        from refiner import generate_preference
        pref = generate_preference(report, session_content)
        if pref:
            print(f"[harness] Preference: {pref}")

    db.close()


# ─── inject ───────────────────────────────────────────────────────────

def _list_pending_skills(skills_dir: Path) -> list:
    """Return list of pending skill files waiting for review."""
    if not skills_dir.exists():
        return []
    return sorted(skills_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)


def _search_fragments(db, query: str, max_results: int = 3, min_confidence: float = 0.6):
    """Search the fragments table for matching entries."""
    try:
        cur = db._conn.execute(
            """SELECT tag, content, confidence, hit_count
               FROM fragments
               WHERE confidence >= ?
               ORDER BY hit_count DESC, last_hit DESC
               LIMIT ?""",
            (min_confidence, max_results * 2),
        )
        rows = cur.fetchall()
        if not rows:
            return []
        # Simple keyword match filtering
        results = []
        query_lower = query.lower()
        for row in rows:
            tag, content, conf, hits = row
            if any(kw in content.lower() for kw in query_lower.split()[:5]):
                results.append({"tag": tag, "content": content[:300], "confidence": conf})
            if len(results) >= max_results:
                break
        return results
    except Exception:
        return []


def cmd_inject():
    """Phase 3: Inject relevant context at session start.

    Outputs structured text that Claude Code reads as context:
      - Pending skill reviews (if any)
      - Matching prompt fragments from past sessions
    """
    config_path = HARNESS_DIR / "harness_config.yaml"
    config = load_config(config_path)
    injector_cfg = config.get("injector", {})

    if not injector_cfg.get("enabled", False):
        print("[harness] injector disabled.")
        return

    db_path = Path(config["harness"]["db_path"])
    skills_dir = HARNESS_DIR / "skills"

    lines = []

    # 1. Pending reviews notification
    pending = _list_pending_skills(skills_dir)
    if pending:
        lines.append("## ⚠️ 待审查的技能更新")
        lines.append("")
        for i, skill_path in enumerate(pending, 1):
            name = skill_path.stem
            mtime = datetime.fromtimestamp(skill_path.stat().st_mtime)
            lines.append(f"{i}. **{name}** ({mtime.strftime('%m-%d %H:%M')})")
            lines.append(f"   审查: `python D:\\Claude\\harness\\harness_daemon.py review`")
        lines.append("")

    # 2. Matching fragments from past experience
    from indexer import HarnessDB
    db = HarnessDB(db_path)

    # Try to get current task context from environment or recent observations
    recent = db.get_recent_observations(3)
    if recent:
        query = " ".join(
            tag for obs in recent for tag in obs.get("tags", []) if tag != "general"
        )
        if query:
            fragments = _search_fragments(
                db,
                query,
                max_results=injector_cfg.get("max_fragments", 3),
                min_confidence=injector_cfg.get("min_confidence", 0.6),
            )
            if fragments:
                lines.append("## 🧠 相关记忆片段")
                lines.append("")
                for f in fragments:
                    lines.append(f"- [{f['tag']}] (置信度: {f['confidence']:.0%}) {f['content']}")
                lines.append("")

    db.close()

    if lines:
        print("\n".join(lines))
    else:
        print("[harness] injector: no context to inject.")


# ─── review ───────────────────────────────────────────────────────────

def cmd_review(cli_args=None):
    """Interactive review of pending skill files.

    Usage:
      python harness_daemon.py review              # List pending skills
      python harness_daemon.py review --approve 1  # Approve skill #1 → activate
      python harness_daemon.py review --reject 1   # Reject skill #1 → archive
      python harness_daemon.py review --show 1     # Show full content of skill #1
    """
    import argparse as ap

    parser = ap.ArgumentParser(description="Review pending harness skills")
    parser.add_argument("--approve", type=int, help="Approve skill by number and activate it")
    parser.add_argument("--reject", type=int, help="Reject skill by number and archive it")
    parser.add_argument("--show", type=int, help="Show full content of skill by number")
    args = parser.parse_args(cli_args or [])

    skills_dir = HARNESS_DIR / "skills"
    config_path = HARNESS_DIR / "harness_config.yaml"
    config = load_config(config_path)

    pending = _list_pending_skills(skills_dir)

    if not pending:
        print("[harness] No pending skill reviews.")
        return

    # --show: display skill content
    if args.show:
        idx = args.show - 1
        if 0 <= idx < len(pending):
            content = pending[idx].read_text(encoding="utf-8")
            print(f"=== {pending[idx].name} ===\n")
            print(content)
        else:
            print(f"[harness] Invalid number. Choose 1-{len(pending)}.")
        return

    # --approve: move skill to active directory
    if args.approve:
        idx = args.approve - 1
        if 0 <= idx < len(pending):
            skill_path = pending[idx]
            active_dir = Path.home() / ".claude" / "skills" / "harness"
            active_dir.mkdir(parents=True, exist_ok=True)
            dest = active_dir / skill_path.name
            shutil.copy2(skill_path, dest)
            skill_path.unlink()  # Remove from review queue
            print(f"[harness] ✅ Approved: {skill_path.name}")
            print(f"[harness]    Activated: {dest}")
        else:
            print(f"[harness] Invalid number. Choose 1-{len(pending)}.")
        return

    # --reject: archive the skill
    if args.reject:
        idx = args.reject - 1
        if 0 <= idx < len(pending):
            skill_path = pending[idx]
            archive_dir = HARNESS_DIR / "skills" / "archive"
            archive_dir.mkdir(parents=True, exist_ok=True)
            dest = archive_dir / skill_path.name
            shutil.move(str(skill_path), str(dest))
            print(f"[harness] ❌ Rejected: {skill_path.name} → archive/")
        else:
            print(f"[harness] Invalid number. Choose 1-{len(pending)}.")
        return

    # Default: list pending skills
    print(f"\n{'='*60}")
    print(f"  待审查技能队列 — {len(pending)} 个待处理")
    print(f"{'='*60}\n")
    for i, skill_path in enumerate(pending, 1):
        content = skill_path.read_text(encoding="utf-8")
        # Extract frontmatter fields
        name = skill_path.stem
        desc = ""
        tags = ""
        for line in content.split("\n"):
            if line.startswith("description:"):
                desc = line.replace("description:", "").strip()
            if line.startswith("tags:"):
                tags = line.replace("tags:", "").strip()
        mtime = datetime.fromtimestamp(skill_path.stat().st_mtime)
        print(f"  [{i}] {name}")
        print(f"      描述: {desc[:80]}")
        print(f"      标签: {tags}")
        print(f"      时间: {mtime.strftime('%Y-%m-%d %H:%M')}")
        print()
    print(f"  操作:")
    print(f"    python harness_daemon.py review --show N    # 查看全文")
    print(f"    python harness_daemon.py review --approve N # 批准 → 激活到 .claude/skills/harness/")
    print(f"    python harness_daemon.py review --reject N  # 拒绝 → 归档")
    print()


# ─── main ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Harness daemon")
    parser.add_argument(
        "command",
        choices=["observe", "inject", "review"],
        nargs="?",
        default=None,
    )
    # Pass remaining args to review subcommand
    args, remaining = parser.parse_known_args()

    try:
        if args.command == "observe":
            cmd_observe()
        elif args.command == "inject":
            cmd_inject()
        elif args.command == "review":
            # Route remaining args to review handler
            cmd_review(remaining)
        else:
            parser.print_help()
    except Exception as e:
        print(f"[harness] Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
