# Skill Analysis

Let me classify this session:

- **SKILL_TYPE**: `task-workflow` — This is a concrete, repeatable diagnostic workflow for checking memory system health across two layers (local files + MCP knowledge graph).
- **QUALITY_SCORE**: `0.75` — The diagnostic procedure is concrete, steps are reproducible, but some variation exists depending on what memory backends are active.
- **ACTION**: `create` — None of the existing skills cover memory system diagnostics. The existing skills (`ai-governance`, `data-compliance`, `news-workflow`, `privacy`) are all mental-model types covering domain knowledge, not system health checks.

---

```markdown
---
name: memory-system-health-check
description: 诊断双层记忆系统（本地MEMORY.md + MCP知识图谱）的可用性和数据完整性
tags: [system-diagnostics, memory, workflow]
triggers:
  - 用户问"记忆功能可用吗"
  - 用户问"检查记忆数据"
  - 用户问"记忆系统状态"
  - Claude 主动建议（会话开始时确认记忆上下文加载正常）
version: 1
harness_confidence: 0.6
---

# 记忆系统健康检查

## 执行逻辑

### When to Use
- 用户直接询问记忆系统状态
- 会话开始需要确认记忆上下文是否完整加载
- 发现记忆读取异常时需要排查
- 定期维护检查（用户未要求但 Claude 发现记忆可能过期时）

### Step-by-Step

#### Phase 1: 本地文件记忆层检查
1. **读取索引文件** — `Read(MEMORY.md)`，确认索引条目数量和最后更新时间
2. **列出所有记忆文件** — `Bash/Get-ChildItem memory/` 获取文件名、大小、最后修改时间
3. **抽查1-2个记忆文件** — 验证文件内容完整、frontmatter 齐全、无损坏

#### Phase 2: MCP 知识图谱层检查
4. **搜索关键词探测** — `mcp__memory__search_nodes(query="memory")` 或类似查询，获取实体/关系基数
5. **如有必要，读取最近图谱快照** — 确认会话摘要的覆盖时间范围
6. **交叉验证** — 对比本地文件和知识图谱中是否有互相引用的内容

#### Phase 3: 结论输出
7. **生成两层状态摘要表**：
   - Layer 1 本地文件：文件数、最后更新时间、数据新鲜度评估
   - Layer 2 MCP图谱：实体数、关系数、覆盖时间范围
8. **标注风险**：数据过期（>7天未更新）、层间不一致、文件损坏
9. **给出可用性结论**：✅ 正常 / ⚠️ 部分异常 / ❌ 不可用

### How to Verify
- 本地文件数量和索引条目数一致
- 记忆文件最后修改时间不超过14天（否则提示"可能过时"）
- MCP 知识图谱返回正常响应（无超时/空结果异常）
- 两层之间存在交叉引用的内容（证明同步正常）

## 异常处理

### Edge Cases
- **索引与文件不匹配**：MEMORY.md 列出3条但文件只有2个 → 报告缺失文件，建议清理索引
- **MCP 服务不可用**：若 `search_nodes` 超时或报错，明确告知"知识图谱层不可用，本地文件层正常"
- **全部为空**：两层均无数据 → 告知用户"记忆系统尚未建立，是否为首次使用？"
- **数据量过大**：MCP 返回上千实体时，只读取摘要而不全量展开，用搜索关键词探测内容结构

### Fallback
- 若 MCP 不可用，仅基于本地文件给出部分诊断结论，明确标注"仅检查了本地层"
- 若本地文件目录不存在，引导用户确认 `memory/` 路径是否正确配置

## Evolution Log
- 2026-05-19 v1: 从会话 session_20260519152047 提取，覆盖双层记忆系统（本地文件 + MCP知识图谱）的完整诊断流程
```