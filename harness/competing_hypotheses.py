"""Competing Hypotheses Engine — seed-propose-test-revise cycle.

Phase 3b of the InterpAgent-inspired news optimization pipeline.

Architecture (adapted from FeatureExplainer):
  1. Seed: template-based causal hypotheses (always available) + optional LLM enhance
  2. Propose: contrastive tests that would falsify each hypothesis
  3. Test: 7-metric evaluation battery against today's news evidence
  4. Filter: False Structure Filter (5 rules) removes spurious patterns
  5. Rank: Pareto dominance filter keeps non-dominated hypotheses
  6. Detect: polysemanticity detection (competing valid interpretations)
  7. Stop/Revise: convergence check, iterate or publish

Reuses:
  - omega_predictor's multi-hypothesis pattern (multiple beliefs per evidence)
  - consistency_verifier's 3-way classification pattern (goal/belief/skill)
  - session_quality's weighted scalar feedback for aggregate ranking
  - feature_library for causal template matching
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from _utils import cosine_sim


# ── Data model ────────────────────────────────────────────────────────────

@dataclass
class ContrastiveTest:
    """A falsifiable test for a hypothesis."""
    description: str
    observable_signal: str      # What to look for
    window_days: int            # How long to wait
    expected_if_true: str
    expected_if_false: str


@dataclass
class Hypothesis:
    hypothesis_id: str
    parent_id: str | None = None
    anomaly_feature_id: str = ""
    statement: str = ""                        # "Anomaly X is caused by Y"
    competing_alternatives: list[str] = field(default_factory=list)
    contrastive_tests: list[ContrastiveTest] = field(default_factory=list)
    metric_scores: dict[str, float] = field(default_factory=dict)
    aggregate_rank: float = 0.0
    status: str = "seeded"                     # seeded|testing|revising|confirmed|popped
    iteration_count: int = 0
    causal_chain: list[str] = field(default_factory=list)
    created_at: str = ""
    last_evaluated: str = ""
    source: str = "template"                   # "template" | "llm"


# ── Causal template library loader ────────────────────────────────────────

def _load_causal_templates() -> dict:
    """Load causal_templates.json or return built-in defaults."""
    template_path = Path(__file__).resolve().parent / "causal_templates.json"
    if template_path.exists():
        try:
            return json.loads(template_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


# ── Step 1: Seed hypotheses ───────────────────────────────────────────────

def seed_hypothesis(
    anomaly_feature_id: str,
    feature_entries: list[dict],
    db=None,
    enhance_with_llm: bool = False,
) -> list[Hypothesis]:
    """Generate competing causal hypotheses for a detected anomaly.

    Layer 1 (always available): Template-based causal hypotheses from causal_templates.json
    Layer 2 (optional): LLM-enhanced context-specific variants

    Args:
        anomaly_feature_id: The matched feature library ID (e.g., "A5", "C4").
        feature_entries: Feature library entries (from DB).
        db: HarnessDB instance (for LLM API config).
        enhance_with_llm: If True and LLM available, add context-specific variants.

    Returns:
        2-3 competing Hypothesis objects.
    """
    templates = _load_causal_templates()
    entry = next((e for e in feature_entries if e["feature_id"] == anomaly_feature_id), None)
    if entry is None:
        return []

    feature_name = entry.get("name_cn", anomaly_feature_id)
    domain = entry.get("category", "unknown domain")
    definition = entry.get("definition", "")

    # Build placeholder values for template slots
    placeholders = {
        "domain": domain,
        "feature_name": feature_name,
        "root_cause": f"系统性{domain}压力",
        "shock": "外部冲击",
        "time_window": "3-7天",
        "actor": "关键参与者",
        "asset": "相关资产",
        "metric": "核心指标",
        "central_bank": "央行",
        "operation": "公开市场操作",
        "market": "市场",
        "problem": "结构性问题",
        "indicator": "指标",
        "concession": "让步",
        "condition": "条件",
        "opponent": "对手方",
        "level": "高层",
        "information": "非公开信息",
        "from_layer": "上游",
        "to_layer": "下游",
        "technology": "新技术",
        "application": "应用",
    }

    hypotheses = []
    template_key = feature_name.split(" ")[0]  # Handle "(English Name)" suffix
    # Also try the raw name
    tmpl = templates.get(feature_name) or templates.get(template_key)

    if tmpl:
        for i, t in enumerate(tmpl.get("templates", [])):
            statement = t["statement"]
            # Fill placeholders
            for key, val in placeholders.items():
                statement = statement.replace("{" + key + "}", val)

            h = Hypothesis(
                hypothesis_id=_make_id(anomaly_feature_id, i),
                anomaly_feature_id=anomaly_feature_id,
                statement=statement,
                source="template",
                created_at=_now(),
                status="seeded",
            )
            # Add competing alternatives (other templates for same feature)
            h.competing_alternatives = [
                _make_id(anomaly_feature_id, j)
                for j in range(len(tmpl["templates"]))
                if j != i
            ]
            hypotheses.append(h)

    # If no templates found, generate a generic hypothesis
    if not hypotheses:
        h = Hypothesis(
            hypothesis_id=_make_id(anomaly_feature_id, 0),
            anomaly_feature_id=anomaly_feature_id,
            statement=f"在{domain}领域检测到{feature_name}异常，由结构性因素驱动。",
            source="generic",
            created_at=_now(),
            status="seeded",
        )
        hypotheses.append(h)

    # Optional LLM enhancement (Phase 3)
    if enhance_with_llm and len(hypotheses) < 3 and db:
        try:
            llm_hyp = _llm_enhance_hypothesis(anomaly_feature_id, entry, db)
            if llm_hyp:
                hypotheses.append(llm_hyp)
        except Exception:
            pass  # LLM failure → template-only output

    return hypotheses[:3]


# ── Step 2: Propose contrastive tests ─────────────────────────────────────

def propose_contrastive_tests(h: Hypothesis, entry: dict) -> list[ContrastiveTest]:
    """Generate 2-3 falsifiable tests for a hypothesis.

    Each test describes what should be observable if the hypothesis is true
    vs what should be observable if it's false.
    """
    tests = []

    # Test 1: Core prediction
    tests.append(ContrastiveTest(
        description=f"核心预测检验",
        observable_signal=f"与{h.statement[:30]}一致的新证据出现",
        window_days=7,
        expected_if_true=f"同领域出现强化{h.anomaly_feature_id}信号的新事件",
        expected_if_false=f"同领域事件与{h.anomaly_feature_id}模式矛盾",
    ))

    # Test 2: Cross-domain check
    tests.append(ContrastiveTest(
        description=f"跨域一致性检验",
        observable_signal=f"相关领域是否同步出现类似模式",
        window_days=7,
        expected_if_true=f"至少1个相关领域同步出现类似信号",
        expected_if_false=f"其他领域独立运行，无同步信号",
    ))

    # Test 3: Alternative cause check
    tests.append(ContrastiveTest(
        description=f"替代原因排除",
        observable_signal=f"排除{h.anomaly_feature_id}的常见替代解释",
        window_days=3,
        expected_if_true=f"常见替代解释的证据未出现",
        expected_if_false=f"替代解释的证据出现，削弱{h.statement[:20]}假设",
    ))

    return tests


# ── Step 3: Evaluate evidence (7-metric battery) ──────────────────────────

def evaluate_evidence(
    h: Hypothesis,
    today_snippets: list[dict],
    db,
    feature_entries: list[dict] | None = None,
    use_llm_coherence: bool = False,
) -> dict[str, float]:
    """Score a hypothesis on the 7-metric battery.

    Returns dict of {metric_name: score} where all scores are in [0, 1].
    """
    if feature_entries is None:
        feature_entries = db.get_feature_library_entries()

    # Get the matched feature library entry embedding
    entry = next((e for e in feature_entries
                 if e["feature_id"] == h.anomaly_feature_id), {})
    hyp_emb = entry.get("embedding")

    metrics = {}

    # 1. Detection F1: Does the hypothesis predict today's headlines?
    metrics["detection_f1"] = _compute_detection_f1(h, today_snippets, entry)

    # 2. Fuzzing F1: Robustness — drop the best evidence
    metrics["fuzzing_f1"] = _compute_fuzzing_f1(h, today_snippets, entry)

    # 3. Surprisal AUROC: How unexpected are matching snippets?
    metrics["surprisal_auroc"] = _compute_surprisal(h, today_snippets, db, entry)

    # 4. Embedding Similarity: Cosine similarity between hypothesis and evidence
    if hyp_emb:
        sims = []
        for s in today_snippets:
            emb = s.get("embedding")
            if emb:
                sims.append(cosine_sim(hyp_emb, emb))
        metrics["embedding_similarity"] = float(np.mean(sims)) if sims else 0.0
    else:
        metrics["embedding_similarity"] = 0.0

    # 5. Statistical Separability (MWU proxy via activation spread)
    metrics["statistical_separability"] = _compute_separability(h, today_snippets, entry)

    # 6. Cohen's d: Effect size proxy
    metrics["cohens_d"] = _compute_effect_size(h, today_snippets, entry)

    # 7. LLM Coherence (configurable — only for Tier 1 / near-confirmed)
    if use_llm_coherence and db:
        metrics["llm_coherence"] = _compute_llm_coherence(h, entry, db)
    else:
        # Proxy: embedding similarity of statement vs snippets
        metrics["llm_coherence"] = metrics.get("embedding_similarity", 0.5)

    return metrics


def _compute_detection_f1(h: Hypothesis, snippets: list[dict], entry: dict) -> float:
    """How well does the hypothesis statement match today's headlines?"""
    if not snippets:
        return 0.0
    # Simple: fraction of headlines that share keywords with the hypothesis statement
    statement_words = set(h.statement.lower().split())
    hits = 0
    for s in snippets:
        headline = s.get("headline", "").lower()
        match_count = sum(1 for w in statement_words if len(w) > 1 and w in headline)
        if match_count >= 2:
            hits += 1
    return min(hits / max(len(snippets), 1), 1.0)


