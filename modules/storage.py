"""
modules/storage.py
Feature store (parsed JSON cache), hash store, and SHA-256 helpers.

MIGRATION NOTE: only _load_hash_store / _save_hash_store changed to use
the SQL Warehouse connector (documentsignalhub.feature_store.hash_store).
Everything else -- the feature-store parsed cache and the validation-
result cache -- stays file-based. Point FEATURE_STORE_PATH
(config/settings.py) at /Volumes/documentsignalhub/raw_documents/files/
instead of a local path and this file needs no other changes.
"""

import datetime
import hashlib
import json
import os

import openpyxl

from config.settings import FEATURE_STORE_PATH
from modules.db_connection import get_connection
from modules.normalization import normalize_str

HASH_STORE_TABLE = "documentsignalhub.feature_store.hash_store"


# ── Hash store (SQL Warehouse-backed) ─────────────────────────────────────────

def _load_hash_store() -> dict:
    store = {}
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT file_hash, filename, first_seen, sheet_hashes FROM {HASH_STORE_TABLE}")
        for row in cur.fetchall():
            store[row.file_hash] = {
                "filename": row.filename,
                "first_seen": row.first_seen.isoformat(),
                "sheet_hashes": json.loads(row.sheet_hashes) if row.sheet_hashes else {},
            }
    return store


def _save_hash_store(store: dict) -> None:
    """Full overwrite -- matches the old json.dump(store, ...) behavior."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(f"DELETE FROM {HASH_STORE_TABLE}")
        for file_hash, rec in store.items():
            first_seen = rec.get("first_seen")
            cur.execute(
                f"INSERT INTO {HASH_STORE_TABLE} (file_hash, filename, first_seen, sheet_hashes) "
                f"VALUES (:file_hash, :filename, :first_seen, :sheet_hashes)",
                {
                    "file_hash": file_hash,
                    "filename": rec.get("filename"),
                    "first_seen": datetime.datetime.fromisoformat(first_seen) if first_seen else datetime.datetime.now(),
                    "sheet_hashes": json.dumps(rec.get("sheet_hashes", {})),
                },
            )


# ── SHA-256 helpers (unchanged) ───────────────────────────────────────────────

def _compute_file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65_536), b""):
            h.update(chunk)
    return h.hexdigest()


def _compute_sheet_sha256(file_path: str, sheet_name: str) -> str:
    ext = os.path.splitext(file_path)[1].lower()

    if ext in (".csv", ".pdf", ".docx"):
        h = hashlib.sha256()
        h.update(sheet_name.encode("utf-8"))
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(65_536), b""):
                h.update(chunk)
        return h.hexdigest()

    wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    ws = wb[sheet_name]
    h  = hashlib.sha256()
    for row in ws.iter_rows(values_only=True):
        for cell in row:
            h.update(str(cell).encode("utf-8"))
    wb.close()
    return h.hexdigest()


# ── Feature store (unchanged -- file-based, just repoint FEATURE_STORE_PATH
#    at your Volume in config/settings.py) ────────────────────────────────────

def _load_from_feature_store(sheet_hash: str) -> dict | None:
    if not sheet_hash:
        return None
    index_path = os.path.join(FEATURE_STORE_PATH, "index.json")
    if not os.path.exists(index_path):
        return None
    try:
        with open(index_path) as f:
            index = json.load(f)
        entry = index.get(sheet_hash)
        if not entry:
            return None
        data_path = entry.get("path")
        if not data_path or not os.path.exists(data_path):
            return None
        with open(data_path) as f:
            return json.load(f)
    except Exception:
        return None


def _save_to_feature_store(sheet_hash: str, sheet_name: str, data: dict) -> str:
    ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(FEATURE_STORE_PATH, f"{sheet_name}_{ts}.json")

    def _san(obj):
        if isinstance(obj, dict):
            return {k: _san(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_san(i) for i in obj]
        if isinstance(obj, str):
            return normalize_str(obj)
        return obj

    with open(path, "w") as f:
        json.dump(_san(data), f, indent=2, ensure_ascii=False)

    index_path = os.path.join(FEATURE_STORE_PATH, "index.json")
    try:
        with open(index_path) as f:
            index = json.load(f)
    except Exception:
        index = {}
    index[sheet_hash] = {
        "path":       path,
        "sheet_name": sheet_name,
        "saved_at":   datetime.datetime.now().isoformat(),
    }
    with open(index_path, "w") as f:
        json.dump(index, f, indent=2)
    return path


# ── Validation result store (unchanged -- file-based) ─────────────────────────

def _load_validation_result(doc_hash: str) -> dict | None:
    val_path = os.path.join(FEATURE_STORE_PATH, f"validation_{doc_hash}.json")
    if not os.path.exists(val_path):
        return None
    try:
        with open(val_path) as f:
            return json.load(f)
    except Exception:
        return None


def _save_validation_result(doc_hash: str, result: dict) -> None:
    val_path = os.path.join(FEATURE_STORE_PATH, f"validation_{doc_hash}.json")
    try:
        with open(val_path, "w") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
    except Exception:
        pass
