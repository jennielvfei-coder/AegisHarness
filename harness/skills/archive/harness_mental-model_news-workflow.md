```markdown
---
name: news-workflow-failure-handling
description: 新闻工作流的 WebFetch 失败处理与 Harness 停止钩子僵尸进程清理
tags: [news-workflow, ai-governance, data-compliance]
triggers:
  - 新闻工作流中 WebFetch 持续返回空或超时
  - 会话结束后 stop hooks 执行时间过长（>30s）
  - arXiv 论文抓取全部失败
version: 2
harness_confidence: 0.54
---

# 新闻工作流故障处理

## 执行逻辑

### When to Use
- 每日新闻工作流运行时，WebFetch 对特定源（arXiv、部分新闻站）持续返回空结果
- 会话结束后 Harness stop hooks 挂起超过 30 秒
- `ps aux | grep mcp` 发现残留的 MCP 僵尸进程

### 核心修正（用户纠正）

**修正 1：arXiv WebFetch 全部失败 → 优先 skipWebFetchPreflight**

原流程对所有源统一使用 WebFetch，但 arXiv 的 WebFetch 每次都失败。用户明确指出应优先使用 `skipWebFetchPreflight: true`（已在 settings.local.json 中配置），对已知拦截源直接跳过 preflight，若仍失败则标记为该源不可用，当日不再重试。

**修正 2：Stop hooks 执行时间过长 → 需要进程清理**

用户纠正：stop hooks 长时间运行的根本原因是 MCP 子进程未正确回收。需要在 Harness Stop Hook 中植入清理脚本。

### Step-by-Step

#### A. WebFetch 逐源降级策略

1. **检查源类型**：区分三类源
   - 聚合 API（World News API）→ 正常调用，不受 WebFetch 影响
   - 直接抓取源 → 使用 WebFetch + `skipWebFetchPreflight: true`
   - JS 渲染备用 → 仅在前两者均失败时启用 browser-use

2. **arXiv 特殊处理**（用户纠正）：
   - arXiv **必须优先使用** `skipWebFetchPreflight: true`
   - 若仍然失败 → 标记 `arxiv_unavailable: true`
   - 当日日报中 arXiv 段标注「arXiv API 不可用，论文解读跳过」
   - **不要反复重试** arXiv WebFetch——一次失败即放弃当日该源

3. **全局 WebFetch 拦截判断**：
   - 若连续 3 个不同域名的 WebFetch 均失败 → 判定为全局拦截
   - 后续源全部跳过 WebFetch，直接使用 API 聚合源兜底
   - 日报中标注「WebFetch 全局不可用，部分源缺失」

#### B. Harness Stop Hook 进程清理（方案 A + B 结合）

**方案 A：脚本清理（快速见效，已采纳）**

在 Harness 的 Stop hook 配置中增加清理脚本：

```bash
#!/bin/bash
# 杀掉所有 MCP 相关僵尸进程（在 stop hook 触发时执行）
MCP_PIDS=$(ps aux | grep -E '(mcp|browser-use|playwright)' | grep -v grep | awk '{print $2}')
for pid in $MCP_PIDS; do
    timeout 5 kill -TERM $pid || kill -9 $pid 2>/dev/null
done
```

- 放置于 `~/.claude/hooks/stop-cleanup.sh`
- 在 Harness 配置中引用：`stop_hooks: ["bash ~/.claude/hooks/stop-cleanup.sh"]`

**方案 B：MCP 配置进程管理（根治，预留升级路径）**

在 `mcp.json` 中为每个 MCP server 增加进程生命周期配置：

```json
{
  "mcpServers": {
    "browser-use": {
      "command": "npx",
      "args": ["@anthropic/mcp-browser-use"],
      "processManagement": {
        "idleTimeout": 300,
        "terminationSignal": "SIGTERM",
        "autoRestart": false
      }
    }
  }
}
```

> **用户决策**：方案 A 和 B 一起做——A 立即生效止损，B 从源头根治。当前 Phase 1 实施 A，Phase 2 在 MCP 配置升级时实施 B。

### How to Verify

**WebFetch 降级验证**：
- 运行日报生成时观察日志，arXiv 源应显示 `skipWebFetchPreflight: true`
- 若 arXiv 仍失败，日报中 arXiv 段应有不可用标注
- 全局拦截时不应有连续 5+ 个 WebFetch 超时日志

**Stop hook 验证**：
- 会话结束后 `ps aux | grep mcp` 应无残留进程
- stop hook 执行时间应 < 10 秒（原来 > 30 秒）
- 下次会话启动时无端口占用冲突

## 异常处理

### Edge Cases
- **部分源可抓、部分不可抓**：按源单独标记，不全局降级
- **skipWebFetchPreflight 也失败**：直接跳过该源，不启用 browser-use（成本太高）
- **stop hook 清理脚本本身卡死**：外层加 `timeout 15` 保护

### Fallback
- WebFetch 全局不可用时：日报仅包含 World News API 聚合源，标注「精简版」
- arXiv 不可用时：跳过论文解读段，8 段日报变为 7 段
- 僵尸进程清理失败：下次会话启动时在 init hook 中追加二次清理

## Updated Guidance

**用户明确纠正**：
1. arXiv WebFetch 每次都失败 → 不要继续用默认方式调 WebFetch，优先 skipWebFetchPreflight
2. 如果 WebFetch 全部被拦截 → 后续源都不再调用 WebFetch，避免浪费时间
3. Stop hooks 长时间运行不是正常现象 → 需要主动清理 MCP 僵尸进程
4. 方案 A（脚本清理）和方案 B（MCP 进程管理）不是二选一 → 两者一起做，A 先落地、B 预留升级路径
```