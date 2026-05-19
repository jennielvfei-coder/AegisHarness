"""
Match all 36 postdoctoral positions against Du Mingxuan's resume.
Score each position and produce ranked recommendations.
"""
import json
from pathlib import Path

OUTPUT_DIR = Path(r"D:\Claude\output")

with open(OUTPUT_DIR / 'westlake_job_details.json', 'r', encoding='utf-8') as f:
    details = json.load(f)

# ============================================================
# Candidate profile (extracted from resume)
# ============================================================
CANDIDATE = {
    'name': '杜明轩',
    'degree': '厦门大学 化学生物学 博士 (2020.9-2026.6)',
    'supervisor': '张晓坤教授',
    'target': '药物研发（肿瘤免疫方向）',
    'core_research': [
        '肿瘤免疫学', 'CD8+ T细胞耗竭', 'Nur77/RXRα信号通路',
        '小分子药物研发', '线粒体功能与免疫代谢',
        '抗肿瘤免疫治疗', '靶向干预策略',
    ],
    'skills': [
        '流式细胞多色分析/分选', '原代T细胞分离与培养', 'T细胞功能评估',
        '移植瘤/转移模型构建', '体内药效评估',
        '分子克隆/WB/Co-IP/IF', '信号通路与蛋白互作分析',
        '线粒体功能检测(OCR/膜电位)', '高通量药物筛选',
    ],
    'publications': [
        'Cell Death & Disease 2026 (共一, IF=9.6)',
        'Developmental Cell 2020 (Cell子刊, IF=12.27)',
        'STAR Protocols 2021',
    ],
}

# ============================================================
# Scoring system
# ============================================================
# Keywords with weights for matching
CATEGORY_WEIGHTS = {
    # Direct match: tumor immunology / cancer immunotherapy
    '肿瘤免疫': {
        'keywords': ['肿瘤', '癌症', '免疫治疗', '免疫疗法', 'immunotherapy', 'cancer', 'tumor',
                     'oncology', '抗肿瘤', '抑瘤', '抗癌'],
        'weight': 10,
    },
    # T cell biology
    'T细胞免疫': {
        'keywords': ['T细胞', 'CD8', 'CD4', 'Tcell', 'T cell', 'T淋巴细胞', '免疫细胞',
                     'Treg', '耗竭', 'exhaustion', 'effector', '记忆T'],
        'weight': 10,
    },
    # Immune signaling / receptors
    '免疫信号': {
        'keywords': ['免疫', 'immune', 'immuno', '炎症', 'inflammation', 'inflammatory',
                     '细胞因子', 'cytokine', '趋化因子', 'chemokine', 'NF-κB', 'STAT',
                     'TCR', 'BCR', 'MHC', 'HLA', 'TLR', 'STING', 'cGAS'],
        'weight': 8,
    },
    # Drug development / small molecule
    '药物研发': {
        'keywords': ['药物', '小分子', 'drug', 'compound', 'inhibitor', '抑制剂',
                     '激动剂', 'agonist', 'antagonist', '靶向', 'targeted', '筛选',
                     'screen', '药物化学', 'medicinal', '先导', 'lead compound',
                     '药效', 'efficacy', 'PK', 'PD', 'ADME'],
        'weight': 8,
    },
    # Metabolism / mitochondria
    '代谢线粒体': {
        'keywords': ['代谢', '线粒体', 'mitochondria', 'metabolism', 'metabolic',
                     'OCR', '糖酵解', 'glycolysis', 'OXPHOS', '氧化磷酸化',
                     '脂代谢', 'lipid', 'TCA', 'NAD', 'ROS', '氧化应激'],
        'weight': 7,
    },
    # Nuclear receptor
    '核受体': {
        'keywords': ['核受体', 'nuclear receptor', 'RXR', 'Nur77', '转录因子',
                     'transcription factor', '受体', 'receptor', 'signal',
                     '信号通路', 'signaling pathway'],
        'weight': 7,
    },
    # Cell death / apoptosis
    '细胞死亡': {
        'keywords': ['凋亡', 'apoptosis', '程序性死亡', 'cell death', 'necroptosis',
                     'ferroptosis', 'pyroptosis', '自噬', 'autophagy', 'senescence'],
        'weight': 5,
    },
    # Molecular biology techniques
    '分子技术': {
        'keywords': ['分子', '蛋白', 'protein', 'WB', 'western', 'Co-IP', '免疫沉淀',
                     'ChIP', 'CRISPR', '基因编辑', 'knockout', 'knockdown',
                     'RNA-seq', '组学', 'omics', '质谱', 'mass spec'],
        'weight': 3,
    },
    # Animal models
    '动物模型': {
        'keywords': ['动物', '小鼠', 'mouse', 'murine', '模型', 'model', '体内',
                     'in vivo', '移植瘤', 'xenograft', 'PDX', '转基因', 'transgenic'],
        'weight': 3,
    },
    # Antibody / protein engineering
    '抗体蛋白': {
        'keywords': ['抗体', 'antibody', '蛋白', 'protein engineering', '重组蛋白',
                     'ADC', '双抗', 'bispecific', 'CAR-T', 'CAR', '纳米抗体'],
        'weight': 6,
    },
    # Infectious disease / microbiology (slightly related)
    '感染免疫': {
        'keywords': ['感染', 'infectious', '微生物', 'microbiome', '菌群', '病毒',
                     'virus', 'bacteria', '病原', 'pathogen', '疫苗', 'vaccine'],
        'weight': 3,
    },
}

