# CLI Contract: DuoNews Pipeline

**Version**: v0.7.0 (proposed)
**Created**: 2026-05-31

## Entry Point

```bash
python -m duonews --step <name> --date <date> [--top N]
```

## Steps

| Step | Command | Input | Output |
|------|---------|-------|--------|
| `github` | `--step github` | GitHub Trending API | state.db `news_snippets` (section=github-trending) |
| `search` | `--step search` | `.constraint_cache.json` 查询词 | state.db `news_snippets` (section=academic-search/policy-industry/livelihood) |
| `arxiv` | `--step arxiv` | arXiv API | state.db `news_snippets` (section=academic-high-impact) |
| `preprocess` | `--step preprocess` | state.db news_snippets + 历史判断基线 | `.daily_brief.md` + `.pipeline_state.json` |
| `cross_day` | `--step cross_day` | state.db news_snippets (1d + 365d) | state.db `cross_day_discoveries` + stdout |
| `report_write` | `--step report_write` **(NEW)** | `.daily_brief.md` + `cross_day_discoveries` + `JudgmentBaseline` | Obsidian vault `news/<date>.md` |
| `push` | `--step push` | Obsidian vault `news/<date>.md` | 飞书 wiki + 群聊卡片 |
| `vectorize` | `--step vectorize` | Obsidian vault `news/<date>.md` | state.db `news_snippets` embeddings |
| `feedback` | `--step feedback` | Obsidian vault `news/<date>.md` | `.constraint_cache.json` (次日查询词) |
| `all` | `--step all` | 以上全部 | 以上全部，链式执行 |
| `diagnose` | `--step diagnose` | state.db | 诊断报告 |

## Pipeline Order (updated)

```python
PIPELINE_ORDER = [
    "github", "search", "arxiv",      # Phase 1: 数据拉取
    "preprocess", "cross_day",         # Phase 2: 预处理 + 分析
    "report_write",                    # Phase 3: 日报生成 (NEW)
    "push", "vectorize", "feedback"    # Phase 4: 分发 + 闭环
]
```

## `.pipeline_state.json` Schema

```json
{
  "run_id": "uuid",
  "date": "2026-05-31",
  "command": "all",
  "started_at": "2026-05-31T08:00:00+08:00",
  "steps": {
    "<step_name>": {
      "status": "pending|running|succeeded|failed|skipped",
      "started_at": "ISO8601|null",
      "completed_at": "ISO8601|null",
      "error": "string|null",
      "rows_affected": 0
    }
  }
}
```

## `--step all` 容错规则

| 失败步骤 | 行为 |
|----------|------|
| `github` | 标注跳过，继续 search |
| `search` 返回 0 | 重试 1 次（用前一天缓存查询词），仍 0 则 **阻断** |
| `arxiv` | 标注跳过，继续 preprocess |
| `preprocess` | **阻断** |
| `cross_day` | 标注"无跨日数据"，继续 report_write |
| `report_write` | **阻断**（无日报则无后续） |
| `push` | 标注失败，继续 vectorize |
| `vectorize` | 标注失败，继续 feedback |
| `feedback` | 标注失败，管线结束 |
