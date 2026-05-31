# News Agent Personalization — Design Spec

**Date:** 2026-05-24
**Status:** draft
**Goal:** Make the daily news workflow progressively personalized — learn 菲菲's news preferences from feature activation history and natural-language feedback, inject personalized context at SessionStart. Zero change to daily report format. Minimal token overhead (~150 tokens/day).

---

## Architecture Overview

Three data flows, all behind the scenes. The user still says "今日新闻" and gets the same 5-section daily report.

```
┌─────────────────────────────────────────────────────────┐
│                    SessionStart                          │
│  intent_matcher → "news" intent → query feature_acts    │
│  → inject ~5 lines of personalized context (~150 tok)   │
├─────────────────────────────────────────────────────────┤
│                  News Workflow (unchanged)                │
│  MCP + WebFetch + arXiv → dedup → template → write      │
│                         +                               │
│  Stream 1: feature_finder matches news → A-H labels     │
│            (local Python, zero LLM tokens)              │
├─────────────────────────────────────────────────────────┤
│                    SessionStop                           │
│  observer reads signal_buffer → updates pref weights    │
│  Stream 3: feedback → weight delta → next-day injection │
└─────────────────────────────────────────────────────────┘
```

---

## Stream 1: Feature Activation (News → Cognitive Labels)

### What it does
Each news item compiled into the daily report gets matched against the 39-entry feature library (A-H anomaly categories) using local Python embedding cosine similarity.

### How it works
1. `feature_finder.py` already exists in `harness/` — reads `feature_library_entries` table, encodes news headlines with BGE-small-zh, computes cosine similarity against feature embeddings.
2. Matches above threshold (0.6) are written to `feature_activations` table.
3. This runs as part of the news compilation step — after titles are collected, before the report is written.

### Data flow
```
news headline → encode(BGE-small-zh) → cosine vs 39 feature embeddings
  → matches above 0.6: write to feature_activations(date, feature_id, activation_strength, source_snippet_ids)
```

### Token cost: **0**
All computation is local Python (embedding + cosine). No LLM calls.

### Existing infrastructure
- `feature_library_entries` table: 39 rows (A1-H6), with `embedding` column
- `feature_activations` table: 13,442 rows already accumulated
- `feature_finder.py`: already exists in `harness/`

### What changes
- Wire `feature_finder.py` into the news workflow (call after dedup, before template rendering)
- Optionally append cognitive labels to the daily report's overview table (e.g., `🧠 D3+H1` after source rating)

---

## Stream 2: Preference Injection (SessionStart → Personalized Context)

### What it does
When `intent_matcher` detects a "news" intent at SessionStart, query 菲菲's top-activated anomaly types and domains from the last 30 days, inject a short personalized context block into the session.

### How it works
1. `intent_matcher.match_intent()` already returns intent + domains.
2. New function `inject_news_preferences()` added to `intent_matcher.py`:
   - Queries `feature_activations` grouped by `feature_id` for last 30 days
   - Queries `news_snippets` for top entity domains
   - Ranks by activation count × strength
   - Formats ~5 lines of context
3. `inject_workflow_context()` in `intent_matcher.py` appends this to its output.

### Injection format (~150 tokens max)
```
📊 菲菲认知偏好档案（30天统计）：
  高关注特征：D3利润池迁移(23), H1能力-治理裂缝(19), B1叙事-现实脱钩(17)
  高关注领域：AI Agent生态, 芯片供应链, arXiv因果论文
  → 今日日报自动偏重上述方向和对应的底层异常类型
```

### Token cost: **~150 tokens** (injected context, only on news-triggered sessions)

### What changes
- Add `_query_news_preferences()` to `intent_matcher.py`
- Modify `inject_workflow_context()` to accept and append preference data
- `harness_daemon.py cmd_inject` already calls `inject_workflow_context` — no change needed there

---

## Stream 3: Feedback Loop (Natural Language → Weight Update)

### What it does
After reading the daily report, 菲菲 says something like "Agent那篇分析不错, 芯片部分太浅了." The existing `hooks.py` `UserPromptSubmit` captures this as a preference/correction signal. Observer processes it on Stop and updates feature weights.

### How it works
1. **Capture**: `hooks.py` `user_prompt_submit()` already detects "preference" and "correction" signals via keyword matching and writes to `signal_buffer`. No change needed.
2. **Process**: `observer.py` (called by `harness_daemon.py observe` on SessionStop) already processes signal_buffer. Extend it to:
   - Detect feedback that references the daily report (by date/timing proximity)
   - Extract mentioned topics (via simple keyword matching against news snippet entities)
   - Write weight deltas to a new `news_preference_weights` table
3. **Apply**: Stream 2 reads these weights next session.

### Signal detection keywords (already in hooks.py)
```
preference: "以后都", "我总是", "偏好", "记住", "默认", "下次", "永远"
correction: "不对", "错了", "不是这样", "太浅", "不够", "有用", "不错"
```

### Token cost: **0**
All processing is SQLite writes + simple keyword matching. No LLM.

### What changes
- Add `news_preference_weights` table to state.db schema
- Extend observer's preference handling to update this table
- Stream 2 reads from this table alongside feature_activations

---

## New/Modified Files

| File | Change | Effort |
|------|--------|--------|
| `harness/intent_matcher.py` | Add `_query_news_preferences()`, modify `inject_workflow_context()` | Small |
| `harness/feature_finder.py` | Wire into news workflow (already exists) | Small |
| `harness/observer.py` | Extend preference handling for news feedback | Small |
| `harness/indexer.py` | Add `news_preference_weights` table schema | Small |
| Daily news workflow | Call `feature_finder` after dedup, before template | Small |

No new files needed. All infrastructure exists.

---

## What Does NOT Change

- Daily report 5-section format (速览 → 重点分析 → 因果追踪 → Prophet → 数据源)
- Trigger: still "今日新闻" or natural language variants
- Data sources: World News API MCP + arXiv + Chinese WebFetch sources
- Fingerprint dedup + story arc linking
- Obsidian vault output path

---

## Success Criteria

1. After 3 consecutive days of news + feedback, Stream 2 injection shows visibly different domain weights than day 1 baseline
2. Feature activation labels appear in daily reports without breaking the 3000-word budget
3. Feedback signals reliably captured in `signal_buffer` with `preference` type
4. Zero additional permission prompts for the user
5. Token overhead per news session < 200 tokens (vs. current baseline)

---

## Risk: Feedback Ambiguity

Natural language feedback is inherently ambiguous. "Agent那篇不错" could mean "interesting topic" or "good analysis quality." Initial implementation uses simple keyword matching against news snippet entities. If precision is too low, we can add a lightweight disambiguation step (e.g., extract both topic AND sentiment: "Agent topic + positive").

**Mitigation:** Start with keyword matching. Measure false positive rate after 7 days. Only add complexity if needed.

---

## Implementation Plan

Phase 1 (this session): Wire Stream 1 + Stream 2 — feature activation during compilation, preference injection at SessionStart
Phase 2 (after 3+ days of data): Stream 3 — feedback loop with weight updates
Phase 3 (after 7+ days): Verify, tune thresholds, add disambiguation if needed
