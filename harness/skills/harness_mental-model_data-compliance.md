```markdown
---
name: news-workflow-webfetch-strategy
description: 新闻工作流中 WebFetch 失败策略：arXiv 优先 skipWebFetchPreflight，全拦截时熔断不重试
tags: [news-workflow, data-compliance, ai-governance]
triggers:
  - arXiv WebFetch 调用失败
  - 新闻源 WebFetch 被全量拦截
  - 讨论新闻聚合 pipeline 的抓取策略
  - 配置 daily-news 工作流
version: 2
harness_confidence: 0.54
---

# 新闻工作流 WebFetch 策略

## 执行逻辑

### When to Use
- 每日新闻工作流执行中，WebFetch 对 arXiv 或其他新闻源返回失败/拦截
- 用户询问是否应继续调用 WebFetch
- 配置或优化新闻源的抓取优先级

### Step-by-Step

1. **arXiv 源：始终使用 skipWebFetchPreflight**
   - arXiv.org 的 API 端点（`export.arxiv.org` / `arxiv.org/abs/*`）对 WebFetch preflight 请求大概率被 CDN/反爬拦截
   - 必须在 `settings.local.json` 中确保 `skipWebFetchPreflight: true`
   - arXiv 抓取走直达路径，不经过 preflight 探测

2. **通用新闻源：实现熔断机制**
   - 若同一新闻源的 WebFetch 连续失败 ≥3 次 → 本轮工作流中标记该源为 `blocked`，不再重试
   - 日志记录：`[WebFetch blocked] source=<name>, reason=<error>, skipped_remaining=true`
   - 后续段（如重点分析、Prophet 信号）依赖该源时，显式标注"数据缺失"

3. **全拦截降级**
   - 若 ≥70% 的新闻源 WebFetch 全部被拦截 → 触发全拦截降级模式
   - 降级策略：
     - 仅使用 World News API 聚合结果（备用方案 A）
     - 已成功抓取的内容正常处理
     - 日报顶部添加元注释：`⚠️ WebFetch 大面积拦截，日报覆盖度受限`
   - 降级模式下不反复重试已失败的源

4. **算力网政策纳入新闻源**
   - 新闻七类源中新增/覆盖"算力网/东数西算"相关政策
   - 关键词：算力网、东数西算、算力调度、数据中心政策、全国一体化算力网络
   - 日报"政策动态"段中为该类预留独立条目

### How to Verify
- arXiv 抓取成功率 > 0（之前每次失败则为修复成功）
- 被拦截源在日志中仅出现一次 `blocked` 标记，无重复重试
- 全拦截降级后日报仍能产出（通过 API 备用方案）

## 异常处理

### Edge Cases
- arXiv API 限流（503）：等待 5s 后重试 1 次，仍失败则标记 blocked
- 部分新闻源间歇性可用：熔断器按"每轮工作流"重置，下次工作流重新尝试
- `skipWebFetchPreflight` 对某些非 arXiv 源可能反而导致失败：仅对已验证的源（arXiv）强制使用

### Fallback
- arXiv 完全不可用时：跳过当日论文解读段，在日报中标注"arXiv 数据不可用"
- World News API 也失败时：使用 browser-use 作为最终备用（JS 渲染）
- 三层降级链：WebFetch (skipPreflight) → World News API → browser-use

## Updated Guidance
**Explicit correction**: 用户指出 arXiv WebFetch 每次都失败（错误方案 3），必须优先使用 `skipWebFetchPreflight: true`。同时要求：若 WebFetch 全部被拦截，新闻流程中后续不再调用，避免无效重试消耗资源。算力网政策需作为独立主题追加入日报。
```