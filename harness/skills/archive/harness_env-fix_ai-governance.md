```markdown
---
name: news-source-resilience
description: 当新闻工作流中国际源不可用时，优雅降级至国内替代源并标记缺失
tags: [ai-governance, news-workflow, privacy, data-compliance, data-quality-failure]
triggers:
  - news-workflow中WebFetch对Reuters/BBC/AP返回ECONNREFUSED或超时
  - ce.cn重定向循环
  - 任何预定义国际源获取失败（status≠200或网络错误）
version: 1
harness_confidence: 0.85
---

# 新闻源弹性降级策略

## 执行逻辑
### When to Use
在执行新闻日报生成工作流（尤其是 `daily-news-trigger` 流程）时，若尝试从国际或国内不可靠源获取新闻失败，则自动应用此降级策略，保证工作流不完全中断。

### Step-by-Step
1. **识别失败模式**  
   - 调用 WebFetch 或 MCP 工具后，捕获以下明确错误：  
     - `ECONNREFUSED`、`timeout`、`重定向次数过多`  
     - 状态码 4xx/5xx  
   - 若源在已知不可用清单中（如 Reuters, BBC, AP, ce.cn），立即标记为 `⚠️ PERMANENTLY_UNAVAILABLE` 并跳过重试。

2. **分类处理**  
   - **国际英文源失效**：完全依赖 World News API MCP（`get_top_news`, `search_news`）作为国际新闻唯一来源，因为它已验证可用。  
   - **国内源失效**（如 ce.cn）：切换至经测试稳定的替代源：  
     - 首选：财联社（cls.cn）  
     - 备选：华尔街见闻（wallstreetcn.com）、36氪（36kr.com）  
   - 若国内替代源也失败，采用已缓存或前一日报的重复话题提示，标记 `⚠️ 国内源全部不可用，仅摘录国际新闻`。

3. **数据去重与交叉验证**  
   - 即使部分源缺失，仍对已获取的新闻条目按标题相似度（>0.85）去重。  
   - 在日报中显式标注每条新闻的“来源评级”和缺失标志，如：  
     - `⭐ 单源（替代源）`  
     - `⚠️ Reuters/BBC/AP 不可用，国际新闻仅来自 World News API`

4. **错误日志与通知**  
   - 将降级过程写入会话日志 `news-fetch-errors.log`，包含时间戳、失败源、所采用的替代源。  
   - 若用户档案中标记了“数据质量敏感”，在日报开头增加显眼提示：“**部分国际新闻源今日不可用，本日报完整性降低**”。

### How to Verify
- 检查生成的 `daily-news-YYYY-MM-DD.md` 文件中是否存在 `⚠️` 标记。  
- 检查日志文件 `news-fetch-errors.log` 中记录了本次降级的条目。  
- 确认日报仍包含主要领域（AI/科技、国际、国内）的新闻，即便来源单一。

## 异常处理
### Edge Cases
- **所有国际+国内源失效**：回退到仅提供 World News API 获得的英文标题（中文翻译由模型完成），并在日报顶部用醒目红字标注“全部预定义新闻源不可用，当前仅包含 World News API 摘要”。  
- **World News API MCP 也未连接**：终止新闻工作流，返回用户纯文本说明“当前无法获取新闻，请稍后再试”，避免生成严重残缺的日报。

### Fallback
若上述替代源均失败，且用户坚持获取新闻，执行最后兜底：使用模型的内部知识（截止训练数据）给出当日宏观话题摘要，但务必在开头声明：“以下内容基于模型预训练知识，非实时新闻，仅供参考。”
```