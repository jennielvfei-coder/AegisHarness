"""ProactiveScanner — preemptive evolution checks for the Harness system.

Four zero-LLM checks running at session start. Each independent try/except.
Hard timeout 200ms total. Returns at most 3 alerts.

Inspired by MOSS's approach: don't wait for failures, scan for improvement
opportunities in your own code and data.
"""

from __future__ import annotations

import ast
import json
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── Data structures ──────────────────────────────────────────────────────

@dataclass
class ScanResult:
    domain: str            # 'complexity' | 'trend' | 'api_surface' | 'pattern'
    severity: str          # 'info' | 'warning' | 'critical'
    message: str
    timestamp: float = field(default_factory=time.time)


# ── Main entry point ─────────────────────────────────────────────────────

def run_scan(db_path: Path, max_results: int = 3) -> list[ScanResult]:
    """Run all four checks. Each independent. Hard timeout 200ms.

    Returns at most max_results, sorted by severity (critical > warning > info).
    """
    start = time.perf_counter()
    results: list[ScanResult] = []

    checks = [
        _check_code_complexity,
        lambda: _check_trends(db_path),
        lambda: _check_api_surface(db_path),
        lambda: _check_patterns(db_path),
    ]

    for check_fn in checks:
        if (time.perf_counter() - start) > 0.200:
            break
        try:
            result = check_fn()
            if result:
                results.append(result)
        except Exception:
            pass

    severity_order = {"critical": 0, "warning": 1, "info": 2}
    results.sort(key=lambda r: severity_order.get(r.severity, 2))
    return results[:max_results]


# ── Check 1: Code complexity ─────────────────────────────────────────────

def _check_code_complexity() -> Optional[ScanResult]:
    """AST-parse harness/*.py. Find the most complex function.

    Uses Python's built-in ast module. Cyclomatic complexity:
    1 + sum of if/for/while/except/and/or decision points.
    Reports if complexity > 10.
    """
    harness_dir = Path(__file__).resolve().parent
    py_files = list(harness_dir.glob("*.py"))

    worst: tuple[str, str, int] = ("", "", 0)
    skip_files = {"__init__.py", "self_model.py", "proactive_scanner.py"}

    for py_file in py_files:
        if py_file.name in skip_files:
            continue
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    cc = _cyclomatic_complexity(node)
                    if cc > worst[2]:
                        worst = (py_file.name, node.name, cc)
        except SyntaxError:
            pass

    if worst[2] > 10:
        severity = "warning" if worst[2] > 20 else "info"
        return ScanResult(
            domain="complexity",
            severity=severity,
            message=f"High cyclomatic complexity: {worst[0]}:{worst[1]} (CC={worst[2]})",
        )
    return None


def _cyclomatic_complexity(node: ast.AST) -> int:
    """Count decision points in a function body."""
    count = 1
    for child in ast.walk(node):
        if isinstance(child, (ast.If, ast.For, ast.While, ast.ExceptHandler)):
            count += 1
        elif isinstance(child, ast.BoolOp):
            count += len(child.values) - 1
    return count


# ── Check 2: Trend detection ─────────────────────────────────────────────

def _check_trends(db_path: Path) -> Optional[ScanResult]:
    """SQL comparison: recent 7d vs prior 7d for key metrics.

    Trends checked:
    - tool_call_log error rate
    - PreThink/Observer conflict count
    Returns the most significant alert.
    """
    conn = sqlite3.connect(str(db_path), timeout=2)
    now = time.time()

    # Error rate trend
    cur = conn.execute(
        "SELECT COUNT(*), SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) "
        "FROM tool_call_log WHERE timestamp > ?",
        (now - 604800,),
    )
    recent_total, recent_errs = cur.fetchone()
    recent_total = recent_total or 1
    recent_rate = (recent_errs or 0) / recent_total

    cur = conn.execute(
        "SELECT COUNT(*), SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) "
        "FROM tool_call_log WHERE timestamp > ? AND timestamp <= ?",
        (now - 1209600, now - 604800),
    )
    prior_total, prior_errs = cur.fetchone()
    prior_total = prior_total or 1
    prior_rate = (prior_errs or 0) / prior_total

    # Conflict trend
    cur = conn.execute(
        "SELECT COUNT(*) FROM observations "
        "WHERE tags LIKE '%conflict%' AND processed_at > ?",
        (now - 604800,),
    )
    recent_conflicts = (cur.fetchone() or [0])[0]

    cur = conn.execute(
        "SELECT COUNT(*) FROM observations "
        "WHERE tags LIKE '%conflict%' AND processed_at > ? AND processed_at <= ?",
        (now - 1209600, now - 604800),
    )
    prior_conflicts = (cur.fetchone() or [0])[0]

    conn.close()

    # Pick the most alarming trend
    if prior_rate > 0 and recent_rate > prior_rate * 2 and recent_rate > 0.2:
        return ScanResult(
            domain="trend",
            severity="critical" if recent_rate > 0.3 else "warning",
            message=f"Tool error rate surged: {prior_rate:.0%} → {recent_rate:.0%} over 7d",
        )

    if prior_conflicts > 0 and recent_conflicts > prior_conflicts * 2:
        return ScanResult(
            domain="trend",
            severity="warning",
            message=f"PreThink/Observer conflicts rising: {prior_conflicts} → {recent_conflicts} over 7d",
        )

    return None


