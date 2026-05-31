```
```
```markdown
---
name: proactive-configuration-audit
description: Systematic approach: before modifying or fixing a local project harness, first check memory for known issues, review recent changes, inspect configuration files, and run diagnostic commands.
tags: [ai-governance, data-compliance, prethink:exploration]
triggers:
  - MCP servers fail to connect
  - Harness hooks stop working
  - Config changes suspected
  - Before applying any fix to Claude Code settings
version: 1
harness_confidence: 0.85
---

# Proactive Configuration Audit

## 执行逻辑

### When to Use
Apply this mental model whenever local Claude commands fail, MCP servers report connection errors, or harness behaviour deviates unexpectedly. Also useful before committing configuration changes.

### Step-by-Step

1. **Read Collective Memory**  
   Open `~/.claude/projects/<project>/memory/MEMORY.md` (or equivalent) to retrieve previously recorded failure patterns, fixes, and architecture contradictions.

2. **Survey Recent Changes**  
   Run:
   - `git diff --stat` to see which files have been modified
   - `git diff` for full content diffs

3. **Inspect Active Configuration**  
   Load:
   - `settings.local.json` and `settings.json` (check for missing entries like `skipWebFetchPreflight`, broken MCP wrapper settings, or missing hooks)
   - Any `.claude/settings.json` or `.claude/hooks.json` that control guard behaviour

4. **Isolate the Failure**  
   Execute a minimal reproduction command (e.g., `python -c "import mcp_wrapper; ..."`) or run a single MCP test. Watch for tracebacks and exit codes.

5. **Cross‑reference with Known Issues**  
   Compare the current symptoms with memory entries (e.g., "MCP Wrapper Failure", "skipWebFetchPreflight Not Set", "Harness Active Guard Layer"). If a match is found, apply the documented fix; otherwise, proceed to deeper investigation.

### How to Verify
- The failing MCP server connects successfully.
- Harness hooks fire correctly in a subsequent session.
- Configuration files contain the expected keys (e.g., `skipWebFetchPreflight: true`).
- A quick end‑to‑end test of the affected tool/command passes.

## 异常处理

### Edge Cases
- **Memory file absent** – `MEMORY.md` does not exist; skip step 1 but still check for other notes in the project.
- **No git repository** – Use file‑system timestamps to identify recent modifications.
- **Settings file missing** – Assume defaults; document the absence for future guards.

### Fallback
If the systematic audit does not reveal the cause, escalate to a full harness restart or regenerate the hook definitions from a known-good template. Record the new failure pattern in `MEMORY.md` once resolved.
```