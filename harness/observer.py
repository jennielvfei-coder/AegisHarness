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
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from prethink import SituationalModel

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
    skills_used: list[str] = field(default_factory=list)  # harness skill names used this session
    constraint_candidates: list[dict] = field(default_factory=list)  # recurring failures → constraints
    decision_trajectory: list[dict] = field(default_factory=list)  # key turning points
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
    """Count tool_result entries that indicate explicit failure."""
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


def _detect_data_quality_failures(entries: list) -> int:
    """Count tool results that succeeded (no error) but returned low-quality data.

    The most insidious failure mode: HTTP 200 with garbage content.
    Common in China-network environments (redirect loops, captcha walls, HTML wrappers).
    """
    quality_failures = 0
    garbage_signals = [
        "redirect", "too many requests", "please wait",
        "captcha", "access denied", "forbidden", "blocked",
        "<!DOCTYPE html", "<html", "重定向", "请稍候",
        "rate limit", "429", "try again later",
        "connection refused", "ECONNREFUSED", "ETIMEDOUT",
        "no data", "empty response", "[]", "{}",
    ]
    for entry in entries:
        if entry.get("type") != "tool_result":
            continue
        content = json.dumps(entry, ensure_ascii=False) if isinstance(entry, dict) else str(entry)
        content_lower = content[:1000].lower()

        # Empty or near-empty result from a tool that should return data
        data_len = len(content)
        if data_len < 80 and entry.get("name", "") in ("WebFetch", "WebSearch", "Bash", "Read"):
            quality_failures += 1
            continue

        # HTML wrapper returned instead of structured data
        if any(s in content_lower for s in garbage_signals):
            quality_failures += 1

    return quality_failures


def _detect_recurring_failures(entries: list) -> list[dict]:
    """Detect tool+domain combinations that failed repeatedly in this session.

    Returns list of constraint candidates:
      {tool_name, match_pattern, failure_count, reason}
    These are used by cmd_observe to seed the constraints table.
    """
    from collections import defaultdict

    # Track failures by (tool_name, domain_key)
    failures: dict[tuple[str, str], list[str]] = defaultdict(list)

    for entry in entries:
        if entry.get("type") != "tool_result":
            continue
        name = entry.get("name", "")
        if name not in ("WebFetch", "WebSearch", "Bash"):
            continue

        content = json.dumps(entry, ensure_ascii=False) if isinstance(entry, dict) else str(entry)
        content_lower = content[:1000].lower()

        # Detect explicit or data-quality failure
        is_failure = False
        error_sigs = ["error", "failed", "traceback", "exception", "timeout",
                      "denied", "blocked", "refused", "cannot", "unable",
                      "redirect", "captcha", "too many requests", "rate limit",
                      "econnrefused", "etimedout"]
        for sig in error_sigs:
            if sig in content_lower:
                is_failure = True
                break
        # Also check empty results
        if len(content) < 80:
            is_failure = True

        if not is_failure:
            continue

        # Extract domain/pattern key from the tool input
        domain = _extract_failure_domain(entry, name)
        if domain:
            failures[(name, domain)].append(content_lower[:200])

    # Filter to recurring (3+ failures on same tool+domain)
    candidates = []
    for (tool_name, domain), contents in failures.items():
        if len(contents) >= 3:
            candidates.append({
                "tool_name": tool_name,
                "match_pattern": domain,
                "failure_count": len(contents),
                "reason": f"{tool_name}→{domain} 本 session 失败 {len(contents)} 次",
            })
    return candidates


def _extract_failure_domain(entry: dict, tool_name: str) -> str:
    """Extract a domain or pattern key from a failed tool call for grouping."""
    tool_input = entry.get("input", {})
    if not isinstance(tool_input, dict):
        return ""

    if tool_name == "WebFetch":
        url = tool_input.get("url", "")
        # Extract hostname
        for prefix in ("https://", "http://"):
            if url.startswith(prefix):
                host = url[len(prefix):].split("/")[0]
                # Strip www.
                if host.startswith("www."):
                    host = host[4:]
                return host
        return url[:50] if url else ""
    elif tool_name == "WebSearch":
        query = tool_input.get("query", "")[:80]
        return query if query else ""
    elif tool_name == "Bash":
        cmd = tool_input.get("command", "")[:100]
        return cmd if cmd else ""
    return ""


