"""Seed known failure patterns into fragments table.

Run once to bootstrap the failure_pattern system with today's discoveries.
Safe to re-run — uses INSERT OR IGNORE with tag-based dedup.
"""

import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "state.db"

SEED_FAILURES = [
    {
        "tag": "websearch-deepseek-incompatible",
        "trigger_phrases": '["WebSearch", "deepseek", "今日新闻", "每日新闻", "搜索新闻", "web search"]',
        "content": (
            "WebSearch 工具在 deepseek-v4-pro 模型下返回 400 错误（tool_choice 不支持）。"
            "新闻工作流、任何需要实时信息检索的任务，不要使用 WebSearch。"
            "替代方案：World News API MCP (get_top_news/search_news) 或 WebFetch 直接抓取。"
        ),
        "fragment_type": "failure_pattern",
        "skill_name": "harness_env-fix_news-workflow",
        "confidence": 0.95,
    },
    {
        "tag": "webfetch-skip-preflight-required",
        "trigger_phrases": '["WebFetch", "skipWebFetchPreflight", "今日新闻", "每日新闻", "36kr", "arxiv", "人民网"]',
        "content": (
            "skipWebFetchPreflight: true 必须设置在 settings.local.json 中。"
            "未设置时，每个 WebFetch 调用走 api.claude.ai 预检（国内被墙），"
            "会导致国际源（Reuters/BBC/AP）ECONNREFUSED，国内源间歇可用。"
            "此配置曾被文档反复引用但从未落地到文件——不要假设它已经设置，每次先验证。"
        ),
        "fragment_type": "failure_pattern",
        "skill_name": "harness_env-fix_news-workflow",
        "confidence": 0.95,
    },
    {
        "tag": "mcp-wrapper-blocks-all-local",
        "trigger_phrases": '["MCP", "mcp_wrapper", "world-news-api", "memory", "browser-use", "local-deep-research"]',
        "content": (
            "mcp_wrapper.py 导致所有 local MCP server 全部 Failed to connect。"
            "绕过方法：在 .mcp.json 中直接使用 node/python 命令启动 MCP server，"
            "不经过 wrapper。示例：world-news-api 用 D:\\\\Claude\\\\node.exe 直接启动 index.js。"
            "修复后需要重启 session 才能载入工具。"
        ),
        "fragment_type": "failure_pattern",
        "skill_name": "harness_env-fix_news-workflow",
        "confidence": 0.95,
    },
    {
        "tag": "webfetch-no-retry-failed-sources",
        "trigger_phrases": '["WebFetch", "retry", "今日新闻", "每日新闻", "ECONNREFUSED", "redirect"]',
        "content": (
            "WebFetch 一个源失败一次即标记为不可用，当日不再重试。"
            "3 个不同域名连续失败 → 判定全局拦截 → 跳过所有后续 WebFetch → 降级到 API 聚合源。"
            "不要反复重试已知失败的域名（如 ce.cn 重定向循环、Reuters/BBC ECONNREFUSED）。"
        ),
        "fragment_type": "failure_pattern",
        "skill_name": "harness_env-fix_news-workflow",
        "confidence": 0.90,
    },
    {
        "tag": "mcp-tools-require-session-restart",
        "trigger_phrases": '["MCP", "session", "restart", "tools", "world-news-api"]',
        "content": (
            "MCP server 在 .mcp.json 中的修改（新增/修改/修复）需要重启 Claude Code session 才能生效。"
            "当前 session 的工具列表在启动时快照，运行中不会热加载新的 MCP 工具。"
            "修复 MCP 配置后，告知用户需要新开 session 或重启。"
        ),
        "fragment_type": "failure_pattern",
        "skill_name": "harness_env-fix_news-workflow",
        "confidence": 0.85,
    },
    {
        "tag": "news-workflow-trigger-fuzzy-match",
        "trigger_phrases": '["今日新闻", "每日新闻", "早上新闻", "新闻简报", "今天有什么新闻"]',
        "content": (
            "用户输入的自然语言变体（'今日新闻'/'早上新闻'/'今天有什么新闻'）"
            "不会匹配精确的 hook matcher '每日新闻'。"
            "意图匹配器 (intent_matcher.py) 已通过特征词加权评分解决此问题。"
        ),
        "fragment_type": "failure_pattern",
        "skill_name": "harness_env-fix_news-workflow",
        "confidence": 0.80,
    },
    {
        "tag": "tool-success-but-data-garbage",
        "trigger_phrases": '["WebFetch", "WebSearch", "HTTP 200", "redirect", "empty response", "HTML wrapper"]',
        "content": (
            "工具调用可能返回 HTTP 200 但数据是垃圾：空返回、HTML 包装、重定向循环、验证码墙。"
            "Observer v2 已增加 _detect_data_quality_failures() 检测此类静默失败。"
            "新闻采集时，一个源返回空/垃圾 → 标记不可用 → 当日不再重试 → 切换到替代源。"
        ),
        "fragment_type": "failure_pattern",
        "skill_name": "harness_env-fix_news-workflow",
        "confidence": 0.90,
    },
    {
        "tag": "observer-blind-to-silent-failures",
        "trigger_phrases": '["observer", "tool_result", "200 OK", "garbage data", "redirect loop"]',
        "content": (
            "Observer v1 _detect_tool_failures() 只检测显式错误关键词，无法检测 HTTP 200 但返回垃圾的工具调用。"
            "v2 新增 _detect_data_quality_failures() — 检测空返回、HTML 包装、重定向、验证码。"
            "数据质量失败计入 _compute_confidence() 评分（+0.15 bonus），提升此类 session 的提取优先级。"
        ),
        "fragment_type": "failure_pattern",
        "skill_name": "harness:observer",
        "confidence": 0.85,
    },
    {
        "tag": "preflight-only-as-text",
        "trigger_phrases": '["preflight", "failure pattern", "injector", "已知失败模式"]',
        "content": (
            "Harness 之前的 preflight 仅为文本注入（'⚠️ 已知失败模式'段），依赖 Claude 读到并主动行动。"
            "v2 injector 已升级为可执行 preflight 检查：skipWebFetchPreflight、MCP wrapper 状态、"
            "工具失败率、过期待审查技能、配置矛盾。Pass 静默，Fail 单行标记。优先级 0.95（最高）。"
            "不再依赖 Claude 的记忆——系统在 session start 时自动拦截。"
        ),
        "fragment_type": "failure_pattern",
        "skill_name": "harness:observer",
        "confidence": 0.90,
    },
]


def seed():
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL;")

    inserted = 0
    for f in SEED_FAILURES:
        # Check if this tag already exists (dedup by tag)
        existing = conn.execute(
            "SELECT id FROM fragments WHERE tag = ?", (f["tag"],)
        ).fetchone()
        if existing:
            print(f"  [skip] {f['tag']} — already exists")
            continue
        conn.execute(
            """INSERT INTO fragments
               (tag, trigger_phrases, content, confidence, fragment_type, skill_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                f["tag"],
                f["trigger_phrases"],
                f["content"],
                f["confidence"],
                f["fragment_type"],
                f["skill_name"],
                time.time(),
            ),
        )
        print(f"  [ok] {f['tag']}")
        inserted += 1

    conn.commit()
    conn.close()
    print(f"\nSeeded {inserted} failure patterns (skipped {len(SEED_FAILURES) - inserted} existing).")


if __name__ == "__main__":
    seed()