# ── Check 3: API surface change detection ────────────────────────────────

def _check_api_surface(db_path: Path) -> Optional[ScanResult]:
    """Compare current MCP server list against last stored snapshot.

    Detects new/removed MCP servers. Stores snapshot in meta_store.
    """
    conn = sqlite3.connect(str(db_path), timeout=2)

    current = _get_current_mcp_summary()
    if not current:
        conn.close()
        return None

    current_json = json.dumps(current, sort_keys=True)

    # Check last stored snapshot
    cur = conn.execute(
        "SELECT value FROM meta_store WHERE key = 'mcp_tool_snapshot'"
    )
    row = cur.fetchone()

    diffs: list[str] = []
    if row:
        try:
            stored = json.loads(row[0])
            for srv_name in current:
                if srv_name not in stored:
                    diffs.append(f"+{srv_name}")
            for srv_name in stored:
                if srv_name not in current:
                    diffs.append(f"-{srv_name}")
        except json.JSONDecodeError:
            stored = {}

    # Always update snapshot
    conn.execute(
        "INSERT OR REPLACE INTO meta_store (key, value, updated_at) "
        "VALUES ('mcp_tool_snapshot', ?, unixepoch())",
        (current_json,),
    )
    conn.commit()
    conn.close()

    if diffs:
        return ScanResult(
            domain="api_surface",
            severity="info",
            message=f"MCP server change: {'; '.join(diffs[:3])}",
        )

    return None


def _get_current_mcp_summary() -> dict[str, dict]:
    """Read MCP config and return {server_name: {}} dict."""
    mcp_paths = [
        Path("D:/Claude/.mcp.json"),
        Path.home() / ".claude" / "mcp.json",
    ]
    for mcp_path in mcp_paths:
        if mcp_path.exists():
            try:
                config = json.loads(mcp_path.read_text(encoding="utf-8"))
                servers = config.get("mcpServers", {})
                return {name: {} for name in servers if servers[name] is not None}
            except Exception:
                continue
    return {}


# ── Check 4: Pattern mining ──────────────────────────────────────────────

def _check_patterns(db_path: Path) -> Optional[ScanResult]:
    """Detect emerging error sources not yet covered by active constraints.

    Queries tool_call_log for high-frequency error tools and
    checks if they're covered by the constraint registry.
    """
    conn = sqlite3.connect(str(db_path), timeout=2)

    # High-frequency error tools in last 7 days
    cur = conn.execute(
        "SELECT tool_name, COUNT(*) as cnt FROM tool_call_log "
        "WHERE status='error' AND timestamp > unixepoch() - 604800 "
        "GROUP BY tool_name ORDER BY cnt DESC LIMIT 10"
    )
    error_tools = {r[0]: r[1] for r in cur.fetchall()}

    # Active constraint tools
    cur = conn.execute(
        "SELECT tool_name FROM constraints "
        "WHERE active = 1 AND (expires_at IS NULL OR expires_at > unixepoch())"
    )
    constrained_tools = {r[0] for r in cur.fetchall()}

    conn.close()

    uncovered = []
    for tool, cnt in sorted(error_tools.items(), key=lambda x: -x[1]):
        if tool not in constrained_tools and tool != "*" and cnt >= 3:
            uncovered.append(f"{tool}({cnt})")

    if uncovered:
        return ScanResult(
            domain="pattern",
            severity="info",
            message=f"Unconstrained error sources: {', '.join(uncovered[:3])}",
        )

    return None
