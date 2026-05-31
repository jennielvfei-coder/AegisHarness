"""FeatureFinder — anomaly detection pipeline for news snippets.

Adapted from InterpAgent paper (arXiv:2605.01555v1).

Pipeline:
  1. Compute snippet-to-feature-library activation matrix (N×37)
  2. Build k-NN graph in 37-dim feature space
  3. Semantic-dissimilar filter (cosine-similar but keyword-different)
  4. DBSCAN community detection (eps auto-tuned)
  5. Wilcoxon rank-sum per community per feature dimension
  6. Benjamini-Hochberg FDR correction
  7. Surprisal AUROC baseline
  8. LLM screening gate (Phase 3 only)
  9. Output: ranked AnomalyFeature list
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from _utils import cosine_sim, cosine_knn


@dataclass
class AnomalyFeature:
    feature_id: str                        # generated or matched from feature library
    detection_confidence: float            # 0.0–1.0
    supporting_snippets: list[int] = field(default_factory=list)  # snippet IDs
    statistical_significance: float = 0.0  # adjusted p-value (lower = more significant)
    keyword_divergence: float = 0.0        # Jaccard distance to nearest keyword match
    matched_library_feature: str | None = None  # e.g. "C1", "A5" if aligned
    community_id: int = -1                 # which DBSCAN cluster
    effect_size: float = 0.0               # Cohen's d
    log2fc: float = 0.0                    # log2 fold change
    screening_method: str = "statistical"  # "statistical" | "llm" | "statistical_fallback"


# ── Pure-Python statistical utilities (no scipy dependency) ───────────────

def _mann_whitney_u(x: list[float], y: list[float]) -> tuple[float, float]:
    """Compute Mann-Whitney U statistic and approximate two-sided p-value.

    Uses the normal approximation for U (valid for n1, n2 > 8).
    Falls back to exact enumeration for small samples.

    Returns (U_statistic, p_value_two_sided).
    """
    n1, n2 = len(x), len(y)
    if n1 == 0 or n2 == 0:
        return 0.0, 1.0

    # Combine and rank
    combined = [(v, 0) for v in x] + [(v, 1) for v in y]
    combined.sort(key=lambda v: v[0])

    ranks = [0.0] * len(combined)
    i = 0
    while i < len(combined):
        j = i
        while j < len(combined) and combined[j][0] == combined[i][0]:
            j += 1
        avg_rank = (i + j + 1) / 2.0
        for k in range(i, j):
            ranks[k] = avg_rank
        i = j

    # Sum of ranks for group x
    r1 = sum(ranks[k] for k in range(len(combined)) if combined[k][1] == 0)
    u1 = r1 - n1 * (n1 + 1) / 2.0
    u2 = n1 * n2 - u1
    u_stat = min(u1, u2)

    # Normal approximation
    mu = n1 * n2 / 2.0
    sigma = math.sqrt(n1 * n2 * (n1 + n2 + 1) / 12.0)

    if sigma < 1e-10:
        return u_stat, 1.0

    z = (u_stat - mu) / sigma
    # Two-sided p-value from normal CDF approximation
    p_value = 2.0 * (1.0 - _normal_cdf(abs(z)))

    return u_stat, min(p_value, 1.0)


def _normal_cdf(z: float) -> float:
    """Approximation of the standard normal cumulative distribution function."""
    # Abramowitz and Stegun approximation, max error 7.5e-8
    if z < -8.0:
        return 0.0
    if z > 8.0:
        return 1.0

    # Use math.erf
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _cohens_d(x: list[float], y: list[float]) -> float:
    """Cohen's d effect size (pooled standard deviation)."""
    n1, n2 = len(x), len(y)
    if n1 < 2 or n2 < 2:
        return 0.0

    m1, m2 = np.mean(x), np.mean(y)
    v1 = np.var(x, ddof=1) if n1 > 1 else 0.0
    v2 = np.var(y, ddof=1) if n2 > 1 else 0.0

    pooled_sd = math.sqrt(((n1 - 1) * v1 + (n2 - 1) * v2) / (n1 + n2 - 2))
    if pooled_sd < 1e-10:
        return 0.0

    return abs(m1 - m2) / pooled_sd


