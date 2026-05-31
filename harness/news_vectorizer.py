"""
[DEPRECATED] 已迁移到 duonews.vectorize
此文件保留作为过渡，一周后（2026-06-07）删除。
"""
from duonews.vectorize import run_vectorizer, parse_news_file, vectorize_snippets, NewsSnippet

if __name__ == "__main__":
    import sys
    print("⚠️  此脚本已废弃，请使用: python -m duonews --step vectorize")
    sys.exit(1)
