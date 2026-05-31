```markdown
---
name: harness-state-sql-diagnostics
description: Diagnose harness health and error accumulation by directly querying harness/state.db
tags: [ai-governance, prethink:exploration]
triggers:
  - "User asks about harness state, errors, hypotheses, or component health"
  - "harness daemon analyze command fails or path issues"
version: 1
harness_confidence: 0.9
---

# Harness State SQL Diagnostics

## 执行逻辑
### When to Use
When the harness daemon command fails (e.g., path issues, Python not found) or when detailed insight into judgment graph, error counts, and hypotheses is needed quickly. Use direct SQL queries on the harness state database.

### Step-by-Step
1. **Connect to SQLite**: `import sqlite3; db = sqlite3.connect('harness/state.db')`
2. **Aggregate Judgment Health**: Query `judgment_entries` for total count and average confidence (extracting `$.confidence` via `json_extract`).
3. **Error/Feature Activations**: Count rows in `feature_activations` where `json_extract(payload, '$.type')` contains 'error' or 'fail'.
4. **Hypotheses Status**: Select all from `hypotheses` showing `hypothesis_id`, `status`, and `json_extract(payload, '$.description')`.
5. **Signal Buffer Errors**: Count signals in `signal_buffer` where `json_extract(payload, '$.signal_type')` contains 'error' or 'fail'.
6. **Recent Judgments**: Retrieve last 10 entries from `judgment_entries` with extracted `category` and `confidence`.
7. **Schema Validation (if needed)**: Use `PRAGMA table_info(table)` to confirm column names.

### How to Verify
- All queries execute without SQL errors. 
- Outputs show meaningful counts > 0 if data exists; otherwise tables may be empty.
- Compare counts with expectations (e.g., error activations should reflect recent failures).

## 异常处理
### Edge Cases
- **DB missing**: file 'harness/state.db' not found; then harness may not be initialized.
- **JSON extraction fails**: If payload is not valid JSON, `json_extract` returns NULL. Handle with COALESCE or check schema.
- **Empty tables**: If no data yet, indicate "no entries".

### Fallback
If direct DB query fails:
1. Attempt `python harness/harness_daemon.py analyze` (with correct path separators).
2. Check if harness process is running (`tasklist` on Windows, `ps` on Unix).
3. Manually inspect latest log files in `harness/` for error messages.
```