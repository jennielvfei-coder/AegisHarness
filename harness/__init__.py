"""AegisHarness — Claude Code self-evolving harness framework.

Human as legislator, system as enforcer. Observer captures correction signals,
PreThink judges severity, Refiner generates constraints, hooks enforce them
across sessions.

Core modules:
    observer       — Transcript analysis + signal extraction
    prethink       — Situational model inference + injection budget
    refiner        — Skill evolution from correction signals
    health_probes  — 6 SQL-based health probes, run at session boundaries
    harness_daemon — CLI: observe, inject, review, analyze, diagnose,
                     cleanup, status, check-tool-log
    hooks          — PreToolUse/PostToolUse/Stop hooks wiring
    indexer        — SQLite ORM for harness state (HarnessDB)
    self_model     — Self-model introspection + evolution tracking
"""

__version__ = "0.1.0"
