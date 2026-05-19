```markdown
---
name: daily-news-workflow
description: 每日新闻完整工作流（7类新闻源→去重→8段日报→Prophet信号→Obsidian vault）
tags: [ai-governance, data-compliance, news-workflow]
version: 0.3.0
auto_generated: true
harness_confidence: 0.54
---

# Daily News Workflow (新闻每日流水线)

## When to Use
当用户要求执行或调整新闻采集、汇总、生成日报、发布到 Obsidian 的完整流程时。适用于任何需要对工作流中的抓取策略、去重规则、日报段数、Prophet 信号或 Harness 钩子（Start/Stop）进行修正的场景。

## Correction (2026-05-19 session)
**错误记录**：在之前的会话中，助手错误地将一次学习/更新应用到了 `claude-for-legal-zh` 技能中的 `refiner`，而非当前新闻工作流技能。该 refiner 与法律中文处理相关，与新闻流水线无关。  
**正确做法**：所有关于新闻工作流本身的修正——包括 Harness Stop Hook 的进程清理、WebFetch 预检跳过、MCP 僵尸进程治理——必须写入本技能文件（`daily-news-workflow`）。不要触碰 `legal-zh/refiner` 或任何其他无关技能。  
**回滚指示**：如果此前不慎更新了 `claude-for-legal-zh` 的 refiner，需要单独回滚 `P₂`（即 `refiner` 节点）至上一个版本，确保法律技能不受污染。

## Updated Guidance (from session session_20260519142144)

### Harness Stop Hook 进程清理（脚本方案，推荐先实施）
在 Harness 的 Stop 钩子中添加清理脚本，每次会话结束后自动杀掉已知的 MCP 僵尸进程。该脚本应作为 `daily-news-workflow` 脚手架的一部分，位于 `hooks/stop-cleanup.sh`。  
**推荐启用原因**：快速见效，无需改动 MCP 配置；可立即降低因未释放端口导致的后续启动失败。

### MCP 进程管理（根治方案，预留设计）
方案 B：在 MCP 配置中增加进程生命周期管理（如 `process.cleanup` 或 `restartPolicy`），从源头避免僵尸进程。  
**当前阶段**：不要求立刻实现，但必须在设计上预留升级路径。技能中应记录需要监控的 MCP 服务列表（browser-use, arXiv fetcher 等），并在 `setup/mcp-config.yaml` 中保留 `cleanup` 字段（默认注释），待后续启用。

### WebFetch 预检跳过与 arXiv 拉取
- **问题**：arXiv 的 WebFetch 每次都被拦截，即使设置 `skipWebFetchPreflight: true` 仍不稳定。  
- **修正**：在新闻流程中，对于已知需要 JS 渲染或存在反爬的源，应优先使用 `browser-use` 方案，WebFetch 仅作为无拦截源（如 RSS 直链）的备选。  
- **配置更新**：`settings.local.json` 中 `skipWebFetchPreflight` 仍保留为 `true`，但工作流 Step 2（源抓取）会首先尝试 browser-use 当源标识包含 `[js-render]` 标签。

### 日报内容追加规则
- 算力网政策类新闻必须追加到当日日报（如 `2026-05-19.md`）的“政策与监管”段。  
- 去重时注意：相同政策的不同解读可能来自多源，保留首发源并合并观点至重点分析。

## How To
1. **确保回滚**（若已误改）：  
   对 `claude-for-legal-zh/refiner` 执行 `git checkout HEAD~1 -- refiner/`，仅回滚该子路径，不波及新闻技能。
2. **添加 Stop Hook 清理脚本**：  
   在本技能目录下创建 `hooks/stop-cleanup.sh`，内容为 `pkill -f "mcp-server"` 等，并在 Harness 配置的 `stop` 生命周期中引用。
3. **更新 MCP 配置预留**：  
   在 `setup/mcp-config.yaml` 中添加注释的 cleanup 块，注明“根治方案预留，勿删除”。
4. **调整抓取顺序**：  
   修改 `fetch_sources` 函数，先检测源标签，若为 `[js-render]` 则调用 browser-use，否则走 WebFetch 且附带 `skipWebFetchPreflight: true`。
5. **记录修正**：  
   每次会话结束后，若还有进程残留，检查 `hooks/stop-cleanup.sh` 是否生效；若无效，则升级为 MCP 进程管理。

## Validation
- 跑一次完整的 `daily-news` 流程，观察 Stop Hook 是否在会话结束时被触发，日志中无 MCP 进程残留。
- 检查 `claude-for-legal-zh` 的 refiner 未被修改（`git diff` 无变化）。
- arXiv 论文抓取不再尝试 WebFetch 直连，全部通过 browser-use 完成。
```