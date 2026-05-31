```markdown
---
name: summarize-outstanding-tasks
description: Use SDP framework to check and summarize all outstanding tasks (harness reviews, session tasks, git workspace).
tags: [sdp, productivity, task-management]
triggers:
  - "sdp查一下目前未完成清单"
  - "列出未完成任务"
  - "show outstanding tasks"
  - "what's pending"
version: 1
harness_confidence: 0.95
---

# 汇总未完成任务

## 执行逻辑
### When to Use
当用户要求查看所有待处理项时，使用此技能通过**谓词链（Propose‑Validate）** 系统性地收集并报告未完成任务。

### Step-by-Step
1. **提出谓词链（Propose）**  
   列出需要收集信息的领域，例如：
   - P₁: Harness 待审查技能队列已读取，输出已知
   - P₂: 当前会话任务列表已检查
   - P₃: Git 工作区未提交/未跟踪变更已识别
   - P₄: 用户收到完整的汇总报告

2. **验证与执行（Validate & Execute）**  
   对每条谓词逐一验证，直到所有前提满足：
   - **Harness 审查队列**：运行  
     `python D:/Claude/harness/harness_daemon.py review`  
     *注意：请使用正斜杠以避免 Shell 转义问题。*
   - **当前会话任务**：检查当前会话的任务记录或待办列表（如 CLAUDE.md、localStorage 等）。
   - **Git 工作区**：执行 `git status` 获取已修改（M）和未跟踪（??）文件。

3. **汇总报告**  
   按照来源分类呈现结果，包含条目名称、相关评分（若有）、时间戳，并明确哪些区域为空。

### How to Verify
- 所有命令成功执行并返回有效输出。
- 最终汇总正确覆盖了所有可用的待完成来源。
- 用户反馈清单完整，无遗漏。

## 异常处理
### Edge Cases
- **路径转义错误**：若 `\\` 被 Shell 吞掉，自动改用正斜杠重试。
- **空结果**：若任何检查返回无待处理项，明确报告“无未完成任务”（而不是省略该项）。
- **命令失败**：捕获错误并给出明确提示，必要时提供手动检查指令（如 `ls` 文件列表）。

### Fallback
- 若 `harness_daemon.py` 未找到，提示用户确认路径或检查安装。
- 若 Git 不可用，仅基于文件系统快照报告未跟踪文件（`??`）。
```