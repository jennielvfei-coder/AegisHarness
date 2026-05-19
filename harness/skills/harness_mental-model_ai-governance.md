我来分析 session 上下文中的纠正点，并更新现有技能。

首先进行分类：

**会话分析：**
- 用户在多个 session 中进行了新闻工作流操作
- 关键纠正：助手错误地更新了 `claude for legal-zh` 里的 refiner，用户明确表示"我不需要更新 claude for legal-zh 里面的refiner，更新错了"
- 本质问题：**作用域越界** — 在新闻工作流任务中，助手做出了超出目标范围的文件变更

**分类结果：**
- SKILL_TYPE: `task-workflow` — 这是一个工作流执行纪律的问题
- QUALITY_SCORE: `0.68` — 纠正点明确且可重复验证，但需要与现有技能合并
- ACTION: `merge` — 合并到现有的 `harness_mental-model_news-workflow.md` (v1 → v2)

---

```markdown
---
name: harness_mental-model_news-workflow
description: 新闻工作流执行纪律：作用域锁定、变更前确认、回滚路径预留
tags: [ai-governance, data-compliance, news-workflow]
triggers:
  - 执行日报生成、新闻抓取、Obsidian vault 更新等新闻工作流任务
  - 涉及 Harness/MCP 配置变更时
  - 用户提到"回滚"或纠正助手越界操作时
version: 2
harness_confidence: 0.54
---

# 新闻工作流执行纪律（Scope Discipline）

## 执行逻辑

### When to Use
- 执行新闻工作流任意环节（7源抓取 → 去重 → 8段日报 → Prophet信号 → Obsidian vault）
- 修改 Harness 配置、MCP 设置、或任何 `.local.json` 文件
- 用户要求在"谓词链"中补缺口或输出 Realize 计划时
- 任何时候涉及跨文件/跨系统写入时

### Step-by-Step
1. **作用域锁定 — 先读后写**
   - 执行任何写入前，明确确认目标文件路径
   - 列出即将变更的文件清单，向用户确认（至少在心里自检："这个文件属于当前任务域吗？"）
   - 反例：在新闻工作流任务中，去碰 `claude for legal-zh` 的 refiner 配置

2. **变更前 diff 检查**
   - 如果是编辑已有文件，先用 `cat` 或 `read` 确认当前内容
   - 判断该文件是否属于当前技能的管辖范围（新闻工作流 vs 法律工作流 vs 其他域）

3. **单域单任务原则**
   - 一个任务只操作一个域的文件
   - 如果需要跨域（如新闻 + 法律），必须先显式拆分任务并分别确认

4. **回滚路径预留**
   - 任何批量修改前，记录原始状态或确保 git 可回滚
   - 用户提到"P₂ 单独回滚"意味着每个变更单元应有独立回滚能力

### How to Verify
- 变更后 `git diff --stat` 只显示目标域的文件
- 没有"意外触及"的无关文件出现在变更列表中
- 用户不再需要说"更新错了，我不需要你更新那个"

## 异常处理

### Edge Cases
- **arXiv WebFetch 总是失败**：优先使用 `skipWebFetchPreflight: true` 直接抓取，不要反复尝试默认 WebFetch
- **WebFetch 全部被拦截**：在新闻流程中标记为不可用，后续不再调用，走备用方案（browser-use）
- **Harness Stop Hook 执行时间过长**：检查是否有 MCP 僵尸进程未清理，方案 B（MCP 配置中增加进程管理）是根治手段，方案 A（Stop Hook 清理脚本）是快速见效手段——**两者一起做**

### Fallback
- 如果作用域不确定，先问用户："这个变更涉及 X 和 Y 两个文件，是否都在您的预期范围内？"
- 如果已经越界，立即执行 `git checkout -- <越界文件>` 回滚，并告知用户

## Updated Guidance
**Explicit correction detected** — 用户在 2026-05-19 会话中纠正：助手在新闻工作流任务中错误更新了 `claude for legal-zh` 的 refiner，属于作用域越界。此技能 v2 新增作用域锁定和变更前确认机制，防止跨域污染。同时收录 arXiv WebFetch 失败处理、MCP 僵尸进程治理（A+B 方案并行）、Harness 深度植入等会话中形成的决策。
```

---

**合并说明：**

| 项目 | 内容 |
|------|------|
| 合并目标 | `harness_mental-model_news-workflow.md` v1 → v2 |
| 核心纠正 | **作用域越界**：新闻工作流任务中误触 legal-zh 的 refiner |
| 新增机制 | 作用域锁定、变更前 diff 检查、单域单任务原则、回滚路径预留 |
| 附带收录 | arXiv skipWebFetchPreflight、MCP 僵尸进程 A+B 并行方案 |
| Quality | 0.68（纠正点明确，但"谓词链"和"Realize 计划"的具体结构还需后续验证） |

现有的其他 4 个类似技能（ai-governance v2、data-compliance v2 等）与本技能标签重合但关注点不同，**不合并**，保留各自独立。