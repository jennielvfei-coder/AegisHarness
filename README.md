# Harness

> **少就是多。**

这不是一个让 Claude 更聪明的系统。这是一个让 Claude 可预测、可审计、可纠错的**更可信**系统。

正常使用 Claude Code。Harness 在后台观察 → 提炼 → 进化。它不是助手，不是记忆库，不是插件。它是一张**约束传播网络**——每一个新模块的存在意义，不是增加能力，而是阻止另一个模块在特定失败模式下犯错。

---

## 与其他 Agent 框架的本质区别

| | 其他 Agent 框架（LangGraph、Hermes 等） | 本 Harness |
|---|---|---|
| **核心隐喻** | 图书馆员 — 帮你记住更多 | 外骨骼 — 物理阻止你做出错误动作 |
| **设计哲学** | 加法：加一个模块来解决一个问题 | 减法：每个模块的存在意义是阻止另一个模块犯错 |
| **LLM 角色** | 必需品 — 核心管线依赖 LLM 推理 | 可选项 — 核心管线零 LLM 调用（关键词 + DB + 正则 + 查表） |
| **信任来源** | "我总是对" | "错在哪一步可以被定位" |
| **进化方向** | 功能堆砌（OR：再加一个 X 或者 Y） | 约束传播（AND NOT：做 X，除非 Y 说不） |
| **知识管理** | 囤积 — 技能越多越好 | 不养闲人 — 闲置自动降权，约束自动过期 |
| **反馈机制** | 正向强化 | 用户纠正权重压倒一切（一次纠正 > 所有正向信号之和） |

**一句话：** 其他框架追求"让 AI 更强大"。本 Harness 追求"让 AI 更可信"。强大的系统你不知道它什么时候犯错。可信的系统，犯错可以被定位、被追溯、被阻止。

---

## 关键设计决策

### LLM 从必需品变成可选项

核心管线零 LLM 调用：PreThink 是关键词 + DB 权重查表，Observer 是正则 + 编辑距离 + 统计计数，Omega 是 3×3 规则矩阵 + DB 查表，session_quality 是纯算术加权。LLM 只在边缘精炼层出现——负责"把已确定的结论写得更清楚"，不负责"决定结论是什么"。

**不是更快、不是更便宜。是可信任。** 同样的输入，永远产出同样的输出。你可以回溯每一步，因为每一步都是确定性的规则。

### 知识不养闲人

技能 7 天无调用自动归档。约束 24 小时自动过期（已验证约束除外）。系统在持续使用中越用越强，停用后自动瘦身。不囤积。

### 减法优于加法

当被问到"有什么不足"时，默认反应不是"加一个东西来解决"，而是——**这条约束是不是该过期了？这个技能是不是从来没被调用过？** 加新段前先删旧段。加新功能前先确认用户 3 天内是否需要。

---

## 核心架构：约束传播网络

Harness 不是一个能力集合。它是一张约束之网。每个节点都在问同一个问题：**"另一个节点是不是要犯错了？"**

```
PreThink ──→ 约束 Observer（routine 不产生信号，只有异常才值得处理）
Observer ──→ 约束 Omega（数据质量失败 ≠ 操作失败，不能混为一谈）
Omega    ──→ 约束 harness_daemon（warn/block/learn 是有后果的行动，不可轻发）
session_quality ──→ 约束 Attention Fuser（用户纠正权重压倒一切）
cosine_gate ──→ 约束 injection（任务切换时回退关键词匹配，不强行注入）
False Structure Filter ──→ 约束 competing_hypotheses（排除虚假模式）
BudgetNode ──→ 约束注入长度（routine = 5 行，不高估自己的重要性）
```

**智能来自约束之间的张力，不是来自模块的叠加。**

每多加一个模块，问题不是"它加了什么能力"，而是"它阻止了什么错误"、"它自己会在什么情况下出错"、"谁在约束它"。

---

## 约束优于知识

这是整个 Harness 最根本的设计选择。

```
知识：  "这个 API 昨天失败了 3 次，建议用备用源。"  → Claude 忽略，继续调用
约束：  "⛔ 此调用已被阻断。连续失败 3 次。"        → 调用不会发生
```

