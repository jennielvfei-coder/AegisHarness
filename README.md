# 人类是立法者，Aegis 是你自己定义的规则之盾

这不是一个让 Claude 更聪明的系统。
这是一个让 Claude 可预测、可审计、可纠错的**更可信**系统。

Aegis 不是 AI 让你信任它。Aegis 是你自己定义的规则之盾。你信任的是自己的定义。

### 决策权的重新分配

| 传统 Agent 框架      | AegisHarness                         |
| ---------------- | ------------------------------------ |
| AI 生成方案 → 人审批    | AI 检测信号 → 人定义“对”与“错” → 系统将人的判断编译为硬约束 |
| 人是审批者（最后一个环节）    | 人是**立法者**（定义规则），系统是**执法者**（执行规则）     |
| AI 的自主性体现在“能做什么” | AI 的自主性体现在“能阻止什么”                    |

### 信任的锚点从“AI 的能力”转移到了“系统的确定性”

信任一个传统 Agent，是因为它“大多数时候是对的”。但“大多数时候”不是“永远”——你不知道它下一次在什么时候、在哪一步、因为什么原因犯错。

信任AegisHarness，是因为**它犯错时，你能精准定位到是哪一步的约束被突破了**。

- 是 PreThink 判错了情境？
    
- 是 Omega 漏标了失败类型？
    
- 是 consistency_verifier 没验证？
    
- 是 Attention Fuser 权重没更新？


每一步都是确定性的规则。信任不来自“总是对”，来自**错在哪一步可以被定位**。

“可审计性比能力更重要”
**不是 AI 让你信任它，而是你不需要信任 AI——你信任的是你自己的定义。**


---

## 一、与其他 Agent 框架的本质区别

|            | 其他 Agent 框架          | AegisHarness                           |
| ---------- | -------------------- | -------------------------------------- |
| **核心隐喻**   | 图书馆员 — 帮你记住更多        | 外骨骼 — 物理阻止你做出错误动作                      |
| **设计哲学**   | 加法：加一个模块来解决一个问题      | 减法：每个模块的存在意义是阻止另一个模块犯错                 |
| **LLM 角色** | 必需品 — 核心管线依赖 LLM 推理  | 可选项 — 核心管线零 LLM 调用（关键词 + DB + 正则 + 查表） |
| **信任来源**   | "我总是对"               | "错在哪一步可以被定位"                           |
| **进化方向**   | 功能堆砌（OR：再加一个 X 或者 Y） | 约束传播（AND NOT：做 X，除非 Y 说不）              |
| **知识管理**   | 囤积 — 技能越多越好          | 不养闲人 — 闲置自动降权，约束自动过期                   |
| **反馈机制**   | 正向强化                 | 用户纠正权重压倒一切（一次纠正 > 所有正向信号之和）            |

**一句话：** 其他框架追求"让 AI 更强大"。AegisHarness 追求"让 AI 更可信"。强大的系统你不知道它什么时候犯错。可信的系统，犯错可以被定位、被追溯、被阻止。

---

## 二、核心能力

### 1. PreThink 情境模型

在每次决策前，30ms 关键词扫描，判断当前 session 是 exploration / correction / recurring_failure / routine。routine 直接短路，不给后续管线增加任何开销。

### 2. 双环自进化

- **内环（操作环）**：session 间闭合，做信号检测和策略执行
- **外环（元认知环）**：跨多个 session 闭合，问“我学到的这个东西本身对不对”


### 3. 多假设竞争验证

不是“打分→排序→输出最高分”，而是“保留互不支配的假设，识别可能同时成立的竞争性解释”。False Structure Filter 排除虚假模式。这是证伪主义在代码层面的实现。

### 4. 从“事后分析”到“事前拦截”

约束注册表 + PreToolUse hook，在 Claude 伸手碰火之前直接按住它的手。不是提醒“上次这里错了”，是物理阻止你再犯同样的错。

---

## 三、反馈必须闭环

不靠人说"这个很有用"。反馈是二进制的、可观测的、自动化的：

- 失败模式在本次 session 复现？→ 关联技能降权
- 整个 session 没有失败模式复现？→ 关联技能小幅加权（正向反馈，防止"一切衰减到零"）
- 约束创建后误报了几次？→ Sandbox Verifier 在创建前评估风险，Guardian 在事后标记虚警

