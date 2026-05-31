"""Co-activation Detector — track feature co-occurrence patterns over time.

Phase 3a of the InterpAgent-inspired news optimization pipeline.

Pipeline:
  1. Build activation time series from feature_activations table (14-day window)
  2. Compute pairwise Pearson r on activation strengths
  3. Filter significant pairs (r > 0.7, co-occurrence >= 3 days)
  4. Generate verification queries for co-activating pairs
  5. Inject queries into next news search cycle
  6. Update confidence on next-day evidence (boost +0.1 / decay ×0.8)

Reuses:
  - observer._detect_recurring_failures() pattern adapted from tool+domain →
    feature pair tracking
  - feature_library for feature definition lookup
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np


# ── Data model ────────────────────────────────────────────────────────────

@dataclass
class CoactivationPair:
    feature_id_a: str
    feature_id_b: str
    pearson_r: float
    cooccurrence_count: int
    verification_query: str
    confidence: float = 0.5
    last_verified: str = ""
    id: int | None = None  # DB row ID for updates


# ── Pearson r (pure Python fallback) ──────────────────────────────────────

def _pearson_r(x: list[float], y: list[float]) -> float:
    """Pearson correlation coefficient. Pure Python, no scipy dependency."""
    n = len(x)
    if n < 3 or len(y) != n:
        return 0.0

    mx = np.mean(x)
    my = np.mean(y)
    sx = np.std(x, ddof=1)
    sy = np.std(y, ddof=1)

    if sx < 1e-10 or sy < 1e-10:
        return 0.0

    cov = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y)) / (n - 1)
    return cov / (sx * sy)


# ── Co-activation analysis ────────────────────────────────────────────────

def compute_pairwise_correlations(
    db,
    window_days: int = 14,
    r_threshold: float = 0.7,
    min_cooccur: int = 3,
    min_data_points: int = 5,
) -> list[CoactivationPair]:
    """Compute pairwise Pearson r over the activation time series.

    For each pair of feature_ids that appear in the feature_activations table:
      - Build a daily activation vector over window_days
      - Compute Pearson r
      - Filter by r_threshold and min_cooccur

    Returns significant co-activation pairs, sorted by Pearson r descending.
    """
    # Load activation history
    all_acts = db.get_feature_activations(days=window_days)
    if len(all_acts) < 10:
        return []

    # Group by date and feature_id
    date_feature_strengths: dict[str, dict[str, float]] = {}
    for act in all_acts:
        d = act["date"]
        fid = act["feature_id"]
        strength = act.get("activation_strength", 0.0)
        if d not in date_feature_strengths:
            date_feature_strengths[d] = {}
        # Sum activation strengths for same (date, feature_id)
        date_feature_strengths[d][fid] = date_feature_strengths[d].get(fid, 0.0) + strength

    if len(date_feature_strengths) < 3:
        return []

    # Get all unique feature_ids
    all_fids = set()
    for daily in date_feature_strengths.values():
        all_fids.update(daily.keys())
    fid_list = sorted(all_fids)

    # Build time series for each feature_id
    dates = sorted(date_feature_strengths.keys())
    time_series: dict[str, list[float]] = {}
    for fid in fid_list:
        time_series[fid] = [
            date_feature_strengths[d].get(fid, 0.0)
            for d in dates
        ]

    # Compute pairwise Pearson r
    pairs = []
    for i in range(len(fid_list)):
        for j in range(i + 1, len(fid_list)):
            fid_a, fid_b = fid_list[i], fid_list[j]
            x = time_series[fid_a]
            y = time_series[fid_b]

            r = _pearson_r(x, y)
            if abs(r) < r_threshold:
                continue

            # Count co-occurrence days (both features activated on same day)
            cooccur = sum(
                1 for xi, yi in zip(x, y)
                if xi > 0 and yi > 0
            )
            if cooccur < min_cooccur:
                continue

            pairs.append(CoactivationPair(
                feature_id_a=fid_a,
                feature_id_b=fid_b,
                pearson_r=round(r, 4),
                cooccurrence_count=cooccur,
                verification_query="",
                confidence=min(0.5 + abs(r) * 0.3, 0.9),
            ))

    pairs.sort(key=lambda p: abs(p.pearson_r), reverse=True)
    return pairs


# ── Verification query generation ─────────────────────────────────────────

def generate_verification_query(
    pair: CoactivationPair,
    db,
    feature_entries: list[dict] | None = None,
) -> str:
    """Generate a conjunctive search query from a co-activating feature pair.

    Uses feature library definitions to extract key entities and constructs
    a search query for the next news cycle.

    Example: C4(象征性让步) × A5(同步异源) →
      "出口管制 中国芯片 自给率 华为 替代"
    """
    if feature_entries is None:
        feature_entries = db.get_feature_library_entries()

    entry_map = {e["feature_id"]: e for e in feature_entries}

    keywords: set[str] = set()
    for fid in [pair.feature_id_a, pair.feature_id_b]:
        entry = entry_map.get(fid, {})
        # Extract key terms from definition and examples
        text = (entry.get("definition", "") + " " +
                entry.get("examples", "") + " " +
                entry.get("typical_implication", ""))

        # Extract Chinese and English key phrases (2+ chars)
        import re
        for token in re.findall(r'[一-鿿\w]{2,}', text):
            # Skip common stop words
            if token not in ("系统", "多个", "原本", "长期", "分散", "发生",
                            "动作", "极短", "时间", "内同", "步出", "通常",
                            "定义", "层级理由", "指向", "一个", "这种", "通过",
                            "可以", "进行", "可能", "或者", "以及", "如果"):
                keywords.add(token)

    # Build search query (max 5-8 terms)
    key_terms = sorted(keywords, key=len, reverse=True)[:6]
    return " ".join(key_terms)


def get_active_verification_queries(
    db,
    limit: int = 3,
    min_confidence: float = 0.5,
) -> list[str]:
    """Return today's active verification queries for the news search step.

    Injects additional search terms into the World News API query list.
    """
    pairs = compute_pairwise_correlations(db, window_days=14)
    pairs = [p for p in pairs if p.confidence >= min_confidence]

    queries = []
    for p in pairs[:limit]:
        if not p.verification_query:
            p.verification_query = generate_verification_query(p, db)
        queries.append(p.verification_query)

    return queries


# ── Confidence update cycle ───────────────────────────────────────────────

def update_cycle(
    date: str,
    db,
    window_days: int = 14,
) -> list[CoactivationPair]:
    """Run the full co-activation update cycle for a given date.

    1. Compute pairwise correlations from historical activations
    2. Generate verification queries for significant pairs
    3. Update confidence: check if verification queries returned results
    4. Persist updated pairs to DB

    Returns active co-activation pairs.
    """
    pairs = compute_pairwise_correlations(db, window_days=window_days)

    if not pairs:
        return []

    # Load today's snippets to check if verification queries hit
    today_snippets = db.get_news_snippets(date=date)
    today_headlines = " ".join(
        s.get("headline", "") for s in today_snippets
    ).lower()

    # Load stored pairs from previous cycles
    feature_entries = db.get_feature_library_entries()

    for pair in pairs:
        if not pair.verification_query:
            pair.verification_query = generate_verification_query(
                pair, db, feature_entries
            )

        # Simple check: does the verification query appear in today's headlines?
        query_terms = pair.verification_query.lower().split()
        hits = sum(1 for term in query_terms if term in today_headlines)

        if hits >= 2:
            # Evidence found → boost confidence
            pair.confidence = min(pair.confidence + 0.1, 1.0)
            pair.last_verified = date
        elif hits == 0:
            # No evidence → decay
            pair.confidence = max(pair.confidence * 0.8, 0.1)

        # Persist
        db.save_coactivation_pair({
            "id": pair.id,
            "feature_id_a": pair.feature_id_a,
            "feature_id_b": pair.feature_id_b,
            "pearson_r": pair.pearson_r,
            "cooccurrence_count": pair.cooccurrence_count,
            "verification_query": pair.verification_query,
            "confidence": pair.confidence,
            "last_verified": pair.last_verified,
        })

    return pairs


# ── CLI ───────────────────────────────────────────────────────────────────

def main():
    import sys
    from indexer import HarnessDB

    db = HarnessDB()

    date = sys.argv[1] if len(sys.argv) > 1 else "2026-05-21"
    pairs = update_cycle(date, db)

    print(f"Co-activation pairs for {date}: {len(pairs)}")
    for p in sorted(pairs, key=lambda x: abs(x.pearson_r), reverse=True)[:10]:
        print(f"  {p.feature_id_a} × {p.feature_id_b}: "
              f"r={p.pearson_r:.3f}, co={p.cooccurrence_count}, "
              f"conf={p.confidence:.3f}")
        if p.verification_query:
            print(f"    query: {p.verification_query[:80]}")

    queries = get_active_verification_queries(db)
    print(f"\nActive verification queries: {len(queries)}")
    for q in queries:
        print(f"  {q}")

    db.close()


if __name__ == "__main__":
    main()
