"""Feedback Learner — lightweight news feedback processing.

Zero-LLM pipeline: reads signal_buffer for news_feedback entries,
matches user feedback against today's news_snippets entities,
detects sentiment from context, and updates entity_feedback_weights.

Design:
- Pure keyword + substring matching. No embeddings, no API calls.
- Runs post-session in cmd_observe().
- Weight decay applied at read time by indexer.get_entity_feedback_weights().
"""

from __future__ import annotations


POSITIVE_KEYWORDS = {"不错", "好", "很好", "很棒", "喜欢", "有意思", "精彩", "详细", "深度", "值得", "推荐"}
NEGATIVE_KEYWORDS = {"太浅", "浅", "差", "不好", "无聊", "没意思", "太长", "太短", "不详细", "一般"}
SENTIMENT_ALL = POSITIVE_KEYWORDS | NEGATIVE_KEYWORDS


def process_feedback(db, date: str) -> int:
    """Main entry: process all pending news_feedback signals for a given date.

    Args:
        db: HarnessDB instance
        date: YYYY-MM-DD string for today's news

    Returns:
        Number of entity-weight matches found
    """
    # Read any uncategorized news_feedback entries from signal_buffer
    try:
        cur = db._conn.execute(
            "SELECT content FROM signal_buffer WHERE signal_type='news_feedback'"
        )
        feedback_messages = [row[0] for row in cur.fetchall() if row[0]]
    except Exception:
        return 0

    if not feedback_messages:
        return 0

    # Collect today's entities from news_snippets
    try:
        cur = db._conn.execute(
            "SELECT entities FROM news_snippets WHERE date=? AND entities IS NOT NULL",
            (date,),
        )
        all_entities: set[str] = set()
        for (entities_json,) in cur.fetchall():
            if entities_json:
                import json as _json
                try:
                    for e in _json.loads(entities_json):
                        if len(e) >= 2:
                            all_entities.add(e)
                except Exception:
                    pass
    except Exception:
        return 0

    if not all_entities:
        return 0

    total_matches = 0
    for msg in feedback_messages:
        matches = _match_feedback_to_entities(msg, all_entities)
        if matches:
            _update_entity_weights(db, matches)
            total_matches += len(matches)

    return total_matches


def _match_feedback_to_entities(message: str, entities: set[str]) -> list[tuple[str, int]]:
    """Match entity substrings in a feedback message, detecting sentiment.

    Entities are sorted longest-first to avoid "AI" matching before "AI芯片".
    Returns [(entity, sentiment), ...] where sentiment is +1, -1, or 0.
    """
    msg_lower = message.lower()
    matches = []

    for entity in sorted(entities, key=len, reverse=True):
        if entity.lower() in msg_lower:
            sentiment = _detect_sentiment(message, entity)
            matches.append((entity, sentiment))

    return matches


def _detect_sentiment(message: str, entity: str) -> int:
    """Check ~20 chars after the entity in message for sentiment keywords.

    Returns +1 (positive), -1 (negative), or 0 (neutral / no signal).
    """
    idx = message.lower().find(entity.lower())
    if idx == -1:
        return 0

    # Look at context after the entity (+10 to +30 chars)
    context = message[idx + len(entity):idx + len(entity) + 30]

    for pw in POSITIVE_KEYWORDS:
        if pw in context:
            return 1
    for nw in NEGATIVE_KEYWORDS:
        if nw in context:
            return -1

    return 0


def _update_entity_weights(db, matches: list[tuple[str, int]]):
    """Apply weight deltas to entity_feedback_weights table with clamping.

    Positive sentiment: +0.15, Negative: -0.10.
    Weights clamped to [-0.50, +0.50].
    """
    import time as _time
    now = _time.time()
    for entity, sentiment in matches:
        if sentiment == 0:
            continue
        delta = 0.15 if sentiment > 0 else -0.10
        db.upsert_entity_feedback_weight(entity, delta, sentiment, now)


# ── CLI test entry ──

def main():
    import sys
    from indexer import HarnessDB

    db = HarnessDB()
    if len(sys.argv) >= 3 and sys.argv[1] == "--test":
        msg = sys.argv[2]
        date = sys.argv[3] if len(sys.argv) > 3 else "2026-05-24"
        messages = msg.split(";")
        for m in messages:
            # Simulate: inject directly into signal_buffer, then process
            db._conn.execute(
                "INSERT INTO signal_buffer(signal_type, content) VALUES(?,?)",
                ("news_feedback", m.strip()),
            )
            db._conn.commit()
        n = process_feedback(db, date)
        print(f"Processed {len(messages)} feedback(s): {n} weight matches")
        cur = db._conn.execute(
            "SELECT entity, weight, positive_count, negative_count "
            "FROM entity_feedback_weights ORDER BY ABS(weight) DESC"
        )
        for row in cur.fetchall():
            print(f"  {row[0]}: weight={row[1]:+.2f} (+{row[2]}/-{row[3]})")
    else:
        print("Usage: python feedback_learner.py --test \"Agent不错, 芯片太浅\" [date]")
    db.close()


if __name__ == "__main__":
    main()
