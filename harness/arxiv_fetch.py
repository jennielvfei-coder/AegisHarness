"""
[DEPRECATED] 已迁移到 duonews.arxiv.fetch_arxiv()
此文件保留作为过渡，一周后（2026-06-07）删除。
"""
from duonews.arxiv import fetch_arxiv

if __name__ == "__main__":
    import sys
    print("⚠️  此脚本已废弃，请使用: python -m duonews --step arxiv")
    sys.exit(1)
