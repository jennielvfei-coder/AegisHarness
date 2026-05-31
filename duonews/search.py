"""Daily news search orchestrator — runs anysearch batch_search + parses results.

Usage:
    python -m duonews --step search --date 2026-05-31

Reads search queries from .constraint_cache.json (written by search_feedback.py)
or falls back to default rotation. Saves normalized snippets to state.db.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

from . import HARNESS_DIR, CONSTRAINT_CACHE
from .ingest import ingest, AnysearchResult

SKILL_DIR = Path.home() / ".claude" / "skills" / "anysearch" / "scripts"
CLI_PATH = SKILL_DIR / "anysearch_cli.py"

# ── Query defaults (used when no cache file exists) ─────────────────────────

ACADEMIC_TOPICS = [
    "social AI, cooperation, collective behavior, multi-agent social simulation",
    "cognitive architectures, reasoning, metacognition, theory of mind",
    "embodied AI, robotics, world models, sensorimotor learning",
    "AI safety, alignment, formal verification, robustness",
    "BCI, neural signal processing, EEG, brain-computer interface",
    "knowledge graphs, neuro-symbolic, knowledge representation",
    "causal inference, mechanistic interpretability, learning theory, generalization",
    "multi-agent coordination, game theory, collective intelligence",
]

LIVELIHOOD_QUERY = (
    "就业 招聘 灵活用工 浙江 | 教育 学区 职业教育 双减 浙江 | "
    "消费 物价 居民收入 零售 浙江 | 基层治理 社区 乡村 县域 浙江"
)

POLICY_QUERY = "AI 政策 科技监管 数据法律 知识产权 数字经济 智算 安全办公 国产替代"


def _get_date(date_str: str | None = None) -> str:
    return date_str or date.today().isoformat()


def _load_queries(date_str: str) -> dict:
    """Load next-day search queries from cache, or build defaults."""
    if CONSTRAINT_CACHE.exists():
        try:
            cache = json.loads(CONSTRAINT_CACHE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"[news_daily_search] WARNING: cache file corrupt ({e}), falling back to defaults")
            cache = None
        if isinstance(cache, dict) and cache.get("date") == date_str:
            return cache.get("queries", {})

    doy = date.fromisoformat(date_str).timetuple().tm_yday
    acad_idx = (doy // 2) % len(ACADEMIC_TOPICS)

    return {
        "academic": ACADEMIC_TOPICS[acad_idx],
        "policy": POLICY_QUERY,
        "livelihood": LIVELIHOOD_QUERY,
    }


def _build_batch_queries(queries: dict) -> list[dict]:
    """Build the 3-slot batch_search query array."""
    return [
        {
            "query": f"{queries['academic']} high-impact",
            "domain": "academic",
            "sub_domain": "academic.search",
            "max_results": 12,
        },
        {
            "query": queries["policy"],
            "content_types": "news",
            "freshness": "day",
            "max_results": 16,
            "zone": "cn",
        },
        {
            "query": queries["livelihood"],
            "content_types": "news",
            "freshness": "day",
            "max_results": 16,
        },
    ]


def _run_batch_search(queries: list[dict]) -> list[list[dict]]:
    """Run anysearch batch_search CLI, return parsed results per slot."""
    from . import get_proxy_env

    queries_json = json.dumps(queries, ensure_ascii=False)
    env = os.environ.copy()
    env.update(get_proxy_env())

    result = subprocess.run(
        [
            sys.executable, str(CLI_PATH), "batch_search",
            "--queries", queries_json,
        ],
        capture_output=True, encoding="utf-8", timeout=60,
        cwd=str(SKILL_DIR),
        env=env,
    )

    if result.returncode != 0:
        print(f"[news_daily_search] batch_search failed: {result.stderr[:500]}")
        return [[], [], []]

    return _parse_markdown_output(result.stdout)


def _parse_markdown_output(stdout: str) -> list[list[dict]]:
    """Parse anysearch batch_search markdown output into 3 lists of result dicts."""
    slots: list[list[dict]] = [[], [], []]
    slot_idx = -1

    sections = re.split(r'## Search Results.*?\n', stdout)

    for section in sections[1:]:
        slot_idx += 1
        if slot_idx >= 3:
            break

        results = re.split(r'### \d+\.\s+', section)
        for block in results[1:]:
            title = ""
            url = ""
            snippet = ""
            result_date = None

            lines = block.strip().split('\n')
            if lines:
                title = lines[0].strip()

            for line in lines[1:]:
                line = line.strip()
                if line.startswith('- **URL**:'):
                    url = line.replace('- **URL**:', '').strip()
                elif line.startswith('date:'):
                    result_date = line.replace('date:', '').strip()
                elif line and not line.startswith('-') and not line.startswith('date:'):
                    if not snippet:
                        snippet = line

            if title and url:
                slots[slot_idx].append({
                    "title": title,
                    "url": url,
                    "snippet": snippet,
                    "result_date": result_date,
                })

    return slots


def run_daily_search(date_str: str | None = None, queries_file: str | None = None):
    """Run daily anysearch news search and save to state.db.

    Args:
        date_str: YYYY-MM-DD, defaults to today
        queries_file: Optional path to custom queries.json
    """
    from harness.indexer import HarnessDB

    today = _get_date(date_str)
    print(f"[news_daily_search] Running anysearch news search for {today}")

    if queries_file:
        queries = json.loads(Path(queries_file).read_text(encoding="utf-8"))
    else:
        queries = _load_queries(today)

    batch = _build_batch_queries(queries)
    print(f"[news_daily_search] Academic: {batch[0]['query'][:60]}...")
    print(f"[news_daily_search] Policy: {batch[1]['query'][:60]}...")
    print(f"[news_daily_search] Livelihood: {batch[2]['query'][:60]}...")

    slots = _run_batch_search(batch)

    section_labels = ["academic-search", "policy-industry", "livelihood"]
    db = HarnessDB()
    total_saved = 0

    for slot_results, label in zip(slots, section_labels):
        if not slot_results:
            print(f"[news_daily_search] WARNING: {label} returned 0 results")
            continue

        parsed = [
            AnysearchResult(
                title=r["title"], url=r["url"],
                snippet=r["snippet"], result_date=r["result_date"],
            )
            for r in slot_results
        ]

        snippets = ingest(parsed, label, date_str=today)
        for snip in snippets:
            row_id = db.save_news_snippet(**snip)
            if row_id:
                total_saved += 1

    db.close()
    print(f"[news_daily_search] Saved {total_saved} new snippets to state.db")
    return total_saved


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Daily anysearch news search")
    parser.add_argument("--date", help="Date in YYYY-MM-DD format")
    parser.add_argument("--queries-file", help="Path to custom queries JSON file")
    args = parser.parse_args()
    run_daily_search(date_str=args.date, queries_file=args.queries_file)
