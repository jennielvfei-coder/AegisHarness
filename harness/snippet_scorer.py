"""Snippet pre-ranker — score all daily snippets BEFORE report generation.

Four-dimensional scoring:
  1. Domain match — how well does this snippet hit 菲菲's core interests?
  2. Entity freshness — are the entities rarely seen recently? (post-vectorize only)
  3. Cross-source — how many independent sources report the same event?
  4. Feedback learning — did similar topics get positive feedback before?

Usage:
    python harness/snippet_scorer.py --date 2026-05-25 [--top 20]

Output: JSON array of ranked snippets with scores, printed to stdout.
Also writes ranked JSON to .constraint_cache.json under key "ranked_snippets".
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path

HARNESS_DIR = Path(__file__).resolve().parent
CACHE_PATH = HARNESS_DIR / ".constraint_cache.json"

# ── 菲菲's core domains with weighted keywords ────────────────────────────

DOMAIN_WEIGHTS: dict[str, dict] = {
    "AI营销": {
        "weight": 3,
        "keywords": [
            "营销", "广告", "品牌", "内容创作", "文案", "电商", "投放",
            "流量", "增长", "social media", "KOL", "内容生成", "创意",
            "智算", "办公", "协作", "企业服务", "SaaS", "B2B",
        ],
    },
    "Agent/工具": {
        "weight": 3,
        "keywords": [
            "Agent", "智能体", "agentic", "多Agent", "coordination",
            "工具使用", "自动化", "workflow", "orchestrat", "multi-agent",
            "LLM agent", "coding agent",
        ],
    },
    "社会心理": {
        "weight": 2,
        "keywords": [
            "社会", "心理", "认知", "行为", "social", "cognitive",
            "偏见", "信任", "cooperation", "collective", "decision",
            "human decision", "persuasi",
        ],
    },
    "脑机接口": {
        "weight": 2,
        "keywords": [
            "脑机", "BCI", "neural signal", "EEG", "脑电",
            "神经信号", "brain-computer",
        ],
    },
    "具身智能": {
        "weight": 2,
        "keywords": [
            "具身", "机器人", "embodied", "robotics", "world model",
            "世界模型", "sensorimotor",
        ],
    },
    "未来预测": {
        "weight": 1,
        "keywords": [
            "预测", "趋势", "未来", "forecast", "前瞻", "scenario",
            "路径", "2027", "2030", "路线图",
        ],
    },
}


def _get_date(date_str: str | None = None) -> str:
    return date_str or date.today().isoformat()


def _domain_score(headline: str, summary: str) -> tuple[float, list[str]]:
    """Score snippet against 菲菲's core domains. Returns (score, matched_domains)."""
    text = f"{headline} {summary}".lower()
    total = 0.0
    matched = []

    for domain, config in DOMAIN_WEIGHTS.items():
        hits = 0
        for kw in config["keywords"]:
            if kw.lower() in text:
                hits += 1
        if hits > 0:
            capped = min(hits, 4)
            total += config["weight"] * capped * 0.5  # each hit contributes weight*0.5
            matched.append(domain)

    return round(total, 1), matched


def _cross_source_score(snippet: dict, all_snippets: list[dict]) -> float:
    """Bonus for events covered by multiple sources (different URLs, similar titles)."""
    title = snippet.get("headline", "")
    if not title:
        return 0.0

    similar = 0
    for other in all_snippets:
        if other is snippet:
            continue
        other_title = other.get("headline", "")
        if not other_title:
            continue
        # Check URL domain diversity
        urls = []
        for s in [snippet, other]:
            sources = s.get("sources", [])
            if sources and isinstance(sources, list) and len(sources) > 0:
                src = sources[0]
                if isinstance(src, dict):
                    urls.append(src.get("url", ""))
                else:
                    urls.append(str(src))

        if len(urls) < 2:
            continue

        # Quick headline similarity
        ratio = SequenceMatcher(None, title[:80], other_title[:80]).ratio()
        if ratio > 0.45 and urls[0] != urls[1]:
            similar += 1

    return min(similar, 3) * 1.5  # max 4.5 bonus


