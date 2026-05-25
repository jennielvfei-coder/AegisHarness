```markdown
---
name: daily-intelligence-briefing
description: "Multi-source daily briefing workflow with MCP, WebFetch, fallback handling, and template-based output."
tags: [contract-review, news-workflow, ai-governance, ip, data-compliance, employment, data-quality-failure, prethink:exploration]
triggers:
  - "daily briefing"
  - "今日新闻"
  - "compile intelligence report"
  - "aggregate sources for daily update"
version: 1
harness_confidence: 0.9
---

# Daily Intelligence Briefing (fragment)

## 执行逻辑
### When to Use
- User requests a recurring daily/intelligence briefing—news, finance, project status, compliance monitoring—that pulls from multiple structured sources, uses MCP tools + WebFetch, and writes to a predefined Markdown template.
- The domain is parameterizable: you merely need a trigger definition, source config, MCP tool, and output template.

### Step-by-Step
1. **Intent & Domain Detection**
   - Use an intent matcher or keyword matching to recognize a “daily briefing” request.
   - Read any associated trigger file (e.g., `daily-news-trigger.md`) for environment preconditions and source list.

2. **Pre-flight Checks (Environment Injection)**
   - Verify the required MCP tool is connected (e.g., `claude mcp list`).
   - Confirm settings like `skipWebFetchPreflight: true`.
   - Identify known‑unreachable WebFetch sources (e.g., international news sites behind a firewall) and mark them ⚠️.
   - If a critical MCP tool is unavailable, abort and notify the user.

3. **Parallel Data Fetching (Priority Order)**
   - **Primary (low‑noise)**: MCP‑backed API calls (e.g., `get_top_news`, `get_financial_data`).
   - **Secondary**: WebFetch from vetted URLs, each with a tailored extraction prompt.
   - Always fetch in parallel to minimise latency.
   - For every essential data type, designate at least one fallback source.

4. **Real‑time Error Classification & Bypass**
   - **B‑class (server/network)**: certificate errors, ECONNREFUSED. Report immediately, skip the source, and activate fallback.
   - **C‑class (rate limits)**: retry once with a short delay; if still failing, fall back.
   - **Data missing**: attempt cross‑source inference; if impossible, note the gap in the final output.
   - **Never halt** the whole pipeline for a single source failure.

5. **Aggregation, Dedup, and Enrichment**
   - Merge headlines/summaries from all successful sources.
   - Remove duplicates by title/URL.
   - Sort by relevance/impact and tag with predefined categories.
   - Perform impact analysis (market, regulatory, technological) where applicable.

6. **Template Rendering**
   - Read the domain‑specific Markdown template (stored in the user’s vault or project).
   - Populate placeholders (date, summary, news table, key signals) without omitting any `[必填]` sections.
   - Maintain strict adherence to the template’s structure; add no extraneous sections.

7. **Write Output**
   - Save the rendered Markdown to the prescribed path (e.g., `news/YYYY-MM-DD.md`).
   - Update any index or recent‑date reference files.

8. **Communication**
   - After data collection: brief status note (“Data collection done. Writing…”).
   - After file written: confirm path and offer to preview.

### How to Verify
- Output file exists and all `[必填]` sections are present.
- Primary source coverage confirmed; any fallback activation is noted.
- No duplicate entries in tables.
- Date and path are correct.
- Quick grep for residual error placeholders (e.g., `{{unfilled}}`).

## 异常处理
### Edge Cases
- **Source downtime**: use the predefined fallback list; log the outage.
- **Empty source response**: try an alternative endpoint or the next fallback.
- **Domain not covered by existing sources**: pause and ask the user for new source URLs or to adjust the template.
- **Template missing**: abort and request template path.

### Fallback
- Maintain a priority list of 2–3 sources per data type (e.g., policy: `gov.cn` → `people.cn` → `cls.cn`).
- If all fallbacks fail, render the section with “Data unavailable” and a timestamp.
- After completion, append failed sources to a monitoring log for future tuning.
```