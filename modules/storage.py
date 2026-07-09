"""
modules/storage.py
Feature store (parsed JSON cache), hash store, and SHA-256 helpers.

MIGRATION NOTE: all persistence now goes through modules/volume_io.py
(Databricks Files API) instead of plain open() -- see that module for
why open()/os.makedirs() against /Volumes doesn't work reliably inside
a Databricks App. _compute_file_sha256 / _compute_sheet_sha256 are
unchanged since they read the uploaded file from local temp storage
(st.session_state.tmpdir), not the Volume.
"""

import datetime
import hashlib
import os

import openpyxl

from config.settings import FEATURE_STORE_PATH, HASH_STORE_PATH
from modules.normalization import normalize_str
from modules.volume_io import load_json, save_json


# ── Hash store ────────────────────────────────────────────────────────────────

def _load_hash_store() -> dict:
    return load_json(HASH_STORE_PATH, default={})


def _save_hash_store(store: dict) -> None:
    save_json(HASH_STORE_PATH, store)


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


# ── Feature store ─────────────────────────────────────────────────────────────

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
    index[sheet_hash] = {
        "path":       path,
        "sheet_name": sheet_name,
        "saved_at":   datetime.datetime.now().isoformat(),
    }
    save_json(index_path, index)
    return path


# ── Validation result store ───────────────────────────────────────────────────

def _load_validation_result(doc_hash: str) -> dict | None:
    val_path = f"{FEATURE_STORE_PATH}/validation_{doc_hash}.json"
    return load_json(val_path, default=None)


def _save_validation_result(doc_hash: str, result: dict) -> None:
    val_path = f"{FEATURE_STORE_PATH}/validation_{doc_hash}.json"
    save_json(val_path, result)
