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
from typing import Optional

import yaml


def _truncate(text: str, max_len: int = 60) -> str:
    """Truncate text cleanly at word boundary, append '...' if cut."""
    text = text.strip()
    if len(text) <= max_len:
        return text
    cut = text[:max_len].rstrip()
    return cut + "..."

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


def _parse_skill_frontmatter(filepath: Path) -> dict:
    """Extract name, description, triggers from a SKILL.md frontmatter."""
    info = {"name": filepath.stem, "description": "", "triggers": []}
    try:
        content = filepath.read_text(encoding="utf-8")
        in_frontmatter = False
        for line in content.split("\n"):
            stripped = line.strip()
            if stripped == "---":
                if not in_frontmatter:
                    in_frontmatter = True
                    continue
                else:
                    break
            if in_frontmatter:
                if stripped.startswith("name:"):
                    val = stripped.removeprefix("name:").strip()
                    if val:
                        info["name"] = val
                elif stripped.startswith("description:"):
                    info["description"] = stripped.removeprefix("description:").strip()
                elif stripped.startswith("triggers:"):
                    continue  # list handled below
                elif stripped.startswith("  - "):
                    info["triggers"].append(stripped.removeprefix("  - ").strip())
    except Exception:
        pass
    return info


def _list_active_skills() -> list[dict]:
    """Return list of approved/active skills from ~/.claude/skills/ (harness_*.md)."""
    active_dir = Path.home() / ".claude" / "skills"
    if not active_dir.exists():
        return []
    skills = []
    for skill_file in sorted(active_dir.glob("harness_*.md")):
        info = _parse_skill_frontmatter(skill_file)
        skills.append(info)
    return skills


def _update_claude_md_skill_index(claude_md_path: Path):
    """Rebuild the skill index table in CLAUDE.md from approved skills."""
    active = _list_active_skills()
    header_lines = []
    table_lines = []
    table_lines.append("| 触发条件 | 技能名 |")
    table_lines.append("|----------|--------|")
    for s in active:
        trigger = s["description"][:80] or (s["triggers"][0][:80] if s["triggers"] else s["name"])
        table_lines.append(f"| {trigger} | `harness:{s['name']}` |")

    if not claude_md_path.exists():
        return

    content = claude_md_path.read_text(encoding="utf-8")
    marker_start = "## 活跃技能索引"
    marker_end = "## 待审查技能"
    if marker_start in content and marker_end in content:
        before = content.split(marker_start)[0]
        after_part = content.split(marker_end)[1] if marker_end in content else ""
        after = marker_end + after_part
        new_content = before + marker_start + "\n\n" + "\n".join(table_lines) + "\n\n" + after
        claude_md_path.write_text(new_content, encoding="utf-8")


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


