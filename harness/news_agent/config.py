"""News agent shared configuration — single source of truth for keywords, weights,
and matcher patterns used by both intent_matcher.py and settings.local.json.

Previously these were hardcoded in two separate places and had drifted out of sync.
"""

from __future__ import annotations

# ── Intent matching keywords ──────────────────────────────────────────
# Used by intent_matcher.py for weighted scoring.
# Keys with non-ASCII are matched by substring containment.

NEWS_INTENT_KEYWORDS: dict[str, int] = {
    # Core triggers (high confidence)
    "新闻": 10, "日报": 10, "简报": 8, "快讯": 8,
    "热点": 7, "动态": 5, "头条": 8, "资讯": 6,
    # Query patterns
    "发生了什么": 7, "有什么": 4, "看看": 3,
    "今天": 5, "今日": 8, "最近": 3, "早上": 5,
    "昨晚": 5, "本周": 4,
    # Reverse triggers
    "大事": 6, "新鲜事": 7, "新消息": 7,
}

NEWS_DOMAIN_KEYWORDS: dict[str, dict] = {
    "科技":     {"weight": 6, "domains": ["tech", "ai"]},
    "AI":       {"weight": 8, "domains": ["ai", "llm"]},
    "人工智能": {"weight": 8, "domains": ["ai", "llm"]},
    "芯片":     {"weight": 6, "domains": ["semiconductor", "chips"]},
    "半导体":   {"weight": 5, "domains": ["semiconductor"]},
    "机器人":   {"weight": 5, "domains": ["robotics", "embodied-ai"]},
    "具身智能": {"weight": 7, "domains": ["embodied-ai"]},
    "财经":     {"weight": 4, "domains": ["finance", "economy"]},
    "经济":     {"weight": 5, "domains": ["economy"]},
    "国际":     {"weight": 4, "domains": ["geopolitics", "world"]},
    "地缘":     {"weight": 5, "domains": ["geopolitics"]},
    "政策":     {"weight": 4, "domains": ["policy", "regulation"]},
    "法律":     {"weight": 3, "domains": ["law", "regulation"]},
    "学术":     {"weight": 5, "domains": ["academic", "arxiv"]},
    "论文":     {"weight": 5, "domains": ["academic", "arxiv"]},
    "互联网":   {"weight": 4, "domains": ["tech", "internet"]},
    "自动驾驶": {"weight": 5, "domains": ["autonomous-driving"]},
    "脑机":     {"weight": 7, "domains": ["bci"]},
    "BCI":      {"weight": 7, "domains": ["bci"]},
    "量子":     {"weight": 5, "domains": ["quantum"]},
    "新能源":   {"weight": 3, "domains": ["energy"]},
    "生物科技": {"weight": 3, "domains": ["biotech"]},
    "太空":     {"weight": 3, "domains": ["space"]},
    "创业":     {"weight": 3, "domains": ["startups"]},
    "投资":     {"weight": 3, "domains": ["investment"]},
}

NEWS_MIN_SCORE: int = 10

# ── Domain-specific source priority ───────────────────────────────────
# Used by inject_workflow_context() to suggest data sources per domain.

NEWS_SOURCE_PRIORITY: dict[str, list[str]] = {
    "tech":         ["arXiv", "36kr", "TechNode"],
    "ai":           ["arXiv", "36kr", "TechNode"],
    "semiconductor": ["财联社", "TechNode"],
    "geopolitics":  ["人民网", "中国政府网"],
    "finance":      ["财联社", "经济日报"],
    "policy":       ["中国政府网", "人民网"],
    "default":      ["World News API", "arXiv", "36kr", "财联社", "人民网"],
}

# ── Domain-specific instruction branches ──────────────────────────────
# Keyed by domain tag; first match wins (checked in list order).

NEWS_DOMAIN_INSTRUCTIONS: list[tuple[list[str], str]] = [
    (["semiconductor", "chips"],
     "**领域指令：** 优先财联社电报+TechNode 半导体源。关注芯片出口管制、产能、设备国产化。"),
    (["tech", "ai"],
     "**领域指令：** 优先抓取 arXiv 论文、36氪科技快讯、TechNode 英文源。\n"
     "论文解读侧重底层因果机制（B-人类行为/A-世界规律/具身智能垂直应用）。"),
    (["geopolitics"],
     "**领域指令：** 优先人民网/中国政府网政策源。侧重中美关系+地缘经济影响链。"),
    (["finance", "economy"],
     "**领域指令：** 优先财联社电报+经济日报。侧重AI产业传导和宏观经济数据。"),
    (["policy"],
     "**领域指令：** 优先中国政府网+人民网政策源。侧重AI监管+数据隐私+科技政策。"),
    (["bci"],
     "**领域指令：** 优先 arXiv BCI/神经科学论文 + 光明网科技版。侧重脑机接口+神经信号处理。"),
]

NEWS_DOMAIN_DEFAULT_INSTRUCTION = "**领域指令：** 全领域覆盖，标准日报模板。"

# ── Workflow identity ─────────────────────────────────────────────────

NEWS_SKILL_NAME = "harness_news-agent"
NEWS_TEMPLATE_PATH = (
    r"C:\Users\Chucky\Documents\Obsidian Vault\claude专属文件夹\news\news-template.md"
)


def build_matcher_regex() -> str:
    """Generate a regex alternation from all intent + domain keywords.

    Used by settings.local.json's UserPromptSubmit hook so the matcher
    pattern never drifts out of sync with intent_matcher.py.

    Returns a regex string like:
        (?:新闻|日报|简报|...|科技|AI|人工智能|...)
    """
    import re
    all_keys = list(NEWS_INTENT_KEYWORDS) + list(NEWS_DOMAIN_KEYWORDS)
    # Sort by length descending so longer patterns match first (not strictly
    # necessary for alternation but makes the generated regex more readable)
    unique = sorted(set(all_keys), key=lambda k: -len(k))
    # Escape regex-special chars, sort for determinism
    escaped = [re.escape(k) for k in unique]
    return "(?:" + "|".join(escaped) + ")"


def build_intents_registry() -> dict:
    """Build the INTENTS dict in the format intent_matcher.py expects.

    This is the programmatic equivalent of the old hardcoded INTENTS dict.
    intent_matcher.py calls this to get its keyword registry.
    """
    return {
        "news": {
            "keywords": dict(NEWS_INTENT_KEYWORDS),
            "domain_keywords": dict(NEWS_DOMAIN_KEYWORDS),
            "min_score": NEWS_MIN_SCORE,
            "skill": NEWS_SKILL_NAME,
            "workflow_params": {
                "source_priority": dict(NEWS_SOURCE_PRIORITY),
            },
        },
    }
