"""Prophet Compiler — bridge daily report Prophet signals to structured predictions.

Converts free-text Prophet signals from markdown reports into machine-actionable
ProphetPrediction objects, then injects them into the competing_hypotheses engine
so yesterday's predictions become today's verification targets.

Usage:
    from duonews.prophet_compiler import compile_prophet_signals
    predictions = compile_prophet_signals(date_str, db)
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path


@dataclass
class ProphetPrediction:
    """A structured, machine-actionable prediction compiled from a report P-table."""
    prediction_id: str                    # e.g., "P1-2026-05-30"
    claim: str                            # The prediction claim
    time_horizon_days: int                # Forecast window in days
    confidence: float                     # 0.0–1.0
    created_date: str                     # YYYY-MM-DD
    expiry_date: str                      # YYYY-MM-DD (created + horizon)
    verification_criteria: str            # What evidence would verify/falsify
    status: str = "observing"             # observing | verified | falsified | expired
    source_report_date: str = ""          # Which daily report this came from
    days_elapsed: int = 0                 # Days since creation
    related_today_snippets: list[int] = field(default_factory=list)
    matched_entities: list[str] = field(default_factory=list)


def compile_prophet_signals(date_str: str, db) -> list[ProphetPrediction]:
    """Compile all active Prophet predictions from historical reports.

    Finds the most recent prior report, extracts structured Prophet signals,
    and returns them as ProphetPrediction objects ready for hypothesis injection.

    Args:
        date_str: Today's date (YYYY-MM-DD). We look for signals from prior reports.
        db: HarnessDB instance.

    Returns:
        List of active ProphetPrediction objects (observing + expired_unverified).
    """
    from . import find_recent_report, extract_judgment_baseline

    predictions: list[ProphetPrediction] = []

    prior = find_recent_report(date_str, max_lookback=7)
    if not prior:
        return predictions

    baseline = extract_judgment_baseline(prior)
    source_date = baseline.get("date", "")

    today = date.fromisoformat(date_str)

    for signal in baseline.get("prophet_signals", []):
        pid = signal.get("id", "?")
        claim = signal.get("claim", "")
        horizon = signal.get("time_horizon_days", 30)
        confidence = signal.get("confidence", 0.5)
        created = signal.get("created_date", source_date)
        verification = signal.get("verification_criteria", "")

        # Calculate expiry
        try:
            created_date = date.fromisoformat(created)
            expiry_date = created_date + timedelta(days=horizon)
            days_elapsed = (today - created_date).days
        except (ValueError, TypeError):
            expiry_date = today
            days_elapsed = 0

        # Determine status
        if days_elapsed > horizon:
            status = "expired_unverified"
        else:
            status = "observing"

        prediction = ProphetPrediction(
            prediction_id=f"{pid}-{source_date}",
            claim=claim,
            time_horizon_days=horizon,
            confidence=confidence,
            created_date=created,
            expiry_date=expiry_date.isoformat(),
            verification_criteria=verification,
            status=status,
            source_report_date=source_date,
            days_elapsed=days_elapsed,
        )

        # Match today's snippets that relate to this prediction's entities
        try:
            # Extract entities from the claim for matching
            from .vectorize import _extract_entities
            pred_entities = _extract_entities(claim)
            prediction.matched_entities = pred_entities

            # Find today's snippets that mention these entities
            today_snippets = db.get_news_snippets(date=date_str)
            for s in today_snippets:
                s_entities = set(s.get("entities", []))
                if s_entities & set(pred_entities):
                    prediction.related_today_snippets.append(s.get("id", 0))
        except Exception:
            pass

        predictions.append(prediction)

    return predictions


def inject_as_hypotheses(predictions: list[ProphetPrediction], db) -> int:
    """Inject Prophet predictions as competing hypotheses for today's analysis.

    Each active prediction becomes a Hypothesis in the competing_hypotheses engine,
    sourced as 'prophet' so it can be compared against feature_finder anomalies.

    Returns the count of injected hypotheses.
    """
    injected = 0

    for pred in predictions:
        if pred.status != "observing":
            continue

        try:
            # Check if already injected for this date
            existing = db._conn.execute(
                "SELECT hypothesis_id FROM hypotheses "
                "WHERE hypothesis_id = ? AND created_at > unixepoch() - 86400",
                (pred.prediction_id,),
            ).fetchone()

            if existing:
                continue

            db._conn.execute(
                """INSERT OR REPLACE INTO hypotheses
                   (hypothesis_id, parent_id, anomaly_feature_id, statement,
                    competing_alternatives, contrastive_tests, metric_scores,
                    aggregate_rank, status, iteration_count, causal_chain,
                    created_at, last_evaluated, source)
                   VALUES (?, NULL, ?, ?, '[]', '[]', '{}', ?, 'seeded', 1, '[]',
                           unixepoch(), unixepoch(), 'prophet')""",
                (
                    pred.prediction_id,
                    pred.prediction_id,  # anomaly_feature_id = self-reference
                    pred.claim,
                    pred.confidence,
                ),
            )
            db._conn.commit()
            injected += 1
        except Exception as e:
            print(f"[prophet_compiler] Failed to inject {pred.prediction_id}: {e}",
                  file=sys.stderr)

    if injected:
        print(f"[prophet_compiler] Injected {injected} Prophet predictions as hypotheses",
              file=sys.stderr)

    return injected


def format_feedback_section(predictions: list[ProphetPrediction]) -> str:
    """Generate a markdown feedback section showing Prophet signal status.

    Used by report_writer to build the '今日反馈' section's Prophet tracking table.
    """
    if not predictions:
        return ""

    lines = [
        "### Prophet 信号追踪",
        "",
        "| ID | 预言 | 时间窗口 | 置信度 | 已过天数 | 状态 | 今日相关新闻 |",
        "|----|------|----------|--------|----------|------|-------------|",
    ]

    for p in predictions:
        related = f"{len(p.related_today_snippets)}条" if p.related_today_snippets else "无"
        status_icon = {
            "observing": "🔍 观察中",
            "expired_unverified": "⚠️ 待验证",
            "verified": "✅ 已验证",
            "falsified": "❌ 已证伪",
        }.get(p.status, "❓")

        lines.append(
            f"| {p.prediction_id} | {p.claim[:50]} | {p.time_horizon_days}天 | "
            f"{p.confidence:.2f} | {p.days_elapsed}天 | {status_icon} | {related} |"
        )

    lines.append("")
    return "\n".join(lines)
