"""Observer — analyze Claude Code transcripts to decide what to extract.

Pure functions: read transcript → return structured observation.
No side effects, no database writes.

v2.0 — semantic signal detection, implicit correction, computed confidence.
"""

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class ObservationReport:
    """Result of analyzing one session transcript."""
    session_id: str
    action: str  # 'create_skill' | 'patch_skill' | 'save_fragment' | 'update_preference' | 'skip'
    confidence: float  # 0.0–1.0, computed from session statistics
    reason: str
    summary: str
    tags: list[str] = field(default_factory=list)
    skill_name: Optional[str] = None
    skill_type: Optional[str] = None  # 'env-fix' | 'task-workflow' | 'mental-model' | None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


def load_config(config_path: Path) -> dict:
    """Load harness configuration from a YAML file."""
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        raise FileNotFoundError(f"Config file not found: {config_path}")
    except yaml.YAMLError as e:
        raise yaml.YAMLError(f"Invalid YAML in config file {config_path}: {e}")


def _read_transcript(transcript_path: Path) -> Optional[dict]:
    """Read transcript JSONL and return parsed entries with metadata."""
    if not transcript_path.exists():
        return None

    entries = []
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped:
                    entries.append(json.loads(stripped))
    except (json.JSONDecodeError, OSError):
        return None

    if not entries:
        return None

    return {
        "entries": entries,
        "content": json.dumps(entries, ensure_ascii=False),
        "message_count": len(entries),
    }


# ─── Statistics ───────────────────────────────────────────────────────

def _count_tool_calls(content: str) -> int:
    """Count tool_use blocks in transcript content."""
    return len(re.findall(r'"type"\s*:\s*"tool_use"', content))


def _count_tool_types(entries: list) -> int:
    """Count distinct tool names used in the session."""
    tools = set()
    for entry in entries:
        if entry.get("type") == "tool_use":
            tools.add(entry.get("name", ""))
    return len(tools)


def _detect_tool_failures(entries: list) -> int:
    """Count tool_result entries that indicate failure."""
    failure_count = 0
    error_patterns = [
        r"Error", r"error", r"failed", r"FAILED",
        r"Traceback", r"exit code: [1-9]", r"Exception",
        r"cannot", r"unable", r"denied", r"blocked",
        r"No such file", r"not found", r"timeout",
    ]
    for entry in entries:
        if entry.get("type") == "tool_result":
            content = json.dumps(entry, ensure_ascii=False) if isinstance(entry, dict) else str(entry)
            for pat in error_patterns:
                if re.search(pat, content):
                    failure_count += 1
                    break
        elif entry.get("status") == "error":
            failure_count += 1
    return failure_count


def _has_user_interruption(entries: list) -> bool:
    """Detect if user interrupted or corrected mid-session."""
    user_msgs = [e for e in entries if e.get("role") == "user"]
    if len(user_msgs) < 2:
        return False
    # Check if later user messages are short corrections (not new topics)
    correction_hints = ["不", "错了", "改", "重新", "换一个", "修", "纠正"]
    for msg in user_msgs[1:]:
        content = msg.get("content", "")
        if any(hint in content for hint in correction_hints) and len(content) < 100:
            return True
    return False


def _compute_confidence(
    tool_count: int,
    msg_count: int,
    failure_count: int,
    tool_diversity: int,
    has_interruption: bool,
) -> float:
    """Compute observation confidence from statistical features.

    Formula: weighted average of normalized signals.
    - Base: 0.3 (won't go below this for actionable sessions)
    - Tool diversity: up to +0.2 (more tools = richer session)
    - Failures: up to +0.3 (errors = learning opportunity)
    - Interruption: up to +0.2 (user correction = strong signal)
    Capped at 0.95.
    """
    score = 0.3

    # Tool diversity bonus
    diversity_bonus = min(tool_diversity / 5.0, 1.0) * 0.2
    score += diversity_bonus

    # Failure bonus
    failure_bonus = min(failure_count / 3.0, 1.0) * 0.3
    score += failure_bonus

    # Interruption bonus (if user corrected mid-session)
    if has_interruption:
        score += 0.2

    # Message richness penalty for very short sessions
    if msg_count < 5:
        score *= 0.5

    return round(min(score, 0.95), 2)


