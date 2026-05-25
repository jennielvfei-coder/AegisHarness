```
```
```markdown
---
name: harness-gap-contradiction-analysis
description: Introspect an AI governance harness to identify knowledge‑action gaps, detect self‑contradictions in improvement plans, and prioritise fixes.
tags: [ai-governance, privacy, news-workflow, data-compliance, contract-review]
triggers:
  - "harness还有什么需要补足提升的地方"
  - "分析当前系统的缺陷"
  - "审查架构并提出改进优先顺序"
  - "这个方案自相矛盾吗"
version: 1
harness_confidence: 0.82
---

# Harness Gap & Contradiction Analysis

## 执行逻辑

### When to Use
- After multiple sessions where the harness under‑performs or forgets lessons.
- Before implementing new features, to avoid reinforcing latent contradictions.
- When a proposed improvement plan feels intuitively off but the reason isn’t clear.

### Step‑by‑Step

1. **Load & diff current state**
   - Read the memory file, configuration, key daemon sources (`harness_daemon.py`, `injector.py`).
   - Run `git log` summarised recent changes.
   - Identify what the system “knows” vs what it actually does.

2. **Surface the knowledge‑action chasm**
   - List features that are documented, seeded, partially built but never completed (e.g., `preflight_check` fragment exists, no executable check).
   - Flag patterns like “知道但没做” (known failure mode described but not prevented).

3. **Map dependencies & 隐藏成本**
   - For each proposed fix, ask *“如果这个改动需要先有另一个未实现的能力，顺序是什么？”* (Does P0 truly need P2?)
   - Quantify context‑bloat risk: will an extra check output 1 line or 10? Aim for **single‑line status indicators** not explanatory paragraphs.
   - Identify symbiotic pairs (e.g., executable preflight ↔ cross‑session data store).

4. **Detect self‑contradictions in the improvement plan**
   - Search for paradoxes like “治疗一种病时加重另一种病” (curing one illness while worsening another).
   - Example: Reducing injector bloat by adding more preflight output – mark as contradiction and re‑design for minimal signal.

5. **Prioritise ruthlessly**
   - Upgrades that close the knowledge‑action gap and serve as building blocks for other fixes get highest priority.
   - One‑shot config fixes (`skipWebFetchPreflight`) rank low unless they block other work.
   - Anything that increases context size is demoted unless no alternative exists.

### How to Verify
- Every identified gap must be accompanied by a concrete, falsifiable statement (e.g., “preflight check exists as text but is never executed by code”).
- Each contradiction must be explicitly resolved; if unresolvable now, create a **debt tracker** with a trigger condition.

## 异常处理

### Edge Cases
- **Unfixable contradictions**: e.g., low context budget and high observability need. Mark as “resource‑bound tradeoff” and record the accepted risk.
- **Dormant failures**: memory entries about errors that haven’t recurred recently – evaluate whether the underlying condition is still present before spending cycles.

### Fallback
If the full analysis is too heavy (session context low), use a rapid alternate:
1. Ask *“为了让这个改进生效，什么必须成立？”*
2. Check whether that prerequisite exists today.
3. If not, the improvement is blocked – only then decide to build the prerequisite or postpone.

> This skill itself is a **meta‑skill**: apply it whenever you plan significant harness modifications to avoid architecture‑level mistakes.
```