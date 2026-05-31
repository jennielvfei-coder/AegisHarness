```
```markdown
---
name: harness-config-audit
description: Systematic audit of Claude Code harness configuration (hooks, settings, MCP servers) after changes or as a pre-session health check
tags: [harness, configuration-audit, hooks, mcp, data-compliance, prethink:systematic]
triggers:
  - User asks to "audit harness" or "check harness config"
  - After modifying `.claude/` files, `settings.json` or MCP wrappers
  - When hooks or MCP servers stop responding
version: 1
harness_confidence: 0.85
---

# Harness Configuration Audit

## 执行逻辑
### When to Use
- 已经更改了 `.claude/` 目录下的任何文件（settings, hooks, MCP wrappers）
- 怀疑 Harness 的 hook 或 MCP server 调用失败
- 作为 SessionStart 的主动式检查（回忆 `Harness Active Guard Layer` 记忆）

### Step-by-Step
1. **回忆已知问题**：读取项目 `memory/MEMORY.md`，列出所有与 harness 配置相关的条目（如 `skipWebFetchPreflight` 缺失、`mcp_wrapper.py` 故障），把这些问题作为强制检查项。
2. **审计近期变更**：执行 `git diff --stat` 和 `git diff`，检查是否有影响 `settings.json`、`settings.local.json`、hooks 文件或 MCP 脚本的改动。任何相关的 diff 都要仔细验证。
3. **检查 settings 文件**：
   - 确认项目根目录存在 `.claude/settings.json` 和/或 `.claude/settings.local.json`。
   - 读取这些文件，验证关键字段：
     - `hooks`：是否包含 `SessionStart`、`UserPromptSubmit` 等必要事件；hook 脚本路径是否有效。
     - `mcpServers`：MCP 服务器的名称和配置是否完整，`command` 和 `args` 指向可执行脚本（注意 Windows 路径与 Python 环境）。
     - 历史补丁字段：例如 `skipWebFetchPreflight` 必须在 `settings.local.json` 中显式设置（否则会触发之前的失败）。
4. **验证 MCP 连通性**：
   - 逐个尝试 import 或调用轻量级 MCP tool（如 `world-news-api`），确认不再有 "Failed to connect" 错误。
   - 如果 MCP wrapper 存在，检查 wrapper 脚本是否有语法错误或依赖缺失。
5. **汇总报告**：列出缺失的 hooks、未设置的关键字段、连不上的 MCP 服务器，以及对应修复建议（直接修改 JSON 或更新脚本）。

### How to Verify
- 修复后，重新执行审计流程（重新读取 memory、git diff、settings文件、MCP 测试），确保所有项通过。
- 关闭并重新打开会话，观察 SessionStart 日志是否正常触发，无 MCP 错误。

## 异常处理
### Edge Cases
- **没有 MEMORY.md**：跳过已知问题回忆，但仍执行步骤 2-4。
- **无 git 仓库**：跳过 `git diff`，改用手动列出 `.claude/` 目录的文件时间戳来推断近期更改。
- **settings 文件分全局/项目两层**：检查全局 `~/.claude/settings.json` 和项目内的 `.claude/settings.json`（或 `.claude/settings.local.json`），合并分析；注意 local 覆盖 global。
- **Windows/Linux 路径差异**：验证 hook/MCP 命令路径时，确保使用正确的分隔符和可执行扩展名。

### Fallback
- 如果无法通过自动测试确认 hooks/MCP 正常工作（例如缺少测试工具），标记为手工检查项，并将观察结果追加到 `MEMORY.md` 中，以便下次自动审计时再次验证。
```