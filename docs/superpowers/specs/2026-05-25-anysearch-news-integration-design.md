# anysearch 新闻管线集成设计

日期：2026-05-25
状态：已确认

## 概述

将 anysearch 搜索技能集成到现有日报工作流中，替代高风险 WebFetch 源，新增学术深度和民生宽度。

## 架构

```
采集层（4路并行，anysearch 三槽合并为一次 batch_search）:

A: World News API MCP (不变)
B: arXiv WebFetch (不变)
C: anysearch batch_search (新增)
   ├─ slot1: academic.search 高因子论文
   ├─ slot2: 国内政策/行业 news
   └─ slot3: 人文社科 news
D: 财联社 WebFetch (不变)

→ 归一化(anysearch_ingest.py)
→ 去重(dedup_check.py) → 故事弧线(story_arc_linker.py)
→ 实体提取 → 嵌入(encoder.py) → 特征聚类(feature_finder.py)
→ 日报模板 v3.1 (5段 + 民生观察)
→ 搜索反馈(search_feedback.py) → 次日搜索词 JSON
```

## anysearch 三槽查询

### Slot 1 — 学术搜索

- domain: academic, sub_domain: academic.search
- max_results: 5
- 8 主题轮转搜索，权重倾斜：社会 AI > 认知科学 > 其他
- 社会 AI 和认知科学每周 3 次，其他 2 次
- 每次 3 条并行查询

### Slot 2 — 国内政策/行业

- content_types: news, zone: cn, freshness: day, max_results: 8
- 单条通用搜索: "AI 政策 科技 监管 法律"
- 替代 6 个 WebFetch 源中的 5 个（36kr/人民网/新华网/光明网/TechNode）

### Slot 3 — 人文社科

- content_types: news, freshness: week, max_results: 8
- 四主题轮流，每天 2 个: 就业/教育/消费/基层治理
- 浙江优先，中英文混合

## 数据归一化

anysearch 结果 → `news_snippets` 表，section 标签:
- academic-high-impact
- policy-industry
- livelihood

实体提取、嵌入计算统一走现有管线。

## 搜索词自适应

`search_feedback.py` — 日报产出后运行：

1. 读当日日报，提取各主题产出得分（进日报=3分，进总览=1分，被过滤=0分）
2. 更新 `search_topic_weights` 表
3. 检测实体频次突增，标记新实体
4. 写出次日搜索词 JSON 到 `.constraint_cache.json`

高温主题加频次，连续 3 天空转降为低频（月查 1 次）。

## 日报新增：民生观察

位置：重点分析之后，因果追踪之前。

四格结构：就业 | 教育 | 消费 | 基层治理
每格 1-3 条，无信号标注"本周无显著信号"。
浙江优先，每条带"对菲菲"关联。

## 错误处理

- 单槽失败 → 不阻塞其他槽，日报标注跳过
- 三槽全失败 → 学术回退 arXiv only，政策回退占位
- search_feedback 失败 → 不影响日报，次日默认轮转

## Token 估算

每次日报 ~25,000 tokens（相比旧方案 ~31,000 净省 ~6,000）

## 实现步骤

1. `anysearch_ingest.py` — 数据归一化 (~60行)
2. `news_daily_search.py` — 采集调度脚本 (~120行)
3. `search_feedback.py` — 搜索词自适应 (~80行)
4. 日报模板 v3.1 + `generate_report.py` 修改
5. daily-news-trigger 工作流更新
6. 端到端测试
