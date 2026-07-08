"""
modules/cache_manager.py
Centralised cache-clearing utilities for the TPA Loss Run Parser.
Handles all 4 cache layers:
  1. Streamlit session state (UI state, selections, modified values)
  2. Feature store — parsed JSON cache (claims_json/, still file-based)
  3. Hash store     — file duplicate memory (now via SQL Warehouse)
  4. Claim dup store — cross-upload claim change tracking (now via SQL Warehouse)
  5. Audit log & export table — also now via SQL Warehouse
"""

import glob
import os

from config.settings import FEATURE_STORE_PATH
from modules.db_connection import get_connection

HASH_STORE_TABLE = "documentsignalhub.feature_store.hash_store"
CLAIM_DUP_TABLE = "documentsignalhub.feature_store.claim_dup_store"
AUDIT_LOG_TABLE = "documentsignalhub.feature_store.audit_log"
EXPORT_TABLE = "documentsignalhub.feature_store.json_export_table"


def _count_and_clear(table: str) -> int:
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS c FROM {table}")
            count = cur.fetchone().c
            cur.execute(f"DELETE FROM {table}")
        return count
    except Exception:
        return 0


def _count(table: str) -> int:
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS c FROM {table}")
            return cur.fetchone().c
    except Exception:
        return 0


# ── Individual clear functions ────────────────────────────────────────────────

def clear_session_cache(session_state) -> int:
    KEEP_KEYS = {
        "conf_threshold", "use_conf_threshold", "active_schema",
        "schema_popup_target", "schema_popup_tab",
        "tmpdir", "last_uploaded", "sheet_names", "sheet_cache",
        "selected_idx", "focus_field",
        "current_file_hash", "sheet_hashes", "sheet_dup_info",
        "is_duplicate_file", "duplicate_first_seen", "duplicate_orig_name",
        "claim_dup_migrated_v2",
    }
    keys_to_del = [
        k for k in list(session_state.keys())
        if k not in KEEP_KEYS
        and not k.startswith("custom_fields_")
        and not k.startswith("_fdi_")
    ]
    for k in keys_to_del:
        del session_state[k]
    return len(keys_to_del)


def clear_parsed_cache() -> tuple[int, int]:
    if not os.path.exists(FEATURE_STORE_PATH):
        return 0, 0

    total_bytes = 0
    total_files = 0

    for fpath in glob.glob(os.path.join(FEATURE_STORE_PATH, "*.json")):
        try:
            total_bytes += os.path.getsize(fpath)
            os.remove(fpath)
            total_files += 1
        except Exception:
            pass

    index_path = os.path.join(FEATURE_STORE_PATH, "index.json")
    if os.path.exists(index_path):
        try:
            total_bytes += os.path.getsize(index_path)
            os.remove(index_path)
            total_files += 1
        except Exception:
            pass

    return total_files, total_bytes


def clear_hash_store() -> int:
    return _count_and_clear(HASH_STORE_TABLE)


def clear_claim_dup_store() -> int:
    return _count_and_clear(CLAIM_DUP_TABLE)


def clear_audit_log() -> int:
    return _count_and_clear(AUDIT_LOG_TABLE)


def clear_export_table() -> int:
    return _count_and_clear(EXPORT_TABLE)


# ── Stats helpers ─────────────────────────────────────────────────────────────

def get_cache_stats() -> dict:
    stats = {}

    parsed_files = glob.glob(os.path.join(FEATURE_STORE_PATH, "*.json"))
    parsed_bytes = sum(os.path.getsize(f) for f in parsed_files if os.path.exists(f))
    stats["parsed"] = {
        "files": len(parsed_files),
        "size_kb": round(parsed_bytes / 1024, 1),
    }

    stats["hash_store"] = {"entries": _count(HASH_STORE_TABLE)}
    stats["claim_dups"] = {"entries": _count(CLAIM_DUP_TABLE)}
    stats["audit_log"] = {"entries": _count(AUDIT_LOG_TABLE)}
    stats["export_table"] = {"entries": _count(EXPORT_TABLE)}

    return stats


def _fmt_size(kb: float) -> str:
    if kb >= 1024:
        return f"{kb / 1024:.1f} MB"
    return f"{kb:.1f} KB"
