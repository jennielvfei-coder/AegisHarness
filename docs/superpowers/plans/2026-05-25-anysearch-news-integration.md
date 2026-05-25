# anysearch 新闻管线集成 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 anysearch 搜索集成到日报工作流：替代 5 个 WebFetch 源，新增高因子论文学术搜索和民生观察板块，加入搜索词自适应调优。

**Architecture:** 三槽 batch_search 并行采集 → anysearch_ingest 归一化进 news_snippets 表 → 走现有去重/嵌入/特征提取管线 → 日报模板新增民生段 → search_feedback 闭环调优次日搜索词。

**Tech Stack:** Python 3.12, SQLite (state.db), anysearch CLI + API, 现有 harness 管线

---

## File Map

| 文件 | 操作 | 职责 |
|------|------|------|
| `harness/anysearch_ingest.py` | 新建 | 数据归一化：anysearch 原始结果 → news_snippets 格式 |
| `harness/news_daily_search.py` | 新建 | 日报采集调度：batch_search + 解析 + 保存 |
| `harness/search_feedback.py` | 新建 | 搜索词自适应：读日报→算权重→写次日配置 |
| `harness/generate_report.py` | 修改 | 支持 livelihood section 聚类输出 |
| `harness/indexer.py` | 修改 | 新建 search_topic_weights 表 |
| `C:\Users\Chucky\Documents\Obsidian Vault\claude专属文件夹\news\news-template.md` | 修改 | 模板 v3.1 新增民生观察段 |
| `C:\Users\Chucky\.claude\projects\D--Claude\memory\daily-news-trigger.md` | 修改 | 工作流集成 anysearch 步骤 |

---

### Task 1: 创建 `anysearch_ingest.py` — 数据归一化模块

**Files:**
- Create: `D:\Claude\harness\anysearch_ingest.py`

- [ ] **Step 1: Write the module**

```python
"""Normalize anysearch search results into news_snippets-compatible dicts."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date


@dataclass
class AnysearchResult:
    """Parsed from anysearch CLI markdown output."""
    title: str
    url: str
    snippet: str
    result_date: str | None = None  # extracted from markdown "date: ..."


def ingest(results: list[AnysearchResult], section: str,
           date_str: str | None = None) -> list[dict]:
    """Convert anysearch results to dicts ready for HarnessDB.save_news_snippet().

    Args:
        results: Parsed anysearch results
        section: One of "academic-high-impact" | "policy-industry" | "livelihood"
        date_str: YYYY-MM-DD, defaults to today

    Returns:
        List of dicts with keys matching news_snippets column names
    """
    today = date_str or date.today().isoformat()

    snippets = []
    for r in results:
        headline = r.title.strip()[:200]
        summary = r.snippet.strip()[:500]
        content_hash = hashlib.sha256(
            f"{headline}{r.url}".encode()
        ).hexdigest()

        snippets.append({
            "date": today,
            "section": section,
            "headline": headline,
            "summary": summary,
            "entities": [],          # filled by news_vectorizer later
            "sources": [{"url": r.url, "title": headline}],
            "source_rating": "⭐⭐",  # anysearch single-source default
            "content_hash": content_hash,
            "embedding": None,       # filled by encoder later
        })

    return snippets
```

- [ ] **Step 2: Verify import works**

```powershell
python -c "from harness.anysearch_ingest import ingest, AnysearchResult; r = AnysearchResult('T', 'http://x', 's'); print(len(ingest([r], 'livelihood')))"
```

Expected: `1`

- [ ] **Step 3: Commit**

```bash
git add harness/anysearch_ingest.py
git commit -m "feat(harness): add anysearch_ingest — normalize anysearch results to news_snippets format"
```

---

### Task 2: 创建 `news_daily_search.py` — 日报采集调度

**Files:**
- Create: `D:\Claude\harness\news_daily_search.py`

- [ ] **Step 1: Write the module**

