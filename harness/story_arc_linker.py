"""故事弧线链接器 —— 对每条候选标题，cosine 查最近 7 天指纹库，输出跨日语义关联。"""
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np

DEFAULT_DB = Path.home() / "Documents" / "Obsidian Vault" / "claude专属文件夹" / "news" / "fingerprints.db"


def link(candidates: list[str], db_path: str | Path, cos_threshold: float = 0.75):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from encoder import get_encoder

    enc = get_encoder()

    con = sqlite3.connect(str(db_path))
    rows = [(r[0], r[1]) for r in con.execute(
        "SELECT title_preview, first_seen FROM fingerprints ORDER BY first_seen DESC"
    )]
    today = max(r[1] for r in rows) if rows else ""
    con.close()

    known_texts = [r[0] for r in rows]
    known_dates = [r[1] for r in rows]
    known_embs = np.array([enc(t) for t in known_texts])
    known_embs = known_embs / (np.linalg.norm(known_embs, axis=1, keepdims=True) + 1e-8)

    arcs: list[tuple[str, str, str, float]] = []
    for title in candidates:
        v = np.array(enc(title))
        v = v / (np.linalg.norm(v) + 1e-8)
        sims = known_embs @ v
        for j, s in enumerate(sims):
            if float(s) >= cos_threshold and known_dates[j] != today and known_texts[j][:40] != title[:40]:
                arcs.append((title, known_texts[j], known_dates[j], float(s)))
                break  # best cross-day match only
    return arcs


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python story_arc_linker.py <candidates_file> [db_path]")
        sys.exit(1)

    candidates_file = sys.argv[1]
    db_path = sys.argv[2] if len(sys.argv) > 2 else str(DEFAULT_DB)

    with open(candidates_file, encoding="utf-8") as f:
        candidates = [line.strip() for line in f if line.strip()]

    arcs = link(candidates, db_path)

    if arcs:
        for new, old, date, score in arcs:
            print(f"↗ 接 [{date}] {old[:50]}  (cos={score:.3f})")
            print(f"   → {new[:60]}")
            print()
    else:
        print("No story arcs found — all titles are new narrative starts")
