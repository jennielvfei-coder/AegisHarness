"""Psi — Inverse Goal Predictor (multi-hypothesis).

Infers the user's latent goal from interaction pairs (user_message → Claude_actions).
Uses k-NN with cosine similarity over stored interaction pair embeddings.
Returns TOP-3 competing goal hypotheses, not a single prediction.

Multi-hypothesis design (inspired by FeatureExplainer):
  - k-NN weighted voting produces a ranked list, not just the winner
  - is_ambiguous flag when top-2 confidence gap < 0.1
  - eliminate_goals_by_session() uses real session outcomes to falsify hypotheses
  - Zero LLM cost: all verification via session_quality + tool_failures + corrections

Falls back to rule-based classification (tool-type counting) on cold start.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from _utils import cosine_sim as _cosine_similarity  # shared implementation

from encoder import encode_cached, text_hash


@dataclass
class GoalPrediction:
    """Inferred user goal from a single message."""
    goal_type: str
    domain: str = ""
    complexity: float = 0.5
    expected_tools: list[str] = field(default_factory=list)
    confidence: float = 0.0
    # Multi-hypothesis fields
    is_ambiguous: bool = False  # True when top-2 confidence gap < 0.1
    rank: int = 0  # Position in the hypothesis list (1-indexed)
    verified: bool = False  # Confirmed by session outcome
    falsified: bool = False  # Disproven by session outcome


@dataclass
class InteractionPair:
    """A user_message → Claude_actions pair extracted from a transcript."""
    user_message: str
    claude_actions: list[dict]
    outcome: str  # 'success' | 'failure' | 'mixed'
    session_id: str = ""


# ── Goal type classification from tool patterns ──

TOOL_GOAL_MAP = {
    "information_retrieval": {"WebFetch", "WebSearch", "Read", "Grep", "Glob"},
    "document_creation": {"Write", "NotebookEdit"},
    "code_modification": {"Edit", "Write", "Bash"},
    "data_analysis": {"Bash", "NotebookEdit", "WebFetch"},
    "workflow_execution": {"Skill", "Agent", "TaskCreate"},
}

GOAL_TYPE_TOOLS = {
    "information_retrieval": ["WebFetch", "WebSearch", "Read", "Grep"],
    "document_creation": ["Write", "Edit"],
    "code_modification": ["Edit", "Write", "Bash"],
    "data_analysis": ["Bash", "NotebookEdit"],
    "workflow_execution": ["Skill", "Agent", "TaskCreate"],
    "other": [],
}

AMBIGUITY_GAP_THRESHOLD = 0.10  # Top-2 confidence gap below this = ambiguous


# ── Text-based cold-start classification ──

TEXT_GOAL_SIGNALS = {
    "information_retrieval": {
        "keywords": ["查找", "搜索", "是什么", "怎么", "如何", "什么是",
                     "哪些", "谁", "哪里", "哪个", "解释", "了解", "看看",
                     "告诉我", "知道", "有没有", "能不能", "为什么", "区别",
                     "查一下", "找一下", "搜一下", "查查", "找找"],
        # Single-char: only match as full word (not part of compound like 审查/调查)
        "single_chars": ["查", "找", "搜"],
        "patterns": ["?"],
    },
    "document_creation": {
        "keywords": ["写", "创建", "生成", "做", "起草", "新建", "构建",
                     "制作", "输出", "撰写", "编写", "出"],
    },
    "code_modification": {
        "keywords": ["改", "修", "优化", "fix", "更新", "重构", "调整",
                     "替换", "迁移", "升级", "修复", "改一下"],
    },
    "data_analysis": {
        "keywords": ["分析", "统计", "计算", "对比", "数据", "趋势",
                     "图表", "汇总", "总结", "归纳", "评估"],
    },
    "workflow_execution": {
        "keywords": ["运行", "执行", "跑", "启动", "部署", "发布",
                     "安装", "配置", "设置", "安排", "定时"],
    },
}


def _classify_by_text(message: str) -> list[GoalPrediction]:
    """Fast text-based goal classification when no tools/k-NN data available.

    Uses message-level features: keywords, length, question density.
    Zero embedding cost — pure string matching.
    """
    msg = message.lower()
    msg_len = len(message)

    scores: dict[str, float] = {}
    # Compound words that contain single-char keywords (for exclusion)
    _compound_exclusions = {
        "查": {"审查", "调查", "侦查", "核查", "复查", "巡查"},
        "找": {"寻找", "查找"},
    }

    for goal_type, signals in TEXT_GOAL_SIGNALS.items():
        kw_hits = sum(1 for kw in signals.get("keywords", []) if kw in msg)
        # Single-char keywords: only match if NOT part of a compound word
        sc_hits = 0
        for sc in signals.get("single_chars", []):
            if sc in msg:
                if sc in _compound_exclusions:
                    if any(compound in msg for compound in _compound_exclusions[sc]):
                        continue  # Skip: single char is part of compound word
                sc_hits += 1
        pattern_hits = sum(1 for p in signals.get("patterns", []) if p in msg)
        raw = (kw_hits + sc_hits) * 0.08
        if pattern_hits > 0:
            raw += 0.05 * pattern_hits
        if raw > 0:
            scores[goal_type] = raw

    # Length-based priors — only as weak tiebreaker when multiple categories fire
    # or to seed scores when NO keywords matched at all
    if not scores:
        if msg_len < 20:
            scores["workflow_execution"] = 0.05
            scores["information_retrieval"] = 0.03
        elif msg_len > 200:
            scores["document_creation"] = 0.05
            scores["data_analysis"] = 0.03
        else:
            scores["information_retrieval"] = 0.03

    if not scores:
        return [GoalPrediction(
            goal_type="other", confidence=0.25, complexity=0.1,
            expected_tools=[], rank=1,
        )]

    # Softmax-style normalization: confidence reflects relative strength
    total = sum(scores.values())
    results = []
    for goal_type, raw in sorted(scores.items(), key=lambda x: -x[1]):
        confidence = min((raw / total) * 0.6, 0.55)  # Cap text-only confidence
        results.append(GoalPrediction(
            goal_type=goal_type,
            confidence=round(confidence, 2),
            complexity=0.1,
            expected_tools=GOAL_TYPE_TOOLS.get(goal_type, []),
        ))

    for i, r in enumerate(results):
        r.rank = i + 1

    if len(results) >= 2:
        gap = results[0].confidence - results[1].confidence
        if gap < AMBIGUITY_GAP_THRESHOLD:
            results[0].is_ambiguous = True
            results[1].is_ambiguous = True

    return results[:3]


def _classify_by_tools_all(tool_uses: list[dict], tool_types: list[str]) -> list[GoalPrediction]:
    """Rule-based goal classification — returns ALL goal types with scores.

    When no tools used yet, falls back to text-based classification.
    Returns list sorted by confidence descending.
    """
    tool_set = set(tool_types)
    if not tool_set:
        return []  # Signal caller to use text-based fallback

    results = []
    for goal_type, expected in TOOL_GOAL_MAP.items():
        overlap = len(tool_set & expected)
        total = len(expected)
        score = overlap / max(total, 1)
        if score > 0:
            confidence = min(score + 0.2, 0.70)
            results.append(GoalPrediction(
                goal_type=goal_type,
                confidence=round(confidence, 2),
                complexity=_compute_complexity(len(tool_uses), len(tool_types)),
                expected_tools=GOAL_TYPE_TOOLS.get(goal_type, []),
            ))

    if not results:
        results.append(GoalPrediction(
            goal_type="other", confidence=0.3, complexity=0.1,
            expected_tools=[],
        ))

    results.sort(key=lambda x: -x.confidence)
    for i, r in enumerate(results):
        r.rank = i + 1

    if len(results) >= 2:
        gap = results[0].confidence - results[1].confidence
        if gap < AMBIGUITY_GAP_THRESHOLD:
            results[0].is_ambiguous = True
            results[1].is_ambiguous = True

    return results[:3]


def _compute_complexity(tool_count: int, tool_diversity: int) -> float:
    """Estimate task complexity from tool usage."""
    if tool_count == 0:
        return 0.1
    diversity_norm = min(tool_diversity / 8.0, 1.0)
    count_norm = min(tool_count / 15.0, 1.0)
    return round(0.3 * diversity_norm + 0.7 * count_norm, 2)


# ── Interaction pair extraction ──

def extract_interaction_pairs(entries: list[dict]) -> list[InteractionPair]:
    """Segment a transcript into (user_message, subsequent_claude_actions) pairs."""
    pairs = []
    current_user_msg = ""
    current_actions: list[dict] = []
    current_errors = 0
    current_success = 0

    for entry in entries:
        if entry.get("role") == "user":
            if current_user_msg and current_actions:
                outcome = "mixed"
                if current_errors == 0 and current_success > 0:
                    outcome = "success"
                elif current_errors > 0 and current_success == 0:
                    outcome = "failure"
                pairs.append(InteractionPair(
                    user_message=current_user_msg,
                    claude_actions=current_actions,
                    outcome=outcome,
                ))
            current_user_msg = entry.get("content", "")[:500]
            current_actions = []
            current_errors = 0
            current_success = 0

        elif entry.get("type") == "tool_use":
            name = entry.get("name", "")
            inp = entry.get("input", {})
            if isinstance(inp, dict):
                inp_str = json.dumps(inp, ensure_ascii=False)[:200]
            else:
                inp_str = str(inp)[:200]
            current_actions.append({"name": name, "input": inp_str})

        elif entry.get("type") == "tool_result":
            content = json.dumps(entry, ensure_ascii=False) if isinstance(entry, dict) else str(entry)
            has_error = any(
                e in content[:500].lower()
                for e in ("error", "failed", "traceback", "exception", "exit code: 1")
            )
            if has_error:
                current_errors += 1
            else:
                current_success += 1

    if current_user_msg and current_actions:
        outcome = "mixed"
        if current_errors == 0 and current_success > 0:
            outcome = "success"
        elif current_errors > 0 and current_success == 0:
            outcome = "failure"
        pairs.append(InteractionPair(
            user_message=current_user_msg,
            claude_actions=current_actions,
            outcome=outcome,
        ))

    return pairs


# ── k-NN goal prediction (multi-hypothesis) ──


def predict_goals(
    user_message: str,
    session_id: str = "",
    db: Optional[object] = None,
    tool_uses: list[dict] | None = None,
    tool_types: list[str] | None = None,
    k: int = 5,
) -> list[GoalPrediction]:
    """Predict the user's goal — returns TOP-3 competing hypotheses.

    Multi-hypothesis design:
      - k-NN weighted voting produces ranked goal_type list
      - Top-3 returned with confidence scores
      - is_ambiguous=True when top-2 gap < 0.1
      - Falls back to rule-based on cold start (also returns top-3)

    Args:
        user_message: The user's message text.
        session_id: Current session ID.
        db: HarnessDB instance for k-NN lookup.
        tool_uses: Tool use summaries (for fallback).
        tool_types: Distinct tool names (for fallback).
        k: Number of neighbors.

    Returns:
        List of 1-3 GoalPrediction objects, sorted by confidence descending.
    """
    # Compute embedding for the user message
    msg_emb = encode_cached(user_message, "user_msg", text_hash(user_message), db)

    # Try k-NN if we have stored pairs
    if db is not None:
        try:
            pairs = db.get_interaction_pairs(limit=200)
            if pairs and len(pairs) >= k:
                scored = []
                for pair in pairs:
                    emb = pair.get("user_message_embedding")
                    if emb is None:
                        continue
                    sim = _cosine_similarity(msg_emb, emb)
                    scored.append((sim, pair))

                if scored:
                    scored.sort(key=lambda x: x[0], reverse=True)
                    top_k = scored[:k]

                    # Weighted vote by goal_type (all candidates, not just winner)
                    goal_votes: dict[str, float] = {}
                    for sim, pair in top_k:
                        gt = pair.get("goal_type", "other")
                        goal_votes[gt] = goal_votes.get(gt, 0) + sim

                    total_weight = sum(goal_votes.values()) or 0.01
                    sorted_goals = sorted(goal_votes.items(), key=lambda x: -x[1])

                    results = []
                    for rank, (goal_type, weight) in enumerate(sorted_goals[:3], 1):
                        results.append(GoalPrediction(
                            goal_type=goal_type,
                            confidence=round(min(weight / total_weight, 0.95), 2),
                            complexity=0.5,
                            expected_tools=GOAL_TYPE_TOOLS.get(goal_type, []),
                            rank=rank,
                        ))

                    # Detect ambiguity
                    if len(results) >= 2:
                        gap = results[0].confidence - results[1].confidence
                        if gap < AMBIGUITY_GAP_THRESHOLD:
                            results[0].is_ambiguous = True
                            results[1].is_ambiguous = True

                    return results
        except Exception:
            pass  # k-NN failed, fall through to rule-based

    # Rule-based fallback
    if tool_uses is None:
        tool_uses = []
    if tool_types is None:
        tool_types = []

    tool_results = _classify_by_tools_all(tool_uses, tool_types)
    if tool_results and tool_results[0].goal_type != "other":
        return tool_results

    # Text-based fallback when tools are uninformative (cold start)
    text_results = _classify_by_text(user_message)
    if text_results and text_results[0].goal_type != "other":
        return text_results

    return tool_results if tool_results else text_results


def predict_goal(
    user_message: str,
    session_id: str = "",
    db: Optional[object] = None,
    tool_uses: list[dict] | None = None,
    tool_types: list[str] | None = None,
    k: int = 5,
) -> GoalPrediction:
    """Convenience wrapper: returns the TOP hypothesis only.

    For multi-hypothesis access, use predict_goals().
    """
    goals = predict_goals(user_message, session_id, db, tool_uses, tool_types, k)
    return goals[0] if goals else GoalPrediction(
        goal_type="other", confidence=0.1, complexity=0.1,
    )


# ── Hypothesis elimination via session outcome ──

def eliminate_goals_by_session(
    hypotheses: list[GoalPrediction],
    session_tool_types: list[str],
    session_tags: list[str],
    has_correction: bool = False,
    has_failure: bool = False,
) -> list[GoalPrediction]:
    """Use real session outcomes to falsify/verify competing goal hypotheses.

    Elimination rules (zero LLM cost):
      1. If a hypothesis predicts tool X should be used, but X was never called → falsified
      2. If session_tags match a hypothesis's domain → verified
      3. If user_correction occurred AND hypothesis was top-ranked → top hypothesis falsified
      4. Surviving hypotheses retain their confidence; falsified ones are marked

    Returns the list with verified/falsified flags set.
    """
    tool_set = set(session_tool_types)
    tag_set = set(session_tags)

    for h in hypotheses:
        expected = set(h.expected_tools)

        # Rule 1: expected tools not used → weak falsification
        if expected and not (expected & tool_set):
            if has_failure:
                h.falsified = True
                h.confidence = max(0.05, h.confidence - 0.3)

        # Rule 2: tag match → verification
        if tag_set and h.domain and any(t in h.domain for t in tag_set):
            h.verified = True
            h.confidence = min(0.95, h.confidence + 0.1)

        # Rule 3: user correction + top hypothesis → falsification
        if has_correction and h.rank == 1:
            h.falsified = True
            h.confidence = max(0.05, h.confidence - 0.4)

    # Re-sort after confidence adjustments
    hypotheses.sort(key=lambda x: (-x.verified, x.falsified, -x.confidence))
    for i, h in enumerate(hypotheses):
        h.rank = i + 1

    return hypotheses


def get_interaction_pairs_for_storage(
    entries: list[dict],
    session_id: str,
    db: Optional[object] = None,
) -> list[dict]:
    """Extract interaction pairs and compute embeddings for DB storage."""
    pairs = extract_interaction_pairs(entries)
    results = []
    for pair in pairs:
        emb = None
        if db is not None:
            try:
                emb = encode_cached(pair.user_message, "user_msg", text_hash(pair.user_message), db)
            except Exception:
                pass
        results.append({
            "session_id": session_id,
            "user_message": pair.user_message,
            "user_message_embedding": emb,
            "claude_actions": pair.claude_actions,
            "outcome": pair.outcome,
        })
    return results
