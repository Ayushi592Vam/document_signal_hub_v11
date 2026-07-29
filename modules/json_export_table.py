"""
modules/json_export_table.py
Tracks every generated JSON export, now backed by a Delta table
(documentsignalhub.feature_store.json_export_table), upserted by a
derived key of filename|sheet|type via delta_io.upsert_row().

MIGRATION NOTE: previously a single JSON blob on the Volume, rewritten
in full on every export. delta_io.upsert_row() already does a MERGE
INTO with match-and-replace semantics, so there's no need to load the
whole table, scan for a matching entry, and write everything back --
that full-rewrite pattern is exactly what this migration removes.
"""

import datetime
import json

from modules.delta_io import upsert_row, delete_all_rows, read_rows

_TABLE = "documentsignalhub.feature_store.json_export_table"


def _make_dup_key(filename: str, sheet: str, export_type: str) -> str:
    return f"{filename}|{sheet}|{export_type}"


def _load_json_export_table() -> list:
    """Full-table read. Reconstructs the original entry dicts from Delta
    rows -- kept for any caller that needs every export record."""
    rows = read_rows(_TABLE)
    out = []
    for r in rows:
        try:
            record = json.loads(r["record_json"] or "{}")
        except Exception:
            record = {}
        record.setdefault("filename", r.get("filename"))
        record.setdefault("sheet", r.get("sheet"))
        record.setdefault("type", r.get("export_type"))
        record.setdefault("record_count", r.get("record_count"))
        record.setdefault("timestamp", str(r.get("export_time", "")))
        out.append(record)
    return out


def _append_json_export(entry: dict, cost_metadata: dict | None = None) -> None:
    """
    entry: filename, sheet, timestamp, type, record_count, json.
    cost_metadata: optional dict (calls, prompt_tokens, completion_tokens,
    total_cost_usd, models_used).

    Upserts ONE row keyed by filename|sheet|type -- same "most recent
    wins" behavior as the old JSON-era code, just without the
    load-entire-list-then-rewrite step.
    """
    if cost_metadata is not None:
        entry = {**entry, "cost_metadata": cost_metadata}

    filename    = entry.get("filename", "")
    sheet       = entry.get("sheet", "")
    export_type = entry.get("type", "")
    dup_key     = _make_dup_key(filename, sheet, export_type)

    upsert_row(_TABLE, "dup_key", {
        "dup_key":      dup_key,
        "filename":     filename,
        "sheet":        sheet,
        "export_type":  export_type,
        "export_time":  entry.get("timestamp", datetime.datetime.now().isoformat()),
        "record_count": int(entry.get("record_count", 0) or 0),
        "record_json":  json.dumps(entry),
    })


def clear_json_export_table() -> None:
    """Full-table clear -- used by the Cache Manager's export-history
    clear action."""
    delete_all_rows(_TABLE)
