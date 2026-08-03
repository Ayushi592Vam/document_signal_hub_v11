"""
modules/mapping_memory.py
Cross-file memory of resolved column -> schema-field mappings, so
semantic/LLM resolution only has to happen once per (schema, normalized
column) pair. Delta-backed, keyed by "schema_name|normalized_source_column",
upserted via MERGE -- same pattern as modules/storage.py's hash_store.
"""

import datetime
import re

from modules.delta_io import get_row, get_rows, upsert_row, upsert_rows

_TABLE = "documentsignalhub.feature_store.field_mapping_memory"


def _normalize(col: str) -> str:
    c = re.sub(r"[_\-]+", " ", col.strip().lower())
    return re.sub(r"\s+", " ", c)


def _key(schema_name: str, source_column: str) -> str:
    return f"{schema_name}|{_normalize(source_column)}"


def lookup_many(schema_name: str, source_columns: list[str]) -> dict[str, dict]:
    """Batch point lookup -- ONE query for every unmapped column in a
    sheet, instead of a call per column."""
    if not source_columns:
        return {}
    keys = [_key(schema_name, c) for c in source_columns]
    rows = get_rows(_TABLE, "mapping_key", keys)
    key_to_col = {_key(schema_name, c): c for c in source_columns}
    out: dict[str, dict] = {}
    for k, row in rows.items():
        col = key_to_col.get(k)
        if col:
            out[col] = {
                "resolved_field": row["resolved_field"],
                "method":         row["method"],
                "confidence":     float(row["confidence"]),
                "hit_count":      int(row["hit_count"]),
                "user_corrected": bool(row["user_corrected"]),
            }
    return out


def remember_batch(schema_name: str, entries: list[dict]) -> None:
    """entries: [{"source_column", "resolved_field", "method", "confidence"}, ...]
    Batch upsert -- one MERGE for the whole sheet. Never overwrites a
    mapping a user has explicitly corrected."""
    if not entries:
        return
    existing = get_rows(
        _TABLE, "mapping_key",
        [_key(schema_name, e["source_column"]) for e in entries],
    )
    rows = []
    for e in entries:
        k = _key(schema_name, e["source_column"])
        prior = existing.get(k)
        if prior and prior.get("user_corrected"):
            continue  # a human already fixed this -- don't clobber it
        hit_count = int(prior["hit_count"]) + 1 if prior else 1
        rows.append({
            "mapping_key":              k,
            "schema_name":              schema_name,
            "source_column_raw":        e["source_column"],
            "source_column_normalized": _normalize(e["source_column"]),
            "resolved_field":           e["resolved_field"],
            "method":                   e["method"],
            "confidence":               float(e["confidence"]),
            "hit_count":                hit_count,
            "last_confirmed_at":        datetime.datetime.now().isoformat(),
            "user_corrected":           False,
        })
    upsert_rows(_TABLE, "mapping_key", rows)


def remember_user_correction(schema_name: str, source_column: str, corrected_field: str) -> None:
    """Call this when a reviewer overrides which schema field a column
    maps to. Marks the row so remember_batch() never overwrites it."""
    upsert_row(_TABLE, "mapping_key", {
        "mapping_key":              _key(schema_name, source_column),
        "schema_name":              schema_name,
        "source_column_raw":        source_column,
        "source_column_normalized": _normalize(source_column),
        "resolved_field":           corrected_field,
        "method":                   "user",
        "confidence":               100.0,
        "hit_count":                1,
        "last_confirmed_at":        datetime.datetime.now().isoformat(),
        "user_corrected":           True,
    })
