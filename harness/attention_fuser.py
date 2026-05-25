"""Attention Pooling Fuser — weighted fusion of multi-source embeddings.

Design:
- fuse(): softmax over learned alpha weights → weighted sum of source embeddings.
- update_weights(): per-source SGD step driven by per_source_quality feedback.
  Single session_quality is still accepted for backward compat but creates collinearity
  in single-user systems (all sources get same gradient → zero variance).
- Source types: user_constant (user_msg, memory_entries) never auto-archive;
  environment sources (claude_behavior, session_tags, history_summary) can.
- Conflict resolution: when collinearity detected (variance < 0.001 for 20+ sessions),
  user_constant sources get boosted weight — in single-user, intent is ground truth.

Cold start: uniform alpha = 0.20 per source (5 sources = 5 scalars).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

import numpy as np

from _utils import softmax_dict as _softmax  # shared implementation

DEFAULT_ALPHAS = {
    "user_msg": 0.20,
    "claude_behavior": 0.20,
    "session_tags": 0.20,
    "history_summary": 0.20,
    "memory_entries": 0.20,
}

SOURCE_KEYS = list(DEFAULT_ALPHAS.keys())

# Sources that represent user intent/knowledge — should not decay or auto-archive.
# Environment sources can decay because their relevance changes with time.
USER_CONSTANT_SOURCES = {"user_msg", "memory_entries"}
ENVIRONMENT_SOURCES = {"claude_behavior", "session_tags", "history_summary"}


def fuse(
    embeddings: dict[str, list[float]],
    alphas: dict[str, float],
) -> tuple[list[float], dict[str, float]]:
    """Fuse source embeddings via attention pooling.

    Args:
        embeddings: Dict mapping source_key → embedding (BGE=512d, MiniLM=384d).
        alphas: Dict mapping source_key → learned weight scalar.

    Returns:
        (fusion_vector, attention_distribution) where fusion_vector dimension
        is auto-detected from available embeddings.
    """
    attention = _softmax(alphas)

    # Auto-detect embedding dimension from available embeddings
    dim = 384
    for key in SOURCE_KEYS:
        if key in embeddings:
            dim = len(embeddings[key])
            break

    fusion = np.zeros(dim, dtype=np.float32)

    for key in SOURCE_KEYS:
        if key in embeddings and key in attention:
            emb = np.array(embeddings[key], dtype=np.float32)
            if len(emb) == dim:
                fusion += attention[key] * emb

    # Normalize
    norm = np.linalg.norm(fusion)
    if norm > 1e-10:
        fusion = fusion / norm

    return (fusion.tolist(), attention)


def compute_per_source_quality(
    session_quality: float,
    has_user_correction: bool = False,
    memory_read_count: int = 0,
) -> dict[str, float]:
    """Compute per-source quality signals to break collinearity.

    Each signal maps to an observable event:
    - user_msg penalized when user corrected (intent wasn't followed)
      Provenance: has_correction from structure["user_corrections"] in observer
    - memory_entries boosted when memory files were actually Read this session
      Provenance: scan entries for Read tool calls targeting memory dir
    - Other sources inherit session_quality unchanged (no per-source signal available)
    """
    qualities = {k: session_quality for k in SOURCE_KEYS}

    if has_user_correction:
        qualities["user_msg"] = max(0.1, session_quality - 0.30)

    if memory_read_count > 0:
        qualities["memory_entries"] = min(1.0, session_quality + 0.10)
    else:
        qualities["memory_entries"] = max(0.1, session_quality - 0.05)

    return qualities


def update_weights(
    alphas: dict[str, float],
    session_quality: float,
    attention_distribution: dict[str, float],
    learning_rate: float = 0.01,
    consecutive_below_threshold: dict[str, int] | None = None,
    archive_threshold: float = 0.05,
    archive_sessions: int = 10,
    per_source_quality: dict[str, float] | None = None,
    no_archive_sources: set[str] | None = None,
    is_collinear: bool = False,
) -> tuple[dict[str, float], dict[str, int]]:
    """Update alpha weights via per-source SGD step.

    When collinearity is detected, user_constant sources get a raised floor (0.25)
    to ensure they dominate the attention distribution in single-user systems.

    Args:
        alphas: Current alpha weights.
        session_quality: Computed quality scalar (0.0–1.0). Fallback if
            per_source_quality not provided for a source.
        attention_distribution: Post-softmax attention weights from fuse().
        learning_rate: Current SGD learning rate.
        consecutive_below_threshold: Per-source counters for archival tracking.
        archive_threshold: Weight below which environment sources are tracked.
        archive_sessions: Consecutive sessions below threshold to trigger archive.
        per_source_quality: Optional per-source quality dict.
        no_archive_sources: Source keys never auto-archived.
        is_collinear: When True, user_constant floor raised to 0.25.

    Returns:
        (updated_alphas, updated_counters)
    """
    if consecutive_below_threshold is None:
        consecutive_below_threshold = {k: 0 for k in SOURCE_KEYS}
    if no_archive_sources is None:
        no_archive_sources = USER_CONSTANT_SOURCES

    user_floor = 0.25 if is_collinear else 0.05

    new_alphas = {}
    new_counters = {}

    for key in SOURCE_KEYS:
        attn = attention_distribution.get(key, 0.0)
        alpha = alphas.get(key, DEFAULT_ALPHAS[key])

        quality = (per_source_quality or {}).get(key, session_quality)

        grad = quality - attn
        new_alpha = alpha + learning_rate * grad

        if key in no_archive_sources:
            new_alpha = max(user_floor, min(2.0, new_alpha))
            new_counters[key] = 0
        else:
            new_alpha = max(0.0, min(2.0, new_alpha))
            if new_alpha < archive_threshold:
                new_counters[key] = consecutive_below_threshold.get(key, 0) + 1
            else:
                new_counters[key] = 0
            if new_counters[key] >= archive_sessions:
                new_alphas[key] = 0.0
                continue

        new_alphas[key] = new_alpha

    return (new_alphas, new_counters)


def check_early_warning(
    below_threshold_counters: dict[str, int],
    archive_sessions: int = 10,
    warn_at: int = 5,
    no_archive_sources: set[str] | None = None,
) -> list[str]:
    """Generate early warnings for sources approaching archival.

    Only warns for environment sources — user-constant sources never archive.
    """
    if no_archive_sources is None:
        no_archive_sources = USER_CONSTANT_SOURCES

    warnings = []
    source_labels = {
        "user_msg": "用户意图 (user_msg)",
        "claude_behavior": "行为模式 (claude_behavior)",
        "session_tags": "领域标签 (session_tags)",
        "history_summary": "历史摘要 (history_summary)",
        "memory_entries": "记忆条目 (memory_entries)",
    }
    for key, count in below_threshold_counters.items():
        if key in no_archive_sources:
            continue  # never warn for user-constant sources
        if warn_at <= count < archive_sessions:
            label = source_labels.get(key, key)
            remaining = archive_sessions - count
            warnings.append(
                f"⚠️  '{label}' 权重连续 {count} 次 < 0.05 "
                f"(距归档还剩 {remaining} 次). "
                f"诊断: 检查此信息源的数据是否噪声过多或定义有误，"
                f"而非此源本身无用。"
            )
    return warnings


def detect_collinearity(
    alphas: dict[str, float],
    variance_window: list[float],
    threshold: float = 0.001,
    min_sessions: int = 20,
) -> tuple[bool, str]:
    """Detect attention source collinearity.

    When all source weights have near-zero variance for many sessions, the
    attention fuser is stuck — all sources get the same gradient. This is
    expected in single-user systems and signals a need to switch from
    "learn which source is better" to "arbitrate source conflicts."

    Returns (is_collinear, diagnostic_message).
    """
    variance_window.append(float(np.var(list(alphas.values()))))
    if len(variance_window) > 50:
        variance_window.pop(0)

    if len(variance_window) < min_sessions:
        return False, ""

    recent = variance_window[-min_sessions:]
    avg_variance = sum(recent) / len(recent)
    is_collinear = avg_variance < threshold

    msg = ""
    if is_collinear:
        msg = (
            f"[harness] mind: attention collinearity detected "
            f"(avg variance={avg_variance:.6f} over {len(recent)} sessions). "
            f"Switching to conflict-resolution mode: user_constant sources "
            f"(user_msg, memory_entries) get boosted weight."
        )
    return is_collinear, msg


def get_learning_rate(
    session_count: int,
    initial: float = 0.01,
    decay: float = 0.001,
    decay_sessions: int = 50,
) -> float:
    """Compute current learning rate with exponential decay.

    lr = decay + (initial - decay) * exp(-session_count / decay_sessions)
    """
    if session_count <= 0:
        return initial
    ratio = math.exp(-session_count / decay_sessions)
    return max(decay, decay + (initial - decay) * ratio)


def archive_source(
    source_key: str,
    alphas: dict[str, float],
) -> dict[str, float]:
    """Freeze a source's weight at zero (archive it)."""
    result = dict(alphas)
    result[source_key] = 0.0
    return result


def is_source_archived(source_key: str, alphas: dict[str, float]) -> bool:
    """Check if a source has been archived (weight frozen at 0)."""
    return alphas.get(source_key, 0.0) == 0.0