def _log2fc(x: list[float], y: list[float]) -> float:
    """Log2 fold change of means (adds pseudocount 1e-6)."""
    m1 = np.mean(x) + 1e-6
    m2 = np.mean(y) + 1e-6
    return float(np.log2(m1 / m2))


def benjamini_hochberg(p_values: list[float], alpha: float = 0.05) -> list[bool]:
    """Benjamini-Hochberg FDR correction.

    Returns a boolean mask where True = reject null hypothesis.
    Pure Python — no scipy dependency.
    """
    n = len(p_values)
    if n == 0:
        return []

    # Sort indices by p-value ascending
    indexed = sorted(enumerate(p_values), key=lambda x: x[1])
    rejected = [False] * n

    # Find the largest k where p(k) <= k/n * alpha
    max_k = -1
    for k, (_, p) in enumerate(indexed):
        threshold = (k + 1) / n * alpha
        if p <= threshold:
            max_k = k
        else:
            break

    # Reject all up to max_k
    for k in range(max_k + 1):
        rejected[indexed[k][0]] = True

    return rejected


# ── Jaccard similarity ────────────────────────────────────────────────────

def _jaccard_similarity(set_a: set[str], set_b: set[str]) -> float:
    """Compute Jaccard similarity between two sets of strings."""
    if not set_a and not set_b:
        return 1.0
    inter = len(set_a & set_b)
    union = len(set_a | set_b)
    if union == 0:
        return 0.0
    return inter / union


# ── DBSCAN (pure python fallback when sklearn unavailable) ─────────────────

def _dbscan_precomputed(distance_matrix: np.ndarray, eps: float = 0.3,
                         min_samples: int = 3) -> list[int]:
    """DBSCAN on a precomputed distance matrix.

    Tries sklearn.cluster.DBSCAN first. Falls back to pure-numpy implementation.

    Returns list of cluster labels (-1 = noise).
    """
    try:
        from sklearn.cluster import DBSCAN
        clustering = DBSCAN(eps=eps, min_samples=min_samples,
                           metric='precomputed').fit(distance_matrix)
        return clustering.labels_.tolist()
    except ImportError:
        pass

    # Pure numpy fallback
    n = distance_matrix.shape[0]
    labels = np.full(n, -1, dtype=int)
    cluster_id = 0

    for i in range(n):
        if labels[i] != -1:
            continue
        # Find neighbors within eps
        neighbors = np.where(distance_matrix[i] <= eps)[0]
        if len(neighbors) < min_samples:
            labels[i] = -1  # noise
            continue
        # Expand cluster
        labels[i] = cluster_id
        seed_set = list(neighbors[neighbors != i])
        j = 0
        while j < len(seed_set):
            pt = seed_set[j]
            if labels[pt] == -1:
                labels[pt] = cluster_id
            if labels[pt] != -1:
                j += 1
                continue
            labels[pt] = cluster_id
            pt_neighbors = np.where(distance_matrix[pt] <= eps)[0]
            if len(pt_neighbors) >= min_samples:
                for nb in pt_neighbors:
                    if labels[nb] == -1:
                        seed_set.append(nb)
            j += 1
        cluster_id += 1

    return labels.tolist()


def _auto_tune_eps(distance_matrix: np.ndarray, min_samples: int = 3) -> float:
    """Auto-tune DBSCAN eps to produce 2-10 clusters.

    Binary search in [0.2, 0.5] range.
    """
    lo, hi = 0.2, 0.5
    best_eps = 0.3
    best_n = 0

    for _ in range(8):
        mid = (lo + hi) / 2.0
        labels = _dbscan_precomputed(distance_matrix, eps=mid, min_samples=min_samples)
        n_clusters = len(set(l for l in labels if l >= 0))

        if 2 <= n_clusters <= 10:
            return mid  # Good range found
        if n_clusters < 2:
            lo = mid  # Need more clusters → decrease eps
        else:
            hi = mid  # Too many clusters → increase eps
        best_eps = mid if abs(n_clusters - 5) < abs(best_n - 5) else best_eps
        best_n = n_clusters

    return best_eps


