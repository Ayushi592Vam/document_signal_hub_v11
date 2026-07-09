"""
modules/claim_dup_store.py

Claim-level duplicate detection across uploads.

HOW IT WORKS
------------
Every time a sheet is parsed, we take a snapshot of each claim keyed by
its Claim ID. On the NEXT upload we re-check each Claim ID:
  - If the Claim ID already exists in the store   → DUPLICATE CLAIM
  - We diff the old field values vs the new ones  → shows Before / After
  - We persist the latest snapshot so the store always reflects the
    most-recently-seen version.

MIGRATION NOTE: persistence now goes through modules/volume_io.py (Files
API) instead of plain open() -- see that module for why.
"""

import datetime

from config.settings import CLAIM_DUP_STORE_PATH
from modules.audit import _append_audit
from modules.volume_io import load_json, save_json


# ── Persistence helpers ───────────────────────────────────────────────────────

def _load_claim_dup_store() -> dict:
    return load_json(CLAIM_DUP_STORE_PATH, default={})


def _save_claim_dup_store(store: dict) -> None:
    save_json(CLAIM_DUP_STORE_PATH, store)


# ── Snapshot builder ──────────────────────────────────────────────────────────

def _snapshot_claim(claim_data: dict, claim_id: str, sheet_name: str, filename: str) -> dict:
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
    detect_claim_id_fn,
) -> dict:
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
    result = dup_results.get(claim_id)
    if result and result.get("is_duplicate"):
        return result
    return None


def clear_claim_dup_store() -> None:
    """Wipe the entire store (useful for reset/testing)."""
    _save_claim_dup_store({})