def _detect_skill_usage(entries: list) -> list[str]:
    """Detect harness skill invocations in the transcript.

    Returns list of harness skill names used in this session.
    These update skill_index.usage_count for the feedback loop.
    """
    skills_used: list[str] = []
    for entry in entries:
        if entry.get("type") == "tool_use" and entry.get("name") == "Skill":
            inp = entry.get("input", {})
            skill_name = inp.get("skill", "") if isinstance(inp, dict) else ""
            if skill_name.startswith("harness_"):
                skills_used.append(skill_name.replace("harness:", ""))
    return list(set(skills_used))


def _detect_tool_result_metrics(entries: list) -> dict:
    """Return aggregate metrics about tool results for cross-session analysis."""
    metrics = {"total_results": 0, "errors": 0, "data_quality_failures": 0,
               "tools_used": set(), "failed_tools": set()}
    for entry in entries:
        if entry.get("type") == "tool_result":
            metrics["total_results"] += 1
            name = entry.get("name", "unknown")
            metrics["tools_used"].add(name)
            content = json.dumps(entry, ensure_ascii=False) if isinstance(entry, dict) else str(entry)
            if any(e in content[:500].lower() for e in ("error", "failed", "traceback", "exception")):
                metrics["errors"] += 1
                metrics["failed_tools"].add(name)
    metrics["tools_used"] = list(metrics["tools_used"])
    metrics["failed_tools"] = list(metrics["failed_tools"])
    return metrics


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
    data_quality_failures: int = 0,
) -> float:
    """Compute observation confidence from statistical features.

    Formula: weighted average of normalized signals.
    - Base: 0.3 (won't go below this for actionable sessions)
    - Tool diversity: up to +0.2 (more tools = richer session)
    - Failures: up to +0.3 (errors = learning opportunity)
    - Data quality failures: up to +0.15 (silent data corruption = strong signal)
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

    # Data quality failure bonus — harder to detect, stronger signal when found
    quality_bonus = min(data_quality_failures / 3.0, 1.0) * 0.15
    score += quality_bonus

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
            if word in section and len(section) > 8:
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
    situational_model=None,  # Optional[SituationalModel]
) -> Optional[ObservationReport]:
    """Analyze the latest Claude Code session and return an ObservationReport.

    Signal priority:
      0. PreThink gate: routine sessions skip all signal detection
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

    # ── PreThink gate: routine sessions bypass all signal computation ──
    if situational_model is not None and situational_model.situation == "routine":
        return ObservationReport(
            session_id=f"session_{datetime.now().strftime('%Y%m%d%H%M%S')}",
            action="skip", confidence=situational_model.confidence,
            reason=f"PreThink: routine session — {situational_model.reasoning_path}",
            summary="", tags=[],
        )

    # Compute statistics
    tool_count = _count_tool_calls(content)
    tool_diversity = _count_tool_types(entries)
    failure_count = _detect_tool_failures(entries)
    data_quality_failures = _detect_data_quality_failures(entries)
    has_interruption = _has_user_interruption(entries)
    is_implicit_correction = _detect_implicit_correction(entries)
    skills_used = _detect_skill_usage(entries)
    constraint_candidates = _detect_recurring_failures(entries)

    sid = f"session_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    tags = _guess_tags(content)
    summary = _generate_summary(content)
    if data_quality_failures > 0:
        tags.append("data-quality-failure")

    # Fold PreThink tags into observer tags for downstream routing
    if situational_model is not None:
        tags.append(f"prethink:{situational_model.situation}")

    # PreThink confidence floor: recurring_failure/correction models get base confidence boost
    _prethink_floor = 0.0
    if situational_model is not None:
        if situational_model.situation == "recurring_failure":
            _prethink_floor = 0.70
        elif situational_model.situation == "correction":
            _prethink_floor = 0.60

    def _conf(interrupted=False):
        raw = _compute_confidence(
            tool_count, msg_count, failure_count, tool_diversity,
            interrupted, data_quality_failures,
        )
        return max(raw, _prethink_floor)

    # Rule 1: Explicit correction → patch_skill
    if _detect_pattern(content, obs_config["patterns"]["correction"]):
        conf = _conf(True)
        return ObservationReport(
            session_id=sid, action="patch_skill", confidence=conf,
            reason=f"Explicit correction detected (dq_failures={data_quality_failures})",
            summary=summary, tags=tags, skills_used=skills_used, constraint_candidates=constraint_candidates,
        )

    # Rule 1a: Implicit correction (retry after tool failure) → patch_skill
    if is_implicit_correction:
        conf = _conf(True)
        return ObservationReport(
            session_id=sid, action="patch_skill", confidence=conf,
            reason=f"Implicit correction detected (dq_failures={data_quality_failures})",
            summary=summary, tags=tags, skills_used=skills_used, constraint_candidates=constraint_candidates,
        )

    # Rule 2: Preference → update_preference
    if _detect_preference_semantic(content):
        conf = _conf(has_interruption)
        return ObservationReport(
            session_id=sid, action="update_preference", confidence=conf,
            reason="Preference statement detected (semantic intent words)",
            summary=summary, tags=tags, skills_used=skills_used, constraint_candidates=constraint_candidates,
        )

    threshold = obs_config.get("min_tool_calls_for_skill", 3)
    content_threshold = obs_config.get("min_content_length_for_skill", 1200)
    is_complex = tool_count >= threshold or len(content) >= content_threshold
    has_failure_or_interruption = failure_count > 0 or has_interruption or data_quality_failures > 0

    # P₃ pre-filter: single named task + no failures (of any kind) → task-workflow
    if (tool_count >= 1 and not has_failure_or_interruption
            and len(tags) <= 2 and tool_count < threshold):
        conf = _conf(False)
        return ObservationReport(
            session_id=sid, action="save_fragment", confidence=conf,
            reason=f"Task-workflow: single named task, no failures (tool_calls={tool_count})",
            summary=summary, tags=tags, skills_used=skills_used, constraint_candidates=constraint_candidates, skill_type="task-workflow",
        )

    # Rule 3: Complex + failure/interruption/data-quality → create_skill
    if is_complex and has_failure_or_interruption:
        conf = _conf(has_interruption)
        return ObservationReport(
            session_id=sid, action="create_skill", confidence=conf,
            reason=f"Complex session with learning signal: tool_calls={tool_count}, "
                   f"failures={failure_count}, dq_failures={data_quality_failures}, "
                   f"interruption={has_interruption}",
            summary=summary, tags=tags, skills_used=skills_used, constraint_candidates=constraint_candidates,
        )

    # Rule 3b: Complex without failure → save_fragment only
    if is_complex and not has_failure_or_interruption:
        conf = _conf(False)
        return ObservationReport(
            session_id=sid, action="save_fragment", confidence=conf,
            reason=f"Complex but clean session (tool_calls={tool_count})",
            summary=summary, tags=tags, skills_used=skills_used, constraint_candidates=constraint_candidates, skill_type="task-workflow",
        )

    # Default: skip
    conf = _conf(has_interruption)
    return ObservationReport(
        session_id=sid, action="skip", confidence=conf,
        reason=f"No strong signal. tool_calls={tool_count}, failures={failure_count}, "
               f"dq_failures={data_quality_failures}, interruption={has_interruption}, "
               f"msg_count={msg_count}",
        summary="", tags=[],
    )


