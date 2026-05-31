```markdown
---
name: harness-health-check
description: 诊断AI治理harness运行状态并统计累积错误的流程
tags: [news-workflow, ai-governance, data-compliance, prethink:exploration]
triggers:
  - 用户询问“我的harness现在运行如何了”或“积累了多少错误”
version: 1
harness_confidence: 0.85
---

# Harness 状态诊断与错误审计

## 执行逻辑
### When to Use
- 需要快速了解 harness 整体健康状况、活跃错误数量和趋势。
- 自动分析命令不可用或信息不完整时，需要手动探查底层存储。

### Step-by-Step
1. **尝试使用内置分析命令**  
   - 执行 `python harness/harness_daemon.py analyze`（注意路径用 `/` 或平台适配分隔符）。  
   - 若因路径错误失败，修正后重试；若命令根本不存在，继续下一步。
2. **直接查询状态数据库**（`harness/state.db`）  
   - 获取 **判断图健康**：`judgment_entries` 总数与平均置信度。  
   - 统计 **错误/失败相关特征激活**：从 `feature_activations` 中筛选包含 `error` 或 `fail` 的条目。  
   - 列出 **所有假设** 及其状态与描述（`hypotheses`）。  
   - 统计 **错误信号** 数量：`signal_buffer` 中 `signal_type` 含 `error`/`fail` 的记录。  
   - 获取 **最近判断条目**（最近 10 条）看趋势。  
   - 获取 **关键表结构**（`PRAGMA table_info`）以确认当前数据模型（针对 `judgment_entries`, `belief_traces`, `false_belief_log`, `observations`, `fusion_sessions`, `dcl_judgments`, `hypotheses`, `signal_buffer`, `feature_activations` 等）。
3. **汇总结果**  
   - 报告错误总数：将特征激活、信号、假设中涉及错误的条目合并去重计数。  
   - 补充展示平均置信度、活跃假设等指标，给出整体健康摘要。

### How to Verify
- 所有 SQL 查询应返回有效数值或结果集，无报错。  
- 人工复核一两个错误条目，确认内容合理（如错误特征激活描述确实为故障）。  
- 若可能，与 harness 自带的仪表板或日志交叉比对。

## 异常处理
### Edge Cases
- **数据库不存在或损坏**：回退到检查日志文件（`harness/logs/`），查找错误堆栈。  
- **路径分隔符问题**（Windows/Linux）：使用 `os.path.join` 或统一使用 `/` 适配。  
- **查询超时**：限制 `LIMIT` 或使用索引字段，必要时只统计计数而不提取详情。  
- **无错误记录**：报告“零错误”，但需确认是因为系统健康还是数据未采集（检查数据库是否为空）。

### Fallback
- 若所有自动化手段失效，建议用户手动检查 `harness` 目录下的最近日志、守护进程输出，或重启 harness 后即刻观察状态。
```