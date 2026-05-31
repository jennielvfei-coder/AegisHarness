"""News Agent — unified entry point for the daily news pipeline.

All news workflow logic lives here. The skill file (harness_news-agent.md)
handles orchestration and user interaction; this package handles data
processing and API calls.

Usage:
    python -m harness.news_agent --step search --date 2026-05-27
    python -m harness.news_agent --step all --date 2026-05-27

Programmatic:
    from harness.news_agent import run_daily_search, generate_brief
"""

from __future__ import annotations
import os
import sys
from pathlib import Path

# ── Proxy detection ───────────────────────────────────────────────────
# Both arxiv.py (urllib) and search.py (subprocess→anysearch) need the
# system proxy.  Without this, external API calls fail with connection
# refused because all outbound traffic must go through 127.0.0.1:7890.

def _detect_system_proxy() -> str | None:
    """Return 'http://127.0.0.1:7890' if the Windows system proxy is
    enabled, otherwise check env vars, otherwise None."""
    # 1. Check env vars first (explicit override)
    for k in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        v = os.environ.get(k)
        if v:
            return v
    # 2. Check Windows system proxy (Control Panel → Internet Settings)
    if sys.platform == "win32":
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            )
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if enabled:
                server, _ = winreg.QueryValueEx(key, "ProxyServer")
                proxy = server.strip() if server.strip() else None
                if proxy:
                    # server may be "127.0.0.1:7890" or "http=127.0.0.1:7890;https=..."
                    # for simplicity take the first entry
                    if "=" in proxy:
                        proxy = proxy.split(";")[0].split("=")[-1]
                    return f"http://{proxy}" if not proxy.startswith("http") else proxy
        except Exception:
            pass
    return None

SYSTEM_PROXY = _detect_system_proxy()


def get_proxy_env() -> dict[str, str]:
    """Return {http_proxy, https_proxy} dict for subprocess env injection."""
    if SYSTEM_PROXY:
        return {"http_proxy": SYSTEM_PROXY, "https_proxy": SYSTEM_PROXY}
    return {}


# ── Shared path constants (single definition, import in submodules) ──
HARNESS_DIR = Path(__file__).resolve().parent.parent
CONSTRAINT_CACHE = HARNESS_DIR / ".constraint_cache.json"
DAILY_BRIEF = HARNESS_DIR / ".daily_brief.md"
FEISHU_DOC_CACHE = HARNESS_DIR / ".feishu_doc_cache.json"
OBSIDIAN_NEWS = Path.home() / "Documents" / "Obsidian Vault" / "claude专属文件夹" / "news"
STATE_DB = HARNESS_DIR / "state.db"


def find_recent_report(date_str: str, max_lookback: int = 7) -> str | None:
    """Find the most recent available daily report before `date_str`.

    When yesterday's news wasn't run, fall back to the last available report
    so the "今日反馈" section still has a judgment baseline to reference.

    Returns the report content as a string, or None if no reports exist.
    """
    from datetime import date, timedelta
    target = date.fromisoformat(date_str)
    for offset in range(1, max_lookback + 1):
        candidate_date = target - timedelta(days=offset)
        candidate_path = OBSIDIAN_NEWS / f"{candidate_date.isoformat()}.md"
        if candidate_path.exists():
            text = candidate_path.read_text(encoding="utf-8")
            if len(text.strip()) > 100:  # not an empty stub
                return text
    return None


def extract_judgment_baseline(report_text: str) -> dict:
    """Extract key judgments from a previous report for feedback continuity.

    Returns a dict with keys: date, headline_judgment, hypotheses, prophet_signals.
    Each value is a compact string suitable for injection into the new report's context.
    """
    import re
    result: dict = {"hypotheses": [], "prophet_signals": []}

    # Extract date from first heading
    m = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', report_text)
    if m:
        result["date"] = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    # Extract "今日判断"
    m = re.search(r'> \*\*今日判断：\*\*\s*(.+?)(?:\n|$)', report_text)
    if m:
        result["headline_judgment"] = m.group(1).strip()[:200]

    # Extract hypothesis table rows (H1, H2, etc.)
    for m in re.finditer(r'^\|\s*(H\d+)\s*\|(.+?)\|(.+?)\|', report_text, re.MULTILINE):
        result["hypotheses"].append(f"{m.group(1)}: {m.group(2).strip()} → {m.group(3).strip()}")

    # Extract prophet signals
    for m in re.finditer(r'^\|\s*(P\d+)\s*\|(.+?)\|(.+?)\|(.+?)\|(.+?)\|', report_text, re.MULTILINE):
        result["prophet_signals"].append(
            f"{m.group(1)}: {m.group(2).strip()} [{m.group(5).strip()}]"
        )

    return result


# ── Public API — re-export from submodules ────────────────────────────
from .ingest import AnysearchResult, ingest
from .search import run_daily_search
from .arxiv import fetch_arxiv
from .preprocess import generate_brief
from .cross_day import search_cross_day, format_cross_day_results
from .push import push_to_feishu
from .vectorize import parse_news_file, vectorize_snippets, NewsSnippet
from .feedback import run_feedback_loop
from .diagnose import run_all_checks
from .config import (
    NEWS_INTENT_KEYWORDS,
    NEWS_DOMAIN_KEYWORDS,
    NEWS_SKILL_NAME,
    build_matcher_regex,
    build_intents_registry,
)