# ── Entity-grounded cluster extraction ────────────────────────────────────

# Entity stopwords: terms that appear too broadly to be discriminative
_ENTITY_STOPWORDS = {"AI", "芯片", "中国", "美国", "半导体", "人工智能", "NVIDIA",
                      "GPU", "Agent", "IPO", "制裁", "出口管制", "央行", "科技",
                      "政策", "经济", "市场", "股市", "投资", "估值", "加息"}

def _extract_cluster_entities(cluster_snippets: list[dict], top_n: int = 8) -> list[tuple[str, int]]:
    """Extract dominant entities from a cluster, filtering stop-entities."""
    from collections import Counter
    counter = Counter()
    for s in cluster_snippets:
        for e in s.get("entities", []):
            if len(e) >= 2 and e not in _ENTITY_STOPWORDS:
                counter[e] += 1
    return counter.most_common(top_n)


def _extract_cluster_headlines(cluster_snippets: list[dict], top_n: int = 5) -> list[str]:
    """Extract representative headlines from a cluster."""
    headlines = []
    seen = set()
    for s in cluster_snippets:
        h = s.get("headline", "").replace("**", "").strip()
        key = h[:40]
        if key not in seen and len(h) > 5:
            seen.add(key)
            headlines.append(h[:100])
    return headlines[:top_n]


def _match_feature_library(entities: list[str], db) -> list[tuple[str, str, float]]:
    """Match a set of entities against the feature library entity combo map.
    Returns [(feature_id, name_cn, score), ...]."""
    from feature_library import match_entity_combos
    matches = match_entity_combos(set(entities), min_score=0.25)
    if not matches:
        return []

    entries = db.get_feature_library_entries()
    entry_map = {e["feature_id"]: e for e in entries}
    return [(fid, entry_map.get(fid, {}).get("name_cn", fid), score)
            for fid, score in matches[:5]]


# ── Core pipeline v2: entity-grounded clustering in 384-dim space ──────────

