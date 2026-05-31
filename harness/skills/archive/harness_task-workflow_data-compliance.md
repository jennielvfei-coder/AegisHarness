```
```markdown
---
name: harness-error-accumulation-diagnostic
description: Diagnose harness running state and error accumulation by querying judgment graph, hypotheses, and signal buffer
tags: [data-compliance, news-workflow, ai-governance, prethink:exploration, harness-health, error-audit, diagnostic]
triggers:
  - User asks about harness status, error counts, or overall health
  - System behavior indicates possible error accumulation in harness
  - Routine health check of the harness agent
version: 1
harness_confidence: 0.9
---

# Harness Error Accumulation Diagnostic

## 执行逻辑

### When to Use
Use this skill when the user asks "我的harness现在运行如何了？", "积累了多少错误？", or any question about harness health, judgment quality, or error accumulation. It provides a structured way to audit the internal state via both the harness daemon and direct database queries.

### Step-by-Step

1. **Attempt official analyze command first**
   Run the harness daemon's built-in analysis:
   ```bash
   python "harness/harness_daemon.py" analyze 2>&1
   ```
   This may give aggregated trends, judgment graph summaries, and error frequencies. If it succeeds (exit 0), present the output directly.

2. **Fallback if analyze command fails**
   If the command fails (path error, missing module, exit code ≠ 0), proceed to manual diagnosis. This is common when paths contain backslashes or the daemon has not been initialized.

3. **Quantify judgment graph health**
   Query the `judgment_entries` table to get total entries and average confidence. This reveals overall activity and belief strength.
   ```sql
   SELECT COUNT(*), AVG(CAST(json_extract(payload, '$.confidence') AS REAL))
   FROM judgment_entries;
   ```

4. **Identify error/failure related feature activations**
   Count activations linked to errors or failures. This directly answers "积累了多少错误？"
   ```sql
   SELECT COUNT(*)
   FROM feature_activations
   WHERE json_extract(payload, '$.type') LIKE '%error%'
      OR json_extract(payload, '$.type') LIKE '%fail%';
   ```

5. **Inspect hypotheses and their status**
   List all hypotheses with their ID, status, and description. Active or unresolved hypotheses may indicate ongoing error patterns.
   ```sql
   SELECT hypothesis_id, status, json_extract(payload, '$.description')
   FROM hypotheses;
   ```

6. **Audit signal buffer for error signals**
   Count error/fail signals in the signal buffer (unprocessed signals can indicate backlog).
   ```sql
   SELECT COUNT(*)
   FROM signal_buffer
   WHERE json_extract(payload, '$.signal_type') LIKE '%error%'
      OR json_extract(payload, '$.signal_type') LIKE '%fail%';
   ```

7. **Summarize recent judgment entries**
   Fetch the last 10 judgment entries to see latest category and confidence trends.
   ```sql
   SELECT entry_id, json_extract(payload, '$.category'), json_extract(payload, '$.confidence')
   FROM judgment_entries
   ORDER BY entry_id DESC
   LIMIT 10;
   ```

8. **Optional: schema validation**
   If tables are unexpected, dump schemas with:
   ```sql
   PRAGMA table_info(judgment_entries);
   PRAGMA table_info(hypotheses);
   -- ... etc.
   ```

9. **Interpret and report**
   Combine the numbers:
   - Total judgment entries and average confidence → overall engagement.
   - Number of error/fail activations → accumulated error footprint.
   - Hypothesis statuses → automated correction progress.
   - Error signals in buffer → backlog of unresolved signals.
   - Recent judgments → current trajectory.

   Deliver a concise summary: "Harness has X judgment entries, average confidence Y. Z error/fail activations recorded. N hypotheses active. M error signals in buffer."

### How to Verify
- After reporting, ask the user if the numbers align with their expectations.
- If error counts are high, suggest further investigation (e.g., drill into specific error categories or hypothesis chains).
- Check if the harness daemon process is actually running (`tasklist` or `ps`) if the analysis command fails completely.

## 异常处理

### Edge Cases
- **Database does not exist or is locked:** Wrap queries in a try/except and suggest the harness may not have been started.
- **JSON fields missing expected keys:** Use safe extraction `json_extract(payload, '$.key')` and handle NULLs gracefully in the report.
- **Path issues on Windows:** Always use forward slashes in Bash commands and quotes around paths containing spaces.
- **No hypotheses or empty buffer:** Report 0 and continue; this is normal for a healthy harness.

### Fallback
If all SQL queries fail, attempt to start the harness daemon with `python harness/harness_daemon.py start` and then re-run the analyze command. If that also fails, instruct the user to manually verify harness installation and logs.
```