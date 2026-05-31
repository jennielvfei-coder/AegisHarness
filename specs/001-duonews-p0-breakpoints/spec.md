# Feature Specification: DuoNews P0 核心断点修复

**Feature Branch**: `001-duonews-p0-breakpoints`

**Created**: 2026-05-31

**Status**: Draft

**Input**: User description: "P0: 流水线调度 + 表格桥接 + 历史判断注入 — 修复 DuoNews 管线三大核心断点，使新闻工作流从半自动变为真正的端到端自动化系统。"

## User Scenarios & Testing *(mandatory)*

### User Story 1 - 一键跑完整条新闻管线 (Priority: P1)

运维者（Chucky）每天早晨希望执行一条命令就完成从数据拉取到日报落盘的完整流程，无需在步骤之间手动介入、复制粘贴或手工写日报。

**Why this priority**: 当前管线在 `preprocess` 步骤后停住——日报的 7 段结构（今日判断→重点分析→因果追踪→Prophet→今日反馈→假设验证→矛盾对）全部依赖人工阅读 `.daily_brief.md` 后手工写作。这是整条管线最大的手动断点。

**Independent Test**: 运行 `python -m duonews --step all --date 2026-06-01`，验证以下所有产出物自动生成且内容非空：
- `.daily_brief.md` 已刷新
- Obsidian vault `news/2026-06-01.md` 存在且 ≥7 段结构完整
- 飞书推送已发送

**Acceptance Scenarios**:

1. **Given** 当日 state.db 有 ≥30 条 news_snippets（含 github-trending ≥10 条），**When** 执行 `--step all`，**Then** Obsidian vault 日报在 5 分钟内自动生成，包含全部 7 段内容，无需人工编辑
2. **Given** 当日 search 步骤返回 0 条结果（API 故障），**When** 执行 `--step all`，**Then** 管线不崩溃，日报标注"今日搜索返回 0 条"，使用前一天缓存的备用查询重试一次
3. **Given** `preprocess` 步骤仅生成 15 条精要（不足 30 条阈值），**When** 执行后续步骤，**Then** 日报仍生成但标注"今日新闻量低于阈值（15/30）"

---

### User Story 2 - 步骤间数据自动桥接 (Priority: P2)

`cross_day` 的语义嗅探结果和 `preprocess` 的精要分类应自动流入日报，而不是各自打印到 stdout 再由人工拼接。

**Why this priority**: 管线中三个步骤（preprocess、cross_day、report_writer）各自产出有价值的结构化数据，但当前彼此不通信——`cross_day` 只打印到 stdout，`preprocess` 的编号列表与日报模板要求的 markdown table 格式不匹配。数据在步骤边界处断裂。

**Independent Test**: 单独检查每一步的日志，验证：
- `cross_day` 结果出现在最终日报的"语义嗅探"段
- `preprocess` 输出的速览数据以 `|col|col|` markdown table 格式出现在日报
- 中间 JSON 文件 `.pipeline_state.json` 包含每步的结构化输出

**Acceptance Scenarios**:

1. **Given** `cross_day` 发现了 3 对跨日实体关联（稀有度 >0.5），**When** `report_writer` 生成日报，**Then** 日报的"语义嗅探"段包含这 3 对关联及其实体名和稀有度得分
2. **Given** `preprocess` 将 30 条新闻分为 7 个类别表，**When** `report_writer` 读取数据，**Then** 日报的"新闻总览表"以 markdown table 格式（`| 来源 | 标题 | 摘要 | 评级 |`）呈现
3. **Given** 任意中间步骤失败，**When** 后续步骤读取 `.pipeline_state.json`，**Then** 能看到前一步的失败标记和已有数据，降级继续

---

### User Story 3 - 昨日判断自动注入今日分析 (Priority: P3)

昨天日报中的"今日判断"、Prophet 信号和假设应自动作为上下文注入今天的分析管线，而不是需要人工回溯昨天的日报。

**Why this priority**: `extract_judgment_baseline()` 和 `find_recent_report()` 已经写好但从未被任何管线代码调用——它们是孤儿函数。历史判断的闭环反馈完全断裂：昨天的判断从不影响今天的检测。

**Independent Test**: 运行两天的管线后验证：
- 第二天日报的"今日反馈"段引用了第一天的"今日判断"
- 第一天的 Prophet 信号中仍在时间窗口内的，在第二天日报中被标记为"待验证"
- `competing_hypotheses` 的假设池中包含从昨天日报提取的假设

**Acceptance Scenarios**:

1. **Given** 昨天日报的"今日判断"为"中美芯片管制将在 30 天内进一步升级"，**When** 今天运行 `preprocess`，**Then** `.daily_brief.md` 的"今日反馈"段包含这条历史判断及与今日新闻的对照
2. **Given** 昨天日报有一个 Prophet 信号 P1"30 天内 H200 出口管制进一步收紧 (置信度 0.65)"，且今天距该信号生成不足 30 天，**When** 今天运行 `report_writer`，**Then** 日报的"今日反馈"段标注 P1 为"观察中（第 N 天/30 天）"，并列出相关今日新闻
3. **Given** 昨天没有日报（间隔 3 天），**When** `find_recent_report()` 回溯查找，**Then** 自动找到 3 天前的最新报告并提取判断基线
4. **Given** 历史判断中提到的实体（如"H200"、"BIS"）出现在今日新闻中，**When** `feature_finder` 检测异常，**Then** 这些实体的检测权重自动提升 1.5x

