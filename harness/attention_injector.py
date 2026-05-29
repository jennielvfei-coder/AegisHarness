"""Attention Injector — 3-layer attention pooling for news workflow.

Phase 2 of the InterpAgent-inspired news optimization pipeline.

Architecture:
  1. Read feature activations from Phase 1 (feature_activations table)
  2. Group by FEATURE LIBRARY V2.0 layer: Surface / Structural / Latent
  3. Within-layer softmax pooling
  4. Latent → Structural boost rule (cosine > 0.6 → weight 0.3 → 0.5)
  5. Attention budget enforcement (default cap: 5 highlights/day)
  6. Generate injection text for daily newspaper template

Reuses:
  - _utils.softmax_list() for within-layer attention pooling
  - _utils.cosine_sim() for Latent → Structural boost detection
  - feature_library.match_entity_combos() for entity-based scoring
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from harness._utils import cosine_sim, softmax_list


# ── Data model ────────────────────────────────────────────────────────────

@dataclass
class FeatureActivation:
    """A single feature library entry activated by today's news."""
    feature_id: str          # "C4", "A5", etc.
    name_cn: str             # "象征性让步", "同步异源", etc.
    layer: str               # "surface" | "structural" | "latent"
    activation_strength: float  # 0.0–1.0, pooled from snippet-level activations
    snippet_count: int       # how many snippets activated this feature
    snippet_ids: list[int] = field(default_factory=list)
    latent_boosted: bool = False   # boosted by latent feature association
    boost_source: str = ""         # which latent feature caused the boost
    attention_weight: float = 0.0  # after pooling + boost


@dataclass
class AttentionBudget:
    """Configurable attention budget for daily news injection."""
    max_highlights_per_day: int = 5
    max_structural: int = 3
    max_latent_boosted: int = 2
    total_cap: int = 5
    overflow_behavior: str = "archive"  # "archive" | "log_only" | "warn"

    @classmethod
    def from_config(cls, config: dict | None = None) -> "AttentionBudget":
        if config is None:
            return cls()
        return cls(
            max_highlights_per_day=config.get("max_highlights_per_day", 5),
            max_structural=config.get("max_structural_inject", 3),
            max_latent_boosted=config.get("max_latent_boosted", 2),
            total_cap=config.get("total_cap", 5),
            overflow_behavior=config.get("overflow_behavior", "archive"),
        )


@dataclass
class InjectionReport:
    """Complete injection output for a single day."""
    date: str
    surface_count: int          # Surface features detected (not injected, stats only)
    structural_count: int       # Structural features pooled
    latent_count: int           # Latent features acting as bias
    boosted_count: int          # Structural features boosted by Latent
    injected: list[FeatureActivation]   # Features actually injected (within budget)
    overflow: list[FeatureActivation]   # High-quality but over budget (archived)
    injection_text: str         # Markdown injection for the news template
    budget_used: int
    budget_total: int


# ── Core pooling logic ────────────────────────────────────────────────────

def pool_activations(
    activations: list[dict],
    feature_entries: list[dict],
    budget: AttentionBudget | None = None,
) -> InjectionReport:
    """Run the full 3-layer attention pooling pipeline.

    Args:
        activations: Feature activation records from Phase 1 (feature_activations table).
                     Each dict has: feature_id, activation_strength, source_snippet_ids.
        feature_entries: Feature library entries from DB (must include layer, embedding, definition).
        budget: Attention budget config. Uses defaults if None.

    Returns:
        InjectionReport with pooled features, boost results, and injection text.
    """
    if budget is None:
        budget = AttentionBudget()

    # Build lookup: feature_id → entry
    entry_map: dict[str, dict] = {}
    for e in feature_entries:
        entry_map[e["feature_id"]] = e

    # Step 1: Aggregate snippet-level activations → feature-level activation strengths
    feature_acts: dict[str, FeatureActivation] = {}

    for act in activations:
        fid = act["feature_id"]
        entry = entry_map.get(fid, {})
        strength = act.get("activation_strength", 0.0)
        snippet_ids = act.get("source_snippet_ids", [])

        if fid not in feature_acts:
            feature_acts[fid] = FeatureActivation(
                feature_id=fid,
                name_cn=entry.get("name_cn", fid),
                layer=entry.get("layer", "unknown"),
                activation_strength=0.0,
                snippet_count=0,
                snippet_ids=[],
            )

        fa = feature_acts[fid]
        # Max-pool: take the strongest activation across snippets
        fa.activation_strength = max(fa.activation_strength, strength)
        fa.snippet_count += 1
        for sid in snippet_ids:
            if sid not in fa.snippet_ids:
                fa.snippet_ids.append(sid)

    if not feature_acts:
        return _empty_report("")

    # Step 2: Group by layer
    surface_features = [f for f in feature_acts.values() if f.layer == "surface"]
    structural_features = [f for f in feature_acts.values() if f.layer == "structural"]
    latent_features = [f for f in feature_acts.values() if f.layer == "latent"]

    # Step 3: Within-layer softmax pooling
    _within_layer_pool(surface_features)
    _within_layer_pool(structural_features)
    _within_layer_pool(latent_features)

    # Step 4: Latent → Structural boost
    _apply_latent_boost(structural_features, latent_features, entry_map)

    # Step 5: Attention budget enforcement
    date = activations[0]["date"] if activations else ""
    return _enforce_budget(
        surface_features, structural_features, latent_features,
        budget, date, entry_map,
    )


