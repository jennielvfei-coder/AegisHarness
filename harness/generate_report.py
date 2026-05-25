"""Generate annotated daily news report with Harness v2 pipeline."""
import sys, json
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))

from feature_finder import (find_features, _extract_cluster_entities,
                             _extract_cluster_headlines, _match_feature_library,
                             generate_cluster_conclusions)
from indexer import HarnessDB
from pathlib import Path

db = HarnessDB()
date_str = sys.argv[1] if len(sys.argv) > 1 else "2026-05-21"

snips = db.get_news_snippets(date=date_str)
if not snips:
    print(f"No snippets for {date_str}")
    db.close()
    sys.exit(1)

anomalies = find_features(snips, db)
conclusions_text = generate_cluster_conclusions(anomalies, snips, db)

# Read original news
news_path = Path(r"C:\Users\Chucky\Documents\Obsidian Vault\claude专属文件夹\news") / f"{date_str}.md"
if not news_path.exists():
    print(f"News file not found: {news_path}")
    db.close()
    sys.exit(1)

news = news_path.read_text(encoding="utf-8")

# Entity frequency for rare detection
all_freq = {}
for s in snips:
    for e in s.get("entities", []):
        all_freq[e] = all_freq.get(e, 0) + 1

# Build cluster analysis
cluster_lines = [
    "## 🔗 语义簇分析（384维嵌入聚类 + 实体提取）",
    "",
    f"{len(snips)}条新闻 -> {len(anomalies)} 个语义簇",
    "",
]

for i, a in enumerate(anomalies):
    indices = [j for j in a.supporting_snippets if j < len(snips)]
    cs = [snips[j] for j in indices]
    entities = [e for e, _ in _extract_cluster_entities(cs, top_n=10)]
    headlines = _extract_cluster_headlines(cs, top_n=4)
    matches = _match_feature_library(entities, db)

    if not entities:
        continue

    # Rare distinguishing entities (appear <= 4 times total)
    rare = sorted([(e, all_freq.get(e, 999)) for e in entities], key=lambda x: x[1])
    rare_ents = [e for e, c in rare if c <= 4][:4]

    # Feature match label
    feat_label = ""
    if matches:
        feat_label = " -> 特征模式: " + ", ".join(
            f"{fid} {name}" for fid, name, _ in matches[:2]
        )

    # Generate conclusion
    if rare_ents:
        main_signal = headlines[0].replace("**", "").strip()[:60]
        rare_str = "、".join(rare_ents)
        conclusion = f"{rare_str} 驱动: {main_signal}{feat_label}"
    else:
        conclusion = headlines[0].replace("**", "").strip()[:80]

    cluster_lines.append(
        f"### 簇{i+1}: " + " . ".join(entities[:4]) + f"  ({len(indices)}条 conf={a.detection_confidence:.2f})"
    )
    cluster_lines.append(f"> {conclusion}")
    cluster_lines.append("")
    for h in headlines:
        cluster_lines.append(f"- {h}")
    cluster_lines.append("")

# Check for livelihood snippets and generate 民生观察 block
liv_snips = [s for s in snips if s.get("section") == "livelihood"]
if liv_snips:
    liv_lines = [
        "",
        "## 🏘️ 民生观察（anysearch 自动采集）",
        "",
    ]
    topic_keywords = {
        "就业": ["就业", "招聘", "灵活用工", "劳动", "失业"],
        "教育": ["教育", "学区", "职业", "双减", "高等"],
        "消费": ["消费", "物价", "零售", "收入", "购买"],
        "基层治理": ["基层", "社区", "乡村", "县域", "治理"],
    }
    for topic in ["就业", "教育", "消费", "基层治理"]:
        matched = []
        for s in liv_snips:
            headline = s.get("headline", "")
            if any(kw in headline for kw in topic_keywords.get(topic, [])):
                matched.append(s)

        liv_lines.append(f"### {topic}")
        if matched:
            for s in matched[:3]:
                source_url = ""
                sources = s.get("sources", [])
                if sources and isinstance(sources, list):
                    source_url = sources[0].get("url", "") if isinstance(sources[0], dict) else str(sources[0])
                headline = s.get("headline", "").replace("**", "").strip()
                liv_lines.append(f"- **{headline}** — {source_url}")
        else:
            liv_lines.append("- 本周无显著信号")
        liv_lines.append("")

    # Prepend livelihood before cluster analysis
    cluster_lines = liv_lines + ["---"] + cluster_lines

# Add conclusions block
cluster_lines.append("---")
cluster_lines.append(conclusions_text)

# Insert before data source section
target = "## 📁 六、数据源说明"
if target in news:
    news = news.replace(target, "\n".join(cluster_lines) + "\n\n---\n\n" + target)

# Write output
out = news_path.parent / f"{date_str}-harness-v2.md"
out.write_text(news, encoding="utf-8")
print(f"Written {len(news)} chars to {out.name}")
print()

# Summary
print("Key clusters:")
for a in anomalies:
    indices = [j for j in a.supporting_snippets if j < len(snips)]
    cs = [snips[j] for j in indices]
    ents = [e for e, _ in _extract_cluster_entities(cs, top_n=4)]
    rare = sorted([(e, all_freq.get(e, 999)) for e in ents], key=lambda x: x[1])[:3]
    rare_str = ", ".join(e for e, c in rare if c <= 4)
    top_ent = " . ".join(ents[:3])
    print(f"  {top_ent} ({len(indices)}条) -> {rare_str}")

db.close()