```python
"""Daily news search orchestrator — runs anysearch batch_search + parses results.

Usage:
    python harness/news_daily_search.py [--date YYYY-MM-DD] [--queries-file path.json]

Reads search queries from .constraint_cache.json (written by search_feedback.py)
or falls back to default rotation. Saves normalized snippets to state.db.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

HARNESS_DIR = Path(__file__).resolve().parent
SKILL_DIR = Path.home() / ".claude" / "skills" / "anysearch" / "scripts"
CLI_PATH = SKILL_DIR / "anysearch_cli.py"
CACHE_PATH = HARNESS_DIR / ".constraint_cache.json"

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

LIVELIHOOD_TOPICS = {
    0: "就业 招聘 灵活用工 基层劳动者 浙江",
    1: "教育 学区 职业教育 双减 高等教育 浙江",
    2: "消费 物价 居民收入 零售 消费信心 浙江",
    3: "基层治理 社区 乡村 县域 社会组织 浙江",
}

POLICY_QUERY = "AI 政策 科技监管 数据法律 知识产权 数字经济"


def _get_date(date_str: str | None = None) -> str:
    return date_str or date.today().isoformat()


def _load_queries(date_str: str) -> dict:
    """Load next-day search queries from cache, or build defaults."""
    if CACHE_PATH.exists():
        cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if cache.get("date") == date_str:
            return cache.get("queries", {})

    # Fallback: default rotation based on day-of-year
    doy = date.fromisoformat(date_str).timetuple().tm_yday
    acad_idx = (doy // 2) % len(ACADEMIC_TOPICS)  # rotate every 2 days
    livelihood_idx = doy % 4

    return {
        "academic": ACADEMIC_TOPICS[acad_idx],
        "policy": POLICY_QUERY,
        "livelihood": LIVELIHOOD_TOPICS[livelihood_idx],
    }


def _build_batch_queries(queries: dict, date_str: str) -> list[dict]:
    """Build the 3-slot batch_search query array."""
    return [
        {
            "query": f"{queries['academic']} high-impact",
            "domain": "academic",
            "sub_domain": "academic.search",
            "max_results": 5,
        },
        {
            "query": queries["policy"],
            "content_types": "news",
            "freshness": "day",
            "max_results": 8,
            "zone": "cn",
        },
        {
            "query": queries["livelihood"],
            "content_types": "news",
            "freshness": "week",
            "max_results": 8,
        },
    ]


def _run_batch_search(queries: list[dict]) -> list[dict]:
    """Run anysearch batch_search CLI, return parsed results per slot.

    Returns list of 3 lists: [academic_results, policy_results, livelihood_results]
    """
    queries_json = json.dumps(queries, ensure_ascii=False)

    result = subprocess.run(
        [
            sys.executable, str(CLI_PATH), "batch_search",
            "--queries", queries_json,
        ],
        capture_output=True, text=True, timeout=60,
        cwd=str(SKILL_DIR),
    )

    if result.returncode != 0:
        print(f"[news_daily_search] batch_search failed: {result.stderr[:500]}")
        return [[], [], []]

    return _parse_markdown_output(result.stdout)


def _parse_markdown_output(stdout: str) -> list[list[dict]]:
    """Parse anysearch batch_search markdown output into 3 lists of result dicts.

    The markdown format per slot is:

        ## Search Results (N results, Xms)

        ### 1. Title
        - **URL**: https://...
        - snippet text
        date: YYYY-MM-DD

    Returns list[list[dict]] with 3 sub-lists (one per batch slot).
    Each result dict: {"title": str, "url": str, "snippet": str, "result_date": str|None}
    """
    slots: list[list[dict]] = [[], [], []]
    slot_idx = -1

    # Split by "## Search Results" header — each marks one slot
    sections = re.split(r'## Search Results.*?\n', stdout)

    for section in sections[1:]:  # skip preamble before first result block
        slot_idx += 1
        if slot_idx >= 3:
            break

        # Parse individual results within this section
        results = re.split(r'### \d+\.\s+', section)
        for block in results[1:]:  # skip empty pre-first-header
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


def main(date_str: str | None = None, queries_file: str | None = None):
    """Run daily anysearch news search and save to state.db.

    Args:
        date_str: YYYY-MM-DD, defaults to today
        queries_file: Optional path to custom queries.json
    """
    from indexer import HarnessDB
    from anysearch_ingest import ingest, AnysearchResult

    today = _get_date(date_str)
    print(f"[news_daily_search] Running anysearch news search for {today}")

    # Load queries
    if queries_file:
        queries = json.loads(Path(queries_file).read_text(encoding="utf-8"))
    else:
        queries = _load_queries(today)

    batch = _build_batch_queries(queries, today)
    print(f"[news_daily_search] Academic: {batch[0]['query'][:60]}...")
    print(f"[news_daily_search] Policy: {batch[1]['query'][:60]}...")
    print(f"[news_daily_search] Livelihood: {batch[2]['query'][:60]}...")

    # Run batch search
    slots = _run_batch_search(batch)

    section_labels = ["academic-high-impact", "policy-industry", "livelihood"]
    db = HarnessDB()
    total_saved = 0

    for slot_results, label in zip(slots, section_labels):
        if not slot_results:
            print(f"[news_daily_search] WARNING: {label} returned 0 results")
            continue

        # Convert to AnysearchResult objects
        parsed = [
            AnysearchResult(
                title=r["title"], url=r["url"],
                snippet=r["snippet"], result_date=r["result_date"],
            )
            for r in slot_results
        ]

        # Normalize and save
        snippets = ingest(parsed, label, date_str=today)
        for snip in snippets:
            row_id = db.save_news_snippet(**snip)
            if row_id:
                total_saved += 1

    db.close()
    print(f"[news_daily_search] Saved {total_saved} new snippets to state.db")
    return total_saved


if __name__ == "__main__":
    date_arg = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--date" else None
    queries_arg = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--queries-file" else (
        sys.argv[4] if len(sys.argv) > 4 and sys.argv[3] == "--queries-file" else None
    )
    main(date_str=date_arg, queries_file=queries_arg)
```

