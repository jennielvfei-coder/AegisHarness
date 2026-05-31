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
import sqlite3
import sys
import time
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


def _detect_conflicts(situational_model, report) -> list[dict]:
    """Detect disagreements between PreThink classification and Observer action.

    Returns list of conflict dicts with keys: type, prethink, observer, description.
    """
    if situational_model is None:
        return []

    conflicts = []
    sm = situational_model

    # Type 1: PreThink detected recurring_failure but Observer didn't act on it
    if sm.situation == "recurring_failure" and report.action in ("skip", "save_fragment"):
        conflicts.append({
            "type": "severity_downgrade",
            "prethink": sm.situation,
            "observer": report.action,
            "description": (
                f"PreThink detected recurring_failure "
                f"(conf={sm.confidence:.2f}, severity={sm.severity}), "
                f"but Observer chose '{report.action}'. "
                f"Possible false negative — consider reviewing the session."
            ),
        })

    # Type 2: PreThink detected correction but Observer skipped
    if sm.situation == "correction" and report.action == "skip":
        conflicts.append({
            "type": "missed_correction",
            "prethink": sm.situation,
            "observer": report.action,
            "description": (
                f"PreThink detected correction (conf={sm.confidence:.2f}), "
                f"but Observer chose 'skip'. "
                f"Possible missed correction signal."
            ),
        })

    # Type 3: PreThink classified as routine but Observer found a strong signal
    if sm.situation == "routine" and report.action == "create_skill":
        conflicts.append({
            "type": "unexpected_complexity",
            "prethink": sm.situation,
            "observer": report.action,
            "description": (
                "PreThink classified as 'routine' but Observer found "
                "create_skill-worthy signal. PreThink may have missed a pattern."
            ),
        })

    return conflicts



# ─── Injector output budget management ──────────────────────────────────

class InjectorOutput:
    """Manage context injection with line budget and priority.

    Each section registers with a priority (0-1, higher = more important).
    On render, sections are sorted by priority and accumulated until the
    line budget is exhausted. Low-priority sections are silently dropped.
    """

    def __init__(self, max_lines: int = 40):
        self._sections: list[tuple[float, str, list[str]]] = []  # (pri, header, lines)
        self._max_lines = max_lines

    def add(self, priority: float, header: str, lines: list[str]):
        if lines:
            self._sections.append((priority, header, lines))

    def render(self) -> str:
        self._sections.sort(key=lambda x: -x[0])  # highest priority first
        output: list[str] = []
        remaining = self._max_lines
        for pri, header, lines in self._sections:
            cost = len(lines) + (2 if header else 0)
            if remaining >= cost:
                if header:
                    output.append(header)
                    output.append("")
                output.extend(lines)
                output.append("")
                remaining -= cost
            elif remaining >= 3:
                # Low budget: emit header + first line only
                if header:
                    output.append(header + " (截断)")
                    output.append("")
                output.append(lines[0])
                output.append("")
                break
            else:
                break
        return "\n".join(output).strip()


def load_config(config_path):
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ─── observe ──────────────────────────────────────────────────────────

def cmd_observe():
    """Phase 1-2: Analyze latest session transcript, save observation, invoke refiner."""
    config_path = HARNESS_DIR / "harness_config.yaml"
    config = load_config(config_path)

    # ── Build transcript from raw Claude Code session logs ──
    import subprocess
    source_script = config["harness"].get("transcript_source", "scripts/get_last_session.py")
    subprocess.run(
        [sys.executable, str(HARNESS_DIR / source_script)],
        capture_output=True, timeout=30,
    )
    transcript_dir = Path(config["harness"]["transcript_dir"])
    transcript_file = config["harness"]["transcript_file"]
    transcript_path = transcript_dir / transcript_file

    db_path = Path(config["harness"]["db_path"])

    from observer import analyze_session, _read_transcript, _count_tool_calls, \
        _detect_tool_failures, _detect_data_quality_failures, _has_user_interruption
    from indexer import HarnessDB

    # ── PreThink: situational model before observer ──
    db = HarnessDB(db_path)
    situational_model = None
    try:
        from prethink import run_prethink
        session_data = _read_transcript(transcript_path)
        if session_data is not None:
            entries = session_data["entries"]
            content = session_data["content"]

            # Build fingerprint from transcript signals
            fingerprint = {
                "tool_count": _count_tool_calls(content),
                "failure_count": _detect_tool_failures(entries),
                "data_quality_failures": _detect_data_quality_failures(entries),
                "has_interruption": _has_user_interruption(entries),
            }

            # Add DB-derived history signals
            try:
                cur = db._conn.execute(
                    "SELECT COUNT(*) FROM tool_call_log WHERE status='error' AND timestamp > unixepoch() - 3600"
                )
                fingerprint["recent_error_count"] = (cur.fetchone() or [0])[0] or 0

                cur = db._conn.execute(
                    "SELECT action FROM observations ORDER BY processed_at DESC LIMIT 1"
                )
                row = cur.fetchone()
                fingerprint["last_action"] = row[0] if row else ""
            except Exception:
                fingerprint["recent_error_count"] = 0
                fingerprint["last_action"] = ""

            # Extract first user message
            user_msg = ""
            for e in entries:
                if e.get("role") == "user":
                    user_msg = e.get("content", "")[:500]
                    break

            situational_model = run_prethink(user_msg, fingerprint, db)
            # Store model for next session's inject phase
            from prethink import situational_model_to_dict
            db.set_meta("latest_situational_model",
                        json.dumps(situational_model_to_dict(situational_model),
                                   ensure_ascii=False))
            print(f"[harness] PreThink: {situational_model.situation} "
                  f"(severity={situational_model.severity}, "
                  f"budget={situational_model.injection_budget}, "
                  f"path={situational_model.reasoning_path})")
    except ImportError:
        pass  # pocketflow not installed
    except Exception as e:
        if config.get("injector", {}).get("verbose", False):
            print(f"[harness] PreThink error (non-fatal): {e}")

    report = analyze_session(transcript_path, config_path, situational_model)
    if report is None:
        print("[harness] No transcript found or nothing to analyze.")
        db.close()
        return

    session_content = ""
    if transcript_path.exists():
        try:
            session_content = transcript_path.read_text(encoding="utf-8")
        except Exception:
            session_content = ""

    # ── Decision trajectory: extract key turning points from transcript ──
    try:
        from observer import extract_decision_trajectory
        session_data = _read_transcript(transcript_path)
        if session_data is not None:
            trajectory = extract_decision_trajectory(session_data["entries"])
            if trajectory:
                report.decision_trajectory = trajectory
                print(f"[harness] Decision trajectory: {len(trajectory)} turning points "
                      f"({', '.join(p['type'] for p in trajectory[:5])})")
    except Exception as e:
        print(f"[harness] WARNING trajectory extraction failed: {e}", file=sys.stderr, flush=True)

    # ── Conflict detection: PreThink vs Observer ──
    conflicts = _detect_conflicts(situational_model, report)
    if conflicts:
        report.conflicts = conflicts
        print(f"\n[harness] ⚠️  {len(conflicts)} PreThink/Observer conflict(s):")
        for c in conflicts:
            print(f"  [{c['type']}] {c['description']}")

    db.save_observation(report)
    print(f"[harness] Observation saved: action={report.action}, "
          f"confidence={report.confidence:.2f}")

    # ── Distribution summary: last 20 observations ──
    try:
        from collections import Counter
        cur = db._conn.execute(
            "SELECT action FROM observations ORDER BY processed_at DESC LIMIT 20"
        )
        recent_actions = [r[0] for r in cur.fetchall()]
        if recent_actions:
            dist = Counter(recent_actions)
            dist_str = ", ".join(f"{a}={c}" for a, c in dist.most_common())
            print(f"[harness] Observer distribution (last {len(recent_actions)}): {dist_str}")
    except Exception:
        pass

    # ── News feedback learner: process today's signal_buffer entries ──
    try:
        from feedback_learner import process_feedback
        from datetime import datetime as _dt
        today = _dt.now().strftime("%Y-%m-%d")
        n = process_feedback(db, today)
        if n:
            print(f"[harness] Feedback learner: {n} entity weight(s) updated")
    except ImportError:
        pass
    except Exception as e:
        if config.get("injector", {}).get("verbose", False):
            print(f"[harness] Feedback learner error (non-fatal): {e}")

    # ── Judgment graph: extract judgments from today's daily report ──
    try:
        from judgment_graph import extract_and_store_judgments
        from datetime import datetime as _dt
        today = _dt.now().strftime("%Y-%m-%d")
        news_filepath = Path(
            r"C:\Users\Chucky\Documents\Obsidian Vault\claude专属文件夹\news"
        ) / f"{today}.md"
        if news_filepath.exists():
            n = extract_and_store_judgments(db, news_filepath, today)
            if n:
                print(f"[harness] Judgment graph: {n} judgments extracted/stored")

            # Process reflection signals (judgment revisions from user feedback)
            from judgment_graph import process_reflections
            r = process_reflections(db, today)
            if r:
                print(f"[harness] Reflection: {r} judgment(s) updated")
    except ImportError:
        pass
    except Exception as e:
        if config.get("injector", {}).get("verbose", False):
            print(f"[harness] Judgment graph error (non-fatal): {e}")

    # ── Track skill usage (feedback loop closure) ──
    if report.skills_used:
        _update_skill_usage(db, report.skills_used)
        print(f"[harness] Skill usage tracked: {report.skills_used}")

    # ── Skill effectiveness: did the failure pattern recur? ──
    _evaluate_skill_effectiveness(db, report, session_content)

    # ── Seed constraints from recurring failures ──
    if report.constraint_candidates:
        _seed_constraints(db, report.constraint_candidates, report.session_id)
        print(f"[harness] Constraints seeded: {len(report.constraint_candidates)}")

    # ── Orchestrate sub-agents ──
    if report.action in ("patch_skill", "create_skill") and report.confidence > 0.3:
        config = load_config(config_path)
        if config.get("refiner", {}).get("enabled", False):
            # Agent 1: Skill Writer (own LLM session, no observer context leakage)
            print("[harness] → Agent: skill_writer")
            from agents.skill_writer import run as skill_writer_run
            sw_result = skill_writer_run(report, session_content, config)
            if sw_result and sw_result.get("path"):
                print(f"[harness] Skill queued: {sw_result['path']}")

                # Agent 2: Fragment Extractor (own LLM session, runs after skill_writer)
                print("[harness] → Agent: fragment_extractor")
                from agents.fragment_extractor import run as fragment_extractor_run
                fragments = fragment_extractor_run(
                    session_content, report.tags,
                    sw_result.get("quality_score", report.confidence),
                    sw_result.get("skill_type", "mental-model"),
                    config,
                )
                print(f"[harness] Fragments extracted: {len(fragments)}")

            # Update observation confidence from skill_writer
            if sw_result:
                from agents.skill_writer import _update_observation_confidence
                _update_observation_confidence(report.session_id, sw_result.get("quality_score", report.confidence))

    elif report.action == "update_preference":
        print("[harness] Preference detected.")
        from refiner import generate_preference
        pref = generate_preference(report, session_content)
        if pref:
            print(f"[harness] Preference: {pref}")

    elif report.action == "save_fragment":
        print("[harness] Task-workflow detected — extracting fragments directly.")
        from agents.fragment_extractor import run as fragment_extractor_run
        config = load_config(config_path)
        fragments = fragment_extractor_run(
            session_content, report.tags, report.confidence,
            report.skill_type or "task-workflow", config,
        )
        print(f"[harness] Fragments extracted: {len(fragments)}")

    # ── Meta-ToM pipeline (offline, post-observer) ──
    mind_cfg = config.get("mind_theory", {})
    if mind_cfg.get("enabled", False):
        try:
            _run_mind_pipeline(
                db, report, transcript_path, session_content, config, config_path,
            )
        except Exception:
            import traceback
            print(f"[harness] mind pipeline error: {traceback.format_exc()}")

    # ── Health check at session end ──
    try:
        from health_probes import run_health_check, save_snapshot, check_rollback_triggers, execute_rollback
        snap = run_health_check(db_path)
        save_snapshot(snap, db_path)
        print(f"[harness] Health check: {snap.overall_status} ({len(snap.alerts)} alerts)")
        if snap.overall_status == "critical":
            print(f"[harness] HEALTH CRITICAL: {len(snap.alerts)} alert(s)")
        actions = check_rollback_triggers(snap, db_path)
        for action in actions:
            print(f"[harness] ROLLBACK TRIGGERED: {action.component_type} — {action.reason}")
            result = execute_rollback(action, db_path)
            print(f"[harness] Rollback: {result.detail}")
    except Exception as e:
        if config.get("injector", {}).get("verbose", False):
            print(f"[harness] Health probe error (non-fatal): {e}")

    # ── Update SelfModel after session processing ──
    try:
        from self_model import build_from_state, persist, snapshot
        sm = build_from_state(db_path)
        persist(sm)
        snapshot(sm)
    except Exception:
        pass

    # ── Auto-maintenance: close the feedback loop ──
    try:
        from auto_maintenance import run as run_auto_maintenance
        run_auto_maintenance(db_path)
    except Exception as e:
        if config.get("injector", {}).get("verbose", False):
            print(f"[harness] Auto-maintenance error (non-fatal): {e}")

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


def _search_fragments(db, query: str, max_results: int = 3, min_confidence: float = 0.6,
                      fragment_type: str = None):
    """Search the fragments table for matching entries."""
    try:
        if fragment_type:
            cur = db._conn.execute(
                """SELECT tag, content, confidence, hit_count
                   FROM fragments
                   WHERE confidence >= ?
                     AND fragment_type = ?
                   ORDER BY hit_count DESC, last_hit DESC
                   LIMIT ?""",
                (min_confidence, fragment_type, max_results * 2),
            )
        else:
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


def _load_correction_keywords() -> tuple[list[str], list[str]]:
    """Load correction keywords from config file. Returns (strong, weak)."""
    config_path = HARNESS_DIR / "correction_keywords.yaml"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        return cfg.get("strong", []), cfg.get("weak", [])
    except Exception:
        # Fallback to hardcoded minimal set
        return (
            ["不对", "错了", "不是这样", "再想想", "反思", "撤回"],
            ["重新", "纠正"],
        )


