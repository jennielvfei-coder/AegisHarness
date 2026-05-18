"""Indexer — SQLite FTS5-backed storage for observations, fragments, and skill index.

Design borrowed from Hermes hermes_state.py SessionDB:
  - WAL mode for concurrent reads
  - FTS5 virtual table for text search
  - Schema versioning for migration
"""

import json
import sqlite3
import threading
from pathlib import Path
from typing import Optional

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    action TEXT NOT NULL,
    confidence REAL NOT NULL,
    reason TEXT,
    summary TEXT,
    tags TEXT,
    skill_name TEXT,
    processed_at REAL NOT NULL,
    created_at REAL NOT NULL DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS idx_observations_action ON observations(action);
CREATE INDEX IF NOT EXISTS idx_observations_processed ON observations(processed_at DESC);

CREATE TABLE IF NOT EXISTS fragments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag TEXT NOT NULL,
    trigger_phrases TEXT,
    content TEXT NOT NULL,
    source_session TEXT,
    confidence REAL DEFAULT 0.5,
    hit_count INTEGER DEFAULT 0,
    last_hit REAL,
    created_at REAL NOT NULL DEFAULT (unixepoch()),
    updated_at REAL
);
CREATE VIRTUAL TABLE IF NOT EXISTS fragments_fts USING fts5(
    tag, trigger_phrases, content, content=fragments, content_rowid=id
);

CREATE TRIGGER IF NOT EXISTS fragments_fts_insert AFTER INSERT ON fragments BEGIN
    INSERT INTO fragments_fts(rowid, tag, trigger_phrases, content)
    VALUES (new.id, new.tag, new.trigger_phrases, new.content);
END;

CREATE TRIGGER IF NOT EXISTS fragments_fts_delete AFTER DELETE ON fragments BEGIN
    INSERT INTO fragments_fts(fragments_fts, rowid, tag, trigger_phrases, content)
    VALUES ('delete', old.id, old.tag, old.trigger_phrases, old.content);
END;

CREATE TABLE IF NOT EXISTS skill_index (
    name TEXT PRIMARY KEY,
    file_path TEXT NOT NULL,
    tags TEXT,
    trigger_patterns TEXT,
    version INTEGER DEFAULT 1,
    harness_confidence REAL DEFAULT 0.5,
    created_at REAL NOT NULL DEFAULT (unixepoch()),
    updated_at REAL,
    usage_count INTEGER DEFAULT 0,
    last_used REAL
);

CREATE TABLE IF NOT EXISTS evolution_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name TEXT,
    action TEXT,
    source_session TEXT,
    change_summary TEXT,
    old_version INTEGER,
    new_version INTEGER,
    timestamp REAL NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS session_meta (
    id TEXT PRIMARY KEY,
    title TEXT,
    tags TEXT,
    observation_action TEXT,
    processed_at REAL NOT NULL DEFAULT (unixepoch())
);
"""


class HarnessDB:
    """SQLite-backed storage for harness observations and indices."""

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            db_path = Path(__file__).resolve().parent / "state.db"
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, timeout=5.0
        )
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._init_schema()

    def _init_schema(self):
        with self._lock:
            self._conn.executescript(SCHEMA_SQL)
            cur = self._conn.execute(
                "SELECT version FROM schema_version"
            )
            row = cur.fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO schema_version(version) VALUES (?)",
                    (SCHEMA_VERSION,)
                )
            self._conn.commit()

    def save_observation(self, report) -> int:
        """Save an ObservationReport to the database.

        Args:
            report: ObservationReport from observer.analyze_session()

        Returns:
            The row ID of the inserted observation.
        """
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO observations
                   (session_id, action, confidence, reason, summary, tags, skill_name, processed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, unixepoch())""",
                (
                    report.session_id,
                    report.action,
                    report.confidence,
                    report.reason,
                    report.summary,
                    json.dumps(report.tags, ensure_ascii=False),
                    report.skill_name,
                ),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_recent_observations(self, limit: int = 10) -> list:
        """Get the most recent observations for inspection."""
        with self._lock:
            cur = self._conn.execute(
                """SELECT session_id, action, confidence, reason, tags, processed_at
                   FROM observations
                   ORDER BY processed_at DESC
                   LIMIT ?""",
                (limit,),
            )
            return [
                {
                    "session_id": row[0],
                    "action": row[1],
                    "confidence": row[2],
                    "reason": row[3],
                    "tags": json.loads(row[4]) if row[4] else [],
                    "processed_at": row[5],
                }
                for row in cur.fetchall()
            ]

    def get_stats(self) -> dict:
        """Return summary statistics for dashboard."""
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) FROM observations"
            ).fetchone()[0]
            by_action = {}
            for row in self._conn.execute(
                "SELECT action, COUNT(*) FROM observations GROUP BY action"
            ):
                by_action[row[0]] = row[1]
            return {"total_observations": total, "by_action": by_action}

    def close(self):
        self._conn.close()
