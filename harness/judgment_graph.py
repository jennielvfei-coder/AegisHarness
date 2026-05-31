"""Judgment Graph — extract, store, link, and query 菲菲's judgments.

Zero-LLM pipeline. Extracts hypotheses, contradictions, and Prophet predictions
from daily report Markdown tables. Stores in judgment_entries table.
Links cross-day judgments by entity overlap. Formats injection context
to prompt Claude to challenge 菲菲's thinking.

Design: system provides data, Claude provides intelligence.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


# ── Extraction ──────────────────────────────────────────────────────────────

def extract_and_store_judgments(db, filepath: Path, date: str) -> int:
    """Parse daily report, extract judgments, store in DB, link cross-day.

    Returns total number of judgment entries stored.
    """
    if not filepath.exists():
        return 0

    text = filepath.read_text(encoding="utf-8")
    entries = _parse_judgment_tables(text, date)
    if not entries:
        return 0

    for entry in entries:
        db.save_judgment_entry(entry)

    n_linked = link_judgments(db, entries, date)
    if n_linked:
        import sys
        print(f"[judgment_graph] {n_linked} cross-day links created",
              file=sys.stderr)

    return len(entries)


def _parse_judgment_tables(text: str, date: str) -> list[dict]:
    """Parse 因果追踪 + Prophet tables from daily report text."""
    entries = []

    # Extract sections by ## headers
    section_3 = _extract_section(text, "三、因果追踪")
    section_4 = _extract_section(text, "四、Prophet 信号")

    if section_3:
        entries.extend(_parse_hypothesis_verified(section_3, date))
        entries.extend(_parse_hypothesis_new(section_3, date))
        entries.extend(_parse_contradictions(section_3, date))

    if section_4:
        entries.extend(_parse_prophets(section_4, date))

    return entries


def _extract_section(text: str, marker: str) -> str | None:
    """Extract a ## section by marker text."""
    pattern = rf'##\s+[^#]*?{re.escape(marker)}[^\n]*\n(.*?)(?=\n##\s|\Z)'
    m = re.search(pattern, text, re.DOTALL)
    return m.group(1) if m else None


def _extract_subsection(text: str, marker: str) -> str | None:
    """Extract a ### subsection by marker text."""
    pattern = rf'###\s+[^#]*?{re.escape(marker)}[^\n]*\n(.*?)(?=\n###\s|\n##\s|\Z)'
    m = re.search(pattern, text, re.DOTALL)
    return m.group(1) if m else None


def _parse_table_rows(section_text: str) -> list[dict[str, str]]:
    """Parse markdown table rows into list of column dicts."""
    rows = []
    header = None
    header_seen = False

    for line in section_text.split('\n'):
        stripped = line.strip()
        if not stripped.startswith('|'):
            continue

        cells = [c.strip() for c in stripped.split('|')[1:-1]]

        # Skip separator rows
        if all(re.match(r'^[-:]+$', c) for c in cells if c):
            header_seen = True
            continue

        if not header_seen:
            header = cells
            header_seen = True
            continue

        # Skip empty rows
        if not any(c for c in cells):
            continue

        if header:
            row_dict = {}
            for i, cell in enumerate(cells):
                if i < len(header):
                    row_dict[header[i]] = cell
                else:
                    row_dict[f"col_{i}"] = cell
            rows.append(row_dict)

    return rows


def _parse_hypothesis_verified(text: str, date: str) -> list[dict]:
    """Parse 假设验证 table."""
    sub = _extract_subsection(text, "假设验证")
    if not sub:
        return []
    rows = _parse_table_rows(sub)
    entries = []
    for r in rows:
        label = r.get("#", "").strip()
        statement = r.get("假设", "").strip().replace("**", "")
        verdict_cell = r.get("判定", "")
        verdict = "confirmed" if "成立" in verdict_cell else (
            "falsified" if "未成立" in verdict_cell else "uncertain")
        anchors_raw = r.get("锚点", "")
        anchors = _parse_anchors(anchors_raw)
        if not label or not statement:
            continue
        entries.append({
            "entry_id": f"{label}-{date}",
            "date": date, "entry_type": "hypothesis_verified",
            "label": label, "statement": statement,
            "verdict": verdict,
            "anchors": json.dumps(anchors, ensure_ascii=False),
            "entities": json.dumps(_extract_entities(statement), ensure_ascii=False),
        })
    return entries


