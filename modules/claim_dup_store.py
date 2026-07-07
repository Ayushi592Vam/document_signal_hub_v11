"""
modules/claim_dup_store.py

Claim-level duplicate detection across uploads.

HOW IT WORKS
------------
Every time a sheet is parsed, we take a snapshot of each claim keyed by
its Claim ID.  The snapshot stores every field value at the time of
ingestion.

On the NEXT upload (same or different file) we re-check each Claim ID:
  - If the Claim ID already exists in the store   → DUPLICATE CLAIM
  - We diff the old field values vs the new ones  → shows Before / After
  - We persist the latest snapshot so the store
    always reflects the most-recently-seen version.

STORE SCHEMA (documentsignalhub.feature_store.claim_dup_store)
----------------------------------------------------------------
Delta table with columns: dup_key, record (JSON string), last_updated.
`record` holds the same shape as before:
{
  "claim_id":    "CLM-001",
  "sheet_name":  "CGL Loss Run",
  "filename":    "loss_run_v2.xlsx",
  "ingested_at": "2024-01-15T10:30:00",
  "fields": { "Claim Number": "CLM-001", ... }
}

NOTE ON APPROACH: unlike audit_log (pure append), this store is loaded
in full, mutated in memory, and saved back whole -- same pattern as the
old JSON file. For demo-scale claim counts this is fine and keeps
check_and_register_claims() below completely unchanged.
"""

import datetime
import json

from pyspark.sql import Row

from modules.audit import _append_audit

CLAIM_DUP_TABLE = "documentsignalhub.feature_store.claim_dup_store"


# ── Persistence helpers ───────────────────────────────────────────────────────

def _load_claim_dup_store() -> dict:
    try:
        df = spark.table(CLAIM_DUP_TABLE)
        return {r.dup_key: json.loads(r.record) for r in df.collect()}
    except Exception:
        return {}


def _save_claim_dup_store(store: dict) -> None:
    spark.sql(f"DELETE FROM {CLAIM_DUP_TABLE}")
    if not store:
        return
    rows = [
        Row(dup_key=k, record=json.dumps(v), last_updated=datetime.datetime.now())
        for k, v in store.items()
    ]
    spark.createDataFrame(rows).write.format("delta").mode("append").saveAsTable(CLAIM_DUP_TABLE)


# ── Snapshot builder ──────────────────────────────────────────────────────────

def _snapshot_claim(claim_data: dict, claim_id: str, sheet_name: str, filename: str) -> dict:
    """
    Flatten a claim row into a simple {field: value} dict for storage.
    Always uses the raw extracted "value" — never "modified" —
    so the snapshot always reflects what was in the original Excel file.
    """
    fields = {}
    for field, info in claim_data.items():
        val = str(info.get("value", "")).strip()
        if not val:
            val = str(info.get("modified", "")).strip()
        if val:
            fields[field] = val
    return {
        "claim_id":    claim_id,
        "sheet_name":  sheet_name,
        "filename":    filename,
        "ingested_at": datetime.datetime.now().isoformat(),
        "fields":      fields,
    }


# ── Diff engine ───────────────────────────────────────────────────────────────

def _diff_snapshots(old_snap: dict, new_snap: dict) -> dict:
    """
    Compare two claim snapshots field by field.
    Returns a dict of changed fields:
      {"field_name": {"before": "old_val", "after": "new_val"}, ...}
    Only reports a change if BOTH before and after have real values,
    or if a previously non-empty field became empty (genuine deletion).
    Fields where the new value is empty but old also empty are skipped.
    """
    old_fields = old_snap.get("fields", {})
    new_fields = new_snap.get("fields", {})
    all_keys   = set(old_fields) | set(new_fields)
    changes    = {}

    for key in sorted(all_keys):
        old_val = old_fields.get(key, "").strip()
        new_val = new_fields.get(key, "").strip()

        if not old_val and not new_val:
            continue

        if old_val != new_val:
            changes[key] = {"before": old_val, "after": new_val}

    return changes


# ── Main check-and-upsert function ────────────────────────────────────────────

def check_and_register_claims(
    data: list,
    sheet_name: str,
    filename: str,
    detect_claim_id_fn,          # pass modules.schema_mapping.detect_claim_id
) -> dict:
    """
    For every claim in `data`:
      1. Build a snapshot
      2. Check if claim_id already exists in store
      3. If yes  → record as duplicate with field diff
      4. Upsert  → store always has latest snapshot

    Returns a result dict keyed by claim_id (unchanged shape from before).
    """
    store   = _load_claim_dup_store()
    results = {}

    for i, claim_data in enumerate(data):
        claim_id = detect_claim_id_fn(claim_data, i)
        if not claim_id:
            continue

        new_snap = _snapshot_claim(claim_data, claim_id, sheet_name, filename)

        if claim_id in store:
            old_snap = store[claim_id]

            old_fields = old_snap.get("fields", {})
            non_empty  = sum(1 for v in old_fields.values() if str(v).strip())
            total_flds = len(old_fields)
            if total_flds == 0 or (non_empty / total_flds) < 0.3:
                store[claim_id] = new_snap
                results[claim_id] = {"is_duplicate": False}
                continue

            changes  = _diff_snapshots(old_snap, new_snap)

            unchanged_count = len(new_snap["fields"]) - len(changes)
            results[claim_id] = {
                "is_duplicate":    True,
                "prev_filename":   old_snap.get("filename", "unknown"),
                "prev_sheet":      old_snap.get("sheet_name", "unknown"),
                "prev_date":       old_snap.get("ingested_at", "")[:19].replace("T", " "),
                "changes":         changes,
                "unchanged_count": max(0, unchanged_count),
                "changed_count":   len(changes),
                "old_fields":      old_snap.get("fields", {}),
                "new_fields":      new_snap["fields"],
            }
            _append_audit({
                "event":         "CLAIM_DUPLICATE_DETECTED",
                "timestamp":     datetime.datetime.now().isoformat(),
                "claim_id":      claim_id,
                "sheet":         sheet_name,
                "filename":      filename,
                "prev_filename": old_snap.get("filename"),
                "changed_fields": list(changes.keys()),
            })
        else:
            results[claim_id] = {"is_duplicate": False}

        store[claim_id] = new_snap

    _save_claim_dup_store(store)
    return results


# ── Single claim lookup (used by UI for display) ──────────────────────────────

def get_claim_dup_result(claim_id: str, dup_results: dict) -> dict | None:
    """
    Returns the dup result for a specific claim_id, or None if not duplicate.
    """
    result = dup_results.get(claim_id)
    if result and result.get("is_duplicate"):
        return result
    return None


def clear_claim_dup_store() -> None:
    """Wipe the entire store (useful for reset/testing)."""
    _save_claim_dup_store({})
