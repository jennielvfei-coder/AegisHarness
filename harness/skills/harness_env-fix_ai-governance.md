```markdown
---
name: harness-fix-verification
description: Systematic verification of harness bug fixes using git diff, database schema checks, and runtime probing.
tags: [ai-governance, data-compliance, prethink:correction]
triggers:
  - When a list of harness bugs with claimed fixes is presented.
  - When asked to “check the repair status” or “verify fixes” in the project harness.
version: 1
harness_confidence: 0.9
---

# Harness Fix Verification

## 执行逻辑
### When to Use
- After receiving a bug‑fix report (e.g., table of bugs, statuses, and key changes) in the self‑modifying harness.
- When no automated test suite is available and fixes must be confirmed manually from recent git changes and database state.

### Step-by-Step
1. **Parse the bug list** – extract bug IDs, claimed status (`✅ 修复`), and the “关键变更” (key changes) for each row.
2. **Map each bug to a verification strategy**:
   - **“96% 损坏” / data corruption** → Check database schema (`PRAGMA table_info`) and compare column names/types to expected, run a query to count recent valid entries.
   - **“NameError 静默崩溃” / import or signal** → `git diff HEAD -- <file>` inspect the exact diff for added `import re` at module level, and for any signal handler restoration.
   - **Other code changes** → Use `git diff --stat` to confirm which files were modified, then drill into relevant hunks.
3. **Execute verifications**:
   - Run `git diff HEAD --stat` to get an overview of changed files.
   - For each suspect file (e.g., `hooks.py`, `health_probes.py`), inspect the diff with `git diff HEAD -- <file>`.
   - For database‑related bugs, connect to the SQLite DB and check table info and a sample query.
   - If applicable, verify that old corrupted history files have been removed (`git log --oneline` or file listing).
4. **Correlate findings with bug statements** – confirm that the observed diff matches the key change description.
5. **Report**:
   - ✅ Fix confirmed if git diff / DB state aligns with the described change.
   - ❌ Fix missing or incomplete if the expected change is absent.
   - ⚠️ Partial if only some symptoms resolved (flag for further action).

### How to Verify
- Every claimed fix must have a concrete artefact (diff hunk, schema change, deleted file) that directly addresses the bug.
- If a bug is marked ✅ but no supporting artefact exists, the report is inconsistent → ask for re‑verification or apply missing changes.

## 异常处理
### Edge Cases
- **Multiple commits**: Use `git log --oneline -n <N>` to find the relevant commit(s). If fixes are staged but not committed, `git diff HEAD` still works.
- **User‑level configuration changes**: Some bugs may involve settings files (e.g., `.claude/settings.local.json`); check both repository and global user config paths.
- **Database path not writable**: Run read‑only `PRAGMA`; if table info is inaccessible, fall back to inspecting migration scripts in the diff.
- **Corrupted data still present after fix**: The fix might only prevent future corruption; verify by checking if the corruption mechanism is removed, then optionally run a repair query.

### Fallback
- If shell access is unavailable, request the user to run the verification commands and paste the output.
- If the harness database is locked or missing, rely solely on `git diff` and file listing.
```