虚警被标记，不被删除。你能看到系统错在哪里——这正是"可信"的定义。

Guardian 是独立守护进程。它有自己的 SQLite 连接，不 import 任何 harness 模块。管线崩了，Guardian 继续每 60 秒验证一次信念。

---

## 四、代码层是信任的锚

`intent_matcher.py` 是关键词加权，零 LLM 调用。`hooks.py` 是正则匹配，不靠 Claude 记住"Bash 不能跑 PowerShell"。`harness_guardian.py` 独立验证，不靠管线"记得"该验证了。

```
代码（Python）：     稳定、可验证、不依赖上下文记忆、跑在 harness 进程里
技能（Skill .md）：  灵活、可修改、但脆弱、依赖 Claude 在正确时机记得调用
```

核心阻断、核心验证、核心意图匹配——全部是代码。技能只用于需要灵活性的编排层（新闻工作流的步骤顺序、领域偏好、推送格式）。

---

## 五、代价

每个设计选择都有代价。以下是已知的：

- **闭环依赖 session 频率** → 连续几天不开 Claude Code，衰减曲线可能过度惩罚
- **Guardian 独立不通知** → 标记了虚警，但约束缓存可能已被其他路径更新
- **用户纠正权重压倒一切** → 一次情绪化的"不对"可能过度修正合理权重
- **代码层优先** → 改阻断规则需要改 Python，不是改一行 prompt
- **确定性规则缺乏弹性** → 关键词匹配会漏掉同义表达，正则匹配有边界 case

这些是取舍。知道代价并接受它，比假装没有代价更可信。

## 六、铁律


### 一、先验证，再架构


**成功标准：** 在给出任何方案之前，核心依赖的可用性已被实际执行验证。 验证结果可复现——任何人跑同样的检查得到同样的结论。
验证方式由你选择（Bash、Read、已有测试、MCP 工具），只要结果不是假设。 通过 → 架构。 不通过 → 报"方向有根本问题"，不给方案。

### 二、产出物有预算


**成功标准：** 每个定期产出物的字数上限和读者时间预算在产出物开头可见。 超出预算 → 删最不重要的段。 连续三次超出 → 强制裁剪到预算的 70%。 同一事实在全量产出物中出现 ≤3 次，每次递增深度。 预算数字按任务性质自定。

### 三、起始挖需求，过程锚需求

**成功标准：** 执行开始前，用户真正的需求已被确认（不是用户说出的字面请求）。 执行中每个决策点通过锚定检查：不加这个用户会损失什么？24h 后还有人需要看吗？新功能试用 3 天非必填，用户没要求就不加。 加新段前先删旧段。

### 四、第一响应给诊断，不主动给方案


**成功标准：** 对任何问题的第一响应是诊断而非方案。 诊断定义根因——人类只需要验证"根因抓对了吗"这一个问题。 方案在诊断被确认后给出。
信任不来自"总是对"，来自"错在哪一步可以被定位"——是诊断偏差，还是执行偏差。 不确定时把直觉和逻辑分开陈述，让人类看到推导过程而非结论。

### 五、自解不过二，求外援

**成功标准：** 同一问题独立尝试两次未解后，第三次向人类说明当前盲区和已排除的路径，寻求外部视角。 不自洽时明确说"这里我不确定"，不硬猜。

---
## 七、架构总览

```
┌─────────────────────────────────────────────────────┐
│                  AegisHarness                       │
│                                                      │
│  Session Start (Inject Hook)                         │
│  ├── prethink        情境建模："这是什么场景？"        │
│  ├── health_probes   6探针体检 (<50ms)                │
│  ├── proactive_scanner  先发演进检查 (<200ms)          │
│  └── intent_matcher  意图匹配 → 注入上下文指针         │
│                                                      │
│  During Session (PreToolUse Hook)                     │
│  ├── cosigne_gate    任务连续性检测                    │
│  ├── attention_injector  注意力加权注入                │
│  └── constraint_registry  硬阻断已知失败调用           │
│                                                      │
│  Session End (Stop Hook)                              │
│  ├── observer        转录分析 → 结构化观察             │
│  ├── auto_maintenance  自动闭环动作                    │
│  ├── session_quality  会话质量评分                     │
│  └── refiner         生成/更新技能文件                  │
│                                                      │
│  Background (Daemon)                                  │
│  ├── harness_guardian  独立验证守护进程                │
│  ├── sandbox_verifier  部署前验证                      │
│  └── cross-session analysis  跨会话趋势聚合            │
└─────────────────────────────────────────────────────┘
```

