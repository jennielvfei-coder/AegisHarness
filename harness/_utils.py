"""Shared utility functions extracted from harness modules.

cosine_sim, _softmax, and cosine_knn are consolidated here so that
new modules (feature_finder, feature_library, attention_injector, etc.)
don't duplicate code from cosine_gate, attention_fuser, and psi_predictor.

Existing modules should re-import from here. The original functions remain
in place as re-exports to avoid breaking existing callers.
"""

from __future__ import annotations

import numpy as np


def cosine_sim(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors.

    Returns 0.0 if either vector has near-zero norm.
    """
    va = np.array(a, dtype=np.float32)
    vb = np.array(b, dtype=np.float32)
    norm_a = np.linalg.norm(va)
    norm_b = np.linalg.norm(vb)
    if norm_a < 1e-10 or norm_b < 1e-10:
        return 0.0
    return float(np.dot(va, vb) / (norm_a * norm_b))


def softmax_dict(d: dict[str, float]) -> dict[str, float]:
    """Softmax over dict values (numerically stable).

    Returns uniform weights if the denominator is near zero.
    """
    values = np.array(list(d.values()), dtype=np.float32)
    values = values - np.max(values)
    exp_vals = np.exp(values)
    total = float(np.sum(exp_vals))
    if total < 1e-10:
        n = len(d)
        return {k: 1.0 / n for k in d}
    return {k: float(exp_vals[i] / total) for i, k in enumerate(d)}


def softmax_list(values: list[float]) -> list[float]:
    """Softmax over a list of floats.

    Returns uniform weights if the denominator is near zero.
    """
    arr = np.array(values, dtype=np.float32)
    arr = arr - np.max(arr)
    exp_arr = np.exp(arr)
    total = float(np.sum(exp_arr))
    if total < 1e-10:
        n = len(values)
        return [1.0 / n] * n
    return [float(v / total) for v in exp_arr]


def cosine_knn(
    query_embedding: list[float],
    candidates: list[dict],
    k: int = 15,
    embedding_key: str = "embedding",
) -> list[tuple[float, dict]]:
    """k-NN search over candidates by cosine similarity.

    Args:
        query_embedding: 384-dim query vector.
        candidates: List of dicts, each containing an embedding list.
        k: Number of nearest neighbors to return.
        embedding_key: Dict key for the candidate embedding.

    Returns:
        List of (similarity_score, candidate_dict), sorted descending.
    """
    scored = []
    for cand in candidates:
        emb = cand.get(embedding_key)
        if emb is None:
            continue
        sim = cosine_sim(query_embedding, emb)
        scored.append((sim, cand))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:k]


def l2_normalize(vec: list[float]) -> list[float]:
    """L2-normalize a vector in-place (returns new list)."""
    arr = np.array(vec, dtype=np.float32)
    norm = np.linalg.norm(arr)
    if norm < 1e-10:
        return vec
    return (arr / norm).tolist()
