```markdown
---
name: known-failure-injection
description: 在复杂多源工作流执行前注入预记录的已知失败模式，避免无效重试并实现优雅降级
tags: [data-compliance, ai-governance, news-workflow, privacy, data-quality-failure]
triggers:
  - 任何依赖多外部数据源且有已知不可靠端点的自动化工作流
version: 1
harness_confidence: 0.82
---

# 已知失败模式注入 — 弹性工作流前置条件

## 执行逻辑

### When to Use
当工作流满足以下**全部**条件时启用：
- 涉及 ≥2 个外部数据源（API、WebFetch、WebSearch）
- 历史上至少一个数据源出现过可复现故障
- 故障模式稳定（如某域名永久不可达、某 API 模型返回 400）

### Step-by-Step

1. **维护已知失败清单** — 在工作流技能文件的 `## 环境前置条件` 区块中，以结构化格式记录：
   ```
   | 数据源 | 故障类型 | 状态 | 替代方案 |
   ```
   - 示例：`WebSearch (deepseek) → 400 错误 → 永久不可用 → 改用 MCP 工具`

2. **SessionStart 自动注入** — harness injector 在会话启动时执行：
   - 读取技能文件中的 `环境前置条件` 区块
   - 将其注入为 Claude 的“已知上下文”（不作为运行时检查，而是作为预置知识）
   - 写入条件：`skipWebFetchPreflight: true` 等同级配置同步写入 `settings.local.json`

3. **工作流执行时静默跳过** — Claude 在任务执行中遇到已知故障源时：
   - 不发起调用（不浪费 token 和延迟）
   - 不输出重试逻辑
   - 在输出中标记 `⚠️` 说明跳过原因
   - 自动切换到记录的替代方案

### How to Verify
- 工作流日志中**不出现**对已知故障源的调用尝试
- 输出中包含 `⚠️` 标记但总数不超过已知故障源数量
- 最终结果完整性 ≥ 设计预期的降级水平

## 异常处理

### Edge Cases
- **故障源意外恢复**：在 `环境前置条件` 中标注 `last_checked: date`，harness injector 定期重新探测（建议每周一次），恢复后自动移除
- **新故障出现**：本次会话中首次遇到的新故障 → 任务完成后以 `env-fix` 技能形式追加到已知清单，同时更新当前技能文件
- **所有数据源均失效**：触发完整降级 → 告知用户“当前所有数据源不可用”，建议稍后重试，不输出空结果

### Fallback
当已知失败清单本身不可读或损坏时：
- 回退到每条数据源独立尝试 + 单次失败即跳过的保守策略
- 会话结束后标记技能文件需修复
```