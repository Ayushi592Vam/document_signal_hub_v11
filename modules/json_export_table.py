"""
modules/json_export_table.py
Tracks every generated JSON export (upsert by filename+sheet+type).
"""

import datetime
import json

from modules.db_connection import get_connection

EXPORT_TABLE = "documentsignalhub.feature_store.json_export_table"


def _load_json_export_table() -> list:
    table = []
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT filename, sheet, export_time, export_type, record_count, export_json FROM {EXPORT_TABLE}"
        )
        for row in cur.fetchall():
            table.append({
                "filename": row.filename,
                "sheet": row.sheet,
                "timestamp": row.export_time.isoformat(),
                "type": row.export_type,
                "record_count": row.record_count,
                "json": row.export_json,
            })
    return table


def _save_json_export_table(table: list) -> None:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(f"DELETE FROM {EXPORT_TABLE}")
        for entry in table:
            ts = entry.get("timestamp")
            cur.execute(
                f"INSERT INTO {EXPORT_TABLE} (filename, sheet, export_time, export_type, record_count, export_json) "
                f"VALUES (:filename, :sheet, :export_time, :export_type, :record_count, :export_json)",
                {
                    "filename": entry.get("filename"),
                    "sheet": entry.get("sheet"),
                    "export_time": datetime.datetime.fromisoformat(ts) if ts else datetime.datetime.now(),
                    "export_type": entry.get("type"),
                    "record_count": entry.get("record_count", 0),
                    "export_json": entry.get("json"),
                },
            )


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
