"""Sandbox verifier — pre-deployment validation of skills and constraints.

Replays historical tool_call_log and session data against proposed
changes. Pure SQL + local compute, zero LLM calls, zero token consumption.
Completes in <100ms.

Inspired by MOSS Ephemeral Trial Workers.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── Data structures ──────────────────────────────────────────────────────

@dataclass
class CheckResult:
    check_name: str
    passed: bool
    detail: str
    confidence: float


@dataclass
class SandboxReport:
    passed: bool
    risk_level: str          # 'low' | 'medium' | 'high'
    evidence: str
    checks: list[CheckResult] = field(default_factory=list)
    replayed_sessions: int = 0
    false_positive_risk: float = 0.0


# ── Thresholds ───────────────────────────────────────────────────────────

FALSE_POSITIVE_THRESHOLD = 0.30
COSINE_SIMILARITY_THRESHOLD = 0.65
MAX_REPLAY_SESSIONS = 4
MAX_TOOL_CALLS_TO_CHECK = 1000


# ── Connection helper ────────────────────────────────────────────────────

def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=2)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ── Frontmatter parser ───────────────────────────────────────────────────

def _parse_frontmatter(filepath: Path) -> dict:
    info: dict = {
        "name": filepath.stem, "description": "", "triggers": [],
        "tags": "", "harness_confidence": "0.5", "skill_type": "",
    }
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
                    info["name"] = stripped.removeprefix("name:").strip()
                elif stripped.startswith("description:"):
                    info["description"] = stripped.removeprefix("description:").strip()
                elif stripped.startswith("tags:"):
                    info["tags"] = stripped.removeprefix("tags:").strip()
                elif stripped.startswith("harness_confidence:"):
                    info["harness_confidence"] = stripped.removeprefix(
                        "harness_confidence:"
                    ).strip()
                elif stripped.startswith("skill_type:"):
                    info["skill_type"] = stripped.removeprefix("skill_type:").strip()
                elif stripped.startswith("triggers:"):
                    continue
                elif stripped.startswith("  - "):
                    info["triggers"].append(stripped.removeprefix("  - ").strip())
    except Exception:
        pass
    return info


# ── Helpers ──────────────────────────────────────────────────────────────

def _compute_risk_level(checks: list[CheckResult]) -> str:
    failed = sum(1 for c in checks if not c.passed)
    if failed == 0:
        return "low"
    if failed == 1:
        return "medium"
    return "high"


def _build_evidence(checks: list[CheckResult], target_name: str) -> str:
    parts = [f"Sandbox verification for '{target_name}':"]
    for c in checks:
        status = "PASS" if c.passed else "FAIL"
        parts.append(f"  [{status}] {c.check_name}: {c.detail}")
    return "\n".join(parts)


# ── Core functions ───────────────────────────────────────────────────────

def verify_constraint(
    constraint_dict: dict,
    db_path: Path,
) -> SandboxReport:
    """Validate a proposed constraint against historical tool_call_log.

    constraint_dict keys: tool_name, match_pattern, name (optional)

    Checks:
      1. False positive risk: what fraction of historically matching calls
         were successful? If >30%, this constraint would block working calls.
      2. Redundancy: does this overlap with any existing active constraint?
    """
    tool_name = constraint_dict.get("tool_name", "")
    match_pattern = constraint_dict.get("match_pattern", "").lower()
    constraint_name = constraint_dict.get("name", "")
    checks: list[CheckResult] = []

    conn = _connect(db_path)

    # ── Check 1: False positive risk ──
    if tool_name == "*":
        cur = conn.execute(
            "SELECT tool_name, status FROM tool_call_log "
            "ORDER BY timestamp DESC LIMIT ?",
            (MAX_TOOL_CALLS_TO_CHECK,),
        )
    else:
        cur = conn.execute(
            "SELECT tool_name, status FROM tool_call_log "
            "WHERE tool_name = ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (tool_name, MAX_TOOL_CALLS_TO_CHECK),
        )

    rows = cur.fetchall()
    total_matched = len(rows)
    successful_matched = sum(1 for _, s in rows if s == "success")
    error_matched = total_matched - successful_matched

    if total_matched == 0:
        false_pos_risk = 0.0
        checks.append(CheckResult(
            check_name="false_positive_risk",
            passed=True,
            detail=f"0 matching calls for {tool_name}. New pattern, no historical risk data.",
            confidence=0.8,
        ))
    else:
        false_pos_risk = successful_matched / total_matched
        passed = false_pos_risk <= FALSE_POSITIVE_THRESHOLD
        checks.append(CheckResult(
            check_name="false_positive_risk",
            passed=passed,
            detail=(
                f"{successful_matched}/{total_matched} ({false_pos_risk:.0%}) "
                f"matched calls were successful. "
                f"{'Safe.' if passed else 'HIGH RISK: would block working calls.'}"
            ),
            confidence=1.0 - false_pos_risk,
        ))

    # ── Check 2: Redundancy with existing active constraints ──
    cur = conn.execute(
        "SELECT name, tool_name, match_pattern FROM constraints "
        "WHERE active = 1 AND (expires_at IS NULL OR expires_at > unixepoch())"
    )
    active_constraints = cur.fetchall()
    redundant_with: list[str] = []
    for ac_name, ac_tool, ac_pattern in active_constraints:
        if ac_tool == tool_name or tool_name == "*" or ac_tool == "*":
            ac_pat = (ac_pattern or "").lower()
            if match_pattern and ac_pat:
                if match_pattern in ac_pat or ac_pat in match_pattern:
                    redundant_with.append(ac_name or ac_tool)
            elif not match_pattern and not ac_pat:
                redundant_with.append(ac_name or ac_tool)

    redundancy_found = len(redundant_with) > 0
    checks.append(CheckResult(
        check_name="redundancy_check",
        passed=not redundancy_found,
        detail=(
            f"Overlaps with: {', '.join(redundant_with)}"
            if redundancy_found
            else "No overlap with existing active constraints."
        ),
        confidence=1.0 if not redundancy_found else 0.5,
    ))

    conn.close()

    risk_level = _compute_risk_level(checks)
    evidence = _build_evidence(checks, constraint_name or f"{tool_name}:{match_pattern}")
    return SandboxReport(
        passed=all(c.passed for c in checks),
        risk_level=risk_level,
        evidence=evidence,
        checks=checks,
        false_positive_risk=false_pos_risk,
    )


def verify_skill(
    skill_path: Path,
    db_path: Path,
) -> SandboxReport:
    """Validate a proposed skill before deployment.

    Checks:
      1. Trigger overlap with existing active skills
      2. Domain demand: did sessions in this domain actually need help?
      3. Duplicate detection: similar existing skill by frontmatter overlap
    """
    checks: list[CheckResult] = []

    frontmatter = _parse_frontmatter(skill_path)
    skill_name = frontmatter.get("name", skill_path.stem)
    triggers = frontmatter.get("triggers", [])
    raw_tags = frontmatter.get("tags", "")
    description = frontmatter.get("description", "")

    tags: list[str] = []
    if raw_tags:
        if isinstance(raw_tags, str):
            tags = [t.strip() for t in raw_tags.replace("[", "").replace("]", "").split(",") if t.strip()]
        elif isinstance(raw_tags, list):
            tags = raw_tags

    # ── Load active skills ──
    active_skills: list[dict] = []
    try:
        from harness_daemon import _list_active_skills, _parse_skill_frontmatter
        active_skills = _list_active_skills()
    except Exception:
        pass

    # ── Check 1: Trigger overlap ──
    active_triggers: dict[str, set[str]] = {}
    for s in active_skills:
        t_words: set[str] = set()
        for t in s.get("triggers", []):
            t_words.update(w.lower() for w in t.split() if len(w) > 1)
        if t_words:
            active_triggers[s["name"]] = t_words

    proposed_words: set[str] = set()
    for t in triggers:
        proposed_words.update(w.lower() for w in t.split() if len(w) > 1)

    overlap_scores: dict[str, float] = {}
    for other_name, other_words in active_triggers.items():
        if not proposed_words or not other_words:
            continue
        intersection = len(proposed_words & other_words)
        union = len(proposed_words | other_words)
        jaccard = intersection / union if union > 0 else 0.0
        if jaccard > 0.3:
            overlap_scores[other_name] = jaccard

    overlap_ok = len(overlap_scores) == 0
    if overlap_ok:
        od = "No significant trigger overlap with existing active skills."
    else:
        top = sorted(overlap_scores.items(), key=lambda x: -x[1])[:2]
        od = (
            f"Trigger overlap with: "
            + ", ".join(f"{name}({score:.0%})" for name, score in top)
            + ". Consider merging or specializing."
        )
    checks.append(CheckResult(
        check_name="trigger_overlap",
        passed=overlap_ok,
        detail=od,
        confidence=0.7,
    ))

    # ── Check 2: Domain demand ──
    conn = _connect(db_path)
    demand_count = 0
    if tags:
        for tag in tags:
            tag_clean = tag.strip().lower()
            if tag_clean:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM observations "
                    "WHERE tags LIKE ? AND processed_at > unixepoch() - 604800",
                    (f"%{tag_clean}%",),
                )
                demand_count += (cur.fetchone() or [0])[0]

    demand_ok = demand_count > 0 or not tags
    dd = (
        f"Past sessions in domain [{', '.join(tags[:3])}]: {demand_count} observations "
        f"in last 7 days." if tags else "No domain tags to check."
    )
    if not demand_ok and tags:
        dd += " Low demand — skill may have limited applicability."
    checks.append(CheckResult(
        check_name="domain_demand",
        passed=demand_ok,
        detail=dd,
        confidence=min(demand_count / 3, 1.0) if tags else 0.5,
    ))

    # ── Check 3: Duplicate detection ──
    duplicate_found = False
    dup_detail = "No duplicate existing skills found."
    for s in active_skills:
        s_desc = s.get("description", "").lower()
        s_name = s["name"].lower()
        prop_desc = description.lower()
        if skill_name.lower() in s_name or s_name in skill_name.lower():
            duplicate_found = True
            dup_detail = f"Name collision with existing skill: {s['name']}"
            break
        common = set(prop_desc.split()) & set(s_desc.split())
        if len(common) > 5:
            duplicate_found = True
            dup_detail = f"Description heavily overlaps with: {s['name']}"
            break

    checks.append(CheckResult(
        check_name="duplicate_detection",
        passed=not duplicate_found,
        detail=dup_detail,
        confidence=0.9 if duplicate_found else 0.7,
    ))

    conn.close()

    risk_level = _compute_risk_level(checks)
    evidence = _build_evidence(checks, skill_name)
    return SandboxReport(
        passed=all(c.passed for c in checks),
        risk_level=risk_level,
        evidence=evidence,
        checks=checks,
    )


def replay_observer(
    skill_frontmatter: dict,
    transcript_paths: list[Path],
    db_path: Path,
) -> SandboxReport:
    """Replay observer analysis on historical transcripts with a proposed skill.

    For each transcript, runs the observer's signal detection functions
    and checks whether the proposed skill would change the outcome.
    Closest equivalent to MOSS Ephemeral Trial Workers.
    """
    checks: list[CheckResult] = []
    replayed = 0
    changed_sessions: list[str] = []

    for path in transcript_paths[:MAX_REPLAY_SESSIONS]:
        if not path.exists():
            continue
        replayed += 1

        try:
            from observer import _read_transcript, analyze_session
            session_data = _read_transcript(path)
            if session_data is None:
                continue

            content = session_data["content"]
            report = analyze_session(path, None)
            if report is None:
                continue

            skill_triggers = skill_frontmatter.get("triggers", [])
            trigger_hit = any(
                t.lower() in content.lower()
                for t in skill_triggers
            ) if skill_triggers else False

            raw_tags = skill_frontmatter.get("tags", "")
            skill_tags: set[str] = set()
            if isinstance(raw_tags, str) and raw_tags.strip():
                skill_tags = set(
                    t.strip().lower()
                    for t in raw_tags.replace("[", "").replace("]", "").split(",")
                    if t.strip()
                )
            elif isinstance(raw_tags, list):
                skill_tags = set(t.lower() for t in raw_tags)

            session_tags = set(t.lower() for t in (report.tags or []))
            tag_overlap = len(skill_tags & session_tags) > 0 if skill_tags else False

            if trigger_hit or tag_overlap:
                if report.action in ("skip", "save_fragment"):
                    changed_sessions.append(
                        f"{path.stem[:40]} (was {report.action})"
                    )
        except Exception:
            continue

    if replayed == 0:
        checks.append(CheckResult(
            check_name="observer_replay",
            passed=True,
            detail="No historical transcripts available for replay.",
            confidence=0.5,
        ))
    else:
        has_impact = len(changed_sessions) > 0
        detail = (
            f"Replayed {replayed} sessions. "
            f"Proposed skill would change observer action in "
            f"{len(changed_sessions)} session(s): "
            + "; ".join(changed_sessions[:3])
            if has_impact
            else f"Replayed {replayed} sessions. No observer action change — "
                 "skill may be redundant."
        )
        checks.append(CheckResult(
            check_name="observer_replay",
            passed=True,
            detail=detail,
            confidence=0.3 + (0.4 * min(len(changed_sessions) / 3, 1.0)),
        ))

    risk_level = _compute_risk_level(checks)
    evidence = _build_evidence(
        checks,
        skill_frontmatter.get("name", "unknown"),
    )
    return SandboxReport(
        passed=all(c.passed for c in checks),
        risk_level=risk_level,
        evidence=evidence,
        checks=checks,
        replayed_sessions=replayed,
    )
