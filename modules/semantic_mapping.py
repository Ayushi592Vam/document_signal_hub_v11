"""
modules/semantic_mapping.py

Embedding-based column-to-schema-field matching. Sits BETWEEN the
existing rule-based matcher (normalization._best_standard_name) and the
existing LLM fallback (schema_mapping.llm_map_unknown_fields()) --
catches synonym-style headers (e.g. "Loss Amt Recv'd" vs "Total Paid")
without paying for an LLM call on every single unmapped column.

In plain terms: turns each column header and each candidate field name
into a list of numbers (an "embedding") that captures its MEANING, then
measures how close those numbers are (cosine similarity, 0 to 1). Close
enough = auto-map, with no LLM call at all.
"""

import os
import json
import math
import urllib.request

_SIMILARITY_THRESHOLD = float(os.environ.get("SEMANTIC_MATCH_THRESHOLD", "0.80"))
_field_embedding_cache: dict[str, list[float]] = {}


def _get_embedding(text: str) -> list[float]:
    endpoint   = os.environ.get("OPENAI_DEPLOYMENT_ENDPOINT", "").rstrip("/")
    api_key    = os.environ.get("OPENAI_API_KEY", "")
    api_ver    = os.environ.get("OPENAI_API_VERSION", "2024-12-01-preview")
    deployment = os.environ.get("OPENAI_EMBEDDING_DEPLOYMENT_NAME", "text-embedding-3-small")
    url = f"{endpoint}/openai/deployments/{deployment}/embeddings?api-version={api_ver}"
    payload = json.dumps({"input": text}).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json", "api-key": api_key},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read().decode())
    return body["data"][0]["embedding"]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _get_field_embedding(field_name: str) -> list[float]:
    # Cached per app instance -- candidate schema fields (Claim Number,
    # Total Paid, etc.) never change mid-session, so this avoids
    # re-embedding the same ~20 field names on every unmapped column.
    if field_name not in _field_embedding_cache:
        _field_embedding_cache[field_name] = _get_embedding(field_name)
    return _field_embedding_cache[field_name]


def semantic_match_field(column_header: str, candidate_fields: list[str]) -> tuple[str | None, float]:
    """Returns (best_field, similarity) or (None, best_score_seen) if
    nothing cleared the threshold -- caller falls back to the LLM."""
    try:
        header_vec = _get_embedding(column_header)
    except Exception:
        return None, 0.0

    best_field, best_score = None, 0.0
    for field in candidate_fields:
        try:
            score = _cosine_similarity(header_vec, _get_field_embedding(field))
        except Exception:
            continue
        if score > best_score:
            best_field, best_score = field, score

    if best_score >= _SIMILARITY_THRESHOLD:
        return best_field, round(best_score, 3)
    return None, round(best_score, 3)
