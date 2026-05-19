# Harness — Claude Code 自进化框架 设计文档

> 版本 v1.0 | 2026-05-18 | 参考 Hermes Agent v0.10.0 架构

---

## 一、目标

在 Claude Code 上架一层轻量 Python harness，实现三个能力：

1. **从经验中自动创建技能** — 会话结束后自动分析，提取可复用的工作模式 → 写入 `.claude/skills/harness/` 技能文件
2. **使用中自改进** — 检测到用户纠正或新变化时，自动 patch 已有技能
3. **跨会话记忆** — 下次相关任务时，自动注入匹配的 prompt 片段和技能引用

用户正常使用 Claude Code，Harness 在后台观察→提炼→进化。无需手动触发。

---

## 二、架构

```
D:\Claude\harness\
├── harness_daemon.py        # 主调度：监听 hook 信号，调度 observe→refine→index→inject
├── observer.py              # 观察：读取 transcript，判断"值不值得提炼"
├── refiner.py               # 提炼：调 LLM 生成技能草稿 + prompt 片段
├── indexer.py               # SQLite FTS5 索引：技能 + 片段 + 标签 + 进化日志
├── injector.py              # 注入：StartSession hook → 搜索匹配的 prompt 片段
├── harness_config.yaml      # 开关、阈值、LLM 配置
├── state.db                 # 技能索引 + prompt 片段库 + 进化日志
├── skills/                  # 自动生成的技能文件 → 同步到 .claude/skills/harness/
├── prompts/                 # prompt 片段 JSON，按 tag/场景分类
└── scripts/                 # Python 辅助脚本（B层）
```

### 数据流

```
Claude Code StopSession hook 触发
    │
    ▼
harness_daemon.py observe --session-id <id>
    │
    ├─ observer.py
    │    读取 transcript
    │    判断: 新能力 / 修正 / 事实 / 噪音
    │
    ├─ refiner.py (仅当 observer 判定"值得提炼")
    │    调 cheap LLM 分析
    │    生成: 技能草稿 / prompt片段 / 偏好更新
    │
    ├─ indexer.py
    │    写入 state.db (SQLite FTS5)
    │    版本追踪
    │
    └─ [Phase 2+] 写 .claude/skills/harness/<skill>.md

Claude Code StartSession hook 触发
    │
    ▼
harness_daemon.py inject --session-id <id>
    │
    ├─ injector.py
    │    分析当前 session 意图
    │    搜索 state.db 匹配的 prompt 片段
    │    输出注入文本 → Claude Code 上下文中加载
```

---

## 三、核心模块

### 3.1 observer.py — 观察层

**输入**: Claude Code transcript（JSONL 或 memory.jsonl）
**输出**: `ObservationReport` — 值得提炼什么，为什么

**判断逻辑**（参考 Hermes MEMORY_GUIDANCE）：

```
对话分析:
  - 检测"纠正模式"     → action: "patch_skill"
  - 检测"新工作流"      → action: "create_skill"
  - 检测"用户偏好陈述"  → action: "update_preference"
  - 检测"事实信息"       → action: "save_fragment"
  - 闲聊 / 简单Q&A      → action: "skip"
```

**信号检测规则**：

| 模式 | 触发条件 |
|------|---------|
| 纠正 | 用户说"不对""不是这样""应该是"，且之后给出了正确做法 |
| 新工作流 | 5+ tool calls + 成功完成任务 + 涉及特定领域知识 |
| 偏好 | 用户说"以后都""我总是""帮我记住" |
| 事实 | 用户披露了影响未来判断的信息（法域、阈值、角色） |

### 3.2 refiner.py — 提炼层

**输入**: ObservationReport + transcript 片段
**输出**: 技能文件草稿 (markdown) / prompt 片段 (结构化 JSON) / 偏好更新

**LLM 配置**:

| 用途 | 模型 | 说明 |
|------|------|------|
| 分析 transcript | deepseek-v4 或 haiku | 便宜、快 |
| 生成技能文件 | deepseek-v4（主力） | 质量要求高 |

**技能文件格式**（兼容 Claude Code skills 标准）：

```markdown
---
name: <kebab-case>
description: <one-line summary>
tags: [tag1, tag2]
triggers: [when to suggest]
version: 1
auto_generated: true
harness_confidence: 0.85
---

# <Skill Name>

## When to Use
...

## How To
...

## Evolution Log
- 2026-05-18 v1: Auto-created from session <id>
```

**Prompt 片段格式**：

```json
{
  "tag": "contract-review",
  "trigger": "审查供应商协议中的责任上限条款",
  "content": "根据2026-05-18的修正：责任上限通常以合同金额的1-2倍为基准...",
  "confidence": 0.9,
  "source_session": "abc123"
}
```

### 3.3 indexer.py — 索引层

**SQLite 结构**（参考 Hermes SessionDB 的 FTS5 设计）：

