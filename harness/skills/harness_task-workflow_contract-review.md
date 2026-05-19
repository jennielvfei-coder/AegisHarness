```
```markdown
---
name: sdp-pending-items
description: 查询并汇总系统待办事项（harness审查队列、git变更、任务列表）
tags: [contract-review, ai-governance, privacy]
triggers:
  - 未完成清单
  - 待办事项
  - 还有什么没做的
  - sdp查一下未完成
version: 1
harness_confidence: 0.8
---

# 未完成清单汇总

## 执行逻辑
### When to Use
- 用户询问“未完成清单”、“待办事项”、“还有哪些事没做完”时触发。

### Step-by-Step
1. 查询 harness daemon 的待审查技能队列（执行 `python path/to/harness_daemon.py review`），获得待审批技能列表及质量分数。
2. 检查当前会话任务列表（来自记忆/上下文），筛选未完成项。
3. 运行 `git status` 识别未提交的修改（M）和未跟踪文件（??）。
4. 将三类结果合并，按优先级（如审查队列质量分数高低、任务紧急度、未提交变更重要性）排序，生成结构化汇总报告。

### How to Verify
- 每一步查询均成功返回无报错。
- 汇总报告明确涵盖所有三个来源，条目完整无遗漏。

## 异常处理
### Edge Cases
- **Harness daemon 不可用**：跳过该部分，报告中注明“Harness 审查队列暂不可用”。
- **Git 仓库不存在或未初始化**：跳过 git 状态部分，报告中注明“当前目录非 git 仓库”。
- **任务列表为空**：报告中注明“当前无待办任务”。

### Fallback
- 如果所有数据源均不可用，返回提示：“目前无法自动收集未完成项，请手动检查 Harness、任务列表和 git 状态。”
```