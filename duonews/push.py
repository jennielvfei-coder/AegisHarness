"""Push daily news report to Feishu wiki child page + group chat summary.

Idempotent by date: creates wiki node under PARENT_NODE, caches tokens for updates.

Usage:
    python -m duonews --step push --date 2026-05-31
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

from . import HARNESS_DIR, CONSTRAINT_CACHE, FEISHU_DOC_CACHE, OBSIDIAN_NEWS

LARK_CLI = "D:/Claude/npm-global/lark-cli.cmd"
PARENT_NODE = "OQlawRWCyiNtdokwq5Ecb3xyn7g"
FALLBACK_CHAT_ID = "oc_a6c486e9557bdc6f04896871f457ecec"


def _load_chat_id():
    if CONSTRAINT_CACHE.exists():
        try:
            data = json.loads(CONSTRAINT_CACHE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("key") == "feishu_daily_news_chat_id":
                        val = item.get("value")
                        if val:
                            return val
            elif isinstance(data, dict):
                val = data.get("feishu", {}).get("daily_news_chat_id")
                if val:
                    return val
        except (json.JSONDecodeError, OSError):
            pass
    return FALLBACK_CHAT_ID


def _load_cache():
    if not FEISHU_DOC_CACHE.exists():
        return {}
    try:
        return json.loads(FEISHU_DOC_CACHE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(data):
    FEISHU_DOC_CACHE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _create_or_update_doc(date_str, report_text):
    """Create wiki node under PARENT_NODE or update existing. Returns wiki URL."""
    doc_title = f"{date_str} 每日新闻"
    cache = _load_cache()
    entry = cache.get(date_str)

    if entry and entry.get("obj_token"):
        obj_token = entry["obj_token"]
        print(f"[feishu_push] Updating existing doc {obj_token}...", file=sys.stderr)
        result = subprocess.run(
            [LARK_CLI, "docs", "+update", "--as", "user",
             "--api-version", "v2",
             "--doc", obj_token,
             "--command", "overwrite",
             "--doc-format", "markdown",
             "--content", "-"],
            input=report_text,
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            print(f"[feishu_push] docs +update failed: {result.stderr[:300]}", file=sys.stderr)
            return None
        wiki_url = f"https://icnn3vs57xkd.feishu.cn/wiki/{entry['node_token']}"
        print(f"[feishu_push] Doc updated: {wiki_url}", file=sys.stderr)
        return wiki_url

    print(f"[feishu_push] Creating wiki node under {PARENT_NODE}...", file=sys.stderr)
    result = subprocess.run(
        [LARK_CLI, "wiki", "+node-create", "--as", "user",
         "--parent-node-token", PARENT_NODE,
         "--title", doc_title],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        print(f"[feishu_push] wiki +node-create failed: {result.stderr[:300]}", file=sys.stderr)
        return None

    try:
        resp = json.loads(result.stdout)
        node_token = resp.get("data", {}).get("node_token", "")
        obj_token = resp.get("data", {}).get("obj_token", "")
    except json.JSONDecodeError:
        print(f"[feishu_push] Bad node-create response: {result.stdout[:300]}", file=sys.stderr)
        return None

    if not node_token or not obj_token:
        print(f"[feishu_push] Missing tokens in response: {result.stdout[:300]}", file=sys.stderr)
        return None

    wiki_url = f"https://icnn3vs57xkd.feishu.cn/wiki/{node_token}"
    print(f"[feishu_push] Wiki node created: {wiki_url}", file=sys.stderr)

    print(f"[feishu_push] Writing content to {obj_token}...", file=sys.stderr)
    result2 = subprocess.run(
        [LARK_CLI, "docs", "+update", "--as", "user",
         "--api-version", "v2",
         "--doc", obj_token,
         "--command", "overwrite",
         "--doc-format", "markdown",
         "--content", "-"],
        input=report_text,
        capture_output=True, text=True, timeout=60
    )
    if result2.returncode != 0:
        print(f"[feishu_push] Content write failed: {result2.stderr[:300]}", file=sys.stderr)

    cache[date_str] = {"node_token": node_token, "obj_token": obj_token}
    _save_cache(cache)

    return wiki_url


def _extract_judgment(report_text):
    """Extract 今日判断 line from report."""
    for line in report_text.split("\n"):
        if "今日判断" in line:
            judgment = line.replace(">", "").replace("**今日判断：**", "").replace("**今日判断:**", "").strip()
            if judgment:
                return judgment
    return ""


def _build_summary(judgment, doc_url, date_str):
    """Build a minimal chat card: judgment + doc link."""
    parts = [f"\U0001f4f0 菲菲每日新闻 — {date_str}"]
    if judgment:
        parts.append("")
        parts.append(f"今日判断：{judgment}")
    parts.append("")
    parts.append(f"阅读全文：{doc_url}")
    return "\n".join(parts)


def push_to_feishu(date_str=None, report_path=None):
    """Push daily news to Feishu: wiki child page + chat summary."""
    date_str = date_str or date.today().isoformat()
    chat_id = _load_chat_id()

    if report_path:
        rp = Path(report_path)
    else:
        rp = OBSIDIAN_NEWS / f"{date_str}.md"

    if not rp.exists():
        print(f"[feishu_push] Report not found: {rp}", file=sys.stderr)
        return False

    report_text = rp.read_text(encoding="utf-8")

    doc_url = _create_or_update_doc(date_str, report_text)
    if not doc_url:
        return False

    judgment = _extract_judgment(report_text)
    summary = _build_summary(judgment, doc_url, date_str)
    content_json = json.dumps({"text": summary}, ensure_ascii=False)
    chat_result = subprocess.run(
        [LARK_CLI, "im", "+messages-send", "--as", "user",
         "--chat-id", chat_id,
         "--content", content_json],
        capture_output=True, text=True, timeout=30
    )
    if chat_result.returncode != 0:
        print(f"[feishu_push] Chat send failed: {chat_result.stderr[:300]}", file=sys.stderr)
        return False

    try:
        cr = json.loads(chat_result.stdout)
        if cr.get("ok"):
            msg_id = cr.get("data", {}).get("message_id", "?")
            print(f"[feishu_push] Chat summary sent: {msg_id}", file=sys.stderr)
        else:
            print(f"[feishu_push] Chat send error: {chat_result.stdout[:300]}", file=sys.stderr)
            return False
    except json.JSONDecodeError:
        print(f"[feishu_push] Chat non-JSON: {chat_result.stdout[:200]}", file=sys.stderr)
        return False

    return True


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Push daily news to Feishu wiki page + chat")
    parser.add_argument("--date", help="Date YYYY-MM-DD")
    parser.add_argument("--report-path", help="Path to report markdown file")
    args = parser.parse_args()
    ok = push_to_feishu(date_str=args.date, report_path=args.report_path)
    sys.exit(0 if ok else 1)