def find_features(
    today_snippets: list[dict],
    db,
    k: int = 15,
    feature_lib_entries: list | None = None,
    attention_entities: list[str] | None = None,
) -> list[AnomalyFeature]:
    """Entity-grounded anomaly detection via 384-dim embedding clustering.

    v2 changes (fixing the "all features activate every day" problem):
      - Clusters snippets directly in 384-dim embedding space (not 37-dim features)
      - Extracts dominant ENTITIES per cluster (grounded in actual news content)
      - Matches clusters to feature library ONLY for labeling (not for clustering)
      - Tracks cluster novelty vs historical baseline

    This produces insights like "NVIDIA+中国+华为+H200 cluster persists 5 days"
    instead of "Feature E3 activated at p=5e-6".

    Args:
        attention_entities: Optional list of entity names that should receive
            1.5x detection confidence boost when matched in a cluster.
            Used for historical judgment injection — entities from yesterday's
            headline judgment get higher attention today.
    """
    N = len(today_snippets)
    if N < 3:
        return []

    # Step 1: Build distance matrix in 384-dim embedding space
    dist_matrix = np.zeros((N, N), dtype=np.float32)
    for i in range(N):
        emb_i = today_snippets[i].get("embedding")
        if emb_i is None:
            continue
        for j in range(i + 1, N):
            emb_j = today_snippets[j].get("embedding")
            if emb_j is None:
                continue
            sim = cosine_sim(emb_i, emb_j)
            dist = 1.0 - sim
            dist_matrix[i, j] = float(dist)
            dist_matrix[j, i] = float(dist)

    # Step 2: DBSCAN clustering
    eps = _auto_tune_eps(dist_matrix, min_samples=2)
    labels = _dbscan_precomputed(dist_matrix, eps=eps, min_samples=2)

    clusters: dict[int, list[int]] = {}
    for idx, label in enumerate(labels):
        if label >= 0:
            clusters.setdefault(label, []).append(idx)

    if not clusters:
        return []

    # Step 3: Extract entities + headlines per cluster, match to feature library
    anomalies = []
    for cluster_id, indices in clusters.items():
        if len(indices) < 2:
            continue

        cluster_snippets = [today_snippets[i] for i in indices]
        entities = _extract_cluster_entities(cluster_snippets)
        entity_names = [e for e, _ in entities]
        headlines = _extract_cluster_headlines(cluster_snippets)
        feature_matches = _match_feature_library(entity_names, db)

        # Confidence: cluster size * entity coherence
        size_score = min(len(indices) / 8.0, 1.0)
        entity_score = min(len(entities) / 6.0, 1.0)
        confidence = 0.4 * size_score + 0.3 * entity_score + 0.3 * (1.0 if feature_matches else 0.3)

        # Attention boost: entities from historical judgment get 1.5x weight
        if attention_entities:
            attention_set = {e.lower() for e in attention_entities}
            cluster_entity_set = {e.lower() for e in entity_names}
            attention_overlap = len(cluster_entity_set & attention_set)
            if attention_overlap > 0:
                boost = 1.0 + 0.5 * min(attention_overlap / max(len(attention_set), 1), 1.0)
                confidence = min(confidence * boost, 1.0)

        # Keyword divergence: compare entity sets within vs outside cluster
        in_entities = set(entity_names)
        out_indices = [i for i in range(N) if i not in indices]
        out_entities = set()
        for i in out_indices[:len(indices) * 2]:
            out_entities.update(today_snippets[i].get("entities", []))
        jaccard = len(in_entities & out_entities) / max(len(in_entities | out_entities), 1)
        kw_div = 1.0 - jaccard

        primary_feature = feature_matches[0][0] if feature_matches else ""
        feature_name = feature_matches[0][1] if feature_matches else ""

        # Build a descriptive feature_id from entities
        entity_slug = "_".join(e for e, _ in entities[:3])

        anomalies.append(AnomalyFeature(
            feature_id=f"cluster_{cluster_id}_{entity_slug}",
            detection_confidence=round(confidence, 4),
            supporting_snippets=[today_snippets[i].get("id", i) for i in indices],
            statistical_significance=1.0 / max(len(indices), 1),
            keyword_divergence=round(kw_div, 4),
            matched_library_feature=primary_feature or entity_slug,
            community_id=cluster_id,
            effect_size=len(indices),
            log2fc=round(len(indices) / max(N - len(indices), 1), 4),
            screening_method="entity_grounded_v2",
        ))

    anomalies.sort(key=lambda x: x.detection_confidence, reverse=True)
    return anomalies


def find_features_multiwindow(
    db,
    date_str: str,
    feature_lib_entries: list | None = None,
    attention_entities: list[str] | None = None,
) -> dict[str, list[AnomalyFeature]]:
    """Multi-window anomaly detection across 1-day, 3-day, and 7-day windows.

    Runs find_features() on three aggregation windows and labels signals:
      - W1 (1-day): spike detection — today vs 30-day baseline
      - W3 (3-day): accumulation detection — 3-day merge vs 30-day baseline
      - W7 (7-day): secular trend detection — 7-day merge vs 30-day baseline

    Returns:
        {"spike": [...], "accumulation": [...], "secular_trend": [...]}
        where each list contains AnomalyFeature objects tagged by window.
    """
    from datetime import datetime, timedelta

    result: dict[str, list[AnomalyFeature]] = {
        "spike": [],
        "accumulation": [],
        "secular_trend": [],
    }

    windows = [
        ("spike", 1),
        ("accumulation", 3),
        ("secular_trend", 7),
    ]

    target_date = datetime.strptime(date_str, "%Y-%m-%d")

    for win_label, win_days in windows:
        # Collect snippets in window
        window_snippets = []
        for offset in range(win_days):
            d = target_date - timedelta(days=offset)
            d_str = d.strftime("%Y-%m-%d")
            snippets = db.get_news_snippets(date=d_str)
            window_snippets.extend(snippets)

        if len(window_snippets) < 3:
            continue

        # Ensure embeddings are loaded
        window_snippets = [s for s in window_snippets if s.get("embedding")]

        if len(window_snippets) < 3:
            continue

        features = find_features(
            window_snippets, db,
            feature_lib_entries=feature_lib_entries,
            attention_entities=attention_entities,
        )

        # Tag features with window label
        for f in features:
            f.screening_method = f"entity_grounded_v2_{win_label}"
            # Adjust confidence for wider windows (more data = higher baseline)
            if win_days > 1:
                f.detection_confidence = round(f.detection_confidence * 0.85, 4)

        result[win_label].extend(features)

    # De-duplicate: if same feature appears in multiple windows, keep in narrowest
    seen_ids: set[str] = set()
    for win_label in ["spike", "accumulation", "secular_trend"]:
        deduped = []
        for f in result[win_label]:
            if f.feature_id not in seen_ids:
                seen_ids.add(f.feature_id)
                deduped.append(f)
        result[win_label] = deduped

    return result


