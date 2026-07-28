"""
modules/semantic_mapping.py

Embedding-based column-to-schema-field matching. Sits BETWEEN the
existing rule-based matcher (normalization._best_standard_name) and the
existing LLM fallback (schema_mapping.llm_map_unknown_fields()) --
catches synonym-style headers (e.g. "Loss Amt Recv'd" vs "Total Paid")
without paying for an LLM call on every single unmapped column.

CHANGED: embeddings now come from Databricks Foundation Model APIs
(the pay-per-token "databricks-gte-large-en" serving endpoint) instead
of a direct Azure OpenAI embeddings call. This endpoint lives inside
the workspace and is queried with the SAME Config()-based
service-principal identity modules/volume_io.py and modules/delta_io.py
already use -- one identity, one bill, no separate embedding secrets
to provision or rotate.

In plain terms: turns each column header and each candidate field name
into a list of numbers (an "embedding") that captures its MEANING, then
measures how close those numbers are (cosine similarity, 0 to 1). Close
enough = auto-map, with no LLM call at all.
"""

import os
import math

from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config

# GTE Large En is the pay-per-token embedding model Databricks hosts
# out of the box -- no endpoint to create, no model to deploy. Override
# via env var if you later stand up a provisioned-throughput or custom
# embedding endpoint instead.
_EMBEDDING_ENDPOINT = os.environ.get("DATABRICKS_EMBEDDING_ENDPOINT", "databricks-gte-large-en")

# NOTE: cosine-similarity scores are model-specific -- they are not
# comparable to the thresholds tuned for the old Azure OpenAI
# text-embedding-3-small vectors. Re-tune this against a few known
# synonym pairs from your own schemas (e.g. "Loss Amt Recv'd" -> "Total
# Paid") before trusting the default.
_SIMILARITY_THRESHOLD = float(os.environ.get("SEMANTIC_MATCH_THRESHOLD", "0.75"))

_field_embedding_cache: dict[str, list[float]] = {}
_client = None


def _get_client() -> WorkspaceClient:
    # Same lazy-singleton pattern as modules/volume_io.py's _get_client()
    # and modules/delta_io.py's _get_config() -- one client per app
    # instance, reused across every call.
    global _client
    if _client is None:
        _client = WorkspaceClient(config=Config())
    return _client


def _get_embedding(text: str) -> list[float]:
    """Calls the Databricks-hosted embedding endpoint (Foundation Model
    APIs, pay-per-token) instead of Azure OpenAI directly. Auth is
    handled entirely by WorkspaceClient(config=Config()) -- the app's
    own service principal, already trusted for the Volume and the SQL
    warehouse."""
    response = _get_client().serving_endpoints.query(
        name=_EMBEDDING_ENDPOINT,
        input=text,
    )
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
