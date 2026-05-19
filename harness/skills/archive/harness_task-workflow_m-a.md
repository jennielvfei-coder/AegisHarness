```markdown
---
name: memory-health-check
description: 诊断双层记忆系统的健康状况，检查本地文件记忆和MCP知识图谱记忆是否正常运作
tags: [m-a, memory, diagnostics, system-health]
triggers:
  - 用户询问记忆/记忆系统/MEMORY 是否可用或状态如何
  - 用户要求检查记忆数据
version: 1
harness_confidence: 0.85
---

# 记忆系统健康检查

## 执行逻辑
### When to Use
用户想了解记忆系统当前状态时触发——包括本地文件记忆和MCP知识图谱记忆两层。关键词："记忆数据"、"记忆功能"、"记忆系统"、"memory"。

### Step-by-Step
1. **并行检查本地层**：读取 `C:\Users\Chucky\.claude\projects\D--Claude\memory\MEMORY.md` 索引文件 + 用 PowerShell 列出该目录下所有文件（含大小和更新时间）
2. **并行检查MCP层**：调用 `mcp__memory__search_nodes`（query="memory"）获取知识图谱概览；若图过大则从返回的实体/关系数量推断规模
3. **合并报告**：以两层结构输出——
   - **本地文件记忆**：表格列出文件、内容摘要、更新时间
   - **MCP知识图谱记忆**：实体数 + 关系数 + 主要内容类型（如"会话摘要"）和覆盖时间范围
   - 每层标注状态（✅可用 / ⚠️部分可用 / ❌不可用）
4. **预警提示**：若MEMORY.md超过7天未更新，标注"记忆可能过时"；若MCP实体数异常低（<50），标注可能原因

### How to Verify
- 本地层：MEMORY.md可读且索引条目数 ≥ 0
- MCP层：`search_nodes` 返回实体数 ≥ 10
- 报告末尾附上"两层记忆系统均可用"或指出具体问题

## 异常处理
### Edge Cases
- **MCP图过大**：`read_graph` 超时或返回大量数据时，回退到 `search_nodes` 获取统计数据
- **MEMORY.md 不存在**：直接报告"本地记忆文件缺失"，仅展示MCP层状态
- **PowerShell 不可用**：回退到 `Bash` 或仅读取 MEMORY.md

### Fallback
若MCP层完全不可用（连接失败），仅报告本地层状态并注明"MCP知识图谱当前不可达，建议检查 MCP memory server 状态"
```