```markdown
---
name: harness-database-diagnostics
description: Directly query harness state database (state.db) to assess errors, judgment health, and hypotheses when the daemon is unavailable or insufficient.
tags: [news-workflow, data-compliance, ai-governance, prethink:exploration, harness, diagnostics, sqlite, error-tracking]
triggers:
  - User asks about harness running status, error accumulation, or "how is my harness doing?"
  - harness_daemon.py analyze fails or returns insufficient data
version: 1
harness_confidence: 0.85
---

# Harness 数据库自诊断

## 执行逻辑

### When to Use
- 需要快速了解 harness 当前错误累积、判断图谱健康度、假设（hypotheses）状态时。
- 当 `harness_daemon.py analyze` 命令执行失败（路径错误、模块丢失等）或输出不足时，直接通过原始 SQL 查询获取聚合信息。

### Step-by-Step
1. **尝试标准分析接口**（可选）  
   `python "harness/harness_daemon.py" analyze`  
   注意：Windows 下必须用正斜杠或双引号包裹路径，避免反斜杠导致 `can't open file` 错误。
   若成功且输出满足需求，则结束。

2. **连接状态数据库**  
   ```python
   import sqlite3
   db = sqlite3.connect('harness/state.db')
   ```

3. **检查判断条目（judgment_entries）**  
   - 统计总条目数与平均置信度：  
     `SELECT COUNT(*), AVG(CAST(json_extract(payload, '$.confidence') AS REAL)) FROM judgment_entries`
   - 查看最近 N 条判断（category, confidence）：  
     `SELECT entry_id, json_extract(payload, '$.category'), json_extract(payload, '$.confidence') FROM judgment_entries ORDER BY entry_id DESC LIMIT 10`
   - 若需错误计数，可扩展 `category LIKE '%error%'` 条件。

4. **检查信号缓冲（signal_buffer）中的错误/失败信号**  
   ```sql
   SELECT COUNT(*) FROM signal_buffer 
   WHERE json_extract(payload, '$.signal_type') LIKE '%error%' 
      OR json_extract(payload, '$.signal_type') LIKE '%fail%'
   ```
   此计数反映待处理的错误事件数量。

5. **检查特征激活（feature_activations）中的错误相关激活**  
   ```sql
   SELECT COUNT(*) FROM feature_activations 
   WHERE json_extract(payload, '$.type') LIKE '%error%' 
      OR json_extract(payload, '$.type') LIKE '%fail%'
   ```

6. **检查假设（hypotheses）状态**  
   ```sql
   SELECT hypothesis_id, status, json_extract(payload, '$.description') FROM hypotheses
   ```
   了解当前活跃、验证中或已拒绝的假设，辅助判断“积累了多少错误”背后的原因。

7. **（可选）检查错误信念（false_belief_log）**  
   ```sql
   SELECT COUNT(*) FROM false_belief_log
   ```
   或者 `belief_traces` 表中的错误关联痕迹，进一步验证错误累积的严重性。

8. **汇总输出**  
   将以上结果整理为人类可读的摘要：  
   - 判断条目总数、平均置信度、近期类别分布  
   - 错误/失败信号数量（待处理）  
   - 错误相关特征激活数量  
   - 假设列表及状态  
   - 错误信念条目数（如有）

### How to Verify
- 执行每一步 SQL 后打印结果，确保无异常（如 `sqlite3.OperationalError`）。
- 对比 daemon 正常时的输出，确认误差在可接受范围内。
- 若数据库缺失表，说明 harness 可能未完全初始化，应提示用户。

## 异常处理

### Edge Cases
- **数据库文件不存在**：`harness/state.db` 缺失 → 提示用户 harness 可能尚未运行，或 state.db 路径配置错误。
- **表或字段缺失**：使用 `PRAGMA table_info` 动态探查可用表，只查询存在的列。
- **JSON 解析失败**：`json_extract` 对非标准 JSON 可能返回 NULL；可在查询前检查 payload 格式，或过滤掉无效行。
- **数据库锁定**：多个实例同时访问可能导致 `database is locked`；等待 1-2 秒重试或复制一份临时数据库。

### Fallback
- 若上述查询均无法获取有意义的数据，回退到直接读取日志文件（如 `harness.log`）或询问用户 harness 的详细配置。
- 如果问题仅是需要路径修正，执行一次路径替换后重试 daemon 命令即可。
```

*Note:* This skill abstracts the diagnostic pattern observed when the user asked “我的harness现在运行如何了？积累了多少错误？” and the assistant first attempted (and failed) the daemon command, then systematically queried the SQLite database to assess errors, judgments, and hypotheses. It is reusable any time the harness daemon is unavailable or deeper insight is needed.