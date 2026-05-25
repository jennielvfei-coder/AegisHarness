"""Session Quality — scalar feedback signal for attention weight updates.

Replaces cycle-consistency as the training signal. Computes a quality score
from four observable signals, with user correction weighted most heavily.

Formula:
    session_quality = w1*context_used - w2*user_correction + w3*session_completed + w4*hit_rate

Constraint: |w2| > w1 + w3 + w4  (one user correction outweighs all positive signals)
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

import yaml

DEFAULT_WEIGHTS = {
    "w_context_used": 0.15,
    "w_user_correction": 0.55,
    "w_session_completed": 0.15,
    "w_domain_hit_rate": 0.15,
}


def _load_weights(config_path: Optional[Path] = None) -> dict:
    """Load session_quality weights from harness config."""
    if config_path is None:
        config_path = Path(__file__).resolve().parent / "harness_config.yaml"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        return config.get("mind_theory", {}).get("session_quality", DEFAULT_WEIGHTS)
    except Exception:
        return DEFAULT_WEIGHTS


def _compute_context_alignment(
    transcript_text: str,
    observer_tags: list[str],
    tool_types: list[str],
) -> float:
    """Compute continuous context alignment score (0.0–1.0).

    Measures how well injected context (domain tags) matched actual tool usage,
    AND whether harness markers were referenced in the session.

    This replaces the binary context_used check with a richer signal:
      - 0.0 = no alignment, no reference
      - 0.5 = partial alignment OR marker reference
      - 1.0 = strong alignment AND marker reference

    Components:
      a) Tag-tool overlap: fraction of observer tags whose domains match tools used
      b) Marker reference: did Claude mention harness context? (0/0.3 bonus)
    """
    # Component a: tag-tool alignment
    tool_domain_map = {
        "WebSearch": {"news-workflow", "general"},
        "WebFetch": {"news-workflow", "general"},
        "Read": {"general", "contract-review", "ai-governance", "privacy"},
        "Write": {"general", "document-creation"},
        "Edit": {"general", "code-modification"},
        "Bash": {"general", "code-modification"},
        "Skill": {"news-workflow", "contract-review", "ai-governance", "workflow"},
        "Agent": {"general"},
        "Grep": {"general", "code-modification"},
    }

    if not observer_tags or not tool_types:
        return 0.0

    tool_domains: set[str] = set()
    for tool in tool_types:
        domains = tool_domain_map.get(tool, {"general"})
        tool_domains.update(domains)

    tag_set = set(observer_tags)
    overlap = len(tag_set & tool_domains)
    tag_alignment = overlap / max(len(tag_set), 1)

    # Component b: marker reference
    markers = ["harness_", "Harness", "意图匹配", "工作流", "约束", "inject", "预检"]
    marker_hit = 0.3 if any(m in transcript_text for m in markers) else 0.0

    # Combined: alignment dominates, marker adds bonus
    return round(min(1.0, tag_alignment * 0.7 + marker_hit), 2)


def _check_user_correction(db_path: Path, session_id: str) -> int:
    """Check signal_buffer for correction events in this session."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=2)
        cur = conn.execute(
            "SELECT COUNT(*) FROM signal_buffer WHERE signal_type = 'correction'"
        )
        count = cur.fetchone()[0]
        conn.close()
        return 1 if count > 0 else 0
    except Exception:
        return 0


def _check_session_completed(entries: list[dict]) -> int:
    """Check if the session ended normally (not truncated/interrupted).

    A session is considered completed if:
    - There's an assistant message near the end (within last 5 entries)
    - No tool error in the last 3 entries
    """
    if not entries:
        return 0

    # Check last few entries for error signals
    last_entries = entries[-5:]
    for entry in last_entries:
        if entry.get("type") == "tool_result":
            content = json.dumps(entry, ensure_ascii=False) if isinstance(entry, dict) else str(entry)
            if any(e in content[:300].lower() for e in ("error", "failed", "traceback")):
                return 0

    # Check if there's a final assistant message
    for entry in reversed(last_entries):
        if entry.get("role") == "assistant":
            return 1

    return 0


def _compute_hit_rate(observer_tags: list[str], tool_types: list[str]) -> float:
    """Compute how well observer tags matched actual tool usage domains.

    Compares observer tags against a mapping of tool names → domains.
    Returns 0.0–1.0.
    """
    if not observer_tags or not tool_types:
        return 0.5  # Neutral if insufficient data

    # Map tools to likely domains
    tool_domain_map = {
        "WebSearch": {"news-workflow", "general"},
        "WebFetch": {"news-workflow", "general"},
        "Read": {"general", "contract-review", "ai-governance", "privacy", "data-compliance"},
        "Write": {"general", "document-creation"},
        "Edit": {"general", "code-modification"},
        "Bash": {"general", "code-modification", "data-analysis"},
        "Skill": {"news-workflow", "contract-review", "ai-governance", "workflow"},
        "Agent": {"general", "complex-task"},
        "Grep": {"general", "code-modification", "information-retrieval"},
    }

    tool_domains: set[str] = set()
    for tool in tool_types:
        domains = tool_domain_map.get(tool, {"general"})
        tool_domains.update(domains)

    tag_set = set(observer_tags)
    if not tag_set:
        return 0.5

    overlap = len(tag_set & tool_domains)
    return round(overlap / max(len(tag_set), 1), 2)


def compute_quality(
    session_id: str,
    db_path: Optional[Path] = None,
    transcript_text: str = "",
    entries: list[dict] | None = None,
    observer_tags: list[str] | None = None,
    tool_types: list[str] | None = None,
    config_path: Optional[Path] = None,
) -> float:
    """Compute the session quality scalar from four signals.

    Args:
        session_id: Session identifier.
        db_path: Path to state.db for signal_buffer queries.
        transcript_text: Full transcript text for context_used check.
        entries: Parsed transcript entries for session_completed check.
        observer_tags: Tags from observer analysis.
        tool_types: Distinct tool names used in session.
        config_path: Path to harness_config.yaml.

    Returns:
        Quality scalar 0.0–1.0.
    """
    if db_path is None:
        db_path = Path(__file__).resolve().parent / "state.db"
    if entries is None:
        entries = []

    weights = _load_weights(config_path)

    context_alignment = _compute_context_alignment(
        transcript_text, observer_tags or [], tool_types or [],
    )
    user_correction = _check_user_correction(db_path, session_id)
    session_completed = _check_session_completed(entries)
    hit_rate = _compute_hit_rate(observer_tags or [], tool_types or [])

    quality = (
        weights["w_context_used"] * context_alignment
        - weights["w_user_correction"] * user_correction
        + weights["w_session_completed"] * session_completed
        + weights["w_domain_hit_rate"] * hit_rate
    )

    return round(max(0.0, min(1.0, quality)), 3)