- [ ] **Step 2: Test with a dry run**

```powershell
python harness/news_daily_search.py
```

Expected: Prints query info, runs batch_search, saves snippets to state.db. Check for any stderr warnings.

- [ ] **Step 3: Commit**

```bash
git add harness/news_daily_search.py
git commit -m "feat(harness): add news_daily_search — anysearch batch_search orchestrator"
```

---

### Task 3: 创建 `search_feedback.py` — 搜索词自适应调优

**Files:**
- Create: `D:\Claude\harness\search_feedback.py`
- Modify: `D:\Claude\harness\indexer.py` (add search_topic_weights table)

- [ ] **Step 1: Add search_topic_weights table to indexer.py**

In `indexer.py`, after the `news_attention_injections` table definition (~line 298), add:

```sql
-- v8: Search feedback tables
CREATE TABLE IF NOT EXISTS search_topic_weights (
    topic_name TEXT PRIMARY KEY,
    weight REAL NOT NULL DEFAULT 1.0,
    hit_count INTEGER NOT NULL DEFAULT 0,
    miss_streak INTEGER NOT NULL DEFAULT 0,
    last_produced_date TEXT,
    updated_at REAL NOT NULL DEFAULT (unixepoch())
);
```

In the second occurrence of schema definitions (~line 520 area), add the same block.

- [ ] **Step 2: Write the feedback module**

