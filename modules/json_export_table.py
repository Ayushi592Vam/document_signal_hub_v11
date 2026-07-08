"""
modules/json_export_table.py
Tracks every generated JSON export (upsert by filename+sheet+type).

MIGRATION NOTE: file-based against a Volume path
(JSON_EXPORT_TABLE_PATH in config/settings.py) -- no compute needed.
"""

import json
from config.settings import JSON_EXPORT_TABLE_PATH


def _load_json_export_table() -> list:
    try:
        with open(JSON_EXPORT_TABLE_PATH) as f:
            return json.load(f)
    except Exception:
        return []


def _save_json_export_table(table: list) -> None:
    with open(JSON_EXPORT_TABLE_PATH, "w") as f:
        json.dump(table, f, indent=2)


def _append_json_export(entry: dict) -> None:
    table = _load_json_export_table()
    for existing in table:
        if (
            existing.get("filename") == entry.get("filename")
            and existing.get("sheet") == entry.get("sheet")
            and existing.get("type") == entry.get("type")
        ):
            existing.update(entry)
            _save_json_export_table(table)
            return
    table.append(entry)
    _save_json_export_table(table)
