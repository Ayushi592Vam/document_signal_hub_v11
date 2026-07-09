"""
modules/json_export_table.py
Tracks every generated JSON export (upsert by filename+sheet+type).

MIGRATION NOTE: persistence now goes through modules/volume_io.py
(Files API) instead of plain open().
"""

from config.settings import JSON_EXPORT_TABLE_PATH
from modules.volume_io import load_json, save_json


def _load_json_export_table() -> list:
    return load_json(JSON_EXPORT_TABLE_PATH, default=[])


def _save_json_export_table(table: list) -> None:
    save_json(JSON_EXPORT_TABLE_PATH, table)


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