```python
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
from typing import Optional

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
TOPIC_QUERIES: dict[str, str] = {
    name: query for name, query in ACADEMIC_TOPICS
}
TOPIC_QUERIES.update({name: query for name, query in LIVELIHOOD_TOPICS})

# ── Scoring ─────────────────────────────────────────────────────────────────

PRODUCED_SCORES = {
    "重点分析": 3,   # appeared in deep analysis section
    "总览": 1,       # appeared in overview table only
    "民生观察": 2,   # appeared in livelihood section
    "filtered": 0,   # was in search results but filtered out
}

MISS_STREAK_THRESHOLD = 3  # consecutive days with no production → downgrade


def _get_date(date_str: str | None = None) -> str:
    return date_str or date.today().isoformat()


def _read_report(date_str: str) -> str | None:
    """Read today's news report from Obsidian vault."""
    report_path = OBSIDIAN_NEWS / f"{date_str}.md"
    if not report_path.exists():
        print(f"[search_feedback] Report not found: {report_path}")
        return None
    return report_path.read_text(encoding="utf-8")


def _score_academic_topics(report_text: str) -> Counter:
    """Score each academic topic by presence in today's report."""
    scores = Counter()
    for name, query in ACADEMIC_TOPICS:
        # Check if topic keywords appear in report sections
        keywords = query.split(", ")[:2]  # first 2 keywords as signal
        for kw in keywords:
            if kw.lower() in report_text.lower():
                # Determine which section it appeared in
                if "重点分析" in context_around(report_text, kw):
                    scores[name] = max(scores[name], PRODUCED_SCORES["重点分析"])
                elif "总览" in context_around(report_text, kw):
                    scores[name] = max(scores[name], PRODUCED_SCORES["总览"])
                else:
                    scores[name] = max(scores[name], PRODUCED_SCORES["filtered"])
    return scores


def _score_livelihood_topics(report_text: str) -> Counter:
    """Score livelihood topics — check 民生观察 section."""
    scores = Counter()
    # Find the livelihood section
    liv_match = re.search(r'🏘️ 民生观察.*?(?=##|\Z)', report_text, re.DOTALL)
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


def context_around(text: str, keyword: str, window: int = 500) -> str:
    """Get text window around keyword occurrence."""
    idx = text.lower().find(keyword.lower())
    if idx == -1:
        return ""
    start = max(0, idx - window)
    end = min(len(text), idx + window)
    return text[start:end]


def _update_weights(db, topic_scores: Counter, today: str):
    """Update search_topic_weights based on today's production scores."""
    for name, score in topic_scores.items():
        if score > 0:
            # Topic produced — boost weight, reset miss streak
            db._conn.execute(
                """INSERT OR REPLACE INTO search_topic_weights
                   (topic_name, weight, hit_count, miss_streak, last_produced_date, updated_at)
                   VALUES (?, 1.0 + ?, ?, 0, ?, unixepoch())""",
                (name, score * 0.2, score, today),
            )
        else:
            # Topic missed — increment streak
            row = db._conn.execute(
                "SELECT miss_streak, weight FROM search_topic_weights WHERE topic_name = ?",
                (name,),
            ).fetchone()
            streak = (row[0] + 1) if row else 1
            old_weight = row[1] if row else 1.0
            new_weight = old_weight * 0.5 if streak >= MISS_STREAK_THRESHOLD else old_weight
            db._conn.execute(
                """INSERT OR REPLACE INTO search_topic_weights
                   (topic_name, weight, hit_count, miss_streak, last_produced_date, updated_at)
                   VALUES (?, ?, 0, ?, NULL, unixepoch())""",
                (name, new_weight, streak),
            )
    db._conn.commit()


def _select_next_queries(db) -> dict:
    """Select top-N academic topics and top-1 livelihood topic for next search."""
    # Academic: pick top 3 topics by weight
    rows = db._conn.execute(
        "SELECT topic_name, weight FROM search_topic_weights "
        "WHERE topic_name IN ({}) "
        "ORDER BY weight DESC LIMIT 3".format(
            ",".join("?" * len(ACADEMIC_TOPICS))
        ),
        [name for name, _ in ACADEMIC_TOPICS],
    ).fetchall()

    selected_academic = []
    for name, weight in rows[:3]:
        query = TOPIC_QUERIES.get(name, "")
        if query:
            selected_academic.append(query)

    # Fill to 3 if needed
    for name, query in ACADEMIC_TOPICS:
        if len(selected_academic) >= 3:
            break
        if not any(name in sa for sa in selected_academic):
            selected_academic.append(query)

    # Livelihood: pick top 1
    rows = db._conn.execute(
        "SELECT topic_name FROM search_topic_weights "
        "WHERE topic_name IN ({}) "
        "ORDER BY weight DESC LIMIT 1".format(
            ",".join("?" * len(LIVELIHOOD_TOPICS))
        ),
        [name for name, _ in LIVELIHOOD_TOPICS],
    ).fetchall()

    liv_name = rows[0][0] if rows else LIVELIHOOD_TOPICS[0][0]
    livelihood_query = TOPIC_QUERIES.get(liv_name, LIVELIHOOD_TOPICS[0][1])

    # Check for entity drift — new entities in last 7 days
    entity_rows = db._conn.execute(
        "SELECT entities FROM news_snippets WHERE date >= date('now', '-7 days')"
    ).fetchall()
    new_entities = _detect_new_entities(entity_rows)

    return {
        "academic": " | ".join(selected_academic),
        "policy": POLICY_QUERY + (" " + " ".join(new_entities[:2]) if new_entities else ""),
        "livelihood": livelihood_query,
    }


def _detect_new_entities(entity_rows) -> list[str]:
    """Detect entities with recent frequency spike."""
    from collections import Counter
    freq = Counter()
    for (ent_json,) in entity_rows:
        if ent_json:
            for e in json.loads(ent_json):
                freq[e] += 1
    # Return entities that appear 3+ times (signal of emerging topic)
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
        print("[search_feedback] No report found, using defaults for tomorrow")
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
    date_arg = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--date" else None
    main(date_str=date_arg)
```