---

### Edge Cases

- 首次运行（无历史日报）时，"今日反馈"段标注"首次运行，无历史判断基线"
- state.db 为空时，管线在 search 步骤后终止并报错，不继续到 preprocess
- 日报文件已存在（同一天跑两次）时，`report_writer` 覆盖旧文件并标注"重新生成"
- 历史日报格式损坏（非标准 markdown）时，`extract_judgment_baseline()` 返回空 dict，不阻塞管线
- 中英文新闻对同一事件的报道（如"英伟达被调查" vs "NVIDIA under investigation"）被 `cross_day` 的规范化步骤合并为同一实体

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: 管线 MUST 新增 `report_write` 步骤，自动将 `.daily_brief.md` 转换为符合 `news-template.md` v3.3 格式的完整日报并写入 Obsidian vault
- **FR-002**: `PIPELINE_ORDER` MUST 在 `preprocess` 和 `push` 之间插入 `report_write` 步骤
- **FR-003**: 管线步骤间 MUST 通过结构化数据（JSON 或 DB 表）传递结果，不再仅依赖 stdout 文本输出
- **FR-004**: `cross_day` 的结果 MUST 写入 state.db 的 `cross_day_discoveries` 新表，供 `report_writer` 消费
- **FR-005**: `preprocess.py` 的速览表输出 MUST 同时生成 markdown table 格式（`|col|col|`）和编号列表格式
- **FR-006**: `report_writer` MUST 在生成日报前自动调用 `find_recent_report()` 和 `extract_judgment_baseline()`，将历史判断注入"今日反馈"段
- **FR-007**: `extract_judgment_baseline()` 的输出 MUST 包含结构化字段：`headline_judgment`、`hypotheses`（含 status）、`prophet_signals`（含 time_horizon_days 和 created_date）、`key_entities`
- **FR-008**: `feature_finder` 的 `find_features()` MUST 接受可选的 `attention_entities` 参数，对匹配实体的检测置信度加权
- **FR-009**: `report_writer` MUST 对 Prophet 信号做到期检查：未到期信号标注"观察中（第 N 天/总天数）"，已到期信号标注"待验证"
- **FR-010**: 管线任意步骤失败时，MUST 记录失败状态到 `.pipeline_state.json`，后续步骤读取该状态决定降级策略。降级判断的上下文（数据源健康状态、约束阻断记录）由 harness `cmd_inject` 通过 SessionStart hook 注入
- **FR-011**: `report_write` 步骤 MUST 调用 `competing_hypotheses.run_hypothesis_cycle()` 生成"假设验证"段内容
- **FR-012**: 日报中 MUST 保留"矛盾对"段（`_find_contradictions()` 的结果），不因自动化而丢失

### Key Entities

- **PipelineStep**: 管线中的一个步骤，属性包括 step_name、status（pending/running/succeeded/failed）、started_at、completed_at、error_message
- **CrossDayDiscovery**: 跨日实体关联，属性包括 today_snippet_id、history_snippet_id、shared_entities、jaccard、rarity、cross_section_bonus
- **JudgmentBaseline**: 历史判断基线，属性包括 source_date、headline_judgment、hypotheses（list）、prophet_signals（list，每个含 claim/verification_criteria/time_horizon_days/confidence/created_date）、key_entities
- **DailyReport**: 最终日报，属性包括 date、sections（7 段）、snippet_count、generation_duration_seconds

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 从执行 `--step all` 到 Obsidian vault 日报落盘的总耗时不超过 5 分钟（不含 LLM API 调用时间）
- **SC-002**: 日报的 7 段结构完整率达到 100%（无遗漏段）
- **SC-003**: 历史判断注入后，"今日反馈"段至少包含 1 条昨日判断对照（有历史日报时）
- **SC-004**: `cross_day` 发现结果在日报中的呈现率达到 100%（即所有发现的跨日关联都出现在日报中）
- **SC-005**: 管线在任意单步骤失败时不崩溃，降级完成率达到 100%（至少生成含标注的部分日报）
- **SC-006**: 从"人工跑每个步骤 + 手工写日报"到"一条命令跑完整管线"，人工介入次数从日均 5-8 次降到 1 次（仅需确认推送）。测量方法：通过 `signal_buffer` 表中 `correction`/`retry` 信号每日计数——目标日均 ≤1 次

## Assumptions

- Harness hooks（SessionStart/Stop/UserPromptSubmit）已在项目 `settings.local.json` 中正确配置并触发——确保 `cmd_inject` 在管线执行前注入 SelfModel 健康状态、约束注册表和意图匹配上下文
- Claude Code 在管线执行期间可用（用于 `report_writer` 中的日报内容生成）
- Obsidian vault 路径不变（`~/Documents/Obsidian Vault/claude专属文件夹/news/`）
- `news-template.md` v3.3 的 7 段结构保持稳定
- state.db schema 可扩展（新增 `cross_day_discoveries` 表和 `pipeline_runs` 表）
- anysearch CLI 和 World News API MCP 保持当前可用状态
- 中文和英文新闻的实体规范化通过现有 `ENTITY_DICT` 的 alias 映射完成（跨语言增强在后续 P3 中处理）
