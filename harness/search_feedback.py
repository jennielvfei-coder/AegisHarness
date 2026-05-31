"""
[DEPRECATED] 已迁移到 duonews.feedback.run_feedback_loop()
此文件保留作为过渡，一周后（2026-06-07）删除。
"""
from duonews.feedback import run_feedback_loop

if __name__ == "__main__":
    import sys
    print("⚠️  此脚本已废弃，请使用: python -m duonews --step feedback")
    sys.exit(1)
