"""
modules/cache_manager.py
Centralised cache-clearing utilities for the TPA Loss Run Parser.
Handles all 4 cache layers:
  1. Streamlit session state (UI state, selections, modified values)
  2. Feature store — parsed JSON cache (claims_json/, still file-based)
  3. Hash store     — file duplicate memory (now Delta table)
  4. Claim dup store — cross-upload claim change tracking (now Delta table)
  5. Audit log & export table — also now Delta tables

NOTE: only the hash_store / claim_dup_store / audit_log / export_table
functions changed storage backend. clear_session_cache() and
clear_parsed_cache() are untouched -- claims_json/ parsed cache still
lives on disk (a Volume once you point FEATURE_STORE_PATH there), it's
a separate concern from the 4 JSON-store tables.
"""

import glob
import os

from config.settings import FEATURE_STORE_PATH

HASH_STORE_TABLE = "documentsignalhub.feature_store.hash_store"
CLAIM_DUP_TABLE = "documentsignalhub.feature_store.claim_dup_store"
AUDIT_LOG_TABLE = "documentsignalhub.feature_store.audit_log"
EXPORT_TABLE = "documentsignalhub.feature_store.json_export_table"


# ── Individual clear functions ────────────────────────────────────────────────

def clear_session_cache(session_state) -> int:
    """
    Clears all runtime UI state from st.session_state.
    Preserves user preferences (conf_threshold, active_schema etc.).
    Returns number of keys cleared.
    """
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
    """
    Deletes all cached parsed JSON files from feature_store/claims_json/.
    Returns (files_deleted, bytes_freed).
    Unchanged from before -- still file-based, point FEATURE_STORE_PATH
    at your Volume path once you migrate uploads there.
    """
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
    """Resets the file hash store so all files are treated as new."""
    try:
        count = spark.table(HASH_STORE_TABLE).count()
        spark.sql(f"DELETE FROM {HASH_STORE_TABLE}")
        return count
    except Exception:
        return 0


def clear_claim_dup_store() -> int:
    """Resets the claim duplicate store."""
    try:
        count = spark.table(CLAIM_DUP_TABLE).count()
        spark.sql(f"DELETE FROM {CLAIM_DUP_TABLE}")
        return count
    except Exception:
        return 0


def clear_audit_log() -> int:
    """Clears the audit log."""
    try:
        count = spark.table(AUDIT_LOG_TABLE).count()
        spark.sql(f"DELETE FROM {AUDIT_LOG_TABLE}")
        return count
    except Exception:
        return 0


def clear_export_table() -> int:
    """Clears the JSON export history table."""
    try:
        count = spark.table(EXPORT_TABLE).count()
        spark.sql(f"DELETE FROM {EXPORT_TABLE}")
        return count
    except Exception:
        return 0


# ── Stats helpers ─────────────────────────────────────────────────────────────

def get_cache_stats() -> dict:
    """
    Returns current size/count for each cache layer.
    """
    stats = {}

    parsed_files = glob.glob(os.path.join(FEATURE_STORE_PATH, "*.json"))
    parsed_bytes = sum(os.path.getsize(f) for f in parsed_files if os.path.exists(f))
    stats["parsed"] = {
        "files": len(parsed_files),
        "size_kb": round(parsed_bytes / 1024, 1),
    }

    try:
        stats["hash_store"] = {"entries": spark.table(HASH_STORE_TABLE).count()}
    except Exception:
        stats["hash_store"] = {"entries": 0}

    try:
        stats["claim_dups"] = {"entries": spark.table(CLAIM_DUP_TABLE).count()}
    except Exception:
        stats["claim_dups"] = {"entries": 0}

    try:
        stats["audit_log"] = {"entries": spark.table(AUDIT_LOG_TABLE).count()}
    except Exception:
        stats["audit_log"] = {"entries": 0}

    try:
        stats["export_table"] = {"entries": spark.table(EXPORT_TABLE).count()}
    except Exception:
        stats["export_table"] = {"entries": 0}

    return stats


def _fmt_size(kb: float) -> str:
    if kb >= 1024:
        return f"{kb / 1024:.1f} MB"
    return f"{kb:.1f} KB"
