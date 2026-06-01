```
```markdown
---
name: multi-tool-parallel-verification
description: 同时使用Read、Grep、Bash、MCP等内置工具执行并行验证，快速检查环境与依赖。
tags: [data-compliance, news-workflow, ai-governance, prethink:exploration]
triggers:
  - "调试一下"
  - "验证环境"
  - "检查依赖"
  - "并行工具"
version: 1
harness_confidence: 0.9
---

# 并行工具验证

## 执行逻辑
### When to Use
- 当用户要求快速检查代码库、文件和外部资源时。
- 当需要并行执行多个独立查询以提高效率时。
### Step-by-Step
1. 识别要验证的目标：可能包括特定文件内容（Read）、代码模式（Grep）、命令执行结果（Bash）和外部搜索（MCP工具如Tavily）。
2. 同时发起多个工具调用，不等待单个结果。
3. 汇总所有结果，列出成功/失败状态。
4. 如果有失败，报告失败原因并提供修复建议。
### How to Verify
- 每个工具调用均返回预期结果或明确的“无结果”信息，无报错。

## 异常处理
### Edge Cases
- 某个MCP工具不可用：回退到Bash或仅报告该部分无法验证。
- Grep无匹配：返回“未找到”，不算失败。
### Fallback
- 若并行调用因速率限制失败，改为顺序执行关键验证。
```