def _parse_hypothesis_new(text: str, date: str) -> list[dict]:
    """Parse 新假设 table."""
    sub = _extract_subsection(text, "新假设")
    if not sub:
        return []
    rows = _parse_table_rows(sub)
    entries = []
    for r in rows:
        label = r.get("#", "").strip()
        statement = r.get("假设", "").strip().replace("**", "")
        signals = r.get("可验证信号", "").strip()
        window_str = r.get("窗口", "").strip()
        window_days = _parse_days(window_str)
        if not label or not statement:
            continue
        entries.append({
            "entry_id": f"{label}-{date}",
            "date": date, "entry_type": "hypothesis_new",
            "label": label, "statement": statement,
            "verifiable_signals": signals,
            "window_days": window_days,
            "entities": json.dumps(_extract_entities(statement), ensure_ascii=False),
        })
    return entries


def _parse_contradictions(text: str, date: str) -> list[dict]:
    """Parse 矛盾对 table."""
    sub = _extract_subsection(text, "矛盾对")
    if not sub:
        return []
    rows = _parse_table_rows(sub)
    entries = []
    for r in rows:
        label = r.get("#", "").strip()
        surface = r.get("表面矛盾", "").strip().replace("**", "")
        cause = r.get("底层因果", "").strip().replace("**", "")
        intuition = r.get("直觉判断", "").strip().replace("**", "")
        if not label or not surface:
            continue
        entries.append({
            "entry_id": f"{label}-{date}",
            "date": date, "entry_type": "contradiction",
            "label": label, "statement": surface,
            "surface_contradiction": surface,
            "underlying_cause": cause,
            "intuition": intuition,
            "entities": json.dumps(_extract_entities(surface + " " + cause), ensure_ascii=False),
        })
    return entries


def _parse_prophets(text: str, date: str) -> list[dict]:
    """Parse Prophet 信号 table."""
    rows = _parse_table_rows(text)
    entries = []
    for r in rows:
        label = r.get("#", "").strip()
        statement = r.get("预测", "").strip().replace("**", "")
        window_str = r.get("窗口", "").strip()
        window_days = _parse_days(window_str)
        prob, low, high = _parse_probability(r.get("概率 [区间]", ""))
        trend = r.get("变动", "").strip()
        if not label or not statement:
            continue
        entries.append({
            "entry_id": f"{label}-{date}",
            "date": date, "entry_type": "prophet",
            "label": label, "statement": statement,
            "window_days": window_days,
            "probability": prob, "prob_range_low": low, "prob_range_high": high,
            "trend": trend,
            "entities": json.dumps(_extract_entities(statement), ensure_ascii=False),
        })
    return entries


def _parse_anchors(raw: str) -> list[str]:
    """Parse anchor evidence like '[1] Deloitte..., [2] NVIDIA...'"""
    anchors = re.findall(r'\[(\d+)\]\s*(.+?)(?=\s*\[(?:\d+)\]|\Z)', raw)
    return [f"[{n}] {desc.strip().rstrip(',')}" for n, desc in anchors]


def _parse_days(raw: str) -> int | None:
    """Parse '90天' or '180天' or '30天' into int."""
    m = re.search(r'(\d+)', raw)
    return int(m.group(1)) if m else None


def _parse_probability(raw: str) -> tuple[float | None, float | None, float | None]:
    """Parse '65% [55-75%]' into (0.65, 0.55, 0.75)."""
    nums = re.findall(r'(\d+)', raw)
    if not nums:
        return None, None, None
    point = int(nums[0]) / 100 if len(nums) >= 1 else None
    low = int(nums[1]) / 100 if len(nums) >= 2 else None
    high = int(nums[2]) / 100 if len(nums) >= 3 else None
    return point, low, high


def _extract_entities(text: str) -> list[str]:
    """Extract canonical entity names from text. Lightweight version —
    uses the same ENTITY_DICT as news_vectorizer."""
    try:
        from news_agent.vectorize import _extract_entities as _ee
        return _ee(text)
    except ImportError:
        return []


# ── Cross-day Linking ────────────────────────────────────────────────────────

