"""News semantic search v3: entity rarity + FTS5 + cross-section matching.

Simple, fast, no API calls needed:
  1. Canonicalize entities via _ALIAS_TO_CANONICAL (cross-lingual bridge)
  2. Extract SPECIFIC entities per snippet (filter out common ones)
  3. For each specific entity, FTS5 search across historical snippets
  4. Rank by: entity rarity * (1 - Jaccard of full entity set)
  5. Save discoveries to cross_day_discoveries table for report_writer
"""

import sys
from pathlib import Path
from collections import Counter

from harness.indexer import HarnessDB

# Entities too common to be discriminative (appear in >30% of snippets)
_STOP_FREQ_RATIO = 0.25


def _canonicalize_entities(entities: list[str]) -> list[str]:
    """Normalize entity names through alias→canonical mapping.

    Bridges Chinese-English entity gaps: "英伟达" → "NVIDIA", etc.
    """
    try:
        from .vectorize import _ALIAS_TO_CANONICAL
    except ImportError:
        return entities

    canonicalized = []
    for e in entities:
        canonical = _ALIAS_TO_CANONICAL.get(e.lower(), e)
        canonicalized.append(canonical)
    return canonicalized


def _get_discriminative_entities(snippet: dict, freq: Counter, total: int,
                                  max_freq_ratio: float = _STOP_FREQ_RATIO) -> set[str]:
    """Return entities that are specific enough to be useful for matching."""
    ents = set(snippet.get("entities", []))
    return {e for e in ents if freq.get(e, 0) / max(total, 1) < max_freq_ratio}


def search_cross_day(date_str: str, db, top_k: int = 10):
    today = db.get_news_snippets(date=date_str)
    historical = [s for s in db.get_news_snippets(days=365) if s.get("date") != date_str]
    if not today or not historical:
        print("No data")
        return []

    # Canonicalize all entities for cross-lingual matching
    for s in today:
        s["entities"] = _canonicalize_entities(s.get("entities", []))
    for s in historical:
        s["entities"] = _canonicalize_entities(s.get("entities", []))

    all_snips = today + historical
    total = len(all_snips)
    ent_freq = Counter()
    for s in all_snips:
        for e in s.get("entities", []):
            ent_freq[e] += 1

    discoveries = []

    for t in today:
        t_disc = _get_discriminative_entities(t, ent_freq, total)
        if not t_disc:
            continue
        t_ents = set(t.get("entities", []))
        t_headline = t.get("headline", "").replace("**", "").strip()
        t_section = t.get("section", "")

        for h in historical:
            h_disc = _get_discriminative_entities(h, ent_freq, total)
            h_ents = set(h.get("entities", []))
            h_headline = h.get("headline", "").replace("**", "").strip()
            h_section = h.get("section", "")

            shared_disc = t_disc & h_disc
            if not shared_disc:
                continue

            inter = len(t_ents & h_ents)
            union = len(t_ents | h_ents)
            jaccard = inter / union if union > 0 else 0
            if jaccard > 0.4:
                continue

            rarity = sum(1.0 / max(ent_freq[e], 1) for e in shared_disc)

            # Cross-section bonus + cross-language bonus
            cross_section = 1.5 if t_section != h_section else 1.0
            # Detect cross-language: if headlines have different character sets
            has_cjk_t = any('一' <= c <= '鿿' for c in t_headline)
            has_cjk_h = any('一' <= c <= '鿿' for c in h_headline)
            if has_cjk_t != has_cjk_h:
                cross_section *= 1.3  # Cross-language bonus

            discoveries.append({
                "today": t,
                "history": h,
                "shared_entities": list(shared_disc),
                "jaccard": jaccard,
                "rarity": rarity,
                "cross_section": cross_section,
            })

    discoveries.sort(key=lambda d: d["rarity"] * d["cross_section"] / (d["jaccard"] + 0.1),
                     reverse=True)
    top_discoveries = discoveries[:top_k]

    # Save discoveries to cross_day_discoveries table for report_writer
    saved = 0
    for d in top_discoveries:
        try:
            db.save_cross_day_discovery(
                run_date=date_str,
                today_snippet_id=d["today"].get("id", 0),
                history_snippet_id=d["history"].get("id", 0),
                history_date=d["history"].get("date", ""),
                shared_entities=d["shared_entities"],
                jaccard=d["jaccard"],
                rarity=d["rarity"],
                cross_section=d["cross_section"],
            )
            saved += 1
        except Exception as e:
            print(f"[cross_day] Failed to save discovery: {e}", file=sys.stderr)

    if saved:
        print(f"[cross_day] Saved {saved} discoveries to cross_day_discoveries table",
              file=sys.stderr)

    return top_discoveries


def format_cross_day_results(discoveries: list) -> str:
    if not discoveries:
        return "## 语义嗅探\n\n知识库还不够大，未找到跨日语义关联。继续积累每日新闻数据。\n"
    lines = ["## 语义嗅探（稀有实体跨日关联）", ""]
    for i, d in enumerate(discoveries):
        t = d["today"]
        h = d["history"]
        lines.append(f"### #{i+1} 共享实体: {', '.join(d['shared_entities'])}")
        lines.append(f"**{t.get('date')} [{t.get('section','')}]:** {t.get('headline','').replace('**','').strip()[:80]}")
        lines.append(f"**{h.get('date')} [{h.get('section','')}]:** {h.get('headline','').replace('**','').strip()[:80]}")
        lines.append(f"实体重叠率={d['jaccard']:.2f} | 稀有度={d['rarity']:.3f} | {'跨板块' if d['cross_section']>1 else '同板块'}")
        lines.append("")
    return "\n".join(lines)


def main():
    date_str = sys.argv[1] if len(sys.argv) > 1 else "2026-05-21"
    db = HarnessDB()
    discoveries = search_cross_day(date_str, db, top_k=10)
    print(format_cross_day_results(discoveries))
    db.close()


if __name__ == "__main__":
    main()
