"""DuoNews — 独立新闻智能体，每日新闻工作流统一编排。

DuoNews 独立于 Harness 系统运行，拥有完整的新闻流水线能力：
搜索 → 预处理 → 跨日分析 → 日报 → 飞书推送 → 向量化 → 反馈闭环。

同时继承 Harness 的核心分析能力：
  - feature_finder: 实体聚类异常检测
  - competing_hypotheses: 竞争假设引擎
  - judgment_graph: 判断图谱跨日追踪
  - feature_library: 特征库匹配

Usage:
    python -m duonews --step search --date 2026-05-31
    python -m duonews --step all --date 2026-05-31

Programmatic:
    from duonews import run_daily_search, generate_brief
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


# ── DuoNews path constants (独立于 Harness，自有目录) ──────────────
DUONEWS_DIR = Path(__file__).resolve().parent          # duonews/
CONSTRAINT_CACHE = DUONEWS_DIR / ".constraint_cache.json"
DAILY_BRIEF = DUONEWS_DIR / ".daily_brief.md"
FEISHU_DOC_CACHE = DUONEWS_DIR / ".feishu_doc_cache.json"
STATE_DB = DUONEWS_DIR / "state.db"
OBSIDIAN_NEWS = Path.home() / "Documents" / "Obsidian Vault" / "claude专属文件夹" / "news"

# Backward-compat alias for submodules that still reference HARNESS_DIR
HARNESS_DIR = DUONEWS_DIR


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

    Returns a dict with keys:
      - date: YYYY-MM-DD of the source report
      - headline_judgment: the "今日判断" text (≤200 chars)
      - hypotheses: list of hypothesis strings (H1: claim → status)
      - prophet_signals: list of structured ProphetSignal dicts with:
          id, claim, time_horizon_days, confidence, created_date,
          verification_criteria, status
      - key_entities: list of entity names extracted from judgment text
        and hypotheses for attention weighting
    """
    import re
    from datetime import date, timedelta

    result: dict = {
        "hypotheses": [],
        "prophet_signals": [],
        "key_entities": [],
    }

    # Extract date from first heading
    m = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', report_text)
    source_date = None
    if m:
        source_date = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        result["date"] = source_date

    # Extract "今日判断"
    m = re.search(r'> \*\*今日判断：\*\*\s*(.+?)(?:\n|$)', report_text)
    if m:
        result["headline_judgment"] = m.group(1).strip()[:200]

    # Extract hypothesis table rows (H1, H2, etc.) — 5-column format:
    # | ID | Claim | Status | Evidence | Confidence |
    for m in re.finditer(
        r'^\|\s*(H\d+)\s*\|(.+?)\|(.+?)\|(.+?)\|(.+?)\|',
        report_text, re.MULTILINE,
    ):
        hid = m.group(1)
        claim = m.group(2).strip()
        status = m.group(3).strip()
        result["hypotheses"].append(f"{hid}: {claim} → {status}")

    # Extract prophet signals — 5-column format:
    # | ID | Claim | Time Horizon | Verification | Confidence |
    for m in re.finditer(
        r'^\|\s*(P\d+)\s*\|(.+?)\|(.+?)\|(.+?)\|(.+?)\|',
        report_text, re.MULTILINE,
    ):
        pid = m.group(1)
        claim = m.group(2).strip()
        time_text = m.group(3).strip()
        verification = m.group(4).strip()
        confidence_str = m.group(5).strip()

        # Parse time horizon: "30天" → 30, "7天" → 7, default 30
        time_days = 30
        time_match = re.search(r'(\d+)\s*天', time_text)
        if time_match:
            time_days = int(time_match.group(1))

        # Parse confidence: "0.65" → 0.65, "高" → 0.7, "中" → 0.5, "低" → 0.3
        confidence = 0.5
        try:
            confidence = float(confidence_str)
        except ValueError:
            conf_map = {"高": 0.7, "中": 0.5, "低": 0.3}
            confidence = conf_map.get(confidence_str, 0.5)

        # Determine status: "observing" if within window, "expired_unverified" if past
        status = "observing"
        if source_date:
            created = date.fromisoformat(source_date)
            expiry = created + timedelta(days=time_days)
            if date.today() > expiry:
                status = "expired_unverified"

        result["prophet_signals"].append({
            "id": pid,
            "claim": claim,
            "time_horizon_days": time_days,
            "confidence": confidence,
            "created_date": source_date or "",
            "verification_criteria": verification,
            "status": status,
        })

    # Extract key_entities from judgment text, hypotheses, and prophet claims
    # Use the ENTITY_DICT alias mapping for entity recognition
    all_text = " ".join([
        result.get("headline_judgment", ""),
        " ".join(result["hypotheses"]),
        " ".join(p["claim"] for p in result["prophet_signals"]),
    ])
    try:
        from .vectorize import _ALIAS_TO_CANONICAL
        found_entities: set[str] = set()
        text_lower = all_text.lower()
        for alias, canonical in _ALIAS_TO_CANONICAL.items():
            if alias in text_lower:
                found_entities.add(canonical)
        result["key_entities"] = sorted(found_entities)[:15]
    except ImportError:
        pass

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
from .prophet_compiler import compile_prophet_signals, inject_as_hypotheses
from .source_bias import track_source_bias, annotate_contradictions
from .config import (
    NEWS_INTENT_KEYWORDS,
    NEWS_DOMAIN_KEYWORDS,
    NEWS_SKILL_NAME,
    build_matcher_regex,
    build_intents_registry,
)
