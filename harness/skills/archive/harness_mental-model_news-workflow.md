```markdown
---
name: news-workflow-refiner-update-isolation
description: When modifying refiner/pipeline configurations for the daily news workflow, isolate changes to the correct workflow files and avoid inadvertently touching unrelated skills (e.g., legal‑zh).
tags: [news-workflow, data-compliance, ai-governance]
triggers:
  - User requests rollback, update, or adjustment of "refiner" or pipeline files in the context of the daily news workflow.
  - User corrects an assistant action that mistakenly changed a different skill’s file (e.g., claude‑for‑legal‑zh refiner).
version: 1
harness_confidence: 0.54
---

# Refiner Update Isolation for Daily News Workflow

## 执行逻辑

### When to Use
- You are asked to update, roll back, or modify refiner files, pipeline step definitions, or quality‑control configurations **that belong to the daily‑news workflow** (`今日新闻 → 8段日报 → Prophet → Obsidian`).
- The conversation mentions `P₂` rollback, `refiner`, `pipeline`, or `config` and the primary **intent** is to adjust the news workflow, not other skills (e.g., legal‑zh, code‑gen, etc.).

### Step-by-Step
1. **Identify the target file set**  
   - Ask yourself: *Which workflow’s refiner is being changed?*  
   - Confirm the absolute or relative path(s) in the Obsidian vault / harness config.  
   - For the daily news workflow, typical paths include:  
     - `skills/daily-news-refiner.md`  
     - `configs/news-workflow-pipeline.json`  
     - `memory/daily-news-trigger/*`  
   - **If the user mentions only “refiner” without specifying which one, prompt:**  
     _“确认一下：你是要更新 每日新闻工作流 的 refiner，还是其他技能（如 legal‑zh）的 refiner？请提供确切文件路径。”_

2. **Before any modification, echo the intended scope**  
   - List every file that will be touched, plus a one‑sentence reason for each.  
   - Ask for explicit approval before execution.

3. **Isolate rollbacks (P₂ scenario)**  
   - When the user says “P₂ 单独回滚到上个版本”, **interpret “P₂” as the specific sub‑component** of the news workflow (e.g., stage‑2 analysis or refiner step), not the whole skill system.  
   - Use version control (git) to revert only the commits/changes that touched the **identified files**. Do not revert unrelated files.  
   - Validate with `git diff --name-only` after the rollback to confirm **only** the daily‑news files were altered.

4. **Double‑check after the change**  
   - After applying any update or rollback, list the files that were modified.  
   - Compare that list against the intended scope.  
   - Run a quick test: check that the news workflow’s main entry point (`daily-news-trigger.md` or equivalent) still references the updated refiner correctly, and that unrelated skills (e.g., `claude‑for‑legal‑zh`) remain untouched.

### How to Verify
- **File integrity:** `git status` shows only the expected files.  
- **Workflow continuity:** The daily news workflow’s trigger command still executes the updated pipeline without errors (dry‑run or minimal run).  
- **Unrelated skills untouched:** Open a random unrelated skill file (e.g., `claude‑for‑legal‑zh`) and confirm no edits by timestamp or diff.

## 异常处理

### Edge Cases
- **Ambiguous refiner name:** User says “更新 refiner” without context. → Ask for explicit path; default to **daily news workflow** only if the conversation was exclusively about the news workflow.  
- **Rollback after multiple changes:** The P₂ component was recently changed together with others. → Use `git log` to isolate the correct commit range and `git revert` with `--no-commit` on the overall merge, then manually stage only the intended files.  
- **User pasted a large block of content (Pasted text #1 +15 lines) to replace part of the refiner:** → Parse exactly which section the user means; if it’s ambiguous, ask to highlight the target section in the refiner file. After insertion, verify no unintended line breaks or syntax corruption.

### Fallback
- If git history is unclear, create a backup of the current refiner file before changes. After update, do a manual diff to ensure the legal‑zh refiner and other unrelated files are unchanged.
- If the mistake already happened (e.g., legal‑zh refiner was wrongly updated), immediately revert that file’s changes and re‑apply only to the daily news refiner.

## Updated Guidance
Explicit correction detected — user corrected assistant output:  
> “P₂ 单独回滚到上个版本 这里错了，我不需要更新 claude for legal-zh 里面的refiner，更新错了，我的原本需求的 [Pasted text #1 +15 lines]”

This skill codifies that **refiner modifications must be scoped to the exact workflow intended**, and that a generic “refiner” request in a news‑workflow context must not spill over into unrelated skills’ configuration files. Always verify the target file path and, when rolling back, isolate the rollback to the specific component (P₂) of the news workflow.
```