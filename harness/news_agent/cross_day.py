"""News semantic search v3: entity rarity + FTS5 + cross-section matching.

Simple, fast, no API calls needed:
  1. Extract SPECIFIC entities per snippet (filter out common ones)
  2. For each specific entity, FTS5 search across historical snippets
  3. Rank by: entity rarity * (1 - Jaccard of full entity set)
  4. Output concrete pairs with headlines
"""

import sys
from pathlib import Path
from collections import Counter

from ..indexer import HarnessDB

# Entities too common to be discriminative (appear in >30% of snippets)
_STOP_FREQ_RATIO = 0.25


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
            cross_section = 1.5 if t_section != h_section else 1.0

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
    return discoveries[:top_k]


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
