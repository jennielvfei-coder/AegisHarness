# Harness Calling Protocol

当用户请求匹配以下触发条件时，使用 `Skill` 工具调用对应技能。技能完整定义在 `~/.claude/skills/harness/` 中，按需读取，不预加载全文。

## 活跃技能索引

| 触发条件 | 技能名 |
|----------|--------|
| Prevent context overflow by injecting only skill triggers, loading full definiti | `harness:context-lazy-skill-injection` |
| Systematically probe all layers of a multi-component system when a user asks "is | `harness:multi-layer-system-diagnostics` |
| Use SDP framework to check and summarize all outstanding tasks (harness reviews, | `harness:summarize-outstanding-tasks` |
| 检查记忆系统（本地文件 + MCP 知识图谱）是否可用，并输出结构化概览 | `harness:memory-system-status-check` |

## 待审查技能

运行 `python D:\Claude\harness\harness_daemon.py review` 查看待审查的技能队列。
