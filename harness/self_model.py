"""SelfModel — unified self-representation of the Harness system.

Replaces 5 fragmented injection calls (active_skills, skill_health,
pending_reviews, constraint_summary, health_alerts) with a single
structured object that both Python code AND Claude can query.

Single DB connection, single filesystem scan, single render pass.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


# ── Data structures ──────────────────────────────────────────────────────

@dataclass
class SkillEntry:
    name: str
    status: str           # 'active' | 'degraded' | 'idle' | 'pending_review'
    description: str = ""
    confidence: float = 0.5
    usage_count: int = 0
    last_used_days: Optional[float] = None
    idle_days: Optional[float] = None


@dataclass
class ConstraintSummary:
    active_count: int = 0
    precision: float = 1.0
    total_violations: int = 0


@dataclass
class HealthDigest:
    status: str = "healthy"       # 'healthy' | 'degraded' | 'critical'
    probe_latency_ms: float = 0.0
    alert_count: int = 0
    top_alerts: list[str] = field(default_factory=list)


@dataclass
class Prediction:
    domain: str            # 'complexity' | 'trend' | 'api_surface' | 'pattern'
    severity: str          # 'info' | 'warning' | 'critical'
    message: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class ActionRecommendation:
    """A concrete, machine-consumable action recommendation derived from system state."""
    priority: str          # 'critical' | 'warning' | 'info'
    mode: str              # 'deep_diagnosis' | 'cautious_execution' | 'review' | 'routine'
    summary: str           # one-line Chinese summary for context injection
    commands: list[str] = field(default_factory=list)  # CLI commands the user can run
    reasons: list[str] = field(default_factory=list)   # signals that triggered this recommendation


@dataclass
class SelfModel:
    version: int = 1
    timestamp: float = field(default_factory=time.time)
    skills: list[SkillEntry] = field(default_factory=list)
    constraints: ConstraintSummary = field(default_factory=ConstraintSummary)
    health: HealthDigest = field(default_factory=HealthDigest)
    predictions: list[Prediction] = field(default_factory=list)
    system_risk_level: int = 0           # 0-100, computed from all health signals
    recommendations: list[ActionRecommendation] = field(default_factory=list)


# ── Paths ────────────────────────────────────────────────────────────────

HARNESS_DIR = Path(__file__).resolve().parent
SELF_MODEL_PATH = HARNESS_DIR / "self_model.json"
HISTORY_DIR = HARNESS_DIR / "self_model_history"
MAX_HISTORY = 50


# ── Builder ──────────────────────────────────────────────────────────────

def build_from_state(db_path: Path) -> SelfModel:
    """Read ALL relevant state from DB + filesystem into a single SelfModel.

    Opens ONE sqlite3 connection, makes ALL queries, closes it once.
    Never raises — returns a minimal SelfModel on any error.
    """
    skills: list[SkillEntry] = []
    constraints = ConstraintSummary()
    health = HealthDigest()

    try:
        conn = sqlite3.connect(str(db_path), timeout=2)
        conn.execute("PRAGMA journal_mode=WAL")

        skills = _build_skills(conn)
        constraints = _build_constraints(conn)
        health = _build_health(conn)

        conn.close()
    except Exception:
        pass

    sm = SelfModel(
        version=1,
        timestamp=time.time(),
        skills=skills,
        constraints=constraints,
        health=health,
    )

    # ── Compute derived fields: risk level + recommendations ──
    sm.system_risk_level, risk_reasons = _compute_system_risk_level(sm)
    sm.recommendations = _generate_recommendations(sm, sm.system_risk_level, risk_reasons)

    return sm


# ── Risk computation ──────────────────────────────────────────────────────

def _compute_system_risk_level(sm: SelfModel) -> tuple[int, list[str]]:
    """Compute system risk level (0-100) from all health signals.

    Pure rule-based. No LLM. Designed to be a "fast variable" that prethink
    can read at init time to decide whether to boost base risk scores.

    Returns (risk_level, reasons_list).
    """
    risk = 0
    reasons: list[str] = []

    # ── Skill health signals ──
    degraded_count = sum(1 for s in sm.skills if s.status == "degraded")
    idle_count = sum(1 for s in sm.skills if s.status == "idle")
    pending_count = sum(1 for s in sm.skills if s.status == "pending_review")

    if degraded_count > 0:
        risk += degraded_count * 15
        reasons.append(f"{degraded_count}个技能降级")
    if idle_count > 0:
        risk += idle_count * 5
        reasons.append(f"{idle_count}个技能闲置")
    if pending_count > 0:
        risk += pending_count * 3
        reasons.append(f"{pending_count}个技能待审查")

    # Low average confidence across skills
    confidences = [s.confidence for s in sm.skills if s.confidence > 0]
    if confidences:
        avg_conf = sum(confidences) / len(confidences)
        if avg_conf < 0.5:
            risk += int((0.5 - avg_conf) * 60)
            reasons.append(f"技能平均有效性{avg_conf:.0%}")

    # ── Constraint precision signals ──
    if sm.constraints.active_count > 0 and sm.constraints.precision < 0.5:
        risk += int((0.5 - sm.constraints.precision) * 60)
        reasons.append(f"约束精度{sm.constraints.precision:.0%}")
    if sm.constraints.total_violations > 10:
        risk += min(sm.constraints.total_violations // 5, 20)
        reasons.append(f"约束违反{sm.constraints.total_violations}次")

    # ── Health signals ──
    if sm.health.status == "degraded":
        risk += 20
        reasons.append("健康状态降级")
    elif sm.health.status == "critical":
        risk += 40
        reasons.append("健康状态危急")

    if sm.health.alert_count > 0:
        risk += min(sm.health.alert_count * 8, 25)
        reasons.append(f"{sm.health.alert_count}条健康告警")

    # ── Probe latency ──
    if sm.health.probe_latency_ms > 500:
        risk += 10
        reasons.append(f"探针延迟{sm.health.probe_latency_ms:.0f}ms")

    return min(risk, 100), reasons


def _generate_recommendations(
    sm: SelfModel, risk_level: int, reasons: list[str]
) -> list[ActionRecommendation]:
    """Generate concrete, actionable recommendations from system state.

    Rules are ordered: first matching rule wins for the mode recommendation.
    Specific per-signal recommendations are additive.
    """
    recs: list[ActionRecommendation] = []

    # ── Primary mode recommendation (based on risk level) ──
    if risk_level >= 50:
        recs.append(ActionRecommendation(
            priority="critical",
            mode="deep_diagnosis",
            summary=f"[建议: 深度诊断] {', '.join(reasons)}。",
            commands=["python harness_daemon.py review --check-health"],
            reasons=reasons,
        ))
    elif risk_level >= 25:
        recs.append(ActionRecommendation(
            priority="warning",
            mode="cautious_execution",
            summary=f"[建议: 谨慎执行] {', '.join(reasons)}。",
            commands=[],
            reasons=reasons,
        ))
    else:
        recs.append(ActionRecommendation(
            priority="info",
            mode="routine",
            summary="系统状态正常，快速执行模式。",
            commands=[],
            reasons=[],
        ))

    # ── Specific per-signal recommendations (additive) ──
    degraded_skills = [s for s in sm.skills if s.status == "degraded"]
    if degraded_skills:
        names = ", ".join(s.name for s in degraded_skills)
        recs.append(ActionRecommendation(
            priority="warning",
            mode="review",
            summary=f"降级技能: {names}",
            commands=["python harness_daemon.py review"],
            reasons=[f"{s.name} confidence={s.confidence:.0%}" for s in degraded_skills],
        ))

    idle_skills = [s for s in sm.skills if s.status == "idle" and s.last_used_days
                   and s.last_used_days > 14]
    if idle_skills:
        names = ", ".join(f"{s.name}({s.last_used_days:.0f}d)" for s in idle_skills)
        recs.append(ActionRecommendation(
            priority="info",
            mode="review",
            summary=f"长期闲置技能: {names}",
            commands=["python harness_daemon.py review"],
            reasons=[f"闲置>14天"],
        ))

    if sm.constraints.precision < 0.4 and sm.constraints.active_count > 0:
        recs.append(ActionRecommendation(
            priority="warning",
            mode="review",
            summary=f"约束精度过低({sm.constraints.precision:.0%})，建议审查约束规则。",
            commands=["python harness_daemon.py review"],
            reasons=[f"constraint precision={sm.constraints.precision:.3f}"],
        ))

    if sm.health.status == "critical":
        recs.append(ActionRecommendation(
            priority="critical",
            mode="deep_diagnosis",
            summary=f"健康状态危急: {', '.join(sm.health.top_alerts[:2])}",
            commands=["python harness_daemon.py diagnose"],
            reasons=sm.health.top_alerts[:2],
        ))

    return recs


def _build_skills(conn: sqlite3.Connection) -> list[SkillEntry]:
    """Merge skill_index table with filesystem scan for active + pending skills."""
    entries: list[SkillEntry] = []
    now = time.time()

    # Query skill_index
    try:
        cur = conn.execute(
            "SELECT name, harness_confidence, usage_count, last_used, created_at "
            "FROM skill_index ORDER BY name"
        )
        db_skills = {r[0]: r for r in cur.fetchall()}
    except Exception:
        db_skills = {}

    # Scan active skills (~/.claude/skills/harness_*.md)
    active_dir = Path.home() / ".claude" / "skills"
    active_names: set[str] = set()
    if active_dir.exists():
        for f in active_dir.glob("harness_*.md"):
            active_names.add(f.stem)

    # Scan pending skills (harness/skills/*.md)
    pending_dir = HARNESS_DIR / "skills"
    pending_names: set[str] = set()
    if pending_dir.exists():
        for f in pending_dir.glob("harness_*.md"):
            pending_names.add(f.stem)

    # Merge
    all_names = set(db_skills.keys()) | active_names | pending_names
    for name in sorted(all_names):
        row = db_skills.get(name)
        confidence = row[1] if row else 0.5
        usage_count = row[2] or 0 if row else 0
        last_used = row[3] if row else None
        created_at = row[4] if row else None

        # Compute status
        is_active = name in active_names
        is_pending = name in pending_names and not is_active

        if is_pending:
            status = "pending_review"
        elif confidence < 0.4:
            status = "degraded"
        elif usage_count == 0 and created_at:
            idle_days = (now - created_at) / 86400
            if idle_days > 3:
                status = "idle"
            else:
                status = "active"
        elif last_used:
            days_since = (now - last_used) / 86400
            if days_since > 14:
                status = "idle"
            else:
                status = "active"
        else:
            status = "active" if is_active else "pending_review"

        # Compute idle/last_used days
        last_used_days = (now - last_used) / 86400 if last_used else None
        idle_days = (now - created_at) / 86400 if created_at and usage_count == 0 else None

        entries.append(SkillEntry(
            name=name,
            status=status,
            description="",
            confidence=confidence,
            usage_count=usage_count,
            last_used_days=round(last_used_days, 1) if last_used_days else None,
            idle_days=round(idle_days, 1) if idle_days else None,
        ))

    return entries


def _build_constraints(conn: sqlite3.Connection) -> ConstraintSummary:
    """Count active constraints and compute precision from tool_call_log."""
    try:
        cur = conn.execute(
            "SELECT COUNT(*), SUM(violation_count) FROM constraints "
            "WHERE active = 1 AND (expires_at IS NULL OR expires_at > unixepoch())"
        )
        active_count, total_violations = cur.fetchone()
        active_count = active_count or 0
        total_violations = total_violations or 0
    except Exception:
        active_count = 0
        total_violations = 0

    # Compute precision from tool_call_log
    precision = 1.0
    try:
        cur = conn.execute(
            "SELECT tool_name FROM constraints "
            "WHERE active = 1 AND (expires_at IS NULL OR expires_at > unixepoch())"
        )
        constraint_tools = [r[0] for r in cur.fetchall()]
        if constraint_tools:
            precisions: list[float] = []
            for tool in constraint_tools:
                if tool == "*":
                    cur2 = conn.execute(
                        "SELECT COUNT(*), SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) "
                        "FROM tool_call_log WHERE timestamp > unixepoch() - 604800"
                    )
                else:
                    cur2 = conn.execute(
                        "SELECT COUNT(*), SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) "
                        "FROM tool_call_log WHERE tool_name = ? "
                        "AND timestamp > unixepoch() - 604800",
                        (tool,),
                    )
                total, errors = cur2.fetchone()
                total = total or 0
                errors = errors or 0
                precisions.append(errors / total if total > 0 else 1.0)
            if precisions:
                precision = sum(precisions) / len(precisions)
    except Exception:
        pass

    return ConstraintSummary(
        active_count=active_count,
        precision=round(precision, 3),
        total_violations=total_violations,
    )


def _build_health(conn: sqlite3.Connection) -> HealthDigest:
    """Load health snapshot from health_probes or compute basic metrics."""
    status = "healthy"
    probe_latency_ms = 0.0
    alert_count = 0
    top_alerts: list[str] = []

    # Try loading from health_probes' snapshot history in meta_store
    try:
        cur = conn.execute(
            "SELECT value FROM meta_store WHERE key = 'health_snapshot_history'"
        )
        row = cur.fetchone()
        if row:
            history = json.loads(row[0])
            if history:
                last = history[-1]
                status = last.get("overall_status", "healthy")
                alerts = last.get("alerts", [])
                alert_count = len(alerts)
                top_alerts = alerts[:2]
                probes = last.get("probes", {})
                lat = probes.get("hook_latency", {})
                probe_latency_ms = lat.get("value", 0.0)
    except Exception:
        pass

    # Fallback: compute hook latency from tool_call_log timestamps
    if probe_latency_ms == 0.0:
        try:
            cur = conn.execute(
                "SELECT timestamp FROM tool_call_log ORDER BY timestamp DESC LIMIT 101"
            )
            timestamps = [r[0] for r in cur.fetchall()]
            if len(timestamps) >= 2:
                diffs = [
                    (timestamps[i] - timestamps[i + 1]) * 1000
                    for i in range(len(timestamps) - 1)
                ]
                probe_latency_ms = round(sorted(diffs)[len(diffs) // 2], 1)
        except Exception:
            pass

    return HealthDigest(
        status=status,
        probe_latency_ms=probe_latency_ms,
        alert_count=alert_count,
        top_alerts=top_alerts,
    )


# ── Render ───────────────────────────────────────────────────────────────

def _format_trend_arrow(current: float, baseline: float) -> str:
    """Return a trend arrow based on comparison."""
    if baseline == 0:
        return ""
    delta = current - baseline
    if delta > 0.1:
        return " ↑"
    elif delta < -0.1:
        return " ↓"
    return ""


def render_snapshot(self_model: SelfModel) -> tuple[str, list[str]]:
    """Render a compact self-portrait for context injection.

    Target: <=12 lines (was 10, +2 for recommendations).
    Priority 0.92 (above constraints at 0.90).
    Returns (header, lines) tuple for InjectorOutput.add().

    Never raises — returns fallback on any error.
    """
    try:
        lines: list[str] = []

        # ── Recommendation line (FIRST — most actionable) ──
        if self_model.recommendations:
            primary = self_model.recommendations[0]
            lines.append(f"**{primary.summary}**")
            # Commands if present
            if primary.commands:
                lines.append(f"  `{' | '.join(primary.commands)}`")

        # ── Risk bar ──
        risk = self_model.system_risk_level
        bar = "█" * (risk // 10) + "░" * (10 - risk // 10)
        risk_label = "CRITICAL" if risk >= 50 else ("WARNING" if risk >= 25 else "NORMAL")
        lines.append(f"System risk: [{bar}] {risk}% {risk_label}")

        # ── Skills line ──
        status_counts: dict[str, int] = {}
        confidences: list[float] = []
        for s in self_model.skills:
            status_counts[s.status] = status_counts.get(s.status, 0) + 1
            if s.confidence > 0:
                confidences.append(s.confidence)

        skill_parts = []
        for st in ("active", "degraded", "idle", "pending_review"):
            if status_counts.get(st, 0) > 0:
                skill_parts.append(f"{status_counts[st]} {st}")
        skill_str = " · ".join(skill_parts) if skill_parts else "0 skills"
        avg_conf = sum(confidences) / len(confidences) if confidences else 0.5
        lines.append(f"Skills: {skill_str} · avg effectiveness {avg_conf:.0%}")

        # ── Constraints line ──
        c = self_model.constraints
        prec_str = ""
        if c.active_count > 0 and c.precision < 0.5:
            prec_str = f" · precision {c.precision:.0%}"
        lines.append(f"Constraints: {c.active_count} active{prec_str}")

        # ── Health line ──
        h = self_model.health
        health_str = f"Health: {h.status}"
        if h.probe_latency_ms > 0:
            health_str += f" · probe latency {h.probe_latency_ms:.0f}ms"
        if h.alert_count > 0:
            health_str += f" · {h.alert_count} alerts"
        lines.append(health_str)

        # ── Warnings (max 6) ──
        warnings: list[str] = []

        # Secondary recommendations (beyond the primary)
        for rec in self_model.recommendations[1:3]:
            prefix = {"critical": "CRIT", "warning": "WARN", "info": "INFO"}.get(rec.priority, "")
            warnings.append(f"  [{prefix}] {rec.summary}")

        # Idle skills approaching archive
        for s in self_model.skills:
            if s.status == "idle" and s.last_used_days:
                warnings.append(
                    f"  {s.name} idle {s.last_used_days:.0f}d"
                )
            elif s.status == "degraded":
                warnings.append(
                    f"  {s.name} degraded (conf {s.confidence:.0%})"
                )
            elif s.status == "pending_review":
                warnings.append(
                    f"  {s.name} pending review"
                )

        # Health alerts
        for alert in h.top_alerts[:2]:
            short = alert[:100]
            warnings.append(f"  [health] {short}")

        # Predictions
        for p in self_model.predictions[:2]:
            prefix = {"critical": "CRIT", "warning": "WARN", "info": "INFO"}.get(p.severity, "")
            warnings.append(f"  [{prefix}] {p.message[:100]}")

        # Add warnings with prefix
        for i, w in enumerate(warnings[:6]):
            prefix = "  " if w.startswith("  ") else "  "
            lines.append(f"{prefix}{w.strip()}")

        return ("## Self", lines[:12])

    except Exception:
        return ("## Self", ["SelfModel render error — check harness state."])


# ── Persistence ──────────────────────────────────────────────────────────

def _serialize(model: SelfModel) -> dict:
    """Convert SelfModel to JSON-serializable dict."""
    return {
        "version": model.version,
        "timestamp": model.timestamp,
        "system_risk_level": model.system_risk_level,
        "skills": [
            {
                "name": s.name, "status": s.status, "description": s.description,
                "confidence": s.confidence, "usage_count": s.usage_count,
                "last_used_days": s.last_used_days, "idle_days": s.idle_days,
            }
            for s in model.skills
        ],
        "constraints": {
            "active_count": model.constraints.active_count,
            "precision": model.constraints.precision,
            "total_violations": model.constraints.total_violations,
        },
        "health": {
            "status": model.health.status,
            "probe_latency_ms": model.health.probe_latency_ms,
            "alert_count": model.health.alert_count,
            "top_alerts": model.health.top_alerts,
        },
        "predictions": [
            {
                "domain": p.domain, "severity": p.severity,
                "message": p.message, "timestamp": p.timestamp,
            }
            for p in model.predictions
        ],
        "recommendations": [
            {
                "priority": r.priority, "mode": r.mode,
                "summary": r.summary, "commands": r.commands,
                "reasons": r.reasons,
            }
            for r in model.recommendations
        ],
    }


def persist(model: SelfModel) -> Path:
    """Atomic write to self_model.json. Never corrupts the file."""
    tmp_path = SELF_MODEL_PATH.with_suffix(".tmp")
    data = _serialize(model)
    tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp_path), str(SELF_MODEL_PATH))
    return SELF_MODEL_PATH


def snapshot(model: SelfModel) -> Path | None:
    """Write a timestamped copy to history dir. Keeps last MAX_HISTORY."""
    try:
        HISTORY_DIR.mkdir(exist_ok=True)
        ts = datetime.fromtimestamp(model.timestamp).strftime("%Y%m%d_%H%M%S")
        path = HISTORY_DIR / f"v{model.version}_{ts}.json"
        data = _serialize(model)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

        # Cleanup old snapshots
        files = sorted(HISTORY_DIR.glob("v*.json"), key=lambda p: p.stat().st_mtime)
        for old in files[:-MAX_HISTORY]:
            old.unlink()
        return path
    except Exception:
        return None
