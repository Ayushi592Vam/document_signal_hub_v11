"""
modules/document_metadata.py
Unified per-document metadata record: Document ID, Tags, Parser Used,
Processing Status, Confidence Score, Execution Time, Version, and
lifecycle history.

Combines three checklist items (Metadata Framework, Document Tags,
Version Tracking) into one record rather than three separate stores,
since they describe the same underlying object.

Persisted to the Volume via modules/volume_io.py, keyed by document_id
(use the file_hash from storage.py's _compute_file_sha256 as the ID).

NOTE ON LINEAGE: Unity Catalog's native Lineage tab tracks table/
notebook/job relationships, not arbitrary Volume file writes. This
module's status_history, alongside modules/orchestrator.py's
run_with_lineage(), is the actual lineage mechanism for this app --
not Unity Catalog's built-in lineage graph.
"""

import datetime

from config.settings import FEATURE_STORE_PATH
from modules.volume_io import load_json, save_json

_METADATA_PATH = f"{FEATURE_STORE_PATH}/document_metadata.json"


def _load_all() -> dict:
    return load_json(_METADATA_PATH, default={})


def _save_all(records: dict) -> None:
    save_json(_METADATA_PATH, records)


def record_metadata(
    document_id: str,        # use file_hash
    filename: str,
    parser_used: str,
    status: str,              # e.g. "ingested", "parsed", "enriched", "exported", "failed"
    confidence_score: float | None = None,
    execution_time_ms: float | None = None,
    tags: list[str] | None = None,
    user: str | None = None,
) -> dict:
    """
    Creates or updates the metadata record for a document. Each call
    bumps `version` and appends to `status_history`, so the full
    lifecycle (Uploaded -> Parsed -> Enriched -> Modified -> Exported)
    is reconstructable from one record.
    """
    records = _load_all()
    now = datetime.datetime.now().isoformat()

    existing = records.get(document_id)
    version  = (existing.get("version", 0) + 1) if existing else 1
    history  = existing.get("status_history", []) if existing else []
    history.append({
        "status":       status,
        "timestamp":    now,
        "parser_used":  parser_used,
    })

    record = {
        "document_id":       document_id,
        "filename":          filename,
        "parser_used":       parser_used,
        "status":            status,
        "confidence_score":  confidence_score,
        "execution_time_ms": execution_time_ms,
        "tags":              sorted(set((existing.get("tags", []) if existing else []) + (tags or []))),
        "user":              user or "unknown",
        "version":           version,
        "status_history":    history,
        "created_at":        existing.get("created_at", now) if existing else now,
        "updated_at":        now,
    }
    records[document_id] = record
    _save_all(records)
    return record


def get_metadata(document_id: str) -> dict | None:
    return _load_all().get(document_id)
