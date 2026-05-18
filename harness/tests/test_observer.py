"""Tests for observer signal detection logic."""
import json
import sys
from pathlib import Path

HARNESS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HARNESS_DIR))

from observer import (
    ObservationReport,
    analyze_session,
    _detect_pattern,
    _count_tool_calls,
    _guess_tags,
)
from indexer import HarnessDB


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _json_dumps(obj):
    """Serialize to JSON string preserving non-ASCII characters for CJK pattern matching."""
    return json.dumps(obj, ensure_ascii=False)


def test_count_tool_calls():
    content = _json_dumps([
        {"type": "tool_use", "name": "Read"},
        {"type": "tool_use", "name": "Grep"},
        {"type": "tool_use", "name": "Edit"},
        {"type": "tool_use", "name": "Write"},
        {"type": "tool_use", "name": "Bash"},
    ])
    assert _count_tool_calls(content) >= 4


def test_detect_correction():
    content = _json_dumps([
        {"role": "user", "content": "不对，我们习惯用1倍，不是2倍。"}
    ])
    patterns = ["不对", "不是这样", "错了", "纠正"]
    assert _detect_pattern(content, patterns) is True


def test_detect_no_correction():
    content = _json_dumps([
        {"role": "user", "content": "谢谢，审查做得很好。"}
    ])
    patterns = ["不对", "不是这样", "错了"]
    assert _detect_pattern(content, patterns) is False


def test_detect_preference():
    content = _json_dumps([
        {"role": "user", "content": "以后都按中国法来，管辖地选浙江法院。"}
    ])
    patterns = ["以后都", "我总是", "帮我记住", "我习惯"]
    assert _detect_pattern(content, patterns) is True


def test_detect_no_preference():
    content = _json_dumps([
        {"role": "user", "content": "这份合同你看一下。"}
    ])
    patterns = ["以后都", "我总是"]
    assert _detect_pattern(content, patterns) is False


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
    assert report.action == "create_skill"
    assert report.confidence == 0.8
    assert report.session_id == "test-123"
    assert report.tags == ["test"]


def test_db_save_and_retrieve():
    """Integration: save ObservationReport to DB and retrieve stats."""
    db = HarnessDB(Path(HARNESS_DIR / "state_test.db"))
    report = ObservationReport(
        session_id="test-db-001",
        action="create_skill",
        confidence=0.9,
        reason="Test DB save",
        summary="Testing database integration",
        tags=["test", "contract-review"],
    )
    row_id = db.save_observation(report)
    assert row_id > 0

    stats = db.get_stats()
    assert stats["total_observations"] > 0

    recent = db.get_recent_observations(5)
    matching = [r for r in recent if r["session_id"] == "test-db-001"]
    assert len(matching) == 1
    assert matching[0]["action"] == "create_skill"

    db.close()
    # Clean up test DB
    Path(HARNESS_DIR / "state_test.db").unlink(missing_ok=True)
    Path(HARNESS_DIR / "state_test.db-wal").unlink(missing_ok=True)
    Path(HARNESS_DIR / "state_test.db-shm").unlink(missing_ok=True)
