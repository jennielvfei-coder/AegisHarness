"""Feature Library V2.0 — structured loader, embedder, and entity combo matcher.

Parses FEATURE LIBRARY V2.0.md into 37 FeatureLibraryEntry objects,
computes 384-dim embeddings for each definition, builds an entity combo → feature
mapping, and supports checksum-based automatic reload.

Usage:
    from feature_library import load_feature_library
    entries, combo_map = load_feature_library(db)
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from _utils import cosine_sim

# ── Data model ────────────────────────────────────────────────────────────

@dataclass
class FeatureLibraryEntry:
    feature_id: str         # "A1", "C4", "D2", etc.
    layer: str              # "surface" | "structural" | "latent"
    category: str           # "时间结构异常" | "叙事结构异常" | ...
    name_cn: str            # "时序压缩" | "同步异源" | ...
    definition: str
    examples: str
    typical_implication: str  # "通常指向" field
    layer_reason: str = ""   # "层级理由" field
    embedding: list[float] | None = None

    @property
    def search_text(self) -> str:
        """Text used for embedding — definition + examples + implications."""
        return f"{self.definition} {self.examples} {self.typical_implication}"


# ── Markdown Parser ───────────────────────────────────────────────────────

# Layer detection by emoji in heading
LAYER_MAP = {
    "🟢": "surface",
    "🟡": "structural",
    "🔴": "latent",
}


def _normalize_field(text: str) -> str:
    """Clean a field value: strip markdown, collapse whitespace."""
    text = re.sub(r'\*+', '', text).strip()
    return re.sub(r'\s+', ' ', text)


def parse(feature_lib_path: Path) -> list[FeatureLibraryEntry]:
    """Parse FEATURE LIBRARY V2.0.md into structured FeatureLibraryEntry objects.

    Returns list of entries (expected 37: 9 surface + 12-13 structural + 15 latent).
    """
    if not feature_lib_path.exists():
        raise FileNotFoundError(f"Feature Library not found: {feature_lib_path}")

    text = feature_lib_path.read_text(encoding="utf-8")
    lines = text.split('\n')

    entries: list[FeatureLibraryEntry] = []
    current_layer = "unknown"
    current_category = "unknown"
    current_entry: dict[str, str] = {}
    in_entry = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Layer header: ## 🟢 第一层：... or ## 🟡 第二层：... or ## 🔴 第三层：...
        if stripped.startswith('## '):
            for emoji, layer_name in LAYER_MAP.items():
                if emoji in stripped:
                    current_layer = layer_name
                    break
            # Special: ⚪ unclassified inbox
            if '⚪' in stripped or '未分类' in stripped:
                current_layer = "unclassified"
            continue

        # Category header: ### A. 时间结构异常 (Time Structure Anomalies)
        if stripped.startswith('### '):
            # Save any in-progress entry before switching category
            if in_entry and current_entry.get("feature_id"):
                entries.append(_build_entry(current_entry, current_layer, current_category))

            current_category = _normalize_field(stripped[4:])
            # Remove English parenthetical
            current_category = re.sub(r'\s*\([^)]*\)\s*', '', current_category).strip()
            current_entry = {}
            in_entry = False
            continue

        # Feature entry: #### A1. 时序压缩 (Timing Compression)
        if stripped.startswith('#### '):
            # Save previous entry
            if in_entry and current_entry.get("feature_id"):
                entries.append(_build_entry(current_entry, current_layer, current_category))

            feature_line = _normalize_field(stripped[5:])
            # Extract feature_id (e.g., "A1") and name (e.g., "时序压缩")
            fid_match = re.match(r'([A-Z]\d+)\.?\s*(.+)', feature_line)
            if fid_match:
                current_entry = {
                    "feature_id": fid_match.group(1),
                    "name_cn": fid_match.group(2).strip(),
                }
                in_entry = True
            else:
                current_entry = {}
                in_entry = False
            continue

        # Field lines within an entry: - **层级理由**：...
        if in_entry and stripped.startswith('- **'):
            field_match = re.match(r'-\s*\*\*(.+?)\*\*[：:]\s*(.*)', stripped)
            if field_match:
                field_name = field_match.group(1).strip()
                field_value = field_match.group(2).strip()

                field_map = {
                    "层级理由": "layer_reason",
                    "定义": "definition",
                    "例": "examples",
                    "通常指向": "typical_implication",
                }
                key = field_map.get(field_name, field_name)
                current_entry[key] = field_value

    # Save the final entry
    if in_entry and current_entry.get("feature_id"):
        entries.append(_build_entry(current_entry, current_layer, current_category))

    return entries


def _build_entry(raw: dict[str, str], layer: str, category: str) -> FeatureLibraryEntry:
    """Construct a FeatureLibraryEntry from parsed fields."""
    return FeatureLibraryEntry(
        feature_id=raw.get("feature_id", "?"),
        layer=layer,
        category=category,
        name_cn=raw.get("name_cn", ""),
        definition=raw.get("definition", ""),
        examples=raw.get("examples", ""),
        typical_implication=raw.get("typical_implication", ""),
        layer_reason=raw.get("layer_reason", ""),
    )


# ── Embedding ─────────────────────────────────────────────────────────────

def embed_definitions(entries: list[FeatureLibraryEntry], db) -> list[FeatureLibraryEntry]:
    """Compute 384-dim embeddings for all entries and store in the database.

    Uses encoder.encode_cached() with source_type="feature_library".
    """
    from encoder import encode_cached

    for entry in entries:
        text = entry.search_text[:8000]
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        embedding = encode_cached(text, "feature_library", text_hash, db)
        entry.embedding = embedding

    # Store all entries to DB
    db.save_feature_library_entries([
        {
            "feature_id": e.feature_id,
            "layer": e.layer,
            "category": e.category,
            "name_cn": e.name_cn,
            "definition": e.definition,
            "examples": e.examples,
            "typical_implication": e.typical_implication,
            "embedding": e.embedding,
            "checksum": "",  # populated by check_and_reload
        }
        for e in entries
    ])

    return entries


# ── Entity Combo Map ──────────────────────────────────────────────────────

# Manually curated entity combo → feature mappings
# Built from FEATURE LIBRARY "例" and "通常指向" fields
# Format: frozenset of canonical entity names → list of (feature_id, weight)
ENTITY_COMBO_MAP: dict[frozenset[str], list[tuple[str, float]]] = {}


def build_entity_combo_map(entries: list[FeatureLibraryEntry]) -> dict[frozenset[str], list[tuple[str, float]]]:
    """Build the entity combo → feature mapping from parsed entries.

    Extracts entity mentions from each feature's 'examples' and 'typical_implication'
    fields, then builds canonical entity combos for matching.

    Falls back to hardcoded mappings for well-known patterns.
    """
    global ENTITY_COMBO_MAP
    combo_map: dict[frozenset[str], list[tuple[str, float]]] = {}

    # Hardcoded high-confidence mappings (curated from FEATURE LIBRARY content)
    hardcoded: dict[frozenset[str], list[tuple[str, float]]] = {
        frozenset({"中国", "美国", "芯片", "出口管制"}): [("C4", 0.85), ("C1", 0.70), ("A5", 0.60)],
        frozenset({"中国", "美国", "关税", "贸易"}): [("C1", 0.80), ("C4", 0.70), ("A5", 0.60)],
        frozenset({"中国", "美国", "政策", "芯片"}): [("C1", 0.75), ("C4", 0.70), ("A5", 0.65)],
        frozenset({"央行", "利率", "流动性"}): [("D2", 0.85), ("D4", 0.60)],
        frozenset({"央行", "Shibor", "地方债"}): [("D2", 0.80), ("F2", 0.65)],
        frozenset({"AI", "利润", "估值"}): [("B1", 0.75), ("D3", 0.70)],
        frozenset({"AI", "Agent", "模型"}): [("D3", 0.80), ("H2", 0.55)],
        frozenset({"AI", "监管", "法规"}): [("E1", 0.80), ("E2", 0.65), ("H1", 0.70)],
        frozenset({"AI", "安全", "对齐"}): [("H1", 0.80), ("E1", 0.55)],
        frozenset({"中国", "俄罗斯", "能源"}): [("C3", 0.60), ("A5", 0.55)],
        frozenset({"制裁", "中国", "芯片"}): [("C4", 0.85), ("C1", 0.60)],
        frozenset({"AI", "职位", "就业"}): [("D3", 0.65), ("H5", 0.70)],
        frozenset({"AI", "选举", "信息"}): [("H3", 0.80), ("G1", 0.60)],
        frozenset({"股市", "估值", "盈利"}): [("B1", 0.70), ("D1", 0.75)],
        frozenset({"IPO", "科技", "估值"}): [("B1", 0.55), ("D1", 0.60)],
    }

    combo_map.update(hardcoded)

    # Auto-extract additional combos from entry examples
    for entry in entries:
        # Look for entity keywords in examples field
        examples_text = entry.examples + " " + entry.typical_implication
        # Extract entity-like terms
        entities = set()
        for token in re.findall(r'[一-鿿\w]+', examples_text):
            if len(token) >= 2:
                entities.add(token)

        if len(entities) >= 2:
            key = frozenset(entities)
            if key not in combo_map:
                combo_map[key] = []
            existing = [fid for fid, _ in combo_map[key]]
            if entry.feature_id not in existing:
                combo_map[key].append((entry.feature_id, 0.55))

    ENTITY_COMBO_MAP = combo_map
    return combo_map


def match_entity_combos(entities: set[str], min_score: float = 0.3) -> list[tuple[str, float]]:
    """Match a set of normalized entities against the entity combo map.

    Uses max-match scoring: for each combo, score = |E ∩ C| / max(|C|, |E|, 1).
    Returns list of (feature_id, combined_score) sorted descending.

    Args:
        entities: Set of canonical entity names from news_vectorizer.
        min_score: Minimum match score to include in results.

    Returns:
        List of (feature_id, score) sorted by score descending.
    """
    if not ENTITY_COMBO_MAP:
        return []

    results: list[tuple[str, float]] = []
    entities_lower = {e.lower() for e in entities}

    for combo_set, feature_list in ENTITY_COMBO_MAP.items():
        combo_lower = {c.lower() for c in combo_set}
        intersection = entities_lower & combo_lower
        if not intersection:
            continue

        # Max-match score
        match_score = len(intersection) / max(len(combo_lower), len(entities_lower), 1)

        for feature_id, base_weight in feature_list:
            combined = match_score * base_weight
            if combined >= min_score:
                results.append((feature_id, combined))

    # Sort by score descending, deduplicate by feature_id (keep max score)
    seen: dict[str, float] = {}
    for fid, score in results:
        if fid not in seen or score > seen[fid]:
            seen[fid] = score

    return sorted([(fid, score) for fid, score in seen.items()],
                  key=lambda x: x[1], reverse=True)


# ── Feature search ────────────────────────────────────────────────────────

def search(query_embedding: list[float], db,
           top_k: int = 5, layer_filter: str | None = None,
           min_similarity: float = 0.0) -> list[tuple[str, float]]:
    """k-NN search over feature library embeddings.

    Args:
        query_embedding: 384-dim query vector.
        db: HarnessDB instance.
        top_k: Number of results.
        layer_filter: Only search entries in this layer.
        min_similarity: Minimum cosine similarity threshold.

    Returns:
        List of (feature_id, similarity_score) sorted descending.
    """
    entries = db.get_feature_library_entries(layer=layer_filter)
    if not entries:
        return []

    scored = []
    for entry in entries:
        emb = entry.get("embedding")
        if emb is None:
            continue
        sim = cosine_sim(query_embedding, emb)
        if sim >= min_similarity:
            scored.append((entry["feature_id"], sim))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]


def compute_activation_matrix(snippets: list[dict], db) -> list[list[float]]:
    """Compute N×37 snippet-to-feature activation matrix.

    For each snippet, compute cosine similarity against all 37 feature library
    entry embeddings. This matrix is the core data structure shared by:
      - feature_finder (k-NN clustering in feature space)
      - attention_injector (3-layer pooling)
      - coactivation_detector (Pearson r over activation time series)

    Args:
        snippets: List of snippet dicts (must have 'embedding' key).
        db: HarnessDB instance.

    Returns:
        N×F matrix as list of lists, where F = number of feature library entries.
    """
    entries = db.get_feature_library_entries()
    if not entries:
        return []

    feature_embeddings = {
        e["feature_id"]: e["embedding"]
        for e in entries if e.get("embedding") is not None
    }

    matrix = []
    for snippet in snippets:
        emb = snippet.get("embedding")
        if emb is None:
            continue
        row = []
        for fid, f_emb in feature_embeddings.items():
            row.append(cosine_sim(emb, f_emb))
        matrix.append(row)

    return matrix


# ── Checksum-based reload ─────────────────────────────────────────────────

def check_and_reload(feature_lib_path: Path, db) -> bool:
    """Check if FEATURE LIBRARY has changed; if so, re-parse, re-embed, rebuild.

    Steps:
      1. Compute SHA-256 of the source .md file.
      2. Compare with stored checksum in meta_store.
      3. If changed (or first run): back up current version, parse, embed, store.
      4. If unchanged: skip (no-op).

    Returns:
        True if a reload was performed, False if unchanged.
    """
    if not feature_lib_path.exists():
        return False

    current_checksum = hashlib.sha256(feature_lib_path.read_bytes()).hexdigest()
    stored_checksum = db.get_meta("feature_library_checksum")

    if current_checksum == stored_checksum:
        return False

    # Backup current version before reloading
    try:
        existing_entries = db.get_feature_library_entries()
        if existing_entries:
            db.save_feature_library_version(
                checksum=stored_checksum or "initial",
                entry_count=len(existing_entries),
                full_snapshot={"entries": existing_entries},
            )
    except Exception:
        pass  # Backup failure is non-fatal

    # Parse, embed, build
    try:
        entries = parse(feature_lib_path)
    except Exception as e:
        print(f"[feature_library] Parse failed: {e} — keeping old version")
        return False

    embed_definitions(entries, db)
    build_entity_combo_map(entries)

    db.set_meta("feature_library_checksum", current_checksum)
    return True


def load_feature_library(db, feature_lib_path: Path | None = None) -> tuple[list[FeatureLibraryEntry], dict]:
    """Main entry point: load (or reload) the feature library.

    Args:
        db: HarnessDB instance.
        feature_lib_path: Path to FEATURE LIBRARY V2.0.md. Defaults to user's vault.

    Returns:
        (entries, entity_combo_map)
    """
    if feature_lib_path is None:
        feature_lib_path = Path(
            r"C:\Users\Chucky\Documents\Obsidian Vault\claude专属文件夹\FEATURE LIBRARY V2.0.md"
        )

    check_and_reload(feature_lib_path, db)

    # Load entries from DB
    raw_entries = db.get_feature_library_entries()
    entries = [
        FeatureLibraryEntry(
            feature_id=e["feature_id"],
            layer=e["layer"],
            category=e["category"],
            name_cn=e["name_cn"],
            definition=e.get("definition", ""),
            examples=e.get("examples", ""),
            typical_implication=e.get("typical_implication", ""),
            embedding=e.get("embedding"),
        )
        for e in raw_entries
    ]

    # Ensure combo map is built
    if not ENTITY_COMBO_MAP:
        build_entity_combo_map(entries)

    return entries, ENTITY_COMBO_MAP


# ── CLI ───────────────────────────────────────────────────────────────────

def main():
    import sys
    from indexer import HarnessDB

    db = HarnessDB()
    lib_path = Path(
        r"C:\Users\Chucky\Documents\Obsidian Vault\claude专属文件夹\FEATURE LIBRARY V2.0.md"
    )

    if len(sys.argv) > 1 and sys.argv[1] == "--reload":
        db.set_meta("feature_library_checksum", "")  # Force reload
        print("Forced reload...")

    entries, combo_map = load_feature_library(db, lib_path)

    layers = {}
    for e in entries:
        layers[e.layer] = layers.get(e.layer, 0) + 1

    print(f"Loaded {len(entries)} entries: {layers}")
    print(f"Entity combo map: {len(combo_map)} patterns")
    print()

    # Test entity match
    test_entities = {"中国", "美国", "芯片", "出口管制"}
    matches = match_entity_combos(test_entities)
    print(f"Test match {test_entities}:")
    for fid, score in matches[:5]:
        entry = next((e for e in entries if e.feature_id == fid), None)
        name = entry.name_cn if entry else "?"
        print(f"  {fid} {name}: {score:.3f}")

    db.close()


if __name__ == "__main__":
    main()
