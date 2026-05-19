Now let me analyze the correction and generate the appropriate skill update.

---

**Analysis of the session context error:**

The core mistake: Assistant over-reached — updating `claude for legal-zh` refiner when the user's original demand was scoped to the **news workflow** only. The user explicitly said "不需要更新 claude for legal-zh 里面的refiner，更新错了" (don't need to update the refiner in claude for legal-zh, updated the wrong thing). This indicates a **scope-discipline failure** in a multi-skill interconnected Harness system.

---

**Classification:**

| Field | Value | Reasoning |
|-------|-------|-----------|
| SKILL_TYPE | `task-workflow` | This is about *how to execute* a bounded update/rollback without cross-contaminating unrelated skill domains |
| QUALITY_SCORE | `0.68` | Concrete correction exists, but the original demand is paste-truncated (partial context). Correction is explicit and repeatable. |
| ACTION | `merge` | Existing skills already at v1 with same tags, qs=0.54. This correction upgrades the model without creating a duplicate. |

---

**Generated skill:**

```markdown
---
name: harness-scope-discipline-rollback
description: When executing updates or rollbacks in a multi-skill Harness, verify target scope before touching any submodule — never let a domain-specific update leak into unrelated skill files.
tags: [data-compliance, news-workflow, ai-governance]
triggers:
  - User requests a rollback or targeted update to a specific skill/workflow
  - Session involves multiple interconnected skill domains
  - Harness stop hooks or MCP processes being modified
version: 2
harness_confidence: 0.54
parent_skills:
  - harness_mental-model_ai-governance.md (v1, qs=0.54)
  - harness_mental-model_data-compliance.md (v1, qs=0.54)
---

# Harness Scope Discipline — Rollback & Update Containment

## 执行逻辑

### When to Use
Triggered when a user requests a targeted update or rollback to **one specific domain** (e.g., news-workflow only), and the Harness contains multiple interconnected skill directories (e.g., `legal-zh/`, `news/`, `obsidian-vault/`).

### Step-by-Step

1. **Parse scope explicitly** — Identify the exact domain/directory the user named. If the user says "P₂ rollback" or "更新 X workflow"，isolate X to its directory path.

2. **Map transitive dependencies** — List all files that *actually* depend on the target. Use `grep` or Harness metadata to check cross-references. Mark files outside the target domain as **NO-TOUCH**.

3. **Scope gate before execution** — Before any `write`/`edit`/`replace`, run internally:
   - "Is this file inside the target domain?" → Yes: proceed. No: **halt and confirm with user**.
   - If a file in another domain surfaced during dependency mapping, explicitly ask: "File Y in domain Z surfaced as a transitive dependency. Include or exclude?"

4. **Execute only within confirmed scope** — Apply the update/rollback. Log every file touched.

5. **Post-update boundary check** — After execution, diff the change set against the original scope declaration. If any file outside scope was modified, flag it as a **scope violation** and revert it before closing.

### How to Verify
- Ask: "Did I modify any file outside the user-specified domain?"
- Check: `git diff --name-only` matches only the target domain paths
- Confirmation: User should *not* see unrelated skill files in the change summary

## 异常处理

### Edge Cases
- **User mentions multiple domains ambiguously** — Ask for explicit comma-separated list before proceeding
- **Rollback has downstream effects** — Surface them but do *not* auto-execute on downstream files; wait for user authorization
- **Pasted context is truncated** (as in this correction) — Do not infer missing content; request re-paste

### Fallback
If scope violation is detected post-execution:
1. Immediately revert the out-of-scope file(s) to previous version
2. Report to user: "File X in domain Y was incorrectly modified — reverted. Reason: scope discipline check failed."
3. Re-execute only the in-scope portion

## Updated Guidance (v2 — from user correction)
**Explicit correction:** Assistant incorrectly updated `claude for legal-zh` refiner when the user's demand was scoped to the news-workflow (`P₂` rollback) only. The legal-zh domain was completely unrelated.

**Revised rule:** *Never assume sibling skill directories need the same treatment as the target.* A rollback in `news-workflow` does not imply any change in `legal-zh`, `obsidian`, or any other domain — unless the user explicitly names them. When in doubt, ask; never auto-expand scope.
```

---

### Summary

| Aspect | Before (v1) | After (v2) |
|--------|------------|------------|
| Quality | 0.54 | 0.68 |
| Key gap | No scope containment rule | Explicit scope gate before any write |
| Trigger | Broad update/rollback | Targeted domain-specific updates in multi-skill Harness |
| Fallback | None | Auto-revert out-of-scope files + report |

The skill now encodes the specific corrective: **"A rollback in news-workflow does not imply any change in legal-zh"** — preventing the exact cross-domain contamination that occurred in this session.