def _compute_fuzzing_f1(h: Hypothesis, snippets: list[dict], entry: dict) -> float:
    """Robustness: detection F1 after dropping the single best-matching snippet."""
    if len(snippets) <= 1:
        return 0.0
    statement_words = set(h.statement.lower().split())
    best_idx = -1
    best_hits = -1
    for i, s in enumerate(snippets):
        headline = s.get("headline", "").lower()
        hits = sum(1 for w in statement_words if len(w) > 1 and w in headline)
        if hits > best_hits:
            best_hits = hits
            best_idx = i
    # Remove best snippet and recompute
    remaining = [s for i, s in enumerate(snippets) if i != best_idx]
    if not remaining:
        return 0.0
    hits = sum(
        1 for s in remaining
        if sum(1 for w in statement_words if len(w) > 1 and w in s.get("headline", "").lower()) >= 2
    )
    return min(hits / len(remaining), 1.0)


def _compute_surprisal(
    h: Hypothesis, snippets: list[dict], db, entry: dict
) -> float:
    """Surprisal = 1 - (similarity of matching snippets to historical baseline).

    High surprisal = this pattern is genuinely rare.
    Low surprisal = this pattern appears regularly (not a real anomaly).
    """
    if not snippets:
        return 0.0
    hyp_emb = entry.get("embedding")
    if hyp_emb is None:
        return 0.0

    # Compute similarity to today's snippets
    today_sims = []
    for s in snippets:
        emb = s.get("embedding")
        if emb:
            today_sims.append(cosine_sim(hyp_emb, emb))
    if not today_sims:
        return 0.0

    # Compare to random historical snippets
    hist = db.get_news_snippets(days=30)
    hist_sims = []
    for s in hist[:50]:  # Sample 50 historical
        emb = s.get("embedding")
        if emb:
            hist_sims.append(cosine_sim(hyp_emb, emb))
    if not hist_sims:
        return 0.5  # Default: neither surprising nor expected

    # Surprisal = 1 - (today_mean / historical_mean capped at 1)
    ratio = np.mean(today_sims) / max(np.mean(hist_sims), 1e-6)
    ratio_capped = min(ratio, 2.0)  # Cap at 2x historical baseline
    surprisal = max(0.0, 1.0 - (ratio_capped - 1.0))
    return float(surprisal)


