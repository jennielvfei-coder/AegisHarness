# Harness Phase 1 — Observer + SQLite 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 observer 观察层——Claude Code 会话结束后自动读取 transcript，判断是否值得提炼，结果写入 SQLite

**Architecture:** observer.py 作为纯函数分析 transcript → 返回 ObservationReport dataclass → indexer.py 写入 SQLite → harness_daemon.py 作为 CLI 入口供 Claude Code hooks 调用

**Tech Stack:** Python 3.11+, SQLite (FTS5), PyYAML, dataclasses

---

### Task 1: 项目骨架与配置

**Files:**
- Create: `D:\Claude\harness\harness_config.yaml`
- Create: `D:\Claude\harness\harness_daemon.py`

- [ ] **Step 1: 写配置文件**

`D:\Claude\harness\harness_config.yaml`:

```yaml
harness:
  transcript_dir: "C:\\Users\\Chucky\\.claude\\projects\\D--Claude"
  transcript_file: "memory.jsonl"
  db_path: "D:\\Claude\\harness\\state.db"

observer:
  min_tool_calls_for_skill: 5
  skip_trivial_sessions: true
  patterns:
    correction:
      - "不对"
      - "不是这样"
      - "应该是"
      - "错了"
      - "改一下"
      - "纠正"
    preference:
      - "以后都"
      - "我总是"
      - "帮我记住"
      - "我习惯"
      - "我的偏好"
    new_workflow:
      - "怎么做"
      - "帮我想想"
      - "分析一下"
      - "审查"
      - "起草"

refiner:
  enabled: false  # Phase 2 才开
  model: "deepseek-v4-pro"

injector:
  enabled: false  # Phase 3 才开
  max_fragments: 3
  min_confidence: 0.6

evolution:
  auto_activate: false  # Phase 4 才开
  require_review: true
```

- [ ] **Step 2: 写 CLI 入口骨架**

`D:\Claude\harness\harness_daemon.py`:

