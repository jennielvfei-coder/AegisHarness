"""Normalize anysearch search results into news_snippets-compatible dicts."""

from __future__ import annotations

import hashlib
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
