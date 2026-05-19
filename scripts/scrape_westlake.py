"""
Scrape Westlake University postdoctoral positions.
Phase 1: Find the API endpoint via network interception.
Phase 2: Extract all postdoc positions for 2026.
"""
import asyncio
import json
import re
from pathlib import Path
from playwright.async_api import async_playwright

OUTPUT_DIR = Path(r"D:\Claude\output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

BASE_URL = "https://www.westlake.edu.cn/careers/OpenPositions/"
POSTDOC_URL = f"{BASE_URL}?keywords=博士后"

# Store captured API requests
api_calls = []
job_data_from_api = []


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox']
        )
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36'
        )
        page = await context.new_page()

        # --- Phase 1: Capture network requests to find API ---
        print("=== Phase 1: Capturing API requests ===")

        async def handle_request(request):
            url = request.url
            if any(kw in url.lower() for kw in ['api', 'list', 'search', 'query', 'json', 'post', 'doc']):
                api_calls.append({
                    'url': url,
                    'method': request.method,
                    'headers': dict(request.headers),
                    'post_data': request.post_data,
                })

        async def handle_response(response):
            url = response.url
            if any(kw in url.lower() for kw in ['api', 'list', 'search', 'query', 'json', 'post', 'doc']):
                try:
                    body = await response.text()
                    if len(body) < 500000:  # Don't capture huge responses
                        api_calls.append({
                            'url': url,
                            'status': response.status,
                            'content_type': response.headers.get('content-type', ''),
                            'body_preview': body[:3000],
                            'body_len': len(body),
                        })
                except Exception:
                    pass

        page.on('request', handle_request)
        page.on('response', handle_response)

        # Navigate to postdoctoral listing
        print(f"Navigating to: {POSTDOC_URL}")
        await page.goto(POSTDOC_URL, wait_until='networkidle', timeout=60000)
        await page.wait_for_timeout(5000)  # Wait for async data loading

        # Print captured API calls
        print(f"\nCaptured {len(api_calls)} API-related requests/responses")
        api_urls = set()
        for item in api_calls:
            url = item.get('url', '')
            if url not in api_urls:
                api_urls.add(url)
                status = item.get('status', 'N/A')
                content_type = item.get('content_type', '')
                body_len = item.get('body_len', 0)
                print(f"  [{status}] {url} ({content_type}, {body_len} bytes)")
                if 'body_preview' in item and body_len > 0:
                    preview = item['body_preview'][:500]
                    if preview:
                        print(f"    Preview: {preview}")

        # Save all captured API data
        with open(OUTPUT_DIR / 'westlake_api_captures.json', 'w', encoding='utf-8') as f:
            json.dump(api_calls, f, ensure_ascii=False, indent=2, default=str)
        print(f"\nAPI captures saved to: {OUTPUT_DIR / 'westlake_api_captures.json'}")

        # --- Phase 2: Extract job listings from the page ---
        print("\n=== Phase 2: Extracting job listings ===")

        # Try to find the API endpoint for jobs from captured requests
        jobs_api_url = None
        for item in api_calls:
            url = item.get('url', '')
            if 'job' in url.lower() or 'position' in url.lower() or 'career' in url.lower():
                if 'json' in item.get('content_type', '') or item.get('body_len', 0) > 100:
                    jobs_api_url = url
                    print(f"Found potential jobs API: {url}")
                    break

        # If API found, try to fetch directly
        if jobs_api_url:
            print(f"\nUsing discovered API: {jobs_api_url}")
        else:
            print("No obvious jobs API found, extracting from DOM...")

        # Extract job links from the DOM
        job_links = await page.evaluate("""
            () => {
                const results = [];
                const links = document.querySelectorAll('a[href]');
                const seen = new Set();
                for (const link of links) {
                    const href = link.getAttribute('href');
                    const text = link.textContent.trim();
                    if (href && href.includes('postdoctoral_fellow') && href.includes('/202') && !seen.has(href)) {
                        seen.add(href);
                        const fullUrl = href.startsWith('http') ? href : 'https://www.westlake.edu.cn' + href;
                        results.push({title: text, url: fullUrl});
                    }
                }
                return results;
            }
        """)
        print(f"Found {len(job_links)} postdoc job links from DOM")

        # Also try to extract any structured data from Vue/SPA
        page_data = await page.evaluate("""
            () => {
                // Try to find Vue component data
                const results = {};
                // Check window for global data
                for (const key of Object.keys(window)) {
                    if (key.includes('job') || key.includes('career') || key.includes('position') || key === 'allJobs') {
                        try {
                            const val = window[key];
                            results[key] = typeof val === 'object' ? JSON.stringify(val).substring(0, 500) : String(val).substring(0, 500);
                        } catch(e) {}
                    }
                }
                return results;
            }
        """)
        print(f"Window data found: {list(page_data.keys())}")

        # Look for Vuex/Pinia store state
        vue_data = await page.evaluate("""
            () => {
                const app = document.querySelector('#app');
                if (app && app.__vue_app__) {
                    return 'Vue app found';
                }
                if (app && app.__vue__) {
                    return 'Vue instance found';
                }
                return 'No Vue instance found on #app';
            }
        """)
        print(f"Vue detection: {vue_data}")

        # Save raw job links
        with open(OUTPUT_DIR / 'westlake_jobs_raw.json', 'w', encoding='utf-8') as f:
            json.dump({
                'source_url': POSTDOC_URL,
                'job_count': len(job_links),
                'api_url': jobs_api_url,
                'api_captures': api_calls,
                'jobs': job_links,
                'window_data': page_data,
            }, f, ensure_ascii=False, indent=2, default=str)
        print(f"\nRaw job data saved to: {OUTPUT_DIR / 'westlake_jobs_raw.json'}")

        await browser.close()


if __name__ == '__main__':
    asyncio.run(main())
