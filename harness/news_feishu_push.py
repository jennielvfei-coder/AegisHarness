"""
[DEPRECATED] 已迁移到 duonews.push.push_to_feishu()
此文件保留作为过渡，一周后（2026-06-07）删除。
"""
from duonews.push import push_to_feishu

if __name__ == "__main__":
    import sys
    print("⚠️  此脚本已废弃，请使用: python -m duonews --step push")
    sys.exit(1)
