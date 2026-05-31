"""Omega v8 — 3 failure modalities × 3 history levels → Block/Warn/Learn.

Replaces v6's 13 belief types. Not a classification system — an escalation gradient.

  3 failure modalities:  operation | semantic | knowledge
  3 history levels:       single    | repeated  | cross_session
  Actions:                Warn      | Block     | Block+Learn

Usage:
    from omega_predictor import classify_failure
    modality, action = classify_failure(entries, session_id, db)
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class FailureReport:
    """What went wrong and what to do about it."""
    modality: str          # "operation" | "semantic" | "knowledge"
    history: str           # "single" | "repeated" | "cross_session"
    action: str            # "warn" | "block" | "learn"
    confidence: float      # 0.0–1.0
    evidence: str          # What triggered this
    context_injection: str  # What to inject into next session (if warn/learn)
    escalate_to_constraint: bool = False
    constraint_pattern: str = ""


# ── Keywords ──────────────────────────────────────────────────────────────

CORRECTION_KEYWORDS = [
    "不对", "错了", "不是这样", "应该是", "改一下",
    "纠正", "重新", "那个不对", "你忘了", "搞错了",
]

SEMANTIC_CORRECTION_KEYWORDS = [
    "看不明白", "毫无意义", "看不懂", "没有关联", "没看懂",
    "没有结论", "跟新闻没有任何关联", "语义不通", "不知道什么意思",
    "完全无关", "毫无用处", "没意义",
]

KNOWLEDGE_GAP_KEYWORDS = [
    "你忘了", "之前说过", "我不是说了", "刚才说了",
    "上面说了", "前面讲了", "我不是让你", "你记错了",
]


# ── Helpers ───────────────────────────────────────────────────────────────

def _entry_text(entry: dict) -> str:
    if isinstance(entry, dict):
        return json.dumps(entry, ensure_ascii=False)
    return str(entry)


def _extract_assistant_text(entry: dict) -> str:
    """Extract actual text from assistant content blocks."""
    text = ""
    content = entry.get("content", [])
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                text += block.get("text", "") + " "
    elif isinstance(content, str):
        text = content
    return text


# ── Modality 1: Operation Failure ────────────────────────────────────────

def _detect_operation_failure(entries: list[dict]) -> dict | None:
    """Detect behavioral failures: tool errors, file issues, constraint violations.

    Merges what was previously: tool_accessibility, file_existence,
    constraint_knowledge, context_completeness.
    """
    failures = []

    for entry in entries:
        if entry.get("type") != "tool_result":
            continue

        content = _entry_text(entry)
        tool_name = entry.get("tool_name", "")

        error_signals = [
            "ECONNREFUSED", "Connection refused", "timeout", "DNS",
            "Error:", "Traceback", "exit code: 1", "Failed to connect",
            "FileNotFound", "No such file", "Permission denied",
            "blocked", "constraint", "violation",
        ]
        hits = [s for s in error_signals if s in content]
        if hits:
            failures.append({
                "tool": tool_name,
                "signals": hits[:3],
                "snippet": content[:200],
            })

    if not failures:
        return None

    # Count unique failure patterns (tool + signal)
    unique = len(set(f"{f['tool']}|{f['signals'][0]}" for f in failures))
    return {
        "modality": "operation",
        "failure_count": len(failures),
        "unique_patterns": unique,
        "evidence": f"{len(failures)} tool failures ({unique} unique patterns): "
                    f"{', '.join(f['signals'][0] for f in failures[:3])}",
        "tool_names": list(set(f["tool"] for f in failures)),
    }


# ── Modality 2: Semantic Failure ─────────────────────────────────────────

def _detect_semantic_failure(entries: list[dict]) -> dict | None:
    """Detect semantic failures: operations succeeded but output was useless.

    Signals: semantic corrections, high code churn, zero entity references.
    Need 2 of 3 to fire.
    """
    # Signal A: Semantic corrections
    sem_corrections = []
    for entry in entries:
        if entry.get("type") != "user":
            continue
        text = _entry_text(entry)
        for kw in SEMANTIC_CORRECTION_KEYWORDS:
            if kw in text:
                sem_corrections.append({"keyword": kw, "preview": text[:100]})
                break

    # Signal B: Code churn
    file_edits: dict[str, int] = defaultdict(int)
    for entry in entries:
        if entry.get("type") != "tool_use":
            continue
        if entry.get("tool") in ("Edit", "Write"):
            fp = entry.get("input", {}).get("file_path", "")
            if fp:
                file_edits[fp] += 1
    high_churn = {f: c for f, c in file_edits.items() if c > 8}

    # Signal C: Zero entity references
    entity_refs = 0
    total_chars = 0
    for entry in entries:
        if entry.get("type") == "assistant":
            text = _extract_assistant_text(entry)
            hits = len(re.findall(
                r'(?:NVIDIA|华为|AMD|TSMC|SpaceX|Baidu|Alibaba|普京|特朗普|'
                r'习近平|OpenAI|Anthropic|Samsung|Intel|苹果|谷歌|'
                r'宇树|KOSPI|Nasdaq|IPO|EUV)',
                text
            ))
            entity_refs += hits
            total_chars += len(text)
    entity_density = entity_refs / max(total_chars / 100, 1)

    sig_a = len(sem_corrections) >= 2
    sig_b = len(high_churn) > 0
    sig_c = entity_density < 0.01 and total_chars > 200

    if sum([sig_a, sig_b, sig_c]) < 2:
        return None

    return {
        "modality": "semantic",
        "correction_count": len(sem_corrections),
        "churn_files": list(high_churn.keys())[:3],
        "entity_density": round(entity_density, 4),
        "evidence": f"semantic_corrections={len(sem_corrections)} "
                    f"churn={len(high_churn)} files "
                    f"entity_density={entity_density:.4f}",
    }


# ── Modality 3: Knowledge Gap ────────────────────────────────────────────

def _detect_knowledge_gap(entries: list[dict]) -> dict | None:
    """Detect knowledge gaps: user says 'I told you this before', 'you forgot'.

    Merges what was previously: user_intent (intent corrections only),
    task_scope (context missing keywords).
    """
    gaps = []
    for entry in entries:
        if entry.get("type") != "user":
            continue
        text = _entry_text(entry)
        for kw in KNOWLEDGE_GAP_KEYWORDS:
            if kw in text:
                gaps.append({"keyword": kw, "preview": text[:120]})
                break

    if len(gaps) < 1:
        return None

    return {
        "modality": "knowledge",
        "gap_count": len(gaps),
        "evidence": f"User indicated missing context {len(gaps)} times: "
                    f"{', '.join(g['keyword'] for g in gaps[:3])}",
    }


# ── History level determination ──────────────────────────────────────────

def _determine_history(modality: str, detection: dict, db=None, session_id: str = "") -> str:
    """Determine failure history: single | repeated | cross_session.

    - single: first occurrence in this session
    - repeated: >= 3 occurrences in this session
    - cross_session: appeared in >= 2 distinct prior sessions (checks belief_traces)
    """
    if modality == "operation":
        count = detection.get("failure_count", 1)
        patterns = detection.get("unique_patterns", 1)
        if count >= 3 or patterns >= 3:
            return "repeated"

    elif modality == "semantic":
        count = detection.get("correction_count", 1)
        if count >= 4:
            return "repeated"

    elif modality == "knowledge":
        count = detection.get("gap_count", 1)
        if count >= 3:
            return "repeated"

    # Check cross-session: has this failure modality appeared in prior sessions?
    if db:
        try:
            belief_type = f"{modality}_failure"
            if session_id:
                row = db._conn.execute(
                    "SELECT COUNT(DISTINCT session_id) FROM belief_traces "
                    "WHERE belief_type=? AND session_id != ?",
                    (belief_type, session_id),
                ).fetchone()
            else:
                row = db._conn.execute(
                    "SELECT COUNT(DISTINCT session_id) FROM belief_traces "
                    "WHERE belief_type=?",
                    (belief_type,),
                ).fetchone()
            if row and row[0] >= 1:
                return "cross_session"
        except Exception:
            pass

        # Fallback: also check constraints table for tool-level blocks
        try:
            tool_names = detection.get("tool_names", [])
            for tool in tool_names:
                row = db._conn.execute(
                    "SELECT COUNT(*) FROM constraints WHERE tool_name=? AND active=1",
                    (tool,),
                ).fetchone()
                if row and row[0] > 0:
                    return "cross_session"
        except Exception:
            pass

    return "single"


# ── Action determination ─────────────────────────────────────────────────

def _determine_action(modality: str, history: str) -> str:
    """3×3 matrix: modality × history → action."""
    matrix = {
        ("operation",  "single"):        "warn",
        ("operation",  "repeated"):      "block",
        ("operation",  "cross_session"): "learn",
        ("semantic",   "single"):        "warn",
        ("semantic",   "repeated"):      "block",
        ("semantic",   "cross_session"): "learn",
        ("knowledge",  "single"):        "warn",
        ("knowledge",  "repeated"):      "block",
        ("knowledge",  "cross_session"): "learn",
    }
    return matrix.get((modality, history), "warn")


# ── Context injection generation ─────────────────────────────────────────

def _generate_injection(modality: str, history: str, detection: dict) -> str:
    """Generate context to inject into the next session."""
    if modality == "operation":
        tools = detection.get("tool_names", [])
        return (
            f"[Omega] 操作失败: {', '.join(tools[:3])} "
            f"({detection.get('failure_count', 0)} 次). "
            f"{'已建立硬约束。' if history in ('repeated', 'cross_session') else '请关注。'}"
        )
    elif modality == "semantic":
        return (
            f"[Omega] 语义层失败: 输出可能无实体关联。"
            f"{'请验证基础假设（模型/API 对当前语言/领域是否有效）。' if history == 'single' else '基础假设已验证错误，不要重复尝试。'}"
        )
    elif modality == "knowledge":
        return (
            f"[Omega] 知识缺口: 用户指出缺失上下文。"
            f"{'请回顾相关 fragment。' if history == 'single' else '请搜索 knowledge_base 补充上下文。'}"
        )
    return ""


# ── Main entry point ─────────────────────────────────────────────────────

def classify_failure(
    entries: list[dict],
    session_id: str = "",
    db=None,
) -> FailureReport | None:
    """Classify the dominant failure pattern in a session.

    Runs all 3 modality detectors. If multiple fire, picks the one with
    strongest signal (semantic > operation > knowledge).

    Returns None if no failure detected.
    """
    # Run all 3 detectors
    detections = {}
    for detector, name in [
        (_detect_semantic_failure, "semantic"),
        (_detect_operation_failure, "operation"),
        (_detect_knowledge_gap, "knowledge"),
    ]:
        result = detector(entries)
        if result:
            detections[name] = result

    if not detections:
        return None

    # Priority: semantic > operation > knowledge
    for modality in ("semantic", "operation", "knowledge"):
        if modality in detections:
            d = detections[modality]
            history = _determine_history(modality, d, db, session_id)
            action = _determine_action(modality, history)
            injection = _generate_injection(modality, history, d)

            escalate = history in ("repeated", "cross_session")
            pattern = ""
            if escalate and modality == "operation":
                tools = d.get("tool_names", [])
                pattern = f"tool:{','.join(tools[:2])}" if tools else ""
            elif escalate and modality == "semantic":
                pattern = "semantic:entity_density_zero"
            elif escalate and modality == "knowledge":
                pattern = "knowledge:context_missing"

            return FailureReport(
                modality=modality,
                history=history,
                action=action,
                confidence=0.85 if history != "single" else 0.65,
                evidence=d["evidence"],
                context_injection=injection,
                escalate_to_constraint=escalate,
                constraint_pattern=pattern,
            )

    return None


# ── Legacy compat ────────────────────────────────────────────────────────

def classify_beliefs(entries: list[dict], session_id: str = "", db=None):
    """Legacy wrapper — converts FailureReport to old BeliefTrace list format.
    Kept for backward compatibility with existing harness_daemon.py.
    """
    report = classify_failure(entries, session_id, db)
    if report is None:
        return []

    # Extract tool_name and match_pattern for constraint seeding (operation only)
    tool_name = ""
    match_pattern = report.constraint_pattern or ""

    if report.modality == "operation":
        detection = _detect_operation_failure(entries)
        if detection:
            tool_name = ",".join(detection.get("tool_names", [])[:2])
    # Semantic/knowledge: tool_name stays empty — they use fragment-based
    # omega_diagnostic injection, not tool constraints.

    return [BeliefTrace(
        belief_type=f"{report.modality}_failure",
        confidence=report.confidence,
        evidence=report.evidence,
        recommended_action="register_constraint" if report.escalate_to_constraint else "flag_for_review",
        tool_name=tool_name,
        match_pattern=match_pattern,
        context_injection=report.context_injection,
    )]


from dataclasses import dataclass as _dc


@_dc
class BeliefTrace:
    """Legacy BeliefTrace for backward compat."""
    belief_type: str
    confidence: float
    evidence: str
    recommended_action: str = ""
    tool_name: str = ""
    match_pattern: str = ""
    hypothesis_group: str = ""
    is_ambiguous: bool = False
    verified: bool = False
    falsified: bool = False
    competing_types: list = field(default_factory=list)
    context_injection: str = ""  # Omega v8: diagnostic text for next-session injection


def eliminate_beliefs_by_session(traces, *args, **kwargs):
    """Legacy compat — no-op in v8, decision is made in classify_failure."""
    return traces


def get_false_beliefs_for_constraints(traces):
    """Extract false beliefs for constraint seeding + blocked escalations.

    In v8, all escalations pass through (no multi-hypothesis blocking).
    Returns (false_beliefs, blocked_escalations).
    """
    results = []
    for t in traces:
        if hasattr(t, 'escalate_to_constraint') and t.escalate_to_constraint:
            results.append(t)
        elif getattr(t, 'recommended_action', '') == 'register_constraint':
            results.append(t)
    return results, []  # v8: no blocked escalations
