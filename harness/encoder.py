"""Encoder — lightweight embedding wrapper with hash-based disk cache.

Design:
- Lazy-singleton: model loads once, shared across all callers.
- Cache-first: check embedding_cache table before computing.
- Graceful degradation: if sentence-transformers fails (e.g. China network),
  falls back to deterministic random projection seeded by content hash.

Usage:
    from encoder import get_encoder, encode_cached

    enc = get_encoder()
    vec = enc("some text")  # => list[float] of 384 dims
    vec = encode_cached("text", "user_msg", "abc123", db)
"""

from __future__ import annotations

import hashlib
import json
import random
import struct
from pathlib import Path
from typing import Callable, Optional

import numpy as np

DB_PATH = Path(__file__).resolve().parent / "state.db"

_encoder: Optional[Callable[[str], list[float]]] = None
_model_available: bool | None = None
_embedding_dim: int = 384  # Updated to 512 when BGE model loads


def _get_bge_model():
    """Try loading BGE-small-zh-v1.5 from modelscope cache. Returns model or None."""
    from pathlib import Path
    cache = Path.home() / ".cache" / "modelscope" / "hub" / "models" / "BAAI"
    if cache.exists():
        for d in cache.iterdir():
            if d.is_dir() and "bge-small-zh" in d.name:
                try:
                    from sentence_transformers import SentenceTransformer
                    return SentenceTransformer(str(d), device="cpu")
                except Exception:
                    pass
    # Try downloading via modelscope
    try:
        from modelscope.hub.snapshot_download import snapshot_download
        from sentence_transformers import SentenceTransformer
        model_dir = snapshot_download("BAAI/bge-small-zh-v1.5")
        return SentenceTransformer(model_dir, device="cpu")
    except Exception:
        pass
    return None


def _random_projection(text: str, dim: int = 384, seed: int | None = None) -> list[float]:
    """Deterministic pseudo-embedding from text hash. Not semantic, but pipeline-safe."""
    if seed is None:
        seed = int(hashlib.sha256(text.encode()).hexdigest()[:16], 16) % (2**31)
    rng = np.random.RandomState(seed)
    vec = rng.randn(dim).astype(np.float32)
    vec = vec / (np.linalg.norm(vec) + 1e-8)
    return vec.tolist()


def get_encoder() -> Callable[[str], list[float]]:
    """Return lazy-singleton encoder. Priority: BGE-small-zh > all-MiniLM > random."""
    global _encoder, _model_available, _embedding_dim

    if _encoder is not None:
        return _encoder

    # 1. Try Chinese-optimized BGE-small-zh (modelscope)
    model = _get_bge_model()
    if model is not None:
        _model_available = True
        _embedding_dim = 512
        _encoder = lambda text: model.encode(text, normalize_embeddings=True).tolist()
        return _encoder

    # 2. Fall back to English all-MiniLM-L6-v2
    if _model_available is None:
        try:
            from sentence_transformers import SentenceTransformer
            model = SentenceTransformer("all-MiniLM-L6-v2")
            _model_available = True
            _embedding_dim = 384
            _encoder = lambda text: model.encode(text, normalize_embeddings=True).tolist()
            return _encoder
        except Exception:
            _model_available = False

    # 3. Final fallback: random projection
    _encoder = lambda text: _random_projection(text)
    return _encoder


def encode_cached(
    text: str,
    source_type: str,
    content_hash: str | None = None,
    db: Optional[object] = None,
) -> list[float]:
    """Encode text, checking embedding_cache table first.

    Args:
        text: Text to encode.
        source_type: Category key ('user_msg', 'claude_behavior', 'session_tags',
                     'history_summary', 'memory_entries').
        content_hash: Pre-computed hash. Computed from text if omitted.
        db: HarnessDB instance. If None, cache is skipped (compute only).

    Returns:
        384-dim embedding vector as list[float].
    """
    if content_hash is None:
        content_hash = hashlib.sha256(text.encode()).hexdigest()

    # Check cache
    if db is not None:
        cached = db.get_embedding_cache(source_type, content_hash)
        if cached is not None:
            return cached

    # Compute
    enc = get_encoder()
    vec = enc(text[:8000])  # Truncate very long texts

    # Store cache
    if db is not None:
        db.set_embedding_cache(source_type, content_hash, vec)

    return vec


def compute_source_embeddings(
    session_data: dict,
    db: Optional[object] = None,
) -> dict[str, list[float]]:
    """Compute embeddings for all five information sources.

    Args:
        session_data: Dict with keys: user_msg, claude_behavior, session_tags,
                      history_summary, memory_entries.
        db: Optional HarnessDB for cache lookup.

    Returns:
        Dict mapping source_type -> 384-dim embedding.
    """
    sources = {}
    for key in ("user_msg", "claude_behavior", "session_tags", "history_summary", "memory_entries"):
        text = session_data.get(key, "")
        if not text:
            text = " "
        content_hash = hashlib.sha256(text.encode()).hexdigest()
        sources[key] = encode_cached(text, key, content_hash, db)

    # Pad all embeddings to uniform dimension (cache may mix 384d MiniLM + 512d BGE)
    max_dim = max(len(v) for v in sources.values()) if sources else 384
    for key in sources:
        if len(sources[key]) < max_dim:
            sources[key] = sources[key] + [0.0] * (max_dim - len(sources[key]))

    return sources


def text_hash(text: str) -> str:
    """Shortcut: SHA-256 hex digest of text."""
    return hashlib.sha256(text.encode()).hexdigest()