def _link_cross_session_correction(db_path: Path, user_message: str) -> str | None:
    """Link this session's correction to the previous session and check for patterns.

    Returns a warning string if the same correction type has appeared
    >= 3 times recently — for injection into the current session.
    Returns None if no correction or no pattern detected.
    """
    strong_kw, weak_kw = _load_correction_keywords()
    is_short = len(user_message) <= 200

    matched_strong = [kw for kw in strong_kw if kw in user_message]
    matched_weak = [kw for kw in weak_kw if kw in user_message] if is_short else []
    matched = matched_strong + matched_weak

    if not matched:
        return None

    try:
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(str(db_path), timeout=2)
        conn.execute("PRAGMA journal_mode=WAL")

        cur = conn.execute(
            "SELECT id, session_id, decision_trajectory FROM observations "
            "ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
        if not row:
            conn.close()
            return None

        obs_id, prev_sid, traj_json = row
        traj = json.loads(traj_json) if traj_json else []

        seq = len(traj) + 1
        traj.append({
            "seq": seq,
            "type": "cross_session_correction",
            "description": f"下个 session 首条消息: {user_message[:120]}",
            "context": f"匹配: {', '.join(matched[:3])}",
        })

        conn.execute(
            "UPDATE observations SET decision_trajectory = ? WHERE id = ?",
            (json.dumps(traj, ensure_ascii=False), obs_id),
        )
        conn.commit()

        print(f"[harness] Cross-session: linked '{matched[0]}' to "
              f"session {prev_sid[:30]}...")

        warning = _check_correction_pattern(conn, matched)
        conn.close()
        return warning
    except Exception:
        return None


def _check_correction_pattern(conn, matched_keywords: list[str]) -> str | None:
    """Check if this correction type appears >= 3 times in recent sessions.

    Returns a warning string for injection, or None.
    """
    try:
        cur = conn.execute(
            "SELECT decision_trajectory FROM observations "
            "WHERE decision_trajectory IS NOT NULL "
            "ORDER BY id DESC LIMIT 50"
        )
        recent_kw_freq: dict[str, int] = {}
        for (traj_json,) in cur.fetchall():
            try:
                traj = json.loads(traj_json) if traj_json else []
            except Exception:
                continue
            for point in traj:
                if point.get("type") == "cross_session_correction":
                    ctx = point.get("context", "")
                    for kw in matched_keywords:
                        if kw in ctx:
                            recent_kw_freq[kw] = recent_kw_freq.get(kw, 0) + 1

        recurring = [(kw, cnt) for kw, cnt in recent_kw_freq.items() if cnt >= 3]
        if not recurring:
            return None

        top = sorted(recurring, key=lambda x: -x[1])[:3]
        pattern_desc = ", ".join(f"'{kw}'(×{cnt})" for kw, cnt in top)
        print(f"[harness] ⚠️  Cross-session pattern: {pattern_desc}")

        return (
            f"你在最近 session 中被同类纠正 {sum(c for _, c in top)} 次"
            f"（{pattern_desc}）。"
            f"如果本次需要改进或给方案，先确认诊断再动手。"
        )
    except Exception:
        return None


def _check_correction_pattern(conn, matched_keywords: list[str]):
    """Check if this correction type appears repeatedly across recent sessions.

    If >= 3 cross-session corrections with overlapping keywords appear
    in the last 7 days, print a diagnostic warning.
    """
    try:
        cur = conn.execute(
            "SELECT decision_trajectory, processed_at FROM observations "
            "WHERE decision_trajectory IS NOT NULL "
            "ORDER BY id DESC LIMIT 50"
        )
        recent_kw_freq: dict[str, int] = {}
        for traj_json, ts in cur.fetchall():
            try:
                traj = json.loads(traj_json) if traj_json else []
            except Exception:
                continue
            for point in traj:
                if point.get("type") == "cross_session_correction":
                    ctx = point.get("context", "")
                    for kw in matched_keywords:
                        if kw in ctx:
                            recent_kw_freq[kw] = recent_kw_freq.get(kw, 0) + 1

        recurring = [(kw, cnt) for kw, cnt in recent_kw_freq.items() if cnt >= 3]
        if recurring:
            top = sorted(recurring, key=lambda x: -x[1])[:3]
            pattern_desc = ", ".join(f"'{kw}'(×{cnt})" for kw, cnt in top)
            print(f"[harness] ⚠️  Cross-session pattern detected: {pattern_desc}")
            print(f"[harness]     This correction type is recurring. "
                  f"Consider root cause: is the same failure mode repeating?")
    except Exception:
        pass


def _search_failure_patterns(db, skill_names: list, max_results: int = 5):
    """Search for failure_pattern fragments associated with given skill names."""
    try:
        results = []
        for name in skill_names:
            cur = db._conn.execute(
                """SELECT tag, content, confidence, hit_count, skill_name
                   FROM fragments
                   WHERE fragment_type = 'failure_pattern'
                     AND (skill_name = ? OR skill_name IS NULL OR skill_name = '')
                   ORDER BY confidence DESC, hit_count DESC
                   LIMIT ?""",
                (name, max_results),
            )
            for row in cur.fetchall():
                tag, content, conf, hits, sn = row
                results.append({
                    "tag": tag,
                    "content": content[:300],
                    "confidence": conf,
                    "skill_name": sn or name,
                })
        return results[:max_results]
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

    config_path = HARNESS_DIR / "harness_config.yaml"
    config = load_config(config_path)
    injector_cfg = config.get("injector", {})

    if not injector_cfg.get("enabled", False):
        print("[harness] injector disabled.")
        return

    db_path = Path(config["harness"]["db_path"])
    skills_dir = HARNESS_DIR / "skills"
    max_lines = injector_cfg.get("max_total_lines", 40)

    # ── PreThink: load situational model from previous session's observe ──
    situational_model = None
    try:
        from indexer import HarnessDB as _HarnessDB
        pre_db = _HarnessDB(db_path)
        raw = pre_db.get_meta("latest_situational_model")
        if raw:
            from prethink import situational_model_from_dict
            situational_model = situational_model_from_dict(json.loads(raw))
            max_lines = situational_model.injection_budget
        pre_db.close()
    except Exception:
        pass

    output = InjectorOutput(max_lines=max_lines)
    ctx: dict = {"active": [], "matched": [], "situational_model": situational_model}

    # ── Health check at session start ──
    health_snapshot = None
    try:
        from health_probes import run_health_check
        health_snapshot = run_health_check(db_path)
        if health_snapshot.overall_status in ("degraded", "critical"):
            print(f"[harness] Health check: {health_snapshot.overall_status.upper()} "
                  f"({len(health_snapshot.alerts)} alerts)", file=sys.stderr, flush=True)
    except Exception:
        pass

    # ── Load constraint cache for PreToolUse hook ──
    # Only write when there ARE active constraints — destructive overwrite
    # would nuke data written by other subsystems (search_feedback, feishu_push, etc.)
    constraints = _load_active_constraints(db_path)
    # Ensure critical constraints are always seeded (idempotent)
    _ensure_critical_constraints(db_path)
    # Reload — _ensure may have added new constraints
    constraints = _load_active_constraints(db_path)
    if constraints:
        _write_constraint_cache(constraints)

    # ── Cross-session trajectory: link this session's corrections to prior session ──
    current_msg = _read_last_user_message()
    correction_warning = None
    if current_msg:
        correction_warning = _link_cross_session_correction(db_path, current_msg)

    # ── Run preflight checks FIRST so source health feeds into intent matcher ──
    preflight_checks = _run_preflight_checks(config, config_path)
    ctx["source_health"] = {label: (passed, detail)
                           for label, passed, detail, _fix in preflight_checks}

    # ── ProactiveScanner: preemptive evolution checks ──
    scanner_results: list = []
    try:
        from proactive_scanner import run_scan
        scanner_results = run_scan(db_path, max_results=3)
    except Exception:
        pass

    # ── Build SelfModel (unified state, replaces fragmented injections) ──
    self_model = None
    try:
        from self_model import build_from_state
        self_model = build_from_state(db_path)
        # Attach scanner results as predictions
        for r in scanner_results:
            from self_model import Prediction
            self_model.predictions.append(Prediction(
                domain=r.domain, severity=r.severity, message=r.message,
            ))
    except Exception:
        pass

    # ── SelfModel injection (replaces active_skills, pending_reviews, skill_health, constraint summary, health alerts) ──
    if self_model:
        try:
            from self_model import render_snapshot
            header, lines = render_snapshot(self_model)
            output.add(0.92, header, lines)
        except Exception:
            _inject_active_skills(output, ctx, db_path)
            _inject_pending_reviews(output, skills_dir)
    else:
        _inject_active_skills(output, ctx, db_path)
        _inject_pending_reviews(output, skills_dir)
    _inject_trigger_matches(output, ctx, injector_cfg)
    _inject_intent_match(output, ctx, injector_cfg)

    # ── Meta-ToM: attention-fused context injection ──
    mind_db = None
    try:
        from indexer import HarnessDB
        mind_db = HarnessDB(db_path)
        _inject_mind_context(output, ctx, config, mind_db)
    except Exception:
        pass

    _inject_constraints_detailed(output, constraints)
    _inject_omega_diagnostics(output, db_path)
    _inject_memory_fragments(output, db_path, injector_cfg)
    _inject_failure_patterns(output, db_path, ctx.get("matched", []), injector_cfg)
    _inject_preflight_checks_from_cache(output, preflight_checks)

    # Inject cross-session correction warning if pattern detected
    if correction_warning:
        output.add(0.85, "## ⚠️ 跨Session纠错模式", [correction_warning])

    result = output.render()
    if result:
        print(result)
    else:
        print("[harness] injector: no context to inject.")

    if mind_db:
        try:
            mind_db.close()
        except Exception:
            pass

    # Persist SelfModel after injection
    if self_model:
        try:
            from self_model import persist, snapshot
            persist(self_model)
            snapshot(self_model)
        except Exception:
            pass


# ─── Skill lifecycle ───────────────────────────────────────────────────

def _update_skill_usage(db, skill_names: list[str]):
    """Increment usage_count and update last_used for skills used this session."""
    try:
        with db._lock:
            for name in skill_names:
                candidates = [name, f"harness_{name}", f"{name}.md"]
                for c in candidates:
                    cur = db._conn.execute(
                        "SELECT name FROM skill_index WHERE name=? OR name LIKE ?",
                        (c, f"%{name}%"),
                    )
                    row = cur.fetchone()
                    if row:
                        db._conn.execute(
                            "UPDATE skill_index SET usage_count = COALESCE(usage_count, 0) + 1, "
                            "last_used = unixepoch() WHERE name=?",
                            (row[0],),
                        )
                        break
            db._conn.commit()
    except Exception:
        pass


def _evaluate_skill_effectiveness(db, report, session_content: str):
    """Binary + positive feedback loop for skill effectiveness.

    Provenance:
    - Negative: failure pattern signatures match session content (same as before)
    - Positive: if constraint_candidates are empty AND session had tool activity,
      all skills get a small boost (clean-session signal).
      Connectivity: report.constraint_candidates comes from observer's
      _detect_recurring_failures → empty candidates = no tool failed 3+ times.

    This closes the feedback loop: skills get rewarded when nothing fails,
    not just punished when failures recur. Prevents "everything decays to zero."
    """
    try:
        session_lower = session_content.lower()

        with db._lock:
            cur = db._conn.execute(
                "SELECT id, tag, skill_name, content, confidence FROM fragments "
                "WHERE fragment_type='failure_pattern' AND skill_name IS NOT NULL"
            )
            patterns = cur.fetchall()

        any_failure_recurred = False

        for fid, tag, skill_name, content, conf in patterns:
            if not skill_name:
                continue

            # Extract error signatures from the failure pattern's tag + content
            sig_words = set(tag.lower().replace("-", " ").split())
            sig_words.update(w.lower() for w in content[:200].split()
                           if len(w) > 3 and w.isalpha())

            hits = sum(1 for sw in sig_words if sw in session_lower)
            match_ratio = hits / max(len(sig_words), 1)

            if match_ratio > 0.3:
                any_failure_recurred = True
                new_conf = max(0.2, (conf or 0.5) - 0.1)
                with db._lock:
                    db._conn.execute(
                        "UPDATE fragments SET confidence=?, updated_at=unixepoch() WHERE id=?",
                        (new_conf, fid),
                    )
                    db._conn.execute(
                        "UPDATE skill_index SET harness_confidence=MAX(0.2, harness_confidence-0.05) "
                        "WHERE name LIKE ?",
                        (f"%{skill_name}%",),
                    )
                    db._conn.commit()
                print(f"[harness] Skill ineffective: {skill_name} — "
                      f"'{tag}' recurred (conf {conf:.2f}→{new_conf:.2f})")

        # ── Positive feedback: if no failure patterns recurred, skills were effective ──
        if not any_failure_recurred:
            for fid, tag, skill_name, content, conf in patterns:
                if not skill_name:
                    continue
                boost = 0.02
                new_conf = min(0.95, (conf or 0.5) + boost)
                with db._lock:
                    db._conn.execute(
                        "UPDATE fragments SET confidence=?, updated_at=unixepoch() WHERE id=?",
                        (new_conf, fid),
                    )
                    db._conn.execute(
                        "UPDATE skill_index SET harness_confidence="
                        "MIN(0.95, harness_confidence+0.01) "
                        "WHERE name LIKE ?",
                        (f"%{skill_name}%",),
                    )
                    db._conn.commit()
            print(f"[harness] Skills boosted: no failure patterns recurred "
                  f"({len(patterns)} patterns)")
    except Exception:
        pass


# ─── Constraint registry ────────────────────────────────────────────────

def _seed_constraints(db, candidates: list[dict], session_id: str):
    """Create constraints from recurring failure patterns detected by observer."""
    now = time.time()
    with db._lock:
        for c in candidates:
            tool = c["tool_name"]
            pattern = c["match_pattern"]
            # Avoid duplicate active constraints for same tool+pattern
            existing = db._conn.execute(
                "SELECT id FROM constraints WHERE tool_name=? AND match_pattern=? AND active=1",
                (tool, pattern),
            ).fetchone()
            if existing:
                continue

            # Sandbox verification before creating constraint
            try:
                from sandbox_verifier import verify_constraint
                constraint_candidate = {
                    "tool_name": tool,
                    "match_pattern": pattern,
                    "name": f"block-{tool.lower()}-{pattern[:30]}",
                }
                vr = verify_constraint(constraint_candidate, db_path=db.db_path)
                if vr.false_positive_risk > 0.30:
                    print(f"[harness] Sandbox: SKIPPED constraint for {tool}({pattern[:40]}) "
                          f"— false positive risk {vr.false_positive_risk:.0%} > 30%")
                    continue
                else:
                    print(f"[harness] Sandbox: constraint for {tool}({pattern[:40]}) OK "
                          f"(fp_risk={vr.false_positive_risk:.0%})")
            except Exception:
                pass  # Non-fatal: proceed with constraint creation if sandbox fails

            name = f"block-{tool.lower()}-{pattern[:30].replace('/', '-').replace('.', '-')}"
            message = (
                f"⛔ 此调用已被 Harness 约束注册表阻断。\n"
                f"原因: {tool}→{pattern} 在近期 session 中连续失败 {c['failure_count']} 次。\n"
                f"替代方案: 使用其他可用数据源或工具。\n"
                f"约束名称: {name}\n"
                f"过期时间: {datetime.fromtimestamp(now + 86400).strftime('%Y-%m-%d %H:%M')} (24h 后自动解除)\n"
                f"违反次数: 0/{5} — 达到上限后升级为 session-fatal"
            )
            db._conn.execute(
                """INSERT INTO constraints
                   (name, tool_name, match_pattern, action, message, source_session,
                    expires_at, created_at)
                   VALUES (?, ?, ?, 'block', ?, ?, ?, ?)""",
                (name, tool, pattern, message, session_id, now + 86400, now),
            )
        db._conn.commit()


def _ensure_critical_constraints(db_path: Path):
    """Seed permanent critical constraints if they don't exist (idempotent).

    These constraints protect shared infrastructure (settings files, hooks)
    from accidental damage during automated operations.
    """
    critical_constraints = [
        {
            "name": "hooks-settings-preserve-foreign",
            "tool_name": "Edit",
            "match_pattern": "settings.local.json",
            "action": "warn",
            "message": (
                "⚠️ 正在修改 settings.local.json 的 hooks 区域。"
                "请确认未删除非 Harness 管理的 Hook（Stop/Notification 等）。"
                "Harness 仅管理: UserPromptSubmit, SessionStart, PreToolUse, PostToolUse。"
                "其他 Hook 事件必须原样保留。"
            ),
        },
        {
            "name": "hooks-settings-preserve-foreign-write",
            "tool_name": "Write",
            "match_pattern": "settings.local.json",
            "action": "warn",
            "message": (
                "⚠️ 正在覆写 settings.local.json。"
                "如果 hooks 字段包含非 Harness 管理的 Hook（Stop/Notification 等），"
                "必须原样保留。Harness 仅管理: UserPromptSubmit, SessionStart, PreToolUse, PostToolUse。"
            ),
        },
    ]
    try:
        conn = sqlite3.connect(str(db_path), timeout=2)
        conn.execute("PRAGMA journal_mode=WAL")
        for c in critical_constraints:
            cur = conn.execute(
                "SELECT id FROM constraints WHERE name=? AND active=1", (c["name"],)
            )
            if cur.fetchone() is None:
                conn.execute(
                    "INSERT INTO constraints "
                    "(name, tool_name, match_pattern, action, message, "
                    "violation_count, max_violations, active, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 0, 999, 1, unixepoch())",
                    (c["name"], c["tool_name"], c["match_pattern"],
                     c["action"], c["message"]),
                )
        conn.commit()
        conn.close()
    except Exception:
        pass  # Non-critical; don't block inject on constraint seeding failure


def _load_active_constraints(db_path: Path) -> list[dict]:
    """Load non-expired active constraints for the PreToolUse hook cache."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=2)
        conn.execute("PRAGMA journal_mode=WAL")
        cur = conn.execute(
            "SELECT name, tool_name, match_pattern, action, message, "
            "violation_count, max_violations "
            "FROM constraints WHERE active=1 AND (expires_at IS NULL OR expires_at > unixepoch())"
        )
        constraints = [
            {
                "name": r[0], "tool_name": r[1], "match_pattern": r[2],
                "action": r[3], "message": r[4],
                "violations": r[5] or 0, "max_violations": r[6] or 5,
            }
            for r in cur.fetchall()
        ]
        conn.close()
        return constraints
    except Exception:
        return []


def _write_constraint_cache(constraints: list[dict]):
    """Write active constraints to a fast-read JSON cache for hook consumption."""
    cache_path = HARNESS_DIR / ".constraint_cache.json"
    try:
        cache_path.write_text(
            json.dumps(constraints, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


# ─── Injector section functions (each independently safe) ───────────────

def _inject_active_skills(output: InjectorOutput, ctx: dict, db_path: Path):
    try:
        active = _list_active_skills()
        ctx["active"] = active
        if not active:
            return

        usage_map = {}
        try:
            from indexer import HarnessDB
            db = HarnessDB(db_path)
            with db._lock:
                cur = db._conn.execute(
                    "SELECT name, usage_count, last_used, created_at FROM skill_index"
                )
                for row in cur.fetchall():
                    usage_map[row[0]] = {
                        "count": row[1] or 0,
                        "last_used": row[2],
                        "created_at": row[3],
                    }
            db.close()
        except Exception:
            pass

        lines = []
        for s in active:
            name = s["name"]
            trigger = _truncate(s["description"], 60) or (
                _truncate(s["triggers"][0], 60) if s["triggers"] else "—"
            )
            marker = ""
            stats = usage_map.get(f"harness_{name}.md") or usage_map.get(name)
            if stats:
                if stats["count"] == 0:
                    age_days = (time.time() - (stats["created_at"] or 0)) / 86400
                    if age_days > 3:
                        marker = " ⚠️闲置"
                    elif age_days > 1:
                        marker = " (新)"
            lines.append(f"- `harness:{name}` — {trigger}{marker}")
        output.add(0.5, "## 活跃 Harness 技能", lines)
    except Exception:
        pass


def _inject_skill_health(output: InjectorOutput, db_path: Path, injector_cfg: dict):
    """Skill health — report idle and low-confidence skills. Priority: 0.65.

    Skills are user-constant knowledge. They do NOT expire from idleness.
    The only degradation mechanism is failure pattern recurrence
    (_evaluate_skill_effectiveness lowers confidence when patterns recur).
    """
    try:
        from indexer import HarnessDB
        db = HarnessDB(db_path)
        cur = db._conn.execute(
            "SELECT name, usage_count, last_used, created_at, harness_confidence "
            "FROM skill_index ORDER BY usage_count ASC"
        )
        idle = []
        low_conf = []
        for row in cur.fetchall():
            name, count, last_used, created_at, conf = row
            count = count or 0
            conf = conf or 0.5
            age_days = (time.time() - (created_at or time.time())) / 86400
            if count == 0 and age_days > 3:
                idle.append((name, age_days))
            if conf < 0.4:
                low_conf.append((name, conf))
        db.close()

        if not idle and not low_conf:
            return

        lines = []
        if low_conf:
            lines.append("以下技能因失败模式复现导致置信度下降：")
            for name, conf in low_conf[:3]:
                lines.append(f"- `{name}` — 置信度 {conf:.0%}（已被 _evaluate_skill_effectiveness 降权）")
        if idle:
            if lines:
                lines.append("")
            lines.append("以下技能近期未使用（不会自动归档）：")
            for name, age in idle[:3]:
                lines.append(f"- `{name}` — {age:.0f} 天未使用")
        if lines:
            output.add(0.65, "## 💤 技能状态", lines)
    except Exception:
        pass


def _inject_omega_diagnostics(output: InjectorOutput, db_path: Path):
    """Read Omega diagnostic fragments and inject them for the next session.

    Connectivity: Omega classify_failure → context_injection →
    classify_beliefs → BeliefTrace.context_injection → fragments table
    (fragment_type='omega_diagnostic') → this function reads it.

    Priority: 0.70 (above skill health at 0.65; below constraints at 0.80).
    Only fresh diagnostics (< 7 days) are injected.
    """
    try:
        from indexer import HarnessDB
        db = HarnessDB(db_path)
        with db._lock:
            cur = db._conn.execute(
                "SELECT tag, content, source_session, confidence, created_at "
                "FROM fragments WHERE fragment_type='omega_diagnostic' "
                "AND created_at > unixepoch() - 604800 "  # 7 days
                "ORDER BY created_at DESC LIMIT 3"
            )
            rows = cur.fetchall()
        db.close()

        if not rows:
            return

        lines = []
        for tag, content, source_session, conf, created_at in rows:
            ts = datetime.fromtimestamp(created_at).strftime("%m-%d %H:%M")
            modality = tag.replace("omega-", "").replace("_failure", "")
            lines.append(f"**[Omega] {modality} 层诊断** ({ts}, conf={conf:.0%}):")
            lines.append(content[:300])

        if lines:
            output.add(0.70, "## 🧠 Omega 诊断注入", lines)
    except Exception:
        pass


def _inject_pending_reviews(output: InjectorOutput, skills_dir: Path):
    try:
        pending = _list_pending_skills(skills_dir)
        if not pending:
            return
        lines = []
        for i, skill_path in enumerate(pending, 1):
            info = _parse_skill_frontmatter(skill_path)
            trigger = _truncate(info["description"], 50) or info["name"]
            lines.append(f"{i}. `{skill_path.stem}` — {trigger} → `review --show {i}`")
        output.add(0.6, f"## 待审查技能 ({len(pending)})", lines)
    except Exception:
        pass


def _inject_trigger_matches(output: InjectorOutput, ctx: dict, injector_cfg: dict):
    try:
        current_topic = _read_last_user_message()
        active = ctx.get("active", [])
        if not current_topic or not active:
            return
        matched = _match_triggers(active, current_topic)
        if not matched:
            return
        ctx["matched"] = matched
        lines = []
        for m in matched:
            lines.append(f"- `harness:{m['name']}` — {m['description'][:80]}")
            lines.append(f"  触发: {', '.join(m['matched_triggers'][:3])}")
        output.add(0.7, "## 检测到相关技能", lines)
    except Exception:
        pass


def _inject_constraints(output: InjectorOutput, constraints: list[dict], injector_cfg: dict):
    """Active constraints — hard blocks on known-bad tool calls. Priority: 0.9."""
    if not constraints:
        return
    lines = []
    for c in constraints:
        escalated = " 🔴已升级" if c["violations"] >= c["max_violations"] else ""
        lines.append(
            f"- ⛔ **{c['tool_name']}** `{c['match_pattern'][:50]}` — "
            f"违反 {c['violations']}/{c['max_violations']}{escalated}"
        )
    output.add(0.9, f"## ⛔ 活跃约束 ({len(constraints)} 项)", lines)


def _inject_constraints_detailed(output: InjectorOutput, constraints: list[dict]):
    """Active constraints — detailed listing only. Summary count is in SelfModel.
    Priority lowered to 0.82 (SelfModel summary is at 0.92).
    """
    if not constraints:
        return
    lines = []
    for c in constraints:
        escalated = " 🔴已升级" if c["violations"] >= c["max_violations"] else ""
        lines.append(
            f"- ⛔ **{c['tool_name']}** `{c['match_pattern'][:50]}` — "
            f"违反 {c['violations']}/{c['max_violations']}{escaled}"
        )
    output.add(0.82, "## ⛔ Constraint Registry", lines)


def _inject_intent_match(output: InjectorOutput, ctx: dict, injector_cfg: dict):
    try:
        current_topic = _read_last_user_message()
        if not current_topic:
            return
        from intent_matcher import match_intent, inject_workflow_context
        intent_result = match_intent(current_topic)
        if intent_result:
            source_health = ctx.get("source_health", {})
            ctx_text = inject_workflow_context(intent_result, source_health=source_health)
            if ctx_text:
                output.add(0.8, "", [ctx_text])
    except ImportError:
        pass
    except Exception:
        if injector_cfg.get("verbose", False):
            import sys as _sys
            print("[harness] intent matcher error", file=_sys.stderr)


def _inject_mind_context(
    output: InjectorOutput,
    ctx: dict,
    config: dict,
    db,
):
    """Inject Meta-ToM fusion context (<=5 lines, priority 0.75).

    Flow: embed current message → cosine gate vs prev_fusion → if continuous, inject.
    On cold start or task-switch: falls through silently (keyword matching handles it).
    """
    mind_cfg = config.get("mind_theory", {})
    if not mind_cfg.get("enabled", False):
        return

    import sys as _sys
    try:
        current_msg = _read_last_user_message()
        if not current_msg:
            return

        from encoder import encode_cached, text_hash
        from cosine_gate import check_continuity, get_prev_fusion, get_default_alphas

        # Check for previous fusion
        prev = get_prev_fusion(db)
        if prev is None:
            return  # Cold start, fall through to keyword matching

        prev_fusion, prev_alphas = prev

        # Embed current message and check continuity
        msg_emb = encode_cached(current_msg, "user_msg", text_hash(current_msg), db)
        is_continuous, score = check_continuity(
            msg_emb, prev_fusion,
            threshold=mind_cfg.get("cosine_gate", {}).get("threshold", 0.4),
        )

        if not is_continuous:
            # Store continuity score in ctx for downstream use
            ctx["mind_continuity"] = score
            if mind_cfg.get("verbose", False):
                print(f"[harness] mind: task switch (continuity={score:.2f})", file=_sys.stderr)
            return

        # Continuous: inject top-2 attention sources
        max_lines = mind_cfg.get("cosine_gate", {}).get("injection_max_lines", 5)
        lines = _build_mind_injection(prev_alphas, db, score, max_lines, mind_cfg)
        if lines:
            output.add(0.75, "## 延续上轮上下文", lines)
            ctx["mind_continuity"] = score

    except ImportError:
        pass  # encoder module not available
    except Exception:
        if mind_cfg.get("verbose", False):
            import traceback
            print(f"[harness] mind inject error: {traceback.format_exc()}", file=_sys.stderr)


def _build_mind_injection(
    alphas: dict[str, float],
    db,
    continuity_score: float,
    max_lines: int,
    mind_cfg: dict,
) -> list[str]:
    """Build injection lines from top attention sources."""
    lines = []
    lines.append(f"连续性: {continuity_score:.0%}")

    # Sort sources by alpha weight, pick top 2
    sorted_sources = sorted(alphas.items(), key=lambda x: -x[1])
    top_sources = [(k, v) for k, v in sorted_sources if v > 0.05][:2]

    source_labels = {
        "user_msg": "用户意图",
        "claude_behavior": "行为模式",
        "session_tags": "领域标签",
        "history_summary": "历史摘要",
        "memory_entries": "记忆条目",
    }

    for key, weight in top_sources:
        label = source_labels.get(key, key)
        lines.append(f"- 关注 {label} (权重: {weight:.2f})")

    return lines[:max_lines]


# ── Meta-ToM pipeline (offline) ─────────────────────────────────────────


def _run_mind_pipeline(
    db,
    report,
    transcript_path: Path,
    session_content: str,
    config: dict,
    config_path: Path,
):
    """Meta-ToM v8 — simplified: Ω (3×3 matrix) + cosgate continuity.

    Killed: Ψ k-NN goal prediction, encoder 5-source fusion, attention_fuser SGD,
    consistency_verifier, session_quality composite scoring.

    Kept: Observer extract_session_structure, Omega classify_failure → constraint seeding,
    cosgate continuity check, session_quality correction-only scoring.
    """
    mind_cfg = config.get("mind_theory", {})

    # Parse transcript entries
    entries = []
    try:
        import json as _json
        if transcript_path.exists():
            for line in transcript_path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped:
                    try:
                        entries.append(_json.loads(stripped))
                    except _json.JSONDecodeError:
                        pass
    except Exception:
        pass

    if not entries:
        print("[harness] mind: no transcript entries found, skipping pipeline")
        return

    from observer import extract_session_structure
    structure = extract_session_structure(entries)

    # ── Pre-compute session outcome signals used by both Ω and Ψ elimination ──
    has_correction = len(structure.get("user_corrections", [])) > 0
    has_failure = len(structure.get("error_tool_calls", [])) > 0

    # ── Ω: classify false beliefs (multi-hypothesis) ──
    session_id = report.session_id
    from omega_predictor import classify_beliefs, eliminate_beliefs_by_session, get_false_beliefs_for_constraints
    belief_traces = classify_beliefs(entries, session_id, db)

    # Eliminate competing belief hypotheses using session outcomes
    belief_traces = eliminate_beliefs_by_session(
        belief_traces,
        structure.get("error_tool_calls", []),
        constraint_violations=len([t for t in belief_traces
                                   if t.belief_type == "constraint_knowledge"]),
        has_user_correction=has_correction,
    )

    for trace in belief_traces:
        db.save_belief_trace(
            session_id, trace.belief_type, trace.confidence,
            trace.evidence, trace.recommended_action,
            getattr(trace, "escalation_blocked_reason", "") or "",
            tool_name=getattr(trace, "tool_name", ""),
            match_pattern=getattr(trace, "match_pattern", ""),
        )
        # ── Save Omega diagnostic context for next-session injection ──
        # Connectivity: context_injection → fragments table → injector reads it
        ctx_inj = getattr(trace, 'context_injection', '')
        if ctx_inj and trace.belief_type in ("semantic_failure", "knowledge_failure", "operation_failure"):
            try:
                with db._lock:
                    db._conn.execute(
                        "INSERT INTO fragments(tag, trigger_phrases, content, "
                        "source_session, confidence, fragment_type, created_at) "
                        "VALUES(?,?,?,?,?,?,unixepoch())",
                        (f"omega-{trace.belief_type}", "",
                         ctx_inj[:2000], session_id, trace.confidence,
                         "omega_diagnostic"),
                    )
                    db._conn.commit()
            except Exception:
                pass

    # Seed constraints from false beliefs (multi-hypothesis aware: skips ambiguous)
    false_beliefs, blocked_escalations = get_false_beliefs_for_constraints(belief_traces)
    for fb in false_beliefs:
        db.save_false_belief(session_id, fb.belief_type, fb.tool_name, fb.match_pattern)
        _seed_false_belief_constraint(db, fb, session_id)
    # Log blocked escalations with reason for audit trail
    for blocked in blocked_escalations:
        bt = blocked["trace"]
        db.save_false_belief(session_id, bt.belief_type, bt.tool_name, bt.match_pattern)
        print(f"[harness] mind: ⬜ escalation BLOCKED — {bt.belief_type}: {blocked['reason'][:120]}")

    if belief_traces:
        ambiguous_count = sum(1 for t in belief_traces if t.is_ambiguous)
        verified_count = sum(1 for t in belief_traces if t.verified)
        amb_tag = f", {ambiguous_count} ambiguous" if ambiguous_count else ""
        ver_tag = f", {verified_count} verified" if verified_count else ""
        blocked_tag = f", {len(blocked_escalations)} blocked" if blocked_escalations else ""
        print(f"[harness] mind: {len(belief_traces)} belief traces{amb_tag}{ver_tag}{blocked_tag}, "
              f"{len(false_beliefs)} escalated to constraints")

    # ── Ψ: predict goals (multi-hypothesis) + extract interaction pairs ──
    from psi_predictor import predict_goals, eliminate_goals_by_session, get_interaction_pairs_for_storage

    pairs_data = get_interaction_pairs_for_storage(entries, session_id, db)
    for pair_data in pairs_data:
        db.save_interaction_pair(
            pair_data["session_id"],
            pair_data["user_message"],
            pair_data["claude_actions"],
            pair_data["outcome"],
            pair_data["user_message_embedding"],
        )

    goal_hypotheses = predict_goals(
        structure["first_user_message"],
        session_id,
        db,
        structure["tool_use_sequence"],
        structure["tool_types"],
        k=mind_cfg.get("psi", {}).get("k_neighbors", 5),
    )

    # Eliminate competing goal hypotheses using session outcomes
    goal_hypotheses = eliminate_goals_by_session(
        goal_hypotheses,
        structure["tool_types"],
        report.tags,
        has_correction=has_correction,
        has_failure=has_failure,
    )

    top_goal = goal_hypotheses[0] if goal_hypotheses else None
    if top_goal:
        ambiguous_tag = " [AMBIGUOUS]" if top_goal.is_ambiguous else ""
        print(f"[harness] mind: goal={top_goal.goal_type} "
              f"confidence={top_goal.confidence:.2f} "
              f"hypotheses={len(goal_hypotheses)}{ambiguous_tag}")
        if top_goal.is_ambiguous and len(goal_hypotheses) >= 2:
            alt_types = [h.goal_type for h in goal_hypotheses[1:]]
            print(f"[harness] mind:   competing: {', '.join(alt_types)}")
        if top_goal.verified:
            print(f"[harness] mind:   goal verified by session outcome")
        if top_goal.falsified:
            print(f"[harness] mind:   top goal FALSIFIED — check alternatives")

    # ── Verification: compare predicted vs actual ──
    from consistency_verifier import verify, get_verification_summary
    from psi_predictor import GoalPrediction

    # Fallback goal prediction if psi produced nothing
    if top_goal is None:
        top_goal = GoalPrediction(
            goal_type="unknown", confidence=0.3, domain="general",
            expected_tools=structure["tool_types"],
        )

    verif_report = verify(
        session_id=session_id,
        goal_prediction=top_goal,
        belief_traces=belief_traces,
        actual_actions=structure.get("tool_use_sequence", []),
        db=db,
        user_corrections=[e.get("content", "") for e in entries
                         if e.get("type") == "user"
                         and any(kw in e.get("content", "") for kw in
                                ["不对", "错了", "不是这样", "纠正", "重新", "改一下",
                                 "你忘了", "搞错了"])],
        error_tool_calls=structure.get("error_tool_calls", []),
    )
    print(f"[harness] mind: verify — {get_verification_summary(verif_report)}")

    # If verification found errors, act on them
    if verif_report.error_type == "goal_error":
        # Psi retraining: the corrected interaction pair was already saved above
        print(f"[harness] mind: verify → goal_error → psi retraining data stored")
    elif verif_report.error_type == "belief_error":
        # Mark beliefs as verified since verification confirmed the error pattern
        for trace in belief_traces:
            db.mark_belief_verified(session_id, trace.belief_type,
                                    was_correct=1, reason="verification_confirmed")
        print(f"[harness] mind: verify → belief_error → {len(belief_traces)} beliefs marked verified")
    elif verif_report.error_type == "skill_gap":
        print(f"[harness] mind: verify → skill_gap → feeding to observer pipeline")
    elif verif_report.error_type == "none" and verif_report.confidence > 0.5:
        # Positive sample: mark beliefs as verified correct
        for trace in belief_traces:
            db.mark_belief_verified(session_id, trace.belief_type,
                                    was_correct=1, reason="verification_passed")
        if belief_traces:
            print(f"[harness] mind: verify → none → {len(belief_traces)} beliefs marked correct")

    # ── Retroactive verification: mark old stale belief_traces as resolved ──
    try:
        cur = db._conn.execute(
            "SELECT COUNT(*) FROM belief_traces "
            "WHERE was_correct = 0 AND created_at < unixepoch() - 259200"  # >3 days old
        )
        stale_count = cur.fetchone()[0]
        if stale_count > 0:
            # Mark stale unverified beliefs as correct=1 (pattern was transient/fixed)
            db._conn.execute(
                "UPDATE belief_traces SET was_correct = 1, "
                "recommended_action = recommended_action || ' [verified: stale_pattern_resolved]' "
                "WHERE was_correct = 0 AND created_at < unixepoch() - 259200"
            )
            db._conn.commit()
            print(f"[harness] mind: retroactive verify → {stale_count} stale beliefs marked resolved (>3d old)")
    except Exception:
        pass

    # ── Encoder: compute source embeddings ──
    from encoder import compute_source_embeddings

    # Build history summary from recent observations
    history_text = _build_history_summary(db)

    # Build memory entries text
    memory_text = _build_memory_text()

    session_data = {
        "user_msg": structure["first_user_message"] or " ",
        "claude_behavior": _summarize_claude_behavior(structure),
        "session_tags": " ".join(report.tags) if report.tags else "general",
        "history_summary": history_text,
        "memory_entries": memory_text,
    }

    embeddings = compute_source_embeddings(session_data, db)

    # ── Attention Fuser ──
    from attention_fuser import (fuse, update_weights, get_learning_rate,
                                  check_early_warning, detect_collinearity,
                                  compute_per_source_quality,
                                  USER_CONSTANT_SOURCES, ENVIRONMENT_SOURCES)
    from cosine_gate import get_default_alphas

    # Get alphas from previous session (or config defaults)
    prev = db.get_latest_fusion()
    if prev and prev.get("alphas"):
        alphas = prev["alphas"]
    else:
        alphas = get_default_alphas(config_path)

    fusion_vector, attention_dist = fuse(embeddings, alphas)

    # ── Session Quality ──
    from session_quality import compute_quality
    quality = compute_quality(
        session_id,
        db_path=config_path.parent / "state.db",
        transcript_text=session_content,
        entries=entries,
        observer_tags=report.tags,
        tool_types=structure["tool_types"],
        config_path=config_path,
    )

    # ── Success memory: store what went RIGHT (symmetric to failure tracking) ──
    from session_quality import extract_success_patterns
    success_patterns = extract_success_patterns(
        session_id=session_id,
        quality=quality,
        tool_types=structure["tool_types"],
        observer_tags=report.tags,
        has_correction=has_correction,
        has_failure=has_failure,
        goal_type=top_goal.goal_type if top_goal else "",
        goal_confidence=top_goal.confidence if top_goal else 0.0,
    )
    if success_patterns:
        try:
            with db._lock:
                for sp in success_patterns:
                    db._conn.execute(
                        "INSERT INTO fragments(tag, trigger_phrases, content, "
                        "source_session, confidence, fragment_type, created_at) "
                        "VALUES(?,?,?,?,?,?,unixepoch())",
                        (sp["tag"], "", sp["content"][:2000],
                         session_id, sp["confidence"], "success_pattern"),
                    )
                db._conn.commit()
            print(f"[harness] mind: success memory → {len(success_patterns)} patterns stored "
                  f"(quality={quality:.3f})")
        except Exception:
            pass

    # ── Per-source quality (breaks collinearity in single-user systems) ──
    # Provenance: count Read tool calls targeting memory directory from entries list
    memory_read_count = 0
    for entry in entries:
        if entry.get("type") == "tool_use" and entry.get("name") == "Read":
            file_path = entry.get("input", {}).get("file_path", "")
            if "memory" in file_path.lower() and ".claude" in file_path.lower():
                memory_read_count += 1

    per_source_q = compute_per_source_quality(
        quality,
        has_user_correction=has_correction,
        memory_read_count=memory_read_count,
    )

    # ── Collinearity detection ──
    collinearity_window = getattr(cmd_inject, '_collinearity_window', [])
    is_collinear, collinearity_msg = detect_collinearity(alphas, collinearity_window)
    cmd_inject._collinearity_window = collinearity_window
    if collinearity_msg:
        print(collinearity_msg)

    # ── Weight Update ──
    attn_cfg = mind_cfg.get("attention", {})
    try:
        cur = db._conn.execute("SELECT COUNT(*) FROM fusion_sessions")
        session_count = cur.fetchone()[0]
    except Exception:
        session_count = 0

    lr = get_learning_rate(
        session_count,
        initial=attn_cfg.get("learning_rate_initial", 0.01),
        decay=attn_cfg.get("learning_rate_decay", 0.001),
        decay_sessions=attn_cfg.get("decay_sessions", 50),
    )

    # Track consecutive-below-threshold from previous session
    consecutive_below = {}
    if prev and prev.get("attention_distribution"):
        prev_attn = prev["attention_distribution"]
        archive_threshold = attn_cfg.get("archive_threshold", 0.05)
        for key in alphas:
            consecutive_below[key] = 0
            if prev_attn.get(key, 0) < archive_threshold:
                consecutive_below[key] = 1

    new_alphas, below_counters = update_weights(
        alphas, quality, attention_dist, lr, consecutive_below,
        archive_threshold=attn_cfg.get("archive_threshold", 0.05),
        archive_sessions=attn_cfg.get("archive_consecutive_sessions", 10),
        per_source_quality=per_source_q,
        no_archive_sources=USER_CONSTANT_SOURCES,
        is_collinear=is_collinear,
    )

    # ── Compute continuity score ──
    from cosine_gate import check_continuity
    msg_emb = embeddings.get("user_msg", [])
    prev_fusion_vec = prev.get("fusion_vector", []) if prev else []
    _, continuity_score = check_continuity(msg_emb, prev_fusion_vec)

    # ── Save fusion session ──
    db.save_fusion_session(
        session_id, fusion_vector, new_alphas, attention_dist,
        continuity_score, quality,
    )

    # ── Archive notification ──
    for key, counter in below_counters.items():
        if counter >= attn_cfg.get("archive_consecutive_sessions", 10):
            print(f"[harness] mind: source '{key}' archived "
                  f"(weight < {attn_cfg.get('archive_threshold', 0.05)} for {counter} sessions)")

    print(f"[harness] mind: fusion saved — quality={quality:.3f}, "
          f"continuity={continuity_score:.3f}, lr={lr:.4f}")

    # Early warning for sources approaching archival
    archive_warnings = check_early_warning(
        below_counters,
        archive_sessions=attn_cfg.get("archive_consecutive_sessions", 10),
        warn_at=5,
        no_archive_sources=USER_CONSTANT_SOURCES,
    )
    for w in archive_warnings:
        print(f"[harness] mind: {w}")


def _seed_false_belief_constraint(db, belief_trace, session_id: str):
    """Create a constraint from a confirmed false belief.

    Only handles operation failures and legacy types — these block specific tools.
    Semantic and knowledge failures are handled via omega_diagnostic fragments
    (saved in the belief_trace loop above), not via tool constraints.
    """
    bt = belief_trace.belief_type

    # Only operation-failure types get tool constraints
    if bt not in ("tool_accessibility", "constraint_knowledge", "operation_failure"):
        return

    if not getattr(belief_trace, 'match_pattern', '') or not getattr(belief_trace, 'tool_name', ''):
        return

    try:
        match_pattern = belief_trace.match_pattern
        tool_name = belief_trace.tool_name
        constraint_name = f"auto: {bt} → {match_pattern[:60]}"
        message = f"历史会话中 {tool_name}({match_pattern[:80]}) 失败。禁止重复调用。"

        with db._lock:
            db._conn.execute(
                """INSERT INTO constraints
                   (name, tool_name, match_pattern, action, message,
                    source_session, expires_at)
                   VALUES (?, ?, ?, 'block', ?, ?, unixepoch() + 21600)""",
                (constraint_name, tool_name,
                 match_pattern, message, session_id),
            )
            db._conn.commit()
    except Exception:
        pass


def _build_history_summary(db) -> str:
    """Build a text summary from recent observations for the encoder."""
    try:
        recent = db.get_recent_observations(5)
        if not recent:
            return "no history"
        parts = []
        for obs in recent:
            tags = obs.get("tags", [])
            action = obs.get("action", "")
            parts.append(f"action={action} tags={','.join(tags)}")
        return " | ".join(parts)
    except Exception:
        return "no history"


def _build_memory_text() -> str:
    """Read memory entries for embedding."""
    try:
        memory_dir = Path.home() / ".claude" / "projects" / "D--Claude" / "memory"
        if not memory_dir.exists():
            return "no memory"
        texts = []
        for md_file in sorted(memory_dir.glob("*.md")):
            if md_file.name == "MEMORY.md":
                continue
            try:
                content = md_file.read_text(encoding="utf-8")
                # Take first 500 chars from each file
                texts.append(content[:500])
            except Exception:
                pass
        return " ".join(texts[:10]) if texts else "no memory"
    except Exception:
        return "no memory"


def _summarize_claude_behavior(structure: dict) -> str:
    """Summarize Claude's tool usage pattern for encoder."""
    tool_uses = structure.get("tool_use_sequence", [])
    if not tool_uses:
        return "no tools"
    names = [t.get("name", "") for t in tool_uses[:20]]
    return " ".join(names)


def _inject_memory_fragments(output: InjectorOutput, db_path: Path, injector_cfg: dict):
    try:
        from indexer import HarnessDB
        db = HarnessDB(db_path)
        recent = db.get_recent_observations(3)
        if not recent:
            db.close()
            return
        query = " ".join(
            tag for obs in recent for tag in obs.get("tags", []) if tag != "general"
        )
        if not query:
            db.close()
            return
        fragments = _search_fragments(
            db, query,
            max_results=injector_cfg.get("max_fragments", 3),
            min_confidence=injector_cfg.get("min_confidence", 0.6),
        )
        db.close()
        if fragments:
            lines = []
            for f in fragments:
                lines.append(
                    f"- [{f['tag']}] (置信度: {f['confidence']:.0%}) {f['content']}")
            output.add(0.55, "## 相关记忆片段", lines)
    except Exception:
        pass


def _inject_failure_patterns(output: InjectorOutput, db_path: Path,
                              matched: list, injector_cfg: dict):
    try:
        if not matched:
            return
        from indexer import HarnessDB
        db = HarnessDB(db_path)
        matched_skill_names = [m["name"] for m in matched]
        failure_patterns = _search_failure_patterns(
            db, matched_skill_names,
            max_results=injector_cfg.get("max_failure_patterns", 5),
        )
        db.close()
        if failure_patterns:
            lines = ["以下是被动经验——加载上述技能前，先检查这些条件是否满足：", ""]
            for fp in failure_patterns:
                lines.append(
                    f"- **{fp['tag']}** [技能: `{fp['skill_name']}`] "
                    f"(置信度: {fp['confidence']:.0%})"
                )
                lines.append(f"  {fp['content']}")
            output.add(0.85, "## ⚠️ 已知失败模式", lines)
    except Exception:
        pass


# ─── Preflight checks ──────────────────────────────────────────────────
#
# Trust levels:
#   L0 — report only (needs human judgment or diagnosis)
#   L1 — auto-fix non-destructive config (settings flags, booleans)
#   L2 — auto-fix MCP/server config (reserved for known-safe rewrites)

def _auto_fix_skipWebFetchPreflight() -> str:
    """L1: Write skipWebFetchPreflight: true to settings.local.json."""
    settings_path = Path.home() / ".claude" / "settings.local.json"
    try:
        if settings_path.exists():
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        else:
            settings = {}
        settings["skipWebFetchPreflight"] = True
        settings_path.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return "fixed"
    except Exception:
        return "failed"


def _auto_fix_wrapper_disabled(config_path: Path) -> str:
    """L1: Set mcp_wrapper.enabled to false in harness_config.yaml."""
    try:
        if not config_path.exists():
            return "failed"
        text = config_path.read_text(encoding="utf-8")
        new_text = text.replace(
            "mcp_wrapper:\n  enabled: true",
            "mcp_wrapper:\n  enabled: false",
        )
        if new_text == text:
            return "failed"
        config_path.write_text(new_text, encoding="utf-8")
        return "fixed"
    except Exception:
        return "failed"


def _run_preflight_checks(config: dict, config_path: Path) -> list:
    """Execute preflight checks. Returns [(name, passed, detail, fix_status)].

    fix_status: None (not fixable), 'fixed' (auto-fixed), 'failed' (fix attempted but failed)
    """
    checks: list = []

    # Check 1: skipWebFetchPreflight — L1 auto-fix
    try:
        settings_path = Path.home() / ".claude" / "settings.local.json"
        if settings_path.exists():
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            if settings.get("skipWebFetchPreflight"):
                checks.append(("skipWebFetchPreflight", True, "", None))
            else:
                fix_result = _auto_fix_skipWebFetchPreflight()
                checks.append(("skipWebFetchPreflight", fix_result == "fixed",
                    "已自动写入 settings.local.json" if fix_result == "fixed"
                    else "自动修复失败，需手动设置 skipWebFetchPreflight: true",
                    fix_result))
        else:
            fix_result = _auto_fix_skipWebFetchPreflight()
            checks.append(("skipWebFetchPreflight", fix_result == "fixed",
                "已创建 settings.local.json 并写入" if fix_result == "fixed"
                else "自动修复失败",
                fix_result))
    except Exception:
        checks.append(("skipWebFetchPreflight", False, "无法读取配置", None))

    # Check 2: MCP servers still using wrapper — L0 report only
    # (L2 auto-fix requires per-server node path + entry point knowledge)
    try:
        mcp_paths = [
            Path("D:/Claude/.mcp.json"),
            Path.home() / ".claude" / "mcp.json",
        ]
        for mcp_path in mcp_paths:
            if mcp_path.exists():
                mcp_config = json.loads(mcp_path.read_text(encoding="utf-8"))
                for name, svr in mcp_config.get("mcpServers", {}).items():
                    args_str = json.dumps(svr.get("args", []))
                    if "mcp_wrapper.py" in args_str:
                        checks.append(("MCP wrapper", False,
                            f"{name} 仍通过 mcp_wrapper 启动。"
                            "绕过方法: 直接 node/python 启动对应 server",
                            None))
                break
    except Exception:
        pass

    # Check 3: Stale pending reviews — L0 report only
    try:
        skills_dir = HARNESS_DIR / "skills"
        now = time.time()
        for sf in skills_dir.glob("*.md"):
            age_days = (now - sf.stat().st_mtime) / 86400
            if age_days > 7:
                checks.append(("过期审查", False,
                    f"{sf.stem} 等待审查 {age_days:.0f} 天", None))
    except Exception:
        pass

    # Check 4: Recent tool failure rate — L0 report only
    try:
        db_path = Path(config["harness"]["db_path"])
        conn = sqlite3.connect(str(db_path), timeout=2)
        cur = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) "
            "FROM tool_call_log WHERE timestamp > unixepoch() - 86400"
        )
        row = cur.fetchone()
        if row and row[0] and row[0] > 5:
            total, errors = row
            errors = errors or 0
            rate = errors / total
            if rate > 0.3:
                checks.append(("工具失败率", False,
                    f"过去24h: {errors}/{total} ({rate:.0%}) > 30%", None))
        conn.close()
    except Exception:
        pass

    # Check 5: harness_config.yaml wrapper contradiction — L1 auto-fix
    try:
        wrapper_enabled = config.get("mcp_wrapper", {}).get("enabled", False)
        if wrapper_enabled:
            fix_result = _auto_fix_wrapper_disabled(config_path)
            checks.append(("Wrapper 配置矛盾", fix_result == "fixed",
                "已自动关闭 mcp_wrapper.enabled" if fix_result == "fixed"
                else "自动修复失败，需手动设置 mcp_wrapper.enabled: false",
                fix_result))
    except Exception:
        pass

    # Check 6: Encoder health — L0 report only
    _probe_encoder_health(checks)

    # Check 7: DB connectivity — L0 report only
    _probe_db_health(checks, config)

    # Check 8: Meta-ToM pipeline status — L0 report only
    _probe_mind_health(checks, config)

    # Check 9: Critical hook integrity — L0 report only
    _probe_hook_integrity(checks)

    # Check 10-N: Data source health probes — L0 report only (can't fix other servers)
    _probe_data_sources(checks)

    return checks


