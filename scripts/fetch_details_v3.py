"""
Fetch detail page content using requests (server-rendered HTML).
Much faster and more reliable than Playwright for static pages.
"""
import json
import re
import requests
from pathlib import Path

OUTPUT_DIR = Path(r"D:\Claude\output")

with open(OUTPUT_DIR / 'westlake_all_jobs.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

jobs_2026 = data['jobs_2026']

session = requests.Session()
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
})

details = []
for i, job in enumerate(jobs_2026, 1):
    url = job['url']
    print(f"[{i}/{len(jobs_2026)}] {job['title'][:80]}...")
    try:
        resp = session.get(url, timeout=15)
        resp.encoding = 'utf-8'
        html = resp.text

        if resp.status_code != 200:
            print(f"  HTTP {resp.status_code}, len={len(html)}")
            details.append({'title': job['title'], 'url': url, 'error': f'HTTP {resp.status_code}'})
            continue

        # Extract mainStr from script tags (TRS CMS pattern)
        main_match = re.search(r'mainStr\s*:\s*"((?:[^"\\]|\\.)*)"', html)
        if not main_match:
            # Try with single quotes or different patterns
            main_match = re.search(r"mainStr\s*:\s*'((?:[^'\\]|\\.)*)'", html)

        if main_match:
            raw = main_match.group(1)
            # Unescape JSON string
            raw = raw.replace('\\"', '"').replace('\\n', '\n').replace('\\/', '/')
            raw = raw.replace('\\t', '\t').replace('\\\\', '\\')

            # Remove HTML tags for text analysis
            cleaned = re.sub(r'<[^>]+>', '', raw)
            cleaned = re.sub(r'&nbsp;', ' ', cleaned)
            cleaned = re.sub(r'&lt;', '<', cleaned)
            cleaned = re.sub(r'&gt;', '>', cleaned)
            cleaned = re.sub(r'&amp;', '&', cleaned)
            cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()

            print(f"  OK: {len(cleaned)} chars of content")
            details.append({
                'title': job['title'],
                'url': url,
                'content': cleaned,
                'content_len': len(cleaned),
            })
        else:
            # Fallback: extract from body
            body_match = re.search(r'<div class="view[^"]*"[^>]*>(.*?)</div>', html, re.DOTALL)
            if body_match:
                cleaned = re.sub(r'<[^>]+>', '', body_match.group(1))
                cleaned = re.sub(r'&nbsp;', ' ', cleaned).strip()
            else:
                # Try to get any text from the page
                text_match = re.search(r'<body[^>]*>(.*?)</body>', html, re.DOTALL)
                if text_match:
                    cleaned = re.sub(r'<[^>]+>', '', text_match.group(1))
                    cleaned = re.sub(r'\s+', ' ', cleaned).strip()[:5000]
                else:
                    cleaned = ''

            print(f"  Fallback: {len(cleaned)} chars")
            details.append({
                'title': job['title'],
                'url': url,
                'content': cleaned[:5000],
                'content_len': len(cleaned),
            })

    except Exception as e:
        print(f"  ERROR: {e}")
        details.append({'title': job['title'], 'url': url, 'error': str(e)})

with open(OUTPUT_DIR / 'westlake_job_details.json', 'w', encoding='utf-8') as f:
    json.dump(details, f, ensure_ascii=False, indent=2)

print(f"\nDone: {len(details)} details saved to {OUTPUT_DIR / 'westlake_job_details.json'}")
