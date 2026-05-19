"""
Fetch detail page content for ALL 36 candidate positions,
then match against the resume profile.
"""
import asyncio
import json
import re
from pathlib import Path
from playwright.async_api import async_playwright

OUTPUT_DIR = Path(r"D:\Claude\output")

with open(OUTPUT_DIR / 'westlake_all_jobs.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

jobs_2026 = data['jobs_2026']


async def fetch_detail(page, job):
    """Fetch a single job detail page and extract key info."""
    try:
        await page.goto(job['url'], wait_until='domcontentloaded', timeout=30000)
        await page.wait_for_timeout(2000)

        # Extract main content
        content = await page.evaluate("""
            () => {
                // Try to get mainStr (TRS CMS pattern)
                const scripts = document.querySelectorAll('script');
                for (const s of scripts) {
                    const text = s.textContent || '';
                    const match = text.match(/mainStr\\s*:\\s*"([^"]+)"/);
                    if (match) return {type: 'mainStr', content: match[1].replace(/\\\\"/g, '"').replace(/\\\\n/g, '\\n')};
                }

                // Fallback: get all text from the page body
                const body = document.body?.innerText || '';
                // Try to find specific content areas
                const mainArea = document.querySelector('.view, .TRS_UEDITOR, article, .content, [class*="detail"], [class*="content"]');
                if (mainArea) return {type: 'content_area', content: mainArea.innerText};
                return {type: 'body', content: body};
            }
        """)

        # Clean HTML from content
        raw = content.get('content', '')
        # Basic HTML tag removal
        cleaned = re.sub(r'<[^>]+>', '', raw)
        cleaned = re.sub(r'&nbsp;', ' ', cleaned)
        cleaned = re.sub(r'&lt;', '<', cleaned)
        cleaned = re.sub(r'&gt;', '>', cleaned)
        cleaned = re.sub(r'&amp;', '&', cleaned)
        cleaned = re.sub(r'\\n', '\n', cleaned)
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)

        # Extract department
        department = await page.evaluate("""
            () => {
                const deptEl = document.querySelector('[class*="depart"], [class*="dept"], [class*="college"]');
                return deptEl ? deptEl.textContent.trim() : '';
            }
        """)

        return {
            'title': job['title'],
            'url': job['url'],
            'department': department,
            'content': cleaned[:5000],  # First 5000 chars
            'content_len': len(cleaned),
        }
    except Exception as e:
        return {'title': job['title'], 'url': job['url'], 'error': str(e)}


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        )
        page = await context.new_page()

        details = []
        for i, job in enumerate(jobs_2026, 1):
            print(f"[{i}/{len(jobs_2026)}] Fetching: {job['title'][:80]}...")
            detail = await fetch_detail(page, job)
            details.append(detail)
            if detail.get('content'):
                print(f"  OK: {detail['content_len']} chars, dept={detail.get('department', '')}")
            else:
                print(f"  FAIL: {detail.get('error', 'unknown')}")

        # Save all details
        with open(OUTPUT_DIR / 'westlake_job_details.json', 'w', encoding='utf-8') as f:
            json.dump(details, f, ensure_ascii=False, indent=2)

        print(f"\nSaved {len(details)} job details to: {OUTPUT_DIR / 'westlake_job_details.json'}")
        await browser.close()


if __name__ == '__main__':
    asyncio.run(main())