def _compute_separability(h: Hypothesis, snippets: list[dict], entry: dict) -> float:
    """Simplified separability: how distinct are the matching vs non-matching snippets?"""
    if len(snippets) < 3:
        return 0.0
    statement_words = set(h.statement.lower().split())
    matching = []
    non_matching = []
    for s in snippets:
        headline = s.get("headline", "").lower()
        hits = sum(1 for w in statement_words if len(w) > 1 and w in headline)
        emb = s.get("embedding")
        if emb:
            if hits >= 2:
                matching.append(emb)
            else:
                non_matching.append(emb)
    if not matching or not non_matching:
        return 0.0

    # Mean cosine similarity between matching and non-matching
    match_centroid = np.mean(matching, axis=0)
    nonmatch_centroid = np.mean(non_matching, axis=0)
    separation = cosine_sim(match_centroid.tolist(), nonmatch_centroid.tolist())
    # Transform: 1 = perfectly separated (cosine=0), 0 = identical (cosine=1)
    return 1.0 - float(separation)


def _compute_effect_size(h: Hypothesis, snippets: list[dict], entry: dict) -> float:
    """Simplified Cohen's d proxy using activation strength spread."""
    if len(snippets) < 3:
        return 0.0
    statement_words = set(h.statement.lower().split())
    matching_scores = []
    non_matching_scores = []
    for s in snippets:
        headline = s.get("headline", "").lower()
        hits = sum(1 for w in statement_words if len(w) > 1 and w in headline)
        if hits >= 2:
            matching_scores.append(hits)
        else:
            non_matching_scores.append(hits * 0.5)
    if not matching_scores or not non_matching_scores:
        return 0.0

    m1 = np.mean(matching_scores)
    m2 = np.mean(non_matching_scores)
    pooled_sd = math.sqrt((np.var(matching_scores, ddof=1) + np.var(non_matching_scores, ddof=1)) / 2)
    if pooled_sd < 1e-6:
        return 0.0
    return float(abs(m1 - m2) / pooled_sd)


