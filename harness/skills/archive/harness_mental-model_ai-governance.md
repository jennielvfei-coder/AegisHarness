```markdown
---
name: daily-news-workflow
description: 每日新闻完整工作流（7类源→去重→8段日报→Prophet信号→Obsidian vault），含arXiv抓取修正与文件隔离规则
tags: [ai-governance, data-compliance, news-workflow]
triggers:
  - "今日新闻"
  - "生成日报"
  - "新闻工作流"
  - "日报"
version: 1.0
harness_confidence: 0.54
---

# 每日新闻工作流

## 执行逻辑

### When to Use
- 用户要求生成当日或补生成指定日期的新闻日报
- 需要输出 `YYYY-MM-DD.md` 完整日报，含8个板块、重点分析、arXiv论文、Prophet信号
- 工作流需要更新 `index.json` 和 `daily-log`

### Step-by-Step
1. **7类信源抓取**
   - World News API（全球聚合）
   - WebFetch（启用 `skipWebFetchPreflight: true` 的设置）
   - browser-use（JS渲染备用）
   - 其他专用信源按既有配置
2. **去重与清洗**：按标题相似度去重，剔除过时内容
3. **生成8段日报**：总览、重点分析×4、arXiv解读、Prophet信号
4. **写入文件**
   - 日报文件：`YYYY-MM-DD.md` 到指定目录
   - 更新 `index.json`（添加当天条目）
   - 更新 `daily-log`（记录生成时间、信源状态）
5. **Prophet信号计算**：基于聚合内容生成趋势评分与信号
6. **同步至Obsidian vault**：确保文件落地到vault路径

### How to Verify
- 日报文件存在且包含8个标准板块
- `index.json` 中新增当天记录
- arXiv板块非空且包含至少一篇有效摘要
- 未修改任何 `legal-zh` 项目下的文件（见异常处理）

## 异常处理

### Edge Cases
1. **arXiv WebFetch 全部失败**
   - **修正**：`arXiv` 抓取必须在 `settings.local.json` 中强制 `skipWebFetchPreflight: true`，因为常规 WebFetch 总是失败。
   - 若仍未取得数据，使用 arXiv API（如 `http://export.arxiv.org/api/query`）作为替代；若仍失败，arXiv板块留空并记录警告。
2. **其他源 WebFetch 被拦截**
   - 如果连续 `N` 次（建议 N=3）WebFetch 均返回拦截/403，后续该次工作流中**不再调用** WebFetch，仅使用 API 类信源，并在日志注明。
3. **文件越界修改**
   - **严格禁止**：工作流运行过程中，**绝不**修改、覆盖或更新 `claude for legal-zh` 项目的任何文件（如 `refiner` 配置或脚本）。两个项目完全隔离。
   - 若需要将日报结果导入法律项目，必须通过用户明确授权的导出/复制流程，而非直接编辑。

### Fallback
- 所有 HTTP 请求失败时，尝试本地缓存（若存在）生成最小可用日报
- Prophet信号缺失时标注“不可用”，不阻塞日报生成

## Updated Guidance
**用户明确纠正后的关键规则**：
- 每日新闻工作流与 `legal-zh` 的 `refiner` 是独立系统；助手不得基于惯性自动“更新” legal-zh 内的配置。任何跨项目操作必须用户单独提出。
- arXiv 抓取已确认常规 `WebFetch` 不可用，强制采用 `skipWebFetchPreflight: true`；该配置应视为工作流预设而非可选项。
```