"""ICL — Information Compression Layer (Phase 4).

Compresses ~20 active hypotheses + anomaly features into 3 tiers:
  Tier 1 (Must Watch) — composite >= 0.7, 1-3 items/day
  Tier 2 (Observe)    — composite 0.4-0.7, 3-8 items/day
  Tier 3 (Noise)      — composite < 0.4, archived silently

Scoring: Impact(0.4) + Urgency(0.3) + Confidence(0.3)

Reuses consistency_verifier's 3-way classification pattern.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class ICLReport:
    date: str
    input_count: int
    tier1_count: int
    tier2_count: int
    tier3_count: int
    compression_ratio: float
    tier1_items: list[dict] = field(default_factory=list)
    tier2_items: list[dict] = field(default_factory=list)
    tier3_items: list[dict] = field(default_factory=list)
    dropped_high_confidence: int = 0


def compress(
    hypotheses: list,
    anomaly_features: list,
    attention_injected: list,
    feature_entries: list[dict],
    date: str = "",
) -> ICLReport:
    """Compress all signals into 3-tier priority output.

    Args:
        hypotheses: Hypothesis objects from Phase 3.
        anomaly_features: AnomalyFeature objects from Phase 1.
        attention_injected: FeatureActivation objects from Phase 2.
        feature_entries: Feature library entries for context.
        date: Date string.

    Returns:
        ICLReport with tier assignments.
    """
    items = []

    # Add hypotheses
    for h in hypotheses:
        if hasattr(h, 'status') and h.status == "popped":
            continue
        statement = h.statement if hasattr(h, 'statement') else h.get('statement', '')
        rank = h.aggregate_rank if hasattr(h, 'aggregate_rank') else h.get('aggregate_rank', 0.5)
        iteration = h.iteration_count if hasattr(h, 'iteration_count') else h.get('iteration_count', 0)
        items.append({
            "source": "hypothesis",
            "id": h.hypothesis_id if hasattr(h, 'hypothesis_id') else h.get('hypothesis_id', '?'),
            "statement": statement,
            "rank": rank,
            "iteration": iteration,
        })

    # Add anomaly features
    for a in anomaly_features:
        fid = a.matched_library_feature if hasattr(a, 'matched_library_feature') else a.get('matched_library_feature', '?')
        conf = a.detection_confidence if hasattr(a, 'detection_confidence') else a.get('detection_confidence', 0.5)
        entry = next((e for e in feature_entries if e.get("feature_id") == fid), {})
        items.append({
            "source": "anomaly",
            "id": fid,
            "name": entry.get("name_cn", fid),
            "definition": entry.get("definition", "")[:60],
            "layer": entry.get("layer", "unknown"),
            "confidence": conf,
        })

    # Add attention-injected features
    for f in attention_injected:
        fid = f.feature_id if hasattr(f, 'feature_id') else f.get('feature_id', '?')
        entry = next((e for e in feature_entries if e.get("feature_id") == fid), {})
        items.append({
            "source": "attention",
            "id": fid,
            "name": entry.get("name_cn", fid),
            "layer": entry.get("layer", "unknown"),
            "weight": f.attention_weight if hasattr(f, 'attention_weight') else f.get('attention_weight', 0),
            "boosted": f.latent_boosted if hasattr(f, 'latent_boosted') else f.get('latent_boosted', False),
            "boost_source": f.boost_source if hasattr(f, 'boost_source') else f.get('boost_source', ''),
        })

    # Score each item on 3 dimensions
    scored = []
    for item in items:
        impact = _score_impact(item, feature_entries)
        urgency = _score_urgency(item)
        confidence = _score_confidence(item)

        composite = 0.4 * impact + 0.3 * urgency + 0.3 * confidence
        item["impact"] = impact
        item["urgency"] = urgency
        item["confidence_score"] = confidence
        item["composite"] = round(composite, 4)
        scored.append(item)

    # Assign tiers
    tier1 = [i for i in scored if i["composite"] >= 0.7]
    tier2 = [i for i in scored if 0.4 <= i["composite"] < 0.7]
    tier3 = [i for i in scored if i["composite"] < 0.4]

    # Sort within tiers by composite descending
    tier1.sort(key=lambda x: x["composite"], reverse=True)
    tier2.sort(key=lambda x: x["composite"], reverse=True)

    # Count high-confidence items dropped to Tier 3
    dropped_high = sum(
        1 for i in tier3 if i.get("confidence_score", 0) > 0.6
    )

    return ICLReport(
        date=date,
        input_count=len(items),
        tier1_count=len(tier1),
        tier2_count=len(tier2),
        tier3_count=len(tier3),
        compression_ratio=(len(tier1) + len(tier2)) / max(len(items), 1),
        tier1_items=tier1,
        tier2_items=tier2,
        tier3_items=tier3,
        dropped_high_confidence=dropped_high,
    )


def _score_impact(item: dict, feature_entries: list[dict]) -> float:
    """Score structural impact: how much would this change the system if true?"""
    score = 0.3  # Base

    entry = next((e for e in feature_entries if e.get("feature_id") == item.get("id")), {})
    layer = item.get("layer", entry.get("layer", "unknown"))

    # Latent features have highest impact
    if layer == "latent":
        score += 0.3
    elif layer == "structural":
        score += 0.15

    # Latent-boosted = higher impact
    if item.get("boosted"):
        score += 0.15
        # Check boost source: if linked to multiple latent features
        boost = item.get("boost_source", "")
        if boost:
            score += 0.05

    # Implication analysis: "结构性"/"系统性"/"根本性"
    impl = entry.get("typical_implication", "")
    if any(w in impl for w in ["结构性", "系统性", "根本性", "不可逆", "终局"]):
        score += 0.1

    # Cross-domain: if anomaly or hypothesis spans multiple domains
    if item.get("source") == "anomaly":
        score += 0.05

    return min(score, 1.0)


def _score_urgency(item: dict) -> float:
    """Score time urgency: how soon must we act or verify?"""
    score = 0.3  # Base

    # Hypotheses with short verification windows are urgent
    iteration = item.get("iteration", 0)
    if iteration >= 2:
        score += 0.1  # Been through multiple cycles — resolution imminent

    # Attention-injected features are "today's signal" — inherently urgent
    if item.get("source") == "attention":
        score += 0.15

    # High-weight features are urgent
    weight = item.get("weight", 0)
    if weight > 0.1:
        score += 0.15
    elif weight > 0.05:
        score += 0.1

    return min(score, 1.0)


def _score_confidence(item: dict) -> float:
    """Score statistical confidence in this signal."""
    score = 0.3  # Base

    # From hypothesis aggregate rank
    rank = item.get("rank", 0.5)
    score += rank * 0.3

    # From anomaly detection confidence
    conf = item.get("confidence", 0.5)
    if conf > 0.7:
        score += 0.2

    # Multiple iterations = more trustworthy
    if item.get("iteration", 0) >= 2:
        score += 0.1

    return min(score, 1.0)


def format_injection(report: ICLReport) -> tuple[str, str]:
    """Generate Tier 1 (核心信号) and Tier 2 (持续观察) injection text."""
    tier1_text = ""
    if report.tier1_items:
        lines = ["## 🎯 今日核心信号（ICL Tier 1）"]
        for i, item in enumerate(report.tier1_items[:3]):
            name = item.get("name", item.get("id", "?"))
            definition = item.get("definition", item.get("statement", ""))[:70]
            boosted = " ↗" + item.get("boost_source", "") if item.get("boosted") else ""
            lines.append(
                f"**#{i+1} [{item['composite']:.2f}] {name}{boosted}** — {definition}"
            )
        tier1_text = "\n".join(lines)

    tier2_text = ""
    if report.tier2_items:
        lines = ["## 📋 持续观察（ICL Tier 2）"]
        lines.append("| 信号 | 综合分 | 类型 | 说明 |")
        lines.append("|------|--------|------|------|")
        for item in report.tier2_items[:8]:
            name = item.get("name", item.get("id", "?"))[:20]
            source = {"hypothesis": "假设", "anomaly": "异常", "attention": "注入"}.get(
                item.get("source", ""), item.get("source", "")
            )
            desc = item.get("statement", item.get("definition", ""))[:50]
            lines.append(f"| {name} | {item['composite']:.2f} | {source} | {desc} |")
        tier2_text = "\n".join(lines)

    return tier1_text, tier2_text


def format_archive(report: ICLReport) -> dict:
    """Generate signal archive JSON for Tier 3 items."""
    return {
        "date": report.date,
        "compression": {
            "input": report.input_count,
            "tier1": report.tier1_count,
            "tier2": report.tier2_count,
            "tier3": report.tier3_count,
            "ratio": report.compression_ratio,
            "dropped_high_confidence": report.dropped_high_confidence,
        },
        "tier3_archived": [
            {
                "id": item.get("id"), "name": item.get("name", ""),
                "composite": item["composite"],
                "reason": f"composite={item['composite']:.2f} < 0.4 threshold",
                "source": item.get("source"),
            }
            for item in report.tier3_items
        ],
    }
