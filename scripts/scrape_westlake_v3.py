"""
Scrape ALL 2026 Westlake University postdoctoral positions.
Strategy: Click "查看更多" to expand, then navigate pagination.
"""
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

OUTPUT_DIR = Path(r"D:\Claude\output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

POSTDOC_URL = "https://www.westlake.edu.cn/careers/OpenPositions/?keywords=博士后"


def extract_jobs_from_page(html_content, base_url="https://www.westlake.edu.cn"):
    """Extract job links from rendered HTML using regex as fallback."""
    import re
    results = []
    seen = set()
    # Match postdoc URLs
    pattern = re.compile(r'href="(/careers/OpenPositions/postdoctoral_fellow/[^"]+)"')
    title_pattern = re.compile(r'title="([^"]*)"')

    for match in pattern.finditer(html_content):
        href = match.group(1)
        if href not in seen:
            seen.add(href)
            full_url = href if href.startswith('http') else base_url + href
            results.append({'url': full_url, 'relative_url': href})

    return results


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        )
        page = await context.new_page()

        print("Navigating to postdoctoral page...")
        await page.goto(POSTDOC_URL, wait_until='networkidle', timeout=60000)
        await page.wait_for_timeout(3000)

        all_jobs = []
        all_html_jobs = []

        # Collect jobs from each page of pagination
        for page_num in range(1, 10):  # Try up to 10 pages
            print(f"\n--- Processing page {page_num} ---")

            # Wait for render
            await page.wait_for_timeout(2000)

            # Click "查看更多" repeatedly to expand all items on this page
            for i in range(10):
                try:
                    load_more = page.locator('text=查看更多').first
                    if await load_more.is_visible(timeout=1000):
                        await load_more.click()
                        await page.wait_for_timeout(1500)
                        print(f"  Clicked 'load more' #{i+1}")
                    else:
                        break
                except Exception:
                    break

            # Extract all postdoc links from current page
            jobs = await page.evaluate("""
                () => {
                    const results = [];
                    const seen = new Set();
                    const links = document.querySelectorAll('a[href*="postdoctoral_fellow"][href*="/202"]');
                    for (const link of links) {
                        const href = link.getAttribute('href');
                        if (href && !seen.has(href)) {
                            seen.add(href);
                            const fullUrl = href.startsWith('http') ? href : 'https://www.westlake.edu.cn' + href;
                            const text = link.textContent.trim();
                            // Extract date from URL
                            const dateMatch = href.match(/(\\d{6})/);
                            const date = dateMatch ? dateMatch[1] : '';
                            results.push({title: text, url: fullUrl, date_from_url: date});
                        }
                    }
                    return results;
                }
            """)
            print(f"  Found {len(jobs)} jobs on page {page_num}")

            # Also get raw HTML for regex extraction
            html = await page.content()
            html_jobs = extract_jobs_from_page(html)
            print(f"  HTML regex found {len(html_jobs)} jobs")

            all_jobs.extend(jobs)
            all_html_jobs.extend(html_jobs)

            # Try to go to next page
            try:
                next_btn = page.locator('.el-pager li.number:last-child, .btn-next:not(.disabled), li.number.active + li.number')
                # More robust: find the next page number
                next_page = await page.evaluate(f"""
                    () => {{
                        // Find active page number
                        const active = document.querySelector('.el-pager li.active, .el-pager li.number.active, .page_box .active');
                        if (active) {{
                            const currentPage = parseInt(active.textContent);
                            // Find next page number
                            const allNums = document.querySelectorAll('.el-pager li.number, .page_box [class*="num"]');
                            for (const el of allNums) {{
                                if (parseInt(el.textContent) === {page_num} + 1) {{
                                    el.click();
                                    return true;
                                }}
                            }}
                        }}
                        // Try btn-next
                        const nextBtn = document.querySelector('.btn-next:not(.disabled), .el-pagination .btn-next:not(.disabled)');
                        if (nextBtn) {{
                            nextBtn.click();
                            return true;
                        }}
                        return false;
                    }}
                """)

                if next_page:
                    print(f"  Moved to page {page_num + 1}")
                    await page.wait_for_timeout(3000)
                else:
                    print(f"  No more pages after page {page_num}")
                    break
            except Exception as e:
                print(f"  Pagination error: {e}")
                break

        # Deduplicate jobs by URL
        seen_urls = set()
        unique_jobs = []
        for job in all_jobs + all_html_jobs:
            url = job['url']
            if url not in seen_urls:
                seen_urls.add(url)
                unique_jobs.append(job)

        # Filter for 2026 only
        jobs_2026 = [j for j in unique_jobs if '/2026' in j['url']]
        print(f"\n{'='*50}")
        print(f"Total unique jobs: {len(unique_jobs)}")
        print(f"2026 jobs: {len(jobs_2026)}")

        # Save
        output = {
            'source_url': POSTDOC_URL,
            'total_unique': len(unique_jobs),
            'jobs_2026_count': len(jobs_2026),
            'all_jobs': unique_jobs,
            'jobs_2026': jobs_2026,
        }
        with open(OUTPUT_DIR / 'westlake_all_jobs.json', 'w', encoding='utf-8') as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        print(f"Saved to: {OUTPUT_DIR / 'westlake_all_jobs.json'}")

        await browser.close()


if __name__ == '__main__':
    asyncio.run(main())
