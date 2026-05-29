"""Consistency Verifier — three-way diagnostic classifier.

Given Psi's goal prediction (g_hat) and Omega's belief traces (b_hat),
predicts what Claude SHOULD have done via nearest-neighbor lookup among
historically successful sessions. Compares with actual actions.

Three error types → three actions:
  goal_error  → store corrected interaction pair for Psi retraining
  belief_error → update Omega classifier weights or flag for review
  skill_gap   → feed into observer → skill_writer pipeline
  none        → positive sample, mild reinforcement of all alphas
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from harness._utils import cosine_sim as _cosine  # shared implementation


@dataclass
class VerificationReport:
    """Diagnostic classification of session outcome."""
    error_type: str  # 'goal_error' | 'belief_error' | 'skill_gap' | 'none'
    confidence: float
    recommended_action: str
    evidence: str
    details: dict = field(default_factory=dict)


def _build_action_vector(actions: list[dict]) -> list[float]:
    """Convert a list of tool_use actions into a simple 384-dim feature vector.

    Uses a bag-of-tool-names projection for comparison.
    """
    if not actions:
        return [0.0] * 384

    # Simple encoding: hash each tool name to a dimension index
    vec = np.zeros(384, dtype=np.float32)
    tool_weights = {
        "WebFetch": 1.0, "WebSearch": 1.0, "Read": 0.8,
        "Write": 1.2, "Edit": 1.2, "Bash": 1.0,
        "Grep": 0.6, "Glob": 0.6, "Skill": 1.5,
        "Agent": 2.0, "TaskCreate": 0.8,
    }
    for action in actions:
        name = action.get("name", "")
        weight = tool_weights.get(name, 0.5)
        idx = hash(name) % 384
        vec[idx] += weight

    norm = np.linalg.norm(vec)
    if norm > 1e-10:
        vec = vec / norm

    return vec.tolist()


def verify(
    session_id: str,
    goal_prediction,  # GoalPrediction from psi_predictor
    belief_traces: list,  # list[BeliefTrace] from omega_predictor
    actual_actions: list[dict],
    db,
    user_corrections: list[str] | None = None,
    error_tool_calls: list[dict] | None = None,
) -> VerificationReport:
    """Classify the session's error type by comparing predicted vs actual behavior.

    Args:
        session_id: Session identifier.
        goal_prediction: GoalPrediction from Psi.
        belief_traces: List of BeliefTrace from Omega.
        actual_actions: List of tool_use summaries from extract_session_structure.
        db: HarnessDB instance.
        user_corrections: List of user correction messages.
        error_tool_calls: List of error tool call summaries.

    Returns:
        VerificationReport with error classification and recommended action.
    """
    user_corrections = user_corrections or []
    error_tool_calls = error_tool_calls or []

    has_correction = len(user_corrections) > 0
    has_failure = len(error_tool_calls) > 0
    has_false_belief = len(belief_traces) > 0

    # ── Find nearest successful sessions as baseline ──
    successful = db.get_successful_sessions(min_quality=0.7, limit=30)

    # Cold-start fallback: no successful sessions yet → skip verification
    if not successful:
        return VerificationReport(
            error_type="none",
            confidence=0.3,
            recommended_action="Insufficient baseline data — deferring verification. "
                             "Will engage after 2+ sessions with quality > 0.7.",
            evidence="No successful sessions in fusion_sessions (min_quality=0.7). "
                     "This is expected during cold start.",
            details={"cold_start": True, "sessions_available": 0},
        )

    # ── Classify ──

    # Case 1: High divergence + user correction → goal_error
    if has_correction and goal_prediction.confidence < 0.6:
        return VerificationReport(
            error_type="goal_error",
            confidence=0.80,
            recommended_action="Psi retraining: store corrected interaction pair",
            evidence=f"User correction + low Psi confidence ({goal_prediction.confidence:.2f}). "
                     f"Corrections: {user_corrections[:2]}",
            details={
                "goal_type": goal_prediction.goal_type,
                "goal_confidence": goal_prediction.confidence,
                "correction_count": len(user_corrections),
            },
        )

    # Case 2: High divergence + tool failure → belief_error
    if has_failure and has_false_belief:
        belief_types = [t.belief_type for t in belief_traces]
        return VerificationReport(
            error_type="belief_error",
            confidence=0.75,
            recommended_action="Omega: update classifier weights or flag for manual review",
            evidence=f"Tool failures ({len(error_tool_calls)}) + false beliefs detected: {belief_types}",
            details={
                "belief_types": belief_types,
                "failure_count": len(error_tool_calls),
                "belief_count": len(belief_traces),
            },
        )

    # Case 3: High divergence + no correction/failure → skill_gap
    if not has_correction and not has_failure and actual_actions and successful:
        # Check if there's a similar successful session
        actual_vec = _build_action_vector(actual_actions)
        best_sim = 0.0
        for s in successful:
            fusion = s.get("fusion_vector")
            if fusion:
                sim = _cosine(actual_vec, fusion)
                best_sim = max(best_sim, sim)

        if best_sim < 0.5:
            return VerificationReport(
                error_type="skill_gap",
                confidence=0.60,
                recommended_action="Feed to observer → skill_writer pipeline (create/patch skill)",
                evidence=f"No similar successful session found (best_sim={best_sim:.3f})",
                details={"best_similarity": best_sim},
            )

    # Case 4: No issues → none
    return VerificationReport(
        error_type="none",
        confidence=0.70,
        recommended_action="Positive sample — mild reinforcement of all attention weights",
        evidence="Session executed without detectable errors",
        details={},
    )


def get_verification_summary(report: VerificationReport) -> str:
    """One-line summary for logging."""
    if report.error_type == "none":
        return "✓ no errors detected"
    return f"{report.error_type} (conf={report.confidence:.2f}): {report.recommended_action[:100]}"
