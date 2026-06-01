"""Intent Matcher — lightweight weighted feature-word scoring for user message routing.

Design:
- Zero LLM dependency. Sub-millisecond scoring.
- Keywords and domain tags are sourced from duonews.config (single source of truth).
- Score = sum(matched keyword weights). Trigger if >= min_score.
- inject_workflow_context() now returns a lightweight pointer to the skill file
  instead of hardcoded workflow instructions.

Usage:
    from intent_matcher import match_intent
    result = match_intent("今天有什么科技新闻")
    # => {"intent": "news", "score": 28, "domains": ["tech", "ai"], ...}
"""

from __future__ import annotations
import json
from pathlib import Path

# ── Intent pattern registry — sourced from duonews.config ────────────

def _load_intents():
    """Lazy-load intents from shared config so import never fails at module level."""
    try:
        from duonews.config import build_intents_registry
        return build_intents_registry()
    except Exception:
        # Fallback: minimal built-in registry (cold start / import error)
        return {
            "news": {
                "keywords": {"新闻": 10, "日报": 10, "简报": 8},
                "domain_keywords": {"AI": {"weight": 8, "domains": ["ai"]}},
                "min_score": 10,
                "skill": "duonews",
                "workflow_params": {"source_priority": {}},
            },
        }

INTENTS = _load_intents()


def match_intent(message: str) -> dict | None:
    """Match user message against all registered intents.

    Returns the best-matching intent with score and extracted domains,
    or None if no intent reaches its min_score threshold.
    """
    if not message or not message.strip():
        return None

    best = None
    best_score = 0

    for intent_name, intent_def in INTENTS.items():
        score = 0
        domain_tags: set[str] = set()

        for keyword, weight in intent_def.get("keywords", {}).items():
            if keyword in message:
                score += weight

        for dk_word, dk_def in intent_def.get("domain_keywords", {}).items():
            if dk_word in message:
                score += dk_def["weight"]
                domain_tags.update(dk_def["domains"])

        if score >= intent_def["min_score"]:
            if score > best_score:
                domains = sorted(domain_tags) if domain_tags else ["general"]
                best = {
                    "intent": intent_name,
                    "score": score,
                    "domains": domains,
                    "skill": intent_def["skill"],
                    "workflow_params": intent_def.get("workflow_params", {}),
                }
                best_score = score

    return best


def match_intent_with_detail(message: str) -> dict | None:
    """Like match_intent but includes detailed scoring for debugging."""
    result = match_intent(message)
    if result is None:
        return None

    result["message"] = message[:200]
    result["all_scores"] = _score_all_intents(message)
    return result


def _score_all_intents(message: str) -> dict:
    """Score all intents for debugging."""
    scores = {}
    for intent_name, intent_def in INTENTS.items():
        score = 0
        for keyword, weight in intent_def.get("keywords", {}).items():
            if keyword in message:
                score += weight
        for dk_word, dk_def in intent_def.get("domain_keywords", {}).items():
            if dk_word in message:
                score += dk_def["weight"]
        scores[intent_name] = score
    return scores


def _query_news_preferences(db_path: str = "D:/Claude/harness/state.db",
                              days: int = 30) -> dict | None:
    """Query feature_activations + news_snippets for top anomaly types and domains.

    Returns None if no data exists (first run / cold start).
    """
    import sqlite3, json as _json
    try:
        conn = sqlite3.connect(db_path, timeout=2)
        cur = conn.execute(
            """SELECT fa.feature_id, fle.name_cn, COUNT(*) as cnt,
                      AVG(fa.activation_strength) as avg_str
               FROM feature_activations fa
               LEFT JOIN feature_library_entries fle ON fa.feature_id = fle.feature_id
               WHERE fa.date >= date('now', ? || ' days')
               GROUP BY fa.feature_id
               ORDER BY cnt * avg_str DESC
               LIMIT 8""",
            (f"-{days}",),
        )
        top_features = [(r[0], r[1] or r[0], r[2], r[3]) for r in cur.fetchall()]
        if not top_features:
            conn.close()
            return None

        cur = conn.execute(
            """SELECT entities FROM news_snippets
               WHERE date >= date('now', ? || ' days') AND entities IS NOT NULL""",
            (f"-{days}",),
        )
        entity_counter = {}
        for (entities_json,) in cur.fetchall():
            try:
                for e in _json.loads(entities_json) if entities_json else []:
                    if len(e) >= 2:
                        entity_counter[e] = entity_counter.get(e, 0) + 1
            except Exception:
                pass
        top_entities = sorted(entity_counter.items(), key=lambda x: -x[1])[:6]

        import time as _time
        now = _time.time()
        fb_weights: dict[str, float] = {}
        try:
            cur = conn.execute(
                "SELECT entity, weight, last_updated FROM entity_feedback_weights"
            )
            for entity, weight, last_updated in cur.fetchall():
                if weight != 0.0 and last_updated:
                    d = (now - last_updated) / 86400.0
                    weight *= 0.95 ** d
                fb_weights[entity.lower()] = weight
        except Exception:
            pass
        conn.close()

        domain_keywords = {
            "AI Agent生态": ["Agent", "AI", "LLM", "GPT", "模型", "智能体", "Agentic"],
            "芯片供应链": ["芯片", "NVIDIA", "TSMC", "半导体", "GPU", "HBM", "CoWoS", "出口管制"],
            "arXiv因果论文": ["arXiv", "机制", "因果", "对齐", "安全"],
            "地缘博弈": ["中美", "制裁", "关税", "脱钩", "俄罗斯", "伊朗"],
            "AI治理": ["政策", "监管", "合规", "隐私", "版权"],
            "市场金融": ["股市", "IPO", "融资", "财报", "营收", "估值"],
        }
        domain_scores = {}
        for ent, cnt in entity_counter.items():
            fb_weight = fb_weights.get(ent.lower(), 0.0)
            adjusted_cnt = cnt * (1.0 + fb_weight)
            for domain, keywords in domain_keywords.items():
                if any(kw in ent for kw in keywords):
                    domain_scores[domain] = domain_scores.get(domain, 0) + adjusted_cnt

        top_domains = [d for d, _ in sorted(domain_scores.items(), key=lambda x: -x[1])[:4]]
        if not top_domains:
            top_domains = ["AI Agent生态", "芯片供应链"]

        return {
            "top_features": top_features[:5],
            "top_domains": top_domains,
        }
    except Exception:
        return None


