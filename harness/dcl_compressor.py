"""DCL — Decision Compression Layer (Phase 5).

Compresses ICL Tier 1+2 (~4-11 items) into 1-3 actionable Judgment Cards.

Each card answers: What changed? So what? Now what?

Pipeline:
  1. Causal Dependency Graph (shared feature → time window containment → library chain)
  2. PageRank root-cause identification
  3. Counterfactual test (impact radius)
  4. Actionability scoring (domain relevance + action implication + time window)
  5. Disruptiveness = 1 - max(similarity to confirmed historical patterns)
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class JudgmentCard:
    judgment_id: str
    date: str
    judgment: str              # Core judgment, ≤50 chars
    confidence: float          # 0.0-1.0
    disruptiveness: float      # 0.0-1.0
    supporting_hypotheses: list[str] = field(default_factory=list)
    counterfactual: str = ""
    action_implication: str = ""
    verification_window: str = ""
    causal_radius: int = 0
    page_rank: float = 0.0


# ── Causal graph construction ─────────────────────────────────────────────

def _build_causal_graph(
    icl_items: list[dict],
    feature_entries: list[dict],
) -> dict[str, list[str]]:
    """Build directed causal dependency graph among ICL items.

    Edge inference rules:
      1. Shared anomaly feature: H_a and H_b from same anomaly → H_a → H_b
      2. Time window containment: if H_a's window contains H_b's → H_a → H_b
      3. Feature library chain: "通常指向" field links features
      4. Explicit dependency: contrastive test references another's signal
    """
    graph: dict[str, list[str]] = {}
    ids = [item.get("id", f"n{i}") for i, item in enumerate(icl_items)]

    for id_ in ids:
        graph[id_] = []

    for i, item_a in enumerate(icl_items):
        id_a = item_a.get("id", f"n{i}")
        for j, item_b in enumerate(icl_items):
            if i == j:
                continue
            id_b = item_b.get("id", f"n{j}")
            edge_weight = 0

            # Rule 1: Same source feature
            if item_a.get("id") == item_b.get("id"):
                edge_weight += 0.25

            # Rule 3: Feature library chain
            entry_a = next((e for e in feature_entries if e.get("feature_id") == item_a.get("id")), {})
            entry_b = next((e for e in feature_entries if e.get("feature_id") == item_b.get("id")), {})
            impl_a = entry_a.get("typical_implication", "")
            feature_b_name = entry_b.get("name_cn", "")
            if feature_b_name and feature_b_name in impl_a:
                edge_weight += 0.25

            # Rule 2: Hypothesis iteration difference (proxy for time containment)
            iter_a = item_a.get("iteration", 0)
            iter_b = item_b.get("iteration", 0)
            if iter_a > iter_b:
                edge_weight += 0.25

            if edge_weight >= 0.25:
                graph[id_a].append(id_b)

    return graph


def _pagerank(graph: dict[str, list[str]], damping: float = 0.85, iters: int = 30) -> dict[str, float]:
    """Simple PageRank on a directed graph."""
    nodes = list(graph.keys())
    n = len(nodes)
    if n == 0:
        return {}

    pr = {node: 1.0 / n for node in nodes}

    for _ in range(iters):
        new_pr = {}
        for node in nodes:
            rank = (1 - damping) / n
            for other in nodes:
                if node in graph.get(other, []):
                    out_degree = max(len(graph[other]), 1)
                    rank += damping * pr[other] / out_degree
            new_pr[node] = rank
        pr = new_pr

    # Normalize
    total = sum(pr.values())
    if total > 0:
        pr = {k: v / total for k, v in pr.items()}

    return pr


# ── Main compression ──────────────────────────────────────────────────────

def compress(
    icl_report,              # ICLReport from Phase 4
    feature_entries: list[dict],
    date: str = "",
    user_domains: list[str] | None = None,
) -> list[JudgmentCard]:
    """Compress ICL Tier 1+2 into 1-3 Judgment Cards.

    Args:
        icl_report: ICLReport from icl_compressor.
        feature_entries: Feature library entries.
        date: Date string.
        user_domains: User's interest domains for actionability scoring.

    Returns:
        1-3 JudgmentCard objects.
    """
    if user_domains is None:
        user_domains = ["AI", "具身智能", "脑机接口", "社会心理学", "未来预测学",
                       "沉浸式交互", "一人公司"]

    # Combine Tier 1 + Tier 2 items
    items = icl_report.tier1_items + icl_report.tier2_items
    if not items:
        return []

    # Step 1: Build causal graph
    graph = _build_causal_graph(items, feature_entries)

    # Step 2: PageRank
    pr = _pagerank(graph)

    # Step 3-4: Score each item
    scored = []
    for item in items:
        id_ = item.get("id", "?")
        pr_score = pr.get(id_, 1.0 / max(len(pr), 1))

        # Causal radius: number of nodes that depend on this one
        causal_radius = sum(1 for v in graph.values() if id_ in v)

        # Counterfactual impact
        impact_radius = len(graph.get(id_, []))

        # Actionability: domain match + time window
        actionability = _score_actionability(item, user_domains)

        # Disruptiveness: 1 - max similarity to confirmed patterns
        disruptiveness = _score_disruptiveness(item, feature_entries)

        # Composite judgment score
        score = (
            0.25 * pr_score +
            0.15 * min(causal_radius / 5, 1.0) +
            0.10 * min(impact_radius / 5, 1.0) +
            0.20 * actionability +
            0.15 * disruptiveness +
            0.15 * item.get("confidence_score", item.get("composite", 0.5))
        )

        # Build judgment statement
        name = item.get("name", item.get("id", ""))
        definition = item.get("definition", item.get("statement", ""))[:80]
        layer = item.get("layer", "unknown")
        layer_prefix = {"latent": "深层结构", "structural": "机制层面", "surface": "表层信号"}.get(layer, "")
        judgment = f"{layer_prefix}: {name} — {definition[:50]}" if layer_prefix else definition[:60]

        # Build counterfactual
        counterfactual = _generate_counterfactual(item)

        # Build action implication
        action = _generate_action(item, user_domains)

        scored.append({
            "item": item,
            "id": id_,
            "score": score,
            "pr_score": pr_score,
            "causal_radius": causal_radius,
            "impact_radius": impact_radius,
            "actionability": actionability,
            "disruptiveness": disruptiveness,
            "judgment": judgment[:80],
            "counterfactual": counterfactual,
            "action": action,
        })

    # Sort and take top 3
    scored.sort(key=lambda x: x["score"], reverse=True)
    top = scored[:3]

    cards = []
    for s in top:
        card = JudgmentCard(
            judgment_id=f"DCL-{date}-{uuid.uuid4().hex[:8]}",
            date=date,
            judgment=s["judgment"],
            confidence=round(s["score"], 2),
            disruptiveness=round(s["disruptiveness"], 2),
            supporting_hypotheses=[s["id"]],
            counterfactual=s["counterfactual"],
            action_implication=s["action"],
            verification_window="3-7 天",
            causal_radius=s["causal_radius"],
            page_rank=round(s["pr_score"], 4),
        )
        cards.append(card)

    return cards


def _score_actionability(item: dict, user_domains: list[str]) -> float:
    """Score actionability based on domain relevance and time pressure."""
    score = 0.2
    text = (item.get("definition", "") + " " + item.get("statement", "")).lower()

    # Domain match
    for domain in user_domains:
        if domain.lower() in text:
            score += 0.15
            break

    # Urgency from item
    urgency = item.get("urgency", 0.3)
    score += urgency * 0.2

    # Boosted items = more actionable (linked to deep drivers)
    if item.get("boosted"):
        score += 0.1

    return min(score, 1.0)


def _score_disruptiveness(item: dict, feature_entries: list[dict]) -> float:
    """Disruptiveness = 1 - similarity to typical implications of known features.

    A disruptive signal is one that does NOT match the expected pattern for its feature type.
    """
    entry = next((e for e in feature_entries if e.get("feature_id") == item.get("id")), {})
    impl = entry.get("typical_implication", "")

    # If the implication is well-known (lots of / separators = many known pathways),
    # it's less disruptive
    known_pathways = impl.count("/") + impl.count("、") + 1
    familiarity = min(known_pathways / 5.0, 1.0)

    # Higher disruptiveness = lower familiarity
    return round(1.0 - familiarity * 0.6, 2)


def _generate_counterfactual(item: dict) -> str:
    """Generate a simple counterfactual: 'If wrong, the alternative is...'"""
    layer = item.get("layer", "")
    alt_map = {
        "latent": "如果错了：可能只是短期波动而非结构性转变，标志是未来30天内模式消退。",
        "structural": "如果错了：可能只是数据采集窗口的偶然对齐，标志是下周模式不持续。",
        "surface": "如果错了：可能是单一事件的过度解读，标志是无后续跟进报道。",
    }
    return alt_map.get(layer, "如果错了：可能是随机波动，标志是未来一周无新证据。")


def _generate_action(item: dict, user_domains: list[str]) -> str:
    """Generate a simple action implication."""
    text = (item.get("definition", "") + item.get("statement", "")).lower()
    for domain in user_domains:
        if domain.lower() in text:
            return f"关注{domain}领域的后续发展，本周追踪相关信号是否强化。"
    return "持续观察，暂不建议行动。"


def format_injection(cards: list[JudgmentCard]) -> str:
    """Generate DCL injection text for the daily report."""
    if not cards:
        return ""

    lines = ["## ⚡ 今日判断（DCL）", ""]
    for i, c in enumerate(cards):
        disrupt_label = "高" if c.disruptiveness > 0.6 else "中" if c.disruptiveness > 0.3 else "低"
        lines.append(f"**判断 #{i+1} [{c.confidence:.2f}]**")
        lines.append(f"{c.judgment}")
        lines.append(f"→ 行动：{c.action_implication}")
        lines.append(f"→ 如果判断错了：{c.counterfactual}")
        lines.append(f"→ 验证窗口：{c.verification_window} | 颠覆性：{disrupt_label} | 影响半径：{c.causal_radius}")
        lines.append("")

    return "\n".join(lines)
