"""Observer — analyze Claude Code transcripts to decide what to extract.

Pure functions: read transcript → return structured observation.
No side effects, no database writes.
"""

import json
import re
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
    confidence: float  # 0.0–1.0
    reason: str  # Human-readable: why this action was chosen
    summary: str  # One-paragraph summary of what was learned
    tags: list[str] = field(default_factory=list)  # e.g. ['contract-review', 'privacy']
    skill_name: Optional[str] = None  # For patch_skill: which skill to update
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


def load_config(config_path: Path) -> dict:
    """Load harness configuration from a YAML file.

    Args:
        config_path: Path to the YAML configuration file.

    Returns:
        Parsed configuration dictionary.

    Raises:
        FileNotFoundError: If the config file does not exist.
        yaml.YAMLError: If the config file contains invalid YAML.
    """
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        raise FileNotFoundError(f"Config file not found: {config_path}")
    except yaml.YAMLError as e:
        raise yaml.YAMLError(f"Invalid YAML in config file {config_path}: {e}")


def _read_last_session(transcript_path: Path) -> Optional[dict]:
    """Read the last complete user+assistant exchange from memory.jsonl.

    Returns the last non-system message block, or None if file missing/empty.
    """
    if not transcript_path.exists():
        return None

    lines = []
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped:
                    lines.append(json.loads(stripped))
    except (json.JSONDecodeError, OSError):
        return None

    if not lines:
        return None

    return {
        "content": json.dumps(lines, ensure_ascii=False),
        "message_count": len(lines),
    }


def _count_tool_calls(content: str) -> int:
    """Count tool_use blocks in transcript content."""
    pattern = r'"type"\s*:\s*"tool_use"'
    return len(re.findall(pattern, content))


def _detect_pattern(content: str, patterns: list[str]) -> bool:
    """Check if any pattern appears in user message sections of the transcript."""
    user_sections = re.findall(r'"role"\s*:\s*"user"[^}]*"content"\s*:\s*"([^"]*)"', content)
    for section in user_sections:
        for phrase in patterns:
            if phrase in section:
                return True
    return False


def _generate_summary(content: str) -> str:
    """Generate a simple heuristic summary of what the session was about.

    In Phase 2, this will be replaced by LLM-based summarization.
    """
    user_msgs = re.findall(r'"role"\s*:\s*"user"[^}]*"content"\s*:\s*"([^"]*)"', content)
    if user_msgs:
        first = user_msgs[0][:200]
        return f"Session about: {first}..."
    return "Session content not extractable."


def analyze_session(
    transcript_path: Path,
    config_path: Optional[Path] = None,
) -> Optional[ObservationReport]:
    """Analyze the latest Claude Code session and return an ObservationReport.

    Args:
        transcript_path: Path to memory.jsonl
        config_path: Path to harness_config.yaml (uses default if None)

    Returns:
        ObservationReport or None if no transcript found / nothing to analyze.
    """
    if config_path is None:
        config_path = Path(__file__).resolve().parent / "harness_config.yaml"

    config = load_config(config_path)
    obs_config = config["observer"]

    session_data = _read_last_session(transcript_path)
    if session_data is None:
        return None

    content = session_data["content"]
    msg_count = session_data["message_count"]
    tool_count = _count_tool_calls(content)

    # Rule 1: Correction pattern → patch_skill (highest priority, always check)
    if _detect_pattern(content, obs_config["patterns"]["correction"]):
        return ObservationReport(
            session_id=f"session_{datetime.now().strftime('%Y%m%d%H%M%S')}",
            action="patch_skill",
            confidence=0.7,
            reason="Correction pattern detected — user corrected assistant output",
            summary=_generate_summary(content),
            tags=_guess_tags(content),
        )

    # Rule 2: Preference statement → update_preference (always check)
    if _detect_pattern(content, obs_config["patterns"]["preference"]):
        return ObservationReport(
            session_id=f"session_{datetime.now().strftime('%Y%m%d%H%M%S')}",
            action="update_preference",
            confidence=0.65,
            reason="Preference statement detected",
            summary=_generate_summary(content),
            tags=_guess_tags(content),
        )

    # Rule 3: Complex session → create_skill candidate
    # Signal: many tool calls OR rich content (MCP session summaries are high-signal even with 0 tool_use)
    threshold = obs_config["min_tool_calls_for_skill"]
    content_threshold = obs_config.get("min_content_length_for_skill", 500)
    if tool_count >= threshold or len(content) >= content_threshold:
        signal = f"tool_calls={tool_count}" if tool_count >= threshold else f"content_len={len(content)}"
        return ObservationReport(
            session_id=f"session_{datetime.now().strftime('%Y%m%d%H%M%S')}",
            action="create_skill",
            confidence=0.5,
            reason=f"Complex session: {signal} >= threshold",
            summary=_generate_summary(content),
            tags=_guess_tags(content),
        )

    # Rule 5: Default — skip
    return ObservationReport(
        session_id=f"session_{datetime.now().strftime('%Y%m%d%H%M%S')}",
        action="skip",
        confidence=0.8,
        reason=f"No strong signal detected. tool_calls={tool_count}, msg_count={msg_count}",
        summary="",
        tags=[],
    )


def _guess_tags(content: str) -> list[str]:
    """Heuristic tag guessing — replaced by LLM in Phase 2."""
    tags = []
    keywords = {
        "contract": "contract-review",
        "合同": "contract-review",
        "隐私": "privacy",
        "个保法": "privacy",
        "PIA": "privacy",
        "DPA": "privacy",
        "DSAR": "privacy",
        "并购": "m-a",
        "尽调": "m-a",
        "数据": "data-compliance",
        "AI": "ai-governance",
        "算法": "ai-governance",
        "版权": "copyright",
        "著作权": "copyright",
        "劳动": "employment",
        "知识产权": "ip",
        "商标": "ip",
        "m&a": "m-a",
        "due diligence": "m-a",
        "intellectual property": "ip",
        "employment": "employment",
        "data": "data-compliance",
    }
    content_lower = content.lower()
    for key, tag in keywords.items():
        if key.lower() in content_lower:
            tags.append(tag)
    return list(set(tags)) if tags else ["general"]
