# Data Model: DuoNews P0 核心断点修复

**Created**: 2026-05-31

## 新增实体

### PipelineRun

一次管线执行记录。

| 字段 | 类型 | 说明 |
|------|------|------|
| run_id | TEXT PK | UUID，每次 `--step all` 生成一个 |
| date | TEXT | 执行日期 YYYY-MM-DD |
| started_at | TEXT | ISO 8601 开始时间 |
| completed_at | TEXT | ISO 8601 完成时间（可为 NULL） |
| status | TEXT | running/succeeded/partial/failed |
| error_count | INTEGER | 失败步骤计数 |

### PipelineStepStatus

单步骤执行状态。存储在 `.pipeline_state.json` 中（不建 DB 表，保持轻量）。

```json
{
  "run_id": "uuid",
  "date": "2026-05-31",
  "steps": {
    "github":    {"status": "succeeded", "started_at": "...", "completed_at": "...", "error": null},
    "search":    {"status": "succeeded", "started_at": "...", "completed_at": "...", "error": null},
    "arxiv":     {"status": "failed",    "started_at": "...", "completed_at": "...", "error": "Connection refused"},
    "preprocess":{"status": "succeeded", "started_at": "...", "completed_at": "...", "error": null},
    "cross_day": {"status": "succeeded", "started_at": "...", "completed_at": "...", "error": null},
    "report_write": {"status": "pending", ...},
    "push":      {"status": "pending", ...},
    "vectorize": {"status": "pending", ...},
    "feedback":  {"status": "pending", ...}
  }
}
```

### CrossDayDiscovery

跨日实体关联发现结果。存储在新 DB 表 `cross_day_discoveries` 中。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增 |
| run_date | TEXT | 运行日期 |
| today_snippet_id | INTEGER | 当日 snippet FK |
| history_snippet_id | INTEGER | 历史 snippet FK |
| history_date | TEXT | 历史 snippet 日期 |
| shared_entities | TEXT(JSON) | 共享实体列表 |
| jaccard | REAL | Jaccard 相似度 |
| rarity | REAL | 稀有度得分 |
| cross_section | REAL | 跨板块奖励因子 |
| created_at | TEXT | ISO 8601 |

### JudgmentBaseline（内存对象，不入库）

从历史日报提取的判断基线。结构已定义在 `__init__.py:102-131`。

```python
{
    "date": "2026-05-30",
    "headline_judgment": "中美芯片管制将在30天内进一步升级",
    "hypotheses": [
        "H1: 芯片管制升级 → 正在验证",
        "H2: AI投资从模型层转向芯片层 → 已确认"
    ],
    "prophet_signals": [
        {
            "id": "P1",
            "claim": "30天内H200出口管制进一步收紧",
            "time_horizon_days": 30,
            "confidence": 0.65,
            "created_date": "2026-05-30",
            "verification_criteria": "BIS新规/ASML声明/台积电公告",
            "status": "observing"  # observing | expired_unverified | verified | falsified
        }
    ],
    "key_entities": ["H200", "BIS", "台积电", "芯片管制"]
}
```

### DailyReport 段结构

最终日报的 7 段（对应 news-template.md v3.3）：

1. **今日判断** — ≤100 字头条判断
2. **新闻总览表** — 7 个分类的 markdown table
3. **重点分析** — 2-3 篇深度分析
4. **因果追踪** — 假设验证 + 新假设 + 矛盾对
5. **Prophet 信号** — 活跃预测及状态
6. **语义嗅探** — cross_day 发现
7. **今日反馈** — 历史判断对照 + Prophet 到期检查

## 现有实体变更

### news_snippets（无 schema 变更）

现有表，无需修改。`report_writer` 读取此表获取当日新闻。

### search_topic_weights（无变更）

现有表，`feedback.py` 维护。

### feature_finder.find_features() 签名变更

```python
# 旧签名
def find_features(today_snippets, db, k=15, feature_lib_entries=None)

# 新签名
def find_features(today_snippets, db, k=15, feature_lib_entries=None,
                  attention_entities: list[str] | None = None)
```

`attention_entities` 中的实体在聚类中被赋予 1.5x 权重，使其更可能被检测为异常。