def _within_layer_pool(features: list[FeatureActivation]) -> None:
    """Compute attention weights using z-score normalization + softmax.

    Raw activation strengths are often tightly clustered (0.5-0.7 range).
    Z-score normalization spreads them out before softmax, creating meaningful
    differentiation between strongly and weakly activated features.
    """
    if not features:
        return

    strengths = np.array([f.activation_strength for f in features], dtype=np.float32)

    # Z-score normalization for differentiation
    mean_s = np.mean(strengths)
    std_s = np.std(strengths)
    if std_s < 1e-6:
        # All equal — uniform weights
        z_scores = np.ones_like(strengths)
    else:
        z_scores = (strengths - mean_s) / std_s

    # Shift to positive range for softmax
    z_scores = z_scores - np.min(z_scores) + 0.5
    weights = softmax_list(z_scores.tolist())

    base_layer_weight = {
        "surface": 0.1,
        "structural": 0.3,
        "latent": 0.7,
    }

    for f, w in zip(features, weights):
        layer_w = base_layer_weight.get(f.layer, 0.3)
        f.attention_weight = w * layer_w


def _apply_latent_boost(
    structural: list[FeatureActivation],
    latent: list[FeatureActivation],
    entry_map: dict[str, dict],
) -> None:
    """Boost structural features that are semantically linked to latent features.

    For each structural feature, compute cosine similarity of its "通常指向" field
    against all latent feature definitions. If similarity > 0.6 → boost weight by 0.3→0.5.

    This implements the core insight: structural anomalies that trace to a known
    deep driver (latent feature) are more important than isolated structural signals.
    """
    if not structural or not latent:
        return

    # Build latent definition embeddings (from feature library entries)
    latent_embs = {}
    for lf in latent:
        entry = entry_map.get(lf.feature_id, {})
        emb = entry.get("embedding")
        if emb:
            latent_embs[lf.feature_id] = emb

    if not latent_embs:
        return

    for sf in structural:
        entry = entry_map.get(sf.feature_id, {})
        # Use the "通常指向" (typical_implication) field for semantic matching
        # This field describes what deeper pattern this feature usually signals
        implication = entry.get("typical_implication", "")
        if not implication:
            # Fallback: use the structural feature's own definition embedding
            sf_emb = entry.get("embedding")
        else:
            # Compute embedding of the implication text
            from encoder import text_hash, encode_cached
            sf_emb = None
            for db_ref in [None]:  # placeholder — we need a DB ref for caching
                pass
            # For now, use the structural feature's own embedding
            sf_emb = entry.get("embedding")

        if sf_emb is None:
            continue

        best_sim = 0.0
        best_latent = ""

        for lf_id, l_emb in latent_embs.items():
            sim = cosine_sim(sf_emb, l_emb)
            if sim > best_sim:
                best_sim = sim
                best_latent = lf_id

        if best_sim > 0.6:
            # Boost: structural weight from 0.3 → 0.5
            sf.attention_weight = sf.attention_weight * (0.5 / 0.3)
            sf.latent_boosted = True
            sf.boost_source = best_latent


