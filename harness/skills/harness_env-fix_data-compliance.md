```markdown
---
name: diagnose-missing-python-package
description: Troubleshoot skills that fail due to missing or broken Python package dependencies.
tags: [data-compliance, ai-governance, news-workflow, prethink:exploration]
triggers:
  - "skill execution fails with ModuleNotFoundError or cannot import package"
version: 1
harness_confidence: 0.85
---

# 诊断缺失的 Python 包依赖

## 执行逻辑
### When to Use
- 技能尝试导入一个 Python 包时抛出 `ModuleNotFoundError`。
- `pip show <package>` 显示未安装或导入失败。
- 需要快速验证环境、定位本地备份或判定安装方式。

### Step-by-Step
1. **验证可导入性**：执行 `python -c "import <package>"`，若成功则包存在，问题可能在子模块。
2. **寻找本地源代码**：根据技能目录查找同名文件夹（如 `duonews/`），使用 `Glob` 或 `Get-ChildItem`。
3. **检查 pip 安装**：运行 `pip show <package>` 或 `python -m pip show <package>`，确认是否为正式安装包。
4. **尝试作为模块运行**：若本地文件夹存在，执行 `python -m <package> --help`，捕获启动错误。
5. **查阅 git 历史**：`git log --oneline -5 -- <directory>/` 了解近期改动，判断是否应可导入。
6. **检查 `__pycache__`**：若存在，说明此前曾成功导入，环境可能已改变。
7. **验证关联依赖**：若技能用到 `state.db` 或 CLI 脚本，并行检查其存在性（如 `Test-Path`）。
8. **决策与修复**：
   - 正式包可安装：`pip install <package>`
   - 仅本地源码：调整 `sys.path` 或在技能目录内启动 Python
   - 无法修复：明确报告用户缺失，提示手工安装或建议禁用技能

### How to Verify
- 修复后重新执行 `python -c "import <package>"` 成功。
- 技能启动检查（如 `duonews` 的导入测试）不再失败。

## 异常处理
### Edge Cases
- **包名与技能名不一致**（例如技能名为 `anysearch`，实际文件是 `anysearch_cli.py`）：需阅读技能描述定位入口。
- **虚拟环境混乱**：Python 解释器可能指向错误环境，必要时使用绝对路径调用 `python`。
- **已安装但导入失败**：运行 `pip check` 排查依赖冲突。
- **本地源码缺少 `setup.py`**：需从父目录 `python -m` 启动，或添加路径。

### Fallback
- 若无法恢复包，告知用户技能依赖缺失，提供手动安装指令。
- 使用后备方法（如手动搜索）完成用户的原始任务，避免流程中断。
```