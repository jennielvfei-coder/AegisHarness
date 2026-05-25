"""Search term adaptive tuning — read daily report → update weights → write next-day config.

Usage:
    python harness/search_feedback.py [--date YYYY-MM-DD]

Runs AFTER the daily report is written. Reads:
  - Today's news file (Obsidian vault)
  - state.db search_topic_weights table
Writes:
  - .constraint_cache.json (next-day search queries)
  - Updated search_topic_weights in state.db
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

HARNESS_DIR = Path(__file__).resolve().parent
CACHE_PATH = HARNESS_DIR / ".constraint_cache.json"
OBSIDIAN_NEWS = Path.home() / "Documents" / "Obsidian Vault" / "claude专属文件夹" / "news"

# ── Topic definitions ────────────────────────────────────────────────────────

ACADEMIC_TOPICS = [
    ("social-ai", "social AI, cooperation, collective behavior, multi-agent social simulation"),
    ("cognitive-science", "cognitive architectures, reasoning, metacognition, theory of mind"),
    ("embodied-ai", "embodied AI, robotics, world models, sensorimotor learning"),
    ("ai-safety", "AI safety, alignment, formal verification, robustness"),
    ("bci", "BCI, neural signal processing, EEG, brain-computer interface"),
    ("knowledge-graphs", "knowledge graphs, neuro-symbolic, knowledge representation"),
    ("causal-inference", "causal inference, mechanistic interpretability, learning theory, generalization"),
    ("multi-agent", "multi-agent coordination, game theory, collective intelligence"),
]

LIVELIHOOD_TOPICS = [
    ("employment", "就业 招聘 灵活用工 基层劳动者 浙江"),
    ("education", "教育 学区 职业教育 双减 高等教育 浙江"),
    ("consumption", "消费 物价 居民收入 零售 消费信心 浙江"),
    ("governance", "基层治理 社区 乡村 县域 社会组织 浙江"),
]

POLICY_QUERY = "AI 政策 科技监管 数据法律 知识产权 数字经济"

# Topic group → query text mappings
TOPIC_QUERIES: dict[str, str] = {}
for name, query in ACADEMIC_TOPICS:
    TOPIC_QUERIES[name] = query
for name, query in LIVELIHOOD_TOPICS:
    TOPIC_QUERIES[name] = query

# ── Scoring ─────────────────────────────────────────────────────────────────

PRODUCED_SCORES = {
    "重点分析": 3,
    "总览": 1,
    "民生观察": 2,
    "filtered": 0,
}

MISS_STREAK_THRESHOLD = 3


def _get_date(date_str: str | None = None) -> str:
    return date_str or date.today().isoformat()


def _read_report(date_str: str) -> str | None:
    """Read today's news report from Obsidian vault."""
    report_path = OBSIDIAN_NEWS / f"{date_str}.md"
    if not report_path.exists():
        print(f"[search_feedback] Report not found: {report_path}")
        return None
    return report_path.read_text(encoding="utf-8")


def _context_around(text: str, keyword: str, window: int = 500) -> str:
    """Get text window around keyword occurrence."""
    idx = text.lower().find(keyword.lower())
    if idx == -1:
        return ""
    start = max(0, idx - window)
    end = min(len(text), idx + window)
    return text[start:end]


def _score_academic_topics(report_text: str) -> Counter:
    """Score each academic topic by presence in today's report."""
    scores = Counter()
    for name, query in ACADEMIC_TOPICS:
        keywords = query.split(", ")[:2]
        for kw in keywords:
            if kw.lower() in report_text.lower():
                context = _context_around(report_text, kw)
                if "重点分析" in context:
                    scores[name] = max(scores[name], PRODUCED_SCORES["重点分析"])
                elif "总览" in context:
                    scores[name] = max(scores[name], PRODUCED_SCORES["总览"])
                else:
                    scores[name] = max(scores[name], PRODUCED_SCORES["filtered"])
    return scores


def _score_livelihood_topics(report_text: str) -> Counter:
    """Score livelihood topics — check 民生观察 section."""
    scores = Counter()
    liv_match = re.search(r'🏘️ 民生观察.*?(?=## |\Z)', report_text, re.DOTALL)
    if not liv_match:
        return scores

    liv_section = liv_match.group(0)
    for name, query in LIVELIHOOD_TOPICS:
        keywords = query.replace(" 浙江", "").split()[:2]
        for kw in keywords:
            if kw in liv_section:
                scores[name] = PRODUCED_SCORES["民生观察"]
                break
        else:
            scores[name] = PRODUCED_SCORES["filtered"]
    return scores


def _update_weights(db, topic_scores: Counter, today: str):
    """Update search_topic_weights based on today's production scores."""
    for name, score in topic_scores.items():
        if score > 0:
            db._conn.execute(
                """INSERT OR REPLACE INTO search_topic_weights
                   (topic_name, weight, hit_count, miss_streak, last_produced_date, updated_at)
                   VALUES (?, MAX(0.1, 1.0 + ?), ?, 0, ?, unixepoch())""",
                (name, score * 0.2, score, today),
            )
        else:
            row = db._conn.execute(
                "SELECT miss_streak, weight FROM search_topic_weights WHERE topic_name = ?",
                (name,),
            ).fetchone()
            streak = (row[0] + 1) if row else 1
            old_weight = row[1] if row else 1.0
            new_weight = old_weight * 0.5 if streak >= MISS_STREAK_THRESHOLD else old_weight
            new_weight = max(0.1, new_weight)  # floor
            db._conn.execute(
                """INSERT OR REPLACE INTO search_topic_weights
                   (topic_name, weight, hit_count, miss_streak, last_produced_date, updated_at)
                   VALUES (?, ?, 0, ?, NULL, unixepoch())""",
                (name, new_weight, streak),
            )
    db._conn.commit()


