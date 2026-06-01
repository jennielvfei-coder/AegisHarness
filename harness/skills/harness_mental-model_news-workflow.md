```markdown
---
name: dependency-validation-sequential-fallback
description: When parallel tool calls for pre-workflow checks fail, methodically fallback to sequential checks to validate all dependencies before proceeding.
tags: [news-workflow, data-compliance, ai-governance, prethink:exploration]
triggers:
  - When a complex skill or workflow requires environment validation and parallel execution encounters errors
version: 1
harness_confidence: 0.85
---

# Pre-Workflow Dependency Validation with Sequential Fallback

## 执行逻辑
### When to Use
- Before executing a multi-step workflow (e.g., DuoNews), you need to ensure all essential components (packages, databases, CLIs) are present and functional.
- Your initial attempt to run parallel checks results in a command failure, causing other parallel calls to be canceled.

### Step-by-Step
1. Identify the list of critical dependencies (e.g., Python package import, database connectivity, CLI tool existence, module internal functions).
2. Attempt a minimal parallel validation, using a single complex command if possible, or limited parallel calls.
3. If any command fails or parallel calls are canceled, switch to **sequential verification**:
   - Execute each check one after another.
   - After each check, note success/failure.
   - Do not proceed to next until current completes.
4. Collect all results; if any critical dependency missing or broken, propose remediation (install package, create missing file, etc.) or gracefully abort.

### How to Verify
- After sequential execution, all checks should complete without errors.
- The workflow can then safely proceed.

## 异常处理
### Edge Cases
- Partial failures in sequential checks: still continue with remaining checks to get full picture, then decide.
- Timeouts: set appropriate timeout per check; if stalled, skip after timeout and mark as unknown.
- Not all dependencies are equal: prioritize checks that may block the entire workflow; non-critical ones can be checked later within the workflow.

### Fallback
- If dependencies cannot be satisfied, abort with clear message listing missing/failed items and suggested fixes.
```