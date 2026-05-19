```markdown
---
name: memory-health-check
description: Verify the two-layer memory system (local files + MCP knowledge graph) and report status
tags: [memory, mcp, diagnostics, health-check]
triggers:
  - User asks to see current memory data or whether memory functionality is available
  - System alert about memory inconsistency
version: 1
harness_confidence: 0.9
---

# Memory Health Check

## 执行逻辑
### When to Use
- User explicitly asks "看下现在的记忆数据" / "记忆功能可用吗" or similar queries about memory status.
- You suspect local or graph memory may be stale, missing, or erroneous.
- As a pre‑flight check before relying on memory-stored preferences or workflows.

### Step-by-Step
1. **Inspect local memory files**
   - Read `C:\Users\Chucky\.claude\projects\<project-name>\memory\MEMORY.md` (adjust project root if needed).
   - List all files in that folder with `Get-ChildItem` (Windows) or equivalent, collecting Name, Length, LastWriteTime.
   - Count the number of memory entries in `MEMORY.md` and note the age of each linked file.

2. **Query MCP knowledge graph**
   - If the memory MCP server is connected, run `mcp__memory__search_nodes` with query `"memory"` (or use `mcp__memory__read_graph` if small).
   - Extact total entity count and relationship count (e.g., from the server’s response header or summary).
   - Check for recent Session Summaries (last 7 days) to confirm active recording.

3. **Compile status report**
   - **Local file layer**: number of entries, freshest/oldest file timestamps, any missing index.
   - **Knowledge graph layer**: total entities/relations, whether recent sessions appear, any access errors.
   - Conclude with “Both layers operational” or flag degraded components (e.g., graph unreachable).

4. **Present to user**
   - Use a concise table/chart style (like the original observation) to show both layers side‑by‑side.
   - If the user just asked “memory function available?”, answer with a short “Yes, both layers are working” and only show details if requested.

### How to Verify
- **Local**: Confirm `MEMORY.md` is readable and references real files; file count > 0.
- **MCP**: Confirm `search_nodes` returns non‑zero results; recent session summaries (last 24h) include the current conversation timestamp.
- **Integration**: Both checks complete without tool errors (no `is_error: true`).

## 异常处理
### Edge Cases
- **Memory folder does not exist**: Report “本地记忆目录未找到，可能项目无 memory 目录” and offer to create one.
- **MCP server disconnected**: Fall back to local check only, and inform user “MCP 知识图谱暂不可用，只检查了本地文件记忆”.
- **Large graph (>1000 nodes)**: Do not attempt to read the whole graph at once; rely on `search_nodes` and extract counts from metadata (e.g., `mcp__memory__read_graph` may still provide total count in a header).
- **MEMORY.md empty/malformed**: Treat as “0 entries” and suggest regeneration.

### Fallback
- If either layer is completely unavailable, still report the other layer’s status clearly, and note which part may need repair.
- If the user requests a deeper repair (e.g., rebuild graph, fix file), transition to a separate diagnostic or maintenance skill.
```