- [ ] **Step 3: Test feedback loop**

```powershell
python harness/search_feedback.py --date 2026-05-25
```

Expected: Creates `.constraint_cache.json` with next-day queries. Prints score summary.

- [ ] **Step 4: Commit**

```bash
git add harness/search_feedback.py harness/indexer.py
git commit -m "feat(harness): add search_feedback — adaptive search term tuning from daily report"
```

---

### Task 4: 更新日报模板 — 新增民生观察段

**Files:**
- Modify: `C:\Users\Chucky\Documents\Obsidian Vault\claude专属文件夹\news\news-template.md`

- [ ] **Step 1: Update template**

Read the template first, then edit to insert the livelihood section between "重点分析" (section 2) and "因果追踪" (section 3).

Insert after the "关联你的学习目标层" block and before "## 📊 三、因果追踪":

```markdown
---

## 🏘️ 二点五、民生观察

> 中国各地民生与社会变迁信号。浙江优先。无信号标注"本周无显著信号"。

### 就业

| 事件 | 地域 | 来源 | 对菲菲 |
|------|------|------|--------|
| 本周无显著信号 | - | - | - |

### 教育

| 事件 | 地域 | 来源 | 对菲菲 |
|------|------|------|--------|
| 本周无显著信号 | - | - | - |

### 消费

| 事件 | 地域 | 来源 | 对菲菲 |
|------|------|------|--------|
| 本周无显著信号 | - | - | - |

### 基层治理

| 事件 | 地域 | 来源 | 对菲菲 |
|------|------|------|--------|
| 本周无显著信号 | - | - | - |

---
```

Update the version line in the template footer from `v3.1` to `v3.2` and note the change.

- [ ] **Step 2: Verify template parses correctly**

```powershell
Get-Content "C:\Users\Chucky\Documents\Obsidian Vault\claude专属文件夹\news\news-template.md" | Select-String "民生观察"
```

Expected: Find the new section header.

- [ ] **Step 3: Commit**

