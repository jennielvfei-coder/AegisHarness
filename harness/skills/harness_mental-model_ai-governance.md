```markdown
---
name: task-context-isolation-before-mutation
description: 修改文件前必须校验当前工作上下文与目标项目一致，禁止跨项目污染（如将 news-workflow 的更新误写入 legal-zh）
tags: [ai-governance, data-compliance, news-workflow]
triggers:
  - 涉及多项目/多 repo 交叉操作时的任何文件写入、重构或配置更新
  - 用户要求"回滚"、"撤销"某次错误写入时
version: 2
harness_confidence: 0.54
---

# 任务上下文隔离 — 写入前校验

## 执行逻辑

### When to Use
用户会话横跨多个项目/子模块（如 news-workflow、legal-zh、Harness 本体）时，**任何写操作前**强制执行本校验。尤其当助手从上下文缓存中提取路径或推断目标时，极易将 A 项目的修改误写入 B 项目的同名文件（如 `refiner` 配置）。

### Step-by-Step
1. **写入前显式锁定目标**：在 `Write` / `Edit` 工具调用前，先在思考链路中输出 3 要素：
   - 当前会话主项目（从 session tags / 用户首条指令推断）
   - 目标文件完整路径
   - 目标文件所属项目
2. **交叉比对**：若「目标文件所属项目」≠「当前会话主项目」，**必须向用户确认**再写入。不得静默执行。
3. **回滚锚点**：每次写入前记录操作摘要（文件路径 + 版本/哈希 + 时间戳），供用户要求"P₂ 单独回滚"时快速定位。
4. **项目边界清单**（从已知会话提取）：
   - `news-workflow` → 日报、Prophet 信号、Obsidian vault、arXiv 抓取
   - `legal-zh` → 法律中文 refiner、合规分析
   - `harness` → Stop Hook、MCP 进程管理、会话生命周期

### How to Verify
- 用户说"回滚 P₂"时，能精确定位到**单次写入**而非整个会话的所有修改。
- 连续 3 次会话未出现"写错项目"的纠正。

## 异常处理

### Edge Cases
- 用户**明确要求**跨项目同步（如"把 news-workflow 的这个配置也复制到 legal-zh"）→ 不拦截，但在响应中标注「跨项目写入」标记。
- 同一会话中有多个活跃项目 → 每次切换项目时重新锁定上下文。

### Fallback
- 若已发生错误写入（如本会话：news-workflow 的 refiner 更新被误写入 legal-zh），执行**最小回滚**：
  1. 定位该次写入的单一文件/变更集
  2. Git 级回滚该文件到上一版本
  3. 不触及同日其他正确写入
  4. 重新在当前正确项目下执行原操作

## Updated Guidance
用户纠正：助手上次将 P₂（refiner 更新）写入了 `claude for legal-zh`，而非当前的 news-workflow 项目。根本原因是上下文缓存中残留了 legal-zh 的路径引用，助手未经项目归属校验即执行写入。自本版本起，所有写入操作强制执行 Step 1-2 的项目锁定与交叉比对。
```