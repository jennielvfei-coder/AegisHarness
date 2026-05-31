"""Source Bias Tracker — detect systematic source biases in news reporting.

Tracks per-source claim patterns and compares against multi-source consensus
to identify sources that systematically overestimate or underestimate certain
event categories.

Usage:
    from duonews.source_bias import track_source_bias, annotate_contradictions
    bias_scores = track_source_bias(date_str, db)
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from dataclasses import dataclass, field


@dataclass
class SourceBiasProfile:
    """Per-source bias tracking profile."""
    source_name: str
    entity_category: str                # e.g., "semiconductor", "geopolitics"
    total_claims: int = 0
    positive_claims: int = 0             # Claims with positive sentiment
    negative_claims: int = 0            # Claims with negative sentiment
    overclaimed_ratio: float = 0.0      # Claims not cross-verified within 24h
    verified_claims: int = 0
    last_updated: str = ""


def _ensure_table(db) -> None:
    """Create source_bias_tracker table if it doesn't exist."""
    db._conn.execute("""
        CREATE TABLE IF NOT EXISTS source_bias_tracker (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_name TEXT NOT NULL,
            entity_category TEXT NOT NULL,
            total_claims INTEGER DEFAULT 0,
            positive_claims INTEGER DEFAULT 0,
            negative_claims INTEGER DEFAULT 0,
            verified_claims INTEGER DEFAULT 0,
            overclaimed_ratio REAL DEFAULT 0.0,
            last_updated TEXT,
            UNIQUE(source_name, entity_category)
        )
    """)
    db._conn.commit()