def link_judgments(db, today_entries: list[dict], date: str) -> int:
    """Find cross-day links between today's judgments and past entries."""
    links_created = 0
    try:
        cur = db._conn.execute(
            """SELECT entry_id, date, entry_type, label, statement, verdict, entities
               FROM judgment_entries
               WHERE date < ? AND date >= date(?, '-60 days')
               ORDER BY date DESC""",
            (date, date),
        )
        past_entries = [
            {"entry_id": r[0], "date": r[1], "entry_type": r[2],
             "label": r[3], "statement": r[4], "verdict": r[5],
             "entities": json.loads(r[6]) if r[6] else []}
            for r in cur.fetchall()
        ]
    except Exception:
        return 0

    if not past_entries:
        return 0

    for today in today_entries:
        today_entities = set(json.loads(today.get("entities", "[]")))
        if not today_entities:
            continue

        for past in past_entries:
            past_entities = set(past.get("entities", []))
            if not past_entities:
                continue

            shared = today_entities & past_entities
            if not shared:
                continue

            jaccard = len(shared) / len(today_entities | past_entities)
            if jaccard < 0.1:
                continue

            link_type = _determine_link_type(today, past)

            try:
                db._conn.execute(
                    """INSERT OR IGNORE INTO judgment_links
                       (source_entry_id, target_entry_id, link_type,
                        shared_entities, jaccard_score)
                       VALUES (?, ?, ?, ?, ?)""",
                    (today["entry_id"], past["entry_id"], link_type,
                     json.dumps(sorted(shared), ensure_ascii=False), round(jaccard, 4)),
                )
                db._conn.commit()
                links_created += 1
            except Exception:
                pass

    return links_created


def _determine_link_type(today: dict, past: dict) -> str:
    """Determine relationship type between two judgments."""
    t_type = today.get("entry_type", "")
    p_type = past.get("entry_type", "")

    # Contradiction check: same topic, opposite verdicts
    if t_type == "hypothesis_verified" and p_type == "hypothesis_verified":
        tv = today.get("verdict")
        pv = past.get("verdict")
        if tv and pv and tv != pv:
            return "contradicts"
        if tv == pv:
            return "supports"

    # Prediction related
    if t_type == "prophet" or p_type == "prophet":
        return "prediction_related"

    return "entity_overlap"


# ── Query for Injection ──────────────────────────────────────────────────────

def query_relevant_judgments(db_path: str, date: str,
                               entities: list[str],
                               topics: list[str] = None) -> list[dict]:
    """Query past judgments relevant to today's news session.

    Layers:
    1. Expiring predictions (window closing within 7 days)
    2. Entity-linked judgments via judgment_links
    3. Stale confirmed hypotheses (un-updated > 5 days)
    """
    import sqlite3
    results = []
    seen: set[str] = set()

    try:
        conn = sqlite3.connect(db_path, timeout=2)

        # Layer 1: Expiring predictions
        cur = conn.execute(
            """SELECT entry_id, date, label, statement, probability, window_days,
                      CAST(julianday(date) + window_days - julianday(?) AS INTEGER) as days_left
               FROM judgment_entries
               WHERE entry_type = 'prophet'
                 AND verdict IS NULL
                 AND days_left >= 0 AND days_left <= 7
               ORDER BY days_left ASC
               LIMIT 3""",
            (date,),
        )
        for row in cur.fetchall():
            eid = row[0]
            if eid not in seen:
                seen.add(eid)
                results.append({
                    "entry_id": eid, "date": row[1], "label": row[2],
                    "statement": row[3], "probability": row[4],
                    "window_days": row[5], "days_left": row[6],
                    "signal": "expiring_prediction",
                })

        # Layer 2: Entity-linked past judgments
        if entities:
            entity_placeholders = ",".join("?" * len(entities))
            cur = conn.execute(
                f"""SELECT DISTINCT je.entry_id, je.date, je.label, je.statement,
                           je.verdict, je.entry_type, jl.link_type, jl.shared_entities
                    FROM judgment_entries je
                    JOIN judgment_links jl ON je.entry_id = jl.target_entry_id
                    WHERE jl.source_entry_id IN (
                        SELECT entry_id FROM judgment_entries WHERE date = ?
                    )
                    ORDER BY je.date DESC
                    LIMIT 6""",
                (date,),
            )
            for row in cur.fetchall():
                eid = row[0]
                if eid not in seen:
                    seen.add(eid)
                    shared = json.loads(row[7]) if row[7] else []
                    results.append({
                        "entry_id": eid, "date": row[1], "label": row[2],
                        "statement": row[3], "verdict": row[4],
                        "entry_type": row[5], "link_type": row[6],
                        "shared_entities": shared,
                        "signal": "entity_linked",
                    })

        # Layer 3: Stale confirmed hypotheses (> 5 days old, no update)
        cur = conn.execute(
            """SELECT entry_id, date, label, statement, verdict
               FROM judgment_entries
               WHERE entry_type = 'hypothesis_verified'
                 AND verdict = 'confirmed'
                 AND julianday(?) - julianday(date) > 5
                 AND last_updated IS NULL
               ORDER BY date ASC
               LIMIT 3""",
            (date,),
        )
        for row in cur.fetchall():
            eid = row[0]
            if eid not in seen:
                seen.add(eid)
                results.append({
                    "entry_id": eid, "date": row[1], "label": row[2],
                    "statement": row[3], "verdict": row[4],
                    "signal": "stale_confirmed",
                })

        conn.close()
    except Exception:
        pass

    return results[:8]