def _probe_encoder_health(checks: list):
    """L0: Check encoder loading status — BGE, MiniLM, or random fallback."""
    try:
        from encoder import get_encoder
        enc = get_encoder()
        # Test with a short string to determine what backend is active
        vec = enc("test")
        dim = len(vec)
        # Check which backend by dimension: BGE=512, MiniLM=384, random=384
        from encoder import _model_available
        if _model_available is True:
            if dim >= 500:
                checks.append(("Encoder", True, f"BGE-small-zh ({dim}d) 正常加载", None))
            else:
                checks.append(("Encoder", True, f"all-MiniLM-L6-v2 ({dim}d) 正常加载", None))
        elif _model_available is False:
            checks.append(("Encoder", False, f"回退到 random projection ({dim}d) — 语义嵌入不可用", None))
        else:
            checks.append(("Encoder", True, f"已加载 ({dim}d)", None))
    except Exception as e:
        checks.append(("Encoder", False, f"加载失败: {str(e)[:80]}", None))


def _probe_db_health(checks: list, config: dict):
    """L0: Check SQLite database connectivity and basic query."""
    try:
        from pathlib import Path as _Path
        db_path = _Path(config["harness"]["db_path"])
        if not db_path.exists():
            checks.append(("Harness DB", False, f"数据库文件不存在: {db_path}", None))
            return
        import sqlite3
        conn = sqlite3.connect(str(db_path), timeout=2)
        cur = conn.execute("SELECT COUNT(*) FROM observations")
        obs_count = cur.fetchone()[0]
        cur = conn.execute("SELECT COUNT(*) FROM fusion_sessions")
        fusion_count = cur.fetchone()[0]
        size_mb = db_path.stat().st_size / (1024 * 1024)
        checks.append(("Harness DB", True,
                       f"OK — {obs_count} obs, {fusion_count} fusions, {size_mb:.1f}MB", None))
        conn.close()
    except Exception as e:
        checks.append(("Harness DB", False, f"连接失败: {str(e)[:80]}", None))


