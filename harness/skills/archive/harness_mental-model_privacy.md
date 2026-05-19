```markdown
---
name: memory-health-check
description: Diagnose and verify the availability and integrity of the memory system, including local file memory and MCP knowledge graph.
tags: [memory, diagnostics, system-check, cli]
triggers:
  - 检查记忆数据
  - 记忆功能可用吗
  - memory check
  - memory health
version: 1
harness_confidence: 0.6
---

# Memory Health Check

## 执行逻辑

### When to Use
当用户询问记忆系统状态、可用性、是否正常工作，或需要查看现有记忆数据概览时触发。

### Step-by-Step
1. **定位本地记忆目录**  
   路径：`C:\Users\Chucky\.claude\projects\D--Claude\memory\`  
   读取根目录下的索引文件 `MEMORY.md`，获取所有已注册记忆模块的列表。

2. **扫描本地记忆文件**  
   使用 PowerShell 命令列出所有文件（`Get-ChildItem <dir> -File | Select-Object Name, Length, LastWriteTime`），记录每个记忆文件的大小和最后修改时间。

3. **检查 MCP 知识图谱**
   - 尝试调用 `mcp__memory__search_nodes` 工具，使用通用查询词（如 `"memory"`），确认 MCP 服务在线且可返回结果。
   - 若存在最近的图谱导出文件（如 `mcp-memory-read_graph-*.txt`），读取前若干行，统计实体数和关系数。
   - 若 MCP 不可用或返回空，标记为不可用状态。

4. **汇总与报告**
   构建如下表格或结构化输出：
   - 本地文件记忆：文件数量、每个文件名、大小、最后更新日期、是否存在过期警告（>3天）。
   - MCP 知识图谱：实体数、关系数、最近会话记录、是否可达。
   - 整体结论：记忆功能是否完整可用。

### How to Verify
- 确认本地 `MEMORY.md` 中列出的 `.md` 文件实际存在且可读。
- MCP 调用返回 `entities` / `relations` 或会话摘要数据，且无错误。
- 报告中无严重缺失（如目录不存在、MEMORY.md 为空但预期有记忆）。

## 异常处理

### Edge Cases
- **记忆目录不存在** → 报告路径缺失，提醒可能需要初始化。
- **`MEMORY.md` 为空或格式异常** → 视为无本地记忆，继续检查 MCP。
- **MCP 服务器不可用** → 仅报告本地部分，提示可尝试 `mcp-server-memory` 重启。
- **图谱导出文件过期或不存在** → 仅依赖 `search_nodes` 结果，不强制要求文件。

### Fallback
- MCP 不可达时：只输出本地记忆状态，并提醒用户“MCP 知识图谱当前不可用，建议检查 MCP Server 运行状态”。
- 本地记忆目录缺失时：输出警告，建议运行记忆初始化命令。

## Evolution Log
- 2026-05-20 v1: Auto-created from session about memory diagnostic and availability check.
```