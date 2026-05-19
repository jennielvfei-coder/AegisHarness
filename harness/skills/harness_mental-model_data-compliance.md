Looking at the session context, I identify the **core correction**: arXiv WebFetch always fails, and the user explicitly corrected the workflow to prioritize `skipWebFetchPreflight`. Additionally, a decision gate for total WebFetch failure is needed. This merges into the existing `harness_mental-model_news-workflow.md` (v2).

**Classification**: `task-workflow` | **Quality**: 0.82 (concrete, repeatable, verification steps present) | **Action**: `merge` → v3

---

```markdown
---
name: daily-news-workflow
description: 每日新闻完整工作流（7类源→去重→8段日报→Prophet→Obsidian），含WebFetch/arXiv容灾策略
tags: [news-workflow, data-compliance, ai-governance]
triggers:
  - 用户要求执行每日新闻流程
  - 用户提及日报生成、新闻聚合或Prophet信号
  - WebFetch在新闻上下文中反复失败时
version: 3
harness_confidence: 0.54
---

# 每日新闻完整工作流

## 执行逻辑

### When to Use
- 用户触发 `/news` 或等效指令
- 每日定时新闻采集（7类新闻源）
- 新闻工作流中 WebFetch 出现批量失败需降级决策时

### 新闻源矩阵（3层互补方案）
| 层级 | 方案 | 适用场景 |
|------|------|----------|
| L1 | World News API | 全球聚合，首选 |
| L2 | WebFetch + `skipWebFetchPreflight: true` | 直接抓取，**arXiv等学术源强制启用** |
| L3 | browser-use | JS渲染备用，前两层全部不可用时触发 |

### Step-by-Step（修正后流程）

**Phase 1：采集**
1. **World News API** 拉取全球新闻聚合
2. **arXiv 采集** → **强制使用 `skipWebFetchPreflight: true`**
   - ⚠️ **用户修正**：arXiv WebFetch 每次都失败，不得使用默认 WebFetch
   - `settings.local.json` 需预先配置 `"skipWebFetchPreflight": true`
3. 其余5类源依次采集，优先 WebFetch with preflight skip
4. **拦截检测**：任一类源连续3次返回空/403/超时 → 标记该类源为 `BLOCKED`

**Phase 2：去重**
5. 跨源去重（标题相似度 ≥ 0.85 视为重复）
6. 按时间衰减排序（越近权重越高）

**Phase 3：生成**
7. 8段日报生成：
   - 总览（27条新闻摘要）
   - 4篇重点分析
   - 10篇arXiv论文解读（仅当arXiv源未BLOCKED）
   - Prophet信号
8. **BLOCKED源处理**：如果某类源已标记 BLOCKED，日报中标注 `[源不可用]`，不重试

**Phase 4：向量数据库升级路径（设计预留）**
- 当前不实施向量数据库
- 日报以 Markdown 存入 Obsidian vault，`index.json` 维护索引
- **升级路径**：`index.json` 结构设计兼容未来 `→ embeddings → vector DB` 迁移
  - 每条记录保留 `id`, `title`, `summary`, `source_url`, `timestamp`, `embedding_slot: null`
  - `embedding_slot` 字段预留给后续 `text-embedding-3-small` 填充

**Phase 5：输出**
9. 写入 `{date}.md` 到 Obsidian vault
10. 更新 `index.json` 和 `daily-log`

### WebFetch 拦截决策门（用户修正）
```
if arXiv → 强制 skipWebFetchPreflight: true
if 非arXiv源 AND 连续失败3次 → BLOCKED，当日不再重试
if 所有WebFetch源均BLOCKED → 降级为仅World News API + browser-use备用
if browser-use也不可用 → 日报标注[全源降级]，仅出已有数据
```

### How to Verify
- [ ] `settings.local.json` 中存在 `"skipWebFetchPreflight": true`
- [ ] arXiv 采集使用了 preflight skip（日志中可见）
- [ ] BLOCKED 检测生效（连续失败3次后停止重试）
- [ ] 日报文件含正确的 `[源不可用]` 或 `[全源降级]` 标注
- [ ] `index.json` 记录含 `embedding_slot: null` 预留字段

## 异常处理

### Edge Cases
- **arXiv 409/超时**：已通过 `skipWebFetchPreflight` 修正，如仍失败 → 标记 BLOCKED，日报省略arXiv段
- **全部7类源不可用**：触发 `[全源降级]`，输出空日报模板，通知用户
- **部分源返回但内容为空**：保留条目，摘要标注 `[内容抓取失败]`

### Fallback
1. WebFetch 单源失败 → 切换 L3 browser-use
2. 全源 WebFetch 失败 → 仅 World News API
3. API + WebFetch 全失效 → 空日报 + 告警
4. 禁止在已标记 BLOCKED 的源上循环重试（用户修正：浪费时间且无意义）

## Updated Guidance
**Explicit correction detected — user corrected assistant output:**

1. **arXiv WebFetch 修正**：arXiv 每次 WebFetch 都失败，必须无条件使用 `skipWebFetchPreflight: true`。此修正已纳入 Phase 1 步骤2。
2. **WebFetch 拦截决策**：用户质疑"如果 WebFetch 全部被拦截，是否后续都不要调用了"→ 确认：一旦标记 BLOCKED，当日不再重试。设计决策门逻辑（见上文）。
3. **向量数据库升级路径**：用户明确"不要求 Phase 4 现在就做向量数据库，但应预留升级路径"→ `index.json` 中预留 `embedding_slot: null` 字段，兼容未来迁移。
```

---

**变更摘要**：
- **合并目标**：`harness_mental-model_news-workflow.md` v2 → v3
- **核心修正**：arXiv 源强制 `skipWebFetchPreflight` + WebFetch 全拦截决策门 + 向量DB升级路径预留
- **质量提升**：qs 0.54 → 0.82（从模糊心智模型变为可执行任务工作流，含具体配置、步骤编号和验证清单）