def _select_next_queries(db) -> dict:
    """Select top-N academic topics and top livelihood topic for next search."""
    # Academic: pick top topics by weight
    acad_names = [name for name, _ in ACADEMIC_TOPICS]
    placeholders = ",".join("?" * len(acad_names))
    rows = db._conn.execute(
        f"SELECT topic_name, weight FROM search_topic_weights "
        f"WHERE topic_name IN ({placeholders}) "
        f"ORDER BY weight DESC LIMIT 3",
        acad_names,
    ).fetchall()

    selected_academic = []
    for row in rows:
        query = TOPIC_QUERIES.get(row[0], "")
        if query:
            selected_academic.append(query)

    # Fill to 3 if needed (no weight data yet)
    for name, query in ACADEMIC_TOPICS:
        if len(selected_academic) >= 3:
            break
        query_text = TOPIC_QUERIES.get(name, "")
        if query_text and query_text not in selected_academic:
            selected_academic.append(query_text)

    # Livelihood: pick top 1
    live_names = [name for name, _ in LIVELIHOOD_TOPICS]
    placeholders = ",".join("?" * len(live_names))
    rows = db._conn.execute(
        f"SELECT topic_name FROM search_topic_weights "
        f"WHERE topic_name IN ({placeholders}) "
        f"ORDER BY weight DESC LIMIT 1",
        live_names,
    ).fetchall()

    liv_name = rows[0][0] if rows else LIVELIHOOD_TOPICS[0][0]
    livelihood_query = TOPIC_QUERIES.get(liv_name, LIVELIHOOD_TOPICS[0][1])

    # Check for entity drift — new entities in last 7 days
    entity_rows = db._conn.execute(
        "SELECT entities FROM news_snippets WHERE date >= date('now', '-7 days')"
    ).fetchall()
    new_entities = _detect_new_entities(entity_rows)

    policy_query = POLICY_QUERY
    if new_entities:
        policy_query += " " + " ".join(new_entities[:2])

    return {
        "academic": " | ".join(selected_academic),
        "policy": policy_query,
        "livelihood": livelihood_query,
    }


def _detect_new_entities(entity_rows) -> list[str]:
    """Detect entities with recent frequency spike."""
    freq = Counter()
    for (ent_json,) in entity_rows:
        if ent_json:
            try:
                for e in json.loads(ent_json):
                    freq[e] += 1
            except (json.JSONDecodeError, TypeError):
                pass
    return [e for e, c in freq.most_common(10) if c >= 3]


def _write_next_day_cache(queries: dict, next_date: str):
    """Write next-day search queries to constraint cache."""
    cache = {
        "date": next_date,
        "queries": queries,
        "generated_at": date.today().isoformat(),
    }
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[search_feedback] Wrote next-day queries for {next_date}")


def main(date_str: str | None = None):
    from indexer import HarnessDB

    today = _get_date(date_str)
    tomorrow = (date.fromisoformat(today) + timedelta(days=1)).isoformat()

    report = _read_report(today)
    if not report:
        print("[search_feedback] No report found, writing default queries for tomorrow")
        queries = {
            "academic": ACADEMIC_TOPICS[0][1],
            "policy": POLICY_QUERY,
            "livelihood": LIVELIHOOD_TOPICS[0][1],
        }
        _write_next_day_cache(queries, tomorrow)
        return

    db = HarnessDB()

    # Ensure table exists
    db._conn.execute("""
        CREATE TABLE IF NOT EXISTS search_topic_weights (
            topic_name TEXT PRIMARY KEY,
            weight REAL NOT NULL DEFAULT 1.0,
            hit_count INTEGER NOT NULL DEFAULT 0,
            miss_streak INTEGER NOT NULL DEFAULT 0,
            last_produced_date TEXT,
            updated_at REAL NOT NULL DEFAULT (unixepoch())
        )
    """)
    db._conn.commit()

    # Score today's topics
    acad_scores = _score_academic_topics(report)
    live_scores = _score_livelihood_topics(report)
    all_scores = Counter()
    all_scores.update(acad_scores)
    all_scores.update(live_scores)

    print(f"[search_feedback] Academic scores: {dict(acad_scores)}")
    print(f"[search_feedback] Livelihood scores: {dict(live_scores)}")

    # Update weights
    _update_weights(db, all_scores, today)

    # Select next-day queries
    next_queries = _select_next_queries(db)
    _write_next_day_cache(next_queries, tomorrow)

    db.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Search term adaptive tuning")
    parser.add_argument("--date", help="Date in YYYY-MM-DD format")
    args = parser.parse_args()
    main(date_str=args.date)
