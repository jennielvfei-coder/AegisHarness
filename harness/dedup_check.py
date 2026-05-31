"""日报写入前去重检查 —— 从 SQLite 指纹库读取已知标题做模糊匹配。"""
import sqlite3
import sys
from difflib import SequenceMatcher
from pathlib import Path

THRESHOLD = 0.7
LOOKBACK_DAYS = 7
DEFAULT_DB = Path.home() / "Documents" / "Obsidian Vault" / "claude专属文件夹" / "news" / "fingerprints.db"


def check(candidates: list[str], db_path: str | Path, threshold: float = THRESHOLD):
    con = sqlite3.connect(str(db_path))
    cur = con.execute(
        "SELECT title_preview, first_seen FROM fingerprints ORDER BY first_seen DESC"
    )
    known = [(r[0], r[1]) for r in cur.fetchall()]
    con.close()

    hits: list[tuple[str, str, str, float]] = []
    for title in candidates:
        for preview, date in known:
            score = SequenceMatcher(None, title, preview).ratio()
            if score >= threshold:
                hits.append((title, preview, date, score))
                break  # one match per candidate is enough
    return hits


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python dedup_check.py <candidates_file> [db_path]")
        sys.exit(1)

    candidates_file = sys.argv[1]
    db_path = sys.argv[2] if len(sys.argv) > 2 else str(DEFAULT_DB)

    with open(candidates_file, encoding="utf-8") as f:
        candidates = [line.strip() for line in f if line.strip()]

    hits = check(candidates, db_path)

    for title, old, date, score in hits:
        print(f"REPEAT [{score:.0%}]: {title[:60]}  ≈  {old[:60]}  ({date})")

    if not hits:
        print("OK — all titles pass dedup")
    else:
        print(f"\n{hits.__len__()} duplicate(s) found — replace or tag [追踪]")
