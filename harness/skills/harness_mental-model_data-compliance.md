```markdown
---
name: config-drift-detection
description: Detect when documented configurations/fixes in memory are missing from actual settings files
tags: [data-compliance, news-workflow, ai-governance, prethink:exploration]
triggers:
  - Session start / harness preflight
  - After any settings file modification
  - When a known fix from memory appears not to be working
version: 1
harness_confidence: 0.6
---

# Config Drift Detection

## 执行逻辑
### When to Use
执行环境配置审计时使用——当记忆库（MEMORY.md / 技能文件）中记录的修复、设置或配置可能未实际写入 `settings.local.json`、`settings.json` 或其他配置文件时。这是 "文档存在但部署缺失" 的问题模式。

### Step-by-Step
1. **提取声明**：扫描 MEMORY.md 和技能文件，提取所有对具体配置键、设置值或标志位的引用（如 `skipWebFetchPreflight`, `hooks`, `mcpServers` 等）。
2. **定位落地文件**：识别每个声明应写入的实际配置文件（`settings.local.json`, `settings.json`, `.env` 等）。
3. **交叉验证**：逐一检查每个声明是否在当前配置文件中实际存在且值正确。
4. **标记漂移**：对于声明存在但配置缺失的项，记录为 `DRIFT` 并从记忆中提取原始修复上下文。
5. **自反检查**：如果检测脚本/工具本身是新创建的，验证其自身是否已被 hooks 或自动化引用——避免工具本身也漂移（Harness V2 架构矛盾 #5：wrapper 自反）。

### How to Verify
- 每个记忆中的配置声明都存在于目标文件中 ✓
- 配置值与记忆中的预期值匹配 ✓
- 无 "被文档反复引用但从未写入" 的孤立声明 ✓

## 异常处理
### Edge Cases
- **配置键名不一致**：记忆中的键名与配置文件中的键名不匹配（如驼峰 vs 蛇形）→ 追踪实际键名并更新记忆
- **多层配置合并**：设置可能被 `defaults.json`、`settings.json` 和 `settings.local.json` 多层覆盖 → 检查最终生效值
- **MCP/Wrapper 中转**：某些配置通过 wrapper 脚本动态注入（如 `mcp_wrapper.py`）→ 直接配置漂移检查不够，需验证 wrapper 本身是否正确传递
- **Hook matcher 未注册**：修复后的 hook 存在但 trigger pattern 无法匹配实际用户输入（意图匹配鸿沟）→ 这是配置漂移的子类：功能存在但无法触发

### Fallback
如果无法自动修复漂移（权限、格式不确定等）→ 生成精确的补丁建议，包含：缺失的键路径、应设置的值、以及来源记忆条目的引用。