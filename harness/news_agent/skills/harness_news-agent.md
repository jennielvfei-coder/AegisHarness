---
name: harness_news-agent
description: 每日新闻工作流统一编排 — 从搜索到日报到飞书推送。触发于新闻、日报、简报、快讯等请求。
confidence: 0.90
tags: [news-workflow, daily-brief, orchestration]
triggers:
  - 新闻
  - 日报
  - 简报
  - 快讯
  - 热点
  - 动态
  - 头条
  - 资讯
  - 发生了什么
  - 今日
  - 今天
  - 大事
  - 新鲜事
  - AI新闻
  - 科技新闻
version: 1
---

# 新闻工作流 Agent

统一入口：`python -m harness.news_agent --step <name> --date <date>`

## 一、前置检查

执行工作流前验证：

1. **MCP 服务器**: `claude mcp list` 确认 `world-news-api` 状态为 connected
2. **anysearch CLI**: 确认 `~/.claude/skills/anysearch/scripts/anysearch_cli.py` 存在
3. **模板文件**: Read `C:\Users\Chucky\Documents\Obsidian Vault\claude专属文件夹\news\news-template.md` (v3.2)
4. **权限**: settings.local.json 已授权 PowerShell、Write、WebFetch
5. **不可用源自动跳过**: WebSearch(DeepSeek不兼容)、Google News RSS(ECONNREFUSED)、Reuters/BBC/AP(ECONNREFUSED)、经济日报(重定向循环)。一次失败即标记，不重试。

## 二、双盲区心智模型

新闻异常检测的两类人类盲区：

**盲区1 — 跨时间量变累积**: 某话题从3次/周升到18次/30天，单日看不可见。需滚动z-score检测（>3σ 或连续5天 >2σ）。

**盲区2 — 跨域弱关联**: 三星罢工解决 vs NVIDIA财报后股价下跌，共享同一因果变量但分属不同板块。需实体共现矩阵 + Jaccard/NPMI边际检测。

注：检测器在 `feature_finder.py`，每日报告生成前触发。

## 三、6步工作流

### Step 1: 并行拉数据

```
python -m harness.news_agent --step search --date <date>
```

同步：World News API MCP — `get_top_news` (us/cn头条) + `search_news` (芯片/地缘/市场/AI营销四类关键词)

目标入库 ≥30条。

### Step 2: 预处理

```
python -m harness.news_agent --step preprocess --date <date> --top 30
```

含 arXiv 论文抓取（idempotent via content_hash dedup）。输出 `.daily_brief.md`。

### Step 3: 语义嗅探

```
python -m harness.news_agent --step cross_day --date <date>
```

跨日稀有实体关联检测。输出实体对及关联得分。

### Step 4: 写日报

Read news-template.md → 按六段结构写日报 → Obsidian vault `news/<date>.md`

### Step 5: 飞书推送

```
python -m harness.news_agent --step push --date <date>
```

禁止手写 lark-cli。脚本封装完整链路（wiki+node-create → docs+update → im+messages-send with JSON）。

### Step 6: 向量化 + 反馈

```
python -m harness.news_agent --step vectorize <news_file.md> --embed
python -m harness.news_agent --step feedback --date <date>
```

向量化日报 → feature_finder 异常检测 → search_feedback 搜索词调权。

## 四、领域优先数据源

| 领域 | 优先源 |
|------|--------|
| semiconductor/chips | 财联社电报 + TechNode |
| tech/ai | arXiv + 36氪 + TechNode |
| geopolitics | 人民网 + 中国政府网 |
| finance/economy | 财联社 + 经济日报 |
| policy | 中国政府网 + 人民网 |
| bci | arXiv BCI/神经科学 + 光明网科技版 |
| default | World News API + arXiv + 36kr + 财联社 + 人民网 |

## 五、格式铁律

- 全用 markdown table（`|列|列|`），不用 bullet list
- 来源评级：⭐⭐⭐ / ⭐⭐ / ⭐
- AI营销段不可省略（即使0条也要保留空表+注释）
- 新闻 <30条 必须补搜
- 所有深度分析标注"对菲菲"的含义

## 六、错误处理

| 步骤 | 失败降级 |
|------|----------|
| search 返回0条 | 检查 anysearch CLI + MCP，fallback 到 WebFetch 关键词搜索 |
| arxiv 无新论文 | 预期行为（周末），跳过 |
| preprocess 无数据 | 检查 state.db news_snippets 表，尝试重新 search |
| push 失败 | 检查 lark-cli 登录状态 (`lark-cli auth`)，手动发送 |
| feedback 无报告 | 写入默认次日查询（统一民生query + 默认学术topic） |

## 七、退出标准

- [ ] state.db news_snippets 表当日入库 ≥30条
- [ ] `.daily_brief.md` 已生成（≥2000字符）
- [ ] Obsidian vault `news/<date>.md` 已写入（≥6段）
- [ ] 飞书 wiki + 群聊卡片已发送
- [ ] `.constraint_cache.json` 已更新次日查询
