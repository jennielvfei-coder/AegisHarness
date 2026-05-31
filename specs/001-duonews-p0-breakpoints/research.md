# Research: DuoNews P0 核心断点修复

**Created**: 2026-05-31

## R1: 日报自动生成策略

**Decision**: 新增 `duonews/report_writer.py` 模块，读取 `.daily_brief.md` + cross_day 结果 + 历史判断基线，按 news-template.md v3.3 的 7 段结构生成完整日报。

**Rationale**:
- `preprocess.py` 已经完成了新闻精要的分类、实体提取、矛盾检测、重点分析候选等所有结构化工作
- 缺失的是"把这些结构化数据填入模板的 7 段结构"这一步——这是纯代码可完成的
- 当前这一步依赖 Claude 手工读 `.daily_brief.md` 然后写 Obsidian vault——把 Claude 的"写作"能力保留在 `report_writer` 中，但让调度自动化

**Alternatives considered**:
- 让 preprocess 直接输出完整日报 → 拒绝，preprocess 职责是数据准备，不应负责最终排版
- 完全去掉 Claude/LLM 参与 → 拒绝，日报的分析性段落（重点分析、因果追踪）需要 LLM 推理能力
- 新增一个 MCP-based agent → 过度设计，一个 Python 模块即可

## R2: 步骤间数据传递格式

**Decision**: 使用 `.pipeline_state.json`（JSON 文件）+ state.db 新表双轨传递。
- 轻量状态（每步的 status/timing/error）→ `.pipeline_state.json`
- 结构化业务数据（cross_day 发现、历史判断基线）→ state.db 新表

**Rationale**:
- JSON 文件：人类可读、易于调试、不依赖 DB schema 变更
- DB 表：大体积数据（cross_day 结果可能很多条）、支持 SQL 查询、与现有架构一致
- 双轨制：`report_writer` 优先读 DB，fallback 到 JSON

**Alternatives considered**:
- 仅 JSON → 拒绝，cross_day 发现可能很多，JSON 文件会膨胀
- 仅 DB → 拒绝，DB schema 变更需要 migration，JSON 更灵活
- 函数返回值传递（内存）→ 拒绝，`__main__.py` 当前每个步骤独立 import 并调用，内存不跨步骤

## R3: 孤儿函数接入策略

**Decision**: 在 `preprocess.py` 的 `generate_brief()` 中直接调用 `find_recent_report()` + `extract_judgment_baseline()`，将结果写入 `.daily_brief.md` 的特定段。在 `feature_finder.find_features()` 签名中新增可选的 `attention_entities` 参数。

**Rationale**:
- `find_recent_report()` 和 `extract_judgment_baseline()` 逻辑已经完整，只缺调用方
- 最小侵入性改动：在 preprocess 中增加一次函数调用 + 一段输出
- `attention_entities` 参数向后兼容——不传时行为不变

**Alternatives considered**:
- 新建独立步骤 "inject_history" → 拒绝，历史注入是 preprocess 的自然扩展，不需要单独步骤
- 修改 `competing_hypotheses` 直接读 Obsidian vault → 拒绝，破坏关注点分离

## R4: 管线容错策略

**Decision**: 每个步骤失败时写入错误状态到 `.pipeline_state.json`，后续步骤检查前序步骤状态，非致命失败降级继续。

致命失败（阻断）：
- search 返回 0 条且重试仍为 0（无数据无法继续）
- state.db 不可写

非致命失败（降级）：
- arxiv 抓取失败 → 日报标注，跳过学术段
- cross_day 无发现 → 日报标注"今日无显著跨日关联"
- push 飞书失败 → 日报已生成，推送稍后重试

**Rationale**: 部分结果 > 无结果。管线应保证在多数故障模式下仍有日报产出。

**Alternatives considered**:
- 遇错即停 → 拒绝，导致单点故障阻塞整条管线
- 完全忽略错误 → 拒绝，数据源故障不应静默
