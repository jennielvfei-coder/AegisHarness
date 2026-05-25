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

SCHEMA_VERSION = 7

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
    updated_at REAL,
    embedding BLOB DEFAULT NULL,    -- Phase 4: vector embedding, NULL until Chroma migration
    fragment_type TEXT DEFAULT 'knowledge',  -- v2: 'knowledge', 'failure_pattern', 'preflight_check'
    skill_name TEXT DEFAULT NULL     -- v2: associated skill name for failure_pattern injection
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

CREATE TABLE IF NOT EXISTS constraints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    tool_name TEXT NOT NULL,        -- 'WebFetch' | 'WebSearch' | 'Bash' | '*'
    match_pattern TEXT NOT NULL,    -- substring match on tool input
    action TEXT NOT NULL DEFAULT 'block',  -- 'block' | 'warn'
    message TEXT NOT NULL,           -- injected when constraint triggers
    source_session TEXT,
    violation_count INTEGER DEFAULT 0,
    max_violations INTEGER DEFAULT 5,
    created_at REAL NOT NULL DEFAULT (unixepoch()),
    expires_at REAL,
    active INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_constraints_active ON constraints(active, expires_at);

-- v4: Meta-Theory of Mind tables
CREATE TABLE IF NOT EXISTS fusion_sessions (
    session_id TEXT PRIMARY KEY,
    fusion_vector TEXT NOT NULL,
    alphas TEXT NOT NULL,
    attention_distribution TEXT,
    continuity_score REAL DEFAULT 0.0,
    session_quality REAL DEFAULT 0.0,
    created_at REAL NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS belief_traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    belief_type TEXT NOT NULL,
    confidence REAL NOT NULL,
    evidence TEXT,
    recommended_action TEXT,
    was_correct INTEGER DEFAULT 0,
    escalation_blocked_reason TEXT DEFAULT NULL,
    created_at REAL NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_belief_traces_session ON belief_traces(session_id);
CREATE INDEX IF NOT EXISTS idx_belief_traces_type ON belief_traces(belief_type);

CREATE TABLE IF NOT EXISTS false_belief_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    belief_type TEXT NOT NULL,
    tool_name TEXT,
    match_pattern TEXT,
    escalated_to_constraint INTEGER DEFAULT 0,
    created_at REAL NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS interaction_pairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    user_message TEXT NOT NULL,
    user_message_embedding TEXT,
    claude_actions TEXT NOT NULL,
    outcome TEXT NOT NULL,
    goal_type TEXT,
    goal_type_verified INTEGER DEFAULT 0,
    created_at REAL NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_interaction_pairs_outcome ON interaction_pairs(outcome);
CREATE INDEX IF NOT EXISTS idx_interaction_pairs_session ON interaction_pairs(session_id);

CREATE TABLE IF NOT EXISTS embedding_cache (
    source_type TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    embedding TEXT NOT NULL,
    created_at REAL NOT NULL DEFAULT (unixepoch()),
    PRIMARY KEY (source_type, content_hash)
);

CREATE TABLE IF NOT EXISTS belief_classifier_weights (
    feature_name TEXT PRIMARY KEY,
    weight REAL NOT NULL,
    updated_at REAL NOT NULL DEFAULT (unixepoch())
);

-- v7: News workflow optimization tables
CREATE TABLE IF NOT EXISTS news_snippets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    section TEXT NOT NULL,
    headline TEXT NOT NULL,
    summary TEXT,
    entities TEXT,
    sources TEXT,
    source_rating TEXT,
    content_hash TEXT UNIQUE,
    embedding TEXT,
    created_at REAL NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_news_snippets_date ON news_snippets(date);
CREATE INDEX IF NOT EXISTS idx_news_snippets_hash ON news_snippets(content_hash);

CREATE TABLE IF NOT EXISTS feature_library_entries (
    feature_id TEXT PRIMARY KEY,
    layer TEXT NOT NULL,
    category TEXT NOT NULL,
    name_cn TEXT NOT NULL,
    definition TEXT,
    examples TEXT,
    typical_implication TEXT,
    embedding TEXT,
    checksum TEXT,
    created_at REAL NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS feature_library_versions (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    checksum TEXT NOT NULL,
    entry_count INTEGER,
    full_snapshot TEXT,
    created_at REAL NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS feature_activations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    feature_id TEXT NOT NULL,
    activation_strength REAL,
    matched_entity_combos TEXT,
    matched_library_features TEXT,
    source_snippet_ids TEXT,
    created_at REAL NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_feature_activations_date ON feature_activations(date);
CREATE INDEX IF NOT EXISTS idx_feature_activations_feature ON feature_activations(feature_id);

CREATE TABLE IF NOT EXISTS entity_feedback_weights (
    entity TEXT PRIMARY KEY,
    weight REAL NOT NULL DEFAULT 0.0,
    positive_count INTEGER DEFAULT 0,
    negative_count INTEGER DEFAULT 0,
    last_updated REAL NOT NULL DEFAULT (unixepoch()),
    created_at REAL NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS judgment_entries (
    entry_id TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    entry_type TEXT NOT NULL,
    label TEXT NOT NULL,
    statement TEXT NOT NULL,
    verdict TEXT,
    anchors TEXT,
    verifiable_signals TEXT,
    window_days INTEGER,
    surface_contradiction TEXT,
    underlying_cause TEXT,
    intuition TEXT,
    probability REAL,
    prob_range_low REAL,
    prob_range_high REAL,
    trend TEXT,
    entities TEXT,
    created_at REAL NOT NULL DEFAULT (unixepoch()),
    last_updated REAL
);
CREATE INDEX IF NOT EXISTS idx_judgment_entries_date ON judgment_entries(date);
CREATE INDEX IF NOT EXISTS idx_judgment_entries_type ON judgment_entries(entry_type);

CREATE TABLE IF NOT EXISTS judgment_links (
    link_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_entry_id TEXT NOT NULL,
    target_entry_id TEXT NOT NULL,
    link_type TEXT NOT NULL,
    shared_entities TEXT,
    jaccard_score REAL,
    created_at REAL NOT NULL DEFAULT (unixepoch()),
    FOREIGN KEY (source_entry_id) REFERENCES judgment_entries(entry_id),
    FOREIGN KEY (target_entry_id) REFERENCES judgment_entries(entry_id)
);
CREATE INDEX IF NOT EXISTS idx_jlinks_source ON judgment_links(source_entry_id);
CREATE INDEX IF NOT EXISTS idx_jlinks_target ON judgment_links(target_entry_id);

CREATE TABLE IF NOT EXISTS judgment_status_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id TEXT NOT NULL,
    date TEXT NOT NULL,
    previous_verdict TEXT,
    new_verdict TEXT,
    previous_probability REAL,
    new_probability REAL,
    evidence_summary TEXT,
    created_at REAL NOT NULL DEFAULT (unixepoch()),
    FOREIGN KEY (entry_id) REFERENCES judgment_entries(entry_id)
);

CREATE TABLE IF NOT EXISTS news_attention_injections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    layer TEXT NOT NULL,
    injection_text TEXT NOT NULL,
    feature_ids TEXT,
    weight_boost_applied INTEGER DEFAULT 0,
    budget_used INTEGER,
    budget_total INTEGER,
    created_at REAL NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS search_topic_weights (
    topic_name TEXT PRIMARY KEY,
    weight REAL NOT NULL DEFAULT 1.0,
    hit_count INTEGER NOT NULL DEFAULT 0,
    miss_streak INTEGER NOT NULL DEFAULT 0,
    last_produced_date TEXT,
    updated_at REAL NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS coactivation_pairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feature_id_a TEXT NOT NULL,
    feature_id_b TEXT NOT NULL,
    pearson_r REAL,
    cooccurrence_count INTEGER DEFAULT 0,
    verification_query TEXT,
    confidence REAL DEFAULT 0.5,
    last_verified TEXT,
    created_at REAL NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS hypotheses (
    hypothesis_id TEXT PRIMARY KEY,
    parent_id TEXT,
    anomaly_feature_id TEXT,
    statement TEXT NOT NULL,
    competing_alternatives TEXT,
    contrastive_tests TEXT,
    metric_scores TEXT,
    aggregate_rank REAL,
    status TEXT DEFAULT 'seeded',
    iteration_count INTEGER DEFAULT 0,
    causal_chain TEXT,
    created_at REAL NOT NULL DEFAULT (unixepoch()),
    last_evaluated REAL
);

CREATE TABLE IF NOT EXISTS false_structures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    description TEXT,
    filter_rule TEXT,
    filter_reason TEXT,
    supporting_snippet_ids TEXT,
    created_at REAL NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS icl_reports (
    date TEXT PRIMARY KEY,
    input_count INTEGER,
    tier1_count INTEGER,
    tier2_count INTEGER,
    tier3_count INTEGER,
    compression_ratio REAL,
    tier1_items TEXT,
    tier2_items TEXT,
    created_at REAL NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS dcl_judgments (
    judgment_id TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    judgment TEXT NOT NULL,
    confidence REAL,
    disruptiveness REAL,
    supporting_hypotheses TEXT,
    counterfactual TEXT,
    action_implication TEXT,
    verification_window TEXT,
    causal_radius INTEGER,
    page_rank REAL,
    created_at REAL NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_dcl_judgments_date ON dcl_judgments(date);

CREATE TABLE IF NOT EXISTS feature_proposals (
    proposal_id TEXT PRIMARY KEY,
    suggested_name TEXT,
    suggested_layer TEXT,
    suggested_definition TEXT,
    supporting_evidence TEXT,
    related_existing_features TEXT,
    confidence REAL,
    status TEXT DEFAULT 'pending_review',
    created_at REAL NOT NULL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS meta_store (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL DEFAULT (unixepoch())
);
"""

MIGRATIONS = {
    2: """
ALTER TABLE fragments ADD COLUMN fragment_type TEXT DEFAULT 'knowledge';
ALTER TABLE fragments ADD COLUMN skill_name TEXT DEFAULT NULL;
""",
    3: """
CREATE TABLE IF NOT EXISTS constraints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    match_pattern TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT 'block',
    message TEXT NOT NULL,
    source_session TEXT,
    violation_count INTEGER DEFAULT 0,
    max_violations INTEGER DEFAULT 5,
    created_at REAL NOT NULL DEFAULT (unixepoch()),
    expires_at REAL,
    active INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_constraints_active ON constraints(active, expires_at);
""",
    4: """
CREATE TABLE IF NOT EXISTS fusion_sessions (
    session_id TEXT PRIMARY KEY,
    fusion_vector TEXT NOT NULL,
    alphas TEXT NOT NULL,
    attention_distribution TEXT,
    continuity_score REAL DEFAULT 0.0,
    session_quality REAL DEFAULT 0.0,
    created_at REAL NOT NULL DEFAULT (unixepoch())
);
CREATE TABLE IF NOT EXISTS belief_traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    belief_type TEXT NOT NULL,
    confidence REAL NOT NULL,
    evidence TEXT,
    recommended_action TEXT,
    was_correct INTEGER DEFAULT 0,
    escalation_blocked_reason TEXT DEFAULT NULL,
    created_at REAL NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_belief_traces_session ON belief_traces(session_id);
CREATE INDEX IF NOT EXISTS idx_belief_traces_type ON belief_traces(belief_type);
CREATE TABLE IF NOT EXISTS false_belief_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    belief_type TEXT NOT NULL,
    tool_name TEXT,
    match_pattern TEXT,
    escalated_to_constraint INTEGER DEFAULT 0,
    created_at REAL NOT NULL DEFAULT (unixepoch())
);
CREATE TABLE IF NOT EXISTS interaction_pairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    user_message TEXT NOT NULL,
    user_message_embedding TEXT,
    claude_actions TEXT NOT NULL,
    outcome TEXT NOT NULL,
    goal_type TEXT,
    goal_type_verified INTEGER DEFAULT 0,
    created_at REAL NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_interaction_pairs_outcome ON interaction_pairs(outcome);
CREATE INDEX IF NOT EXISTS idx_interaction_pairs_session ON interaction_pairs(session_id);
CREATE TABLE IF NOT EXISTS embedding_cache (
    source_type TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    embedding TEXT NOT NULL,
    created_at REAL NOT NULL DEFAULT (unixepoch()),
    PRIMARY KEY (source_type, content_hash)
);
CREATE TABLE IF NOT EXISTS belief_classifier_weights (
    feature_name TEXT PRIMARY KEY,
    weight REAL NOT NULL,
    updated_at REAL NOT NULL DEFAULT (unixepoch())
);
""",
    5: """
ALTER TABLE interaction_pairs ADD COLUMN goal_type_verified INTEGER DEFAULT 0;
""",
    6: """
ALTER TABLE belief_traces ADD COLUMN escalation_blocked_reason TEXT DEFAULT NULL;
""",
    7: """
CREATE TABLE IF NOT EXISTS news_snippets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    section TEXT NOT NULL,
    headline TEXT NOT NULL,
    summary TEXT,
    entities TEXT,
    sources TEXT,
    source_rating TEXT,
    content_hash TEXT UNIQUE,
    embedding TEXT,
    created_at REAL NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_news_snippets_date ON news_snippets(date);
CREATE INDEX IF NOT EXISTS idx_news_snippets_hash ON news_snippets(content_hash);
CREATE TABLE IF NOT EXISTS feature_library_entries (
    feature_id TEXT PRIMARY KEY,
    layer TEXT NOT NULL,
    category TEXT NOT NULL,
    name_cn TEXT NOT NULL,
    definition TEXT,
    examples TEXT,
    typical_implication TEXT,
    embedding TEXT,
    checksum TEXT,
    created_at REAL NOT NULL DEFAULT (unixepoch())
);
CREATE TABLE IF NOT EXISTS feature_library_versions (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    checksum TEXT NOT NULL,
    entry_count INTEGER,
    full_snapshot TEXT,
    created_at REAL NOT NULL DEFAULT (unixepoch())
);
CREATE TABLE IF NOT EXISTS feature_activations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    feature_id TEXT NOT NULL,
    activation_strength REAL,
    matched_entity_combos TEXT,
    matched_library_features TEXT,
    source_snippet_ids TEXT,
    created_at REAL NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_feature_activations_date ON feature_activations(date);
CREATE INDEX IF NOT EXISTS idx_feature_activations_feature ON feature_activations(feature_id);
CREATE TABLE IF NOT EXISTS news_attention_injections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    layer TEXT NOT NULL,
    injection_text TEXT NOT NULL,
    feature_ids TEXT,
    weight_boost_applied INTEGER DEFAULT 0,
    budget_used INTEGER,
    budget_total INTEGER,
    created_at REAL NOT NULL DEFAULT (unixepoch())
);
CREATE TABLE IF NOT EXISTS search_topic_weights (
    topic_name TEXT PRIMARY KEY,
    weight REAL NOT NULL DEFAULT 1.0,
    hit_count INTEGER NOT NULL DEFAULT 0,
    miss_streak INTEGER NOT NULL DEFAULT 0,
    last_produced_date TEXT,
    updated_at REAL NOT NULL DEFAULT (unixepoch())
);
CREATE TABLE IF NOT EXISTS coactivation_pairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feature_id_a TEXT NOT NULL,
    feature_id_b TEXT NOT NULL,
    pearson_r REAL,
    cooccurrence_count INTEGER DEFAULT 0,
    verification_query TEXT,
    confidence REAL DEFAULT 0.5,
    last_verified TEXT,
    created_at REAL NOT NULL DEFAULT (unixepoch())
);
CREATE TABLE IF NOT EXISTS hypotheses (
    hypothesis_id TEXT PRIMARY KEY,
    parent_id TEXT,
    anomaly_feature_id TEXT,
    statement TEXT NOT NULL,
    competing_alternatives TEXT,
    contrastive_tests TEXT,
    metric_scores TEXT,
    aggregate_rank REAL,
    status TEXT DEFAULT 'seeded',
    iteration_count INTEGER DEFAULT 0,
    causal_chain TEXT,
    created_at REAL NOT NULL DEFAULT (unixepoch()),
    last_evaluated REAL
);
CREATE TABLE IF NOT EXISTS false_structures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    description TEXT,
    filter_rule TEXT,
    filter_reason TEXT,
    supporting_snippet_ids TEXT,
    created_at REAL NOT NULL DEFAULT (unixepoch())
);
CREATE TABLE IF NOT EXISTS icl_reports (
    date TEXT PRIMARY KEY,
    input_count INTEGER,
    tier1_count INTEGER,
    tier2_count INTEGER,
    tier3_count INTEGER,
    compression_ratio REAL,
    tier1_items TEXT,
    tier2_items TEXT,
    created_at REAL NOT NULL DEFAULT (unixepoch())
);
CREATE TABLE IF NOT EXISTS dcl_judgments (
    judgment_id TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    judgment TEXT NOT NULL,
    confidence REAL,
    disruptiveness REAL,
    supporting_hypotheses TEXT,
    counterfactual TEXT,
    action_implication TEXT,
    verification_window TEXT,
    causal_radius INTEGER,
    page_rank REAL,
    created_at REAL NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_dcl_judgments_date ON dcl_judgments(date);
CREATE TABLE IF NOT EXISTS feature_proposals (
    proposal_id TEXT PRIMARY KEY,
    suggested_name TEXT,
    suggested_layer TEXT,
    suggested_definition TEXT,
    supporting_evidence TEXT,
    related_existing_features TEXT,
    confidence REAL,
    status TEXT DEFAULT 'pending_review',
    created_at REAL NOT NULL DEFAULT (unixepoch())
);
CREATE TABLE IF NOT EXISTS meta_store (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL DEFAULT (unixepoch())
);
""",
}


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
            else:
                current = row[0]
                self._run_migrations(current)
            self._conn.commit()

    def _run_migrations(self, current_version: int):
        """Run schema migrations from current_version to SCHEMA_VERSION."""
        for v in range(current_version + 1, SCHEMA_VERSION + 1):
            sql = MIGRATIONS.get(v)
            if sql:
                print(f"[indexer] Running migration v{v}...")
                try:
                    self._conn.executescript(sql)
                    self._conn.execute(
                        "UPDATE schema_version SET version = ?", (v,)
                    )
                except Exception as e:
                    print(f"[indexer] Migration v{v} failed (may already be applied): {e}")

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

    # ── v4: Meta-Theory of Mind accessors ─────────────────────────────────

    def get_latest_fusion(self) -> dict | None:
        """Return the most recent fusion session row."""
        with self._lock:
            cur = self._conn.execute(
                """SELECT session_id, fusion_vector, alphas, attention_distribution,
                          continuity_score, session_quality
                   FROM fusion_sessions
                   ORDER BY created_at DESC LIMIT 1"""
            )
            row = cur.fetchone()
            if row is None:
                return None
            return {
                "session_id": row[0],
                "fusion_vector": json.loads(row[1]),
                "alphas": json.loads(row[2]),
                "attention_distribution": json.loads(row[3]) if row[3] else None,
                "continuity_score": row[4],
                "session_quality": row[5],
            }

    def save_fusion_session(
        self,
        session_id: str,
        fusion_vector: list[float],
        alphas: dict[str, float],
        attention_distribution: dict[str, float] | None = None,
        continuity_score: float = 0.0,
        session_quality: float = 0.0,
    ) -> int:
        """Insert a fusion session row."""
        with self._lock:
            cur = self._conn.execute(
                """INSERT OR REPLACE INTO fusion_sessions
                   (session_id, fusion_vector, alphas, attention_distribution,
                    continuity_score, session_quality)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    json.dumps(fusion_vector),
                    json.dumps(alphas),
                    json.dumps(attention_distribution) if attention_distribution else None,
                    continuity_score,
                    session_quality,
                ),
            )
            self._conn.commit()
            return cur.lastrowid

    def save_belief_trace(
        self,
        session_id: str,
        belief_type: str,
        confidence: float,
        evidence: str = "",
        recommended_action: str = "",
        escalation_blocked_reason: str = "",
    ) -> int:
        """Insert a belief trace row."""
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO belief_traces
                   (session_id, belief_type, confidence, evidence, recommended_action,
                    escalation_blocked_reason)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, belief_type, confidence, evidence, recommended_action,
                 escalation_blocked_reason or None),
            )
            self._conn.commit()
            return cur.lastrowid

    def save_false_belief(
        self,
        session_id: str,
        belief_type: str,
        tool_name: str = "",
        match_pattern: str = "",
    ) -> int:
        """Insert a false belief log entry."""
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO false_belief_log
                   (session_id, belief_type, tool_name, match_pattern)
                   VALUES (?, ?, ?, ?)""",
                (session_id, belief_type, tool_name, match_pattern),
            )
            self._conn.commit()
            return cur.lastrowid

    def save_interaction_pair(
        self,
        session_id: str,
        user_message: str,
        claude_actions: list[dict],
        outcome: str,
        user_message_embedding: list[float] | None = None,
        goal_type: str = "",
        goal_type_verified: bool = False,
    ) -> int:
        """Insert an interaction pair for Psi training."""
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO interaction_pairs
                   (session_id, user_message, user_message_embedding,
                    claude_actions, outcome, goal_type, goal_type_verified)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    user_message[:2000],
                    json.dumps(user_message_embedding) if user_message_embedding else None,
                    json.dumps(claude_actions, ensure_ascii=False),
                    outcome,
                    goal_type,
                    1 if goal_type_verified else 0,
                ),
            )
            self._conn.commit()
            return cur.lastrowid

    def verify_interaction_pair_goal(self, pair_id: int, goal_type: str) -> None:
        """Mark an interaction pair's goal_type as verified by session outcome."""
        with self._lock:
            self._conn.execute(
                "UPDATE interaction_pairs SET goal_type = ?, goal_type_verified = 1 WHERE id = ?",
                (goal_type, pair_id),
            )
            self._conn.commit()

    def get_interaction_pairs(self, outcome: str | None = None, limit: int = 100) -> list[dict]:
        """Retrieve interaction pairs, optionally filtered by outcome."""
        with self._lock:
            if outcome:
                cur = self._conn.execute(
                    """SELECT id, session_id, user_message, user_message_embedding,
                              claude_actions, outcome, goal_type
                       FROM interaction_pairs
                       WHERE outcome = ?
                       ORDER BY created_at DESC
                       LIMIT ?""",
                    (outcome, limit),
                )
            else:
                cur = self._conn.execute(
                    """SELECT id, session_id, user_message, user_message_embedding,
                              claude_actions, outcome, goal_type
                       FROM interaction_pairs
                       ORDER BY created_at DESC
                       LIMIT ?""",
                    (limit,),
                )
            return [
                {
                    "id": row[0],
                    "session_id": row[1],
                    "user_message": row[2],
                    "user_message_embedding": json.loads(row[3]) if row[3] else None,
                    "claude_actions": json.loads(row[4]) if row[4] else [],
                    "outcome": row[5],
                    "goal_type": row[6],
                }
                for row in cur.fetchall()
            ]

    def get_embedding_cache(self, source_type: str, content_hash: str) -> list[float] | None:
        """Get cached embedding or None."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT embedding FROM embedding_cache WHERE source_type = ? AND content_hash = ?",
                (source_type, content_hash),
            )
            row = cur.fetchone()
            return json.loads(row[0]) if row else None

    def set_embedding_cache(self, source_type: str, content_hash: str, embedding: list[float]) -> None:
        """Cache an embedding vector."""
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO embedding_cache
                   (source_type, content_hash, embedding, created_at)
                   VALUES (?, ?, ?, unixepoch())""",
                (source_type, content_hash, json.dumps(embedding)),
            )
            self._conn.commit()

    def get_successful_sessions(self, min_quality: float = 0.7, limit: int = 50) -> list[dict]:
        """Retrieve high-quality fusion sessions for nearest-neighbor lookup."""
        with self._lock:
            cur = self._conn.execute(
                """SELECT session_id, fusion_vector, alphas, session_quality
                   FROM fusion_sessions
                   WHERE session_quality >= ?
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (min_quality, limit),
            )
            return [
                {
                    "session_id": row[0],
                    "fusion_vector": json.loads(row[1]),
                    "alphas": json.loads(row[2]),
                    "session_quality": row[3],
                }
                for row in cur.fetchall()
            ]

    # ── meta_store helpers ────────────────────────────────────────────────

    def get_meta(self, key: str) -> str | None:
        """Read a value from meta_store."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT value FROM meta_store WHERE key = ?", (key,)
            )
            row = cur.fetchone()
            return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        """Write a value to meta_store."""
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO meta_store (key, value, updated_at)
                   VALUES (?, ?, unixepoch())""",
                (key, value),
            )
            self._conn.commit()

    # ── v7: News pipeline accessors ────────────────────────────────────────

    def save_news_snippet(self, date: str, section: str, headline: str,
                          summary: str, entities: list[str], sources: list[dict],
                          source_rating: str, content_hash: str,
                          embedding: list[float]) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO news_snippets
                   (date, section, headline, summary, entities, sources,
                    source_rating, content_hash, embedding)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (date, section, headline, summary,
                 json.dumps(entities, ensure_ascii=False),
                 json.dumps(sources, ensure_ascii=False),
                 source_rating, content_hash, json.dumps(embedding)),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_news_snippets(self, days: int = 30, date: str | None = None) -> list[dict]:
        with self._lock:
            if date:
                cur = self._conn.execute(
                    """SELECT id, date, section, headline, summary, entities,
                              sources, source_rating, content_hash, embedding
                       FROM news_snippets WHERE date = ?""",
                    (date,),
                )
            else:
                cur = self._conn.execute(
                    """SELECT id, date, section, headline, summary, entities,
                              sources, source_rating, content_hash, embedding
                       FROM news_snippets
                       WHERE date >= date('now', ? || ' days')
                       ORDER BY date DESC""",
                    (f"-{days}",),
                )
            return [
                {
                    "id": row[0], "date": row[1], "section": row[2],
                    "headline": row[3], "summary": row[4],
                    "entities": json.loads(row[5]) if row[5] else [],
                    "sources": json.loads(row[6]) if row[6] else [],
                    "source_rating": row[7], "content_hash": row[8],
                    "embedding": json.loads(row[9]) if row[9] else None,
                }
                for row in cur.fetchall()
            ]

    def save_feature_library_entries(self, entries: list[dict]) -> None:
        with self._lock:
            for e in entries:
                self._conn.execute(
                    """INSERT OR REPLACE INTO feature_library_entries
                       (feature_id, layer, category, name_cn, definition,
                        examples, typical_implication, embedding, checksum)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (e["feature_id"], e["layer"], e["category"], e["name_cn"],
                     e.get("definition", ""), e.get("examples", ""),
                     e.get("typical_implication", ""),
                     json.dumps(e["embedding"]) if e.get("embedding") else None,
                     e.get("checksum", "")),
                )
            self._conn.commit()

    def get_feature_library_entries(self, layer: str | None = None) -> list[dict]:
        with self._lock:
            if layer:
                cur = self._conn.execute(
                    """SELECT feature_id, layer, category, name_cn, definition,
                              examples, typical_implication, embedding
                       FROM feature_library_entries WHERE layer = ?""",
                    (layer,),
                )
            else:
                cur = self._conn.execute(
                    """SELECT feature_id, layer, category, name_cn, definition,
                              examples, typical_implication, embedding
                       FROM feature_library_entries"""
                )
            return [
                {
                    "feature_id": row[0], "layer": row[1], "category": row[2],
                    "name_cn": row[3], "definition": row[4],
                    "examples": row[5], "typical_implication": row[6],
                    "embedding": json.loads(row[7]) if row[7] else None,
                }
                for row in cur.fetchall()
            ]

    def save_feature_library_version(self, checksum: str, entry_count: int,
                                      full_snapshot: dict) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO feature_library_versions
                   (checksum, entry_count, full_snapshot)
                   VALUES (?, ?, ?)""",
                (checksum, entry_count, json.dumps(full_snapshot, ensure_ascii=False)),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_feature_library_versions(self, limit: int = 10) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                """SELECT version_id, checksum, entry_count, full_snapshot, created_at
                   FROM feature_library_versions
                   ORDER BY version_id DESC LIMIT ?""",
                (limit,),
            )
            return [
                {"version_id": row[0], "checksum": row[1],
                 "entry_count": row[2],
                 "full_snapshot": json.loads(row[3]) if row[3] else {},
                 "created_at": row[4]}
                for row in cur.fetchall()
            ]

    def save_feature_activations(self, activations: list[dict]) -> None:
        with self._lock:
            for a in activations:
                self._conn.execute(
                    """INSERT INTO feature_activations
                       (date, feature_id, activation_strength,
                        matched_entity_combos, matched_library_features,
                        source_snippet_ids)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (a["date"], a["feature_id"], a["activation_strength"],
                     json.dumps(a.get("matched_entity_combos", [])),
                     json.dumps(a.get("matched_library_features", [])),
                     json.dumps(a.get("source_snippet_ids", []))),
                )
            self._conn.commit()

    def get_feature_activations(self, feature_id: str | None = None,
                                  days: int = 14) -> list[dict]:
        with self._lock:
            if feature_id:
                cur = self._conn.execute(
                    """SELECT date, feature_id, activation_strength,
                              matched_entity_combos, matched_library_features,
                              source_snippet_ids
                       FROM feature_activations
                       WHERE feature_id = ?
                         AND date >= date('now', ? || ' days')
                       ORDER BY date""",
                    (feature_id, f"-{days}"),
                )
            else:
                cur = self._conn.execute(
                    """SELECT date, feature_id, activation_strength,
                              matched_entity_combos, matched_library_features,
                              source_snippet_ids
                       FROM feature_activations
                       WHERE date >= date('now', ? || ' days')
                       ORDER BY date""",
                    (f"-{days}",),
                )
            return [
                {"date": row[0], "feature_id": row[1],
                 "activation_strength": row[2],
                 "matched_entity_combos": json.loads(row[3]) if row[3] else [],
                 "matched_library_features": json.loads(row[4]) if row[4] else [],
                 "source_snippet_ids": json.loads(row[5]) if row[5] else []}
                for row in cur.fetchall()
            ]

    # ── Entity feedback weights ──────────────────────────────────────────

    def get_entity_feedback_weights(self) -> dict[str, float]:
        """Return {entity_lower: decayed_weight} for all entities with feedback.

        Applies daily decay: weight *= 0.95^days_elapsed.
        """
        import time as _time
        now = _time.time()
        weights = {}
        with self._lock:
            cur = self._conn.execute(
                "SELECT entity, weight, last_updated FROM entity_feedback_weights"
            )
            for entity, weight, last_updated in cur.fetchall():
                if weight != 0.0 and last_updated:
                    days = (now - last_updated) / 86400.0
                    weight *= 0.95 ** days
                weights[entity.lower()] = weight
        return weights

    def upsert_entity_feedback_weight(self, entity: str, delta: float,
                                        sentiment: int, now: float):
        """Atomic read-modify-write with clamping to [-0.50, +0.50]."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT weight, positive_count, negative_count "
                "FROM entity_feedback_weights WHERE entity=?",
                (entity,),
            )
            row = cur.fetchone()
            if row:
                old_weight, pos, neg = row
                new_weight = max(-0.50, min(0.50, (old_weight or 0.0) + delta))
                pos = (pos or 0) + (1 if sentiment > 0 else 0)
                neg = (neg or 0) + (1 if sentiment < 0 else 0)
                self._conn.execute(
                    "UPDATE entity_feedback_weights SET weight=?, positive_count=?, "
                    "negative_count=?, last_updated=? WHERE entity=?",
                    (new_weight, pos, neg, now, entity),
                )
            else:
                new_weight = max(-0.50, min(0.50, delta))
                pos = 1 if sentiment > 0 else 0
                neg = 1 if sentiment < 0 else 0
                self._conn.execute(
                    "INSERT INTO entity_feedback_weights(entity, weight, "
                    "positive_count, negative_count, last_updated) "
                    "VALUES (?,?,?,?,?)",
                    (entity, new_weight, pos, neg, now),
                )
            self._conn.commit()

    # ── Judgment graph ──────────────────────────────────────────────────

    def save_judgment_entry(self, entry: dict):
        """Insert or replace a judgment entry."""
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO judgment_entries
                   (entry_id, date, entry_type, label, statement, verdict,
                    anchors, verifiable_signals, window_days,
                    surface_contradiction, underlying_cause, intuition,
                    probability, prob_range_low, prob_range_high, trend,
                    entities, last_updated)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,unixepoch())""",
                (entry["entry_id"], entry["date"], entry["entry_type"],
                 entry["label"], entry["statement"],
                 entry.get("verdict"), entry.get("anchors"),
                 entry.get("verifiable_signals"), entry.get("window_days"),
                 entry.get("surface_contradiction"), entry.get("underlying_cause"),
                 entry.get("intuition"),
                 entry.get("probability"), entry.get("prob_range_low"),
                 entry.get("prob_range_high"), entry.get("trend"),
                 entry.get("entities"),
                 ),
            )
            self._conn.commit()

    def get_judgment_entries(self, entry_type: str | None = None,
                               days: int = 30) -> list[dict]:
        """Retrieve judgment entries, optionally filtered by type."""
        with self._lock:
            if entry_type:
                cur = self._conn.execute(
                    """SELECT entry_id, date, entry_type, label, statement,
                              verdict, anchors, verifiable_signals, window_days,
                              surface_contradiction, underlying_cause, intuition,
                              probability, prob_range_low, prob_range_high, trend, entities
                       FROM judgment_entries
                       WHERE entry_type = ? AND date >= date('now', ? || ' days')
                       ORDER BY date DESC, label""",
                    (entry_type, f"-{days}"),
                )
            else:
                cur = self._conn.execute(
                    """SELECT entry_id, date, entry_type, label, statement,
                              verdict, anchors, verifiable_signals, window_days,
                              surface_contradiction, underlying_cause, intuition,
                              probability, prob_range_low, prob_range_high, trend, entities
                       FROM judgment_entries
                       WHERE date >= date('now', ? || ' days')
                       ORDER BY date DESC, label""",
                    (f"-{days}",),
                )
            import json as _json
            return [
                {"entry_id": r[0], "date": r[1], "entry_type": r[2],
                 "label": r[3], "statement": r[4], "verdict": r[5],
                 "anchors": _json.loads(r[6]) if r[6] else [],
                 "verifiable_signals": r[7], "window_days": r[8],
                 "surface_contradiction": r[9], "underlying_cause": r[10],
                 "intuition": r[11],
                 "probability": r[12], "prob_range_low": r[13],
                 "prob_range_high": r[14], "trend": r[15],
                 "entities": _json.loads(r[16]) if r[16] else []}
                for r in cur.fetchall()
            ]

    def save_hypothesis(self, h: dict) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO hypotheses
                   (hypothesis_id, parent_id, anomaly_feature_id, statement,
                    competing_alternatives, contrastive_tests, metric_scores,
                    aggregate_rank, status, iteration_count, causal_chain,
                    last_evaluated)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, unixepoch())""",
                (h["hypothesis_id"], h.get("parent_id"),
                 h.get("anomaly_feature_id"), h["statement"],
                 json.dumps(h.get("competing_alternatives", [])),
                 json.dumps(h.get("contrastive_tests", [])),
                 json.dumps(h.get("metric_scores", {})),
                 h.get("aggregate_rank", 0.0), h.get("status", "seeded"),
                 h.get("iteration_count", 0),
                 json.dumps(h.get("causal_chain", [])),
                 ),
            )
            self._conn.commit()

    def get_active_hypotheses(self) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                """SELECT hypothesis_id, parent_id, anomaly_feature_id, statement,
                          competing_alternatives, contrastive_tests, metric_scores,
                          aggregate_rank, status, iteration_count, causal_chain
                   FROM hypotheses
                   WHERE status IN ('seeded', 'testing', 'revising')
                   ORDER BY aggregate_rank ASC"""
            )
            return [
                {"hypothesis_id": row[0], "parent_id": row[1],
                 "anomaly_feature_id": row[2], "statement": row[3],
                 "competing_alternatives": json.loads(row[4]) if row[4] else [],
                 "contrastive_tests": json.loads(row[5]) if row[5] else [],
                 "metric_scores": json.loads(row[6]) if row[6] else {},
                 "aggregate_rank": row[7], "status": row[8],
                 "iteration_count": row[9],
                 "causal_chain": json.loads(row[10]) if row[10] else []}
                for row in cur.fetchall()
            ]

    def save_false_structure(self, date: str, description: str,
                               filter_rule: str, filter_reason: str,
                               snippet_ids: list[int]) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO false_structures
                   (date, description, filter_rule, filter_reason,
                    supporting_snippet_ids)
                   VALUES (?, ?, ?, ?, ?)""",
                (date, description, filter_rule, filter_reason,
                 json.dumps(snippet_ids)),
            )
            self._conn.commit()
            return cur.lastrowid

    def save_icl_report(self, report: dict) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO icl_reports
                   (date, input_count, tier1_count, tier2_count, tier3_count,
                    compression_ratio, tier1_items, tier2_items)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (report["date"], report["input_count"],
                 report["tier1_count"], report["tier2_count"],
                 report["tier3_count"], report["compression_ratio"],
                 json.dumps(report.get("tier1_items", [])),
                 json.dumps(report.get("tier2_items", []))),
            )
            self._conn.commit()

    def save_dcl_judgment(self, j: dict) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO dcl_judgments
                   (judgment_id, date, judgment, confidence, disruptiveness,
                    supporting_hypotheses, counterfactual, action_implication,
                    verification_window, causal_radius, page_rank)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (j["judgment_id"], j["date"], j["judgment"],
                 j["confidence"], j.get("disruptiveness", 0.0),
                 json.dumps(j.get("supporting_hypotheses", [])),
                 j.get("counterfactual", ""), j.get("action_implication", ""),
                 j.get("verification_window", ""), j.get("causal_radius", 0),
                 j.get("page_rank", 0.0)),
            )
            self._conn.commit()

    def save_feature_proposal(self, p: dict) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO feature_proposals
                   (proposal_id, suggested_name, suggested_layer,
                    suggested_definition, supporting_evidence,
                    related_existing_features, confidence, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (p["proposal_id"], p["suggested_name"],
                 p["suggested_layer"], p["suggested_definition"],
                 json.dumps(p.get("supporting_evidence", [])),
                 json.dumps(p.get("related_existing_features", [])),
                 p.get("confidence", 0.0),
                 p.get("status", "pending_review")),
            )
            self._conn.commit()

    def get_pending_feature_proposals(self) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                """SELECT proposal_id, suggested_name, suggested_layer,
                          suggested_definition, supporting_evidence,
                          related_existing_features, confidence
                   FROM feature_proposals
                   WHERE status = 'pending_review'
                   ORDER BY confidence DESC"""
            )
            return [
                {"proposal_id": row[0], "suggested_name": row[1],
                 "suggested_layer": row[2], "suggested_definition": row[3],
                 "supporting_evidence": json.loads(row[4]) if row[4] else [],
                 "related_existing_features": json.loads(row[5]) if row[5] else [],
                 "confidence": row[6]}
                for row in cur.fetchall()
            ]

    def save_coactivation_pair(self, pair: dict) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO coactivation_pairs
                   (id, feature_id_a, feature_id_b, pearson_r,
                    cooccurrence_count, verification_query, confidence,
                    last_verified)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (pair.get("id"), pair["feature_id_a"], pair["feature_id_b"],
                 pair.get("pearson_r", 0.0), pair.get("cooccurrence_count", 0),
                 pair.get("verification_query", ""), pair.get("confidence", 0.5),
                 pair.get("last_verified")),
            )
            self._conn.commit()

    def close(self):
        self._conn.close()
