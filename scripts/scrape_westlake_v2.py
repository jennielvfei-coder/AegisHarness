"""
Scrape ALL 2026 Westlake University postdoctoral positions.
Strategy: Scroll the listing page to load all items, then extract from DOM.
Also try to access internal Vue app state for complete data.
"""
import asyncio
import json
import re
from pathlib import Path
from playwright.async_api import async_playwright

OUTPUT_DIR = Path(r"D:\Claude\output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

POSTDOC_URL = "https://www.westlake.edu.cn/careers/OpenPositions/?keywords=博士后"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        )
        page = await context.new_page()

        print("Navigating to postdoctoral page...")
        await page.goto(POSTDOC_URL, wait_until='domcontentloaded', timeout=60000)

        # Wait for SPA to render
        await page.wait_for_timeout(5000)

        # --- Strategy 1: Scroll down to trigger lazy loading ---
        print("\nScrolling to load all items...")
        prev_count = 0
        for i in range(20):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1500)

            # Count visible job cards
            count = await page.evaluate("""
                () => document.querySelectorAll('a[href*="postdoctoral_fellow"]').length
            """)
            if count != prev_count:
                print(f"  Scroll {i+1}: found {count} postdoc links")
                prev_count = count
            else:
                # Try clicking "load more" button if any
                btns = await page.evaluate("""
                    () => {
                        const btns = document.querySelectorAll('button, a, div[class*="more"], div[class*="load"]');
                        for (const b of btns) {
                            if (b.textContent.includes('更多') || b.textContent.includes('加载') || b.textContent.includes('下一页')) {
                                return b.textContent.trim();
                            }
                        }
                        return null;
                    }
                """)
                if btns:
                    print(f"  Found button: '{btns}'")
                else:
                    break  # No more items and no load button

        # --- Strategy 2: Try to read Vue/Pinia internal state ---
        print("\nTrying to access internal state...")
        internal_data = await page.evaluate("""
            () => {
                const app = document.querySelector('#app');
                // Try Vue 3 __vue_app__
                if (app && app.__vue_app__) {
                    const root = app.__vue_app__._instance;
                    if (root && root.proxy) {
                        const proxy = root.proxy;
                        const keys = Object.keys(proxy).filter(k => !k.startsWith('$') && !k.startsWith('_'));
                        return {type: 'vue3_root', keys: keys.slice(0, 30)};
                    }
                }
                // Try all elements for Vue instances
                const all = document.querySelectorAll('*');
                for (const el of all) {
                    if (el.__vueParentComponent) {
                        const comp = el.__vueParentComponent;
                        const props = comp.props;
                        if (props && (props.list || props.data || props.items || props.jobs)) {
                            return {type: 'vue_component', keys: Object.keys(props).slice(0, 30)};
                        }
                    }
                }
                return {type: 'none'};
            }
        """)
        print(f"Internal state: {json.dumps(internal_data, ensure_ascii=False)}")

        # --- Strategy 3: Extract ALL job links from fully-scrolled DOM ---
        print("\nExtracting all visible job links...")
        all_jobs = await page.evaluate("""
            () => {
                const results = [];
                const seen = new Set();

                // Method A: Direct postdoc links
                const links = document.querySelectorAll('a[href*="postdoctoral_fellow"]');
                for (const link of links) {
                    const href = link.getAttribute('href');
                    if (href && !seen.has(href)) {
                        seen.add(href);
                        const fullUrl = href.startsWith('http') ? href : 'https://www.westlake.edu.cn' + href;
                        const text = link.textContent.trim();
                        // Get parent card/container text for more context
                        let card = link.closest('div, li, tr');
                        let dept = '';
                        if (card) {
                            const deptEl = card.querySelector('[class*="depart"], [class*="college"], [class*="school"]');
                            if (deptEl) dept = deptEl.textContent.trim();
                        }
                        // Extract date from URL
                        const dateMatch = href.match(/(\\d{6})/);
                        const date = dateMatch ? dateMatch[1] : '';
                        results.push({
                            title: text,
                            url: fullUrl,
                            department: dept,
                            date_from_url: date,
                        });
                    }
                }
                return results;
            }
        """)
        print(f"Total unique postdoc jobs extracted: {len(all_jobs)}")

        # Also try to get ALL jobs (not just postdoc) for more complete view
        all_career_links = await page.evaluate("""
            () => {
                const results = [];
                const links = document.querySelectorAll('a[href*="/careers/OpenPositions/"]');
                const seen = new Set();
                for (const link of links) {
                    const href = link.getAttribute('href');
                    if (href && !seen.has(href) && !href.endsWith('/OpenPositions/')) {
                        seen.add(href);
                        const text = link.textContent.trim();
                        if (text.length > 5) {
                            results.push({title: text, url: href.startsWith('http') ? href : 'https://www.westlake.edu.cn' + href});
                        }
                    }
                }
                return results;
            }
        """)
        print(f"All career links found: {len(all_career_links)}")

        # --- Strategy 4: Check for page state / pagination info ---
        page_state = await page.evaluate("""
            () => {
                // Look for pagination elements
                const pagination = document.querySelectorAll('[class*="page"], [class*="pagin"], .el-pagination, .el-pager');
                const results = [];
                for (const el of pagination) {
                    results.push({tag: el.tagName, class: el.className, text: el.textContent.trim().substring(0, 200)});
                }

                // Look for result count
                const counts = document.querySelectorAll('[class*="total"], [class*="count"], [class*="result"]');
                for (const el of counts) {
                    const text = el.textContent.trim();
                    if (text && /\\d+/.test(text)) {
                        results.push({type: 'count', text: text.substring(0, 100)});
                    }
                }
                return results;
            }
        """)
        print(f"Pagination info: {json.dumps(page_state, ensure_ascii=False)}")

        # Save all results
        output = {
            'source_url': POSTDOC_URL,
            'total_jobs': len(all_jobs),
            'jobs': all_jobs,
            'pagination_info': page_state,
            'all_career_links': all_career_links,
        }
        with open(OUTPUT_DIR / 'westlake_all_jobs.json', 'w', encoding='utf-8') as f:
            json.dump(output, f, ensure_ascii=False, indent=2)

        print(f"\nSaved {len(all_jobs)} jobs to: {OUTPUT_DIR / 'westlake_all_jobs.json'}")

        await browser.close()


if __name__ == '__main__':
    asyncio.run(main())
