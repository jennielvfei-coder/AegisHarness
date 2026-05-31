"""Search term adaptive tuning — read daily report → update weights → write next-day config.

Usage:
    python -m duonews --step feedback --date 2026-05-31

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
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from . import DUONEWS_DIR, CONSTRAINT_CACHE, OBSIDIAN_NEWS

# ── Topic definitions ────────────────────────────────────────────────────────

ACADEMIC_TOPICS = [
    ("social-ai", [
        "social AI, cooperation, collective behavior, multi-agent social simulation",
        "社会AI", "多智能体", "协作", "集体行为", "多Agent", "群体智能",
    ]),
    ("cognitive-science", [
        "cognitive architectures, reasoning, metacognition, theory of mind",
        "认知架构", "推理", "元认知", "心智理论", "认知科学",
    ]),
    ("embodied-ai", [
        "embodied AI, robotics, world models, sensorimotor learning",
        "具身智能", "机器人", "世界模型", "具身AI",
    ]),
    ("ai-safety", [
        "AI safety, alignment, formal verification, robustness",
        "AI安全", "对齐", "形式验证", "鲁棒性",
    ]),
    ("bci", [
        "BCI, neural signal processing, EEG, brain-computer interface",
        "脑机接口", "神经信号", "BCI",
    ]),
    ("knowledge-graphs", [
        "knowledge graphs, neuro-symbolic, knowledge representation",
        "知识图谱", "神经符号", "知识表示",
    ]),
    ("causal-inference", [
        "causal inference, mechanistic interpretability, learning theory, generalization",
        "因果推断", "机制可解释性", "学习理论", "泛化",
    ]),
    ("multi-agent", [
        "multi-agent coordination, game theory, collective intelligence",
        "多Agent协调", "博弈论", "集体智能",
    ]),
]

LIVELIHOOD_TOPICS = [
    ("employment", [
        "就业 招聘 灵活用工 基层劳动者 浙江",
        "就业", "招聘", "灵活用工", "劳动",
    ]),
    ("education", [
        "教育 学区 职业教育 双减 高等教育 浙江",
        "教育", "学区", "职业教育", "双减",
    ]),
    ("consumption", [
        "消费 物价 居民收入 零售 消费信心 浙江",
        "消费", "物价", "居民收入", "零售",
    ]),
    ("governance", [
        "基层治理 社区 乡村 县域 社会组织 浙江",
        "基层治理", "社区", "乡村", "县域",
    ]),
]

POLICY_QUERY = "AI 政策 科技监管 数据法律 知识产权 数字经济 智算 安全办公 国产替代"

TOPIC_QUERIES: dict[str, str] = {}
for name, parts in ACADEMIC_TOPICS:
    TOPIC_QUERIES[name] = parts[0]
for name, parts in LIVELIHOOD_TOPICS:
    TOPIC_QUERIES[name] = parts[0]

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
    report_path = OBSIDIAN_NEWS / f"{date_str}.md"
    if not report_path.exists():
        print(f"[search_feedback] Report not found: {report_path}")
        return None
    return report_path.read_text(encoding="utf-8")


def _context_around(text: str, keyword: str, window: int = 500) -> str:
    idx = text.lower().find(keyword.lower())
    if idx == -1:
        return ""
    start = max(0, idx - window)
    end = min(len(text), idx + window)
    return text[start:end]


def _score_academic_topics(report_text: str) -> Counter:
    scores = Counter()
    for name, parts in ACADEMIC_TOPICS:
        keywords = parts[1:]
        for kw in keywords:
            if kw in report_text:
                context = _context_around(report_text, kw)
                if "重点分析" in context:
                    scores[name] = max(scores[name], PRODUCED_SCORES["重点分析"])
                elif "总览" in context or "速览" in context:
                    scores[name] = max(scores[name], PRODUCED_SCORES["总览"])
                else:
                    scores[name] = max(scores.get(name, 0), PRODUCED_SCORES["filtered"])
    return scores


def _score_livelihood_topics(report_text: str) -> Counter:
    scores = Counter()
    liv_match = re.search(r'🏘️\s*民生观察.*?(?=## [一二三四五六]|\Z)', report_text, re.DOTALL)
    if not liv_match:
        overview_match = re.search(r'## 一、今日速览.*?(?=## 二|\Z)', report_text, re.DOTALL)
        target = overview_match.group(0) if overview_match else report_text
    else:
        target = liv_match.group(0)

    for name, parts in LIVELIHOOD_TOPICS:
        keywords = parts[1:]
        matched = False
        for kw in keywords:
            if kw and kw in target:
                scores[name] = max(scores.get(name, 0), PRODUCED_SCORES["民生观察"])
                matched = True
                break
        if not matched:
            scores[name] = PRODUCED_SCORES["filtered"]
    return scores


def _update_weights(db, topic_scores: Counter, today: str):
    for name, score in topic_scores.items():
        row = db._conn.execute(
            "SELECT weight, hit_count, miss_streak FROM search_topic_weights WHERE topic_name = ?",
            (name,),
        ).fetchone()

        old_weight = row[0] if row else 1.0

        if score > 0:
            alpha = 0.3
            normalized = score / 3.0
            new_weight = (1 - alpha) * old_weight + alpha * (1.0 + normalized)
            db._conn.execute(
                """INSERT OR REPLACE INTO search_topic_weights
                   (topic_name, weight, hit_count, miss_streak, last_produced_date, updated_at)
                   VALUES (?, ?, ?, 0, ?, unixepoch())""",
                (name, new_weight, score, today),
            )
        else:
            streak = (row[2] + 1) if row else 1
            new_weight = old_weight * 0.5 if streak >= MISS_STREAK_THRESHOLD else old_weight
            new_weight = max(0.1, new_weight)
            db._conn.execute(
                """INSERT OR REPLACE INTO search_topic_weights
                   (topic_name, weight, hit_count, miss_streak, last_produced_date, updated_at)
                   VALUES (?, ?, 0, ?, NULL, unixepoch())""",
                (name, new_weight, streak),
            )
    db._conn.commit()


def _select_next_queries(db, today: str) -> dict:
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

    for name, query in ACADEMIC_TOPICS:
        if len(selected_academic) >= 3:
            break
        query_text = TOPIC_QUERIES.get(name, "")
        if query_text and query_text not in selected_academic:
            selected_academic.append(query_text)

    livelihood_query = (
        "就业 招聘 灵活用工 浙江 | 教育 学区 职业教育 双减 浙江 | "
        "消费 物价 居民收入 零售 浙江 | 基层治理 社区 乡村 县域 浙江"
    )

    entity_rows = db._conn.execute(
        "SELECT entities FROM news_snippets WHERE date >= date(?, '-7 days')",
        (today,),
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
    cache = {
        "date": next_date,
        "queries": queries,
        "generated_at": date.today().isoformat(),
    }
    CONSTRAINT_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[search_feedback] Wrote next-day queries for {next_date}")


def run_feedback_loop(date_str: str | None = None):
    from harness.indexer import HarnessDB

    today = _get_date(date_str)
    tomorrow = (date.fromisoformat(today) + timedelta(days=1)).isoformat()

    report = _read_report(today)
    if not report:
        print("[search_feedback] No report found, writing default queries for tomorrow")
        queries = {
            "academic": TOPIC_QUERIES.get(ACADEMIC_TOPICS[0][0], ""),
            "policy": POLICY_QUERY,
            "livelihood": (
                "就业 招聘 灵活用工 浙江 | 教育 学区 职业教育 双减 浙江 | "
                "消费 物价 居民收入 零售 浙江 | 基层治理 社区 乡村 县域 浙江"
            ),
        }
        _write_next_day_cache(queries, tomorrow)
        return

    db = HarnessDB()

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

    acad_scores = _score_academic_topics(report)
    live_scores = _score_livelihood_topics(report)

    all_scores = Counter()
    all_scores.update(acad_scores)
    all_scores.update(live_scores)
    for name, _ in ACADEMIC_TOPICS:
        if name not in all_scores:
            all_scores[name] = 0
    for name, _ in LIVELIHOOD_TOPICS:
        if name not in all_scores:
            all_scores[name] = 0

    print(f"[search_feedback] Academic scores: {dict(acad_scores)}")
    print(f"[search_feedback] Livelihood scores: {dict(live_scores)}")

    _update_weights(db, all_scores, today)
    next_queries = _select_next_queries(db, today)
    _write_next_day_cache(next_queries, tomorrow)

    db.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Search term adaptive tuning")
    parser.add_argument("--date", help="Date in YYYY-MM-DD format")
    args = parser.parse_args()
    run_feedback_loop(date_str=args.date)
