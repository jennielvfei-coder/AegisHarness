```markdown
---
name: multi-layer-system-diagnostics
description: Systematically probe all layers of a multi-component system when a user asks "is X working?" or "check my Y data", aggregating results into a transparent, data-backed status report.
tags: [ip, contract-review, privacy, news-workflow, ai-governance, copyright, data-compliance, m-a, employment]
triggers:
  - "看下我现在的记忆数据"
  - "检查下...功能可用吗"
  - "我的...数据还在吗"
  - "系统状态怎么样"
version: 1
harness_confidence: 0.85
---

# Multi-Layer System Diagnostics

## 执行逻辑

### When to Use
When a user asks about the **availability, health, or completeness** of any system that comprises multiple independent layers (e.g., memory: local files + MCP knowledge graph; news: API + Obsidian vault).  
Use when the query pattern matches:
- "看下我现在的[记忆/数据]...可用吗"
- "检查下...功能正常吗"
- "我的[某系统]数据还在吗"
- Any implicit trust-but-verify request for system readiness.

### Step-by-Step
1. **Identify System Layers**  
   Decompose the system into its constituent layers. Example for memory:  
   - Layer 1: Local file store (MEMORY.md, skill files)  
   - Layer 2: External tool/server (MCP memory graph, API endpoints)  
   - Layer 3: Index/registry (obsidian vault links, database indices)

2. **Probe Each Layer Independently**  
   - For each layer, craft the **minimal, non-destructive query** that returns existence + freshness.  
   - Prefer `Read` + `Bash`/`PowerShell` for local file layers; `search_nodes`/`list_entities` for graph layers; simple GET for API layers.  
   - Run **in parallel** where possible to reduce perceived latency.

3. **Collect Raw Signals**  
   From the results, extract three signals per layer:  
   - **Exists**: yes/no list of expected components  
   - **Size/metric**: file count, entity count, record count  
   - **Freshness**: `LastWriteTime`, `updated X days ago`, generation timestamp

4. **Aggregate into Human-Readable Status Report**  
   - Group findings by layer in a lightweight format (bulleted lists, simple tables).  
   - Highlight any anomalies: missing components, stale data, access errors.  
   - Provide a one-line verdict: "All layers operational, N records available."

### How to Verify
- User receives a clear, immediate answer without having to read raw tool outputs.  
- Every layer the assistant knows about is explicitly mentioned (no silent omissions).  
- The summary includes **specific numbers**: file count, entity count, last-modify dates.

## 异常处理

### Edge Cases
- **New/uninitialized system**: If a layer has zero files/entities, report "empty but accessible" rather than "broken."  
- **Partial outage**: If one layer fails (e.g., MCP server returns error), report the failure explicitly and continue reporting other layers — never halt diagnostics on first error.  
- **Stale-but-valid data**: If data is old but system is designed to be static (e.g., reference lists), note age but don't flag as error.

### Fallback
- If a tool required for probing is unavailable (e.g., `mcp__memory__search_nodes` is missing), fall back to reading raw tool-result files or cached exports from previous sessions.  
- If ALL probing fails, return a transparent "Unable to verify — all diagnostic paths blocked" with specific reasons for each blocked path.
```