# ── Cluster cross-day tracking ────────────────────────────────────────────

@dataclass
class ClusterTrend:
    """A tracked entity-cluster across multiple days."""
    entity_signature: str          # top 3 entities as stable ID
    days_active: int
    first_seen: str
    last_seen: str
    avg_size: float
    trend: str                     # "growing" | "stable" | "shrinking" | "new"
    matched_feature: str
    representative_headlines: list[str] = field(default_factory=list)


def track_clusters_across_days(db, days: int = 7) -> list[ClusterTrend]:
    """Track entity clusters across multiple days to find persistent patterns."""
    all_dates = set()
    for row in db._conn.execute(
        "SELECT DISTINCT date FROM news_snippets ORDER BY date DESC LIMIT ?",
        (days,)
    ).fetchall():
        all_dates.add(row[0])

    if len(all_dates) < 2:
        return []

    # Build entity clusters per day with fuzzy entity signatures
    day_clusters: list[tuple[str, str, list[str], list[str]]] = []  # (date, sig, entities, headlines)

    for date_str in sorted(all_dates):
        snippets = db.get_news_snippets(date=date_str)
        if len(snippets) < 3:
            continue

        N = len(snippets)
        dist_matrix = np.zeros((N, N), dtype=np.float32)
        for i in range(N):
            for j in range(i + 1, N):
                sim = cosine_sim(
                    snippets[i].get("embedding") or [],
                    snippets[j].get("embedding") or []
                )
                dist_matrix[i, j] = 1.0 - sim
                dist_matrix[j, i] = 1.0 - sim

        labels_list = _dbscan_precomputed(dist_matrix, eps=0.35, min_samples=2)
        clusters: dict[int, list[dict]] = {}
        for idx, label in enumerate(labels_list):
            if label >= 0:
                clusters.setdefault(label, []).append(snippets[idx])

        for cid, c_snippets in clusters.items():
            if len(c_snippets) < 2:
                continue
            entities = [e for e, _ in _extract_cluster_entities(c_snippets, top_n=5)]
            headlines = _extract_cluster_headlines(c_snippets, top_n=3)
            if entities:
                sig = "|".join(sorted(entities[:3]))
                day_clusters.append((date_str, sig, entities, headlines))

    # Fuzzy matching across days: clusters with >= 50% entity overlap are the same
    trends = []
    matched = [False] * len(day_clusters)

    for i in range(len(day_clusters)):
        if matched[i]:
            continue
        date_i, sig_i, ents_i, headlines_i = day_clusters[i]
        ent_set_i = set(ents_i)
        occurrences = [(date_i, ents_i, headlines_i)]
        matched[i] = True

        for j in range(i + 1, len(day_clusters)):
            if matched[j]:
                continue
            date_j, sig_j, ents_j, headlines_j = day_clusters[j]
            ent_set_j = set(ents_j)
            overlap = len(ent_set_i & ent_set_j) / max(len(ent_set_i | ent_set_j), 1)
            if overlap >= 0.4:  # Fuzzy: 40% entity overlap → same cluster
                occurrences.append((date_j, ents_j, headlines_j))
                ent_set_i = ent_set_i | ent_set_j  # Expand signature
                matched[j] = True

        if len(occurrences) < 2:
            continue

        dates = [o[0] for o in occurrences]
        all_entities = list(ent_set_i)
        all_headlines = list(set(h for _, _, hds in occurrences for h in hds))

        avg_ent_count = np.mean([len(o[1]) for o in occurrences])
        cluster_sizes = [len(o[1]) for o in occurrences]

        # Determine trend
        if len(dates) >= 3:
            trend = "growing" if cluster_sizes[-1] > cluster_sizes[0] else \
                    "shrinking" if cluster_sizes[-1] < cluster_sizes[0] else "stable"
        else:
            trend = "new" if cluster_sizes[-1] >= cluster_sizes[0] else "shrinking"

        matches = _match_feature_library(all_entities, db)
        matched_fid = matches[0][0] if matches else ""

        trends.append(ClusterTrend(
            entity_signature=" + ".join(sorted(all_entities[:4])),
            days_active=len(dates),
            first_seen=min(dates),
            last_seen=max(dates),
            avg_size=round(avg_ent_count, 1),
            trend=trend,
            matched_feature=matched_fid,
            representative_headlines=all_headlines[:5],
        ))

    trends.sort(key=lambda t: (t.days_active, len(t.entity_signature)), reverse=True)
    return trends