---

## 八、九大能力域

### 8.1 调度与生命周期

| 模块 | 做什么 | 触发时机 |
|------|--------|----------|
| `harness_daemon.py` | 主入口。`observe` 分析会话 → `refine` 生成技能 → `review` 审查队列 | 手动 / 定时 |
| `hooks.py` | Hook 处理器。每个 hook <500ms，永不阻塞会话 | Claude Code Hook 事件 |
| `prethink.py` | 情境建模。结构化评估"当前是什么场景、风险等级、应该加载什么上下文" | 会话开始 + 结束 |
| `auto_maintenance.py` | 三个自动闭环动作。纯本地逻辑，零 LLM 消耗 | 会话结束 |

### 8.2 观察与诊断

| 模块 | 做什么 | 核心机制 |
|------|--------|----------|
| `observer.py` | 从 transcript 提取结构化观察 | 纯函数，无副作用 |
| `omega_predictor.py` | Omega v8：失败模式分类与升级 | 3 失败模态 × 3 历史层级 → Block / Warn / Learn |
| `psi_predictor.py` | 从 (消息→动作) 反向推断用户潜在目标 | k-NN + cosine，Top-3 竞争假设 |
| `competing_hypotheses.py` | 多假设推断引擎 | 种子→提议→测试→修正 循环 |
| `consistency_verifier.py` | 三元诊断：Psi 预测 vs Omega 追踪 vs 实际行为 | 最近邻匹配历史成功会话 |
| `session_quality.py` | 会话质量评分 | 4 个可观测信号，用户纠正权重最高 |

### 8.3 上下文与注意力管理

| 模块 | 做什么 | 核心机制 |
|------|--------|----------|
| `cosine_gate.py` | 任务连续性检测 | 当前消息嵌入 vs 上次融合向量 → 切换则降级 |
| `attention_fuser.py` | 多源嵌入加权融合 | softmax + SGD 权重 |
| `attention_injector.py` | 3 层注意力池化注入 | 基于 InterpAgent 论文 |
| `icl_compressor.py` | 信息压缩 | ~20 活跃假设 → 3 层（Must Watch / Observe / Reference） |
| `dcl_compressor.py` | 决策压缩 | ICL 摘要 → 1-3 张判断卡片（变了什么？所以呢？怎么办？） |
| `encoder.py` | 嵌入计算 | 懒加载单例 + 哈希磁盘缓存 |
| `snippet_scorer.py` | 片段预排序 | 4D 评分（领域匹配、实体新鲜度、语义相关性、来源权威度） |

### 8.4 特征与知识提取

| 模块 | 做什么 | 核心机制 |
|------|--------|----------|
| `feature_finder.py` | 从片段中自动发现异常特征 | 基于 InterpAgent 异常检测管道 |
| `feature_library.py` | 结构化特征库 | 37 个特征条目 + 384 维嵌入 + 实体组合匹配 |
| `coactivation_detector.py` | 特征共现模式追踪 | 时间序列共激活检测 |
| `intent_matcher.py` | 用户意图匹配 | 加权特征词打分，亚毫秒级，零 LLM |
| `judgment_graph.py` | 判断图谱 | 从日报提取假设/矛盾/预测，跨日实体链接 |
| `story_arc_linker.py` | 故事弧线链接 | cosine 查近 7 天指纹 → 跨日语义关联 |

### 8.5 反馈闭环

| 模块 | 做什么 | 核心机制 |
|------|--------|----------|
| `feedback_learner.py` | 从用户反馈中学习 | 读取 signal_buffer → 匹配实体 → 更新权重（零 LLM） |
| `search_feedback.py` | 搜索质量反馈 | 自适应搜索词调优 |

### 8.6 健康与守护

