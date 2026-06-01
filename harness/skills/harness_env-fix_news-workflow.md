```
```markdown
---
name: harness-post-change-health-check
description: Systematic diagnostic checklist to validate harness integrity after any configuration or code modification.
tags: [harness, diagnostics, configuration-validation, mcp, hooks, prethink:exploration]
triggers:
  - After modifying settings.local.json, settings.json, hooks, MCP wrappers, or any harness infrastructure
  - When a previous session ended with configuration fixes
  - Before starting a task that depends on harness features (MCP servers, web fetch, observers)
  - User mentions "harness broke", "check config", "are hooks working", or similar concerns
version: 1
harness_confidence: 0.85
---

# Harness Post-Change Health Check

## 执行逻辑
### When to Use
Run this checklist whenever the project's harness (Claude Code extensions, MCP servers, hooks, observers, settings layers) has been modified. This prevents silent breakage from cascading config errors and ensures guard layers are active before the next task.

### Step-by-Step
1. **Memory Audit**: Read `memory/MEMORY.md` for any recent entries about harness failures, missing settings, or broken connections. This reveals known brittle spots.
2. **Git Diff Review**: Run `git diff` (or `git diff --stat`) to identify all modified files. Look for changes to:
   - `.claude/settings.local.json`, `.claude/settings.json`
   - `mcp_wrapper.py` or any MCP server configuration
   - Hook scripts referenced in settings
   - Project-specific packages (e.g., `duonews/`)
3. **Settings Layering Check**: Ensure critical settings (`skipWebFetchPreflight`, hook paths, MCP server definitions) exist in the active layer (usually `settings.local.json`). Verify that `settings.json` exists if expected.
   - If a setting is documented in memory but missing from the active file, add it.
4. **Hook Presence**: If hooks are defined, confirm the hook scripts exist at the exact paths specified and are executable/syntactically correct.
5. **MCP Server Connection Test**: For each MCP server defined, attempt a minimal import or connectivity check. If a wrapper (e.g., `mcp_wrapper.py`) is used, verify it doesn't block all servers.
6. **Package Import Sanity**: For any local package touched by the diff (e.g., `duonews`), run `python -c "import <package>"` to catch broken imports.
7. **Destructive Change Flagging**: Log any known issue entries that match the current changes, and flag them in the session notes.

### How to Verify
- All critical settings from memory are present in the active settings file.
- No import errors or MCP connection failures in the quick tests.
- Git diff shows expected changes only; no accidental overwrites.

## 异常处理
### Edge Cases
- **settings.json missing but settings.local.json present**: Determine if defaults are intentionally omitted; if hooks/MCPs rely on merged layers, create a minimal `settings.json`.
- **MCP wrapper bypass active**: If `mcp_wrapper.py` is known to break connections, verify alternative paths are enabled (e.g., direct MCP calls).
- **Multiple sessions modifying same files**: Run the checklist after each session's changes; cross-session drift is a known failure mode.

### Fallback
- If a setting is missing but uncertain of correct value, consult `memory/MEMORY.md` entries for past fixes.
- If MCP servers still fail after known fixes, fall back to direct HTTP requests or user-provided data; document the failure in memory.
```