def track_source_bias(date_str: str, db) -> dict[str, SourceBiasProfile]:
    """Track source bias for today's news snippets.

    Analyzes sentiment distribution per source×entity_category and compares
    against the average across all sources for the same category.

    Returns a dict keyed by "source_name|entity_category" → SourceBiasProfile.
    """
    _ensure_table(db)

    profiles: dict[str, SourceBiasProfile] = {}

    snippets = db.get_news_snippets(date=date_str)
    if not snippets:
        return profiles

    # Sentiment keywords
    positive_kw = ["增长", "突破", "合作", "缓和", "开放", "创新", "利好",
                   "growth", "breakthrough", "cooperation", "progress", "rally"]
    negative_kw = ["下跌", "制裁", "冲突", "风险", "危机", "衰退", "威胁", "限制",
                   "decline", "sanction", "conflict", "risk", "crisis", "threat"]

    # Category detection keywords
    category_kw = {
        "semiconductor": ["芯片", "半导体", "NVIDIA", "TSMC", "H200", "B200", "EUV",
                          "chip", "semiconductor", "wafer"],
        "geopolitics": ["制裁", "出口管制", "贸易", "关税", "地缘",
                        "sanction", "export control", "tariff", "geopolitic"],
        "ai": ["AI", "模型", "GPT", "大模型", "智能体", "Agent",
               "artificial intelligence", "LLM", "transformer"],
        "economy": ["GDP", "通胀", "利率", "就业", "PMI", "股市",
                    "inflation", "interest rate", "employment", "stock market"],
    }

    # Build per-source×category stats
    source_cat_stats: dict[str, dict] = {}  # "source|cat" → {pos, neg, total}

    for s in snippets:
        sources = s.get("sources", [])
        headline = s.get("headline", "")
        summary = s.get("summary", "")

        if not sources:
            continue

        text = f"{headline} {summary}".lower()
        source_name = sources[0].get("name", "unknown") if isinstance(sources, list) and sources else str(sources)

        # Determine entity categories
        matched_cats = []
        for cat, kws in category_kw.items():
            if any(kw.lower() in text for kw in kws):
                matched_cats.append(cat)

        if not matched_cats:
            matched_cats = ["general"]

        # Determine sentiment
        pos_hits = sum(1 for kw in positive_kw if kw.lower() in text)
        neg_hits = sum(1 for kw in negative_kw if kw.lower() in text)
        sentiment = "neutral"
        if pos_hits > neg_hits:
            sentiment = "positive"
        elif neg_hits > pos_hits:
            sentiment = "negative"

        for cat in matched_cats:
            key = f"{source_name}|{cat}"
            if key not in source_cat_stats:
                source_cat_stats[key] = {"positive": 0, "negative": 0, "total": 0}
            source_cat_stats[key]["total"] += 1
            if sentiment == "positive":
                source_cat_stats[key]["positive"] += 1
            elif sentiment == "negative":
                source_cat_stats[key]["negative"] += 1

    # Calculate global averages per category for bias detection
    cat_global: dict[str, dict] = {}
    for key, stats in source_cat_stats.items():
        _, cat = key.split("|", 1)
        if cat not in cat_global:
            cat_global[cat] = {"total": 0, "positive": 0, "negative": 0, "sources": 0}
        cat_global[cat]["total"] += stats["total"]
        cat_global[cat]["positive"] += stats["positive"]
        cat_global[cat]["negative"] += stats["negative"]
        cat_global[cat]["sources"] += 1

    # Build profiles and detect bias
    for key, stats in source_cat_stats.items():
        source_name, cat = key.split("|", 1)
        total = stats["total"]

        # Calculate this source's positive ratio vs category average
        src_pos_ratio = stats["positive"] / max(total, 1)
        global_stats = cat_global.get(cat, {"positive": 0, "total": 1, "sources": 1})
        global_pos_ratio = global_stats["positive"] / max(global_stats["total"], 1)

        # Overclaimed ratio: deviation from category average
        # Positive deviation = this source is more positive than average for this category
        overclaimed = src_pos_ratio - global_pos_ratio

        profile = SourceBiasProfile(
            source_name=source_name,
            entity_category=cat,
            total_claims=total,
            positive_claims=stats["positive"],
            negative_claims=stats["negative"],
            verified_claims=0,  # Populated by cross-day verification
            overclaimed_ratio=round(overclaimed, 4),
            last_updated=date_str,
        )
        profiles[key] = profile

        # Persist to DB
        db._conn.execute(
            """INSERT OR REPLACE INTO source_bias_tracker
               (source_name, entity_category, total_claims, positive_claims,
                negative_claims, verified_claims, overclaimed_ratio, last_updated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (source_name, cat, total, stats["positive"], stats["negative"],
             0, round(overclaimed, 4), date_str),
        )

    db._conn.commit()

    # Log significant biases
    for key, profile in profiles.items():
        if abs(profile.overclaimed_ratio) > 0.3 and profile.total_claims >= 3:
            direction = "positive-skewed" if profile.overclaimed_ratio > 0 else "negative-skewed"
            print(f"[source_bias] {profile.source_name}/{profile.entity_category}: "
                  f"{direction} bias={profile.overclaimed_ratio:+.3f} "
                  f"(n={profile.total_claims})", file=sys.stderr)

    return profiles


def annotate_contradictions(contradiction_pairs: list[dict],
                             db) -> list[dict]:
    """Annotate contradiction pairs with source bias information.

    For each contradiction pair, look up the source bias profiles and add
    bias annotations to help readers understand potential source skew.
    """
    annotated = []
    for pair in contradiction_pairs:
        entity = pair.get("entity", "")
        pos_headline = pair.get("positive", "")
        neg_headline = pair.get("negative", "")

        # Detect category from entity
        cat = "general"
        if any(kw in entity for kw in ["芯片", "NVIDIA", "半导体", "chip"]):
            cat = "semiconductor"
        elif any(kw in entity for kw in ["制裁", "贸易", "出口", "geopolitic"]):
            cat = "geopolitics"

        # Look up bias profiles for this category
        rows = db._conn.execute(
            """SELECT source_name, overclaimed_ratio FROM source_bias_tracker
               WHERE entity_category = ? AND total_claims >= 3
               ORDER BY ABS(overclaimed_ratio) DESC LIMIT 3""",
            (cat,),
        ).fetchall()

        bias_notes = []
        for row in rows:
            bias_notes.append(f"{row[0]}(bias={row[1]:+.2f})")

        pair["bias_annotations"] = bias_notes
        annotated.append(pair)

    return annotated