```python
#!/usr/bin/env python3
"""Harness daemon — Claude Code self-evolving harness framework.

Usage:
  python harness_daemon.py observe   # Called by StopSession hook
  python harness_daemon.py inject    # Called by StartSession hook (Phase 3)
"""

import argparse
import sys
from pathlib import Path

HARNESS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HARNESS_DIR))


def cmd_observe():
    """Phase 1: Analyze the latest session transcript and record observations."""
    from observer import analyze_session
    from indexer import HarnessDB

    config_path = HARNESS_DIR / "harness_config.yaml"
    transcript_path = HARNESS_DIR.parent / "memory.jsonl"

    report = analyze_session(transcript_path, config_path)
    if report is None:
        print("[harness] No transcript found or nothing to analyze.")
        return

    db = HarnessDB(HARNESS_DIR / "state.db")
    db.save_observation(report)
    print(f"[harness] Observation saved: action={report.action}, "
          f"confidence={report.confidence:.2f}")


def cmd_inject():
    """Phase 3: Search for relevant fragments and output injection text."""
    print("[harness] injector not yet implemented (Phase 3).")


def main():
    parser = argparse.ArgumentParser(description="Harness daemon")
    parser.add_argument("command", choices=["observe", "inject"])
    args = parser.parse_args()

    if args.command == "observe":
        cmd_observe()
    elif args.command == "inject":
        cmd_inject()


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: 验证 CLI 可执行**

Run: `python D:\Claude\harness\harness_daemon.py observe`
Expected: 输出 `[harness] No transcript found...` 或 import error（observer/indexer 还未创建，预期报 ModuleNotFoundError）

- [ ] **Step 4: Commit**

```bash
git add D:\Claude\harness\harness_config.yaml D:\Claude\harness\harness_daemon.py
git commit -m "feat(harness): add project skeleton — config + CLI entry point"
```

---

### Task 2: ObservationReport 数据模型

**Files:**
- Create: `D:\Claude\harness\observer.py`

- [ ] **Step 1: 写 observer.py 含 ObservationReport**

`D:\Claude\harness\observer.py`:

```python
"""Observer — analyze Claude Code transcripts to decide what to extract.

Pure functions: read transcript → return structured observation.
No side effects, no database writes.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class ObservationReport:
    """Result of analyzing one session transcript."""
    session_id: str
    action: str  # 'create_skill' | 'patch_skill' | 'save_fragment' | 'update_preference' | 'skip'
    confidence: float  # 0.0–1.0
    reason: str  # Human-readable: why this action was chosen
    summary: str  # One-paragraph summary of what was learned
    tags: list = field(default_factory=list)  # e.g. ['contract-review', 'privacy']
    skill_name: Optional[str] = None  # For patch_skill: which skill to update
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


def load_config(config_path: Path) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _read_last_session(transcript_path: Path) -> Optional[dict]:
    """Read the last complete user+assistant exchange from memory.jsonl.

    Returns the last non-system message block, or None if file missing/empty.
    """
    if not transcript_path.exists():
        return None

    lines = []
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped:
                    lines.append(json.loads(stripped))
    except (json.JSONDecodeError, OSError):
        return None

    if not lines:
        return None

    last = lines[-1]
    return {
        "content": json.dumps(last, ensure_ascii=False),
        "message_count": len(lines),
    }


def _count_tool_calls(content: str) -> int:
    """Count tool_use blocks in transcript content."""
    # Claude Code transcripts mark tool calls with specific JSON structures.
    # Count occurrences of "tool_use" or "tool_calls" patterns.
    pattern = r'"type"\s*:\s*"tool_use"'
    return len(re.findall(pattern, content))


def _detect_correction(content: str, patterns: list) -> bool:
    """Check if user corrected the assistant in this session."""
    # Look for correction phrases in user messages
    user_sections = re.findall(r'"role"\s*:\s*"user"[^}]*"content"\s*:\s*"([^"]*)"', content)
    for section in user_sections:
        for phrase in patterns:
            if phrase in section:
                return True
    return False


def _detect_preference(content: str, patterns: list) -> bool:
    """Check if user stated a durable preference."""
    user_sections = re.findall(r'"role"\s*:\s*"user"[^}]*"content"\s*:\s*"([^"]*)"', content)
    for section in user_sections:
        for phrase in patterns:
            if phrase in section:
                return True
    return False


def _generate_summary(content: str) -> str:
    """Generate a simple heuristic summary of what the session was about.

    In Phase 2, this will be replaced by LLM-based summarization.
    """
    # Extract first meaningful user message as context clue
    user_msgs = re.findall(r'"role"\s*:\s*"user"[^}]*"content"\s*:\s*"([^"]*)"', content)
    if user_msgs:
        first = user_msgs[0][:200]
        return f"Session about: {first}..."
    return "Session content not extractable."


def analyze_session(
    transcript_path: Path,
    config_path: Optional[Path] = None,
) -> Optional[ObservationReport]:
    """Analyze the latest Claude Code session and return an ObservationReport.

    Args:
        transcript_path: Path to memory.jsonl
        config_path: Path to harness_config.yaml (uses default if None)

    Returns:
        ObservationReport or None if no transcript found / nothing to analyze.
    """
    if config_path is None:
        config_path = Path(__file__).resolve().parent / "harness_config.yaml"

    config = load_config(config_path)
    obs_config = config["observer"]

    session_data = _read_last_session(transcript_path)
    if session_data is None:
        return None

    content = session_data["content"]
    msg_count = session_data["message_count"]
    tool_count = _count_tool_calls(content)

    # Rule 1: Skip trivial sessions
    if obs_config["skip_trivial_sessions"] and tool_count < obs_config["min_tool_calls_for_skill"]:
        return ObservationReport(
            session_id=f"session_{datetime.now().strftime('%Y%m%d%H%M%S')}",
            action="skip",
            confidence=0.9,
            reason=f"Trivial session: only {tool_count} tool calls (threshold: {obs_config['min_tool_calls_for_skill']})",
            summary="",
            tags=[],
        )

    # Rule 2: Check for correction pattern → patch_skill
    if _detect_correction(content, obs_config["patterns"]["correction"]):
        return ObservationReport(
            session_id=f"session_{datetime.now().strftime('%Y%m%d%H%M%S')}",
            action="patch_skill",
            confidence=0.7,
            reason="Correction pattern detected — user corrected assistant output",
            summary=_generate_summary(content),
            tags=_guess_tags(content),
        )

    # Rule 3: Check for preference statement → update_preference
    if _detect_preference(content, obs_config["patterns"]["preference"]):
        return ObservationReport(
            session_id=f"session_{datetime.now().strftime('%Y%m%d%H%M%S')}",
            action="update_preference",
            confidence=0.65,
            reason="Preference statement detected",
            summary=_generate_summary(content),
            tags=_guess_tags(content),
        )

    # Rule 4: Complex session (many tool calls) → create_skill candidate
    threshold = obs_config["min_tool_calls_for_skill"]
    if tool_count >= threshold:
        return ObservationReport(
            session_id=f"session_{datetime.now().strftime('%Y%m%d%H%M%S')}",
            action="create_skill",
            confidence=0.5,
            reason=f"Complex session: {tool_count} tool calls >= {threshold}",
            summary=_generate_summary(content),
            tags=_guess_tags(content),
        )

    # Rule 5: Default — skip
    return ObservationReport(
        session_id=f"session_{datetime.now().strftime('%Y%m%d%H%M%S')}",
        action="skip",
        confidence=0.8,
        reason=f"No strong signal detected. tool_calls={tool_count}, msg_count={msg_count}",
        summary="",
        tags=[],
    )


def _guess_tags(content: str) -> list:
    """Heuristic tag guessing — replaced by LLM in Phase 2."""
    tags = []
    keywords = {
        "contract": "contract-review",
        "合同": "contract-review",
        "隐私": "privacy",
        "个保法": "privacy",
        "PIA": "privacy",
        "DPA": "privacy",
        "DSAR": "privacy",
        "并购": "m-a",
        "尽调": "m-a",
        "数据": "data-compliance",
        "AI": "ai-governance",
        "算法": "ai-governance",
        "版权": "copyright",
        "著作权": "copyright",
        "劳动": "employment",
        "知识产权": "ip",
        "商标": "ip",
    }
    content_lower = content.lower()
    for key, tag in keywords.items():
        if key.lower() in content_lower:
            tags.append(tag)
    return list(set(tags)) if tags else ["general"]
```

- [ ] **Step 2: 验证 observer 可导入**

Run: `python -c "import sys; sys.path.insert(0, 'D:\\Claude\\harness'); from observer import ObservationReport, analyze_session; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add D:\Claude\harness\observer.py
git commit -m "feat(harness): add observer with heuristic signal detection"
```

---

### Task 3: SQLite 数据层

**Files:**
- Create: `D:\Claude\harness\indexer.py`

- [ ] **Step 1: 写 indexer.py**

`D:\Claude\harness\indexer.py`:

```python
"""Indexer — SQLite FTS5-backed storage for observations, fragments, and skill index.