def _probe_mind_health(checks: list, config: dict):
    """L0: Check Meta-ToM pipeline — last fusion session and lag."""
    try:
        from pathlib import Path as _Path
        db_path = _Path(config["harness"]["db_path"])
        if not db_path.exists():
            return
        import sqlite3, time as _time
        conn = sqlite3.connect(str(db_path), timeout=2)
        # Last fusion session
        cur = conn.execute(
            "SELECT session_id, session_quality, created_at FROM fusion_sessions "
            "ORDER BY created_at DESC LIMIT 1"
        )
        row = cur.fetchone()
        if row:
            sid, quality, created = row
            lag_hours = (_time.time() - (created or 0)) / 3600
            if lag_hours < 2:
                detail = f"最近: {quality:.2f} quality ({sid[:20]}..., {lag_hours:.1f}h前)"
                passed = True
            elif lag_hours < 24:
                detail = f"最近: {quality:.2f} quality ({sid[:20]}..., {lag_hours:.0f}h前) ⚠️ 延迟偏高"
                passed = True
            else:
                detail = f"最近: {quality:.2f} quality — 已 {lag_hours:.0f}h 无更新 ⚠️"
                passed = False
            checks.append(("Meta-ToM 管线", passed, detail, None))
        else:
            checks.append(("Meta-ToM 管线", False, "无融合记录 — 管线可能从未运行", None))
        conn.close()
    except Exception as e:
        checks.append(("Meta-ToM 管线", False, f"查询失败: {str(e)[:80]}", None))


