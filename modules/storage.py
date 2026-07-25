"""
modules/storage.py
Feature store (parsed JSON cache, still on the Volume) and hash store
(now Delta, keyed by file_hash) SHA-256 helpers.

MIGRATION NOTE: hash_store moved from a single JSON blob to a Delta
table, upserted by file_hash via modules.delta_io.upsert_row(). A new
upload now only writes its OWN row instead of loading the entire store,
mutating one key, and saving the whole thing back -- the point of using
a real database instead of a flat file. _load_hash_store() (full-table
read) is kept ONLY for app2.py's cross-file sheet-duplicate index, which
genuinely needs every file's sheet_hashes; a single file's own lookup
should use _get_hash_entry() instead.
"""

import datetime
import hashlib
import json
import os

import openpyxl

from config.settings import FEATURE_STORE_PATH
from modules.normalization import normalize_str
from modules.volume_io import load_json, save_json
from modules.delta_io import read_rows, upsert_row, get_row

_HASH_STORE_TABLE = "documentsignalhub.feature_store.hash_store"


def _load_hash_store() -> dict:
    """Full-table read. Use only where every file's record is needed
    (the cross-file sheet-duplicate scan) -- for a single file, use
    _get_hash_entry() instead."""
    rows = read_rows(_HASH_STORE_TABLE)
    return {
        r["file_hash"]: {
            "filename":     r["filename"],
            "first_seen":   str(r["first_seen"]),
            "sheet_hashes": json.loads(r["sheet_hashes"] or "{}"),
        }
        for r in rows
    }


def _get_hash_entry(file_hash: str) -> dict | None:
    """Single-key lookup -- one indexed row read, not a full-table scan."""
    r = get_row(_HASH_STORE_TABLE, "file_hash", file_hash)
    if not r:
        return None
    return {
        "filename":     r["filename"],
        "first_seen":   str(r["first_seen"]),
        "sheet_hashes": json.loads(r["sheet_hashes"] or "{}"),
    }


def _save_hash_entry(file_hash: str, filename: str, first_seen: str, sheet_hashes: dict) -> None:
    """Upserts ONE file's hash record."""
    upsert_row(_HASH_STORE_TABLE, "file_hash", {
        "file_hash":    file_hash,
        "filename":     filename,
        "first_seen":   first_seen,
        "sheet_hashes": json.dumps(sheet_hashes),
    })


# ── Feature store (unchanged — still Volume/JSON-backed) ─────────────────────

def _compute_file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65_536), b""):
            h.update(chunk)
    return h.hexdigest()


def _compute_sheet_sha256(file_path: str, sheet_name: str) -> str:
    ext = os.path.splitext(file_path)[1].lower()
    if ext in (".csv", ".pdf", ".docx", ".html", ".htm"):
        h = hashlib.sha256()
        h.update(sheet_name.encode("utf-8"))
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(65_536), b""):
                h.update(chunk)
        return h.hexdigest()
    wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    ws = wb[sheet_name]
    values: list[str] = []
    for row in ws.iter_rows(values_only=True):
        for raw_cell in row:
            if raw_cell is None:
                continue
            v = str(raw_cell).strip()
            if v:
                values.append(v)
    wb.close()
    values.sort()
    h = hashlib.sha256()
    for v in values:
        h.update(v.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _load_from_feature_store(sheet_hash: str) -> dict | None:
    if not sheet_hash:
        return None
    index_path = f"{FEATURE_STORE_PATH}/index.json"
    index = load_json(index_path, default=None)
    if not index:
        return None
    entry = index.get(sheet_hash)
    if not entry:
        return None
    data_path = entry.get("path")
    if not data_path:
        return None
    return load_json(data_path, default=None)


def _save_to_feature_store(sheet_hash: str, sheet_name: str, data: dict) -> str:
    ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = f"{FEATURE_STORE_PATH}/{sheet_name}_{ts}.json"

    def _san(obj):
        if isinstance(obj, dict):
            return {k: _san(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_san(i) for i in obj]
        if isinstance(obj, str):
            return normalize_str(obj)
        return obj

    save_json(path, _san(data))
    index_path = f"{FEATURE_STORE_PATH}/index.json"
    index = load_json(index_path, default={})
    index[sheet_hash] = {"path": path, "sheet_name": sheet_name, "saved_at": datetime.datetime.now().isoformat()}
    save_json(index_path, index)
    return path


# ── Validation result store (unchanged -- file-based via Files API) ──────────

def _load_validation_result(doc_hash: str) -> dict | None:
    val_path = f"{FEATURE_STORE_PATH}/validation_{doc_hash}.json"
    return load_json(val_path, default=None)


def _save_validation_result(doc_hash: str, result: dict) -> None:
    val_path = f"{FEATURE_STORE_PATH}/validation_{doc_hash}.json"
    save_json(val_path, result)