# ─── Edit distance ────────────────────────────────────────────────────

def _edit_distance(s1: str, s2: str) -> int:
    """Levenshtein distance between two strings."""
    if len(s1) < len(s2):
        return _edit_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)
    prev = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            curr.append(min(
                curr[-1] + 1,
                prev[j + 1] + 1,
                prev[j] + (0 if c1 == c2 else 1),
            ))
        prev = curr
    return prev[-1]


def _detect_implicit_correction(entries: list) -> bool:
    """Detect if user re-ran a similar command after a tool failure.

    Pattern: tool_result with error → next user message is a modified version
    of the previous user message (edit distance < 30%).
    """
    user_msgs = [
        e.get("content", "")
        for e in entries
        if e.get("role") == "user" and e.get("content")
    ]
    if len(user_msgs) < 2:
        return False

    # Check adjacent pairs of user messages: did user retry with a slight modification?
    for i in range(len(user_msgs) - 1):
        a, b = user_msgs[i][:200], user_msgs[i + 1][:200]
        max_len = max(len(a), len(b))
        if max_len < 10:
            continue
        dist = _edit_distance(a, b)
        similarity = 1.0 - (dist / max_len)
        # High similarity but not identical → user tweaked and retried
        if 0.6 <= similarity < 1.0:
            return True
    return False


# ─── Signal detection ─────────────────────────────────────────────────

_PREFERENCE_INTENT_WORDS = [
    "记住", "以后都", "以后", "默认", "下次", "我习惯", "我总是",
    "我的偏好", "帮我记住", "永远", "从今往后", "一直",
]


def _detect_pattern(content: str, patterns: list[str]) -> bool:
    """Check if any pattern appears in user message sections."""
    user_sections = re.findall(
        r'"role"\s*:\s*"user"[^}]*"content"\s*:\s*"([^"]*)"', content
    )
    for section in user_sections:
        for phrase in patterns:
            if phrase in section:
                return True
    return False


def _detect_preference_semantic(content: str) -> bool:
    """Detect preference statements using intent words, not just keywords."""
    user_sections = re.findall(
        r'"role"\s*:\s*"user"[^}]*"content"\s*:\s*"([^"]*)"', content
    )
    for section in user_sections:
        for word in _PREFERENCE_INTENT_WORDS:
            if word in section and len(section) > 20:
                return True
    return False


def _generate_summary(content: str) -> str:
    """Heuristic summary of session topic."""
    user_msgs = re.findall(
        r'"role"\s*:\s*"user"[^}]*"content"\s*:\s*"([^"]*)"', content
    )
    if user_msgs:
        first = user_msgs[0][:200]
        return f"Session about: {first}..."
    return "Session content not extractable."


def _guess_tags(content: str) -> list[str]:
    """Heuristic tag guessing."""
    tags = []
    keywords = {
        "contract": "contract-review", "合同": "contract-review",
        "隐私": "privacy", "个保法": "privacy", "PIA": "privacy",
        "DPA": "privacy", "DSAR": "privacy",
        "并购": "m-a", "尽调": "m-a", "m&a": "m-a", "due diligence": "m-a",
        "数据": "data-compliance", "data": "data-compliance",
        "AI": "ai-governance", "算法": "ai-governance",
        "版权": "copyright", "著作权": "copyright",
        "劳动": "employment", "employment": "employment",
        "知识产权": "ip", "商标": "ip", "intellectual property": "ip",
        "每日新闻": "news-workflow", "新闻": "news-workflow",
    }
    content_lower = content.lower()
    for key, tag in keywords.items():
        if key.lower() in content_lower:
            tags.append(tag)
    return list(set(tags)) if tags else ["general"]


# ─── Main analysis ────────────────────────────────────────────────────