def _feedback_score(matched_domains: list[str], topic_weights: dict) -> float:
    """Bonus from feedback learning — topics that performed well get boosted."""
    if not matched_domains:
        return 0.0

    bonus = 0.0
    for domain in matched_domains:
        w = topic_weights.get(domain, 1.0)
        if w > 1.0:
            bonus += (w - 1.0) * 1.5  # above-baseline weight → score bonus
    return round(bonus, 1)


def _load_topic_weights(db) -> dict:
    """Load current topic weights from search_topic_weights table."""
    try:
        rows = db._conn.execute(
            "SELECT topic_name, weight FROM search_topic_weights"
        ).fetchall()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {}


def _cross_section_bonus(snippets: list[dict]) -> dict:
    """Ensure coverage across all news sections.

    Returns a small bonus per section to prevent all top-N being from one section.
    """
    section_presence = Counter(s.get("section", "") for s in snippets)
    bonuses = {}
    for s in snippets:
        sec = s.get("section", "")
        if sec and section_presence[sec] <= 3:
            bonuses[s.get("id", "")] = 1.0
    return bonuses


def score_snippets(date_str: str, db, top_n: int = 20) -> list[dict]:
    """Score all snippets for a date, return top-N ranked with scores."""
    snippets = db.get_news_snippets(date=date_str)
    if not snippets:
        print(f"[snippet_scorer] No snippets for {date_str}", file=sys.stderr)
        return []

    topic_weights = _load_topic_weights(db)
    section_bonuses = _cross_section_bonus(snippets)

    scored = []
    for s in snippets:
        headline = s.get("headline", "")
        summary_val = s.get("summary", "")

        d_score, matched = _domain_score(headline, summary_val)
        cs_score = _cross_source_score(s, snippets)
        fb_score = _feedback_score(matched, topic_weights)
        sec_bonus = section_bonuses.get(s.get("id", ""), 0)

        total = d_score + cs_score + fb_score + sec_bonus

        scored.append({
            "id": s.get("id"),
            "date": s.get("date"),
            "section": s.get("section"),
            "headline": headline,
            "summary": summary_val[:120] if summary_val else "",
            "sources": s.get("sources", []),
            "source_rating": s.get("source_rating", ""),
            "total_score": round(total, 1),
            "domain_score": d_score,
            "cross_source_score": round(cs_score, 1),
            "feedback_score": fb_score,
            "section_bonus": sec_bonus,
            "matched_domains": matched,
        })

    scored.sort(key=lambda x: x["total_score"], reverse=True)
    top = scored[:top_n]

    # Persist to cache for next steps
    _update_cache(date_str, top)

    return top


def _update_cache(date_str: str, ranked: list[dict]):
    """Write ranked snippets to constraint cache for downstream use."""
    cache = {}
    if CACHE_PATH.exists():
        try:
            cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cache = {}

    cache["ranked_snippets"] = {
        "date": date_str,
        "count": len(ranked),
        "snippets": [
            {
                "rank": i + 1,
                "headline": s["headline"],
                "section": s["section"],
                "total_score": s["total_score"],
                "matched_domains": s["matched_domains"],
            }
            for i, s in enumerate(ranked)
        ],
    }
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def main(date_str: str | None = None, top_n: int = 20):
    from indexer import HarnessDB

    today = _get_date(date_str)
    db = HarnessDB()
    ranked = score_snippets(today, db, top_n=top_n)
    db.close()

    if not ranked:
        print("[]")
        return

    print(json.dumps(ranked, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Pre-rank daily news snippets")
    parser.add_argument("--date", help="Date in YYYY-MM-DD format")
    parser.add_argument("--top", type=int, default=20, help="Number of top snippets to output")
    args = parser.parse_args()
    main(date_str=args.date, top_n=args.top)