def _read_last_user_message() -> Optional[str]:
    """Read the most recent user message from history.jsonl or latest_session."""
    history_path = Path.home() / ".claude" / "history.jsonl"
    if history_path.exists():
        try:
            with open(history_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if lines:
                last = json.loads(lines[-1].strip())
                return last.get("display", "")[:300]
        except Exception:
            pass
    # Fallback: read from latest_session.jsonl
    session_path = HARNESS_DIR / "latest_session.jsonl"
    if session_path.exists():
        try:
            with open(session_path, "r", encoding="utf-8") as f:
                for line in f:
                    entry = json.loads(line.strip())
                    if entry.get("role") == "user":
                        return entry.get("content", "")[:300]
        except Exception:
            pass
    return None


def _match_triggers(skills: list[dict], user_message: str) -> list[dict]:
    """Match user message against skill trigger patterns. Return matched skills."""
    matched = []
    msg_lower = user_message.lower()
    for s in skills:
        triggers = s.get("triggers", [])
        if not triggers:
            continue
        hits = [t for t in triggers if t.lower() in msg_lower or any(
            kw in msg_lower for kw in t.lower().split()
        )]
        if hits:
            s["matched_triggers"] = hits
            matched.append(s)
    return matched[:3]


def cmd_inject():
    """Phase 3: Inject minimal context at session start.

    Outputs structured text that Claude Code reads as context:
      - Active skills index (name + one-line trigger) — lightweight reference
      - Pending skill reviews (if any) — compact format
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

    # 1. Active skills — name + one-line trigger only, no full content
    active = _list_active_skills()
    if active:
        lines.append("## 活跃 Harness 技能")
        lines.append("")
        for s in active:
            trigger = _truncate(s["description"], 60) or (
                _truncate(s["triggers"][0], 60) if s["triggers"] else "—"
            )
            lines.append(f"- `harness:{s['name']}` — {trigger}")
        lines.append("")

    # 2. Pending reviews — compact format
    pending = _list_pending_skills(skills_dir)
    if pending:
        lines.append(f"## 待审查技能 ({len(pending)})")
        lines.append("")
        for i, skill_path in enumerate(pending, 1):
            info = _parse_skill_frontmatter(skill_path)
            trigger = _truncate(info["description"], 50) or info["name"]
            lines.append(f"{i}. `{skill_path.stem}` — {trigger} → `review --show {i}`")
        lines.append("")

    # 3. Trigger matching — suggest skills for current session topic
    current_topic = _read_last_user_message()
    if current_topic and active:
        matched = _match_triggers(active, current_topic)
        if matched:
            lines.append("## 检测到相关技能")
            lines.append("")
            for m in matched:
                lines.append(f"- `harness:{m['name']}` — {m['description'][:80]}")
                lines.append(f"  触发: {', '.join(m['matched_triggers'][:3])}")
            lines.append("")

    # 4. Matching fragments from past experience
    from indexer import HarnessDB
    db = HarnessDB(db_path)

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
                lines.append("## 相关记忆片段")
                lines.append("")
                for f in fragments:
                    lines.append(f"- [{f['tag']}] (置信度: {f['confidence']:.0%}) {f['content']}")
                lines.append("")

    db.close()

    if lines:
        print("\n".join(lines))
    else:
        print("[harness] injector: no context to inject.")


# ─── cleanup ──────────────────────────────────────────────────────────

def _load_active_wrapper_pids(config):
    """Return dict of {label: {wrapper_pid, child_pid, start_time}} from PID files."""
    wrapper_cfg = config.get("mcp_wrapper", {})
    if not wrapper_cfg.get("enabled", False):
        return {}
    pid_dir = Path(wrapper_cfg.get("pid_dir", ""))
    if not pid_dir or not pid_dir.exists():
        return {}
    active = {}
    for pid_file in pid_dir.glob("*.json"):
        try:
            data = json.loads(pid_file.read_text(encoding="utf-8"))
            # Verify the wrapper process is still alive
            wrapper_pid = data.get("wrapper_pid")
            if wrapper_pid:
                import os as _os
                try:
                    _os.kill(wrapper_pid, 0)  # signal 0 = existence check
                except OSError:
                    # Wrapper is dead — stale PID file, clean it up
                    pid_file.unlink(missing_ok=True)
                    continue
            active[data.get("label", pid_file.stem)] = data
        except Exception:
            pass
    return active


def cmd_cleanup():
    """Kill orphaned MCP server processes, skipping those managed by active wrappers."""
    import subprocess as sp

    config_path = HARNESS_DIR / "harness_config.yaml"
    config = load_config(config_path)

    # Load active wrapper registry
    active_wrappers = _load_active_wrapper_pids(config)
    protected_pids = set()
    for data in active_wrappers.values():
        if data.get("wrapper_pid"):
            protected_pids.add(data["wrapper_pid"])
        if data.get("child_pid"):
            protected_pids.add(data["child_pid"])

    if active_wrappers:
        labels = ", ".join(active_wrappers.keys())
        print(f"[harness] Active wrappers: {labels} — protected PIDs: {protected_pids}")

    mcp_patterns = [
        r"local_deep_research\.mcp",
        r"browser_use\.mcp",
        r"world-news-api",
        r"server-memory",
    ]
    killed = []
    skipped = []

    for pattern in mcp_patterns:
        try:
            result = sp.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f"Get-WmiObject Win32_Process -Filter \"name='python.exe'\" | "
                    f"Where-Object {{ $_.CommandLine -match '{pattern}' }} | "
                    f"ForEach-Object {{ $_.ProcessId }}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            pids = [int(p.strip()) for p in result.stdout.strip().split() if p.strip().isdigit()]
            for pid in pids:
                if pid in protected_pids:
                    skipped.append((pattern, pid))
                else:
                    try:
                        sp.run(
                            ["taskkill", "/F", "/PID", str(pid)],
                            capture_output=True,
                            timeout=5,
                        )
                        killed.append((pattern, pid))
                    except Exception:
                        pass
        except Exception:
            pass

    # Also check for node MCP processes
    try:
        result = sp.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-WmiObject Win32_Process -Filter \"name='node.exe'\" | "
                "Where-Object { $_.CommandLine -match 'server-memory|world-news-api' } | "
                "ForEach-Object { $_.ProcessId }",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        pids = [int(p.strip()) for p in result.stdout.strip().split() if p.strip().isdigit()]
        for pid in pids:
            if pid in protected_pids:
                skipped.append(("node-mcp", pid))
            else:
                try:
                    sp.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=5)
                    killed.append(("node-mcp", pid))
                except Exception:
                    pass
    except Exception:
        pass

    if killed:
        print(f"[harness] Cleanup killed {len(killed)} orphan(s): "
              f"{', '.join(f'{p} (PID {i})' for p, i in killed)}")
    if skipped:
        print(f"[harness] Cleanup skipped {len(skipped)} active process(es): "
              f"{', '.join(f'{p} (PID {i})' for p, i in skipped)}")
    if not killed and not skipped:
        print("[harness] Cleanup: no MCP processes found")

    # Clean up stale PID files (wrappers that died without atexit cleanup)
    wrapper_cfg = config.get("mcp_wrapper", {})
    pid_dir = Path(wrapper_cfg.get("pid_dir", ""))
    if pid_dir and pid_dir.exists():
        for pid_file in pid_dir.glob("*.json"):
            try:
                data = json.loads(pid_file.read_text(encoding="utf-8"))
                wp = data.get("wrapper_pid")
                if wp:
                    import os as _os
                    try:
                        _os.kill(wp, 0)
                    except OSError:
                        pid_file.unlink(missing_ok=True)
            except Exception:
                pass


# ─── status ───────────────────────────────────────────────────────────

def cmd_status():
    """Print unified harness health report."""
    import subprocess as sp

    config_path = HARNESS_DIR / "harness_config.yaml"
    config = load_config(config_path)

    print(f"\n{'='*60}")
    print(f"  Harness Health Report — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    # 1. Config
    print("── Config ──")
    print(f"  Transcript dir : {config['harness']['transcript_dir']}")
    print(f"  DB path         : {config['harness']['db_path']}")
    print(f"  MCP bridge      : {config['harness'].get('mcp_bridge', False)}")
    print(f"  Refiner         : {'ON' if config.get('refiner', {}).get('enabled') else 'OFF'}")
    print(f"  Injector        : {'ON' if config.get('injector', {}).get('enabled') else 'OFF'}")
    wrapper_cfg = config.get("mcp_wrapper", {})
    print(f"  MCP Wrapper     : {'ON' if wrapper_cfg.get('enabled') else 'OFF'}")
    if wrapper_cfg.get("pid_dir"):
        print(f"  PID dir         : {wrapper_cfg['pid_dir']}")

    # 2. Wrapper status
    print("\n── MCP Wrappers ──")
    active_wrappers = _load_active_wrapper_pids(config)
    if active_wrappers:
        for label, data in active_wrappers.items():
            print(f"  ✅ {label}")
            print(f"     wrapper PID {data['wrapper_pid']}  |  child PID {data['child_pid']}  |  "
                  f"started {data.get('start_time', '?')[:19]}")
    else:
        print("  (no active wrappers)")

    # Check for stale PID files
    wrapper_cfg = config.get("mcp_wrapper", {})
    pid_dir = Path(wrapper_cfg.get("pid_dir", ""))
    stale = []
    if pid_dir and pid_dir.exists():
        for pf in pid_dir.glob("*.json"):
            if pf.stem not in active_wrappers:
                stale.append(pf.stem)
    if stale:
        print(f"\n  ⚠️  Stale PID files: {', '.join(stale)}")

    # 3. Orphaned MCP processes
    print("\n── Orphan Detection ──")
    protected_pids = set()
    for data in active_wrappers.values():
        if data.get("wrapper_pid"):
            protected_pids.add(data["wrapper_pid"])
        if data.get("child_pid"):
            protected_pids.add(data["child_pid"])

    mcp_patterns = [
        r"local_deep_research\.mcp",
        r"browser_use\.mcp",
        r"world-news-api",
        r"server-memory",
    ]
    orphans = []
    for pattern in mcp_patterns:
        try:
            result = sp.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f"Get-WmiObject Win32_Process -Filter \"name='python.exe'\" | "
                    f"Where-Object {{ $_.CommandLine -match '{pattern}' }} | "
                    f"ForEach-Object {{ $_.ProcessId }}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            pids = [int(p.strip()) for p in result.stdout.strip().split() if p.strip().isdigit()]
            for pid in pids:
                if pid not in protected_pids:
                    orphans.append((pattern, pid))
        except Exception:
            pass
    try:
        result = sp.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-WmiObject Win32_Process -Filter \"name='node.exe'\" | "
                "Where-Object { $_.CommandLine -match 'server-memory|world-news-api' } | "
                "ForEach-Object { $_.ProcessId }",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        pids = [int(p.strip()) for p in result.stdout.strip().split() if p.strip().isdigit()]
        for pid in pids:
            if pid not in protected_pids:
                orphans.append(("node-mcp", pid))
    except Exception:
        pass

    if orphans:
        print(f"  🔴 {len(orphans)} orphan(s) detected:")
        for pattern, pid in orphans:
            print(f"     {pattern} (PID {pid})")
    else:
        print("  🟢 No orphaned MCP processes")

    # 4. Harness DB stats
    print("\n── Harness DB ──")
    db_path = Path(config["harness"]["db_path"])
    if db_path.exists():
        from indexer import HarnessDB
        db = HarnessDB(db_path)
        try:
            observations = db.get_recent_observations(100)
            print(f"  Total observations : {len(observations)} (recent 100)")
            if observations:
                actions = {}
                for obs in observations:
                    a = obs.get("action", "unknown")
                    actions[a] = actions.get(a, 0) + 1
                for a, c in sorted(actions.items(), key=lambda x: -x[1]):
                    print(f"    {a}: {c}")

            cur = db._conn.execute("SELECT COUNT(*) FROM fragments")
            frag_count = cur.fetchone()[0]
            print(f"  Total fragments    : {frag_count}")
        except Exception as e:
            print(f"  DB query error: {e}")
        db.close()
    else:
        print("  No database found")

    # 5. Pending reviews
    print("\n── Pending Reviews ──")
    skills_dir = HARNESS_DIR / "skills"
    pending = _list_pending_skills(skills_dir)
    if pending:
        print(f"  {len(pending)} skill(s) waiting:")
        for spath in pending[:5]:
            print(f"    - {spath.name}")
    else:
        print("  (none)")

    print(f"\n{'='*60}\n")


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

    # --approve: move skill to active directory, update CLAUDE.md
    if args.approve:
        idx = args.approve - 1
        if 0 <= idx < len(pending):
            skill_path = pending[idx]
            active_dir = Path.home() / ".claude" / "skills"
            active_dir.mkdir(parents=True, exist_ok=True)
            dest = active_dir / skill_path.name  # harness_<type>_<name>.md
            shutil.copy2(skill_path, dest)
            skill_path.unlink()  # Remove from review queue
            print(f"[harness] ✅ Approved: {skill_path.name}")
            print(f"[harness]    Activated: {dest}")

            # Update CLAUDE.md skill index
            for candidate in [
                Path("D:/Claude/CLAUDE.md"),
                Path("D:/Claude/.claude/CLAUDE.md"),
            ]:
                if candidate.exists():
                    _update_claude_md_skill_index(candidate)
                    print(f"[harness]    Updated: {candidate}")
                    break
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
        qs = ""
        stype = ""
        for line in content.split("\n"):
            if line.startswith("description:"):
                desc = line.replace("description:", "").strip()
            if line.startswith("tags:"):
                tags = line.replace("tags:", "").strip()
            if line.startswith("harness_confidence:"):
                qs = line.replace("harness_confidence:", "").strip()
            if line.startswith("skill_type:"):
                stype = line.replace("skill_type:", "").strip()
        mtime = datetime.fromtimestamp(skill_path.stat().st_mtime)
        type_tag = f"[{stype}]" if stype else ""
        qs_tag = f"qs={qs}" if qs else ""
        print(f"  [{i}] {name} {type_tag} {qs_tag}")
        print(f"      描述: {desc[:80]}")
        print(f"      标签: {tags}")
        print(f"      时间: {mtime.strftime('%Y-%m-%d %H:%M')}")
        print()
    print(f"  操作:")
    print(f"    python harness_daemon.py review --show N    # 查看全文")
    print(f"    python harness_daemon.py review --approve N # 批准 → 激活到 .claude/skills/")
    print(f"    python harness_daemon.py review --reject N  # 拒绝 → 归档")
    print()


# ─── main ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Harness daemon")
    parser.add_argument(
        "command",
        choices=["observe", "inject", "review", "cleanup", "status"],
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
        elif args.command == "cleanup":
            cmd_cleanup()
        elif args.command == "status":
            cmd_status()
        else:
            parser.print_help()
    except Exception as e:
        print(f"[harness] Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