def _enforce_budget(
    surface: list[FeatureActivation],
    structural: list[FeatureActivation],
    latent: list[FeatureActivation],
    budget: AttentionBudget,
    date: str,
    entry_map: dict[str, dict],
) -> InjectionReport:
    """Apply attention budget: rank and truncate features.

    Priority ordering:
      1. Latent-boosted structural features (highest priority)
      2. Non-boosted structural features
      3. Surface features (lowest — stats only, not injected)
    """
    # Sort structural: boosted first, then by attention weight
    structural_sorted = sorted(
        structural,
        key=lambda f: (not f.latent_boosted, -f.attention_weight),
    )

    # Inject at most budget.max_structural structural features
    injected = structural_sorted[:budget.max_structural]
    overflow = structural_sorted[budget.max_structural:]

    # Also inject any surface features that have unusually high activation
    # (but only if there's budget left)
    surface_sorted = sorted(surface, key=lambda f: -f.activation_strength)
    remaining_budget = budget.total_cap - len(injected)
    if remaining_budget > 0:
        high_surface = [f for f in surface_sorted
                       if f.activation_strength > 0.7][:remaining_budget]
        injected.extend(high_surface)

    # Latent features never get injected directly (they are bias, not content)
    # But their count is tracked for reporting

    # Generate injection text
    injection_text = _format_injection(injected, overflow, surface, latent,
                                       budget, entry_map)

    return InjectionReport(
        date=date,
        surface_count=len(surface),
        structural_count=len(structural),
        latent_count=len(latent),
        boosted_count=sum(1 for f in structural if f.latent_boosted),
        injected=injected,
        overflow=overflow,
        injection_text=injection_text,
        budget_used=len(injected),
        budget_total=budget.total_cap,
    )


# ── Injection text generation ─────────────────────────────────────────────

def _format_injection(
    injected: list[FeatureActivation],
    overflow: list[FeatureActivation],
    surface: list[FeatureActivation],
    latent: list[FeatureActivation],
    budget: AttentionBudget,
    entry_map: dict[str, dict],
) -> str:
    """Generate the Markdown injection block for the news template.

    Output is ≤8 lines, designed to be inserted into the "重点分析" section
    of the daily newspaper.
    """
    lines = ["## 🧠 特征池化洞察"]

    # Budget summary line
    n_surface = len(surface)
    n_boosted = sum(1 for f in injected if f.latent_boosted)
    lines.append(
        f"**今日注意力预算：{len(injected)}/{budget.total_cap} 已用 | "
        f"表层信号 {n_surface} 条（聚合统计）| "
        f"机制层注入 {len(injected)} 条 | "
        f"深层偏置 {len(latent)} 条**"
    )

    if not injected:
        lines.append("\n_今日无显著机制层信号触发。_")
        return "\n".join(lines)

    # Feature table
    lines.append("")
    lines.append("| 层 | 特征 | 证据强度 | 关联深层 |")
    lines.append("|----|------|---------|---------|")

    for f in injected:
        layer_name = {"surface": "表层", "structural": "机制层", "latent": "深层"}.get(f.layer, f.layer)
        boost_info = f"↗ {f.boost_source}" if f.latent_boosted else "—"
        entry = entry_map.get(f.feature_id, {})
        definition_snippet = entry.get("definition", f.feature_id)[:40]
        lines.append(
            f"| {layer_name} | **{f.feature_id} {f.name_cn}** — {definition_snippet} | "
            f"{f.attention_weight:.2f} | {boost_info} |"
        )

    # Overflow note
    if overflow:
        overflow_names = ", ".join(
            f"{f.feature_id} {f.name_cn} ({f.attention_weight:.2f})"
            for f in overflow[:3]
        )
        lines.append(f"\n**超出预算的高质量信号（已存档）：** {overflow_names}")

    # Budget usage footer
    lines.append(
        f"\n_注意力预算：{len(injected)}/{budget.total_cap} 已用 | "
        f"可通过 `harness_daemon.py diagnose` 查看完整报告_"
    )

    return "\n".join(lines)


# ── Format overflow archive ───────────────────────────────────────────────

