```
```markdown
---
name: harness-active-guard-layer
description: Automatically validate harness configuration at session start and apply fixes via env-fix skills
tags: [harness, env-fix, preflight, auto-check]
triggers:
  - At the start of a new session (before user's first task)
  - When harness configuration files (settings, hooks, memory) may be stale
  - When a previous session recorded a harness-related failure
version: 1
harness_confidence: 0.9
---

# Harness Active Guard Layer

## 执行逻辑
### When to Use
The harness should actively guard session integrity, not merely store past failures.  
Use this skill at session start to automatically verify and repair the harness configuration (settings, hooks, memory-derived preflight checks) before the user begins work.

### Step-by-Step
1. **Load current configuration**  
   Read `settings.local.json`, `settings.json`, active hooks configuration, and relevant entries from `memory/MEMORY.md`.
2. **Compare against mandatory baseline**  
   The baseline includes:
   - `skipWebFetchPreflight = true` in settings
   - All required hooks present and executable
   - All memory items tagged `[preflight]` have been applied in the current session
3. **Detect discrepancies**  
   If any required entry is missing or stale, flag it.
4. **Invoke env-fix skill**  
   For each discrepancy, trigger the corresponding env-fix routine (e.g., write missing setting, install missing hook, re-apply memory-derived fix).
5. **Report status**  
   Output a concise summary: which checks passed, which were repaired, and any that require manual intervention.

### How to Verify
After applying fixes, re-run the configuration check. All mandatory items should now be present.  
Additionally, test a light harness operation (e.g., health-check hook) to confirm end‑to‑end validity.

## 异常处理
### Edge Cases
- **`settings.json` missing entirely** — Create a minimal valid JSON with required fields, then merge with `settings.local.json`.
- **Hooks reference non‑existent scripts** — Disable the broken hook, log the error, and create a memory to restore it once the script is available.
- **User has intentionally changed/removed a required setting** — Respect local intent unless it compromises harness health; if so, warn and ask for confirmation before overwriting.

### Fallback
If automatic repair fails (e.g., insufficient permissions, corrupted file), report the exact problem to the user with clear manual resolution steps.  
Store a memory (`memory/MEMORY.md`) of the failure so that the guard layer re‑attempts the fix next session and does not silently degrade.
```