# ── Injection Formatting ─────────────────────────────────────────────────────

def format_judgment_injection(judgments: list[dict]) -> str:
    """Format past judgments as injection context to prompt Claude.

    Structure (targeting ~200 tokens):
    1. Linked past judgments relevant to today
    2. Expiring prediction urgency
    3. Stale judgments needing revisit
    4. Action prompts for Claude
    """
    if not judgments:
        return ""

    lines = ["## 🧠 认知判断图谱"]
    lines.append("")

    expiring = [j for j in judgments if j.get("signal") == "expiring_prediction"]
    stale = [j for j in judgments if j.get("signal") == "stale_confirmed"]
    linked = [j for j in judgments if j.get("signal") == "entity_linked"]

    # Section 1: Linked past judgments
    if linked:
        lines.append("**你之前的判断，今日可能相关:**")
        lines.append("")
        for j in linked[:4]:
            vmark = {"confirmed": "✅", "falsified": "❌", "uncertain": "⚠️"}.get(
                j.get("verdict", ""), "")
            lines.append(
                f"- {j['label']} {vmark} {j['statement'][:60]} "
                f"({j['date'][5:]})"
            )
            if j.get("shared_entities"):
                ents = ", ".join(j["shared_entities"][:3])
                lines.append(f"  共享实体: {ents}")
            lt = j.get("link_type", "")
            if lt == "contradicts":
                lines.append(f"  ⚠️ 与今日判断有潜在矛盾")
        lines.append("")

    # Section 2: Expiring predictions
    if expiring:
        lines.append("**⏰ 预测窗口即将关闭:**")
        lines.append("")
        for j in expiring[:2]:
            prob_str = f"{j.get('probability', 0)*100:.0f}%" if j.get("probability") else "?"
            lines.append(
                f"- {j['label']} \"{j['statement'][:50]}\" "
                f"— {j['days_left']}天后到期 (概率: {prob_str})"
            )
        lines.append("")

    # Section 3: Stale judgments
    if stale:
        lines.append("**🔄 超过5天未更新的判断:**")
        lines.append("")
        for j in stale[:2]:
            vmark = {"confirmed": "✅", "falsified": "❌"}.get(
                j.get("verdict", ""), "")
            lines.append(f"- {j['label']} {vmark} {j['statement'][:60]} ({j['date'][5:]})")
        lines.append("")

    # Section 4: Action prompts
    lines.append("**⚠️ 请在日报中检查:**")
    lines.append("")
    prompts = []

    if linked:
        prompts.append("以上判断是否被今日新证据支持或削弱？需要更新判定吗？")
    if expiring:
        labels = ", ".join(j["label"] for j in expiring)
        prompts.append(f"{labels} 预测窗口将到期——基于今日信息更新概率？")
    if stale:
        labels = ", ".join(j["label"] for j in stale)
        prompts.append(f"{labels} 长期未更新——是否可转为\"已验证\"或\"已推翻\"？")

    if not prompts:
        prompts.append("今日新闻中是否有证据需要更新已有判断？")

    for i, p in enumerate(prompts, 1):
        lines.append(f"{i}. {p}")

    return "\n".join(lines)


# ── Reflection: process user judgment updates ────────────────────────────

