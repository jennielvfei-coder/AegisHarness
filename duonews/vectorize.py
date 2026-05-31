"""News Vectorizer — parse daily .md reports, extract structured snippets, embed, store.

Parses the Obsidian vault daily newspaper format into NewsSnippet objects,
computes 384-dim embeddings via encoder.py, and stores them in state.db.

Entity extraction uses a 3-layer pipeline:
  1. Dictionary matching
  2. Normalization via entity_aliases.json
  3. spaCy NER fallback — optional, silently skipped if unavailable
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import HARNESS_DIR

# ── Entity extraction resources ──────────────────────────────────────────

ENTITY_DICT: dict[str, list[str]] = {
    "美国": ["US", "United States", "美方", "U.S.", "America", "华盛顿"],
    "中国": ["CN", "China", "中方", "PRC", "中国大陆", "北京"],
    "欧盟": ["EU", "European Union", "欧方", "Europe", "布鲁塞尔"],
    "日本": ["JP", "Japan", "日方", "东京"],
    "韩国": ["KR", "Korea", "South Korea", "首尔", "KOSPI"],
    "印度": ["IN", "India", "印方", "新德里"],
    "俄罗斯": ["RU", "Russia", "俄方", "莫斯科"],
    "伊朗": ["IR", "Iran", "伊方", "德黑兰"],
    "NVIDIA": ["英伟达", "Nvidia", "NVDA", "黄仁勋"],
    "AMD": ["Advanced Micro Devices", "苏姿丰"],
    "Intel": ["英特尔"],
    "华为": ["Huawei", "昇腾", "鸿蒙"],
    "Apple": ["苹果", "AAPL"],
    "Google": ["谷歌", "Alphabet", "GOOGL"],
    "Microsoft": ["微软", "MSFT"],
    "OpenAI": [],
    "Anthropic": ["Claude"],
    "Meta": ["META", "Facebook"],
    "Tesla": ["特斯拉", "TSLA"],
    "SpaceX": [],
    "Samsung": ["三星"],
    "SK_Hynix": ["SK海力士", "SK Hynix"],
    "TSMC": ["台积电", "台积"],
    "Alibaba": ["阿里", "阿里巴巴", "BABA"],
    "Baidu": ["百度", "BIDU", "李彦宏"],
    "Tencent": ["腾讯"],
    "ByteDance": ["字节跳动", "字节"],
    "DeepSeek": [],
    "宇树科技": ["Unitree"],
    "AI": ["人工智能", "artificial intelligence", "大模型", "LLM", "GPT"],
    "Agent": ["AI Agent", "智能体", "Agentic"],
    "芯片": ["半导体", "chip", "semiconductor", "集成电路", "IC"],
    "GPU": ["图形处理器", "H200", "B200", "GB300", "Blackwell", "Rubin"],
    "出口管制": ["export control", "出口限制", "实体清单", "贸易限制", "EUV"],
    "具身智能": ["embodied AI", "机器人", "robotics", "VLA"],
    "脑机接口": ["BCI", "brain-computer interface", "神经接口"],
    "制裁": ["sanctions", "封锁", "禁运"],
    "央行": ["中央银行", "central bank", "美联储", "Fed", "ECB", "PBOC", "人民银行"],
    "加息": ["rate hike", "利率上调", "升息"],
    "降息": ["rate cut", "利率下调"],
    "通胀": ["inflation", "CPI", "物价"],
    "油价": ["原油", "oil price", "WTI", "Brent"],
    "Shibor": ["隔夜拆借利率", "SHIBOR", "银行间利率"],
    "地方债": ["地方政府债务", "local government debt"],
    "IPO": ["首次公开募股", "上市"],
    "特朗普": ["Trump", "川普"],
    "习近平": ["Xi", "习主席"],
    "普京": ["Putin"],
    "马斯克": ["Musk", "Elon Musk"],
    "Karpathy": ["Andrej Karpathy"],
}

_ALIAS_TO_CANONICAL: dict[str, str] = {}
for _canonical, _aliases in ENTITY_DICT.items():
    for _a in _aliases:
        _ALIAS_TO_CANONICAL[_a.lower()] = _canonical
    _ALIAS_TO_CANONICAL[_canonical.lower()] = _canonical

COMPANY_PATTERN = re.compile(
    r'\b([A-Z][a-z]* (?:Inc|Corp|Ltd|LLC|Group|Tech|AI|Lab|Capital|Partners))\b|'
    r'\b([A-Z]{2,6}(?:\.[A-Z]{1,2})?)\b'
)
POLICY_PATTERN = re.compile(
    r'(出口管制|反垄断|制裁|实体清单|关税|贸易战|进口|出口|调查|'
    r'国家安全|数据安全|隐私|合规|法案|条例|规定|禁令|限制|禁运)'
)
ECON_PATTERN = re.compile(
    r'(利率|通胀|CPI|PPI|PMI|GDP|失业率|汇率|国债|收益率|'
    r'回购|逆回购|MLF|LPR|降准|加息|降息|宽松|紧缩|财政|预算|赤字)'
)

_NLP = None


def _get_nlp():
    global _NLP
    if _NLP is None:
        try:
            import spacy
            _NLP = spacy.load("en_core_web_sm")
        except Exception:
            _NLP = False
    return _NLP if _NLP is not False else None


@dataclass
class NewsSnippet:
    date: str
    section: str
    headline: str
    summary: str
    entities: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    source_rating: str = ""
    content_hash: str = ""
    embedding: list[float] | None = None


def _extract_summary(text: str, max_chars: int = 500, min_chars: int = 300) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text

    sentence_breaks = re.finditer(r'[。！？\n\.!?]', text[min_chars:max_chars])
    positions = [m.start() + min_chars for m in sentence_breaks]
    if positions:
        return text[:positions[-1] + 1].strip()

    fallback = max(text.rfind('，', 0, max_chars), text.rfind(',', 0, max_chars),
                   text.rfind(' ', 0, max_chars))
    if fallback > min_chars:
        return text[:fallback].strip()

    return text[:max_chars].strip()


def _extract_entities(text: str) -> list[str]:
    """4-layer entity extraction pipeline.

    Layer 1: ENTITY_DICT dictionary matching (high precision, bilingual)
    Layer 2: jieba + TF-IDF keyword extraction (Chinese unregistered terms)
    Layer 3: Regex patterns (COMPANY/POLICY/ECON)
    Layer 4: spaCy en_core_web_sm (English NER fallback)
    """
    found: set[str] = set()
    text_lower = text.lower()

    _generic_terms = {"制裁", "关税", "进口", "出口", "调查", "合规", "条例", "规定",
                      "禁令", "限制", "禁运", "利率", "通胀", "回购", "降准", "加息",
                      "降息", "宽松", "紧缩", "财政", "预算", "赤字", "消费", "投资",
                      "隐私", "法案", "数据安全", "国家安全", "反垄断", "实体清单",
                      "公司", "科技", "技术", "数据", "市场", "经济", "发展", "产业",
                      "领域", "方面", "相关", "进行", "一个", "可以", "已经", "目前"}

    # Layer 1: Dictionary matching
    for alias, canonical in _ALIAS_TO_CANONICAL.items():
        if alias in text_lower:
            found.add(canonical)

    # Layer 2: jieba Chinese word segmentation + keyword extraction
    try:
        import jieba
        import jieba.analyse
        # Extract top TF-IDF keywords as candidate entities
        keywords = jieba.analyse.extract_tags(text, topK=15, withWeight=False)
        # Filter: keep only multi-character Chinese words (likely entities)
        for kw in keywords:
            if len(kw) >= 2 and any('一' <= c <= '鿿' for c in kw):
                # Check against generic term filter
                if kw not in _generic_terms:
                    found.add(kw)
    except ImportError:
        pass

    # Layer 3: Regex patterns
    for pattern in [COMPANY_PATTERN, POLICY_PATTERN, ECON_PATTERN]:
        for m in pattern.finditer(text):
            found.add(m.group(0))

    # Layer 4: spaCy English NER
    nlp = _get_nlp()
    if nlp is not None:
        try:
            doc = nlp(text[:2000])
            for ent in doc.ents:
                if ent.label_ in ("PERSON", "ORG", "GPE"):
                    found.add(ent.text)
        except Exception:
            pass

    found -= _generic_terms
    found = {e for e in found if not (len(e) <= 3 and e.isascii() and e.isupper())}
    return sorted(found)


def _parse_table_rows(section_text: str) -> list[dict]:
    rows = []
    header_seen = False
    in_table = False

    for line in section_text.split('\n'):
        stripped = line.strip()
        if not stripped.startswith('|'):
            in_table = False
            header_seen = False
            continue

        cells = [c.strip() for c in stripped.split('|')[1:-1]]

        if all(re.match(r'^[-:]+$', c) for c in cells if c):
            header_seen = True
            continue

        if not header_seen:
            header_seen = True
            continue

        if not any(c for c in cells):
            continue

        headline = cells[1].replace('**', '').strip() if len(cells) > 1 else ""
        summary = ""
        if len(cells) >= 3:
            summary = cells[2].strip().replace('**', '')
        if len(cells) >= 4 and not summary:
            summary = cells[3].strip().replace('**', '')

        rows.append({"headline": headline, "summary": summary})

    return rows


def _is_news_section(sub_name: str, section_title: str) -> bool:
    news_markers = ["新闻总览表", "总览表", "今日速览", "速览"]
    is_news = any(m in section_title for m in news_markers)
    if not is_news:
        return False

    exclude = ["论文因果解读", "因果解读", "对菲菲的启发", "命中你的核心",
               "数据源说明", "Prophet", "假设验证", "矛盾对", "新假设",
               "重点分析", "因果追踪", "今日摘要", "今日判断", "关联你的学习目标"]
    if any(ex in sub_name for ex in exclude):
        return False

    return True


def _guess_section_name(subsection_title: str) -> str:
    mapping = {
        "AI/科技": "AI/Tech", "AI营销": "AI Marketing", "AI 营销": "AI Marketing",
        "中美/AI治理": "US-China-AI-Governance", "中美": "US-China",
        "中美关系": "US-China", "地缘/宏观": "Geopolitics-Macro",
        "地缘": "Geopolitics", "国际地缘": "Geopolitics",
        "宏观": "Macroeconomics", "宏观经济": "Macroeconomics",
        "政策/法律": "Policy/Law", "政策": "Policy/Law", "法律": "Policy/Law",
        "arXiv": "arXiv", "AI学术前沿": "arXiv", "AI学术": "arXiv",
        "热门话题": "Hot Topics",
    }
    for key, label in mapping.items():
        if key in subsection_title:
            return label
    return subsection_title[:30]


def _extract_sources_from_row(raw_line: str) -> list[dict]:
    sources = []
    credibility = {
        "SCMP": 0.85, "RTTNews": 0.80, "TechNode": 0.80, "ToI": 0.75,
        "36氪": 0.80, "财联社": 0.65, "人民网": 0.85, "中国政府网": 0.90,
        "光明网": 0.80, "环球网": 0.75, "CCTV": 0.85, "大众网": 0.70,
        "World News API": 0.85, "Bloomberg": 0.90, "Reuters": 0.95,
        "AP": 0.95, "Livemint": 0.70, "The Star": 0.75,
        "arXiv": 0.90, "Forum AI": 0.80, "SCMP+": 0.85,
    }
    for name, cred in credibility.items():
        if name in raw_line:
            sources.append({"name": name, "type": "news", "credibility": cred})
    if not sources:
        sources.append({"name": "unknown", "type": "news", "credibility": 0.50})
    return sources


def _compute_content_hash(headline: str, sources: list[dict], date: str) -> str:
    normalized = re.sub(r'[^\w一-鿿]', '', headline.lower())[:80]
    primary_source = sources[0]["name"] if sources else "unknown"
    key = f"{normalized}|{primary_source}|{date}"
    return hashlib.sha256(key.encode()).hexdigest()


def parse_news_file(filepath: Path) -> list[NewsSnippet]:
    if not filepath.exists():
        raise FileNotFoundError(f"News file not found: {filepath}")

    text = filepath.read_text(encoding="utf-8")

    date_match = re.search(r'(\d{4}-\d{2}-\d{2})', filepath.name)
    date = date_match.group(1) if date_match else "unknown"

    sections = re.split(r'\n(?=## )', text)
    snippets: list[NewsSnippet] = []

    for section in sections:
        header_match = re.match(r'##\s+(.+)', section)
        if not header_match:
            continue
        section_title = header_match.group(1)

        subsections = re.split(r'\n(?=### )', section)

        for sub in subsections:
            sub_header_match = re.match(r'###\s+(.+)', sub)
            sub_name = sub_header_match.group(1) if sub_header_match else section_title
            section_label = _guess_section_name(sub_name)

            if not _is_news_section(sub_name, section_title):
                continue

            table_rows = _parse_table_rows(sub)

            for row in table_rows:
                headline = row["headline"]
                if not headline or len(headline) < 5:
                    continue

                summary = _extract_summary(row["summary"] or headline)
                entities = _extract_entities(headline + " " + summary)
                sources = _extract_sources_from_row(sub)
                source_rating = ""
                content_hash = _compute_content_hash(headline, sources, date)

                snippet = NewsSnippet(
                    date=date, section=section_label,
                    headline=headline, summary=summary,
                    entities=entities, sources=sources,
                    source_rating=source_rating, content_hash=content_hash,
                )
                snippets.append(snippet)

    return snippets


def vectorize_snippets(snippets: list[NewsSnippet], db) -> list[NewsSnippet]:
    from harness.encoder import encode_cached

    for s in snippets:
        text = (s.headline + " " + s.summary)[:8000]
        embedding = encode_cached(text, "news_snippet",
                                  hashlib.sha256(text.encode()).hexdigest(), db)
        s.embedding = embedding

        db.save_news_snippet(
            date=s.date, section=s.section,
            headline=s.headline, summary=s.summary,
            entities=s.entities, sources=s.sources,
            source_rating=s.source_rating, content_hash=s.content_hash,
            embedding=embedding,
        )

    return snippets


def load_recent_snippets(db, days: int = 30, date: str | None = None) -> list[dict]:
    return db.get_news_snippets(days=days, date=date)


def load_entity_aliases(aliases_path: Path | None = None) -> None:
    global ENTITY_DICT, _ALIAS_TO_CANONICAL
    if aliases_path is None:
        aliases_path = HARNESS_DIR / "entity_aliases.json"

    if not aliases_path.exists():
        return

    try:
        with open(aliases_path, "r", encoding="utf-8") as f:
            extra = json.load(f)

        for canonical, aliases in extra.items():
            if canonical in ENTITY_DICT:
                ENTITY_DICT[canonical].extend(aliases)
                ENTITY_DICT[canonical] = list(set(ENTITY_DICT[canonical]))
            else:
                ENTITY_DICT[canonical] = aliases

        _ALIAS_TO_CANONICAL.clear()
        for canonical, aliases in ENTITY_DICT.items():
            for a in aliases:
                _ALIAS_TO_CANONICAL[a.lower()] = canonical
            _ALIAS_TO_CANONICAL[canonical.lower()] = canonical
    except (json.JSONDecodeError, OSError):
        pass


def run_vectorizer():
    import sys
    if len(sys.argv) < 2:
        print("Usage: python -m duonews --step vectorize <news_file.md> [--embed]")
        return

    from harness.indexer import HarnessDB
    db = HarnessDB()

    filepath = Path(sys.argv[1])
    if not filepath.is_absolute():
        filepath = Path.cwd() / filepath

    snippets = parse_news_file(filepath)
    print(f"Parsed {len(snippets)} snippets from {filepath.name}")

    if len(sys.argv) > 2 and sys.argv[2] == "--embed":
        vectorize_snippets(snippets, db)
        print(f"Embedded and stored {len(snippets)} snippets")

    for i, s in enumerate(snippets[:5]):
        print(f"  [{i}] {s.section} | {s.headline[:60]}... | entities: {s.entities[:5]}")


if __name__ == "__main__":
    run_vectorizer()