def analyze_session(
    transcript_path: Path,
    config_path: Optional[Path] = None,
) -> Optional[ObservationReport]:
    """Analyze the latest Claude Code session and return an ObservationReport.

    Signal priority:
      1. Explicit correction ("不对/错了") → patch_skill
      2. Implicit correction (retry after failure) → patch_skill
      3. Preference statement (记住/以后/默认) → update_preference
      4. Complex session (3+ tool calls, 1200+ chars, has failure/interruption) → create_skill
      5. Task-workflow (single named task, no failures) → save_fragment
      6. Default → skip
    """
    if config_path is None:
        config_path = Path(__file__).resolve().parent / "harness_config.yaml"

    config = load_config(config_path)
    obs_config = config["observer"]

    session_data = _read_transcript(transcript_path)
    if session_data is None:
        return None

    entries = session_data["entries"]
    content = session_data["content"]
    msg_count = session_data["message_count"]

    # Compute statistics
    tool_count = _count_tool_calls(content)
    tool_diversity = _count_tool_types(entries)
    failure_count = _detect_tool_failures(entries)
    has_interruption = _has_user_interruption(entries)
    is_implicit_correction = _detect_implicit_correction(entries)

    sid = f"session_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    tags = _guess_tags(content)
    summary = _generate_summary(content)

    # Rule 1: Explicit correction → patch_skill
    if _detect_pattern(content, obs_config["patterns"]["correction"]):
        conf = _compute_confidence(tool_count, msg_count, failure_count, tool_diversity, True)
        return ObservationReport(
            session_id=sid, action="patch_skill", confidence=conf,
            reason="Explicit correction detected — user corrected assistant output",
            summary=summary, tags=tags,
        )

    # Rule 1a: Implicit correction (retry after tool failure) → patch_skill
    if is_implicit_correction:
        conf = _compute_confidence(tool_count, msg_count, failure_count, tool_diversity, True)
        return ObservationReport(
            session_id=sid, action="patch_skill", confidence=conf,
            reason="Implicit correction detected — user re-ran modified command after tool failure",
            summary=summary, tags=tags,
        )

    # Rule 2: Preference → update_preference
    if _detect_preference_semantic(content):
        conf = _compute_confidence(tool_count, msg_count, failure_count, tool_diversity, has_interruption)
        return ObservationReport(
            session_id=sid, action="update_preference", confidence=conf,
            reason="Preference statement detected (semantic intent words)",
            summary=summary, tags=tags,
        )

    # P₃ pre-filter: single named task + no failures → task-workflow, skip refiner
    threshold = obs_config.get("min_tool_calls_for_skill", 3)
    content_threshold = obs_config.get("min_content_length_for_skill", 1200)
    is_complex = tool_count >= threshold or len(content) >= content_threshold
    has_failure_or_interruption = failure_count > 0 or has_interruption

    if not has_failure_or_interruption and len(tags) <= 2 and tool_count < threshold:
        # Simple session with no learning signal → save as fragment only
        conf = _compute_confidence(tool_count, msg_count, failure_count, tool_diversity, False)
        return ObservationReport(
            session_id=sid, action="save_fragment", confidence=conf,
            reason=f"Task-workflow: single named task, no failures (tool_calls={tool_count})",
            summary=summary, tags=tags, skill_type="task-workflow",
        )

    # Rule 3: Complex + failure/interruption → create_skill
    if is_complex and has_failure_or_interruption:
        conf = _compute_confidence(tool_count, msg_count, failure_count, tool_diversity, has_interruption)
        return ObservationReport(
            session_id=sid, action="create_skill", confidence=conf,
            reason=f"Complex session with learning signal: tool_calls={tool_count}, "
                   f"failures={failure_count}, interruption={has_interruption}",
            summary=summary, tags=tags,
        )

    # Rule 3b: Complex without failure → save_fragment only (P₃: observer pre-filter)
    if is_complex and not has_failure_or_interruption:
        conf = _compute_confidence(tool_count, msg_count, failure_count, tool_diversity, False)
        return ObservationReport(
            session_id=sid, action="save_fragment", confidence=conf,
            reason=f"Complex but clean session — no failures, storing as fragment (tool_calls={tool_count})",
            summary=summary, tags=tags, skill_type="task-workflow",
        )

    # Default: skip
    conf = _compute_confidence(tool_count, msg_count, failure_count, tool_diversity, has_interruption)
    return ObservationReport(
        session_id=sid, action="skip", confidence=conf,
        reason=f"No strong signal. tool_calls={tool_count}, failures={failure_count}, "
               f"interruption={has_interruption}, msg_count={msg_count}",
        summary="", tags=[],
    )