# ── Meta-ToM: structured session extraction ─────────────────────────────

def extract_session_structure(entries: list[dict]) -> dict:
    """Extract structured data from a transcript for the Theory of Mind pipeline.

    Returns a dict with keys needed by Psi, Omega, encoder, and consistency verifier.
    """
    if not entries:
        return {
            "first_user_message": "",
            "tool_use_sequence": [],
            "assistant_responses": [],
            "error_tool_calls": [],
            "stop_reason": None,
            "user_corrections": [],
            "tool_count": 0,
            "tool_types": [],
        }

    # First user message
    first_user_msg = ""
    for entry in entries:
        if entry.get("role") == "user":
            first_user_msg = entry.get("content", "")[:500]
            break

    # Tool use sequence
    tool_uses = []
    error_calls = []
    tool_names = set()
    for entry in entries:
        if entry.get("type") == "tool_use":
            name = entry.get("name", "")
            inp = entry.get("input", {})
            if isinstance(inp, dict):
                inp_summary = _summarize_input(name, inp)
            else:
                inp_summary = str(inp)[:200]
            tool_uses.append({"name": name, "input_summary": inp_summary})
            tool_names.add(name)

        if entry.get("type") == "tool_result":
            content = json.dumps(entry, ensure_ascii=False) if isinstance(entry, dict) else str(entry)
            if any(e in content[:500].lower() for e in ("error", "failed", "traceback", "exception")):
                name = entry.get("name", "")
                error_calls.append({"name": name, "error": content[:300]})

    # Assistant responses (first 200 chars each)
    assistant_msgs = []
    for entry in entries:
        if entry.get("role") == "assistant":
            assistant_msgs.append(entry.get("content", "")[:200])

    # Stop reason
    stop_reason = None
    for entry in reversed(entries):
        if entry.get("type") == "stop":
            stop_reason = entry.get("stop_reason", "")

    # User corrections
    correction_keywords = [
        "不对", "错了", "不是这样", "应该是", "改一下",
        "纠正", "重新", "那个不对",
    ]
    corrections = []
    for entry in entries:
        if entry.get("role") == "user":
            msg = entry.get("content", "")
            if any(kw in msg for kw in correction_keywords) and len(msg) < 200:
                corrections.append(msg[:200])

    return {
        "first_user_message": first_user_msg,
        "tool_use_sequence": tool_uses,
        "assistant_responses": assistant_msgs,
        "error_tool_calls": error_calls,
        "stop_reason": stop_reason,
        "user_corrections": corrections,
        "tool_count": len(tool_uses),
        "tool_types": sorted(tool_names),
    }