def _format_preference_context(prefs: dict | None) -> str:
    """Format preference data into a compact injection block (~150 tokens)."""
    if not prefs:
        return ""
    lines = ["📊 菲菲认知偏好档案（30天统计）："]
    feat_str = ", ".join(
        f"{fid} {name}({cnt})" for fid, name, cnt, _ in prefs["top_features"][:5]
    )
    lines.append(f"  高关注特征：{feat_str}")
    lines.append(f"  高关注领域：{', '.join(prefs['top_domains'])}")
    lines.append("  → 今日日报自动偏重上述方向和对应的底层异常类型")
    return "\n".join(lines)


def _pick_domain_instruction(domains: list[str]) -> str:
    """Pick the best domain instruction from shared config."""
    try:
        from duonews.config import NEWS_DOMAIN_INSTRUCTIONS, NEWS_DOMAIN_DEFAULT_INSTRUCTION
    except Exception:
        return "**领域指令：** 全领域覆盖，标准日报模板。"

    domain_set = set(domains)
    for domain_tags, instruction in NEWS_DOMAIN_INSTRUCTIONS:
        if any(t in domain_set for t in domain_tags):
            return instruction
    return NEWS_DOMAIN_DEFAULT_INSTRUCTION


def inject_workflow_context(intent_result: dict, source_health: dict | None = None,
                              db_path: str = "D:/Claude/harness/state.db") -> str:
    """Generate the context injection string for a matched intent.

    Returns a compact (5-8 line) block that points to the skill file
    and the unified duonews package — no hardcoded workflow steps.
    """
    if not intent_result:
        return ""

    lines = []
    intent = intent_result["intent"]
    domains = intent_result.get("domains", ["general"])
    params = intent_result.get("workflow_params", {})

    if intent == "news":
        lines.append("## 意图匹配：新闻工作流")
        lines.append(f"检测到新闻请求。关注领域：{'、'.join(domains)}")

        # Source priority
        source_priority = params.get("source_priority", {})
        priority = source_priority.get("default", [])
        for d in domains:
            if d in source_priority and d not in ("default",):
                priority = source_priority[d]
                break
        if priority:
            lines.append(f"**优先数据源：** {' → '.join(priority)}")

        # Source health from preflight probes
        if source_health:
            dead = {l for l, (p, d) in source_health.items() if not p}
            degraded = {l for l, (p, d) in source_health.items() if p and d != "HTTPS OK"}
            if dead:
                lines.append(f"**⚠️ 不可用源：** {', '.join(sorted(dead))}")
            if degraded:
                lines.append(f"**⚠️ 降级源：** {', '.join(sorted(degraded))}")

        # Personalized preference context from DB
        prefs = _query_news_preferences(db_path)
        pref_context = _format_preference_context(prefs)
        if pref_context:
            lines.append("")
            lines.append(pref_context)

        # Domain instruction (from shared config, not hardcoded)
        domain_instruction = _pick_domain_instruction(domains)
        lines.append("")
        lines.append(domain_instruction)

        # Judgment graph context
        try:
            from judgment_graph import query_relevant_judgments, format_judgment_injection
            from datetime import datetime as _dt
            today = _dt.now().strftime("%Y-%m-%d")
            judgments = query_relevant_judgments(db_path, today, [], None)
            judgment_context = format_judgment_injection(judgments)
            if judgment_context:
                lines.append("")
                lines.append(judgment_context)
        except ImportError:
            pass

        # Pointer to skill + unified entry point (replaces old 60-line hardcoded workflow)
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("**执行指令：** 使用 `Skill` 工具调用 `duonews` 技能。")
        lines.append("统一入口：`python -m duonews --step <name> --date <date>`")
        lines.append("可用步骤：github | search | arxiv | preprocess | cross_day | push | vectorize | feedback | diagnose | all")
        lines.append("**格式权威源：** `news-template.md` (v3.3)")

    return "\n".join(lines)


# ── CLI entry point (for Claude Code hook integration) ──

def main():
    import sys
    if len(sys.argv) < 3:
        return

    action = sys.argv[1]
    message = " ".join(sys.argv[2:])

    if action == "match":
        result = match_intent(message)
        if result:
            print(inject_workflow_context(result))
    elif action == "debug":
        result = match_intent_with_detail(message)
        if result:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("No intent matched.")


if __name__ == "__main__":
    main()