def _compute_llm_coherence(h: Hypothesis, entry: dict, db) -> float:
    """LLM-judged coherence (expensive — use sparingly)."""
    try:
        import requests
        import yaml

        config_path = Path(__file__).resolve().parent / "harness_config.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        refiner_cfg = config.get("refiner", {})

        prompt = (
            f"Rate how well this hypothesis explains the detected anomaly, on a 0-10 scale:\n\n"
            f"Hypothesis: {h.statement}\n"
            f"Feature: {entry.get('name_cn', 'unknown')} — {entry.get('definition', '')}\n\n"
            f"Respond with JSON: {{\"score\": 0-10, \"reasoning\": \"<1 sentence>\"}}"
        )

        resp = requests.post(
            f"{refiner_cfg.get('base_url', 'http://localhost:11434')}/v1/messages",
            headers={
                "Authorization": f"Bearer {refiner_cfg.get('token', '')}",
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
            },
            json={"model": "deepseek-v4-pro", "max_tokens": 200,
                  "temperature": 0.0,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if "content" in data and isinstance(data["content"], list):
            text = "\n".join(
                i.get("text", "") for i in data["content"]
                if i.get("type") == "text"
            )
            result = json.loads(text.split("```")[0])  # Handle code block wrap
            return float(result.get("score", 5)) / 10.0
    except Exception:
        pass
    return 0.5  # Default on failure


# ── Step 4: False Structure Filter ────────────────────────────────────────

def false_structure_filter(
    h: Hypothesis,
    metric_scores: dict[str, float],
    snippets: list[dict],
    db,
) -> tuple[bool, str]:
    """5-rule False Structure Filter.

    Returns (is_false_structure, reason).
    True = this hypothesis is likely a spurious pattern, should be excluded.
    """
    # Rule 1: Low surprisal → too obvious, likely data bias
    if metric_scores.get("surprisal_auroc", 0.5) < 0.3:
        return True, "low_surprisal: pattern too common, likely data bias not real anomaly"

    # Rule 2: Statistical-semantic discordance
    if (metric_scores.get("cohens_d", 0) > 0.8 and
        metric_scores.get("llm_coherence", 0.5) < 0.4):
        return True, "stat_semantic_mismatch: large effect but weak semantic coherence — vector space artifact"

    # Rule 3: Single-source bias (adapted: source credibility check)
    if snippets:
        # Check diversity of sources
        sources = set()
        for s in snippets[:10]:
            src_list = s.get("sources", [])
            for src in (src_list or []):
                if isinstance(src, dict):
                    sources.add(src.get("name", ""))
        if len(sources) == 1 and sources != {"unknown"}:
            sole_source = next(iter(sources))
            cred = 0.5
            for src in (snippets[0].get("sources") or []):
                if isinstance(src, dict) and src.get("name") == sole_source:
                    cred = src.get("credibility", 0.5)
            if cred < 0.65:
                return True, f"single_source_low_cred: sole source '{sole_source}' credibility={cred:.2f}"

    # Rule 4: Insufficient entity diversity
    all_entities = set()
    for s in snippets:
        all_entities.update(s.get("entities", []))
    if len(all_entities) < 3:
        return True, f"low_entity_diversity: only {len(all_entities)} unique entities across snippets"

    # Rule 5: Weekday autocorrelation check
    # Simplified: check if activation pattern correlates with specific days
    # (Full check requires multi-day data; skip if only single day available)
    # For now: check if all snippets from same section (indicating narrow domain)
    sections = set(s.get("section", "") for s in snippets)
    if len(sections) == 1 and len(snippets) > 5:
        return True, f"single_section_bias: all {len(snippets)} snippets from same section '{next(iter(sections))}'"

    return False, ""


# ── Step 5: Pareto filter ─────────────────────────────────────────────────

def pareto_filter(hypotheses: list[Hypothesis]) -> list[Hypothesis]:
    """Pareto dominance filter: keep only non-dominated hypotheses.

    h1 dominates h2 if h1 >= h2 on all metrics AND h1 > h2 on at least one.
    """
    if len(hypotheses) <= 1:
        return hypotheses

    # List of metric names (must be present in all hypotheses)
    all_metrics = set()
    for h in hypotheses:
        all_metrics.update(h.metric_scores.keys())
    metric_keys = sorted(all_metrics)

    if not metric_keys:
        return hypotheses

    n = len(hypotheses)
    dominated = [False] * n

    for i in range(n):
        if dominated[i]:
            continue
        mi = hypotheses[i].metric_scores
        for j in range(n):
            if i == j or dominated[j]:
                continue
            mj = hypotheses[j].metric_scores

            # Check if mi dominates mj
            i_better = False
            i_at_least = True
            for key in metric_keys:
                vi = mi.get(key, 0.0)
                vj = mj.get(key, 0.0)
                if vi < vj:
                    i_at_least = False
                    break
                if vi > vj:
                    i_better = True

            if i_at_least and i_better:
                dominated[j] = True

    return [h for i, h in enumerate(hypotheses) if not dominated[i]]


# ── Step 6: Polysemanticity detection ──────────────────────────────────────

def detect_polysemanticity(surviving: list[Hypothesis]) -> list[list[Hypothesis]]:
    """Detect polysemantic hypotheses: competing valid interpretations.

    If >= 2 hypotheses survive Pareto filtering and each excel on disjoint
    metric subsets, they are polysemantic — both may be partially true.
    """
    if len(surviving) < 2:
        return [surviving]

    # Check semantic distinctness of hypothesis statements
    groups: list[list[Hypothesis]] = []
    remaining = list(surviving)

    while remaining:
        h = remaining.pop(0)
        group = [h]

        # Find hypotheses that are semantically similar to h (not polysemantic)
        others_still_remaining = []
        for other in remaining:
            # Simple check: keyword overlap in statements
            words_h = set(h.statement.lower().split())
            words_o = set(other.statement.lower().split())
            overlap = len(words_h & words_o) / max(len(words_h | words_o), 1)

            if overlap > 0.5:
                # Similar — same semantic group
                group.append(other)
            else:
                others_still_remaining.append(other)

        remaining = others_still_remaining
        groups.append(group)

    return groups


# ── Step 7: Stop condition ────────────────────────────────────────────────

def stop_condition(h: Hypothesis, prev_rank: float = None) -> tuple[bool, str]:
    """Check if a hypothesis has converged.

    Stop if:
      a) >= 5/7 metrics above 0.6 (high confidence)
      b) 3 iterations with no improvement
      c) Evidence consistently contradicts
    """
    scores = h.metric_scores
    if not scores:
        return False, "no_scores_yet"

    high_metrics = sum(1 for v in scores.values() if v > 0.6)
    if high_metrics >= 5 and h.iteration_count >= 2:
        return True, f"converged: {high_metrics}/7 metrics above 0.6"

    if h.iteration_count >= 3 and prev_rank is not None:
        if abs(h.aggregate_rank - prev_rank) < 0.05:
            return True, "stalled: no improvement in 3 iterations"

    if h.iteration_count >= 3 and high_metrics <= 2:
        return True, "falsified: evidence consistently contradicts"

    return False, "continue"


# ── Aggregate ranking ─────────────────────────────────────────────────────

def compute_aggregate_rank(h: Hypothesis) -> float:
    """Compute aggregate rank from 7 metric scores.

    Weights: Detection F1(0.15) + Fuzzing F1(0.10) + Surprisal(0.20) +
             Embedding Sim(0.10) + Separability(0.15) + Cohen's d(0.15) +
             LLM Coherence(0.15)
    """
    weights = {
        "detection_f1": 0.15,
        "fuzzing_f1": 0.10,
        "surprisal_auroc": 0.20,
        "embedding_similarity": 0.10,
        "statistical_separability": 0.15,
        "cohens_d": 0.15,
        "llm_coherence": 0.15,
    }
    rank = 0.0
    for key, w in weights.items():
        rank += h.metric_scores.get(key, 0.0) * w
    return round(rank, 4)


# ── Format causal chain ───────────────────────────────────────────────────

def format_causal_chain(h: Hypothesis) -> str:
    """Format the auditable causal chain for injection into the daily report."""
    chain_lines = h.causal_chain or []
    if not chain_lines:
        return f"_[{h.hypothesis_id}] {h.status}: {h.statement[:80]}..._ — 因果链为空（新建假设）"

    lines = [f"**{h.hypothesis_id}** [{h.status}] — {h.statement[:80]}..."]
    lines.append("")
    for step in chain_lines:
        lines.append(f"  → {step}")

    if h.metric_scores:
        top_metrics = sorted(h.metric_scores.items(), key=lambda x: -x[1])[:3]
        lines.append("")
        lines.append(f"  指标: " + ", ".join(
            f"{k}={v:.2f}" for k, v in top_metrics
        ))

    return "\n".join(lines)


# ── Full cycle runner ─────────────────────────────────────────────────────

def run_hypothesis_cycle(
    anomaly_features: list,
    today_snippets: list[dict],
    db,
    use_llm_coherence: bool = False,
    use_llm_seed: bool = False,
) -> list[Hypothesis]:
    """Run the full propose-test-revise cycle for all active anomalies.

    Args:
        anomaly_features: AnomalyFeature objects from Phase 1 feature_finder.
        today_snippets: Today's snippets from DB.
        db: HarnessDB instance.
        use_llm_coherence: Enable LLM coherence scoring (expensive).
        use_llm_seed: Enable LLM-enhanced hypothesis seeding.

    Returns:
        List of surviving, ranked Hypothesis objects.
    """
    feature_entries = db.get_feature_library_entries()

    all_hypotheses: list[Hypothesis] = []

    for anomaly in anomaly_features:
        fid = anomaly.matched_library_feature
        if not fid:
            continue

        # Check if this anomaly already has active hypotheses
        existing = db.get_active_hypotheses()
        existing_for_feature = [h for h in existing
                               if h.get("anomaly_feature_id") == fid]

        if existing_for_feature:
            # Continue existing hypothesis cycle
            for h_dict in existing_for_feature:
                h = Hypothesis(
                    hypothesis_id=h_dict["hypothesis_id"],
                    parent_id=h_dict.get("parent_id"),
                    anomaly_feature_id=h_dict.get("anomaly_feature_id", fid),
                    statement=h_dict.get("statement", ""),
                    competing_alternatives=h_dict.get("competing_alternatives", []),
                    status=h_dict.get("status", "testing"),
                    iteration_count=h_dict.get("iteration_count", 1),
                    causal_chain=h_dict.get("causal_chain", []),
                )
                h.contrastive_tests = propose_contrastive_tests(
                    h, feature_entries[0] if feature_entries else {}
                )
                h.metric_scores = evaluate_evidence(
                    h, today_snippets, db, feature_entries,
                    use_llm_coherence=use_llm_coherence
                )
                h.aggregate_rank = compute_aggregate_rank(h)
                h.causal_chain.append(
                    f"Tested(metrics: {json.dumps({k:round(v,2) for k,v in h.metric_scores.items()})})"
                )
                all_hypotheses.append(h)
        else:
            # Seed new hypotheses
            new = seed_hypothesis(fid, feature_entries, db, enhance_with_llm=use_llm_seed)
            for h in new:
                entry = next((e for e in feature_entries if e["feature_id"] == fid), {})
                h.contrastive_tests = propose_contrastive_tests(h, entry)
                h.metric_scores = evaluate_evidence(
                    h, today_snippets, db, feature_entries,
                    use_llm_coherence=use_llm_coherence
                )
                h.aggregate_rank = compute_aggregate_rank(h)
                h.causal_chain.append(f"Seeded(from {anomaly.matched_library_feature})")
                h.causal_chain.append(
                    f"Tested(metrics: {json.dumps({k:round(v,2) for k,v in h.metric_scores.items()})})"
                )
                all_hypotheses.append(h)

    # False Structure Filter
    survivors = []
    for h in all_hypotheses:
        is_false, reason = false_structure_filter(h, h.metric_scores, today_snippets, db)
        if is_false:
            # Log false structure
            db.save_false_structure(
                date=_now()[:10],
                description=f"Hypothesis {h.hypothesis_id}: {h.statement[:60]}",
                filter_rule=reason.split(":")[0].strip(),
                filter_reason=reason,
                snippet_ids=[],
            )
            h.status = "popped"
            h.causal_chain.append(f"Popped(false_structure: {reason})")
            # Still persist the popped hypothesis for audit
            _persist_hypothesis(h, db)
        else:
            survivors.append(h)

    # Pareto filter
    survivors = pareto_filter(survivors)

    # Polysemanticity groups
    poly_groups = detect_polysemanticity(survivors)

    # Check stop conditions, revise if needed
    final = []
    for group in poly_groups:
        for h in group:
            prev_rank = None  # Could load from previous cycle
            should_stop, reason = stop_condition(h, prev_rank)

            if should_stop:
                h.status = "confirmed" if "converged" in reason else "popped"
                h.causal_chain.append(f"Stop({reason})")
            else:
                if h.status != "seeded":
                    h.status = "revising"
                h.iteration_count += 1
                h.causal_chain.append(f"Revise({reason})")
                # In full implementation: call revise_hypothesis() with LLM
                # For now: keep the hypothesis as-is for next cycle

            h.last_evaluated = _now()
            _persist_hypothesis(h, db)
            final.append(h)

    # Sort by aggregate rank (lower = better)
    final.sort(key=lambda x: x.aggregate_rank, reverse=True)
    return final


def _persist_hypothesis(h: Hypothesis, db) -> None:
    """Save hypothesis to DB."""
    db.save_hypothesis({
        "hypothesis_id": h.hypothesis_id,
        "parent_id": h.parent_id,
        "anomaly_feature_id": h.anomaly_feature_id,
        "statement": h.statement,
        "competing_alternatives": h.competing_alternatives,
        "contrastive_tests": [
            {"description": t.description,
             "observable_signal": t.observable_signal,
             "window_days": t.window_days,
             "expected_if_true": t.expected_if_true,
             "expected_if_false": t.expected_if_false}
            for t in h.contrastive_tests
        ],
        "metric_scores": h.metric_scores,
        "aggregate_rank": h.aggregate_rank,
        "status": h.status,
        "iteration_count": h.iteration_count,
        "causal_chain": h.causal_chain,
    })


def _llm_enhance_hypothesis(feature_id: str, entry: dict, db) -> Hypothesis | None:
    """Generate an LLM-enhanced hypothesis (optional, fallback to None)."""
    try:
        import requests, yaml
        config_path = Path(__file__).resolve().parent / "harness_config.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        refiner_cfg = config.get("refiner", {})

        prompt = (
            f"Given this detected anomaly pattern:\n"
            f"Feature: {entry.get('name_cn', feature_id)}\n"
            f"Definition: {entry.get('definition', '')}\n\n"
            f"Propose ONE causal hypothesis (1 sentence, <50 words) that explains "
            f"this pattern as a structural driver, NOT a surface observation.\n"
            f"Respond JSON: {{\"hypothesis\": \"...\"}}"
        )
        resp = requests.post(
            f"{refiner_cfg.get('base_url')}/v1/messages",
            headers={"Authorization": f"Bearer {refiner_cfg.get('token', '')}",
                     "Content-Type": "application/json",
                     "anthropic-version": "2023-06-01"},
            json={"model": "deepseek-v4-pro", "max_tokens": 200,
                  "temperature": 0.3,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if "content" in data and isinstance(data["content"], list):
            text = "\n".join(
                i.get("text", "") for i in data["content"] if i.get("type") == "text"
            )
            result = json.loads(text.split("```")[0])
            return Hypothesis(
                hypothesis_id=_make_id(feature_id, 99),  # 99 = LLM-generated
                anomaly_feature_id=feature_id,
                statement=result.get("hypothesis", ""),
                source="llm",
                created_at=_now(),
                status="seeded",
            )
    except Exception:
        pass
    return None


# ── Utilities ─────────────────────────────────────────────────────────────

def _make_id(feature_id: str, variant: int) -> str:
    ts = str(int(time.time()))[-6:]
    return f"H-{feature_id}-{variant}-{ts}"


def _now() -> str:
    from datetime import datetime
    return datetime.now().isoformat()


# ── CLI ───────────────────────────────────────────────────────────────────

def main():
    import sys
    from indexer import HarnessDB

    db = HarnessDB()
    date = sys.argv[1] if len(sys.argv) > 1 else "2026-05-21"

    # Get anomalies from feature_activations
    entries = db.get_feature_library_entries()
    activations = db.get_feature_activations(days=1)
    activations = [a for a in activations if a.get("date") == date]

    # Build anomaly features from top activations
    fid_scores: dict[str, float] = {}
    for a in activations:
        fid = a["feature_id"]
        fid_scores[fid] = max(fid_scores.get(fid, 0), a.get("activation_strength", 0))

    from dataclasses import dataclass as dc
    @dc
    class SimpleAnomaly:
        matched_library_feature: str
        detection_confidence: float

    anomalies = [
        SimpleAnomaly(fid, score)
        for fid, score in sorted(fid_scores.items(), key=lambda x: -x[1])[:5]
    ]

    snippets = db.get_news_snippets(date=date)

    hypotheses = run_hypothesis_cycle(anomalies, snippets, db)
    print(f"Hypotheses after full cycle: {len(hypotheses)}")

    for h in hypotheses[:10]:
        print(f"\n  {h.hypothesis_id} [{h.status}] rank={h.aggregate_rank:.3f}")
        print(f"    {h.statement[:80]}...")
        if h.metric_scores:
            print(f"    metrics: {json.dumps({k:round(v,2) for k,v in sorted(h.metric_scores.items())})}")
        print(f"    chain: {' → '.join(h.causal_chain[-3:])}")

    db.close()


if __name__ == "__main__":
    main()
