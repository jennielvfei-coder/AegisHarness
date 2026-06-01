"""Health probes — continuous monitoring with automatic rollback triggers.

Six probes, each a SQL query or simple computation. Runs at session
boundaries (start during cmd_inject, end during cmd_observe).
Completes in <50ms. Zero LLM calls.

Uses meta_store for:
  - health_snapshot_history: JSON list of last 20 snapshots
  - constraint_snapshot_{id}: JSON snapshot before modification
  - skill_snapshot_{name}: JSON snapshot before modification
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── Data structures ──────────────────────────────────────────────────────

@dataclass
class ProbeReading:
    name: str
    value: float
    trend: str              # 'stable' | 'rising' | 'falling'
    threshold_warning: float
    threshold_critical: float
    status: str             # 'ok' | 'warning' | 'critical'


@dataclass
class HealthSnapshot:
    timestamp: float
    probes: dict[str, ProbeReading] = field(default_factory=dict)
    overall_status: str = "healthy"
    alerts: list[str] = field(default_factory=list)


@dataclass
class RollbackAction:
    component_type: str     # 'constraint' | 'skill'
    component_id: str
    reason: str
    severity: str           # 'warning' | 'critical'


@dataclass
class RollbackResult:
    action: RollbackAction
    success: bool
    detail: str


# ── Thresholds ───────────────────────────────────────────────────────────

HOOK_LATENCY_WARN_MS = 500
HOOK_LATENCY_CRIT_MS = 1000
ERROR_RATE_WARN = 0.25
ERROR_RATE_CRIT = 0.40
CONFIDENCE_DECLINE_WARN = 0.05
CONFIDENCE_DECLINE_CRIT = 0.15
CONSTRAINT_PRECISION_WARN = 0.30
CONSTRAINT_PRECISION_CRIT = 0.15
MAX_SNAPSHOT_HISTORY = 20


# ── Connection helper ────────────────────────────────────────────────────

def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=2)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ── Probes ───────────────────────────────────────────────────────────────

def _probe_hook_latency(db_path: Path) -> ProbeReading:
    """Check if hook latency is within safe bounds.

    Uses actual duration_ms from tool_call_log (populated by hooks.py
    post_tool_use). Falls back to inter-call timing from timestamps
    when no duration_ms data exists (legacy entries).
    """
    # Prefer actual hook execution duration
    conn = _connect(db_path)
    cur = conn.execute(
        "SELECT duration_ms FROM tool_call_log "
        "WHERE duration_ms IS NOT NULL AND duration_ms > 0 "
        "ORDER BY id DESC LIMIT 100"
    )
    durations = [r[0] for r in cur.fetchall()]
    conn.close()

    if len(durations) >= 2:
        median = sorted(durations)[len(durations) // 2]
    else:
        # Fallback: inter-call timing from timestamps (legacy, inaccurate)
        conn = _connect(db_path)
        cur = conn.execute(
            "SELECT timestamp FROM tool_call_log ORDER BY timestamp DESC LIMIT 101"
        )
        timestamps = [r[0] for r in cur.fetchall()]
        conn.close()

        if len(timestamps) < 2:
            return ProbeReading(
                name="hook_latency", value=0.0, trend="stable",
                threshold_warning=HOOK_LATENCY_WARN_MS,
                threshold_critical=HOOK_LATENCY_CRIT_MS,
                status="ok",
            )

        diffs = [
            (timestamps[i] - timestamps[i + 1]) * 1000
            for i in range(len(timestamps) - 1)
        ]
        median = sorted(diffs)[len(diffs) // 2]

    status = (
        "critical" if median > HOOK_LATENCY_CRIT_MS
        else "warning" if median > HOOK_LATENCY_WARN_MS
        else "ok"
    )
    return ProbeReading(
        name="hook_latency",
        value=round(median, 1),
        trend="stable",
        threshold_warning=HOOK_LATENCY_WARN_MS,
        threshold_critical=HOOK_LATENCY_CRIT_MS,
        status=status,
    )


def _probe_constraint_precision(db_path: Path) -> ProbeReading:
    """What fraction of active constraints target tools that actually fail?

    Low precision = constraints are blocking healthy tool calls.
    """
    conn = _connect(db_path)
    cur = conn.execute(
        "SELECT tool_name, match_pattern FROM constraints "
        "WHERE active = 1 AND (expires_at IS NULL OR expires_at > unixepoch())"
    )
    constraints = cur.fetchall()
    conn.close()

    if not constraints:
        return ProbeReading(
            name="constraint_precision", value=1.0, trend="stable",
            threshold_warning=CONSTRAINT_PRECISION_WARN,
            threshold_critical=CONSTRAINT_PRECISION_CRIT,
            status="ok",
        )

    conn = _connect(db_path)
    precision_values: list[float] = []
    for tool_name, _match_pattern in constraints:
        if tool_name == "*":
            cur2 = conn.execute(
                "SELECT status FROM tool_call_log "
                "WHERE timestamp > unixepoch() - 604800 LIMIT 500"
            )
        else:
            cur2 = conn.execute(
                "SELECT status FROM tool_call_log "
                "WHERE tool_name = ? AND timestamp > unixepoch() - 604800 LIMIT 500",
                (tool_name,),
            )
        rows = cur2.fetchall()
        if not rows:
            precision_values.append(1.0)
            continue
        errors = sum(1 for (s,) in rows if s == "error")
        precision_values.append(errors / len(rows))
    conn.close()

    avg_precision = sum(precision_values) / len(precision_values)
    status = (
        "critical" if avg_precision < CONSTRAINT_PRECISION_CRIT
        else "warning" if avg_precision < CONSTRAINT_PRECISION_WARN
        else "ok"
    )
    return ProbeReading(
        name="constraint_precision",
        value=round(avg_precision, 3),
        trend="stable",
        threshold_warning=CONSTRAINT_PRECISION_WARN,
        threshold_critical=CONSTRAINT_PRECISION_CRIT,
        status=status,
    )


def _probe_skill_effectiveness_trend(db_path: Path) -> ProbeReading:
    """Is avg skill_index.harness_confidence declining?

    Compares last 7 days vs prior 7 days.
    """
    now = time.time()
    conn = _connect(db_path)
    cur = conn.execute(
        "SELECT harness_confidence, last_used FROM skill_index "
        "WHERE harness_confidence IS NOT NULL"
    )
    rows = cur.fetchall()
    conn.close()

    recent_confs: list[float] = []
    prior_confs: list[float] = []
    for conf, last_used in rows:
        if last_used and last_used > now - 604800:
            recent_confs.append(conf)
        elif last_used:
            prior_confs.append(conf)

    recent_avg = sum(recent_confs) / len(recent_confs) if recent_confs else 0.5
    prior_avg = sum(prior_confs) / len(prior_confs) if prior_confs else 0.5

    decline = prior_avg - recent_avg
    trend = "falling" if decline > CONFIDENCE_DECLINE_WARN else "stable"
    if decline < -0.01:
        trend = "rising"

    status = "ok"
    if decline > CONFIDENCE_DECLINE_CRIT:
        status = "critical"
    elif decline > CONFIDENCE_DECLINE_WARN:
        status = "warning"

    return ProbeReading(
        name="skill_effectiveness_trend",
        value=round(recent_avg, 3),
        trend=trend,
        threshold_warning=CONFIDENCE_DECLINE_WARN,
        threshold_critical=CONFIDENCE_DECLINE_CRIT,
        status=status,
    )


def _probe_tool_error_rate(db_path: Path) -> ProbeReading:
    """Is the overall tool error rate trending up?

    Compares last 24h vs 24h-48h window.
    """
    now = time.time()
    conn = _connect(db_path)

    cur = conn.execute(
        "SELECT COUNT(*), SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) "
        "FROM tool_call_log WHERE timestamp > ?",
        (now - 86400,),
    )
    total_recent, errors_recent = cur.fetchone()
    total_recent = total_recent or 0
    errors_recent = errors_recent or 0
    recent_rate = errors_recent / total_recent if total_recent > 0 else 0.0

    cur = conn.execute(
        "SELECT COUNT(*), SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) "
        "FROM tool_call_log WHERE timestamp > ? AND timestamp <= ?",
        (now - 172800, now - 86400),
    )
    total_baseline, errors_baseline = cur.fetchone()
    total_baseline = total_baseline or 0
    errors_baseline = errors_baseline or 0
    baseline_rate = errors_baseline / total_baseline if total_baseline > 0 else 0.0

    conn.close()

    delta = recent_rate - baseline_rate
    trend = "falling" if delta < -0.05 else "rising" if delta > 0.05 else "stable"

    status = "ok"
    if recent_rate > ERROR_RATE_CRIT:
        status = "critical"
    elif recent_rate > ERROR_RATE_WARN:
        status = "warning"

    return ProbeReading(
        name="tool_error_rate",
        value=round(recent_rate, 3),
        trend=trend,
        threshold_warning=ERROR_RATE_WARN,
        threshold_critical=ERROR_RATE_CRIT,
        status=status,
    )


def _probe_mcp_availability(db_path: Path) -> ProbeReading:
    """Check MCP server availability via recent error history.

    Queries tool_call_log for mcp__ prefixed tool errors in the last hour.
    """
    conn = _connect(db_path)
    cur = conn.execute(
        "SELECT COUNT(*) FROM tool_call_log "
        "WHERE status='error' AND tool_name LIKE 'mcp__%' "
        "AND timestamp > unixepoch() - 3600"
    )
    mcp_errors = (cur.fetchone() or [0])[0]
    conn.close()

    value = 1.0 if mcp_errors == 0 else max(0.0, 1.0 - mcp_errors * 0.3)
    return ProbeReading(
        name="mcp_availability",
        value=value,
        trend="stable",
        threshold_warning=0.7,
        threshold_critical=0.4,
        status="ok" if value > 0.7 else "warning" if value > 0.4 else "critical",
    )


def _probe_observer_consistency(db_path: Path) -> ProbeReading:
    """Are PreThink/Observer conflicts increasing?

    Counts observations where PreThink detected a correction/recurring_failure
    but Observer chose skip, in the last 7 days vs prior 7 days.
    """
    conn = _connect(db_path)
    now = time.time()

    cur = conn.execute(
        "SELECT COUNT(*) FROM observations "
        "WHERE tags LIKE '%prethink:%' AND action = 'skip' "
        "AND processed_at > ?",
        (now - 604800,),
    )
    recent_conflicts = (cur.fetchone() or [0])[0]

    cur = conn.execute(
        "SELECT COUNT(*) FROM observations "
        "WHERE tags LIKE '%prethink:%' AND action = 'skip' "
        "AND processed_at > ? AND processed_at <= ?",
        (now - 1209600, now - 604800),
    )
    prior_conflicts = (cur.fetchone() or [0])[0]

    # Also count explicit conflict-tagged observations
    cur = conn.execute(
        "SELECT COUNT(*) FROM observations "
        "WHERE tags LIKE '%conflict%' AND processed_at > ?",
        (now - 604800,),
    )
    explicit_conflicts = (cur.fetchone() or [0])[0]

    conn.close()

    total = recent_conflicts + explicit_conflicts
    trend = "rising" if recent_conflicts > prior_conflicts else "stable"
    if recent_conflicts < prior_conflicts:
        trend = "falling"

    status = "warning" if total > 5 else "ok"
    if total > 10:
        status = "critical"

    return ProbeReading(
        name="observer_consistency",
        value=float(total),
        trend=trend,
        threshold_warning=5,
        threshold_critical=10,
        status=status,
    )


# ── Main health check ────────────────────────────────────────────────────

def run_health_check(db_path: Path) -> HealthSnapshot:
    """Run all six probes and return a HealthSnapshot.

    Each probe is independently try/except — one failure doesn't affect others.
    """
    probes: dict[str, ProbeReading] = {}

    probe_funcs = [
        ("hook_latency", _probe_hook_latency),
        ("constraint_precision", _probe_constraint_precision),
        ("skill_effectiveness_trend", _probe_skill_effectiveness_trend),
        ("tool_error_rate", _probe_tool_error_rate),
        ("mcp_availability", lambda p: _probe_mcp_availability(p)),
        ("observer_consistency", _probe_observer_consistency),
    ]

    for name, func in probe_funcs:
        try:
            probes[name] = func(db_path)
        except Exception:
            probes[name] = ProbeReading(
                name=name, value=0.0, trend="stable",
                threshold_warning=0.5, threshold_critical=0.3,
                status="ok",
            )

    alerts: list[str] = []
    worst_status = "healthy"
    status_order = {"ok": 0, "warning": 1, "critical": 2}

    for name, probe in probes.items():
        if probe.status == "critical":
            alerts.append(f"[CRIT] {name}: val={probe.value:.2f}, trend={probe.trend}")
            worst_status = "critical"
        elif probe.status == "warning" and worst_status != "critical":
            alerts.append(f"[WARN] {name}: val={probe.value:.2f}, trend={probe.trend}")
            if worst_status == "healthy":
                worst_status = "degraded"

    return HealthSnapshot(
        timestamp=time.time(),
        probes=probes,
        overall_status=worst_status,
        alerts=alerts,
    )


# ── Rollback logic ───────────────────────────────────────────────────────

def check_rollback_triggers(
    snapshot: HealthSnapshot,
    db_path: Path,
) -> list[RollbackAction]:
    """Check if any probe crossed its critical threshold persistently.

    Loads previous snapshots from meta_store. If a probe was critical in
    the last snapshot AND is still critical now, triggers rollback.
    """
    actions: list[RollbackAction] = []
    conn = _connect(db_path)

    cur = conn.execute(
        "SELECT value FROM meta_store WHERE key = 'health_snapshot_history'"
    )
    row = cur.fetchone()
    previous_snapshots: list[dict] = []
    if row:
        try:
            previous_snapshots = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            previous_snapshots = []

    for name, probe in snapshot.probes.items():
        if probe.status != "critical":
            continue

        was_critical_before = False
        if previous_snapshots:
            last = previous_snapshots[-1]
            last_probes = last.get("probes", {})
            last_reading = last_probes.get(name, {})
            was_critical_before = last_reading.get("status") == "critical"

        if was_critical_before:
            action = _build_rollback_action(name, probe, snapshot)
            if action:
                actions.append(action)

    conn.close()
    return actions


def _build_rollback_action(
    probe_name: str,
    probe: ProbeReading,
    snapshot: HealthSnapshot,
) -> Optional[RollbackAction]:
    """Map a critical probe to a specific rollback action."""
    if probe_name == "constraint_precision":
        return RollbackAction(
            component_type="constraint",
            component_id="auto",
            reason=f"Constraint precision critical ({probe.value:.2f}). "
                   "Constraints blocking successful tool calls.",
            severity="critical",
        )
    elif probe_name == "skill_effectiveness_trend":
        return RollbackAction(
            component_type="skill",
            component_id="auto",
            reason=f"Skill effectiveness declining ({probe.value:.2f}). "
                   "Skills may be causing regressions.",
            severity="critical",
        )
    return None


def execute_rollback(
    action: RollbackAction,
    db_path: Path,
) -> RollbackResult:
    """Execute a rollback action.

    - constraint: deactivate the constraint with highest false-positive risk
    - skill: downgrade lowest-confidence skill to 0.3
    Logs to evolution_log and snapshots component state before change.
    """
    conn = _connect(db_path)

    try:
        if action.component_type == "constraint":
            cur = conn.execute(
                "SELECT id, name, tool_name FROM constraints "
                "WHERE active = 1 AND (expires_at IS NULL OR expires_at > unixepoch())"
            )
            constraints = cur.fetchall()

            worst_id = None
            worst_name = ""
            worst_ratio = -1.0

            for cid, cname, c_tool in constraints:
                if c_tool == "*":
                    cur2 = conn.execute(
                        "SELECT COUNT(*), SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) "
                        "FROM tool_call_log WHERE timestamp > unixepoch() - 86400"
                    )
                else:
                    cur2 = conn.execute(
                        "SELECT COUNT(*), SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) "
                        "FROM tool_call_log WHERE tool_name = ? "
                        "AND timestamp > unixepoch() - 86400",
                        (c_tool,),
                    )
                total, successes = cur2.fetchone()
                total = total or 0
                successes = successes or 0
                ratio = successes / total if total > 0 else 0.0
                if ratio > worst_ratio:
                    worst_ratio = ratio
                    worst_id = cid
                    worst_name = cname

            if worst_id and worst_name:
                conn.execute(
                    "INSERT OR REPLACE INTO meta_store (key, value, updated_at) "
                    "VALUES (?, ?, unixepoch())",
                    (f"constraint_snapshot_{worst_id}",
                     json.dumps({"id": worst_id, "name": worst_name,
                                 "deactivated_at": time.time()})),
                )
                conn.execute(
                    "UPDATE constraints SET active = 0 WHERE id = ?",
                    (worst_id,),
                )
                conn.execute(
                    "INSERT INTO evolution_log (skill_name, action, change_summary, timestamp) "
                    "VALUES (?, 'rollback_constraint', ?, unixepoch())",
                    (worst_name, f"Auto-rollback: {action.reason}"),
                )
                conn.commit()
                conn.close()
                return RollbackResult(
                    action=action, success=True,
                    detail=f"Deactivated constraint '{worst_name}' (id={worst_id})",
                )

        elif action.component_type == "skill":
            cur = conn.execute(
                "SELECT name, harness_confidence FROM skill_index "
                "ORDER BY harness_confidence ASC LIMIT 1"
            )
            row = cur.fetchone()
            if row:
                skill_name, old_conf = row
                conn.execute(
                    "INSERT OR REPLACE INTO meta_store (key, value, updated_at) "
                    "VALUES (?, ?, unixepoch())",
                    (f"skill_snapshot_{skill_name}",
                     json.dumps({"name": skill_name, "confidence": old_conf,
                                 "snapshot_time": time.time()})),
                )
                conn.execute(
                    "UPDATE skill_index SET harness_confidence = 0.3, "
                    "updated_at = unixepoch() WHERE name = ?",
                    (skill_name,),
                )
                conn.execute(
                    "INSERT INTO evolution_log (skill_name, action, change_summary, timestamp) "
                    "VALUES (?, 'rollback_skill', ?, unixepoch())",
                    (skill_name, f"Auto-rollback: conf {old_conf:.2f} -> 0.3. {action.reason}"),
                )
                conn.commit()
                conn.close()
                return RollbackResult(
                    action=action, success=True,
                    detail=f"Downgraded skill '{skill_name}' confidence {old_conf:.2f} -> 0.3",
                )

        conn.close()
        return RollbackResult(action=action, success=False, detail="No target found for rollback.")

    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        return RollbackResult(action=action, success=False, detail=f"Error: {e}")


# ── Snapshot persistence ─────────────────────────────────────────────────

def save_snapshot(snapshot: HealthSnapshot, db_path: Path):
    """Persist health snapshot to meta_store, keeping last MAX_SNAPSHOT_HISTORY."""
    conn = _connect(db_path)
    cur = conn.execute(
        "SELECT value FROM meta_store WHERE key = 'health_snapshot_history'"
    )
    row = cur.fetchone()
    history: list[dict] = []
    if row:
        try:
            history = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            history = []

    snapshot_dict = {
        "timestamp": snapshot.timestamp,
        "probes": {
            name: {
                "value": p.value,
                "trend": p.trend,
                "status": p.status,
            }
            for name, p in snapshot.probes.items()
        },
        "overall_status": snapshot.overall_status,
        "alerts": snapshot.alerts,
    }
    history.append(snapshot_dict)
    if len(history) > MAX_SNAPSHOT_HISTORY:
        history = history[-MAX_SNAPSHOT_HISTORY:]

    conn.execute(
        "INSERT OR REPLACE INTO meta_store (key, value, updated_at) "
        "VALUES ('health_snapshot_history', ?, unixepoch())",
        (json.dumps(history),),
    )
    conn.commit()
    conn.close()


# ── Injection helper ─────────────────────────────────────────────────────

def format_injection_alerts(snapshot: HealthSnapshot) -> list[str]:
    """Format health alerts for context injection. Max 5 lines."""
    lines: list[str] = []
    if snapshot.overall_status == "critical":
        lines.append("CRITICAL: health probes critical — review before proceeding.")
    elif snapshot.overall_status == "degraded":
        lines.append("WARNING: health probes show degradation.")

    for alert in snapshot.alerts[:3]:
        lines.append(f"- {alert}")

    return lines[:5]
