"""
Fetch detail pages for relevant postdoctoral positions.
Focus on: Medical School, life sciences with potential cancer/immunology focus.
"""
import asyncio
import json
import re
from pathlib import Path
from playwright.async_api import async_playwright

OUTPUT_DIR = Path(r"D:\Claude\output")

# Load all jobs
with open(OUTPUT_DIR / 'westlake_all_jobs.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

jobs_2026 = data['jobs_2026']

print(f"Loaded {len(jobs_2026)} jobs for 2026")

# First pass: identify potentially relevant positions by title keywords
cancer_keywords = ['肿瘤', '免疫', '癌症', 'T细胞', '医学', '抗体', 'drug', '药', '治疗', 'infection',
                   'infectious', 'disease', 'cancer', 'tumor', 'immune', 'immuno', 'T cell',
                   'inflammation', 'inflammatory', 'oncology', 'vaccine', 'CAR', 'cell therapy']

potentially_relevant = []
for job in jobs_2026:
    title = job['title'].lower()
    score = 0
    for kw in cancer_keywords:
        if kw.lower() in title:
            score += 1
    if score > 0:
        job['keyword_score'] = score
        potentially_relevant.append(job)

# Also always include ALL Medical School positions
medical_jobs = [j for j in jobs_2026 if '医学院' in j['title'] or 'bshmedical' in j['url']]
for j in medical_jobs:
    if j not in potentially_relevant:
        j['keyword_score'] = 1
        potentially_relevant.append(j)

print(f"\nPotentially relevant ({len(potentially_relevant)}):")
for i, job in enumerate(potentially_relevant, 1):
    print(f"  {i}. [{job['keyword_score']}] {job['title']}")
    print(f"     {job['url']}")

# Save candidates
with open(OUTPUT_DIR / 'westlake_candidates.json', 'w', encoding='utf-8') as f:
    json.dump(potentially_relevant, f, ensure_ascii=False, indent=2)
print(f"\nSaved candidates to: {OUTPUT_DIR / 'westlake_candidates.json'}")