def _summarize_input(tool_name: str, inp: dict) -> str:
    """Create a short summary of a tool input for the structure extractor."""
    if tool_name == "WebFetch":
        return f"url={inp.get('url', '')[:150]}"
    elif tool_name == "WebSearch":
        return f"query={inp.get('query', '')[:150]}"
    elif tool_name in ("Read", "Edit", "Write"):
        return f"path={inp.get('file_path', '')[:150]}"
    elif tool_name == "Bash":
        return f"cmd={inp.get('command', '')[:150]}"
    elif tool_name == "Grep":
        return f"pattern={inp.get('pattern', '')[:150]}"
    elif tool_name == "Skill":
        return f"skill={inp.get('skill', '')[:100]}"
    else:
        return json.dumps(inp, ensure_ascii=False)[:200]


# ── Decision trajectory extraction ───────────────────────────────────────

def extract_decision_trajectory(entries: list[dict]) -> list[dict]:
    """Extract key decision turning points from a session transcript.

    Scans entries chronologically and records:
      - correction: user said "不对"/"错了", what was corrected
      - pivot: tool failed → strategy changed (different tool/approach next)
      - resolution: tool succeeded after a failure chain

    Each point: {seq, type, description, context}
    Max 8 points to keep the trajectory compact.
    """
    if not entries:
        return []

    points: list[dict] = []
    seq = 0

    # Build chronological list of (index, entry) for all meaningful entries
    timeline = []
    for i, entry in enumerate(entries):
        if entry.get("role") == "user":
            timeline.append((i, "user", entry.get("content", "")[:200]))
        elif entry.get("type") == "tool_use":
            name = entry.get("name", "")
            inp = entry.get("input", {})
            if isinstance(inp, dict):
                inp_s = _summarize_input(name, inp)
            else:
                inp_s = str(inp)[:150]
            timeline.append((i, "tool_use", f"{name}: {inp_s}"))
        elif entry.get("type") == "tool_result":
            content = json.dumps(entry, ensure_ascii=False) if isinstance(entry, dict) else str(entry)
            is_error = any(e in content[:500].lower() for e in (
                "error", "failed", "traceback", "exception", "timeout", "denied", "blocked"
            ))
            is_data_quality = _is_data_quality_failure(entry)
            if is_error or is_data_quality:
                label = "data_quality_failure" if is_data_quality and not is_error else "error"
                name = entry.get("name", entry.get("tool_name", "unknown"))
                timeline.append((i, "tool_failure", f"{name}: {label}"))
            else:
                name = entry.get("name", entry.get("tool_name", "unknown"))
                timeline.append((i, "tool_success", name))

    # Pass 1: detect user corrections
    correction_kw = ["不对", "错了", "不是这样", "应该是", "改一下", "纠正", "重新", "那个不对"]
    for i, (idx, etype, content) in enumerate(timeline):
        if etype != "user":
            continue
        if any(kw in content for kw in correction_kw) and len(content) < 200:
            # Find what came before — last assistant or tool response
            prev_context = ""
            for j in range(i - 1, max(i - 3, -1), -1):
                if timeline[j][1] in ("tool_use", "tool_failure", "tool_success"):
                    prev_context = timeline[j][2][:120]
                    break
            seq += 1
            points.append({
                "seq": seq,
                "type": "correction",
                "description": f"用户纠正: {content[:100]}",
                "context": prev_context,
            })
            if seq >= 8:
                return points

    # Pass 2: detect strategy pivots (failure → different tool type)
    for i in range(len(timeline) - 2):
        if timeline[i][1] != "tool_failure":
            continue
        # Look for next tool_use that's a different tool
        fail_tool = timeline[i][2].split(":")[0].strip()
        for j in range(i + 1, min(i + 4, len(timeline))):
            if timeline[j][1] == "tool_use":
                next_tool = timeline[j][2].split(":")[0].strip()
                if next_tool and fail_tool and next_tool != fail_tool:
                    seq += 1
                    points.append({
                        "seq": seq,
                        "type": "pivot",
                        "description": f"策略转变: {fail_tool} 失败 → 改用 {next_tool}",
                        "context": timeline[j][2][:120],
                    })
                    break

    # Pass 3: detect resolution (success after ≥2 failures)
    failure_streak = 0
    for i, (idx, etype, content) in enumerate(timeline):
        if etype == "tool_failure":
            failure_streak += 1
        elif etype == "tool_success" and failure_streak >= 2:
            seq += 1
            points.append({
                "seq": seq,
                "type": "resolution",
                "description": f"突破: 连续 {failure_streak} 次失败后 {content} 成功",
                "context": f"第 {idx} 步解决阻塞",
            })
            failure_streak = 0
            if seq >= 8:
                return points
        elif etype == "tool_success":
            failure_streak = 0

    # Sort by sequence number
    points.sort(key=lambda p: p["seq"])
    return points[:8]


def _is_data_quality_failure(entry: dict) -> bool:
    """Check if a tool_result succeeded but returned garbage data."""
    content = json.dumps(entry, ensure_ascii=False) if isinstance(entry, dict) else str(entry)
    content_lower = content[:1000].lower()
    garbage_signals = [
        "redirect", "too many requests", "please wait",
        "captcha", "access denied", "forbidden", "blocked",
        "<!doctype html", "<html", "重定向", "请稍候",
        "rate limit", "429", "try again later",
        "connection refused", "econnrefused", "etimedout",
        "no data", "empty response",
    ]
    if any(s in content_lower for s in garbage_signals):
        return True
    # Empty/near-empty result
    if len(content) < 80 and entry.get("name", "") in ("WebFetch", "WebSearch", "Bash", "Read"):
        return True
    return False
