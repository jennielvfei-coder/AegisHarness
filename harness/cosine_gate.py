"""Cosine Gate — task continuity detection for context injection.

At injection time, compares the current user message embedding with the
previous session's fusion vector. If cosine similarity is below threshold,
the session is a task-switch — fall back to keyword matching.

Cold start (no previous fusion): always falls back to keyword matching.
The continuity score itself becomes a 6th attention source (learnable weight).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np

from _utils import cosine_sim as _cosine  # shared implementation


def check_continuity(
    emb_current: list[float],
    prev_fusion: list[float],
    threshold: float = 0.4,
) -> tuple[bool, float]:
    """Check if the current session is a continuation of the previous one.

    Args:
        emb_current: 384-dim embedding of the current user message.
        prev_fusion: 384-dim fusion vector from the previous session.
        threshold: Cosine similarity threshold for continuity (default 0.4).

    Returns:
        (is_continuous, score) where score is the raw cosine similarity.
    """
    if not emb_current or not prev_fusion:
        return (False, 0.0)

    # Pad to uniform dimension (BGE=512d, MiniLM=384d)
    max_dim = max(len(emb_current), len(prev_fusion))
    if len(emb_current) < max_dim:
        emb_current = emb_current + [0.0] * (max_dim - len(emb_current))
    if len(prev_fusion) < max_dim:
        prev_fusion = prev_fusion + [0.0] * (max_dim - len(prev_fusion))

    score = _cosine(emb_current, prev_fusion)
    return (score >= threshold, round(score, 4))


def get_prev_fusion(db) -> tuple[list[float], dict[str, float]] | None:
    """Read the most recent fusion session from the database.

    Args:
        db: HarnessDB instance.

    Returns:
        (fusion_vector, alphas) tuple, or None on cold start.
    """
    row = db.get_latest_fusion()
    if row is None:
        return None

    fusion_vector = row.get("fusion_vector")
    alphas = row.get("alphas")
    if fusion_vector is None or alphas is None:
        return None

    return (fusion_vector, alphas)


def get_default_alphas(config_path: Optional[Path] = None) -> dict[str, float]:
    """Get default alpha weights from config or hardcoded defaults."""
    try:
        import yaml
        if config_path is None:
            config_path = Path(__file__).resolve().parent / "harness_config.yaml"
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        return config.get("mind_theory", {}).get("attention", {}).get("initial_alphas", {
            "user_msg": 0.20,
            "claude_behavior": 0.20,
            "session_tags": 0.20,
            "history_summary": 0.20,
            "memory_entries": 0.20,
        })
    except Exception:
        return {
            "user_msg": 0.20,
            "claude_behavior": 0.20,
            "session_tags": 0.20,
            "history_summary": 0.20,
            "memory_entries": 0.20,
        }