```bash
git add "C:\Users\Chucky\Documents\Obsidian Vault\claude专属文件夹\news\news-template.md"
git commit -m "feat(template): v3.2 — add livelihood observation section for 民生观察"
```

---

### Task 5: 更新 `generate_report.py` — 支持 livelihood 聚类输出

**Files:**
- Modify: `D:\Claude\harness\generate_report.py`

- [ ] **Step 1: Read current generate_report.py structure**

The current script reads snips, runs feature_finder, generates cluster output, and inserts it before "六、数据源说明". We need to also process livelihood snippets and insert them before the same target, OR output them separately.

The simplest approach: when building cluster_lines, detect livelihood snippets by `section == "livelihood"` and group them separately. But the existing cluster logic already handles cross-section clustering — livelihood snippets may naturally cluster with policy snippets.

Instead, modify the script to also output a livelihood summary block when livelihood snippets exist.

- [ ] **Step 2: Add livelihood summary generation**

Add this after the cluster_lines generation (~line 85 in current code), before the target insertion:

```python
# Check for livelihood snippets
liv_snips = [s for s in snips if s.get("section") == "livelihood"]
if liv_snips:
    liv_lines = [
        "## 🏘️ 民生观察（anysearch 自动采集）",
        "",
    ]
    for topic in ["就业", "教育", "消费", "基层治理"]:
        topic_keywords = {
            "就业": ["就业", "招聘", "灵活用工", "劳动", "失业"],
            "教育": ["教育", "学区", "职业", "双减", "高等"],
            "消费": ["消费", "物价", "零售", "收入", "购买"],
            "基层治理": ["基层", "社区", "乡村", "县域", "治理"],
        }
        matched = []
        for s in liv_snips:
            headline = s.get("headline", "")
            if any(kw in headline for kw in topic_keywords.get(topic, [])):
                matched.append(s)

        liv_lines.append(f"### {topic}")
        if matched:
            for s in matched[:3]:
                source_url = s.get("sources", [{}])[0].get("url", "") if s.get("sources") else ""
                headline = s.get("headline", "").replace("**", "").strip()
                liv_lines.append(f"- **{headline}** — {source_url}")
        else:
            liv_lines.append("- 本周无显著信号")
        liv_lines.append("")

    # Insert livelihood before cluster analysis
    cluster_lines = liv_lines + ["---"] + cluster_lines
```

- [ ] **Step 3: Verify generate_report still runs**

```powershell
python harness/generate_report.py 2026-05-25
```

Expected: No errors. Output has livelihood section if livelihood snippets exist in state.db.

- [ ] **Step 4: Commit**

```bash
git add harness/generate_report.py
git commit -m "feat(harness): add livelihood section support to generate_report"
```

---

### Task 6: 更新 daily-news-trigger 工作流记忆

**Files:**
- Modify: `C:\Users\Chucky\.claude\projects\D--Claude\memory\daily-news-trigger.md`

- [ ] **Step 1: Update the memory file**

Key changes to the memory file:

1. **步骤 2（三数据源并行搜索）** — 新增源 D：anysearch batch_search
   - 替换源 C（国内 WebFetch 6 站 → 仅保留财联社）
   - 新增 anysearch 学术 + 政策 + 民生三槽查询

2. **步骤 7（特征激活）** — 新增步骤 7c：搜索反馈
   - 日报写入后运行 `python harness/search_feedback.py --date <date>`

3. **环境依赖清单** — 新增 anysearch API key 状态

Make these edits to the file:

After the "源 C：中国科技/政策源 WebFetch" section, replace with:

