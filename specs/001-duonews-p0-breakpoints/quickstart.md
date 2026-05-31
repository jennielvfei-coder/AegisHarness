# Quickstart: DuoNews P0 核心断点修复

**Created**: 2026-05-31

## 改了什么

三个核心断点修复，将 DuoNews 管线从"半自动"变为"端到端自动化"：

1. **调度修复** — 新增 `report_write` 步骤，日报自动生成不再依赖人工
2. **表格桥接** — `cross_day` 结果入库，`preprocess` 输出表格格式，步骤间结构化传数据
3. **历史判断注入** — 孤儿函数 `extract_judgment_baseline()` 接入管线，昨日判断成为今日输入

## 使用方式

```powershell
# 一键跑完整条管线（从数据拉取到日报落盘到飞书推送）
python -m duonews --step all --date 2026-06-01

# 单步调试
python -m duonews --step report_write --date 2026-06-01

# 查看管线执行状态
cat duonews/.pipeline_state.json
```

## 新增/修改文件

```
duonews/
├── __main__.py              # [修改] PIPELINE_ORDER 新增 report_write
├── __init__.py              # [修改] extract_judgment_baseline 返回结构化字段
├── preprocess.py            # [修改] 调用 find_recent_report + 输出表格格式
├── report_writer.py         # [新增] 日报自动生成模块
└── .pipeline_state.json     # [新增] 运行时生成，管线状态追踪

harness/
├── indexer.py               # [修改] 新增 cross_day_discoveries 表
└── feature_finder.py        # [修改] find_features 接受 attention_entities
```

## 验证清单

- [ ] `--step all` 一次性跑通全部步骤
- [ ] Obsidian vault 日报 7 段完整
- [ ] 日报"语义嗅探"段包含 cross_day 发现
- [ ] 日报"今日反馈"段包含昨日判断对照
- [ ] 日报速览表使用 markdown table 格式
- [ ] Prophet 信号标注了"观察中/待验证"状态
- [ ] 任意步骤失败不导致管线崩溃
- [ ] `.pipeline_state.json` 记录了每步执行状态