# ── Critical hook definitions ──────────────────────────────────────────────
# Hooks that the harness does NOT own but must verify exist.
# Format: {hook_event: {"command_fragment": "unique substring in command", "label": "human name"}}
_CRITICAL_FOREIGN_HOOKS: dict = {
    "Stop": {
        "command_fragment": "claude-code-notifier",
        "label": "claude-code-notifier (Stop → 桌面通知: 回复完成)",
    },
    "Notification": {
        "command_fragment": "claude-code-notifier",
        "label": "claude-code-notifier (Notification → 桌面通知: 等待输入)",
    },
}

# Hook events the harness manages — these must NOT be dropped when harness writes config.
_HARNESS_MANAGED_HOOK_EVENTS: frozenset = frozenset({
    "UserPromptSubmit", "SessionStart", "PreToolUse", "PostToolUse",
})


def _load_all_hooks() -> dict:
    """Merge hooks from settings.json and settings.local.json.

    settings.local.json takes precedence per Claude Code merge semantics.
    Returns a dict of {hook_event: [entries]}.
    """
    all_hooks: dict = {}
    for path in (
        Path.home() / ".claude" / "settings.json",
        Path.home() / ".claude" / "settings.local.json",
    ):
        try:
            if path.exists():
                s = json.loads(path.read_text(encoding="utf-8"))
                for event, entries in s.get("hooks", {}).items():
                    all_hooks[event] = entries
        except Exception:
            pass
    return all_hooks


def _probe_hook_integrity(checks: list):
    """L0: Verify critical non-harness hooks exist in settings.

    Reads both settings.json and settings.local.json (merged).
    Reports any missing critical foreign hooks.
    Does NOT auto-fix — harness does not own these hooks.
    """
    all_hooks = _load_all_hooks()
    for event, spec in _CRITICAL_FOREIGN_HOOKS.items():
        entries = all_hooks.get(event, [])
        found = False
        for entry in entries:
            for h in entry.get("hooks", []):
                cmd = h.get("command", "")
                if spec["command_fragment"] in cmd:
                    found = True
                    break
            if found:
                break
        if not found:
            checks.append((
                "关键Hook缺失",
                False,
                f"{event} Hook 未指向 {spec['label']}。"
                "桌面通知将不会触发。需手动恢复。",
                None,
            ))


def _preserve_foreign_hooks_before_write(settings_path: Path) -> dict:
    """Read existing settings, return the foreign hooks that must be preserved.

    Call BEFORE writing to a settings file. Returns a dict of
    {hook_event: [entries]} that the harness must NOT strip.

    Used as a safety net: if any harness code rewrites the hooks section,
    it must merge these back in.
    """
    foreign: dict = {}
    try:
        if settings_path.exists():
            s = json.loads(settings_path.read_text(encoding="utf-8"))
            for event, entries in s.get("hooks", {}).items():
                if event not in _HARNESS_MANAGED_HOOK_EVENTS:
                    foreign[event] = entries
    except Exception:
        pass
    return foreign


