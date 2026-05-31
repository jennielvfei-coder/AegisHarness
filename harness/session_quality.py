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
import sys
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
    except Exception as e:
        print(f"[harness] ERROR [session_quality._load_weights] {e}", file=sys.stderr, flush=True)
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
    """Check signal_buffer for correction events in this session.

    Returns:
        1 if correction found, 0 if no correction, -1 if DB query failed.
    """
    try:
        conn = sqlite3.connect(str(db_path), timeout=2)
        cur = conn.execute(
            "SELECT COUNT(*) FROM signal_buffer "
            "WHERE signal_type = 'correction' AND session_id = ?",
            (session_id,)
        )
        count = cur.fetchone()[0]
        conn.close()
        return 1 if count > 0 else 0
    except Exception as e:
        print(f"[harness] ERROR [session_quality._check_user_correction] {e}",
              file=sys.stderr, flush=True)
        return -1


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
    user_correction_raw = _check_user_correction(db_path, session_id)
    session_completed = _check_session_completed(entries)
    hit_rate = _compute_hit_rate(observer_tags or [], tool_types or [])

    if user_correction_raw == -1:
        print(f"[harness] WARNING session_quality: correction check failed "
              f"for {session_id} — treating as unknown (no penalty applied)",
              file=sys.stderr, flush=True)
        user_correction = 0  # unknown → don't penalize
    else:
        user_correction = user_correction_raw

    quality = (
        weights["w_context_used"] * context_alignment
        - weights["w_user_correction"] * user_correction
        + weights["w_session_completed"] * session_completed
        + weights["w_domain_hit_rate"] * hit_rate
    )

    return round(max(0.0, min(1.0, quality)), 3)


def extract_success_patterns(
    session_id: str,
    quality: float,
    tool_types: list[str],
    observer_tags: list[str],
    has_correction: bool,
    has_failure: bool,
    goal_type: str = "",
    goal_confidence: float = 0.0,
) -> list[dict]:
    """Extract what went RIGHT in this session — symmetric to failure detection.

    Returns list of success pattern dicts ready for fragment storage.
    Each dict has: tag, content, confidence, fragment_type='success_pattern'.

    Only extracts when the session was genuinely successful (quality > 0.3,
    no corrections, no failures).
    """
    if quality < 0.3 or has_correction or has_failure:
        return []

    patterns = []

    # Pattern 1: Effective tool combination
    if len(tool_types) >= 2:
        tool_combo = "+".join(sorted(set(tool_types)))
        patterns.append({
            "tag": f"success-tool-combo:{tool_combo}",
            "content": f"Session {session_id}: effective tool combination [{tool_combo}] "
                       f"with quality={quality:.2f}. Tools: {', '.join(tool_types)}. "
                       f"Tags: {', '.join(observer_tags) if observer_tags else 'none'}.",
            "confidence": min(quality + 0.2, 1.0),
        })

    # Pattern 2: Successful domain handling
    if observer_tags:
        for tag in observer_tags:
            if tag not in ("general", "routine", "exploration"):
                patterns.append({
                    "tag": f"success-domain:{tag}",
                    "content": f"Session {session_id}: successfully handled [{tag}] task "
                               f"with quality={quality:.2f}. "
                               f"Goal: {goal_type} (conf={goal_confidence:.2f}).",
                    "confidence": quality,
                })

    # Pattern 3: Accurate goal prediction
    if goal_type and goal_type != "unknown" and goal_confidence >= 0.5:
        patterns.append({
            "tag": f"success-goal:{goal_type}",
            "content": f"Session {session_id}: goal [{goal_type}] predicted with "
                       f"confidence={goal_confidence:.2f}, session quality={quality:.2f}. "
                       f"No failures or corrections.",
            "confidence": goal_confidence,
        })

    # Pattern 4: Clean session marker (for cold-start baselines)
    patterns.append({
        "tag": "success-clean-session",
        "content": f"Session {session_id}: clean execution — quality={quality:.2f}, "
                   f"tools={len(tool_types)}, corrections=0, failures=0.",
        "confidence": quality,
    })

    return patterns
