```markdown
---
name: memory-system-status-check
description: 检查记忆系统（本地文件 + MCP 知识图谱）是否可用，并输出结构化概览
tags: [memory, system-check, claude, mcp]
triggers:
  - 用户询问记忆状态 / 可用性
  - "记忆功能可用吗"
  - "看一下记忆数据"
version: 1
harness_confidence: 0.9
---

# 记忆系统状态检查

## 执行逻辑
### When to Use
- 用户主动请求检查记忆系统是否正常、数据是否完整
- 怀疑记忆丢失或未保存时进行诊断
- 首次了解记忆存储布局

### Step-by-Step
1. **本地文件索引读取**  
   - 读取 `C:\Users\Chucky\.claude\projects\D--Claude\memory\MEMORY.md`（若存在）  
   - 解析其中列出的记忆条目（标题、链接）

2. **本地文件列表**  
   - 使用 `Bash` 或 `PowerShell` 列出 `memory/` 目录下所有文件（名称、大小、修改时间）  
   - 排除隐藏文件，确保覆盖所有 `.md` 记忆文件

3. **关键记忆内容抽查（可选）**  
   - 若 MEMORY.md 中有引用，可快速读取一两个文件的前言（front matter）确认格式完整  
   - 不在此步骤中深度审查内容，仅在概览时提及

4. **MCP 知识图谱检查**  
   - 调用 `mcp__memory__search_nodes`，查询关键词 `"memory"` 获取实体数量与关联关系概述  
   - 若返回过大，可先尝试 `mcp__memory__read_graph` 分页读取基本统计（实体数、关系数）  
   - 记录图数据库状态（连接正常/异常，数据规模）

5. **生成状态概览**  
   - 汇总两层数据：本地文件数量、最近更新时间、MCP 中实体/关系总量  
   - 指出任何异常（例如 MEMORY.md 缺失、MCP 无响应）  
   - 以清晰表格或列表呈现，给出“可用/不可用”判断

### How to Verify
- 回复中应包含：
  - 本地记忆文件名 + 更新时间
  - MCP 实体数/关系数（若可用）
  - 整体结论（如“两层记忆均可用，数据完整”）
- 若 MCP 不可用，明确说明“本地记忆正常，知识图谱未连接”

## 异常处理
### Edge Cases
- **MEMORY.md 不存在**：仅通过文件列表生成概览，并提醒索引文件缺失  
- **memory 目录为空**：报告无本地记忆  
- **MCP 服务未连接**：跳过知识图谱部分，仅展示本地状态  
- **MCP search_nodes 返回超长结果**：截断并说明“数据量大，仅展示概要”

### Fallback
- 若所有检查手段都失败，返回“无法读取记忆系统状态，请检查文件权限或 MCP 配置”
- 任何时候至少返回文件列表结果，保证部分信息可获取
```