def _probe_data_sources(checks: list):
    """Probe WebFetch + MCP data source health. L0 report-only.

    Design:
      - SSL probe (Python ssl module) — detects UNEXPECTED_EOF, CERTIFICATE_VERIFY_FAILED, etc.
      - HTTP fallback — confirms 502 or other server-side errors.
      - Timeout: 3s per source, 15s total budget.
      - MCP check: read .mcp.json for server entries, verify they don't use wrapper.

    Output: appends (name, passed, detail, None) tuples to checks list.
    """
    import ssl as _ssl
    import socket as _socket

    SOURCES = [
        ("中国政府网 (gov.cn)", "www.gov.cn", "https://www.gov.cn/"),
        ("光明网 (gmw.cn)", "www.gmw.cn", "https://www.gmw.cn/"),
        ("36氪 (36kr.com)", "36kr.com", "https://36kr.com/newsflashes"),
        ("财联社 (cls.cn)", "www.cls.cn", "https://www.cls.cn/telegraph"),
        ("人民网 (people.com.cn)", "www.people.com.cn", "https://www.people.com.cn/"),
        ("TechNode (technode.com)", "technode.com", "https://technode.com/"),
    ]

    for label, host, _url in SOURCES:
        try:
            passed = False
            detail = ""
            ctx = _ssl.create_default_context()
            with ctx.wrap_socket(_socket.socket(), server_hostname=host) as s:
                s.settimeout(3)
                s.connect((host, 443))
                s.getpeercert()  # verify handshake complete
            passed = True
            detail = "HTTPS OK"
        except _socket.timeout:
            detail = "HTTPS 超时 (3s)"
        except OSError as e:
            err_str = str(e)
            if "UNEXPECTED_EOF" in err_str.upper() or "EOF" in err_str.upper():
                # Server-side TLS issue — probe HTTP to confirm
                try:
                    with _socket.create_connection((host, 80), timeout=3) as sock:
                        sock.sendall(
                            f"GET / HTTP/1.0\r\nHost: {host}\r\nUser-Agent: HarnessPreflight/1.0\r\n\r\n"
                            .encode()
                        )
                        response = sock.recv(1024).decode("latin-1", errors="replace")
                        if "502" in response[:50] or "503" in response[:50]:
                            detail = f"服务端宕机 — TLS EOF + HTTP {response.split()[1] if len(response.split())>1 else '502/503'}"
                        else:
                            detail = f"TLS握手失败 ({err_str[:60]}) — HTTP可达但非正常响应"
                except Exception:
                    detail = f"服务端宕机 — TLS EOF + HTTP不可达 ({err_str[:60]})"
            elif "CERTIFICATE" in err_str.upper():
                detail = f"证书验证失败 ({err_str[:80]})"
            else:
                detail = f"连接失败 ({err_str[:80]})"
        except Exception as e:
            detail = f"未知错误 ({str(e)[:80]})"

        checks.append((label, passed, detail, None))

    # MCP connectivity check — read .mcp.json, verify no wrapper
    try:
        mcp_paths = [
            Path("D:/Claude/.mcp.json"),
            Path.home() / ".claude" / "mcp.json",
        ]
        mcp_servers = {}
        wrapper_count = 0
        total = 0
        for mcp_path in mcp_paths:
            if mcp_path.exists():
                mcp_config = json.loads(mcp_path.read_text(encoding="utf-8"))
                for name, svr in mcp_config.get("mcpServers", {}).items():
                    total += 1
                    args_str = json.dumps(svr.get("args", []))
                    if "mcp_wrapper.py" in args_str:
                        wrapper_count += 1
                        mcp_servers[name] = "wrapper"
                    else:
                        mcp_servers[name] = "direct"
                break
        if total > 0:
            healthy = total - wrapper_count
            if wrapper_count > 0:
                checks.append(("MCP 连接状态", False,
                    f"{healthy}/{total} 直连正常, {wrapper_count} 个仍走 wrapper", None))
            else:
                checks.append(("MCP 连接状态", True,
                    f"{healthy}/{total} 全部直连正常", None))
    except Exception:
        pass


def _inject_preflight_checks(output: InjectorOutput, config: dict):
    """Execute preflight checks and inject. Deprecated in favor of cmd_inject's
    pre-run + _inject_preflight_checks_from_cache. Kept for external callers."""
    config_path = HARNESS_DIR / "harness_config.yaml"
    checks = _run_preflight_checks(config, config_path)
    _inject_preflight_checks_from_cache(output, checks)


def _inject_preflight_checks_from_cache(output: InjectorOutput, checks: list):
    """Inject preflight results from already-run checks.

    Priority: 0.95 (highest). Line budget: 8 max (increased from 5 for data source health).
    """

    try:
        fixed = [(n, d) for n, passed, d, fs in checks if fs == "fixed"]
        failed = [(n, d) for n, passed, d, fs in checks if fs == "failed"]
        unfixed = [(n, d) for n, passed, d, fs in checks if not passed and fs is None]

        lines = []

        if fixed:
            names = ", ".join(n for n, _ in fixed)
            lines.append(f"- ✅ 已自动修复: {names}")
        if failed:
            for name, detail in failed[:3]:
                lines.append(f"- ⚠️ 修复失败: **{name}** — {detail}")

        if unfixed:
            for name, detail in unfixed[:8]:  # bumped from 5 to 8 for data sources
                lines.append(f"- ❌ **{name}**: {detail}")
            if len(unfixed) > 8:
                lines.append(f"- ... 及其他 {len(unfixed) - 8} 项需要关注")

        if lines:
            tag = "Preflight" if not fixed else f"Preflight (✅ {len(fixed)} 项已修复)"
            output.add(0.95, f"## ⚠️ {tag}", lines)
    except Exception:
        pass


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


# ─── analyze ──────────────────────────────────────────────────────────

def cmd_analyze(lookback_days: int = 7):
    """Cross-session aggregation — compute patterns invisible to single-session observer.

    Outputs:
      - Session & observation counts
      - Recurring failure patterns with hit rates
      - Tool/source reliability scores
      - Skill usage vs. registration ratio
      - Unreviewed skill queue health
    """
    config_path = HARNESS_DIR / "harness_config.yaml"
    config = load_config(config_path)
    db_path = Path(config["harness"]["db_path"])

    if not db_path.exists():
        print("[harness] No database found. Run observe first.")
        return

    from indexer import HarnessDB
    db = HarnessDB(db_path)
    cutoff = time.time() - (lookback_days * 86400)

    print(f"\n{'='*60}")
    print(f"  Cross-Session Analysis — past {lookback_days} days")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    # ── 1. Session & observation counts ──
    try:
        cur = db._conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT session_id) FROM observations "
            "WHERE processed_at > ?", (cutoff,)
        )
        obs_count, sess_count = cur.fetchone()
        print(f"── Volume ──")
        print(f"  Sessions processed : {sess_count or 0}")
        print(f"  Observations       : {obs_count or 0}")

        cur = db._conn.execute(
            "SELECT action, COUNT(*) FROM observations "
            "WHERE processed_at > ? GROUP BY action ORDER BY COUNT(*) DESC",
            (cutoff,)
        )
        for row in cur.fetchall():
            print(f"    {row[0]}: {row[1]}")
        print()
    except Exception as e:
        print(f"  (observations query failed: {e})\n")

    # ── 2. Tool reliability scores ──
    try:
        cur = db._conn.execute(
            "SELECT tool_name, COUNT(*) as total, "
            "SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) as errors "
            "FROM tool_call_log WHERE timestamp > ? "
            "GROUP BY tool_name HAVING total >= 3 "
            "ORDER BY total DESC",
            (cutoff,)
        )
        tool_rows = cur.fetchall()
        if tool_rows:
            print("── Tool Reliability ──")
            for tool, total, errors in tool_rows:
                errors = errors or 0
                rate = errors / total
                flag = " ⚠️" if rate > 0.25 else ""
                bar = _reliability_bar(1.0 - rate)
                print(f"  {tool:20s} {bar} {total - errors}/{total} ({rate:.0%} fail){flag}")
            print()
    except Exception:
        pass  # tool_call_log table may not exist yet

    # ── 3. Failure pattern recurrence ──
    try:
        cur = db._conn.execute(
            "SELECT tag, COUNT(*) as hit_count, AVG(confidence) as avg_conf "
            "FROM fragments WHERE fragment_type='failure_pattern' "
            "GROUP BY tag ORDER BY hit_count DESC LIMIT 10"
        )
        fp_rows = cur.fetchall()
        if fp_rows:
            print("── Failure Pattern Registry ──")
            for tag, hits, conf in fp_rows:
                print(f"  {tag:35s} hits={hits}  avg_conf={conf:.2f}")
            print()
    except Exception:
        pass

    # ── 3.5. Belief trace recurrence (cross-session) ──
    try:
        cur = db._conn.execute(
            "SELECT belief_type, COUNT(*) as total, "
            "COUNT(DISTINCT session_id) as sessions, "
            "AVG(confidence) as avg_conf, "
            "SUM(CASE WHEN was_correct=1 THEN 1 ELSE 0 END) as verified "
            "FROM belief_traces "
            "WHERE created_at > ? "
            "GROUP BY belief_type "
            "ORDER BY total DESC",
            (cutoff,)
        )
        bt_rows = cur.fetchall()
        if bt_rows:
            print("── Belief Trace Recurrence (cross-session) ──")
            for btype, total, sessions, conf, verified in bt_rows:
                verified = verified or 0
                recurring_flag = " ⚠️ RECURRING" if total >= 3 and sessions >= 2 else ""
                unverified_flag = " [UNVERIFIED]" if total > 0 and verified == 0 else ""
                print(f"  {btype:30s} {total:3d} traces × {sessions} sessions"
                      f"  avg_conf={conf:.2f}  verified={verified}/{total}{recurring_flag}{unverified_flag}")
            print()
    except Exception:
        pass

    # ── 4. Skill usage health ──
    try:
        cur = db._conn.execute(
            "SELECT COUNT(*), SUM(usage_count), "
            "SUM(CASE WHEN usage_count > 0 THEN 1 ELSE 0 END) "
            "FROM skill_index"
        )
        total_skills, total_uses, used_skills = cur.fetchone()
        if total_skills:
            print("── Skill Health ──")
            print(f"  Registered : {total_skills}")
            print(f"  Ever used  : {used_skills or 0}/{total_skills}")
            print(f"  Total uses : {total_uses or 0}")
            # Skills never used
            cur = db._conn.execute(
                "SELECT name FROM skill_index WHERE usage_count=0 OR usage_count IS NULL"
            )
            unused = cur.fetchall()
            if unused:
                names = ", ".join(r[0].split("_", 2)[-1] if "_" in r[0] else r[0]
                                 for r in unused[:5])
                print(f"  Never used : {names}")
            print()
    except Exception:
        pass

    # ── 5. Pending review queue health ──
    try:
        skills_dir = HARNESS_DIR / "skills"
        pending = _list_pending_skills(skills_dir)
        now = time.time()
        stale = 0
        for sp in pending:
            if (now - sp.stat().st_mtime) / 86400 > 7:
                stale += 1
        print("── Review Queue ──")
        print(f"  Pending : {len(pending)}")
        print(f"  Stale (>7d) : {stale}")
        print()
    except Exception:
        pass

    # ── 6. Component Ablation (marginal contribution) ──
    _run_ablation_analysis(db, cutoff)

    db.close()
    print(f"{'='*60}\n")


def _run_ablation_analysis(db, cutoff: float):
    """Estimate each component's marginal contribution to session outcomes.

    Retrospective analysis from existing data — not a controlled experiment.
    Reports correlations and patterns, not causal claims. The output tells you
    which component deserves deeper investigation, not which one "works."

    Four analyses:
      A. Constraint registry — do blocked patterns correlate with tool failures?
      B. PreThink — does situation classification match session outcome?
      C. Attention fuser — are weights converging? does convergence help?
      D. Skill effectiveness — do sessions with skills have fewer failures?
    """
    print("── Component Ablation (marginal contribution estimates) ──")
    print("  (retrospective correlation analysis, not controlled experiment)")
    print()

    # A: Constraint registry — check if blocked patterns were real failures
    _ablation_constraints(db, cutoff)

    # B: PreThink — classification vs outcome alignment
    _ablation_prethink(db, cutoff)

    # C: Attention fuser — weight convergence vs quality trend
    _ablation_attention(db, cutoff)

    # D: Skill effectiveness — with-skill vs without-skill failure rates
    _ablation_skills(db, cutoff)

    print()


def _ablation_constraints(db, cutoff: float):
    """A: Constraint registry — blocked patterns vs actual failure history."""
    try:
        cur = db._conn.execute(
            "SELECT tool_name, match_pattern, violation_count, created_at "
            "FROM constraints WHERE active=1 AND created_at > ?", (cutoff,)
        )
        constraints = cur.fetchall()
        if not constraints:
            print("  [A] Constraint registry: no active constraints in window")
            return

        total_blocked = 0
        confirmed_failures = 0
        for tool, pattern, violations, created in constraints:
            total_blocked += (violations or 0)
            # Check tool_call_log for this pattern before constraint creation
            try:
                cur2 = db._conn.execute(
                    "SELECT COUNT(*), SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) "
                    "FROM tool_call_log WHERE tool_name=? AND timestamp < ?",
                    (tool, created),
                )
                row = cur2.fetchone()
                if row and row[0]:
                    total, errors = row
                    errors = errors or 0
                    if total >= 3 and errors / total > 0.5:
                        confirmed_failures += 1
            except Exception:
                pass

        precision = confirmed_failures / max(len(constraints), 1)
        print(f"  [A] Constraint registry: {len(constraints)} active, "
              f"{total_blocked} total violations blocked")
        print(f"      Pattern confirmed as high-failure: {confirmed_failures}/{len(constraints)} "
              f"(precision ~{precision:.0%})")
        if precision < 0.5:
            print(f"      ⚠️  Low precision — constraints may be blocking healthy calls")
    except Exception:
        pass


def _ablation_prethink(db, cutoff: float):
    """B: PreThink — how often does situation match eventual session outcome?"""
    try:
        cur = db._conn.execute(
            "SELECT tags, action FROM observations WHERE processed_at > ?",
            (cutoff,)
        )
        rows = cur.fetchall()
        if not rows:
            return

        align = 0
        total = 0
        mismatches = []
        for tags_json, action in rows:
            try:
                tags = json.loads(tags_json) if tags_json else []
            except Exception:
                tags = []
            prethink = ""
            for t in tags:
                if t.startswith("prethink:"):
                    prethink = t.split(":", 1)[1]
                    break
            if not prethink:
                continue
            total += 1
            # Alignment rules:
            # PreThink "correction" / "recurring_failure" → action should be patch_skill/create_skill
            # PreThink "routine" → action should be skip/save_fragment
            if prethink in ("correction", "recurring_failure"):
                if action in ("patch_skill", "create_skill"):
                    align += 1
                else:
                    mismatches.append(f"{prethink}→{action}")
            elif prethink == "routine":
                if action in ("skip", "save_fragment"):
                    align += 1
                else:
                    mismatches.append(f"{prethink}→{action}")
            elif prethink == "exploration":
                if action in ("create_skill", "save_fragment"):
                    align += 1
                else:
                    mismatches.append(f"{prethink}→{action}")

        if total > 0:
            print(f"  [B] PreThink: {align}/{total} align ({align/max(total,1):.0%}) "
                  f"between situation→action prediction")
            if mismatches:
                from collections import Counter
                top_mismatches = Counter(mismatches).most_common(3)
                print(f"      Top mismatches: {', '.join(f'{k}({v})' for k,v in top_mismatches)}")
    except Exception:
        pass


