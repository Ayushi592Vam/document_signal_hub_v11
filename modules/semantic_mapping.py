"""
modules/semantic_mapping.py

Embedding-based column-to-schema-field matching. Sits BETWEEN the
existing rule-based matcher (normalization._best_standard_name) and the
existing LLM fallback (schema_mapping.llm_map_unknown_fields()) --
catches synonym-style headers (e.g. "Loss Amt Recv'd" vs "Total Paid")
without paying for an LLM call on every single unmapped column.

CHANGED: embeddings now come from a Databricks Foundation Model API
serving endpoint (Mosaic AI Model Serving) instead of a raw urllib call
to a separately-configured Azure OpenAI embeddings resource. This reuses
the SAME service-principal auth (databricks.sdk.core.Config) that
modules/delta_io.py already uses for the SQL warehouse -- no separate
OPENAI_EMBEDDING_ENDPOINT / OPENAI_EMBEDDING_API_KEY secrets needed, and
the call never leaves the Databricks workspace boundary.

In plain terms: turns each column header and each candidate field name
into a list of numbers (an "embedding") that captures its MEANING, then
measures how close those numbers are (cosine similarity, 0 to 1). Close
enough = auto-map, with no LLM call at all.
"""

import os
import math

from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config

_SIMILARITY_THRESHOLD = float(os.environ.get("SEMANTIC_MATCH_THRESHOLD", "0.80"))

# Foundation Model API pay-per-token embedding endpoint. Override via env
# if your workspace has a different serving endpoint name provisioned
# (e.g. a provisioned-throughput endpoint instead of pay-per-token).
_EMBEDDING_ENDPOINT = os.environ.get(
    "DATABRICKS_EMBEDDING_ENDPOINT", "databricks-gte-large-en"
)

_field_embedding_cache: dict[str, list[float]] = {}

_cfg: Config | None = None
_client: WorkspaceClient | None = None


def _get_client() -> WorkspaceClient:
    """Reuses the same auth pattern as modules/delta_io.py's
    _get_connection() -- one Config(), one client, cached at module level
    rather than re-authenticating on every embedding call."""
    global _cfg, _client
    if _client is None:
        _cfg = Config()
        _client = WorkspaceClient(config=_cfg)
    return _client


def _get_embedding(text: str) -> list[float]:
    client = _get_client()
    response = client.serving_endpoints.query(
        name=_EMBEDDING_ENDPOINT,
        input=[text],
    )
    # Foundation Model API embedding responses follow the OpenAI-compatible
    # shape: {"data": [{"embedding": [...]}]}
    return response.data[0].embedding


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
