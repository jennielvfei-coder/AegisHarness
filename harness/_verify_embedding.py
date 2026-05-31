"""Verification: can all-MiniLM-L6-v2 distinguish Chinese harness memory topics?"""
import numpy as np
from sentence_transformers import SentenceTransformer

model = SentenceTransformer('all-MiniLM-L6-v2')

fragments = {
    'contract-review': [
        '合同审查流程：先检查主体信息，再审查核心条款，最后交叉验证',
        '违约责任条款需要注意违约金上限和免责范围',
        '保密协议NDA中的保密期限和范围界定',
    ],
    'data-compliance': [
        '数据合规检查清单：个保法第13条合法性基础，跨境传输评估',
        'PIA个人信息保护影响评估需要在处理前完成',
        '数据出境安全评估申报材料包括数据处理者信息、接收方信息',
    ],
    'ai-governance': [
        'AI治理框架：算法备案、模型可解释性、训练数据合规',
        '生成式AI服务需要标注AI生成内容并进行安全评估',
        '自动化决策的透明度和可解释性要求',
    ],
    'news-workflow': [
        '每日新闻工作流：World News API + arXiv 双源抓取',
        '新闻去重后用ICL compressor压缩，七类结构化日报',
        'Prophet信号检测：从新闻中提取可预测事件的概率信号',
    ],
    'harness-memory': [
        'Harness的记忆系统需要从关键词匹配升级为语义检索',
        '上下文注入的40行预算限制了记忆召回的有效载荷',
        'observer被动等待信号，应该主动探测认知盲区',
    ],
}

all_texts = []
labels = []
for topic, texts in fragments.items():
    for t in texts:
        all_texts.append(t)
        labels.append(topic)

embeddings = model.encode(all_texts, normalize_embeddings=True)

print('=== Intra-topic (同主题) similarities ===')
for topic in fragments:
    idxs = [i for i, l in enumerate(labels) if l == topic]
    sims = []
    for i in range(len(idxs)):
        for j in range(i+1, len(idxs)):
            sim = np.dot(embeddings[idxs[i]], embeddings[idxs[j]])
            sims.append(sim)
    print(f'  {topic}: mean={np.mean(sims):.3f}, range=[{min(sims):.3f}, {max(sims):.3f}]')

print()
print('=== Cross-topic (跨主题) similarities ===')
centers = {}
for topic in fragments:
    idxs = [i for i, l in enumerate(labels) if l == topic]
    centers[topic] = np.mean(embeddings[idxs], axis=0)

for t1 in fragments:
    for t2 in fragments:
        if t1 < t2:
            sim = np.dot(centers[t1], centers[t2])
            print(f'  {t1} vs {t2}: {sim:.3f}')

print()
print('=== Separation quality (gap > 0.1 = OK) ===')
for topic in fragments:
    idxs = [i for i, l in enumerate(labels) if l == topic]
    intra_sims = [np.dot(embeddings[idxs[i]], embeddings[idxs[j]]) for i in range(len(idxs)) for j in range(i+1,len(idxs))]
    intra_mean = np.mean(intra_sims)
    cross_sims = [np.dot(centers[topic], centers[t2]) for t2 in fragments if t2 != topic]
    cross_mean = np.mean(cross_sims)
    gap = intra_mean - cross_mean
    flag = 'OK' if gap > 0.1 else 'WEAK' if gap > 0.0 else 'FAIL'
    print(f'  {topic}: intra={intra_mean:.3f}, inter={cross_mean:.3f}, gap={gap:+.3f} [{flag}]')

# Critical test: can the model retrieve the right topic for a query?
print()
print('=== Retrieval test (query vs stored fragments) ===')
queries = [
    '合同里的违约金条款怎么审查',
    '个人信息出境需要什么手续',
    '算法备案的流程是什么',
    '今天的新闻有什么重要事件',
    'Harness记不住之前说过的话怎么办',
]
for q in queries:
    q_emb = model.encode([q], normalize_embeddings=True)[0]
    sims = [(np.dot(q_emb, emb), labels[i]) for i, emb in enumerate(embeddings)]
    sims.sort(reverse=True)
    top3 = sims[:3]
    print(f'  Query: "{q[:30]}..."')
    for s, label in top3:
        print(f'    {label}: sim={s:.3f}')