def _ablation_attention(db, cutoff: float):
    """C: Attention fuser — weight convergence and quality correlation."""
    try:
        cur = db._conn.execute(
            "SELECT alphas, attention_distribution, session_quality "
            "FROM fusion_sessions WHERE created_at > ? "
            "ORDER BY created_at ASC",
            (cutoff,)
        )
        rows = cur.fetchall()
        if len(rows) < 3:
            return

        qualities = []
        variances = []
        for alphas_json, attn_json, quality in rows:
            try:
                alphas = json.loads(alphas_json) if alphas_json else {}
                values = [v for v in alphas.values() if isinstance(v, (int, float))]
                if values and len(values) >= 2:
                    import statistics
                    variances.append(statistics.variance(values))
                else:
                    variances.append(0.0)
            except Exception:
                variances.append(0.0)
            qualities.append(quality or 0.0)

        # Trend: are weights converging (variance decreasing)?
        mid = len(variances) // 2
        early_var = sum(variances[:mid]) / max(mid, 1)
        late_var = sum(variances[mid:]) / max(len(variances) - mid, 1)
        converging = late_var < early_var

        # Correlation: does lower variance correlate with higher quality?
        if len(qualities) >= 5 and len(variances) >= 5 and max(variances) > 1e-10:
            import numpy as np
            try:
                corr = np.corrcoef(variances, qualities)[0, 1]
                corr = 0.0 if np.isnan(corr) else float(corr)
            except Exception:
                corr = 0.0
        else:
            corr = 0.0

        print(f"  [C] Attention fuser: {len(rows)} fusion sessions")
        print(f"      Weight convergence: {'converging' if converging else 'diverging'} "
              f"(variance early={early_var:.4f} → late={late_var:.4f})")
        print(f"      Variance~quality correlation: r={corr:+.3f} "
              f"({'weights more focused = better quality' if corr < -0.2 else 'no clear relationship' if abs(corr) < 0.2 else 'higher variance = better quality (unexpected)'})")

        # Top/bottom sources
        if rows:
            try:
                last_alphas = json.loads(rows[-1][0]) if rows[-1][0] else {}
                sorted_src = sorted(last_alphas.items(), key=lambda x: -x[1])
                if sorted_src:
                    top = sorted_src[0]
                    bottom = sorted_src[-1]
                    src_labels = {"user_msg": "用户意图", "claude_behavior": "行为模式",
                                  "session_tags": "领域标签", "history_summary": "历史摘要",
                                  "memory_entries": "记忆条目"}
                    print(f"      Strongest source: {src_labels.get(top[0], top[0])} "
                          f"(α={top[1]:.3f})")
                    if bottom[1] < 0.05:
                        print(f"      Dormant source: {src_labels.get(bottom[0], bottom[0])} "
                              f"(α={bottom[1]:.3f} — near archive threshold)")
            except Exception:
                pass
    except Exception:
        pass


def _ablation_skills(db, cutoff: float):
    """D: System trend — is the healthy/failure ratio improving over time?

    Healthy actions: save_fragment (clean task-workflow), skip (routine)
    Failure-driven actions: create_skill, patch_skill (correction/failure needed)
    Update_preference is neutral (not a failure, not a task).

    If the system is improving, the ratio should shift toward healthy sessions.
    Splits the window in half to compare early vs late periods.
    """
    try:
        cur = db._conn.execute(
            "SELECT action, processed_at FROM observations "
            "WHERE processed_at > ? ORDER BY processed_at ASC",
            (cutoff,)
        )
        rows = cur.fetchall()
        if len(rows) < 6:
            return

        mid = len(rows) // 2
        early = rows[:mid]
        late = rows[mid:]

        def _health_ratio(subset):
            healthy = sum(1 for a, _ in subset if a in ("save_fragment", "skip"))
            failure = sum(1 for a, _ in subset if a in ("create_skill", "patch_skill"))
            total = healthy + failure
            return healthy / max(total, 1)

        early_ratio = _health_ratio(early)
        late_ratio = _health_ratio(late)
        delta = late_ratio - early_ratio

        print(f"  [D] System health trend ({len(rows)} sessions):")
        print(f"      Early window: {early_ratio:.0%} healthy ({mid} sessions)")
        print(f"      Late window:  {late_ratio:.0%} healthy ({len(rows)-mid} sessions)")
        print(f"      Trend: {delta:+.0%} "
              f"({'improving' if delta > 0.05 else 'declining' if delta < -0.05 else 'stable'})")
    except Exception:
        pass


def _reliability_bar(rate: float, width: int = 10) -> str:
    """Mini ASCII bar chart for reliability visualization."""
    filled = int(round(rate * width))
    empty = width - filled
    if rate >= 0.9:
        bar_char = "█"
    elif rate >= 0.7:
        bar_char = "▓"
    elif rate >= 0.5:
        bar_char = "▒"
    else:
        bar_char = "░"
    return f"[{bar_char * filled}{' ' * empty}]"


# ─── diagnose ─────────────────────────────────────────────────────────

def cmd_diagnose(lookback_days: int = 7):
    """Produce a structured diagnostic report from Harness-collected data.

    Principle: data Claude cannot access on its own.
    No analysis, no recommendations — just numbers, trends, and status.
    Claude is perfectly capable of analysis once it has the data.
    """
    config_path = HARNESS_DIR / "harness_config.yaml"
    config = load_config(config_path)
    db_path = Path(config["harness"]["db_path"])
    cutoff = time.time() - (lookback_days * 86400)
    now = time.time()

    report: list[str] = []
    sep = "─" * 55

    def _h(title: str):
        report.append(f"\n  {title}")
        report.append(f"  {sep}")

    def _kv(k: str, v, flag: str = ""):
        flag_str = f"  {flag}" if flag else ""
        report.append(f"  {k:24s} {v}{flag_str}")

    def _bar(rate: float, w: int = 8) -> str:
        return _reliability_bar(rate, w)

    # ── Header ──
    report.append(f"Harness 诊断报告 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    report.append(f"回溯: {lookback_days} 天")

    # ── 1. Constraint Registry ──
    _h("约束注册表")
    try:
        conn = sqlite3.connect(str(db_path), timeout=3)
        cur = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN violation_count > 0 THEN 1 ELSE 0 END) "
            "FROM constraints WHERE active=1 AND (expires_at IS NULL OR expires_at > ?)",
            (now,)
        )
        active, violated = cur.fetchone()
        active = active or 0; violated = violated or 0

        cur = conn.execute(
            "SELECT COUNT(*) FROM constraints WHERE created_at > ?", (cutoff,)
        )
        created_this_week = cur.fetchone()[0]

        cur = conn.execute(
            "SELECT SUM(violation_count) FROM constraints WHERE active=1"
        )
        total_violations = cur.fetchone()[0] or 0

        conn.close()
        _kv("活跃约束", active)
        _kv("有违反记录", violated, "⚠️" if violated > 0 else "")
        _kv("总违反次数", total_violations, "🔴" if total_violations > 5 else "")
        _kv("本周创建", created_this_week)
        if active == 0:
            report.append("  (无活跃约束 — 注册表上线不足 24h 或无可复现失败)")
    except Exception as e:
        report.append(f"  查询失败: {e}")

    # ── 2. Preflight ──
    _h("Preflight")
    try:
        settings_path = Path.home() / ".claude" / "settings.local.json"
        if settings_path.exists():
            s = json.loads(settings_path.read_text(encoding="utf-8"))
            _kv("skipWebFetchPreflight", "✓" if s.get("skipWebFetchPreflight") else "✗")
        else:
            _kv("skipWebFetchPreflight", "文件不存在")

        wrapper_enabled = config.get("mcp_wrapper", {}).get("enabled", None)
        _kv("mcp_wrapper.enabled", wrapper_enabled,
            "⚠️" if wrapper_enabled else "✓ 已自动修复")
    except Exception as e:
        report.append(f"  查询失败: {e}")

    # MCP servers still using wrapper
    try:
        mcp_path = Path("D:/Claude/.mcp.json")
        if mcp_path.exists():
            mcp = json.loads(mcp_path.read_text(encoding="utf-8"))
            wrapper_servers = []
            for name, svr in mcp.get("mcpServers", {}).items():
                if "mcp_wrapper.py" in json.dumps(svr.get("args", [])):
                    wrapper_servers.append(name)
            if wrapper_servers:
                _kv("MCP wrapper 残留", ", ".join(wrapper_servers), "待 L2 修复")
    except Exception:
        pass

    # ── 3. Skill Pipeline ──
    _h("技能管线")
    try:
        active_dir = Path.home() / ".claude" / "skills"
        active_count = len(list(active_dir.glob("harness_*.md"))) if active_dir.exists() else 0
        archive_dir = HARNESS_DIR / "skills" / "archive"
        archive_count = len(list(archive_dir.glob("*.md"))) if archive_dir.exists() else 0
        pending_count = len(_list_pending_skills(HARNESS_DIR / "skills"))

        conn = sqlite3.connect(str(db_path), timeout=3)
        cur = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN usage_count > 0 THEN 1 ELSE 0 END) "
            "FROM skill_index"
        )
        registered, used = cur.fetchone()
        registered = registered or 0; used = used or 0
        conn.close()

        _kv("活跃", active_count)
        _kv("注册 (skill_index)", registered)
        _kv("曾使用", f"{used}/{registered}" if registered > 0 else "N/A",
            "⚠️ 0 使用率" if registered > 0 and used == 0 else "")
        _kv("待审查", pending_count)
        _kv("已归档", archive_count)
    except Exception as e:
        report.append(f"  查询失败: {e}")

    # ── 4. Tool Reliability ──
    _h("工具可靠性")
    try:
        conn = sqlite3.connect(str(db_path), timeout=3)
        cur = conn.execute(
            "SELECT tool_name, COUNT(*) as total, "
            "SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) as errors "
            "FROM tool_call_log WHERE timestamp > ? "
            "GROUP BY tool_name HAVING total >= 3 "
            "ORDER BY total DESC LIMIT 10",
            (cutoff,)
        )
        rows = cur.fetchall()
        conn.close()
        if rows:
            for tool, total, errors in rows:
                errors = errors or 0
                rate = 1.0 - (errors / total)
                flag = " ⚠️" if rate < 0.7 else " 🔴" if rate < 0.5 else ""
                _kv(tool, f"{_bar(rate)} {total-errors}/{total} ({rate:.0%})", flag)
        else:
            report.append("  (无足够数据 — tool_call_log 可能为空)")
    except Exception as e:
        report.append(f"  查询失败: {e}")

    # ── 5. Observer Signal Quality ──
    _h("Observer 信号质量")
    try:
        conn = sqlite3.connect(str(db_path), timeout=3)
        cur = conn.execute(
            "SELECT action, COUNT(*) FROM observations "
            "WHERE processed_at > ? GROUP BY action", (cutoff,)
        )
        action_counts = {r[0]: r[1] for r in cur.fetchall()}
        total_obs = sum(action_counts.values())

        cur = conn.execute(
            "SELECT COUNT(DISTINCT session_id) FROM observations WHERE processed_at > ?",
            (cutoff,)
        )
        session_count = cur.fetchone()[0] or 0
        conn.close()

        _kv("Sessions", session_count)
        _kv("Observations", total_obs)
        create = action_counts.get("create_skill", 0)
        _kv("  create_skill", create,
            "⚠️ 偏高" if session_count > 0 and create / session_count > 0.3 else "")
        _kv("  patch_skill", action_counts.get("patch_skill", 0))
        _kv("  save_fragment", action_counts.get("save_fragment", 0))
        _kv("  skip", action_counts.get("skip", 0))

        threshold = config.get("observer", {}).get("min_tool_calls_for_skill", 8)
        _kv("create 阈值", f"{threshold} tool calls")
    except Exception as e:
        report.append(f"  查询失败: {e}")

    # ── 6. Memory System ──
    _h("记忆系统")
    try:
        memory_dir = Path.home() / ".claude" / "projects" / "D--Claude" / "memory"
        if memory_dir.exists():
            md_files = list(memory_dir.glob("*.md"))
            _kv("条目数", len(md_files))
            total_size = sum(f.stat().st_size for f in md_files)
            _kv("总大小", f"{total_size / 1024:.0f} KB")

            new_this_week = sum(
                1 for f in md_files if (now - f.stat().st_mtime) < lookback_days * 86400
            )
            _kv("本周新增/更新", new_this_week)

            # Read MEMORY.md to get the index size
            index_path = memory_dir / "MEMORY.md"
            if index_path.exists():
                lines = [l for l in index_path.read_text(encoding="utf-8").split("\n")
                        if l.startswith("- [")]
                _kv("MEMORY.md 索引行", len(lines))
        else:
            report.append("  目录不存在")
    except Exception as e:
        report.append(f"  查询失败: {e}")

    # ── 7. Config Health ──
    _h("配置一致性")
    checks = []
    # Check for known contradictions
    if config.get("mcp_wrapper", {}).get("enabled"):
        checks.append("mcp_wrapper.enabled=true 与已知失败矛盾")
    if not config.get("injector", {}).get("preflight_enabled", True):
        checks.append("preflight 注入被禁用")
    observer_cfg = config.get("observer", {})
    if observer_cfg.get("min_tool_calls_for_skill", 8) < 5:
        checks.append(f"create_skill 阈值偏低 ({observer_cfg.get('min_tool_calls_for_skill')})")
    if checks:
        for c in checks:
            report.append(f"  ⚠️ {c}")
    else:
        report.append("  ✓ 未检测到配置矛盾")

    report.append("")
    print("\n".join(report))


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

            # Sandbox verification before deployment
            try:
                from sandbox_verifier import verify_skill
                db_path_review = Path(config["harness"]["db_path"])
                sr = verify_skill(skill_path, db_path_review)
                print(f"\n[harness] Sandbox verification for {skill_path.name}:")
                print(f"  Risk: {sr.risk_level.upper()}")
                for check in sr.checks:
                    status = "PASS" if check.passed else "FAIL"
                    print(f"  [{status}] {check.check_name}: {check.detail[:120]}")
                if sr.risk_level == "high":
                    print(f"\n[harness] WARNING: High risk detected for {skill_path.name}!")
                    resp = input("    Confirm approve? (y/N): ").strip().lower()
                    if resp != "y":
                        print("[harness] Approval cancelled.")
                        return
            except Exception as e:
                if config.get("injector", {}).get("verbose", False):
                    print(f"[harness] Sandbox verifier error (non-fatal): {e}")

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

