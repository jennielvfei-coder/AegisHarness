"""Fetch today's cs.AI papers from arXiv API, classify, and save to state.db.

Queries export.arxiv.org (free, no key), parses Atom XML with stdlib only,
classifies papers into 8 topic directions, and saves as news_snippets with
section="academic-high-impact".

Usage:
    python -m harness.news_agent --step arxiv --date 2026-05-25
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path

from . import HARNESS_DIR

ARXIV_API_URL = "https://export.arxiv.org/api/query"
SEARCH_QUERY = "cat:cs.AI"
REQUEST_TIMEOUT = 15
RETRY_DELAYS = [5, 15, 30]  # seconds, exponential-ish backoff

TOPIC_CLASSIFIERS: list[tuple[str, list[str]]] = [
    ("social-ai", [
        "social AI", "cooperation", "collective behavior",
        "multi-agent social", "social simulation", "social behavior",
        "社会", "合作", "集体行为", "社会模拟",
    ]),
    ("cognitive-science", [
        "cognitive architecture", "reasoning", "metacognition",
        "theory of mind", "cognitive science", "cognitive model",
        "认知", "推理", "元认知", "心智理论",
    ]),
    ("embodied-ai", [
        "embodied AI", "robotics", "world model", "sensorimotor",
        "robot", "manipulation", "navigation", "physical",
        "具身", "机器人", "世界模型", "传感器",
    ]),
    ("ai-safety", [
        "AI safety", "alignment", "formal verification", "robustness",
        "adversarial", "guardrail", "red team", "safety",
        "安全", "对齐", "验证", "鲁棒",
    ]),
    ("bci", [
        "BCI", "neural signal", "EEG", "brain-computer",
        "neural interface", "brain signal", "neuro",
        "脑机", "脑电", "神经信号",
    ]),
    ("knowledge-graphs", [
        "knowledge graph", "neuro-symbolic", "knowledge representation",
        "ontology", "symbolic reasoning", "graph neural",
        "知识图谱", "神经符号", "知识表示",
    ]),
    ("causal-inference", [
        "causal inference", "mechanistic interpretability",
        "learning theory", "generalization", "causality",
        "causal discovery", "结构因果", "因果推断", "泛化",
    ]),
    ("multi-agent", [
        "multi-agent", "coordination", "game theory",
        "collective intelligence", "agent coordination",
        "cooperative AI", " emergent",
        "多智能体", "博弈", "协调", "涌现",
    ]),
]


def _get_date(date_str=None):
    return date_str or date.today().isoformat()


def _build_query_url(max_results=30):
    return (
        f"{ARXIV_API_URL}?search_query={SEARCH_QUERY}"
        f"&sortBy=submittedDate&sortOrder=descending"
        f"&max_results={max_results}"
    )


def _fetch_atom(url):
    from . import SYSTEM_PROXY

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "HarnessDailyNews/1.0 (contact: harness@local)"},
    )

    def _open():
        if SYSTEM_PROXY:
            proxy_handler = urllib.request.ProxyHandler(
                {"http": SYSTEM_PROXY, "https": SYSTEM_PROXY}
            )
            opener = urllib.request.build_opener(proxy_handler)
            return opener.open(req, timeout=REQUEST_TIMEOUT)
        return urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)

    last_err = None
    for attempt, delay in enumerate([1] + RETRY_DELAYS):
        try:
            if attempt > 0:
                print(
                    f"[arxiv_fetch] Retry {attempt}/{len(RETRY_DELAYS)}"
                    f" after {delay}s (429 backoff)",
                    file=sys.stderr,
                )
            time.sleep(delay)
            resp = _open()
            if resp.status == 429:
                last_err = Exception("HTTP 429 Too Many Requests")
                continue
            if resp.status != 200:
                print(f"[arxiv_fetch] HTTP {resp.status}", file=sys.stderr)
                return None
            return resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                last_err = e
                continue
            print(f"[arxiv_fetch] HTTP {e.code}: {e}", file=sys.stderr)
            return None
        except Exception as e:
            last_err = e
            break

    print(f"[arxiv_fetch] Request failed: {last_err}", file=sys.stderr)
    return None


def _parse_entries(xml_text):
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "arxiv": "http://arxiv.org/schemas/atom",
    }
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"[arxiv_fetch] XML parse error: {e}", file=sys.stderr)
        return []

    entries = []
    for entry_elem in root.findall("atom:entry", ns):
        def _text(tag):
            el = entry_elem.find(f"atom:{tag}", ns)
            return el.text.strip() if el is not None and el.text else ""

        arxiv_id = ""
        id_text = _text("id")
        m = re.search(r"arxiv\.org/abs/([^v]+)", id_text)
        if m:
            arxiv_id = m.group(1)

        authors = [
            a.find("atom:name", ns).text.strip()
            for a in entry_elem.findall("atom:author", ns)
            if a.find("atom:name", ns) is not None
        ]

        categories = [
            c.get("term", "")
            for c in entry_elem.findall("atom:category", ns)
        ]

        primary_cat = ""
        pc = entry_elem.find("arxiv:primary_category", ns)
        if pc is not None:
            primary_cat = pc.get("term", "")

        comment = _text("arxiv:journal_ref") or ""
        if not comment:
            comment_el = entry_elem.find("arxiv:comment", ns)
            if comment_el is not None and comment_el.text:
                comment = comment_el.text.strip()

        entries.append({
            "arxiv_id": arxiv_id,
            "title": _text("title").replace("\n", " ").strip(),
            "summary": _text("summary").replace("\n", " ").strip(),
            "published": _text("published"),
            "authors": authors,
            "categories": categories,
            "primary_category": primary_cat,
            "comment": comment,
        })

    return entries


def _filter_recent(entries, date_str, lookback=4):
    target = date.fromisoformat(date_str)
    cutoff = target - timedelta(days=lookback)

    filtered = []
    for e in entries:
        pub = e.get("published", "")
        if not pub:
            continue
        try:
            pub_date = date.fromisoformat(pub[:10])
        except ValueError:
            continue
        if cutoff <= pub_date <= target:
            filtered.append(e)

    return filtered


def _classify_paper(entry):
    text = f"{entry['title']} {entry['summary']}".lower()
    best_topic = None
    best_hits = 0

    for topic, keywords in TOPIC_CLASSIFIERS:
        hits = sum(1 for kw in keywords if kw.lower() in text)
        if hits > best_hits:
            best_hits = hits
            best_topic = topic

    return best_topic if best_hits >= 2 else None


def _papers_to_snippets(entries, date_str):
    snippets = []
    for e in entries:
        title = e["title"][:200]
        arxiv_id = e["arxiv_id"]
        abstract = e["summary"][:400]
        topic = e.get("_topic", "")
        primary_cat = e.get("primary_category", "cs.AI")

        summary_parts = [f"[{arxiv_id}]"]
        if topic:
            summary_parts.append(f"({topic})")
        if e.get("comment"):
            summary_parts.append(e["comment"][:80])
        summary_parts.append(abstract)
        summary = " ".join(summary_parts)[:500]

        content_hash = hashlib.sha256(
            f"{arxiv_id}{title[:100]}{date_str}".encode()
        ).hexdigest()

        snippets.append({
            "date": date_str,
            "section": "academic-high-impact",
            "headline": title,
            "summary": summary,
            "entities": [primary_cat, topic] if topic else [primary_cat],
            "sources": [{
                "url": f"https://arxiv.org/abs/{arxiv_id}",
                "title": title,
            }],
            "source_rating": "arxiv",
            "content_hash": content_hash,
            "embedding": None,
        })

    return snippets


def _fallback_anysearch(date_str: str, top_n: int = 15) -> int:
    """When arXiv is unavailable, search academic topics via anysearch CLI.

    Uses the same ACADEMIC_TOPICS as search.py, queries 3 rotated topics,
    and saves results as section="academic-high-impact" (same as arXiv).
    """
    import json
    import subprocess
    from pathlib import Path
    from ..indexer import HarnessDB
    from .ingest import ingest, AnysearchResult
    from . import get_proxy_env

    SKILL_DIR = Path.home() / ".claude" / "skills" / "anysearch" / "scripts"
    CLI_PATH = SKILL_DIR / "anysearch_cli.py"

    if not CLI_PATH.exists():
        print("[arxiv_fetch] anysearch CLI not found for fallback", file=sys.stderr)
        return 0

    # Rotate through 3 academic topics for diversity
    doy = date.fromisoformat(date_str).timetuple().tm_yday
    topics = [
        "AI safety, alignment, formal verification, robustness high-impact",
        "multi-agent coordination, game theory, collective intelligence, cognitive architecture",
        "embodied AI, robotics, world models, causal inference, knowledge graphs",
    ]
    topic = topics[doy % len(topics)]

    print(f"[arxiv_fetch] arXiv unavailable, falling back to anysearch: {topic[:60]}...", file=sys.stderr)

    try:
        env = os.environ.copy()
        env.update(get_proxy_env())
        result = subprocess.run(
            [
                sys.executable, str(CLI_PATH), "search",
                topic,
                "--domain", "academic",
                "--sub_domain", "academic.search",
                "--max_results", "15",
                "--freshness", "week",
            ],
            capture_output=True, encoding="utf-8", timeout=45,
            cwd=str(SKILL_DIR),
            env=env,
        )

        if result.returncode != 0:
            print(f"[arxiv_fetch] anysearch fallback failed: {result.stderr[:300]}", file=sys.stderr)
            return 0

        # Parse anysearch output — extract title/url/snippet pairs
        parsed: list[dict] = []
        lines = result.stdout.split('\n')
        cur: dict = {}
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # Match numbered entries like "### 1. Title" or "## Title"
            if re.match(r'^#{2,4}\s+\d*\.?\s*', line):
                if cur.get('title') and cur.get('url'):
                    parsed.append(cur)
                cur = {'title': re.sub(r'^#{2,4}\s+\d*\.?\s*', '', line).strip()}
            elif line.startswith('- **URL**:') or line.startswith('**URL**:'):
                cur['url'] = line.split(':', 1)[-1].strip()
            elif line.startswith('http') and 'url' not in cur:
                cur['url'] = line.strip()
            elif not line.startswith('#') and not line.startswith('-') and 'snippet' not in cur and cur.get('title'):
                cur['snippet'] = line[:300]

        if cur.get('title') and cur.get('url'):
            parsed.append(cur)

        if not parsed:
            print("[arxiv_fetch] anysearch fallback: no results parsed", file=sys.stderr)
            return 0

        any_results = [
            AnysearchResult(
                title=r['title'][:200],
                url=r['url'],
                snippet=r.get('snippet', ''),
                result_date=date_str,
            )
            for r in parsed[:top_n]
        ]

        snippets = ingest(any_results, "academic-high-impact", date_str=date_str)
        db = HarnessDB()
        saved = 0
        for snip in snippets:
            row_id = db.save_news_snippet(**snip)
            if row_id:
                saved += 1
        db.close()

        print(f"[arxiv_fetch] anysearch fallback: saved {saved} papers to state.db", file=sys.stderr)
        return saved

    except Exception as e:
        print(f"[arxiv_fetch] anysearch fallback error: {e}", file=sys.stderr)
        return 0


def fetch_arxiv(date_str=None, max_results=50, top_n=15):
    from ..indexer import HarnessDB

    today = _get_date(date_str)
    print(f"[arxiv_fetch] Querying arXiv for {today} (max_results={max_results})", file=sys.stderr)

    url = _build_query_url(max_results)
    xml_text = _fetch_atom(url)
    if xml_text is None:
        print("[arxiv_fetch] arXiv API unavailable, trying anysearch fallback...", file=sys.stderr)
        return _fallback_anysearch(today, top_n)

    entries = _parse_entries(xml_text)
    print(f"[arxiv_fetch] Parsed {len(entries)} entries from Atom feed", file=sys.stderr)

    filtered = _filter_recent(entries, today)
    print(f"[arxiv_fetch] {len(filtered)} papers match date filter", file=sys.stderr)

    if not filtered:
        print("[arxiv_fetch] No papers for today (expected on weekends)", file=sys.stderr)
        return 0

    for e in filtered:
        e["_topic"] = _classify_paper(e)
        text = f"{e['title']} {e['summary']}".lower()
        e["_hits"] = sum(
            sum(1 for kw in kws if kw.lower() in text)
            for _, kws in TOPIC_CLASSIFIERS
        )

    filtered.sort(key=lambda e: e["_hits"], reverse=True)
    top = filtered[:top_n]

    classified = sum(1 for e in top if e["_topic"])
    print(f"[arxiv_fetch] Top {len(top)} papers, {classified} classified", file=sys.stderr)

    snippets = _papers_to_snippets(top, today)
    db = HarnessDB()
    saved = 0
    for snip in snippets:
        row_id = db.save_news_snippet(**snip)
        if row_id:
            saved += 1
    db.close()

    print(f"[arxiv_fetch] Saved {saved} papers to state.db", file=sys.stderr)
    return saved


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Fetch arXiv cs.AI papers for daily news")
    parser.add_argument("--date", help="Date YYYY-MM-DD")
    parser.add_argument("--max-results", type=int, default=30)
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args()
    n = fetch_arxiv(date_str=args.date, max_results=args.max_results, top_n=args.top)
    print(n)
