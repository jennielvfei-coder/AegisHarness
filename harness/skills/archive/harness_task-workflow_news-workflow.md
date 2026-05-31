```
```
```markdown
---
name: harness-error-diagnosis
description: 通过查询 harness 的 SQLite 状态库，快速评估错误积累与系统健康度。
tags: [news-workflow, ai-governance, data-compliance, prethink:exploration]
triggers:
  - 用户询问“harness 运行如何”“积累了多少错误”等系统状态
  - 需要审计 AI 治理流水线的故障与置信度趋势
version: 1
harness_confidence: 0.85
---

# 诊断 Harness 错误积累

## 执行逻辑
### When to Use
- 用户或系统触发对 harness 运行健康的检查。
- 希望获得错误信号数量、判断图健康度、假设验证状态的聚合视图。

### Step-by-Step
1. **首选入口**：尝试运行分析命令  
   `python harness/harness_daemon.py analyze`  
   若因路径错误或其他原因失败（退出码 2），直接进入步骤 2。

2. **直接诊断数据库**：  
   - 定位数据库 `harness/state.db`（若路径不同需替换）。  
   - 使用 Python `sqlite3` 连接，一次性或分批执行以下查询，避免锁冲突。

   a) **错误/失败信号总数**：  
      ```sql
      SELECT COUNT(*) FROM signal_buffer
       WHERE json_extract(payload, '$.signal_type') LIKE '%error%'
          OR json_extract(payload, '$.signal_type') LIKE '%fail%';
      ```

   b) **特征激活中的错误/失败次数**：  
      ```sql
      SELECT COUNT(*) FROM feature_activations
       WHERE json_extract(payload, '$.type') LIKE '%error%'
          OR json_extract(payload, '$.type') LIKE '%fail%';
      ```

   c) **判断图健康状况**：  
      ```sql
      SELECT COUNT(*) AS total_judgments,
             AVG(CAST(json_extract(payload, '$.confidence') AS REAL)) AS avg_confidence
      FROM judgment_entries;
      ```
      再取最近 10 条判断查看置信度趋势：  
      ```sql
      SELECT entry_id, json_extract(payload, '$.category'), 
             json_extract(payload, '$.confidence')
      FROM judgment_entries ORDER BY entry_id DESC LIMIT 10;
      ```

   d) **假设状态**：  
      ```sql
      SELECT hypothesis_id, status, json_extract(payload, '$.description')
      FROM hypotheses;
      ```
      标记处于 `failed`、`contradicted`、`pending` 的假设数量。

3. **输出摘要**：  
   - 错误信号总数。
   - 特征激活中错误/失败数量。
   - 判断图总条目数、平均置信度、最近判断示例。
   - 假设状态分布。
   - 若平均置信度持续低于阈值（如 0.6），标记为需要干预。

### How to Verify
- 执行查询后确认返回数字非负，且与系统近期行为吻合。
- 若 `hypotheses` 表为空但应有数据，检查上游融合层是否正常写入。

## 异常处理
### Edge Cases
- **路径分隔符问题**：Windows 下可能需用反斜杠或正斜杠，优先使用 `harness/harness_daemon.py`。
- **数据库锁定**：若其他进程正在写库，增加超时参数或重试。
- **表缺失**：若某表不存在（如旧版 harness 无 `belief_traces`），则跳过对应查询，并在结果中注明。
- **大量条目**：`judgment_entries` 或 `signal_buffer` 极大时，限制 `LIMIT` 并索引时间戳，避免全表扫描影响响应。

### Fallback
- 如果数据库完全不可访问，提示用户检查 harness 进程是否运行、磁盘空间、权限。
- 可回退到读取最近的日志文件（如 `harness/logs/`），正则匹配 `ERROR` 字样作为临时统计。
```