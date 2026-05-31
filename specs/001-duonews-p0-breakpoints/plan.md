# Implementation Plan: DuoNews P0 核心断点修复

**Branch**: `001-duonews-p0-breakpoints` | **Date**: 2026-05-31 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/001-duonews-p0-breakpoints/spec.md`

## Summary

修复 DuoNews 管线三个核心断点：新增 `report_write` 步骤实现日报自动生成（调度修复），通过 state.db + `.pipeline_state.json` 实现步骤间结构化数据传递（表格桥接），将孤儿函数 `extract_judgment_baseline()` 接入管线实现历史判断闭环（历史判断注入）。

## Technical Context

**Language/Version**: Python 3.11+
**Primary Dependencies**: harness.indexer (SQLite), anysearch CLI, MCP world-news-api
**Storage**: SQLite (state.db) + JSON (.pipeline_state.json)
**Testing**: Manual verification via `python -m duonews --step all --date <date>`
**Target Platform**: Windows 11, PowerShell
**Project Type**: CLI tool + library package
**Performance Goals**: Full pipeline <5 min (excluding LLM API calls)
**Constraints**: Must not break existing `--step <single>` invocation; backward-compatible
**Scale/Scope**: ~6 files modified, ~2 files created, ~300 lines net new code

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

本项目 constitution 为模板未填充。以 CLAUDE.md 铁律为 de facto 原则：

| 铁律 | 符合 | 说明 |
|------|------|------|
| 一、先验证，再架构 | ✅ | 代码验证完成，基于验证结果设计 |
| 二、产出物有预算 | ✅ | 每个文件内容精简，无冗余 |
| 三、锚需求 | ✅ | 三个断点直接来自用户诊断，不衍生额外需求 |
| 四、第一响应给诊断 | ✅ | 诊断在先，方案在后 |
| 五、自解不过二 | ✅ | 首轮方案 |

**Gate Result**: PASS

## Project Structure

### Documentation (this feature)

```text
specs/001-duonews-p0-breakpoints/
├── plan.md
├── spec.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   └── cli-contract.md
├── checklists/
│   └── requirements.md
└── tasks.md             # /speckit-tasks output
```

### Source Code (repository root)

```text
duonews/
├── __main__.py              # [MODIFY] PIPELINE_ORDER + _run_step + .pipeline_state.json
├── __init__.py              # [MODIFY] extract_judgment_baseline() 返回结构化字段
├── preprocess.py            # [MODIFY] 调用 find_recent_report + 表格格式输出
├── cross_day.py             # [MODIFY] 结果写入 cross_day_discoveries 表
├── report_writer.py         # [NEW] 日报自动生成 (~200 lines)
└── .pipeline_state.json     # [NEW] 运行时生成

harness/
├── indexer.py               # [MODIFY] cross_day_discoveries 表 + CRUD
└── feature_finder.py        # [MODIFY] attention_entities 参数
```

**Structure Decision**: DuoNews 是独立 Python 包，新增模块放入 `duonews/`。Harness 改动最小（DB schema + 函数签名）。

## Complexity Tracking

无违规，无需记录。

## Implementation Phases

### Phase A: 数据库 + 数据模型（无破坏性）

1. `harness/indexer.py`: 新增 `cross_day_discoveries` 表 DDL + `get_cross_day_discoveries()` / `save_cross_day_discovery()` 方法
2. `duonews/__init__.py`: `extract_judgment_baseline()` 增强——Prophet 信号返回 `time_horizon_days`、`created_date`、`verification_criteria`、`status`、`key_entities`
3. `harness/feature_finder.py`: `find_features()` 新增可选参数 `attention_entities: list[str] | None = None`

### Phase B: 表格桥接 + 历史注入

4. `duonews/cross_day.py`: `search_cross_day()` 结束后写入 `cross_day_discoveries` 表
5. `duonews/preprocess.py`:
   - `generate_brief()` 调用 `find_recent_report()` + `extract_judgment_baseline()`
   - 速览表增加 markdown table 格式输出
   - `.daily_brief.md` 增加"历史判断基线"段和"cross_day 发现"段

### Phase C: 日报自动生成

6. `duonews/report_writer.py`（新增）:
   - `generate_report(date_str)` — 主编排
   - 读取 `.daily_brief.md` + `cross_day_discoveries` + `JudgmentBaseline`
   - 按 news-template.md v3.3 7 段结构生成日报
   - Prophet 信号到期检查
   - 调用 `competing_hypotheses.run_hypothesis_cycle()` 生成假设段
   - 写入 Obsidian vault `news/<date>.md`

### Phase D: 管线调度 + 容错

7. `duonews/__main__.py`:
   - `PIPELINE_ORDER` 插入 `report_write`
   - `_run_step()` 新增 `report_write` 分支
   - 每步写入 `.pipeline_state.json`
   - `--step all` 按容错规则降级
8. 端到端验证

## Task Dependencies

```
Phase A (DB + model) ──→ Phase B (bridge + inject) ──→ Phase C (report_writer) ──→ Phase D (orchestration)
     ↓                          ↓                              ↓                          ↓
  无破坏性                   依赖 A 的表                  依赖 B 的数据             依赖 C 的模块
  可独立测试                 可独立测试                   可独立测试                端到端测试
```
