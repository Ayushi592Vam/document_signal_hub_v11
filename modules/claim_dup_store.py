"""
modules/claim_dup_store.py

Claim-level duplicate detection across uploads. Now backed by a Delta
table (documentsignalhub.feature_store.claim_dup_store), keyed by
dup_key (claim_id or check_number), upserted per claim via MERGE.

TRADEOFF, stated explicitly: the old JSON version batched every claim
in a sheet into ONE save at the end of the upload. This version issues
one MERGE per claim (point lookup + point upsert), since avoiding a
full-table load on every upload is the actual reason to migrate off
JSON. For a very large sheet this means more round-trips, not fewer --
if that becomes a real latency problem, switch to a single bulk MERGE
using a VALUES-list source instead of per-claim MERGE calls.

NOTE: this module no longer exposes _load_claim_dup_store() /
_save_claim_dup_store() -- those were the old JSON-era whole-store
read/write helpers. Callers that need to clear ONE claim's duplicate
history (e.g. ui/claim_dup_panel.py's "Clear duplicate history for
this claim" button) should use clear_claim_dup_entry(claim_id) below,
which deletes exactly that row via delta_io.delete_row() instead of
loading the entire table into memory, mutating a dict, and writing it
all back.
"""

import datetime
import json

from modules.audit import _append_audit
from modules.delta_io import get_row, upsert_row, delete_all_rows, delete_row

_CLAIM_DUP_TABLE = "documentsignalhub.feature_store.claim_dup_store"


# ── Snapshot builder (unchanged) ──────────────────────────────────────────────

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


# ── Diff engine (unchanged) ───────────────────────────────────────────────────

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

def check_and_register_claims(data, sheet_name, filename, detect_claim_id_fn):
    results = {}

    # 1) Collect all claim IDs first
    id_map = {}  # claim_id -> claim_data
    for i, claim_data in enumerate(data):
        claim_id = detect_claim_id_fn(claim_data, i)
        if claim_id:
            id_map[claim_id] = claim_data

    if not id_map:
        return results

    # 2) ONE batched read for all existing rows, instead of N get_row calls
    existing_rows = batch_get_rows(_CLAIM_DUP_TABLE, "dup_key", list(id_map.keys()))
    existing_by_id = {r["dup_key"]: r for r in existing_rows}

    # 3) Build all new snapshots + diff logic in memory (no I/O here)
    upsert_batch = []
    for claim_id, claim_data in id_map.items():
        new_snap = _snapshot_claim(claim_data, claim_id, sheet_name, filename)
        existing = existing_by_id.get(claim_id)
        # ... same diff logic as before, populate results[claim_id] ...
        upsert_batch.append({
            "dup_key": claim_id,
            "record": json.dumps(new_snap),
            "last_updated": datetime.datetime.now().isoformat(),
        })

    # 4) ONE batched MERGE for all rows, instead of N upsert_row calls
    batch_upsert_rows(_CLAIM_DUP_TABLE, "dup_key", upsert_batch)

    return results


# ── Single claim lookup (unchanged — operates on the results dict) ───────────

def get_claim_dup_result(claim_id: str, dup_results: dict) -> dict | None:
    result = dup_results.get(claim_id)
    if result and result.get("is_duplicate"):
        return result
    return None


# ── NEW: single-record clear (replaces the old _load/_save whole-store pair) ─

def clear_claim_dup_entry(claim_id: str) -> None:
    """
    Removes exactly ONE claim's duplicate-history row, keyed by dup_key.

    This is what ui/claim_dup_panel.py's "Clear duplicate history for
    this claim" button should call. It replaces the old JSON-era pattern
    of _load_claim_dup_store() -> mutate dict -> _save_claim_dup_store(),
    which doesn't exist anymore now that this table is Delta-backed --
    there's no "whole store" to load into a dict in the first place.
    """
    if not claim_id:
        return
    delete_row(_CLAIM_DUP_TABLE, "dup_key", claim_id)


def clear_claim_dup_store() -> None:
    """Full-table clear -- used by the Cache Manager's 'Clear caches'
    buttons, not by the per-claim clear action above."""
    delete_all_rows(_CLAIM_DUP_TABLE)
