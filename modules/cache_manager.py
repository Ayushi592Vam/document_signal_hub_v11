"""
modules/cache_manager.py
Centralised cache-clearing utilities for the TPA Loss Run Parser.
Handles all 4 cache layers:
  1. Streamlit session state (UI state, selections, modified values)
  2. Feature store — parsed JSON cache (claims_json/)
  3. Hash store     — file duplicate memory (hash_store.json)
  4. Claim dup store — cross-upload claim change tracking (claim_dup_store.json)

MIGRATION NOTE: all Volume access now goes through modules/volume_io.py
(Files API) instead of plain open()/os calls.
"""

from config.settings import (
    FEATURE_STORE_PATH,
    HASH_STORE_PATH,
    CLAIM_DUP_STORE_PATH,
    AUDIT_LOG_PATH,
    JSON_EXPORT_TABLE_PATH,
)
from modules.volume_io import load_json, save_json, _get_client


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
    """
    Deletes cached parsed JSON files (feature_store/*.json) via the Files
    API. Returns (files_deleted, bytes_freed) -- bytes_freed is always 0
    now since the Files API list endpoint doesn't return size cheaply;
    only the count is meaningful.
    """
    try:
        entries = list(_get_client().files.list_directory_contents(FEATURE_STORE_PATH))
    except Exception:
        return 0, 0

    deleted = 0
    for entry in entries:
        if entry.path.endswith(".json"):
            try:
                _get_client().files.delete(entry.path)
                deleted += 1
            except Exception:
                pass
    return deleted, 0


def clear_hash_store() -> int:
    data = load_json(HASH_STORE_PATH, default={})
    count = len(data)
    save_json(HASH_STORE_PATH, {})
    return count


def clear_claim_dup_store() -> int:
    data = load_json(CLAIM_DUP_STORE_PATH, default={})
    count = len(data)
    save_json(CLAIM_DUP_STORE_PATH, {})
    return count


def clear_audit_log() -> int:
    data = load_json(AUDIT_LOG_PATH, default=[])
    count = len(data)
    save_json(AUDIT_LOG_PATH, [])
    return count


def clear_export_table() -> int:
    data = load_json(JSON_EXPORT_TABLE_PATH, default=[])
    count = len(data)
    save_json(JSON_EXPORT_TABLE_PATH, [])
    return count


# ── Stats helpers ─────────────────────────────────────────────────────────────

def get_cache_stats() -> dict:
    stats = {}

    try:
        entries = list(_get_client().files.list_directory_contents(FEATURE_STORE_PATH))
        json_entries = [e for e in entries if e.path.endswith(".json")]
        stats["parsed"] = {"files": len(json_entries), "size_kb": 0.0}
    except Exception:
        stats["parsed"] = {"files": 0, "size_kb": 0.0}

    stats["hash_store"] = {"entries": len(load_json(HASH_STORE_PATH, default={}))}
    stats["claim_dups"] = {"entries": len(load_json(CLAIM_DUP_STORE_PATH, default={}))}
    stats["audit_log"] = {"entries": len(load_json(AUDIT_LOG_PATH, default=[]))}
    stats["export_table"] = {"entries": len(load_json(JSON_EXPORT_TABLE_PATH, default=[]))}

    return stats


def _fmt_size(kb: float) -> str:
    if kb >= 1024:
        return f"{kb / 1024:.1f} MB"
    return f"{kb:.1f} KB"
