"""Fetch GitHub Trending repos and save to state.db as section="github-trending".

Usage:
    python -m duonews --step github --date 2026-05-31

Fetches https://github.com/trending (daily) via WebFetch/urllib,
parses repo cards, classifies by category, and saves to state.db.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import date
from pathlib import Path

GITHUB_TRENDING_URL = "https://github.com/trending?since=daily"
REQUEST_TIMEOUT = 20

# Classification keywords for repo categorization
CATEGORY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("ai-agent", [
        "agent", "multi-agent", "agentic", "autonomous", "swarm",
        "llm agent", "ai agent", "tool-use", "function call",
    ]),
    ("ai-model", [
        "LLM", "language model", "transformer", "fine-tune", "inference",
        "GPT", "Claude", "diffusion", "text-to-", "speech", "TTS",
        "embedding", "RAG", "vector", "generation",
    ]),
    ("dev-tool", [
        "CLI", "terminal", "shell", "git", "workflow", "automation",
        "plugin", "extension", "IDE", "editor", "linter", "formatter",
        "code", "programming", "debug", "compiler",
    ]),
    ("infra-platform", [
        "infra", "platform", "database", "storage", "kubernetes",
        "docker", "deploy", "cloud", "server", "api", "backend",
        "monitoring", "observability", "pipeline",
    ]),
    ("data-ai-infra", [
        "data", "pipeline", "ETL", "vector db", "embedding",
        "training", "dataset", "synthetic data", "benchmark",
        "evaluation", "scraping", "crawler",
    ]),
    ("frontend-ui", [
        "frontend", "UI", "UX", "react", "vue", "component",
        "design", "css", "animation", "canvas", "visual",
    ]),
]


def _get_date(date_str: str | None = None) -> str:
    return date_str or date.today().isoformat()


def _fetch_trending_page() -> str | None:
    """Fetch the GitHub Trending daily page as HTML."""
    req = urllib.request.Request(
        GITHUB_TRENDING_URL,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        resp = urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)
        if resp.status != 200:
            print(f"[github_trending] HTTP {resp.status}", file=sys.stderr)
            return None
        return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[github_trending] Fetch failed: {e}", file=sys.stderr)
        return None


def _parse_trending_html(html: str) -> list[dict]:
    """Parse GitHub trending page HTML, extract repo cards.

    Returns list of dicts with keys: name, description, language, stars_today, total_stars, url.
    """
    repos: list[dict] = []

    # GitHub trending uses <article class="Box-row"> for each repo
    # Try to extract repo info from the HTML using regex patterns
    # Pattern 1: Find repo names from h2 headings
    h2_pattern = re.compile(
        r'<h2[^>]*class="[^"]*h3[^"]*lh-condensed[^"]*"[^>]*>.*?<a[^>]*href="/([^/"]+)/([^/"]+)"[^>]*>',
        re.DOTALL,
    )

    matches = h2_pattern.findall(html)
    if not matches:
        # Fallback: try broader pattern
        h2_pattern = re.compile(
            r'<a[^>]*href="/([a-zA-Z0-9._-]+)/([a-zA-Z0-9._-]+)"[^>]*>\s*<span[^>]*>\1',
            re.DOTALL,
        )
        matches = h2_pattern.findall(html)

    # Extract star counts
    star_pattern = re.compile(
        r'/([a-zA-Z0-9._-]+)/([a-zA-Z0-9._-]+)/stargazers[^"]*"[^>]*>\s*([\d,]+)\s*</a>',
        re.DOTALL,
    )
    star_map: dict[str, str] = {}
    for owner, name, count in star_pattern.findall(html):
        star_map[f"{owner}/{name}"] = count.replace(",", "")

    # Try simpler star extraction for "stars today"
    stars_today_pattern = re.compile(
        r'/([a-zA-Z0-9._-]+)/([a-zA-Z0-9._-]+)[^"]*"[^>]*>\s*([\d,]+)\s*stars\s+today',
        re.DOTALL,
    )
    today_star_map: dict[str, str] = {}
    for owner, name, count in stars_today_pattern.findall(html):
        today_star_map[f"{owner}/{name}"] = count.replace(",", "")

    # Extract descriptions
    desc_pattern = re.compile(
        r'<p[^>]*class="[^"]*col-9[^"]*color-fg-muted[^"]*my-1[^"]*pr-4[^"]*"[^>]*>\s*(.*?)\s*</p>',
        re.DOTALL,
    )
    descriptions = desc_pattern.findall(html)

    for i, (owner, name) in enumerate(matches):
        full_name = f"{owner}/{name}"
        desc = ""
        if i < len(descriptions):
            desc = re.sub(r'<[^>]+>', '', descriptions[i]).strip()
            # Trim to 200 chars
            desc = desc[:200]

        # Try to extract language
        lang = ""
        lang_pattern = re.compile(
            rf'{re.escape(full_name)}.*?<span[^>]*itemprop="programmingLanguage"[^>]*>\s*(\w[\w\s+#.-]*?)\s*</span>',
            re.DOTALL,
        )
        lang_match = lang_pattern.search(html)
        if lang_match:
            lang = lang_match.group(1).strip()

        repos.append({
            "name": full_name,
            "owner": owner,
            "repo": name,
            "description": desc,
            "language": lang,
            "stars_today": int(today_star_map.get(full_name, 0)),
            "total_stars": int(star_map.get(full_name, 0)),
            "url": f"https://github.com/{full_name}",
        })

    return repos


def _classify_repo(repo: dict) -> str:
    """Classify a repo into a category based on name + description."""
    text = f"{repo['name']} {repo['description']}".lower()
    best_cat = "other"
    best_hits = 0
    for cat, keywords in CATEGORY_KEYWORDS:
        hits = sum(1 for kw in keywords if kw.lower() in text)
        if hits > best_hits:
            best_hits = hits
            best_cat = cat
    return best_cat


def _repos_to_snippets(repos: list[dict], date_str: str) -> list[dict]:
    """Convert parsed repos to news_snippet format for state.db."""
    snippets = []
    for r in repos:
        cat = _classify_repo(r)
        headline = f"[GitHub Trending] {r['name']}"
        summary = (
            f"⭐ {r['stars_today']} today ({r['total_stars']} total) | "
            f"{r['language'] or 'N/A'} | {cat} | "
            f"{r['description'][:150]}"
        )[:500]

        content_hash = hashlib.sha256(
            f"{r['name']}{date_str}".encode()
        ).hexdigest()

        snippets.append({
            "date": date_str,
            "section": "github-trending",
            "headline": headline,
            "summary": summary,
            "entities": [cat, r["language"]] if r["language"] else [cat],
            "sources": [{
                "url": r["url"],
                "title": r["name"],
            }],
            "source_rating": "github-trending",
            "content_hash": content_hash,
            "embedding": None,
        })

    return snippets


def fetch_github_trending(date_str: str | None = None, top_n: int = 25) -> int:
    """Fetch GitHub trending repos and save to state.db.

    Returns number of repos saved.
    """
    from harness.indexer import HarnessDB

    today = _get_date(date_str)
    print(f"[github_trending] Fetching GitHub trending for {today}", file=sys.stderr)

    html = _fetch_trending_page()
    if html is None:
        print("[github_trending] Page fetch failed", file=sys.stderr)
        return 0

    repos = _parse_trending_html(html)
    print(f"[github_trending] Parsed {len(repos)} repos from trending page", file=sys.stderr)

    if not repos:
        return 0

    # Classify and sort by stars today
    for r in repos:
        r["_category"] = _classify_repo(r)

    repos.sort(key=lambda r: r["stars_today"], reverse=True)
    top = repos[:top_n]

    by_cat: dict[str, int] = {}
    for r in top:
        by_cat[r["_category"]] = by_cat.get(r["_category"], 0) + 1
    print(f"[github_trending] Top {len(top)} repos by category: {by_cat}", file=sys.stderr)

    snippets = _repos_to_snippets(top, today)
    db = HarnessDB()
    saved = 0
    for snip in snippets:
        row_id = db.save_news_snippet(**snip)
        if row_id:
            saved += 1
    db.close()

    print(f"[github_trending] Saved {saved} repos to state.db", file=sys.stderr)
    return saved


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Fetch GitHub trending repos")
    parser.add_argument("--date", help="Date YYYY-MM-DD")
    parser.add_argument("--top", type=int, default=25)
    args = parser.parse_args()
    n = fetch_github_trending(date_str=args.date, top_n=args.top)
    print(n)