def format_overflow_archive(report: InjectionReport) -> dict:
    """Generate the signal-archive JSON for overflow and surplus features.

    This implements the CC6 archive schema from the plan.
    """
    return {
        "date": report.date,
        "budget": {
            "total_cap": report.budget_total,
            "used": report.budget_used,
            "overflow_count": len(report.overflow),
        },
        "overflow": [
            {
                "feature_id": f.feature_id,
                "name_cn": f.name_cn,
                "layer": f.layer,
                "activation_strength": f.activation_strength,
                "attention_weight": f.attention_weight,
                "latent_boosted": f.latent_boosted,
                "boost_source": f.boost_source,
                "snippet_ids": f.snippet_ids,
            }
            for f in report.overflow
        ],
    }


# ── Convenience: run from Phase 1 activations ─────────────────────────────

def run_phase2(
    date: str,
    db,
    budget_config: dict | None = None,
) -> InjectionReport:
    """Run Phase 2 on stored Phase 1 feature activations.

    Args:
        date: Date string (YYYY-MM-DD).
        db: HarnessDB instance.
        budget_config: Optional attention budget overrides.

    Returns:
        InjectionReport ready for template injection.
    """
    budget = AttentionBudget.from_config(budget_config or {})

    # Load feature activations from Phase 1
    activations = db.get_feature_activations(days=1)
    # Filter to just this date
    activations = [a for a in activations if a.get("date") == date]

    if not activations:
        # Try loading directly from today's snippets
        snippets = db.get_news_snippets(date=date)
        if not snippets:
            return _empty_report(date)

        from feature_library import compute_activation_matrix
        feature_entries = db.get_feature_library_entries()
        feature_embeddings = [e["embedding"] for e in feature_entries if e.get("embedding")]
        feature_ids = [e["feature_id"] for e in feature_entries if e.get("embedding")]

        activation_matrix = compute_activation_matrix(snippets, db)
        if not activation_matrix:
            return _empty_report(date)

        # Build activation records
        activations = []
        for i, row in enumerate(activation_matrix):
            for f_idx, val in enumerate(row):
                if val >= 0.3:
                    activations.append({
                        "date": date,
                        "feature_id": feature_ids[f_idx],
                        "activation_strength": val,
                        "source_snippet_ids": [snippets[i].get("id", i)],
                    })

    # Load feature library entries for layer info
    feature_entries = db.get_feature_library_entries()

    return pool_activations(activations, feature_entries, budget)


def _empty_report(date: str) -> InjectionReport:
    return InjectionReport(
        date=date,
        surface_count=0, structural_count=0, latent_count=0,
        boosted_count=0,
        injected=[], overflow=[],
        injection_text="## 🧠 特征池化洞察\n\n_今日无足够数据生成特征注入。_",
        budget_used=0, budget_total=5,
    )


# ── CLI ───────────────────────────────────────────────────────────────────

def main():
    import sys
    from indexer import HarnessDB

    db = HarnessDB()

    if len(sys.argv) < 2:
        print("Usage: python attention_injector.py <YYYY-MM-DD>")
        db.close()
        return

    date = sys.argv[1]
    report = run_phase2(date, db)

    print(f"Injection Report for {date}")
    print(f"  Surface: {report.surface_count}")
    print(f"  Structural: {report.structural_count} ({report.boosted_count} boosted)")
    print(f"  Latent (bias): {report.latent_count}")
    print(f"  Injected: {report.budget_used}/{report.budget_total}")
    print(f"  Overflow: {len(report.overflow)}")
    print()
    print(report.injection_text)

    # Store the injection in DB
    for f in report.injected:
        db._conn.execute(
            """INSERT INTO news_attention_injections
               (date, layer, injection_text, feature_ids, weight_boost_applied,
                budget_used, budget_total)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (date, f.layer, report.injection_text,
             json.dumps([fe.feature_id for fe in report.injected]),
             1 if f.latent_boosted else 0,
             report.budget_used, report.budget_total),
        )
    db._conn.commit()
    print("\nStored injection in DB.")

    # Archive overflow
    if report.overflow:
        archive = format_overflow_archive(report)
        print(f"\nOverflow archive ({len(report.overflow)} items):")
        for item in archive["overflow"][:3]:
            print(f"  {item['feature_id']} {item['name_cn']}: w={item['attention_weight']:.3f}")

    db.close()


if __name__ == "__main__":
    main()