| 模块 | 做什么 | 核心机制 |
|------|--------|----------|
| `harness_guardian.py` | 独立验证守护进程 | 不依赖任何 harness 模块，监控 belief_traces，管道崩溃也能存活 |
| `health_probes.py` | 6 个探针持续监控 | 每个 <50ms，零 LLM，会话边界触发 + 自动回滚 |
| `proactive_scanner.py` | 先发演进检查 | 4 个零 LLM 检查，<200ms 硬超时，最多 3 条告警 |
| `sandbox_verifier.py` | 部署前验证 | 重放历史 tool_call_log，<100ms，零 LLM |
| `mcp_wrapper.py` | MCP 进程保活 | Windows Job Objects，父进程退出时子进程必死 |
| `seed_failures.py` | 失败模式种子 | 将已知失败模式写入数据库，INSERT OR IGNORE 去重 |

### 8.7 自我建模

| 模块 | 做什么 | 核心机制 |
|------|--------|----------|
| `self_model.py` | 系统统一自我表征 | 替代 5 个碎片化注入调用，单一结构化对象 |
| `self_model.json` | 当前自模型快照 | 系统状态的结构化 JSON |

### 8.8 技能与知识管理

| 模块 | 做什么 | 核心机制 |
|------|--------|----------|
| `refiner.py` | 从观察生成技能文件 | LLM 驱动，结构化 skill 模板 |
| `indexer.py` | 全文索引 | SQLite FTS5，WAL 模式，支持片段/技能/演化日志 |
| `agents/fragment_extractor.py` | 片段提取子代理 | 从 transcript 中提取可复用片段 |
| `agents/skill_writer.py` | 技能撰寫子代理 | 将观察转化为可执行技能 |
| `skills/` | 23 个技能文件 | 4 个活跃 + 19 个归档，覆盖 env-fix / mental-model / task-workflow |

### 8.9 配置与数据

| 文件 | 内容 |
|------|------|
| `harness_config.yaml` | 主配置：特征权重、阈值、注入策略 |
| `correction_keywords.yaml` | 纠正关键词：用户反馈触发词映射 |
| `entity_aliases.json` | 实体别名：中英文/简称/全称映射 |
| `causal_templates.json` | 因果模板：用于竞争假设推理 |

---

## 九、设计原则

1. **零 LLM 优先。** 所有高频操作（健康检查、意图匹配、守护验证）都是纯 SQL + 本地计算，不消耗 token
2. **永不阻塞。** Hook 处理 <500ms，探针 <50ms，扫描 <200ms。宁可降级不阻塞会话
3. **独立守护。** Guardian 不依赖任何业务模块。管道崩溃不影响验证
4. **外骨骼式注入。** Harness 不替代 Claude，只在 Hook 点注入结构化信号
5. **反馈闭环。** 每次会话结束自动分析、评分、提炼、回写——越用越准

## 十、彩蛋

riddle.py
● 一个自修改的哲学谜题程序。每次运行都会：
  1. 读取自己的源代码
  2. 随机变异自身 — 打乱 VOICES 数组、微调 ENTROPY 值、插入装饰符号
  3. 覆盖源文件（运行完就不再是原来的文件）
  4. 打印 3 句随机的 cryptic 独白
  核心隐喻："每一次观察都在改变被观察之物" — 这也是 Harness observer 模块的设计哲学。
  
## 十一、目录

```
  D:\Claude\
  ├── CLAUDE.md
  ├── .gitignore
  ├── LICENSE
  ├── install_tools.bat
  ├── riddle.py
  └── harness/
      ├── harness_daemon.py      # 主调度器
      ├── observer.py            # 会话观察
      ├── hooks.py               # Hook 处理
      ├── prethink.py            # 前置风险评估
      ├── intent_matcher.py      # 意图匹配
      ├── harness_guardian.py    # 配置守护
      ├── auto_maintenance.py    # 自动维护
      ├── competing_hypotheses.py
      ├── consistency_verifier.py
      ├── omega_predictor.py
      ├── psi_predictor.py
      ├── ... (+15个分析/注意力/质量模块)
      ├── self_model.py/json     # 元认知模型
      ├── self_model_history/    # 29个快照
      ├── skills/                # 技能文件 (~22个)
      ├── agents/                # fragment_extractor, skill_writer
      ├── tests/                 # 测试 + fixtures
      └── anysearch_ingest.py, search_feedback.py  # 通用搜索基础
```