# Negative keywords (definitely NOT relevant)
NEGATIVE_KEYWORDS = [
    '电化学', 'electrochemic', '钙钛矿', 'perovskite', '光伏', '太阳能',
    '太赫兹', 'terahertz', '光子学', 'photonics', '量子', 'quantum',
    '机器人', 'robot', '力学', 'mechanic', '流体', 'fluid',
    '催化剂', 'catalysis', '光催化', 'photocatalysis', '合成气', 'syngas',
    '半导体', 'semiconductor', '纳米材料', 'nanomaterial', 'CO2还原',
    '光合作用', 'photosynthesis', '太阳能燃料', 'solar fuel',
    '基因组编辑', '合成生物学', 'synthetic biology',
]


def score_job(job_detail):
    """Calculate relevance score for a job position."""
    title = job_detail.get('title', '')
    content = job_detail.get('content', '')
    all_text = (title + ' ' + content).lower()

    # Check negative keywords - if many hit, heavily penalize
    neg_hits = sum(1 for kw in NEGATIVE_KEYWORDS if kw.lower() in all_text)
    if neg_hits >= 2:
        return {'total_score': -100, 'categories': {}, 'neg_hits': neg_hits,
                'verdict': 'NOT RELEVANT', 'reason': f'方向完全不匹配（{neg_hits}个负向关键词）'}

    # Score each category
    scores = {}
    for cat_name, cat_info in CATEGORY_WEIGHTS.items():
        cat_score = 0
        matched_kw = []
        for kw in cat_info['keywords']:
            if kw.lower() in all_text:
                cat_score += cat_info['weight']
                matched_kw.append(kw)
        if cat_score > 0:
            scores[cat_name] = {'score': cat_score, 'matched': matched_kw}

    total = sum(s['score'] for s in scores.values())

    # Determine verdict
    if total >= 40:
        verdict = 'STRONG MATCH'
        reason = '研究方向高度吻合'
    elif total >= 20:
        verdict = 'GOOD MATCH'
        reason = '研究方向有较多重叠'
    elif total >= 10:
        verdict = 'POSSIBLE MATCH'
        reason = '部分研究方向相关'
    elif total >= 5:
        verdict = 'WEAK MATCH'
        reason = '仅少数方向相关'
    else:
        verdict = 'NOT RELEVANT'
        reason = '研究方向不匹配'

    return {
        'total_score': total,
        'categories': scores,
        'verdict': verdict,
        'reason': reason,
        'neg_hits': neg_hits if neg_hits >= 2 else 0,
    }


# Run matching
results = []
for job in details:
    if job.get('error'):
        results.append({'title': job['title'], 'url': job['url'],
                       'error': job['error'], 'total_score': -999,
                       'verdict': 'ERROR'})
        continue

    match = score_job(job)
    results.append({
        'title': job['title'],
        'url': job['url'],
        'content_preview': job.get('content', '')[:300],
        **match,
    })

# Sort by score descending
results.sort(key=lambda x: x['total_score'], reverse=True)

# ============================================================
# Print results
# ============================================================
print("=" * 80)
print("西湖大学 2026 博士后岗位 — 与杜明轩简历匹配分析")
print("=" * 80)
print(f"\n候选人背景: {CANDIDATE['degree']}")
print(f"研究方向: {', '.join(CANDIDATE['core_research'][:4])}")
print()

for i, r in enumerate(results, 1):
    if r['total_score'] <= 0:
        continue
    print(f"\n{'='*60}")
    print(f"#{i} [{r['verdict']}] Score: {r['total_score']}")
    print(f"岗位: {r['title']}")
    print(f"URL: {r['url']}")
    print(f"原因: {r['reason']}")
    if r['categories']:
        print(f"匹配详情:")
        for cat, info in sorted(r['categories'].items(), key=lambda x: x[1]['score'], reverse=True):
            print(f"  [{info['score']}分] {cat}: {', '.join(info['matched'][:5])}")
    print(f"内容摘要: {r['content_preview'][:200]}...")

# Also show non-relevant count
irrelevant = [r for r in results if r['total_score'] <= 0]
print(f"\n{'='*80}")
print(f"匹配统计:")
print(f"  STRONG MATCH (>=40): {sum(1 for r in results if r['verdict'] == 'STRONG MATCH')}")
print(f"  GOOD MATCH (>=20): {sum(1 for r in results if r['verdict'] == 'GOOD MATCH')}")
print(f"  POSSIBLE MATCH (>=10): {sum(1 for r in results if r['verdict'] == 'POSSIBLE MATCH')}")
print(f"  WEAK MATCH (>=5): {sum(1 for r in results if r['verdict'] == 'WEAK MATCH')}")
print(f"  NOT RELEVANT: {sum(1 for r in results if r['verdict'] == 'NOT RELEVANT')}")
print(f"  ERROR: {sum(1 for r in results if r['verdict'] == 'ERROR')}")

# Save detailed results
with open(OUTPUT_DIR / 'westlake_match_results.json', 'w', encoding='utf-8') as f:
    json.dump({
        'candidate': CANDIDATE,
        'results': results,
        'generated_at': '2026-05-13',
    }, f, ensure_ascii=False, indent=2)

print(f"\n详细结果已保存到: {OUTPUT_DIR / 'westlake_match_results.json'}")
