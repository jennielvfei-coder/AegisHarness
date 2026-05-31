```markdown
---
name: harness-state-diagnosis
description: 快速诊断Harness系统运行状态与错误累积情况（基于状态数据库的标准化查询模式）
tags: [data-compliance, news-workflow, ai-governance, diagnostics, prethink:exploration]
triggers:
  - "我的harness现在运行如何了"
  - "积累了多少错误"
  - "检查harness健康状况"
  - "harness health check"
  - "harness state summary"
version: 1
harness_confidence: 0.92
---

# Harness 状态诊断

## 执行逻辑
### When to Use
当用户询问 Harness 的运行状态、错误数量、累积趋势或整体健康度时，使用此诊断模式。

### Step-by-Step
1. **定位状态数据库**  
   默认路径：`harness/state.db`（若不存在，说明 Harness 未启动或路径错误）。

2. **建立只读连接并获取表结构（首次）**  
   执行 `PRAGMA table_info(<table>)` 确认关键表存在：
   - `judgment_entries`：判决历史
   - `feature_activations`：特征激活记录
   - `signal_buffer`：信号缓冲
   - `hypotheses`：假设记录

3. **查询总体判决健康度**  
   ```sql
   SELECT COUNT(*), AVG(CAST(json_extract(payload, '$.confidence') AS REAL))
   FROM judgment_entries;
   ```
   置信度均值低于阈值（如 0.5）或数量骤降为异常信号。

4. **统计错误/失败相关信号与激活**  
   - 特征激活：
     ```sql
     SELECT COUNT(*) FROM feature_activations
     WHERE json_extract(payload, '$.type') LIKE '%error%'
        OR json_extract(payload, '$.type') LIKE '%fail%';
     ```
   - 信号缓冲：
     ```sql
     SELECT COUNT(*) FROM signal_buffer
     WHERE json_extract(payload, '$.signal_type') LIKE '%error%'
        OR json_extract(payload, '$.signal_type') LIKE '%fail%';
     ```
   这两个数字给出直接错误量级。

5. **检查活跃假设**  
   ```sql
   SELECT hypothesis_id, status, json_extract(payload, '$.description')
   FROM hypotheses;
   ```
   观察是否存在大量未解决或失败假设。

6. **获取近期判决趋势**  
   ```sql
   SELECT entry_id, json_extract(payload, '$.category'), json_extract(payload, '$.confidence')
   FROM judgment_entries
   ORDER BY entry_id DESC LIMIT 10;
   ```
   若最近条目中错误类别占比高，表明系统正在经历异常。

7. **汇总并报告**  
   给出错误总数、置信度趋势、未解决假设数量以及是否需要干预的建议。

### How to Verify
- 确认 `state.db` 文件存在且可读。
- 所有查询返回有效数值，无 SQL 错误。
- 若表不存在或为空，返回明确的“无数据”提示，而非报错。

## 异常处理
### Edge Cases
- **路径分隔符问题**：在 Windows 的使用 `python "harness/harness_daemon.py" analyze 2>&1` 等命令时，确保使用正斜杠并加双引号，避免反斜杠转义导致的 `Errno 2`。  
- **数据库锁定**：连接时使用 `mode=ro` 或先复制一份临时文件再查询。  
- **大表性能**：若数据量极大，添加 `LIMIT` 或使用聚合函数避免全扫描。  
- **空表**：明确报告“当前无记录”，避免除以零等错误。

### Fallback
若 `state.db` 不存在，且无法通过 daemon 的 `analyze` 命令获取聚合统计，则告知用户 Harness 可能未启动，并建议先检查进程状态或启动 daemon。
```