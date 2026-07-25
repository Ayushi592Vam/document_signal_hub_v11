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
"""

import datetime
import json

from modules.audit import _append_audit
from modules.delta_io import get_row, upsert_row, delete_all_rows

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

def check_and_register_claims(
    data: list,
    sheet_name: str,
    filename: str,
    detect_claim_id_fn,
) -> dict:
    results = {}

    for i, claim_data in enumerate(data):
        claim_id = detect_claim_id_fn(claim_data, i)
        if not claim_id:
            continue

        new_snap = _snapshot_claim(claim_data, claim_id, sheet_name, filename)
        existing = get_row(_CLAIM_DUP_TABLE, "dup_key", claim_id)

        if existing:
            old_snap   = json.loads(existing["record"])
            old_fields = old_snap.get("fields", {})
            non_empty  = sum(1 for v in old_fields.values() if str(v).strip())
            total_flds = len(old_fields)

            if total_flds == 0 or (non_empty / total_flds) < 0.3:
                results[claim_id] = {"is_duplicate": False}
            else:
                changes = _diff_snapshots(old_snap, new_snap)
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
                    "event":          "CLAIM_DUPLICATE_DETECTED",
                    "timestamp":      datetime.datetime.now().isoformat(),
                    "claim_id":       claim_id,
                    "sheet":          sheet_name,
                    "filename":       filename,
                    "prev_filename":  old_snap.get("filename"),
                    "changed_fields": list(changes.keys()),
                })
        else:
            results[claim_id] = {"is_duplicate": False}

        upsert_row(_CLAIM_DUP_TABLE, "dup_key", {
            "dup_key":      claim_id,
            "record":       json.dumps(new_snap),
            "last_updated": datetime.datetime.now().isoformat(),
        })

    return results


# ── Single claim lookup (unchanged — operates on the results dict) ───────────

def get_claim_dup_result(claim_id: str, dup_results: dict) -> dict | None:
    result = dup_results.get(claim_id)
    if result and result.get("is_duplicate"):
        return result
    return None


def clear_claim_dup_store() -> None:
    delete_all_rows(_CLAIM_DUP_TABLE)
