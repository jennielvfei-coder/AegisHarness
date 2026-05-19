```markdown
---
name: context-lazy-skill-injection
description: Prevent context overflow by injecting only skill triggers, loading full definitions on demand.
tags: [context-management, skill-injection, ai-governance, news-workflow, data-compliance]
triggers:
  - Designing a harness / skill loading system for an AI assistant.
  - Encountering context limits due to accumulated skill definitions in pre-session injection.
version: 1
harness_confidence: 0.95
---

# Lazy‑Loaded Skill Injection

## 执行逻辑
### When to Use
- The assistant’s system prompt or pre‑session injection (e.g., `CLAUDE.md`, injector output) contains numerous full skill descriptions, threatening the context window.
- You are building a harness that injects approved skills and pending‑review notifications into each new session.
- Session‑start context must remain small while keeping many skills available.

### Step‑by‑Step
1. **Maintain a skill registry** – A single file (e.g., `CLAUDE.md`, `skills_registry.yaml`) that lists for every active skill:
   - `name`
   - One‑line trigger description (natural language, example: “When the user says ‘daily news’, execute `multi-source-news-workflow`”)
   - Path to the full skill definition file.
2. **Strip full skill instructions** from the system message and injector output. Never paste the entire `SKILL.md` content into the initial context.
3. **Inject only the registry** (or a compact summary) during session start.
   - Example snippet:
     ```
     Available skills (trigger phrase → file):
     - daily news → skills/harness/multi-source-news-workflow/SKILL.md
     - data compliance audit → skills/harness/compliance-audit/SKILL.md
     ```
4. **Implement on‑demand loading** – When the assistant detects a trigger phrase in the user’s input:
   - Use the registry to find the correct `SKILL.md` path.
   - Read and execute the full skill from that file.
   - If the environment supports tools (e.g., `Bash`, `Read`), implement a helper function to load a skill by name.
5. **Store full skill definitions** as standalone Markdown files:
   - Directory: `.claude/skills/harness/<skill‑name>/SKILL.md`
   - Contents: detailed steps, fallback plans, output templates, example dialogs, etc.
6. **Handle pending‑review skills** – Inject only a short notification (e.g., “3 skills awaiting review: news‑summary‑v2, …”) instead of their full content.

### How to Verify
- Initial context size is significantly smaller than before the refactor.
- The assistant can correctly invoke all registered skills by reading the appropriate file on demand.
- The registry stays synchronised with the actual skill files.
- Pending‑review notifications do not leak full descriptions.

## 异常处理
### Edge Cases
- **Skill file missing** – Gracefully degrade by informing the user and offering to fall back to a generic help workflow.
- **Trigger ambiguity** – If multiple skills have similar triggers, the assistant can ask a clarifying question before loading any file.
- **Read errors / permissions** – The assistant should retry once or fall back to a pre‑loaded summary (if available) or report the problem.
- **Registry overflow** – When the number of registered skills grows too large for a single message, group them by category and show only a top‑level list, with an option to list all.

### Fallback
- If the environment cannot load external files (e.g., no filesystem tool), keep a minimal “skeleton” of the skill (1‑2 lines) in the registry and drop all non‑critical definitions.
- Provide a `help all` trigger that dynamically lists the complete registry from disk.
```