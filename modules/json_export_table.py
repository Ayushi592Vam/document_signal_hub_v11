"""
modules/json_export_table.py
Tracks every generated JSON export (upsert by filename+sheet+type).

MIGRATION NOTE: same upsert-in-Python-then-full-save pattern as
claim_dup_store.py -- loads the whole table, does the upsert check
in memory, writes it all back. Fine at demo scale.
"""

import datetime
import json

from pyspark.sql import Row

EXPORT_TABLE = "documentsignalhub.feature_store.json_export_table"


def _load_json_export_table() -> list:
    try:
        df = spark.table(EXPORT_TABLE)
        return [
            {
                "filename": r.filename,
                "sheet": r.sheet,
                "timestamp": r.export_time.isoformat(),
                "type": r.export_type,
                "record_count": r.record_count,
                "json": r.export_json,
            }
            for r in df.collect()
        ]
    except Exception:
        return []


def _save_json_export_table(table: list) -> None:
    spark.sql(f"DELETE FROM {EXPORT_TABLE}")
    if not table:
        return
    rows = []
    for entry in table:
        ts = entry.get("timestamp")
        rows.append(Row(
            filename=entry.get("filename"),
            sheet=entry.get("sheet"),
            export_time=datetime.datetime.fromisoformat(ts) if ts else datetime.datetime.now(),
            export_type=entry.get("type"),
            record_count=entry.get("record_count", 0),
            export_json=entry.get("json"),
        ))
    spark.createDataFrame(rows).write.format("delta").mode("append").saveAsTable(EXPORT_TABLE)


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
