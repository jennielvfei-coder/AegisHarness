# Tasks: DuoNews P0 核心断点修复

**Input**: Design documents from `specs/001-duonews-p0-breakpoints/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/cli-contract.md

**Tests**: Not requested — verification via manual pipeline execution.

**Organization**: Tasks grouped by user story for independent implementation and testing.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1, US2, US3)
- Exact file paths included in all descriptions

## Path Conventions

- `duonews/` — DuoNews package (CLI + library)
- `harness/` — Harness shared library

---

## Phase 1: Setup

**Purpose**: Verify project is ready for implementation

- [x] T000 **[P] Verify harness hooks are wired** — check `D:\Claude\.claude\settings.local.json` contains SessionStart (`harness_daemon.py inject`), Stop (`harness_daemon.py observe`), UserPromptSubmit (`hooks.py user-msg` + `hooks.py news-detect`). Run `python harness/harness_daemon.py inject` — output must include SelfModel summary (no "Hook Integrity" warning). Without hooks, `cmd_inject` context (health status, constraint registry, intent matching) never reaches Claude during pipeline execution.
- [x] T001 Verify state.db is accessible and harness.indexer imports cleanly — run `python -c "from harness.indexer import HarnessDB; db = HarnessDB(); print('ok')"`
- [x] T002 [P] Verify duonews CLI entry point works — run `python -m duonews --help`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: DB schema + data model changes needed by all user stories

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [x] T003 Create `cross_day_discoveries` table DDL and `get_cross_day_discoveries()` / `save_cross_day_discovery()` methods in `harness/indexer.py`
- [x] T004 [P] Enhance `extract_judgment_baseline()` in `duonews/__init__.py` to return structured Prophet fields: `time_horizon_days`, `created_date`, `verification_criteria`, `status`, `key_entities` — backward-compatible, existing callers unaffected
- [x] T005 [P] Add optional `attention_entities: list[str] | None = None` parameter to `find_features()` in `harness/feature_finder.py` — when provided, matching entities get 1.5x detection weight

**Checkpoint**: Foundation ready — all three user stories can begin. T004 and T005 are [P] and can run in parallel.

---

## Phase 3: User Story 1 — 一键跑完整条管线 (Priority: P1) 🎯 MVP

**Goal**: 执行 `python -m duonews --step all --date <date>` 即可自动生成 Obsidian vault 完整日报，无需人工介入

**Independent Test**: 运行 `python -m duonews --step all --date 2026-06-01`，验证 `news/2026-06-01.md` 存在且包含完整 7 段结构

### Implementation for User Story 1

- [x] T006 [US1] Create `duonews/report_writer.py` with `generate_report(date_str)` function that:
  - Reads `.daily_brief.md` for classified snippets and analysis candidates
  - Reads `cross_day_discoveries` from state.db for semantic sniffing section
  - Reads `JudgmentBaseline` via `extract_judgment_baseline(find_recent_report(date_str))`
  - Generates 7-section report per news-template.md v3.3 format
  - Handles degraded mode: missing cross_day data → "今日无显著跨日关联"; missing history → "首次运行，无历史判断基线"
  - Writes final report to Obsidian vault `news/<date>.md`

- [x] T007 [US1] Modify `duonews/__main__.py`:
  - Insert `"report_write"` into `PIPELINE_ORDER` after `"cross_day"` and before `"push"`
  - Add `report_write` to `STEP_MAP`: `("duonews.report_writer", "generate_report")`
  - Add `report_write` branch in `_run_step()` calling `func(date_str=date_str)`
  - Add `.pipeline_state.json` write at each step start/complete in `--step all` loop:
    ```json
    {"run_id": "<uuid>", "date": "<date>", "steps": {"<name>": {"status": "...", ...}}}
    ```

- [x] T008 [US1] Implement fault tolerance in `--step all` loop in `duonews/__main__.py`:
  - `github` failure → skip, continue
  - `search` returns 0 → retry once with cached queries, still 0 → abort pipeline
  - `arxiv` failure → skip, continue
  - `preprocess` failure → abort (no data for report)
  - `cross_day` failure → skip, continue (report_writer handles missing data)
  - `report_write` failure → abort (no report, nothing to push)
  - `push`/`vectorize`/`feedback` failure → skip, continue (report already generated)

**Checkpoint**: `--step all` 一键生成完整日报，容错降级生效

---

## Phase 4: User Story 2 — 步骤间数据自动桥接 (Priority: P2)

**Goal**: `cross_day` 结果自动流入日报"语义嗅探"段，`preprocess` 输出以 markdown table 格式呈现

**Independent Test**: 检查日报"语义嗅探"段包含 cross_day 发现结果，速览表使用 `|col|col|` 格式

### Implementation for User Story 2

- [x] T009 [US2] Modify `search_cross_day()` in `duonews/cross_day.py` to save discoveries to `cross_day_discoveries` table via db after computing and ranking results. Add entity canonicalization step (normalize aliases → canonical names via `_ALIAS_TO_CANONICAL` from vectorize.py) before matching to improve cross-language entity alignment.

- [x] T010 [US2] Modify `generate_brief()` in `duonews/preprocess.py`:
  - Output markdown table format (`| 来源 | 标题 | 摘要 | 评级 |`) alongside numbered list format for the 速览表 section
  - Add a "## 语义嗅探原始数据" section to `.daily_brief.md` with cross_day discoveries loaded from `cross_day_discoveries` table, so report_writer has structured data to consume

- [x] T011 [US2] Update `generate_report()` in `duonews/report_writer.py` to consume:
  - Markdown table format from `.daily_brief.md` for the 新闻总览表 section
  - Cross-day discoveries from `.daily_brief.md` 语义嗅探 section (populated by T010) for the 语义嗅探 report section

**Checkpoint**: 步骤间数据通过 `.daily_brief.md` + state.db 完整传递，无 stdout 断裂

---

## Phase 5: User Story 3 — 昨日判断自动注入今日分析 (Priority: P3)

**Goal**: 历史日报中的"今日判断"和 Prophet 信号自动作为上下文进入当日分析和日报

**Independent Test**: 运行两天管线后，第二天日报"今日反馈"段引用第一天判断，Prophet 信号标注"观察中"状态

### Implementation for User Story 3

- [x] T012 [US3] Modify `generate_brief()` in `duonews/preprocess.py`:
  - Call `find_recent_report(date_str)` at start
  - If report found, call `extract_judgment_baseline(report_text)` to get structured `JudgmentBaseline`
  - Inject `JudgmentBaseline.key_entities` as `attention_entities` when calling `find_features()` (via score_snippets or independent call)
  - Add "## 历史判断基线" section to `.daily_brief.md` containing headline_judgment, hypotheses, and prophet_signals from baseline

- [x] T013 [US3] Update `generate_report()` in `duonews/report_writer.py`:
  - Read "历史判断基线" section from `.daily_brief.md`
  - Generate "今日反馈" report section with:
    - Yesterday's headline_judgment vs today's news contrast
    - Prophet signal到期检查: signals within time window → "观察中（第N天/总天数）"; expired → "待验证"
    - Hypothesis status updates: confirmed/popped/still-testing
  - Call `run_hypothesis_cycle()` from `harness.competing_hypotheses` with `anomaly_features` + `today_snippets` to generate "假设验证" section

**Checkpoint**: 历史判断闭环 — 昨日判断 → 今日检测加权 → 今日反馈对照

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Edge cases, robustness, final validation

- [x] T014 Handle edge cases across all modules:
  - First run (no history): report_writer labels "首次运行" instead of crashing
  - Empty state.db: pipeline aborts at search step with clear error message
  - Re-run same date: report_writer overwrites existing report, adds "重新生成" note
  - Corrupted history markdown: `extract_judgment_baseline()` returns empty dict gracefully
  - Cross-lingual entity overlap: canonicalization from T009 handles 英伟达↔NVIDIA cases

- [x] T015 Run full quickstart.md validation checklist:
  - `--step all` completes end-to-end
  - Obsidian vault report has all 7 sections
  - cross_day discoveries appear in 语义嗅探
  - Yesterday's judgment appears in 今日反馈
  - Tables use `|col|col|` markdown format
  - Prophet signals show observing/expired status
  - Pipeline state JSON records all step statuses
  - Single step failure does not crash pipeline

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No dependencies — start immediately
- **Phase 2 (Foundational)**: Depends on Phase 1 — BLOCKS all user stories
- **Phase 3 (US1)**: Depends on Phase 2 — No dependencies on US2/US3 (uses degraded mode)
- **Phase 4 (US2)**: Depends on Phase 2 — Can start in parallel with US1
- **Phase 5 (US3)**: Depends on Phase 2 + T012 needs T010's `.daily_brief.md` sections
- **Phase 6 (Polish)**: Depends on all user stories

### User Story Dependencies

- **US1 (P1)**: Independent after Phase 2 — uses degraded mode for missing US2/US3 data
- **US2 (P2)**: Independent after Phase 2 — enhances US1's report_writer after it exists
- **US3 (P3)**: Depends on Phase 2 + US2 T010 (needs `.daily_brief.md` sections added in T010)

### Within Each User Story

- US1: T006 (report_writer) → T007 (__main__ integration) → T008 (fault tolerance)
- US2: T009 (cross_day DB) → T010 (preprocess format) → T011 (report_writer consume)
- US3: T012 (preprocess inject) → T013 (report_writer feedback section)

### Parallel Opportunities

- Phase 2: T004 and T005 can run in parallel (different files)
- Phase 3 (US1) and Phase 4 (US2) can start in parallel after Phase 2
- Phase 5 (US3) can partially overlap with US2 (T012 can start after T010)

---

## Parallel Example: Phase 2 Foundational

```bash
# Run in parallel:
Task: "Enhance extract_judgment_baseline() in duonews/__init__.py"
Task: "Add attention_entities to find_features() in harness/feature_finder.py"
# These touch different files with no shared state
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup (T001-T002)
2. Complete Phase 2: Foundational (T003-T005)
3. Complete Phase 3: User Story 1 (T006-T008)
4. **STOP and VALIDATE**: Run `python -m duonews --step all --date <today>`, verify 7-section report generated
5. Deploy — pipeline is now automated end-to-end

### Incremental Delivery

1. Setup + Foundational → DB schema ready
2. Add US1 → `--step all` produces complete report → **MVP!**
3. Add US2 → cross_day data flows into report, tables are proper format → **Data integrity**
4. Add US3 → yesterday's judgments inform today's analysis → **Learning loop closed**
5. Polish → edge cases handled → **Production ready**

---

## Notes

- [P] tasks touch different files with no shared state — can truly run in parallel
- [US?] label maps task to specific user story for traceability
- Each user story checkpoint is independently verifiable
- No test tasks included (not requested in spec)
- Commit after each phase checkpoint
- US1 is the MVP — it alone transforms the pipeline from manual to automated
- Total: 15 tasks across 6 phases
