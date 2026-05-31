"""Preprocess daily news — condense 50+ raw snippets into a structured brief.

Reads ranked snippets from state.db (via snippet_scorer), groups by template
category, extracts entities and cross-source signals, and outputs a compact
structured markdown brief. Claude reads this brief instead of raw snippets.

Usage:
    python -m harness.news_agent --step preprocess --date 2026-05-25
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from . import HARNESS_DIR, DAILY_BRIEF

# —— Category classification keywords ——————————————————————————————————————

_MARKETING_KW = [
    "营销", "广告", "品牌", "文案", "电商", "投放", "流量", "增长",
    "social media", "KOL", "内容生成", "创意", "智算", "办公", "协作",
    "企业服务", "SaaS", "B2B", "内容创作",
]

_TECH_KW = [
    "LLM", "GPT", "大模型", "模型", "Agent", "智能体", "芯片", "NVIDIA",
    "GPU", "机器人", "具身", "自动驾驶", "开源", "训练", "推理",
    "embedding", "transformer", "RLHF", "fine-tun",
]

_GOVERNANCE_KW = [
    "制裁", "出口管制", "chip ban", "贸易战", "关税", "AI治理",
    "AI安全", "对齐", "alignment", "监管", "白宫", "国会", "EU AI",
]

_GEO_KW = [
    "地缘", "军事", "冲突", "供应链", "能源", "通胀", "央行", "GDP",
    "geopolit", "war", "conflict", "NATO", "南海", "台海",
]

_POLICY_KW = [
    "政策", "法律", "法规", "合规", "数据法", "隐私", "知识产权",
    "版权", "专利", "GDPR", "数字法", "网络安全", "个人信息",
]

_LIVELIHOOD_KW = [
    "就业", "教育", "消费", "物价", "收入", "基层", "社区",
    "乡村", "县域", "招聘", "灵活用工", "学区", "零售", "养老",
]

_TAG_MAP = {
    "AI营销": "AI广告/内容",
    "Agent/工具": "Agent",
    "社会心理": "社会心理",
    "脑机接口": "BCI",
    "具身智能": "具身",
    "未来预测": "趋势",
}


def _get_date(date_str=None):
    return date_str or date.today().isoformat()


def _classify(headline, section, entities, summary):
    text = f"{headline} {summary} {' '.join(entities)}".lower()
    if section == "academic-high-impact":
        return "arXiv+学术"
    if section == "academic-search":
        return "AI/科技"
    if section == "livelihood":
        return "民生观察"
    for kw in _MARKETING_KW:
        if kw.lower() in text:
            return "AI营销"
    for kw in _GOVERNANCE_KW:
        if kw.lower() in text:
            return "中美/AI治理"
    for kw in _GEO_KW:
        if kw.lower() in text:
            return "地缘/宏观"
    for kw in _POLICY_KW:
        if kw.lower() in text:
            return "政策/法律"
    for kw in _LIVELIHOOD_KW:
        if kw.lower() in text:
            return "民生观察"
    for kw in _TECH_KW:
        if kw.lower() in text:
            return "AI/科技"
    return "AI/科技"


def _secondary_classify(headline, section, entities, summary, primary):
    text = f"{headline} {summary} {' '.join(entities)}".lower()
    checks = [
        ("AI营销", _MARKETING_KW),
        ("中美/AI治理", _GOVERNANCE_KW),
        ("地缘/宏观", _GEO_KW),
        ("政策/法律", _POLICY_KW),
        ("民生观察", _LIVELIHOOD_KW),
        ("AI/科技", _TECH_KW),
    ]
    best = None
    best_hits = 0
    for cat, kws in checks:
        if cat == primary:
            continue
        hits = sum(1 for kw in kws if kw.lower() in text)
        if hits > best_hits and hits >= 2:
            best_hits = hits
            best = cat
    return best


def _find_contradictions(ranked):
    positive_kw = ["缓和", "合作", "稳定", "突破", "增长", "开放", "和平", "协议",
                   "缓和", "de-escalat", "cooperation", "peace", "deal", "agreement",
                   "progress", "breakthrough"]
    negative_kw = ["冲突", "危机", "抵制", "制裁", "衰退", "对抗", "不持久", "风险",
                   "威胁", "失败", "conflict", "crisis", "sanction", "tension",
                   "war", "threat", "collapse", "resist"]

    pairs = []
    for i, a in enumerate(ranked):
        for b in ranked[i+1:]:
            a_ents = set(a.get("entities", []))
            b_ents = set(b.get("entities", []))
            if not (a_ents & b_ents):
                continue
            a_text = f"{a.get('headline','')} {a.get('summary','')}".lower()
            b_text = f"{b.get('headline','')} {b.get('summary','')}".lower()
            a_pos = any(kw.lower() in a_text for kw in positive_kw)
            a_neg = any(kw.lower() in a_text for kw in negative_kw)
            b_pos = any(kw.lower() in b_text for kw in positive_kw)
            b_neg = any(kw.lower() in b_text for kw in negative_kw)
            if (a_pos and not a_neg and b_neg and not b_pos) or \
               (a_neg and not a_pos and b_pos and not b_neg):
                shared = a_ents & b_ents
                pairs.append({
                    "entity": ", ".join(list(shared)[:3]),
                    "positive": a.get("headline", "")[:60] if a_pos else b.get("headline", "")[:60],
                    "negative": a.get("headline", "")[:60] if a_neg else b.get("headline", "")[:60],
                })
    return pairs[:5]


def _domain_tag(matched):
    tags = [_TAG_MAP.get(d, d) for d in matched]
    return "/".join(tags[:2]) if tags else "综合"


def _why_hint(headline, summary, matched):
    hints = {
        "AI营销": "AI内容生产工具链变化",
        "Agent/工具": "Agent能力边界变化",
        "社会心理": "社会行为模式信号",
        "脑机接口": "脑机接口新进展",
        "具身智能": "物理世界AI突破",
        "未来预测": "长期趋势信号",
    }
    for d in matched:
        if d in hints:
            return hints[d]
    if summary:
        return summary[:25]
    return "新技术/产业动态"


def _extract_url(snippet):
    sources = snippet.get("sources", [])
    if sources and isinstance(sources, list) and len(sources) > 0:
        src = sources[0]
        return src.get("url", "") if isinstance(src, dict) else str(src)
    return ""


def generate_brief(date_str=None, top_n=30):
    """Generate structured brief from daily news snippets."""
    from ..indexer import HarnessDB

    today = _get_date(date_str)
    db = HarnessDB()

    # Fetch arXiv papers before scoring (idempotent via content_hash dedup)
    try:
        from .arxiv import fetch_arxiv
        n_arxiv = fetch_arxiv(date_str=today, max_results=30, top_n=10)
        if n_arxiv:
            print(f"[preprocess] arXiv: {n_arxiv} papers added", file=sys.stderr)
    except Exception as e:
        print(f"[preprocess] arXiv fetch skipped: {e}", file=sys.stderr)

    # Load ranked snippets (scored by snippet_scorer)
    try:
        from ..snippet_scorer import score_snippets
        ranked = score_snippets(today, db, top_n=top_n)
    except Exception:
        raw = db.get_news_snippets(date=today)
        ranked = [
            {"id": s.get("id"), "date": s.get("date"),
             "section": s.get("section"), "headline": s.get("headline", ""),
             "summary": (s.get("summary") or "")[:120],
             "sources": s.get("sources", []),
             "source_rating": s.get("source_rating", ""),
             "total_score": 0, "matched_domains": []}
            for s in raw
        ][:top_n]

    raw_snips = {s["id"]: s for s in db.get_news_snippets(date=today)}

    arxiv_ids = {s.get("id") for s in ranked}
    arxiv_papers = [
        s for s in raw_snips.values()
        if s.get("section") == "academic-high-impact" and s.get("id") not in arxiv_ids
    ]

    db.close()

    if not ranked and not arxiv_papers:
        print(f"[preprocess] No snippets for {today}", file=sys.stderr)
        return None

    for r in ranked:
        full = raw_snips.get(r.get("id"), {})
        r["entities"] = full.get("entities", [])
    if arxiv_papers:
        for s in arxiv_papers:
            ranked.append({
                "id": s.get("id"), "date": s.get("date"),
                "section": "academic-high-impact",
                "headline": s.get("headline", ""),
                "summary": (s.get("summary") or "")[:120],
                "sources": s.get("sources", []),
                "source_rating": s.get("source_rating", "arxiv"),
                "entities": s.get("entities", []),
                "total_score": -1,
                "matched_domains": ["学术"],
                "cross_source_score": 0,
            })

    tables = {
        "AI/科技": [], "AI营销": [], "中美/AI治理": [],
        "地缘/宏观": [], "政策/法律": [], "arXiv+学术": [], "民生观察": [],
    }
    for s in ranked:
        headline = s.get("headline", "")
        section = s.get("section", "")
        entities = s.get("entities", [])
        summary = s.get("summary", "")
        cat = _classify(headline, section, entities, summary)
        if cat == "arXiv+学术":
            url = _extract_url(s)
            if url and "arxiv.org" not in url:
                cat = _classify(headline, "academic-search", entities, summary)
        secondary = _secondary_classify(headline, section, entities, summary, cat)
        s["_secondary_cat"] = secondary
        tables[cat].append(s)

    candidates = sorted(ranked, key=lambda x: x.get("total_score", 0), reverse=True)
    analysis = []
    for s in candidates[:5]:
        analysis.append({
            "headline": s.get("headline", "").replace("**", "").strip(),
            "score": s.get("total_score", 0),
            "domains": s.get("matched_domains", []),
            "summary": (s.get("summary") or "")[:200],
            "url": _extract_url(s),
        })

    cross = []
    for s in ranked:
        if s.get("cross_source_score", 0) >= 1.5:
            url = _extract_url(s)
            cross.append({
                "headline": s.get("headline", "").replace("**", "").strip(),
                "cross_score": s["cross_source_score"],
                "url": url,
            })

    all_ents = []
    for s in ranked:
        all_ents.extend(s.get("entities", []))
    freq = Counter(all_ents)
    rare = [(e, c) for e, c in freq.most_common(30) if c <= 3]
    common = [(e, c) for e, c in freq.most_common(10) if c > 3]

    liv_items = tables.get("民生观察", [])
    liv_topics = {
        "就业": ["就业", "招聘", "灵活用工", "劳动", "失业", "岗位"],
        "教育": ["教育", "学区", "职业", "双减", "高等", "学校"],
        "消费": ["消费", "物价", "零售", "收入", "购买", "价格"],
        "基层治理": ["基层", "社区", "乡村", "县域", "治理", "街道"],
    }
    livelihood = {}
    for topic, kws in liv_topics.items():
        matched = [s for s in liv_items if any(kw in s.get("headline", "") for kw in kws)]
        livelihood[topic] = [
            {"headline": s["headline"][:60], "url": _extract_url(s)}
            for s in matched[:3]
        ]

    d = date.fromisoformat(today)
    wdays = ["一", "二", "三", "四", "五", "六", "日"]
    date_disp = f"{d.year}年{d.month}月{d.day}日 星期{wdays[d.weekday()]}"

    lines = [
        f"# 每日新闻精要 — {date_disp}",
        "",
        f"> {len(ranked)} 条排序精要，已按模板分类预填。",
        "",
        "---",
        "",
        "## 📊 速览表",
        "",
    ]

    for cat in ["AI/科技", "AI营销", "中美/AI治理", "地缘/宏观", "政策/法律", "arXiv+学术"]:
        items = tables.get(cat, [])
        if not items:
            lines.append(f"### {cat}（0条）")
            lines.append("")
            if cat == "AI营销":
                lines.append("> ⚠️ 今日无直接信号 — 此段不可省略，请在日报中保留空表。")
            lines.append("")
            continue

        lines.append(f"### {cat}（{len(items)}条）")
        lines.append("")
        for i, item in enumerate(items[:8]):
            url = _extract_url(item)
            tag = _domain_tag(item.get("matched_domains", []))
            why = _why_hint(item.get("headline", ""), item.get("summary", ""),
                            item.get("matched_domains", []))
            h = item.get("headline", "").replace("**", "").strip()[:60]
            sec = f" ↳ {item['_secondary_cat']}" if item.get("_secondary_cat") else ""
            lines.append(f"{i+1}. **{h}** `{tag}` — {why}{sec} [→]({url})")
        lines.append("")

    lines.append("### 民生观察")
    lines.append("")
    for topic in ["就业", "教育", "消费", "基层治理"]:
        items = livelihood.get(topic, [])
        if items:
            item_strs = [f"**{it['headline']}** [→]({it['url']})" for it in items]
            lines.append(f"- **{topic}**: " + "; ".join(item_strs))
        else:
            lines.append(f"- **{topic}**: 无显著信号")
    lines.append("")

    lines.extend([
        "---",
        "",
        "## 🏷️ 实体信号",
        "",
    ])
    if rare:
        lines.append(f"**罕见实体**（≤3次，潜在新信号）: {', '.join(e for e, c in rare[:15])}")
    if common:
        lines.append(f"**高频实体**: {', '.join(f'{e}({c})' for e, c in common[:8])}")
    lines.append("")

    lines.extend([
        "---",
        "",
        "## 🔥 重点分析候选",
        "",
    ])
    for i, c in enumerate(analysis):
        domains = ", ".join(c["domains"]) if c["domains"] else "综合"
        lines.append(f"**{i+1}. {c['headline'][:80]}** ({domains}, 评分{c['score']})")
        if c["summary"]:
            lines.append(f"> {c['summary'][:150]}")
        if c["url"]:
            lines.append(f"> [→]({c['url']})")
        lines.append("")

    cross_dedup = {c["headline"][:60]: c for c in cross}
    if cross_dedup:
        lines.extend([
            "---",
            "",
            "## 🔗 多源交叉验证",
            "",
        ])
        for h, c in list(cross_dedup.items())[:5]:
            lines.append(f"- **{h}** (交叉分{c['cross_score']}) [→]({c['url']})")
        lines.append("")

    contradictions = _find_contradictions(ranked)
    if contradictions:
        lines.extend([
            "---",
            "",
            "## ⚠️ 矛盾信号（保留张力，勿平滑）",
            "",
        ])
        for c in contradictions:
            lines.append(f"- **矛盾实体**: {c['entity']}")
            lines.append(f"  - 📈 {c['positive'][:70]}")
            lines.append(f"  - 📉 {c['negative'][:70]}")
        lines.append("")

    lines.extend([
        "---",
        "",
        "## 📋 待完成",
        "",
        "基于以上精要，生成完整日报（`news/{date_str}.md`）：",
        "",
        "1. **今日判断** ≤100字",
        "2. **重点分析** 2-3篇 — 从候选列表选，写事实链→矛盾→因果链→对菲菲",
        "3. **因果追踪** — 假设验证+新假设+矛盾对，每条≥2锚点",
        "4. **Prophet信号**",
        "5. **今日反馈**",
        "6. **写入日报** → Obsidian vault `news/YYYY-MM-DD.md`",
        "",
    ])

    brief = "\n".join(lines)
    DAILY_BRIEF.write_text(brief, encoding="utf-8")
    print(f"[preprocess] {DAILY_BRIEF} ({len(brief)} chars, {len(ranked)} snippets)", file=sys.stderr)
    print(brief)

    return brief


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Preprocess daily news snippets")
    parser.add_argument("--date", help="Date YYYY-MM-DD")
    parser.add_argument("--top", type=int, default=30)
    args = parser.parse_args()
    generate_brief(date_str=args.date, top_n=args.top)
