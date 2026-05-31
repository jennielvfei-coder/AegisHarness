```markdown
---
name: diagnose-harness-state-and-errors
description: Diagnose the operational state of the harness system, count accumulated errors, and present aggregated trends by querying the judgment graph and signal buffer.
tags: [ai-governance, data-compliance, news-workflow, prethink:exploration]
triggers:
  - User asks "how is the harness running?" or "how many errors have accumulated?"
  - Routine health check of harness daemon
version: 1
harness_confidence: 0.8
---

# Diagnose Harness State and Accumulated Errors

## 执行逻辑
### When to Use
When the user needs a quick diagnostic summary of the harness daemon's health—specifically operation status, error counts, recent judgments, and hypothesis activity—this workflow provides a repeatable path from high‑level command to low‑level SQL queries.

### Step-by-Step
1. **Primary Approach: Invoke the harness analyze subcommand**
   - Execute `python harness/harness_daemon.py analyze` (adjust separator and working directory as needed).
   - If the command succeeds, parse the output and present a summary.
2. **Fallback: Direct database interrogation**
   - Connect to the SQLite state database (typically `harness/state.db`).
   - Query the following tables and views to gather health metrics:
     a. `judgment_entries` – total count, average confidence, most recent entries (categories and confidence).
     b. `feature_activations` – count of rows where payload type contains `error` or `fail`.
     c. `hypotheses` – list all hypotheses with status and description.
     d. `signal_buffer` – count of error/fail signal types.
   - Use `json_extract` to navigate payload columns; encapsulate queries in a single Python script (`-c`) to avoid quoting issues.
3. **Enrich with additional context**
   - Optionally retrieve schema from key tables (`PRAGMA table_info`) if unfamiliar with the exact structure.
   - Check `belief_traces`, `false_belief_log`, and `dcl_judgments` for deeper insights if errors appear.
4. **Summarize and format**
   - Aggregate key numbers: judgment count, average confidence, error‑related activations, active/failed hypotheses, queued error signals.
   - Present findings concisely, highlighting anomalies.

### How to Verify
- Confirm that the output includes:
  - Total judgment entries and average confidence.
  - Error‑related feature activation count.
  - List of available hypotheses with statuses.
  - Recent judgment categories.
- Cross‑check at least one number manually with a simple SQLite query.

## 异常处理
### Edge Cases
- **Path separator issues**: Windows and Unix differ in backslash/forward slash; prefer `pathlib` or raw strings when constructing file paths.
- **Command not found**: If `harness_daemon.py` is missing, immediately fall back to direct SQLite queries.
- **Database locked**: Retry after a short delay (e.g., 1 second) or read from a backup if available.
- **Empty tables**: Treat missing tables as “no data” and report clearly—do not raise errors.
- **Large payloads**: Use `json_extract` to filter only necessary fields, avoiding scanning entire JSON blobs.

### Fallback
- If both the daemon command and the SQLite database are unavailable, inspect the file system:
  - Look for log files in the `harness/` directory.
  - Use `dir`/`ls -la` to check for recent modifications (e.g., `.log`, `.db‑journal` files).
- When even filesystem access fails, report that the harness appears to be offline and recommend a restart or manual check.
```