PreToolUse hook 在工具调用发生前拦截。不是提示，不是建议，是硬阻断。知识会被上下文淹没。约束不会被忽略。

约束生灭有期：跨 session 模式自动创建 24h 约束；session 内网络故障自动创建 1h 临时约束；已验证的虚警约束自动解除。**不养永久的规则。**

---

## 反馈必须闭环

不靠人说"这个很有用"。反馈是二进制的、可观测的、自动化的：

- 失败模式在本次 session 复现？→ 关联技能降权
- 整个 session 没有失败模式复现？→ 关联技能小幅加权（正向反馈，防止"一切衰减到零"）
- 约束创建后误报了几次？→ Sandbox Verifier 在创建前评估风险，Guardian 在事后标记虚警

虚警被标记，不被删除。你能看到系统错在哪里——这正是"可信"的定义。

Guardian 是独立守护进程。它有自己的 SQLite 连接，不 import 任何 harness 模块。管线崩了，Guardian 继续每 60 秒验证一次信念。

---

## 代码层是信任的锚

`intent_matcher.py` 是关键词加权，零 LLM 调用。`hooks.py` 是正则匹配，不靠 Claude 记住"Bash 不能跑 PowerShell"。`harness_guardian.py` 独立验证，不靠管线"记得"该验证了。

```
代码（Python）：     稳定、可验证、不依赖上下文记忆、跑在 harness 进程里
技能（Skill .md）：  灵活、可修改、但脆弱、依赖 Claude 在正确时机记得调用
```

核心阻断、核心验证、核心意图匹配——全部是代码。技能只用于需要灵活性的编排层（新闻工作流的步骤顺序、领域偏好、推送格式）。

---

## 代价

每个设计选择都有代价。以下是已知的：

- **只阻断不教育** → Claude 不知道为什么被阻断，可能在下个 session 再次尝试同一路径
- **闭环依赖 session 频率** → 连续几天不开 Claude Code，衰减曲线可能过度惩罚
- **Guardian 独立不通知** → 标记了虚警，但约束缓存可能已被其他路径更新
- **用户纠正权重压倒一切** → 一次情绪化的"不对"可能过度修正合理权重
- **代码层优先** → 改阻断规则需要改 Python，不是改一行 prompt
- **确定性规则缺乏弹性** → 关键词匹配会漏掉同义表达，正则匹配有边界 case

这些不是 bug，是取舍。知道代价并接受它，比假装没有代价更可信。

---

## 运行

```powershell
python D:\Claude\harness\harness_guardian.py status    # 系统状态
python D:\Claude\harness\harness_daemon.py observe      # 手动触发 session 分析
python D:\Claude\harness\harness_daemon.py review       # 审查待激活技能
python -m harness.news_agent --step all --date 2026-05-31   # 新闻工作流
```

配置：`harness/harness_config.yaml`

---

## 目录