def process_reflections(db, date: str) -> int:
    """Read signal_buffer for judgment_update signals, apply changes to judgment_entries.

    Parses natural language like:
      "H3不成立了，因为Manus禁令被撤销"
      "P4的概率上调到70%"
      "C1的底层因果已经变了"

    Returns number of judgments updated.
    """
    try:
        cur = db._conn.execute(
            "SELECT content FROM signal_buffer WHERE signal_type='judgment_update'"
        )
        messages = [row[0] for row in cur.fetchall() if row[0]]
    except Exception:
        return 0

    if not messages:
        return 0

    updated = 0
    for msg in messages:
        labels = _extract_judgment_labels(msg)
        for label in labels:
            new_verdict = _infer_verdict_update(msg)
            new_prob = _infer_probability_update(msg)

            # Find the latest entry for this label
            cur = db._conn.execute(
                "SELECT entry_id, statement, verdict, probability FROM judgment_entries "
                "WHERE label=? ORDER BY date DESC LIMIT 1",
                (label,),
            )
            row = cur.fetchone()
            if not row:
                continue

            entry_id, statement, old_verdict, old_prob = row
            import time as _time
            now = _time.time()

            if new_prob is not None and old_prob is not None:
                db._conn.execute(
                    "UPDATE judgment_entries SET probability=?, prob_range_low=?, "
                    "prob_range_high=?, last_updated=? WHERE entry_id=?",
                    (new_prob, max(0, new_prob - 0.10), min(1.0, new_prob + 0.10),
                     now, entry_id),
                )
                db._conn.commit()
                updated += 1

            if new_verdict and new_verdict != old_verdict:
                db._conn.execute(
                    "UPDATE judgment_entries SET verdict=?, last_updated=? WHERE entry_id=?",
                    (new_verdict, now, entry_id),
                )
                # Also log the change
                try:
                    db._conn.execute(
                        """INSERT INTO judgment_status_log
                           (entry_id, date, previous_verdict, new_verdict, evidence_summary)
                           VALUES (?,?,?,?,?)""",
                        (entry_id, date, old_verdict, new_verdict,
                         f"User reflection: {msg[:200]}"),
                    )
                    db._conn.commit()
                except Exception:
                    pass  # Table might not exist yet
                updated += 1

    return updated


def _extract_judgment_labels(msg: str) -> list[str]:
    """Extract judgment labels like H3, C2, P4 from a message."""
    import re
    return list(set(re.findall(r'[HCP]\d+', msg)))


def _infer_verdict_update(msg: str) -> str | None:
    """Infer new verdict from revision message.

    'H3不成立了' → 'falsified'
    'H2被推翻了' → 'falsified'
    'H1还是成立的' → 'confirmed'
    """
    if any(w in msg for w in ("不成立", "推翻", "错了", "不对", "被证伪", "未成立")):
        return "falsified"
    if any(w in msg for w in ("还是成立", "依然成立", "维持成立")):
        return "confirmed"
    return None


def _infer_probability_update(msg: str) -> float | None:
    """Infer new probability from revision message.

    'P4的概率上调到70%' → 0.70
    'P1下调到30%' → 0.30
    """
    import re
    # Find numbers that appear after "%" or "到" — not label numbers like P4
    m = re.search(r'(\d+)\s*%', msg)
    if m:
        return int(m.group(1)) / 100.0
    # Fallback: "上调到70" or "改成65"
    m = re.search(r'[到改成](\d+)', msg)
    if m:
        val = int(m.group(1))
        if val <= 100 and ("概率" in msg or val > 10):
            return val / 100.0
    return None


# ── CLI test entry ──

def main():
    import sys
    from indexer import HarnessDB

    db = HarnessDB()

    if len(sys.argv) >= 2:
        filepath = Path(sys.argv[1])
        if not filepath.is_absolute():
            filepath = Path.cwd() / filepath

        date_match = re.search(r'(\d{4}-\d{2}-\d{2})', filepath.name)
        date = date_match.group(1) if date_match else "unknown"

        if not filepath.exists():
            print(f"File not found: {filepath}")
            db.close()
            return

        # Test extraction
        text = filepath.read_text(encoding="utf-8")
        entries = _parse_judgment_tables(text, date)
        print(f"Extracted {len(entries)} judgments:")

        by_type = {}
        for e in entries:
            t = e["entry_type"]
            by_type.setdefault(t, []).append(e)
        for t, elist in by_type.items():
            print(f"\n  {t} ({len(elist)}):")
            for e in elist[:3]:
                print(f"    {e['label']}: {e['statement'][:80]}")
                if e.get("verdict"):
                    print(f"      判定: {e['verdict']}")
                if e.get("probability"):
                    print(f"      概率: {e['probability']} [{e.get('prob_range_low','?')}-{e.get('prob_range_high','?')}]")

        # Test store + link
        n_stored = extract_and_store_judgments(db, filepath, date)
        print(f"\nStored: {n_stored} entries")

        # Test query
        all_entities = set()
        for e in entries:
            ents = json.loads(e.get("entities", "[]"))
            all_entities.update(ents)
        related = query_relevant_judgments(
            "D:/Claude/harness/state.db", date, list(all_entities),
        )
        print(f"Related judgments found: {len(related)}")
        injection = format_judgment_injection(related)
        if injection:
            print(f"\n--- Injection ({len(injection)} chars) ---")
            print(injection)
    else:
        print("Usage: python judgment_graph.py <news_file.md>")

    db.close()


if __name__ == "__main__":
    main()
