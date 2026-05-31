```markdown
---
name: proactive-config-audit
description: 系统化审计项目配置健康状态的四阶段检查模式：记忆→代码变更→配置文件→运行时状态，在问题触发之前发现缺口
tags: [data-compliance, ai-governance, news-workflow, prethink:exploration]
triggers:
  - session 启动时的 harness 健康检查
  - 配置变更后的完整性验证
  - 排查 "为什么某功能不工作" 时
  - 用户要求 "检查项目状态" 或类似模糊指令
version: 1
harness_confidence: 0.85
---

# 主动配置审计模式 (Proactive Configuration Audit)

## 执行逻辑

### When to Use
- Session 开始，尚未明确任务时，自动触发健康检查
- 刚完成一组修改，需要验证副作用
- 遇到模糊的 "不工作" 信号，需要系统化定位缺口
- 项目处于快速迭代期，配置漂移风险高

### Step-by-Step（四阶段递进）

**Phase 1: 读取记忆中的已知问题**
- 读取 MEMORY.md 或等效知识库文件
- 列出所有已记录的失败模式、配置缺陷、架构矛盾
- 目标：避免重复踩坑，带着 "已知坏味道清单" 进入后续阶段

**Phase 2: 审计代码变更面**
- `git diff --stat` 获取变更文件概览
- `git diff` 查看具体改动内容
- 判断：这些改动是否会引入新的配置依赖？是否可能破坏现有的 hooks/settings？

**Phase 3: 配置文件交叉验证**
- 读取 `settings.local.json`（或等效本地配置）
- 读取 `settings.json`（或等效默认配置模板）
- 检查关键字段是否存在：hooks、skipWebFetchPreflight、MCP 配置等
- 核心判断：**"应该有但实际没有的配置项"**——这是最高优先级的缺口
- 如果默认配置模板本身不存在，这也是一个需要报告的缺口

**Phase 4: 运行时包/依赖验证**
- 检查关键包的目录结构是否完整（如 `duonews/**/*.py`）
- 验证包是否可导入、是否有语法错误
- 检查测试是否通过（如果有测试套件）

### How to Verify
- Phase 1 输出：已知问题的 checklist（逐项确认是否已修复/仍存在）
- Phase 3 关键信号：某个被文档/代码引用但配置文件中不存在的字段 → 立即报告
- Phase 4 关键信号：包存在但无法导入，或目录为空 → 立即报告
- 所有阶段完成且无新缺口发现 → 审计通过

## 异常处理

### Edge Cases
- **settings.json 缺失但 settings.local.json 存在**：这是常见模式，但如果 hooks/MCP 配置只在 settings.json 中定义，则本地覆盖文件会缺少这些字段。需检查两文件之间的字段覆盖率
- **git 仓库不是干净的**：Phase 2 的 diff 如果有未提交修改，需区分是本次 session 的修改还是遗留的脏状态
- **大项目配置分散**：配置可能在 `.claude/`、`pyproject.toml`、环境变量等多处，需根据项目约定扩展 Phase 3 的文件列表

### Fallback
- 如果 MEMORY.md 不存在：跳过 Phase 1，从 Phase 2 开始
- 如果 git 不可用：跳过 Phase 2，直接进入 Phase 3
- 如果 settings.local.json 不存在：报告为高优先级缺口，因为这是大多数 harness 功能的基础依赖
- 四阶段全部无法执行 → 降级为"该项目无已知配置模式，需手动探索"
```