```
D:\Claude\harness/
│
├── 编排层 ─────────────────────────────────────────────────────
│   ├── harness_daemon.py (3940L)   守护进程：observe / inject / review / status / cleanup
│   ├── harness_guardian.py (380L)  独立守护：每60s验证信念，不依赖任何 harness 模块
│   └── hooks.py (528L)             实时 hook：PreToolUse 硬阻断 + PostToolUse 违规追踪
│
├── 感知层 (Perception) ────────────────────────────────────────
│   ├── observer.py (837L)          会话分析：信号检测 → ObservationReport
│   ├── prethink.py (455L)          四节点情境模型：预判 → 严重度 → 锚定 → 预算
│   ├── intent_matcher.py (318L)    关键词加权意图匹配（零 LLM）
│   ├── session_quality.py (297L)   会话质量评分（纯算术加权）
│   └── story_arc_linker.py (61L)   叙事弧链接
│
├── 认知层 (Cognition) ─────────────────────────────────────────
│   ├── self_model.py (447L)        统一自我模型（技能 + 约束 + 健康 + 预测）
│   ├── attention_fuser.py (279L)   多源注意力融合
│   ├── attention_injector.py (527L) 上下文注入 + 行预算控制
│   ├── feature_finder.py (819L)    特征发现（表面 / 结构 / 潜变量）
│   ├── feature_library.py (502L)   特征库（注册 + 匹配 + 衰减）
│   ├── coactivation_detector.py (304L) 共激活模式检测
│   ├── consistency_verifier.py (198L) 信念一致性验证
│   ├── cosine_gate.py (93L)        余弦门控（任务切换时回退关键词匹配）
│   └── competing_hypotheses.py (941L) ACH 竞争假设分析
│
├── 元认知层 (Meta-Cognition) ──────────────────────────────────
│   ├── psi_predictor.py (484L)     Ψ 目标推断（用户真正要什么）
│   ├── omega_predictor.py (447L)   Ω 信念分类（正确 / 错误 / 不确定）
│   ├── judgment_graph.py (701L)    判断图（因果链 + 概率校准）
│   ├── dcl_compressor.py (306L)    双重编码压缩（声明式 + 程序式）
│   └── icl_compressor.py (269L)    ICL 上下文压缩
│
├── 存储层 (Storage) ───────────────────────────────────────────
│   ├── indexer.py (1424L)          SQLite FTS5 全文本索引（6 表 + embedding 预留）
│   ├── encoder.py (171L)           文本嵌入（BGE / MiniLM / 随机回退）
│   ├── health_probes.py (642L)     健康探针（DB / 编码器 / 数据源 / MCP / Hook 完整性）
│   ├── seed_failures.py (164L)     失败模式种子数据（6 条已验证）
│   └── feedback_learner.py (161L)  反馈学习器
│
├── 演化层 (Evolution) ─────────────────────────────────────────
│   ├── refiner.py (476L)           LLM 驱动技能生成 + 24h 反膨出上限
│   ├── auto_maintenance.py (306L)  自动归档/批准/降级（零 LLM）
│   ├── sandbox_verifier.py (452L)  技能沙箱安全验证
│   ├── proactive_scanner.py (290L) 主动扫描：发现需进化的技能
│   ├── generate_report.py (147L)   日报生成
│   └── snippet_scorer.py (270L)    4D 片段预排序
│
├── 辅助模块 ───────────────────────────────────────────────────
│   ├── agents/
│   │   ├── skill_writer.py         子代理：ObservationReport → 技能文件
│   │   └── fragment_extractor.py   子代理：transcript → 结构化记忆
│   ├── skills/                     技能审查队列（pending）
│   ├── skills/archive/             已归档技能（15 个）
│   ├── self_model_history/         自我模型版本快照（~30 个）
│   ├── tests/                      测试套件 + fixtures
│   ├── harness_config.yaml         配置文件
│   ├── state.db                    SQLite 运行数据库
│   ├── correction_keywords.yaml    纠正关键词
│   ├── entity_aliases.json         实体别名
│   └── causal_templates.json       因果模板
│
└── 根目录 ─────────────────────────────────────────────────────
    ├── README.md                   本文件
    ├── CLAUDE.md                   项目指令（Harness Calling Protocol）
    ├── LICENSE                     MIT
    ├── install_tools.bat           工具安装脚本
    └── riddle.py                   谜题
```

### 模块统计

| 层 | 模块数 | 总行数 | LLM 依赖 |
|---|---|---|---|
| 编排 | 3 | ~4,800 | 零（全部确定性和规则驱动） |
| 感知 | 5 | ~2,050 | 零 |
| 认知 | 9 | ~4,050 | 零（核心管线）/ 边缘（ACH 可选 LLM） |
| 元认知 | 5 | ~2,200 | 零（规则矩阵 + 统计 + DB 查表） |
| 存储 | 5 | ~2,550 | 零 |
| 演化 | 6 | ~2,050 | 仅 refiner（LLM 辅助写技能文件） |
| 辅助 | 4 | ~500 | 仅 agents（LLM 辅助写内容） |
| **总计** | **44** | **~17,000** | **核心管线零 LLM** |
