"""
[DEPRECATED] 已迁移到 harness.news_agent.ingest
此文件保留作为过渡，一周后（2026-06-03）删除。
"""
from harness.news_agent.ingest import AnysearchResult, ingest

if __name__ == "__main__":
    import sys
    print("⚠️  此脚本已废弃，请使用: python -m harness.news_agent --step search")
    sys.exit(1)
