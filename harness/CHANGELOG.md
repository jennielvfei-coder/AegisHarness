# Harness 更新日志

> 项目：Claude Code 自进化框架  
> 路径：`D:\Claude\harness\`  
> 19 个 commits | 15 测试全绿 | 2026-05-18 → 2026-05-19

---

## 一、需求驱动的核心升级

### 1. 资产/日记分类（Hermes 法则）

**问题**：每日新闻工作流被错误学成常驻技能，污染上下文。MCP memory.jsonl 信号密度极低（~4 条目/会话），Harness 几乎无法从正常对话中自动提炼有价值技能。

**解决**：
- refiner 新增 `SKILL_TYPE` 三分类：`env-fix`（跨任务修复）、`mental-model`（通用推理）、`task-workflow`（特定任务→存 fragment 不入技能库）
- LLM 同时输出 `QUALITY_SCORE`（0.0-1.0）和 `ACTION`（create/merge/discard）
- observer 预判：单一任务+无失败 → `save_fragment`，跳过 refiner
- **完全移除 MCP 依赖**，改用 `.claude/projects/*/uuid.jsonl` 原始 transcript 作为数据源
- 信号密度提升：4 条目/会话 → **463 条目/会话**（115x），tool_use 从 1 个模拟 → **287 个真实**

**相关文件**：`refiner.py`, `observer.py`, `scripts/get_last_session.py`

---

### 2. 信号检测升级：关键词 → 语义+统计

**问题**：硬编码关键词匹配贫瘠，隐式纠正漏检，置信度固定 0.7/0.65/0.5。

**解决**：
- 移除硬编码置信度，`_compute_confidence()` 统计驱动（工具多样性 + 错误计数 + 中断 + 消息量）
- 隐式纠正检测：编辑距离比对 + tool failure 检测 → "用户重新执行修改后的命令"自动识别
- 语义偏好检测：意图词 "记住/以后/默认/下次" 替代纯关键词
- 阈值调整：`min_tool_calls=3`, `min_content_length=1200`
- **仅有一次失败/中断的复杂会话才送入 refiner**，避免噪音

**相关文件**：`observer.py`（完整重写 v2.0）

---

### 3. 技能生命周期：合并、版本、路径

**问题**：重复技能无去重，版本无追踪，路径 `~/.claude/skills/harness/` 不兼容。

**解决**：
- 生成前 FTS5 搜索 `skill_index` → LLM 决定 create/merge/discard
- `evolution_log` 表记录所有版本变更
- 统一路径：`.claude/skills/harness_<type>_<name>.md`（无子目录）
- `_index_skill()` 自动 bump 版本号
- `QUALITY_SCORE` 回写到 `observations.confidence`

**相关文件**：`refiner.py`, `indexer.py`

---

### 4. 置信度模型升级

**问题**：`observer.py` 硬编码 0.7/0.65/0.5，无区分度。

**解决**：
- 初始置信度：`_compute_confidence()` 从会话统计特征计算
- refiner prompt 增加：`"Rate reusability and specificity 0.0-1.0. 0.8+ = concrete, repeatable workflow."`
- 回写：QUALITY_SCORE → observations.confidence
- 置信度现在有区分度：复杂会话 0.60-0.95，简单会话 0.15-0.46

**相关文件**：`observer.py`, `refiner.py`

---

### 5. 技能路径统一

**问题**：审查队列 `harness/skills/` → 批准后 `~/.claude/skills/harness/` → Claude Code 原生不识别。

**解决**：
- 批准后直接写入 `.claude/skills/harness_<type>_<name>.md`（无子目录）
- 清理旧路径残留
- review 面板显示 `[type] qs=X.XX`

**相关文件**：`harness_daemon.py`

---

### 6. 技能模块化（Hermes 三段式）

**问题**：生成的 SKILL.md 像说明书，不是可执行模板。

**解决**：
- 所有 prompt 模板更新为 Hermes 三段式结构：
  1. **Frontmatter**：name/description/tags/triggers/version/harness_confidence
  2. **执行逻辑**：When to Use → Step-by-Step → How to Verify
  3. **异常处理**：Edge Cases → Fallback
- 新增 `triggers` 字段支持按需注入

**相关文件**：`refiner.py`, `agents/skill_writer.py`

---

### 7. 按需注入（Trigger-based Loading）

**问题**：技能全量注入浪费 token。

**解决**：
- 三层叠加加载：
  - L1：Claude Code 原生 `.claude/skills/` 自动匹配
  - L2：frontmatter `triggers` 精准匹配
  - L3：`injector` 扫描当前用户消息 → 命中 triggers → "检测到X任务，是否加载Y技能？"
- 不全量注入，只提示

**相关文件**：`harness_daemon.py`（`cmd_inject` → `_match_triggers()`）

---

### 8. 记忆系统升级预留

**问题**：fragments 表纯文本匹配，未来需要向量检索。

**解决**：
- `fragments` 表新增 `embedding BLOB DEFAULT NULL`
- `harness_config.yaml` 预置 `vector_db` 段（`enabled: false`, `backend: "chroma"`）
- Phase 4 时只需开启配置

**相关文件**：`indexer.py`, `harness_config.yaml`

---

## 二、架构升级

### 子代理编排

**问题**：refiner 单体混杂"分析摘要"和"撰写技能"两个目标，共享 LLM 上下文。

**解决**：
- `agents/skill_writer.py` — 专职技能生成，独立 LLM 会话
- `agents/fragment_extractor.py` — 专职提取结构化记忆，独立 LLM 会话
- `harness_daemon.py` — 降级为纯编排器
- 触发规则：纠正/复杂+失败 → 双 agent；单一任务 → 只走 fragment_extractor

**相关文件**：`agents/skill_writer.py`, `agents/fragment_extractor.py`, `harness_daemon.py`

---

### Hooks 实时信号捕获

**问题**：所有分析依赖会话结束后，信号密度低。

**解决**：
- `UserPromptSubmit` hook：每条用户消息实时扫描 "不对/记住/以后" → 写入 `signal_buffer` 表
- `hooks.py`：PreToolUse + PostToolUse 已预留，分步上线
- 防护：`async: true` + 全局 try/except + DB 超时 2s + 纯内存匹配 <1ms

**相关文件**：`hooks.py`, `settings.json`

---

### 原始 Transcript 数据源

**问题**：MCP memory.jsonl 存知识图谱三元组，非真实对话。

**解决**：
- `scripts/get_last_session.py`：发现最近 `.claude/projects/*/uuid.jsonl` → 标准化为 JSONL
- 解析完整的 `tool_use` / `tool_result` / `is_error` / `stop_reason` 字段
- 去重 + 去 thinking 块

**信号对比**：

| 指标 | MCP memory（旧） | 原始 transcript（新） |
|------|-----------------|---------------------|
| 条目 | ~21 | 463 |
| Tool use | 1（模拟） | 287（真实） |
| Tool 多样性 | 1 种 | 24 种 |
| 错误检测 | 0 | `is_error` 标志 |

**相关文件**：`scripts/get_last_session.py`, `observer.py`

---

## 三、文件结构（终态）

```
D:\Claude\harness\
├── harness_daemon.py          # 编排器：observe / inject / review
├── observer.py                # v2.0 信号检测（统计置信度+隐式纠正）
├── refiner.py                 # 旧版兼容 wrapper → 委托 agents/
├── indexer.py                 # SQLite FTS5 存储（6表 + embedding 预留）
├── hooks.py                   # 实时 hook 处理器
├── harness_config.yaml        # 配置中心
├── state.db                   # SQLite 数据库
├── latest_session.jsonl       # 当前会话标准化 transcript
├── agents/
│   ├── skill_writer.py        # 子代理：ObservationReport → 技能文件
│   └── fragment_extractor.py  # 子代理：transcript → 结构化记忆
├── scripts/
│   ├── get_last_session.py    # 发现+标准化原始 transcript
│   ├── transcript_source.py   # 三源合并（fallback）
│   └── mcp_bridge.py          # MCP 桥（fallback）
├── skills/                    # 审查队列
│   └── archive/               # 已拒绝
├── sessions/                  # 历史会话归档
├── tests/
│   ├── test_observer.py       # 15 个单元测试
│   └── fixtures/              # 4 个场景 fixture
└── docs/
    └── plans/
```

---

## 四、Hooks 配置状态

```json
{
  "UserPromptSubmit": [{ "command": "python hooks.py user-msg", "async": true }],
  "SessionStart":     [{ "command": "python harness_daemon.py inject", "async": true }],
  "Stop":             [
    { "command": "python harness_daemon.py cleanup", "async": true },
    { "command": "python harness_daemon.py observe", "async": true }
  ]
}
```

---

## 五、已知限制

- **隐式纠正**：依赖 transcript 中相邻用户消息编辑距离比对，MCP 摘要中信号稀疏（原始 transcript 已大幅改善）
- **LLM 稳定性**：DeepSeek API 偶有 DNS 失败，agent 层已全局 try/except 保护
- **fragment_extractor**：依赖 LLM 生成质量，Phase 4 考虑本地小模型
- **向量 DB**：Chroma 集成留到 Phase 4
- **PreToolUse/PostToolUse hooks**：已预留，待 UserPromptSubmit 验证稳定后上线

---

*最后更新：2026-05-19 | 19 commits | 15 测试全绿*
