```
```markdown
---
name: config-drift-detection
description: 系统性地检测记忆声称已修复的问题与实际配置文件之间的漂移——记忆说修了但配置没改
tags: [ai-governance, data-compliance, news-workflow, prethink:exploration]
triggers:
  - session start 自检
  - 发现相同问题反复出现
  - 用户报告"已修复"的功能仍然失败
  - git diff 显示预期之外的配置缺失
version: 1
harness_confidence: 0.85
---

# 配置漂移检测

## 执行逻辑
### When to Use
- Session 开始时做自检（Harness Active Guard Layer 触发）
- 任何"已修复"的问题再次出现时
- 检查 settings.local.json / settings.json / hooks 配置是否与记忆一致
- MCP 连接失败、功能缺失等疑似配置问题的根因排除

### Step-by-Step
1. **读取记忆**：扫描 `memory/MEMORY.md`，提取所有标记为"已修复"的记忆条目
2. **提取声称**：从每个条目中解析出具体的修复动作（例如："已将 skipWebFetchPreflight 写入 settings.local.json"）
3. **读取实际配置**：打开声称修改过的配置文件，检查键是否真实存在
4. **交叉验证**：逐条对比声称 vs. 实际，标记漂移项
5. **输出差异报告**：列出所有"记忆说已修复但配置未体现"的项

### How to Verify
- 漂移项计数为 0 表示配置与记忆同步
- 若漂移项 > 0，触发修复动作（写入缺失配置）
- 修复后重新运行本检测，确认计数归零

## 异常处理
### Edge Cases
- **记忆条目未明确声明修复动作**：标记为"需澄清"，跳过自动修复
- **配置文件不存在**：视为完整漂移，从零创建
- **键存在但值不符合预期**：标记为部分漂移，提示人工审查
- **多个记忆条目声称修复同一文件**：合并去重后统一验证

### Fallback
- 若无法确定某个键的期望值，从对应记忆条目内容中推断
- 若记忆内容也不足以推断，回退到手动确认并提示用户
```
```