```sql
-- Prompt 片段库（类比 Hermes 的 messages_fts，但存的是提炼后的片段而非原始消息）
CREATE TABLE fragments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag TEXT NOT NULL,
    trigger_phrases TEXT,     -- JSON array
    content TEXT NOT NULL,
    source_session TEXT,
    confidence REAL DEFAULT 0.5,
    hit_count INTEGER DEFAULT 0,
    last_hit REAL,
    created_at REAL NOT NULL,
    updated_at REAL
);
CREATE VIRTUAL TABLE fragments_fts USING fts5(tag, trigger_phrases, content);

-- 技能索引
CREATE TABLE skill_index (
    name TEXT PRIMARY KEY,
    file_path TEXT NOT NULL,
    tags TEXT,                -- JSON array
    trigger_patterns TEXT,    -- JSON array
    version INTEGER DEFAULT 1,
    harness_confidence REAL DEFAULT 0.5,
    created_at REAL NOT NULL,
    updated_at REAL,
    usage_count INTEGER DEFAULT 0,
    last_used REAL
);

-- 进化日志
CREATE TABLE evolution_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name TEXT,
    action TEXT,              -- 'create', 'patch', 'deprecate'
    source_session TEXT,
    change_summary TEXT,
    old_version INTEGER,
    new_version INTEGER,
    timestamp REAL NOT NULL
);

-- 会话元数据（轻量版，不存全量消息）
CREATE TABLE session_meta (
    id TEXT PRIMARY KEY,
    title TEXT,
    tags TEXT,
    observation_action TEXT,  -- 'create_skill', 'patch_skill', 'save_fragment', 'skip'
    processed_at REAL
);
```

### 3.4 injector.py — 注入层

**输入**: 当前 session 的初始 user message（来自 StartSession hook 上下文）
**输出**: 注入文本，追加到 Claude Code 的系统上下文

**匹配逻辑**：

1. 用 user message 搜索 fragments_fts → 取 top-3 相关片段
2. 用 user message 匹配 skill_index.trigger_patterns → 提示相关技能
3. 组合注入：

```
[Harness: 以下是你从过去经验中学到的]

## 相关记忆
- [fragment.content]（置信度: 0.9）

## 相关技能
- /harness:contract-review 版本3，上次使用2026-05-15
```

---

## 四、Claude Code 集成

### 4.1 Hooks 配置

```json
{
  "hooks": {
    "StopSession": [{
      "matcher": "*",
      "command": "python D:\\Claude\\harness\\harness_daemon.py observe"
    }],
    "StartSession": [{
      "matcher": "*",
      "command": "python D:\\Claude\\harness\\harness_daemon.py inject"
    }]
  }
}
```

### 4.2 技能输出路径

自动生成的技能写入 `~\.claude\skills\harness\`，Claude Code 自动识别为 `/harness:<skill-name>`。

### 4.3 进化日志 → 用户 memory

检测到用户偏好变更时，同步更新 `C:\Users\Chucky\.claude\projects\D--Claude\memory\` 下的 memory 文件。

---

## 五、自改进循环

```
会话中：
  用户: "不对，责任上限我们习惯用合同金额2倍，不是1倍"
  Claude Code 按修正后的值回答

会话结束（StopSession）：
  observer: 检测到"纠正模式"，action=patch_skill
  refiner: 生成 patch — 更新 commercial-legal 审查指引中的责任上限默认值
  indexer: skill_index.commercial-legal version 1→2, evolution_log 记录

下次审查合同（StartSession）：
  injector: "当前任务匹配 commercial-legal 技能（v2，修正了责任上限默认值）"
  Claude Code 直接用 v2 的值回答 → 不需要用户再纠正
```

---

## 六、Hermes 参考的设计模式

| Hermes 模块 | 借鉴的设计 | 我们简化了什么 |
|------------|-----------|--------------|
| `hermes_state.py` SessionDB | SQLite WAL + FTS5 + 版本迁移 | 不存全量消息，只存提炼后的片段和索引 |
| `prompt_builder.py` MEMORY_GUIDANCE | "陈述事实，不写指令"的记忆基调 | 直接沿用 |
| `prompt_builder.py` SKILLS_GUIDANCE | 复杂任务后自动保存技能 | 做成 hook 自动化，而非 agent 自觉调用 |
| `trajectory_compressor.py` | 分析 transcript 提取信号 | 不压缩完整 trajectory，只提取 pattern summary |
| `skills/` SKILL.md 格式 | YAML frontmatter + markdown body | 兼容 Claude Code 的 skill 格式 |
| `toolsets.py` skill_manage 工具 | 自动创建/更新技能的 API | 我们通过 refiner.py 实现，不暴露为 tool |

---

## 七、落地节奏

| Phase | 内容 | 依赖 | 状态 |
|-------|------|------|------|
| **1** | observer.py + SQLite schema + StopSession hook。只观察不生成，验证 transcript 读到且判断正确 | harness_daemon.py 骨架 | 待实现 |
| **2** | refiner.py 生成技能草稿 → 写入 `skills/harness/`。人工审核后激活 | Phase 1 稳定 | 待实现 |
| **3** | injector.py + StartSession hook。prompt 片段自动注入 | Phase 2 稳定 | 待实现 |
| **4** | 全自动闭环。observer→refiner→injector 无人值守，含自修正 | Phase 3 充分验证 | 待实现 |

---

## 八、配置文件

`harness_config.yaml`：

```yaml
harness:
  transcript_source: "C:\\Users\\Chucky\\.claude\\projects\\D--Claude\\memory.jsonl"
  skills_output_dir: "C:\\Users\\Chucky\\.claude\\skills\\harness"
  memory_output_dir: "C:\\Users\\Chucky\\.claude\\projects\\D--Claude\\memory"
  db_path: "D:\\Claude\\harness\\state.db"

observer:
  min_tool_calls_for_skill: 5
  skip_trivial_sessions: true

refiner:
  model: "deepseek-v4-pro"
  max_tokens_per_skill: 2000

injector:
  max_fragments: 3
  min_confidence: 0.6
  enabled: false  # Phase 3 才开

evolution:
  auto_activate_skills: false  # Phase 4 才开
  require_review: true
```

---

*设计完成日期：2026-05-18 | 下一步：writing-plans 生成实现计划*