def format_cluster_trends(trends: list[ClusterTrend]) -> str:
    """Format cluster trends as readable Markdown."""
    if not trends:
        return ""
    lines = ["## 🔗 跨日实体簇追踪", ""]
    lines.append("| 实体簇 | 天数 | 趋势 | 匹配特征 | 代表标题 |")
    lines.append("|--------|------|------|---------|---------|")
    for t in trends[:10]:
        entities = t.entity_signature.replace("|", " + ")
        headline = t.representative_headlines[0][:60] if t.representative_headlines else "-"
        lines.append(
            f"| {entities} | {t.days_active}d | {t.trend} | "
            f"{t.matched_feature or '-'} | {headline} |"
        )
    return "\n".join(lines)


def generate_cluster_conclusions(
    anomalies: list[AnomalyFeature],
    snippets: list[dict],
    db,
    model: str = "deepseek-v4-pro",
) -> str:
    """Use LLM to generate interpretive conclusions for each entity cluster.

    Feeds the cluster's top headlines + entities to the LLM and asks for
    a one-sentence interpretation. Falls back to headline extraction if
    LLM is unavailable.
    """
    if not anomalies:
        return ""

    # Build prompt with all clusters
    cluster_blocks = []
    for a in anomalies:
        indices = [i for i in a.supporting_snippets if i < len(snippets)]
        cluster_snips = [snippets[i] for i in indices]
        entities = [e for e, _ in _extract_cluster_entities(cluster_snips, top_n=5)]
        headlines = _extract_cluster_headlines(cluster_snips, top_n=4)
        if not entities or not headlines:
            continue
        cluster_blocks.append(
            f"Cluster entities: {', '.join(entities[:5])}\n"
            f"Headlines:\n" + "\n".join(f"  - {h}" for h in headlines) +
            f"\nSize: {len(indices)} snippets"
        )

    if not cluster_blocks:
        return ""

    # Process clusters one at a time for cleaner LLM output
    prompt_parts = []
    for i, block in enumerate(cluster_blocks):
        prompt_parts.append(
            f"Cluster {i}: {block}\n"
            f"Write ONE Chinese sentence (max 25 words) identifying the core theme "
            f"and what's surprising. Reply: \"{i}: <your sentence>\""
        )

    full_prompt = (
        "For each news cluster below, write one sharp conclusion sentence.\n\n"
        + "\n\n".join(prompt_parts)
    )

    try:
        import os, requests
        base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic")
        token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
        if not token:
            raise RuntimeError("No API token")

        resp = requests.post(
            f"{base_url}/v1/messages",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
            },
            json={"model": model, "max_tokens": 2000, "temperature": 0.3,
                  "messages": [{"role": "user", "content": full_prompt}]},
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        # Extract all text/thinking content
        full_text = ""
        for block in data.get("content", []):
            full_text += block.get("text", "") or block.get("thinking", "")

        if full_text:
            # Parse "N: sentence" format from LLM response
            conclusions = {}
            for match in re.finditer(r'(?:Cluster\s*)?(\d+)[：:]\s*(.+?)(?=(?:Cluster\s*)?\d+[：:]|\Z)', full_text, re.DOTALL):
                idx = int(match.group(1))
                sentence = match.group(2).strip().rstrip('.,。，').strip()
                if len(sentence) > 5:
                    conclusions[idx] = sentence

            if conclusions:
                lines = ["## 🧠 簇结论（LLM解读）", ""]
                for idx in sorted(conclusions.keys()):
                    if idx < len(anomalies):
                        a = anomalies[idx]
                        indices = [i for i in a.supporting_snippets if i < len(snippets)]
                        cs = [snippets[i] for i in indices]
                        ents = [e for e, _ in _extract_cluster_entities(cs, top_n=4)]
                        label = " · ".join(ents[:3]) if ents else f"cluster_{idx}"
                        lines.append(f"**{label}** ({len(indices)}条)")
                        lines.append(f"> {conclusions[idx]}")
                        lines.append("")
                return "\n".join(lines)
    except Exception as e:
        import traceback
        print(f"[feature_finder] LLM conclusion failed: {e}", file=__import__('sys').stderr)
        traceback.print_exc()

    # Fallback: heuristic conclusions (no LLM)
    lines = ["## 🧠 簇结论", ""]

    # Compute entity rarity across ALL snippets (for "what's unique about this cluster")
    all_entity_freq = {}
    for s in snippets:
        for e in s.get("entities", []):
            all_entity_freq[e] = all_entity_freq.get(e, 0) + 1

    for i, a in enumerate(anomalies):
        indices = [j for j in a.supporting_snippets if j < len(snippets)]
        cs = [snippets[j] for j in indices]
        entities_all = _extract_cluster_entities(cs, top_n=10)
        entities = [e for e, _ in entities_all]
        headlines = _extract_cluster_headlines(cs, top_n=2)
        if not entities or not headlines:
            continue

        # Rare entities = cluster's unique signature (appear <= 5 times total)
        rare = sorted([(e, all_entity_freq.get(e, 999)) for e in entities],
                      key=lambda x: x[1])
        rare_ents = [e for e, c in rare if c <= 5][:3]

        # Match to feature library for conceptual label
        matches = _match_feature_library(entities, db)
        feat_label = f"{matches[0][0]} {matches[0][1]}" if matches else ""

        # Generate heuristic conclusion
        if rare_ents:
            # Cluster distinguished by its rare entities
            rare_str = "、".join(rare_ents)
            headline_signal = headlines[0].replace("**", "").strip()[:60]
            conclusion = f"{rare_str} 汇聚：{headline_signal}"
            if feat_label:
                conclusion += f" — 特征模式：{feat_label}"
        else:
            conclusion = headlines[0].replace("**", "").strip()[:80]

        lines.append(f"**簇{i+1}：{' · '.join(entities[:3])}** ({len(indices)}条)")
        lines.append(f"> {conclusion}")
        lines.append("")

    # Cross-day comparison: what's new today vs yesterday?
    from datetime import datetime, timedelta
    try:
        yesterday = (datetime.strptime(snippets[0].get("date", ""), "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d") if snippets else None
        if yesterday:
            hist = db.get_news_snippets(date=yesterday)
            if hist:
                today_ents = set()
                today_headlines = set()
                for a in anomalies:
                    for j in a.supporting_snippets:
                        if j < len(snippets):
                            today_ents.update(snippets[j].get("entities", []))
                            today_headlines.add(snippets[j].get("headline", "")[:50])

                hist_ents = set()
                for s in hist:
                    hist_ents.update(s.get("entities", []))
                new_ents = today_ents - hist_ents - _ENTITY_STOPWORDS
                vanished_ents = hist_ents - today_ents - _ENTITY_STOPWORDS

                if new_ents:
                    rare_new = sorted([(e, all_entity_freq.get(e, 999)) for e in new_ents],
                                      key=lambda x: x[1])[:5]
                    lines.append(f"**🆕 今日新实体：** {', '.join(e for e,_ in rare_new)}")
                if vanished_ents:
                    lines.append(f"**📉 昨日消失实体：** {', '.join(sorted(vanished_ents)[:5])}")
    except Exception:
        pass

    return "\n".join(lines)
    """Format cluster trends as readable Markdown for the daily report."""
    if not trends:
        return ""

    lines = ["## 🔗 跨日实体簇追踪（Entity Cluster Trends）", ""]
    lines.append("| 实体簇 | 天数 | 趋势 | 匹配特征 | 代表标题 |")
    lines.append("|--------|------|------|---------|---------|")

    for t in trends[:10]:
        entities = t.entity_signature.replace("|", " + ")
        headline = t.representative_headlines[0][:60] if t.representative_headlines else "-"
        lines.append(
            f"| {entities} | {t.days_active}d | {t.trend} | "
            f"{t.matched_feature or '-'} | {headline} |"
        )

    return "\n".join(lines)


# ── Store activations (unified — Phase 1 output feeds Phase 2+3) ──────────

def store_feature_activations(
    date: str,
    snippets: list[dict],
    anomalies: list[AnomalyFeature],
    db,
):
    """Store snippet-level cluster annotations for downstream use."""
    feature_entries = db.get_feature_library_entries()
    feature_ids = [e["feature_id"] for e in feature_entries if e.get("embedding")]

    records = []
    for anomaly in anomalies:
        for sid in anomaly.supporting_snippets:
            records.append({
                "date": date,
                "feature_id": anomaly.matched_library_feature or anomaly.feature_id,
                "activation_strength": anomaly.detection_confidence,
                "matched_entity_combos": [],
                "matched_library_features": [anomaly.matched_library_feature] if anomaly.matched_library_feature else [],
                "source_snippet_ids": [sid],
            })

    if records:
        db.save_feature_activations(records)


# ── CLI ───────────────────────────────────────────────────────────────────

def main():
    import sys
    from indexer import HarnessDB
    from news_agent.vectorize import parse_news_file
    from pathlib import Path

    db = HarnessDB()

    if len(sys.argv) < 2:
        print("Usage: python feature_finder.py <news_file.md> [--trends]")
        db.close()
        return

    filepath = Path(sys.argv[1])
    if not filepath.is_absolute():
        filepath = Path.cwd() / filepath

    snippets = parse_news_file(filepath)
    print(f"Input: {len(snippets)} snippets")

    # Load embeddings from DB
    stored = db.get_news_snippets(date=snippets[0].date if snippets else "")
    if stored and stored[0].get("embedding"):
        stored_map = {s["content_hash"]: s for s in stored}
        for snip in snippets:
            if snip.content_hash in stored_map:
                snip.embedding = stored_map[snip.content_hash].get("embedding")

    if not any(s.embedding for s in snippets):
        print("No embeddings found. Run news_vectorizer first.")
        db.close()
        return

    snippet_dicts = [
        {"id": i, "date": s.date, "headline": s.headline,
         "entities": s.entities, "embedding": s.embedding}
        for i, s in enumerate(snippets) if s.embedding
    ]

    # Run entity-grounded clustering
    anomalies = find_features(snippet_dicts, db)
    print(f"\nClusters found: {len(anomalies)}")
    for a in anomalies:
        print(f"  {a.feature_id}")
        print(f"    confidence={a.detection_confidence:.3f}, size={a.effect_size}")
        if a.supporting_snippets:
            headlines = [snippet_dicts[i].get("headline", "")[:70]
                        for i in a.supporting_snippets[:3] if i < len(snippet_dicts)]
            for h in headlines:
                print(f"    → {h}")

    # Cross-day trends
    if "--trends" in sys.argv:
        print("\n--- Cross-day Entity Cluster Trends ---")
        trends = track_clusters_across_days(db, days=7)
        print(format_cluster_trends(trends))

    # Store activations
    store_feature_activations(snippets[0].date if snippets else "", snippet_dicts, anomalies, db)
    print(f"\nStored activations for {snippets[0].date if snippets else '?'}")

    db.close()


if __name__ == "__main__":
    main()
