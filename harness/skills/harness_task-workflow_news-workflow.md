```markdown
---
name: duonews-workflow-diagnostics
description: Debug and execute DuoNews multi-source news aggregation when dependencies or DB state are broken
tags: [news-workflow, ai-governance, data-compliance, prethink:exploration]
triggers:
  - DuoNews skill fails to import or reports NO_PRIOR
  - duonews state.db is stale or missing today's data
  - multi-source news collection pipeline breaks
version: 1
harness_confidence: 0.55
---

# DuoNews Workflow Diagnostics

## 执行逻辑
### When to Use
Activate when `duonews` skill invocation fails pre-flight checks — missing Python imports (`ModuleNotFoundError`), empty `state.db`, absent `anysearch_cli.py`, or corrupted `__pycache__`.

### Step-by-Step
1. **Verify package presence at known paths**
   ```
   D:\Claude\duonews\          ← primary module directory
   D:\Claude\duonews\state.db  ← SQLite with `news_snippets` table
   C:\Users\Chucky\.claude\skills\anysearch\scripts\anysearch_cli.py
   ```
   Check git history: `git -C D:\Claude log --oneline -5 -- duonews/`

2. **Validate DB state**
   ```sql
   SELECT COUNT(*) FROM news_snippets WHERE date = '<TODAY>';
   SELECT COUNT(*) FROM news_snippets WHERE date = '<TODAY>' AND section = 'github-trending';
   ```
   If zero rows → run collection; if section counts mismatch → re-run that section.

3. **Probe Python module chain**
   ```bash
   python -c "import duonews; print(duonews.__file__)"
   python -c "import duonews.search"
   python -m duonews --help
   pip show duonews   # returns nothing if not installed as editable pkg
   ```

4. **Run core functions in order**
   - `find_recent_report('<TODAY>')` → returns prior report text or `NO_PRIOR`
   - `extract_judgment_baseline('<TODAY>')` → baseline for AI governance evaluation
   - Scrape sources: GitHub Trending + anysearch feeds → insert into `news_snippets`
   - Sections expected: `github-trending`, plus others from `anysearch_cli.py` queries

5. **Clear stale bytecode if imports fail after code changes**
   ```powershell
   Remove-Item D:\Claude\duonews\__pycache__ -Recurse -Force
   ```

### How to Verify
- `news_snippets` has >0 rows for today across all expected sections
- `find_recent_report(today)` returns non-empty text
- No `ModuleNotFoundError` on `import duonews.search`

## 异常处理
### Edge Cases
- **Package installed but unimportable**: corrupted `__pycache__` → delete and reimport.
- **Database locked**: retry with timeout; close other CLI instances accessing `state.db`.
- **anysearch_cli.py missing**: dependency broken; locate alternative path or re-install anysearch skill.
- **GitHub Trending returns 0 rows**: API rate limit hit or parsing changed → check raw HTML/API response.

### Fallback
If all diagnostics fail and duonews remains unrecoverable:
1. Run anysearch directly for today's news (single-source fallback)
2. Manually construct news summary from cached sources
3. Tag session for later duonews repair with full traceback
```