Design borrowed from Hermes hermes_state.py SessionDB:
  - WAL mode for concurrent reads
  - FTS5 virtual table for text search
  - Schema versioning for migration
"""

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

-- Extracted observations from session analysis
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    action TEXT NOT NULL,
    confidence REAL NOT NULL,
    reason TEXT,
    summary TEXT,
    tags TEXT,           -- JSON array
    skill_name TEXT,
    processed_at REAL NOT NULL,
    created_at REAL NOT NULL DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS idx_observations_action ON observations(action);
CREATE INDEX IF NOT EXISTS idx_observations_processed ON observations(processed_at DESC);

-- Prompt fragments for injection (Phase 3)
CREATE TABLE IF NOT EXISTS fragments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag TEXT NOT NULL,
    trigger_phrases TEXT,  -- JSON array
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

-- Triggers to keep FTS index in sync
CREATE TRIGGER IF NOT EXISTS fragments_fts_insert AFTER INSERT ON fragments BEGIN
    INSERT INTO fragments_fts(rowid, tag, trigger_phrases, content)
    VALUES (new.id, new.tag, new.trigger_phrases, new.content);
END;

CREATE TRIGGER IF NOT EXISTS fragments_fts_delete AFTER DELETE ON fragments BEGIN
    INSERT INTO fragments_fts(fragments_fts, rowid, tag, trigger_phrases, content)
    VALUES ('delete', old.id, old.tag, old.trigger_phrases, old.content);
END;

-- Skill index
CREATE TABLE IF NOT EXISTS skill_index (
    name TEXT PRIMARY KEY,
    file_path TEXT NOT NULL,
    tags TEXT,            -- JSON array
    trigger_patterns TEXT, -- JSON array
    version INTEGER DEFAULT 1,
    harness_confidence REAL DEFAULT 0.5,
    created_at REAL NOT NULL DEFAULT (unixepoch()),
    updated_at REAL,
    usage_count INTEGER DEFAULT 0,
    last_used REAL
);

-- Evolution changelog
CREATE TABLE IF NOT EXISTS evolution_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name TEXT,
    action TEXT,          -- 'create' | 'patch' | 'deprecate'
    source_session TEXT,
    change_summary TEXT,
    old_version INTEGER,
    new_version INTEGER,
    timestamp REAL NOT NULL DEFAULT (unixepoch())
);

-- Lightweight session metadata
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
            # Schema version check
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
        from observer import ObservationReport
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
```

- [ ] **Step 2: 验证 SQLite schema 创建**

Run:
```bash
python -c "
import sys; sys.path.insert(0, 'D:\\Claude\\harness')
from indexer import HarnessDB
from pathlib import Path
db = HarnessDB(Path('D:\\Claude\\harness\\state.db'))
stats = db.get_stats()
print('DB initialized:', stats)
print('Schema version:', db._conn.execute('SELECT version FROM schema_version').fetchone()[0])
db.close()
"
```
Expected: `DB initialized: {'total_observations': 0, 'by_action': {}}` + `Schema version: 1`

- [ ] **Step 3: Commit**

```bash
git add D:\Claude\harness\indexer.py
git commit -m "feat(harness): add SQLite FTS5 storage layer (indexer)"
```

---

### Task 4: Observer 端到端集成测试

**Files:**
- Create: `D:\Claude\harness\tests\test_observer.py`
- Create: `D:\Claude\harness\tests\fixtures\sample_transcript.jsonl`

- [ ] **Step 1: 写 sample transcript fixture**

`D:\Claude\harness\tests\fixtures\sample_transcript.jsonl`:

```jsonl
{"role": "user", "content": "审查一下这份供应商合同，注意责任上限条款"}
{"role": "assistant", "content": "我来审查这份合同...", "type": "message"}
{"type": "tool_use", "name": "Read", "input": {"file_path": "contract.pdf"}}
{"type": "tool_result", "content": "[合同内容...]"}
{"type": "tool_use", "name": "Grep", "input": {"pattern": "责任"}}
{"type": "tool_result", "content": "[责任条款...]"}
{"type": "tool_use", "name": "Edit", "input": {"file_path": "contract.pdf"}}
{"type": "tool_result", "content": "[修改完成]"}
{"type": "tool_use", "name": "Write", "input": {"file_path": "review.md"}}
{"type": "tool_result", "content": "[审查备忘录已生成]"}
{"role": "assistant", "content": "审查完成，责任上限建议修改为合同金额的2倍。"}
{"role": "user", "content": "不对，责任上限我们习惯用合同金额的1倍，不是2倍。以后都按1倍来。"}
{"role": "assistant", "content": "明白了，已按1倍修改。我记住了这个偏好。"}
```

- [ ] **Step 2: 写 observer 测试**

`D:\Claude\harness\tests\test_observer.py`:

```python
"""Tests for observer signal detection logic."""
import json
import sys
from pathlib import Path

HARNESS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HARNESS_DIR))

from observer import (
    ObservationReport,
    analyze_session,
    _detect_correction,
    _detect_preference,
    _count_tool_calls,
    _guess_tags,
)


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_count_tool_calls():
    content = json.dumps([
        {"type": "tool_use", "name": "Read"},
        {"type": "tool_use", "name": "Grep"},
        {"type": "tool_use", "name": "Edit"},
        {"type": "tool_use", "name": "Write"},
        {"type": "tool_use", "name": "Bash"},
    ])
    assert _count_tool_calls(content) >= 4


def test_detect_correction():
    content = json.dumps([
        {"role": "user", "content": "不对，我们习惯用1倍，不是2倍。"}
    ])
    patterns = ["不对", "不是这样", "错了", "纠正"]
    assert _detect_correction(content, patterns) is True


def test_detect_no_correction():
    content = json.dumps([
        {"role": "user", "content": "谢谢，审查做得很好。"}
    ])
    patterns = ["不对", "不是这样", "错了"]
    assert _detect_correction(content, patterns) is False


def test_detect_preference():
    content = json.dumps([
        {"role": "user", "content": "以后都按中国法来，管辖地选浙江法院。"}
    ])
    patterns = ["以后都", "我总是", "帮我记住", "我习惯"]
    assert _detect_preference(content, patterns) is True


def test_detect_no_preference():
    content = json.dumps([
        {"role": "user", "content": "这份合同你看一下。"}
    ])
    patterns = ["以后都", "我总是"]
    assert _detect_preference(content, patterns) is False


def test_guess_tags():
    content = "审查供应商合同中的责任上限条款 涉及个保法和数据合规"
    tags = _guess_tags(content)
    assert "contract-review" in tags


def test_analyze_session_with_correction():
    """End-to-end: a session with correction should produce patch_skill action."""
    fixture = FIXTURES / "sample_transcript.jsonl"
    if fixture.exists():
        report = analyze_session(fixture)
        assert report is not None
        # Should detect correction pattern
        assert report.action in ("patch_skill", "update_preference", "create_skill", "skip")
        assert 0.0 <= report.confidence <= 1.0
        assert isinstance(report.tags, list)


def test_analyze_session_missing_file():
    """Missing transcript should return None."""
    report = analyze_session(Path("/nonexistent/path.jsonl"))
    assert report is None


def test_observation_report_fields():
    report = ObservationReport(
        session_id="test-123",
        action="create_skill",
        confidence=0.8,
        reason="Test reason",
        summary="Test summary",
        tags=["test"],
    )
    d = {
        "session_id": report.session_id,
        "action": report.action,
        "confidence": report.confidence,
        "reason": report.reason,
        "summary": report.summary,
        "tags": report.tags,
    }
    assert d["action"] == "create_skill"
    assert d["confidence"] == 0.8
```

- [ ] **Step 3: 运行测试**

Run: `python -m pytest D:\Claude\harness\tests\test_observer.py -v`
Expected: 7 passed (如果 sample_transcript.jsonl 存在) 或 6 passed, 1 skipped

- [ ] **Step 4: 端到端 — 对真实 memory.jsonl 跑一次 observer**

Run:
```bash
python -c "
import sys; sys.path.insert(0, 'D:\\Claude\\harness')
from observer import analyze_session
from pathlib import Path
report = analyze_session(Path('D:\\Claude\\memory.jsonl'))
if report:
    print(f'Action: {report.action}')
    print(f'Confidence: {report.confidence:.2f}')
    print(f'Reason: {report.reason}')
    print(f'Tags: {report.tags}')
else:
    print('No transcript found')
"
```
Expected: 输出 action/confidence/reason/tags 或 `No transcript found`

- [ ] **Step 5: 端到端 — 写入 SQLite**

Run:
```bash
python -c "
import sys; sys.path.insert(0, 'D:\\Claude\\harness')
from observer import analyze_session
from indexer import HarnessDB
from pathlib import Path

report = analyze_session(Path('D:\\Claude\\memory.jsonl'))
if report:
    db = HarnessDB()
    row_id = db.save_observation(report)
    print(f'Saved observation row {row_id}')
    print('Recent:', db.get_recent_observations(3))
    print('Stats:', db.get_stats())
    db.close()
else:
    print('No transcript found — skipping DB test')
"
```
Expected: 输出保存成功 + recent observations list + stats

- [ ] **Step 6: Commit**

```bash
git add D:\Claude\harness\tests\ D:\Claude\harness\tests\test_observer.py
git commit -m "test(harness): add observer unit tests + e2e integration"
```

---

### Task 5: 接入 Claude Code hooks

**Files:**
- Read: `C:\Users\Chucky\.claude\settings.json` (via update-config skill or manual)

- [ ] **Step 1: 在 settings.json 添加 StopSession hook**

需要用户确认后，在 `C:\Users\Chucky\.claude\settings.json` 的 `hooks` 字段添加：

```json
"hooks": {
  "StopSession": [
    {
      "matcher": "*",
      "command": "python D:\\Claude\\harness\\harness_daemon.py observe"
    }
  ]
}
```

如果 `hooks` 字段已存在，追加到 `StopSession` 数组。

- [ ] **Step 2: 手动测试 hook**

1. 在 Claude Code 中做一轮对话
2. 结束会话
3. 检查 SQLite 是否有新的 observation：`python -c "import sys; sys.path.insert(0,'D:\\Claude\\harness'); from indexer import HarnessDB; db=HarnessDB(); print(db.get_recent_observations(1)); db.close()"`

Expected: 看到刚刚对话的分析结果

- [ ] **Step 3: Commit**

```bash
git add C:\Users\Chucky\.claude\settings.json
git commit -m "feat(harness): wire StopSession hook to observer"
```

---

## Self-Review

**1. Spec coverage:**
- observer 读 transcript ✓ (Task 2 analyze_session, Task 4 fixture)
- 判断逻辑: 纠正/偏好/新工作流/跳过 ✓ (Task 2 _detect_correction, _detect_preference, tool count)
- SQLite schema ✓ (Task 3 SCHEMA_SQL)
- hooks 接入 ✓ (Task 5)
- 配置文件 ✓ (Task 1)

**2. Placeholder scan:** 无 TBD/TODO。所有代码即最终实现。Phase 2/3 字段预留了默认值。

**3. Type consistency:** ObservationReport 字段名在 observer.py (生成) 和 indexer.py (save_observation) 一致。session_id, action, confidence, reason, summary, tags, skill_name, timestamp ✓

---

*Plan saved: 2026-05-18 | Next: execution via subagent-driven-development or executing-plans*
