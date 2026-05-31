"""Report Writer — generate complete 7-section daily report from pipeline outputs.

Reads .daily_brief.md (preprocess output), cross_day_discoveries (state.db),
and historical judgment baseline, then generates a complete news report
conforming to news-template.md v3.3 and writes it to the Obsidian vault.

Usage:
    python -m duonews --step report_write --date 2026-05-31
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

from . import DUONEWS_DIR, DAILY_BRIEF, OBSIDIAN_NEWS


def _get_date(date_str: str | None = None) -> str:
    return date_str or date.today().isoformat()


def _parse_brief_sections(brief_text: str) -> dict[str, str]:
    """Parse .daily_brief.md into named sections.

    Returns a dict mapping section header → section body text.
    """
    sections: dict[str, str] = {}
    current_header = "_preamble"
    current_body: list[str] = []

    for line in brief_text.split("\n"):
        if line.startswith("## ") or line.startswith("# "):
            if current_body:
                sections[current_header] = "\n".join(current_body).strip()
            current_header = line.strip()
            current_body = []
        else:
            current_body.append(line)

    if current_body:
        sections[current_header] = "\n".join(current_body).strip()

    return sections


def _read_cross_day_data(date_str: str) -> list[dict]:
    """Load cross-day discoveries from state.db for the given date."""
    try:
        from harness.indexer import HarnessDB
        db = HarnessDB()
        discoveries = db.get_cross_day_discoveries(run_date=date_str, limit=20)
        db.close()
        return discoveries
    except Exception as e:
        print(f"[report_writer] cross_day data unavailable: {e}", file=sys.stderr)
        return []


def _read_judgment_baseline(date_str: str) -> dict:
    """Load historical judgment baseline by finding the most recent prior report."""
    from . import find_recent_report, extract_judgment_baseline
    try:
        prior = find_recent_report(date_str, max_lookback=7)
        if prior:
            return extract_judgment_baseline(prior)
    except Exception as e:
        print(f"[report_writer] judgment baseline unavailable: {e}", file=sys.stderr)
    return {}


def _generate_section_overview(brief_sections: dict, ranked_count: int) -> str:
    """Generate the 新闻总览表 section with markdown tables."""
    lines = ["## 📊 一、今日速览", ""]

    cat_map = [
        ("AI/科技", "AI/科技"),
        ("AI营销", "AI营销"),
        ("中美/AI治理", "中美/AI治理"),
        ("地缘/宏观", "地缘/宏观"),
        ("政策/法律", "政策/法律"),
        ("arXiv+学术", "arXiv+学术"),
    ]

    for cat_key, cat_name in cat_map:
        # Match section headers with optional item counts: "### AI/科技（5条）" or "### AI/科技"
        body = ""
        for section_header, section_body in brief_sections.items():
            if cat_key in section_header:
                body = section_body
                break

        lines.append(f"### {cat_name}")
        lines.append("")

        items = re.findall(r'\d+\.\s+\*\*(.+?)\*\*\s*`(.+?)`\s*—\s*(.+?)(?:\s*\[→\]\((.+?)\))?$',
                           body, re.MULTILINE)

        if not items:
            lines.append(f"| 来源 | 标题 | 摘要 | 评级 |")
            lines.append("|------|------|------|------|")
            lines.append(f"| — | 今日无{cat_name}信号 | — | — |")
            lines.append("")
            if cat_name == "AI营销":
                lines.append("> ⚠️ 今日无直接信号 — 此段不可省略，请在日报中保留空表。")
                lines.append("")
            continue

        lines.append("| 来源 | 标题 | 摘要 | 评级 |")
        lines.append("|------|------|------|------|")
        for item in items[:8]:
            headline = item[0].strip()
            tag = item[1].strip()
            why = item[2].strip().rstrip("↳").strip()
            url = item[3] if len(item) > 3 and item[3] else ""
            source_cell = f"[{tag}]({url})" if url else tag
            rating = "⭐⭐⭐" if "重点" in why else ("⭐⭐" if "验证" in why else "⭐")
            lines.append(f"| {source_cell} | {headline[:60]} | {why[:80]} | {rating} |")
        lines.append("")

    # 民生观察
    livelihood = brief_sections.get("### 民生观察", "")
    if livelihood.strip():
        lines.append("### 民生观察")
        lines.append("")
        lines.append(livelihood)
        lines.append("")

    return "\n".join(lines)


def _generate_section_analysis(brief_sections: dict) -> str:
    """Generate the 重点分析 section from brief candidates."""
    lines = ["## 📈 二、重点分析", ""]

    analysis_text = brief_sections.get("## 🔥 重点分析候选", "")
    if not analysis_text:
        lines.append("> 今日无重点分析候选。")
        lines.append("")
        return "\n".join(lines)

    # Extract analysis candidates from brief
    candidates = re.findall(
        r'\*\*(\d+)\.\s+(.+?)\*\*\s*\((.+?),\s*评分(\d+)\)',
        analysis_text,
    )
    for num, headline, domains, score in candidates[:3]:
        lines.append(f"### {num}. {headline.strip()}")
        lines.append(f"**领域**: {domains.strip()} | **评分**: {score}")
        lines.append("")
        # Extract the summary line for this candidate
        summary_match = re.search(
            rf'\*\*{re.escape(num)}\.\s+{re.escape(headline.strip())}.*?\n>\s*(.+?)(?:\n|$)',
            analysis_text, re.DOTALL,
        )
        if summary_match:
            lines.append(f"> {summary_match.group(1).strip()[:300]}")
            lines.append("")

    return "\n".join(lines)


def _generate_section_causal(brief_sections: dict, date_str: str,
                               db=None) -> str:
    """Generate the 因果追踪 section with contradiction data and hypotheses."""
    lines = ["## 📊 三、因果追踪", ""]

    # Contradictions from brief — annotate with source bias if available
    contradictions_text = brief_sections.get("## ⚠️ 矛盾信号（保留张力，勿平滑）", "")
    if contradictions_text and "矛盾实体" in contradictions_text:
        # Add bias annotation if source_bias data available
        try:
            from .source_bias import track_source_bias
            if db:
                biases = track_source_bias(date_str, db)
                if biases:
                    bias_summary = []
                    for key, profile in list(biases.items())[:5]:
                        if abs(profile.overclaimed_ratio) > 0.2:
                            bias_summary.append(
                                f"{profile.source_name}({profile.entity_category}:"
                                f"{profile.overclaimed_ratio:+.2f})"
                            )
                    if bias_summary:
                        lines.append("### 来源偏差标注")
                        lines.append("")
                        lines.append(f"> 偏差来源: {', '.join(bias_summary)}")
                        lines.append("")
        except Exception:
            pass

        lines.append("### 矛盾信号")
        lines.append("")
        lines.append(contradictions_text)
        lines.append("")

    # Cross-validation
    cross_text = brief_sections.get("## 🔗 多源交叉验证", "")
    if cross_text:
        lines.append("### 多源交叉验证")
        lines.append("")
        lines.append(cross_text)
        lines.append("")

    # Attempt hypothesis generation from competing_hypotheses
    try:
        from harness.indexer import HarnessDB
        from harness.competing_hypotheses import run_hypothesis_cycle
        db = HarnessDB()
        snippets = db.get_news_snippets(date=date_str)
        if len(snippets) >= 5:
            hypotheses = run_hypothesis_cycle([], snippets, db, use_llm_seed=False)
            if hypotheses:
                lines.append("### 竞争假设")
                lines.append("")
                lines.append("| ID | 假设 | 状态 | 综合评分 |")
                lines.append("|----|------|------|----------|")
                for h in hypotheses[:5]:
                    lines.append(
                        f"| {h.hypothesis_id} | {h.statement[:80]} | "
                        f"{h.status} | {h.aggregate_rank:.3f} |"
                    )
                lines.append("")
        db.close()
    except Exception as e:
        print(f"[report_writer] hypothesis cycle skipped: {e}", file=sys.stderr)

    lines.append("### 新假设待验证")
    lines.append("")
    lines.append("> 基于今日信号，以下假设待后续验证：")
    lines.append("")
    # Extract entity signals for hypothesis suggestions
    entity_text = brief_sections.get("## 🏷️ 实体信号", "")
    rare_match = re.search(r'\*\*罕见实体\*\*[^:]*:\s*(.+?)$', entity_text, re.MULTILINE)
    if rare_match:
        lines.append(f"- 罕见实体信号: {rare_match.group(1).strip()[:200]}")
    lines.append("")

    return "\n".join(lines)


def _generate_section_prophet(judgment_baseline: dict, date_str: str = "",
                               db=None) -> str:
    """Generate the Prophet 信号 section using compiled predictions.

    Uses prophet_compiler to get structured ProphetPrediction objects,
    which include today-news matching and status tracking.
    """
    lines = ["## 🔮 四、Prophet 信号", ""]

    # Try using compiled predictions first (more structured)
    predictions = []
    if db:
        try:
            from .prophet_compiler import compile_prophet_signals, inject_as_hypotheses
            predictions = compile_prophet_signals(date_str, db)
            # Auto-inject as hypotheses for competing_hypotheses
            if predictions:
                inject_as_hypotheses(predictions, db)
        except Exception:
            pass

    if not predictions:
        # Fallback: use judgment_baseline signals
        signals = judgment_baseline.get("prophet_signals", [])
        if not signals:
            lines.append("> 无活跃 Prophet 信号。")
            lines.append("")
            return "\n".join(lines)

        today = date.today()
        lines.append("| ID | 预言 | 时间窗口 | 置信度 | 验证标准 | 状态 |")
        lines.append("|----|------|----------|--------|----------|------|")
        for s in signals:
            created = s.get("created_date", "")
            horizon = s.get("time_horizon_days", 30)
            try:
                created_date = date.fromisoformat(created)
                days_elapsed = (today - created_date).days
            except (ValueError, TypeError):
                days_elapsed = 0
            status = f"观察中（第{days_elapsed}天/{horizon}天）" if days_elapsed <= horizon else "⚠️ 待验证"
            lines.append(
                f"| {s.get('id', '?')} | {s.get('claim', '')[:60]} | "
                f"{horizon}天 | {s.get('confidence', '?')} | "
                f"{s.get('verification_criteria', '')[:30]} | {status} |"
            )
        lines.append("")
        return "\n".join(lines)

    # Use compiled predictions
    from .prophet_compiler import format_feedback_section
    feedback = format_feedback_section(predictions)
    if feedback:
        lines.append(feedback)
    else:
        lines.append("> 无活跃 Prophet 信号。")
        lines.append("")

    return "\n".join(lines)


def _generate_section_sniff(cross_day_data: list[dict]) -> str:
    """Generate the 语义嗅探 section from cross-day discoveries."""
    lines = ["### 语义嗅探（跨日实体关联）", ""]

    if not cross_day_data:
        lines.append("> 今日无显著跨日语义关联。")
        lines.append("")
        return "\n".join(lines)

    for i, d in enumerate(cross_day_data[:10]):
        entities = ", ".join(d.get("shared_entities", [])[:5])
        lines.append(f"### #{i+1} 共享实体: {entities}")
        lines.append(f"- 历史日期: {d.get('history_date', '?')}")
        lines.append(f"- 实体重叠率: {d.get('jaccard', 0):.2f}")
        lines.append(f"- 稀有度: {d.get('rarity', 0):.3f}")
        cross_tag = "跨板块" if d.get('cross_section', 1) > 1 else "同板块"
        lines.append(f"- 类型: {cross_tag}")
        lines.append("")

    return "\n".join(lines)


def _generate_section_feedback(judgment_baseline: dict, brief_sections: dict) -> str:
    """Generate the 今日反馈 section comparing today with historical judgments."""
    lines = ["## 💬 五、今日反馈", ""]

    headline = judgment_baseline.get("headline_judgment", "")
    source_date = judgment_baseline.get("date", "历史")

    if not headline:
        lines.append("> 首次运行或无可参考的历史判断基线。积累数据后此段将自动填充。")
        lines.append("")
        return "\n".join(lines)

    lines.append(f"### 昨日判断回顾（{source_date}）")
    lines.append("")
    lines.append(f"> {headline}")
    lines.append("")

    # Compare with today's entity signals
    entity_section = brief_sections.get("## 🏷️ 实体信号", "")
    if entity_section:
        lines.append("### 今日信号对照")
        lines.append("")
        lines.append(entity_section[:500])
        lines.append("")

    # Hypothesis status
    hypotheses = judgment_baseline.get("hypotheses", [])
    if hypotheses:
        lines.append("### 历史假设状态")
        lines.append("")
        for h in hypotheses[:5]:
            lines.append(f"- {h}")
        lines.append("")

    return "\n".join(lines)


def generate_report(date_str: str | None = None) -> str | None:
    """Generate a complete 7-section daily news report.

    Reads pipeline outputs and writes a complete report to the Obsidian vault.
    This is the automated replacement for the manual "write the report" step.

    Returns the report text, or None if generation fails.
    """
    today = _get_date(date_str)

    # ── Load all inputs ──────────────────────────────────────────────────
    if not DAILY_BRIEF.exists():
        print(f"[report_writer] ERROR: {DAILY_BRIEF} not found — run preprocess first",
              file=sys.stderr)
        return None

    brief_text = DAILY_BRIEF.read_text(encoding="utf-8")
    brief_sections = _parse_brief_sections(brief_text)

    cross_day_data = _read_cross_day_data(today)
    judgment_baseline = _read_judgment_baseline(today)

    # ── Open DB connection for section generators ──────────────────────
    from harness.indexer import HarnessDB
    db = HarnessDB()

    # ── Extract metadata ─────────────────────────────────────────────────
    d = date.fromisoformat(today)
    wdays = ["一", "二", "三", "四", "五", "六", "日"]
    date_disp = f"{d.year}年{d.month}月{d.day}日 星期{wdays[d.weekday()]}"

    # Count snippets from brief
    ranked_count = len(re.findall(r'^\d+\.\s+\*\*', brief_text, re.MULTILINE))

    # ── Build report sections ────────────────────────────────────────────
    report_lines = [
        f"# 每日新闻精要 — {date_disp}",
        "",
        f"> **今日判断：** [基于以下新闻总览自动生成，请审阅补充]",
        "",
        f"> {ranked_count} 条新闻精要，已按模板分类。",
        "",
        "---",
        "",
    ]

    # Section 2: 新闻总览表
    report_lines.append(_generate_section_overview(brief_sections, ranked_count))
    report_lines.append("---")
    report_lines.append("")

    # Section 3: 重点分析
    report_lines.append(_generate_section_analysis(brief_sections))
    report_lines.append("---")
    report_lines.append("")

    # Section 3: 因果追踪 (includes contradictions, cross-day sniffing, hypotheses)
    causal_content = _generate_section_causal(brief_sections, today, db=db)
    # Append cross-day sniffing as subsection of 因果追踪
    sniff_content = _generate_section_sniff(cross_day_data)
    if sniff_content:
        causal_content += "\n" + sniff_content
    report_lines.append(causal_content)
    report_lines.append("---")
    report_lines.append("")

    # Section 4: Prophet 信号
    report_lines.append(_generate_section_prophet(judgment_baseline, date_str=today, db=db))
    report_lines.append("---")
    report_lines.append("")

    # Section 5: 今日反馈
    report_lines.append(_generate_section_feedback(judgment_baseline, brief_sections))
    report_lines.append("---")
    report_lines.append("")

    # Section 6: 数据源
    report_lines.extend([
        "## 📁 六、数据源",
        "",
        "| 来源 | 类型 | 可信度 | 说明 |",
        "|------|------|--------|------|",
        "| World News API | 英文头条 | ⭐⭐⭐ | 国际新闻主源 |",
        "| anysearch | 中英文综合搜索 | ⭐⭐ | 政策/民生/学术搜索 |",
        "| arXiv | 学术论文 | ⭐⭐⭐ | AI/BCI/具身智能论文 |",
        "| GitHub Trending | 开源趋势 | ⭐⭐ | AI Agent/模型/工具热点 |",
        "| 人民网/中国政府网 | 官方政策 | ⭐⭐⭐ | 政策法规权威源 |",
        "| 财联社/36氪/TechNode | 行业媒体 | ⭐⭐ | 科技/财经动态 |",
        "",
    ])

    # ── Footer ───────────────────────────────────────────────────────────
    report_lines.extend([
        "---",
        "",
        f"*报告由 DuoNews 管线自动生成于 {today}*",
        "",
    ])

    report = "\n".join(report_lines)

    # ── Close DB ───────────────────────────────────────────────────────
    db.close()

    # ── Write to Obsidian vault ──────────────────────────────────────────
    OBSIDIAN_NEWS.mkdir(parents=True, exist_ok=True)
    report_path = OBSIDIAN_NEWS / f"{today}.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"[report_writer] Report written: {report_path} ({len(report)} chars)",
          file=sys.stderr)

    return report


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Generate daily news report")
    parser.add_argument("--date", help="Date YYYY-MM-DD")
    args = parser.parse_args()
    generate_report(date_str=args.date)