def cmd_news_analyze(date_str: str | None = None, phases_str: str = "1"):
    """Run the news analysis pipeline for a specific date.

    Phases:
      1 — news_vectorizer + feature_library + feature_finder
      2 — attention_injector (3-layer pooling)
      3 — coactivation_detector + competing_hypotheses
      4 — ICL compression
      5 — DCL compression
    """
    from indexer import HarnessDB
    from news_vectorizer import parse_news_file, vectorize_snippets
    from feature_library import load_feature_library, compute_activation_matrix
    from feature_finder import find_features, store_feature_activations
    from pathlib import Path

    phases = set(int(p) for p in phases_str.split(",") if p.strip().isdigit())
    if not phases:
        phases = {1}

    db = HarnessDB()
    vault_news = Path(r"C:\Users\Chucky\Documents\Obsidian Vault\claude专属文件夹\news")

    # Determine date
    if date_str:
        news_file = vault_news / f"{date_str}.md"
        if not news_file.exists():
            print(f"[news-analyze] File not found: {news_file}")
            db.close()
            return
    else:
        from datetime import date
        date_str = date.today().isoformat()
        news_file = vault_news / f"{date_str}.md"
        if not news_file.exists():
            # Try yesterday
            from datetime import timedelta
            yesterday = (date.today() - timedelta(days=1)).isoformat()
            news_file = vault_news / f"{yesterday}.md"
            date_str = yesterday
            if not news_file.exists():
                print(f"[news-analyze] No news file found for {date.today().isoformat()} or {yesterday}")
                db.close()
                return

    print(f"[news-analyze] Processing {news_file.name} (phases: {sorted(phases)})")

    # ── Phase 1: Foundation ───────────────────────────────────────────────
    if 1 in phases:
        print("[phase 1] Parsing news file...")
        snippets = parse_news_file(news_file)
        print(f"  Parsed {len(snippets)} snippets")

        # Vectorize
        print("[phase 1] Vectorizing snippets...")
        vectorize_snippets(snippets, db)
        print(f"  Embedded and stored {len(snippets)} snippets")

        # Load feature library
        print("[phase 1] Loading feature library...")
        entries, combo_map = load_feature_library(db)
        print(f"  {len(entries)} feature library entries, {len(combo_map)} combo patterns")

        # FeatureFinder
        print("[phase 1] Running FeatureFinder...")
        snippet_dicts = [
            {"id": i, "date": s.date, "headline": s.headline,
             "entities": s.entities, "embedding": s.embedding}
            for i, s in enumerate(snippets) if s.embedding
        ]
        # Convert FeatureLibraryEntry objects to dicts for find_features
        entry_dicts = [
            {"feature_id": e.feature_id, "layer": e.layer,
             "category": e.category, "name_cn": e.name_cn,
             "definition": e.definition, "examples": e.examples,
             "typical_implication": e.typical_implication,
             "embedding": e.embedding}
            for e in entries
        ]
        anomalies = find_features(snippet_dicts, db, feature_lib_entries=entry_dicts)
        print(f"  {len(anomalies)} candidate anomalies detected")

        # Store activations
        store_feature_activations(date_str, snippet_dicts, anomalies, db)
        print("  Feature activations stored")

        # Display top anomalies
        if anomalies:
            print("\n  Top anomalies:")
            for a in anomalies[:5]:
                entry = next((e for e in entries if e.feature_id == a.matched_library_feature), None)
                name = entry.name_cn if entry else a.matched_library_feature
                print(f"    {a.matched_library_feature} {name}: "
                      f"conf={a.detection_confidence:.3f} "
                      f"p={a.statistical_significance:.4e} "
                      f"d={a.effect_size:.3f}")

    # ── Phase 2: Attention Injection ──────────────────────────────────────
    if 2 in phases:
        print("[phase 2] Running attention injection...")
        from attention_injector import run_phase2, format_overflow_archive

        report = run_phase2(date_str, db)
        print(f"  Surface: {report.surface_count}")
        print(f"  Structural: {report.structural_count} ({report.boosted_count} boosted)")
        print(f"  Latent (bias): {report.latent_count}")
        print(f"  Injected: {report.budget_used}/{report.budget_total}")
        print(f"  Overflow: {len(report.overflow)}")
        print()

        # Store injection in DB
        date_ = report.date
        injection_json = report.injection_text
        for f in report.injected:
            db._conn.execute(
                """INSERT INTO news_attention_injections
                   (date, layer, injection_text, feature_ids, weight_boost_applied,
                    budget_used, budget_total)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (date_, f.layer, injection_json,
                 json.dumps([fe.feature_id for fe in report.injected]),
                 1 if f.latent_boosted else 0,
                 report.budget_used, report.budget_total),
            )
        db._conn.commit()

        # Show injection text
        print(report.injection_text)

        # Archive overflow if needed
        if report.overflow:
            archive = format_overflow_archive(report)
            archive_path = Path(r"C:\Users\Chucky\Documents\Obsidian Vault\claude专属文件夹\news\signal-archive")
            archive_path.mkdir(parents=True, exist_ok=True)
            archive_file = archive_path / f"{date_str}.json"
            archive_file.write_text(json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\n  Overflow archive: {archive_file}")

    # ── Phase 3: Co-activation + Hypotheses ───────────────────────────
    if 3 in phases:
        print("[phase 3] Running co-activation + hypotheses engine...")
        from coactivation_detector import update_cycle, get_active_verification_queries
        from competing_hypotheses import run_hypothesis_cycle

        fid_scores: dict[str, float] = {}
        acts = db.get_feature_activations(days=1)
        acts = [a for a in acts if a.get("date") == date_str]
        for a in acts:
            fid = a["feature_id"]
            fid_scores[fid] = max(fid_scores.get(fid, 0), a.get("activation_strength", 0))

        from dataclasses import dataclass as dc
        @dc
        class _SA:
            matched_library_feature: str
            detection_confidence: float
        top_anomalies = [_SA(fid, score)
                        for fid, score in sorted(fid_scores.items(), key=lambda x: -x[1])[:7]]

        pairs = update_cycle(date_str, db)
        print(f"  Co-activation pairs: {len(pairs)}")
        queries = get_active_verification_queries(db, limit=3)
        if queries:
            print(f"  Verification queries: {len(queries)}")

        snips = [{"id": s.get("id"), "headline": s.get("headline", ""),
                   "entities": s.get("entities", []), "embedding": s.get("embedding"),
                   "sources": s.get("sources", []), "section": s.get("section", "")}
                  for s in db.get_news_snippets(date=date_str)]

        hypotheses = run_hypothesis_cycle(top_anomalies, snips, db)
        print(f"  Active hypotheses: {len(hypotheses)}")
        surviving = [h for h in hypotheses if h.status in ("seeded", "testing", "revising")]
        if surviving:
            print(f"  Surviving ({len(surviving)}):")
            for h in sorted(surviving, key=lambda x: x.aggregate_rank, reverse=True)[:5]:
                print(f"    {h.hypothesis_id} [{h.status}] r={h.aggregate_rank:.3f} | {h.statement[:70]}...")
        popped = [h for h in hypotheses if h.status == "popped"]
        if popped:
            print(f"  False structures filtered: {len(popped)}")

    # ── Phase 4: ICL Compression ──────────────────────────────────────
    if 4 in phases:
        print("[phase 4] Running ICL compression...")
        from icl_compressor import compress as icl_compress, format_injection as icl_format, format_archive

        # Gather all signals
        attention_injected_list = report.injected if 'report' in dir() else []
        anomaly_objs = [
            type('Anomaly', (), {'matched_library_feature': a.matched_library_feature,
                                 'detection_confidence': a.detection_confidence})
            for a in (anomalies if 'anomalies' in dir() else [])
        ] if 'anomalies' in dir() else []

        hyp_objs = hypotheses if 'hypotheses' in dir() else []
        fe_entries = db.get_feature_library_entries()

        icl_report = icl_compress(hyp_objs, anomaly_objs, attention_injected_list, fe_entries, date_str)
        print(f"  Input: {icl_report.input_count} → T1:{icl_report.tier1_count} T2:{icl_report.tier2_count} T3:{icl_report.tier3_count}")
        print(f"  Compression ratio: {icl_report.compression_ratio:.2f}")

        tier1, tier2 = icl_format(icl_report)
        if tier1:
            print(f"\n{tier1}")
        if tier2:
            print(f"\n{tier2}")

        # Archive Tier 3
        archive = format_archive(icl_report)
        archive_path = Path(r"C:\Users\Chucky\Documents\Obsidian Vault\claude专属文件夹\news\signal-archive")
        archive_path.mkdir(parents=True, exist_ok=True)
        archive_file = archive_path / f"icl-{date_str}.json"
        archive_file.write_text(json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n  ICL archive: {archive_file}")

        # Store in DB
        db.save_icl_report({
            "date": date_str,
            "input_count": icl_report.input_count,
            "tier1_count": icl_report.tier1_count,
            "tier2_count": icl_report.tier2_count,
            "tier3_count": icl_report.tier3_count,
            "compression_ratio": icl_report.compression_ratio,
            "tier1_items": icl_report.tier1_items,
            "tier2_items": icl_report.tier2_items,
        })

    # ── Phase 5: DCL Compression ──────────────────────────────────────
    if 5 in phases:
        print("[phase 5] Running DCL compression...")
        from dcl_compressor import compress as dcl_compress, format_injection as dcl_format

        dcl_cards = dcl_compress(icl_report if 'icl_report' in dir() else None, fe_entries, date_str)
        print(f"  Judgment cards: {len(dcl_cards)}")

        if dcl_cards:
            dcl_text = dcl_format(dcl_cards)
            print(f"\n{dcl_text}")

            for c in dcl_cards:
                db.save_dcl_judgment({
                    "judgment_id": c.judgment_id,
                    "date": c.date,
                    "judgment": c.judgment,
                    "confidence": c.confidence,
                    "disruptiveness": c.disruptiveness,
                    "supporting_hypotheses": c.supporting_hypotheses,
                    "counterfactual": c.counterfactual,
                    "action_implication": c.action_implication,
                    "verification_window": c.verification_window,
                    "causal_radius": c.causal_radius,
                    "page_rank": c.page_rank,
                })
            print(f"  Stored {len(dcl_cards)} judgment cards in DB")

    print(f"\n[news-analyze] Done. Use `python harness_daemon.py diagnose` for full report.")
    db.close()


def cmd_feature_lib(remaining: list[str]):
    """Feature library management subcommands.

    Usage:
      python harness_daemon.py feature-lib rollback --version N
      python harness_daemon.py feature-lib diff --v1 N --v2 M
      python harness_daemon.py feature-lib reload
    """
    from indexer import HarnessDB
    from feature_library import load_feature_library
    from pathlib import Path

    db = HarnessDB()
    lib_path = Path(r"C:\Users\Chucky\Documents\Obsidian Vault\claude专属文件夹\FEATURE LIBRARY V2.0.md")

    if not remaining:
        # Default: show status
        entries, _ = load_feature_library(db)
        layers = {}
        for e in entries:
            layers[e.layer] = layers.get(e.layer, 0) + 1
        print(f"Feature Library: {len(entries)} entries ({layers})")
        checksum = db.get_meta("feature_library_checksum")
        print(f"Current checksum: {checksum}")
        versions = db.get_feature_library_versions(limit=5)
        print(f"Stored versions: {len(versions)}")
        db.close()
        return

    if remaining[0] == "reload":
        db.set_meta("feature_library_checksum", "")
        entries, _ = load_feature_library(db, lib_path)
        print(f"Force-reloaded: {len(entries)} entries")
    elif remaining[0] == "rollback":
        # Parse --version N
        version = None
        for i, arg in enumerate(remaining):
            if arg == "--version" and i + 1 < len(remaining):
                version = int(remaining[i + 1])
        if version is None:
            print("Usage: python harness_daemon.py feature-lib rollback --version N")
            db.close()
            return
        versions = db.get_feature_library_versions(limit=100)
        target = next((v for v in versions if v["version_id"] == version), None)
        if target is None:
            print(f"Version {version} not found")
            db.close()
            return
        # Restore from snapshot
        entries_data = target["full_snapshot"].get("entries", [])
        db.save_feature_library_entries([
            {"feature_id": e["feature_id"], "layer": e["layer"],
             "category": e["category"], "name_cn": e["name_cn"],
             "definition": e.get("definition", ""),
             "examples": e.get("examples", ""),
             "typical_implication": e.get("typical_implication", ""),
             "embedding": e.get("embedding"), "checksum": target["checksum"]}
            for e in entries_data
        ])
        db.set_meta("feature_library_checksum", target["checksum"])
        print(f"Rolled back to version {version} (checksum: {target['checksum'][:12]}...)")
    elif remaining[0] == "diff":
        v1 = v2 = None
        for i, arg in enumerate(remaining):
            if arg == "--v1" and i + 1 < len(remaining):
                v1 = int(remaining[i + 1])
            if arg == "--v2" and i + 1 < len(remaining):
                v2 = int(remaining[i + 1])
        if v1 is None or v2 is None:
            print("Usage: python harness_daemon.py feature-lib diff --v1 N --v2 M")
            db.close()
            return
        versions = db.get_feature_library_versions(limit=100)
        ver_a = next((v for v in versions if v["version_id"] == v1), None)
        ver_b = next((v for v in versions if v["version_id"] == v2), None)
        if not ver_a or not ver_b:
            print("Version not found")
            db.close()
            return
        ids_a = {e["feature_id"] for e in ver_a["full_snapshot"].get("entries", [])}
        ids_b = {e["feature_id"] for e in ver_b["full_snapshot"].get("entries", [])}
        added = ids_b - ids_a
        removed = ids_a - ids_b
        if added:
            print(f"Added in v{v2}: {sorted(added)}")
        if removed:
            print(f"Removed in v{v2}: {sorted(removed)}")
        if not added and not removed:
            print("No entry differences between versions")

    db.close()


def main():
    parser = argparse.ArgumentParser(description="Harness daemon")
    parser.add_argument(
        "command",
        choices=["observe", "inject", "review", "analyze", "diagnose",
                 "cleanup", "status", "news-analyze", "feature-lib", "guardian"],
        nargs="?",
        default=None,
    )
    parser.add_argument(
        "--days", type=int, default=7,
        help="Lookback days for analyze command (default: 7)"
    )
    parser.add_argument(
        "--date", type=str, default=None,
        help="Specific date for news-analyze (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--phases", type=str, default="1",
        help="Phases to run for news-analyze (e.g. '1,2' or '1,2,3,4,5')"
    )
    # Pass remaining args to review subcommand
    args, remaining = parser.parse_known_args()

    try:
        if args.command == "observe":
            cmd_observe()
        elif args.command == "inject":
            cmd_inject()
        elif args.command == "review":
            cmd_review(remaining)
        elif args.command == "analyze":
            cmd_analyze(args.days)
        elif args.command == "diagnose":
            cmd_diagnose(args.days)
        elif args.command == "cleanup":
            cmd_cleanup()
        elif args.command == "status":
            cmd_status()
        elif args.command == "news-analyze":
            cmd_news_analyze(args.date, args.phases)
        elif args.command == "feature-lib":
            cmd_feature_lib(remaining)
        elif args.command == "guardian":
            # Delegate to independent guardian subprocess
            from harness_guardian import run_pulse, run_status, run_daemon
            if remaining and remaining[0] in ("pulse", "status", "daemon"):
                sub_cmd = remaining[0]
                if sub_cmd == "pulse":
                    run_pulse()
                elif sub_cmd == "status":
                    run_status()
                elif sub_cmd == "daemon":
                    interval = int(remaining[1]) if len(remaining) > 1 else 60
                    run_daemon(interval=interval)
            else:
                # Default: one-shot pulse
                run_pulse()
        else:
            parser.print_help()
    except Exception as e:
        print(f"[harness] Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