```markdown
### 源 C：anysearch batch_search（新增，替代多数 WebFetch）

调用 `harness/news_daily_search.py` 通过 anysearch API 三槽并行搜索：

**槽 1 — 学术（高因子论文）：**
- domain: academic, sub_domain: academic.search
- max_results: 5
- 搜索词由 `.constraint_cache.json` 驱动（search_feedback.py 每日更新）

**槽 2 — 国内政策/行业：**
- content_types: news, zone: cn, freshness: day, max_results: 8
- 覆盖 AI 政策、科技监管、数据法律、知识产权

**槽 3 — 人文社科/民生：**
- content_types: news, freshness: week, max_results: 8
- 就业/教育/消费/基层治理，浙江优先

### 源 D：财联社 WebFetch（保留）

- **财联社电报**：`https://www.cls.cn/telegraph` → 限前 50 行

### 已移除的 WebFetch 源

以下源已被 anysearch 替代，不再直接 WebFetch：
- 36kr 快讯 → anysearch 槽 2
- 人民网 → anysearch 槽 2
- 新华网 → anysearch 槽 2
- 光明网 → anysearch 槽 2
- TechNode → anysearch 槽 2
```

Update step 7 to include search feedback:

```markdown
## 步骤 7：特征激活 + 搜索反馈

### 7a. 向量化 + 存储嵌入
### 7b. 特征聚类 + 激活存储
### 7c. 搜索词自适应（新增）

```powershell
python harness/search_feedback.py --date <date>
```

读取当日日报，计算各搜索主题产出得分，更新权重，生成次日搜索词到 `.constraint_cache.json`。
```

Update environment dependency table:

```markdown
| anysearch API | ✅ 已配置 | API key 写入 .env |
| anysearch batch_search | ✅ 可用 | 三槽并行搜索 |
```

- [ ] **Step 2: Verify memory file is valid markdown**

```powershell
Get-Content "C:\Users\Chucky\.claude\projects\D--Claude\memory\daily-news-trigger.md" | Select-String "anysearch"
```

Expected: Multiple matches showing anysearch integration.

- [ ] **Step 3: Commit**

```bash
git add "C:\Users\Chucky\.claude\projects\D--Claude\memory\daily-news-trigger.md"
git commit -m "feat(memory): integrate anysearch into daily news trigger workflow"
```

---

### Task 7: 端到端集成测试

- [ ] **Step 1: Run the full anysearch pipeline**

```powershell
python harness/news_daily_search.py --date 2026-05-25
```

Expected: 3 slots all return results, snippets saved to state.db.

- [ ] **Step 2: Verify snippets in DB**

```powershell
python -c "from harness.indexer import HarnessDB; db = HarnessDB(); snips = db.get_news_snippets(date='2026-05-25'); acad = [s for s in snips if s['section']=='academic-high-impact']; live = [s for s in snips if s['section']=='livelihood']; print(f'academic: {len(acad)}, livelihood: {len(live)}, total: {len(snips)}')"
```

Expected: `academic: 5+, livelihood: 4+, total: N`

- [ ] **Step 3: Run vectorization on the new snippets**

```powershell
python harness/news_vectorizer.py "C:\Users\Chucky\Documents\Obsidian Vault\claude专属文件夹\news\2026-05-25.md" --embed
```

Expected: No errors. Embeddings computed and stored.

- [ ] **Step 4: Run search feedback**

```powershell
python harness/search_feedback.py --date 2026-05-25
```

Expected: `.constraint_cache.json` created with next-day queries. Check weights in state.db:

```powershell
python -c "import sqlite3; conn = sqlite3.connect('harness/state.db'); [print(r) for r in conn.execute('SELECT * FROM search_topic_weights')]"
```

- [ ] **Step 5: Verify .constraint_cache.json is valid**

```powershell
python -c "import json; c = json.load(open('harness/.constraint_cache.json')); print(c.keys()); print(c['queries'].keys())"
```

Expected: `dict_keys(['date', 'queries', 'generated_at'])`, `dict_keys(['academic', 'policy', 'livelihood'])`

- [ ] **Step 6: Commit final verification**

```bash
git add harness/.constraint_cache.json  # if tracked
git commit -m "test(harness): verify anysearch news pipeline end-to-end"
```
