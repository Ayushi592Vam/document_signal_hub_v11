"""
modules/cache_manager.py
Centralised cache-clearing utilities for the TPA Loss Run Parser.
Handles all 4 cache layers:
  1. Streamlit session state (UI state, selections, modified values)
  2. Feature store — parsed JSON cache (claims_json/)
  3. Hash store     — file duplicate memory (hash_store.json)
  4. Claim dup store — cross-upload claim change tracking (claim_dup_store.json)

MIGRATION NOTE: all paths now point at a Unity Catalog Volume instead of
local disk (config/settings.py). No compute needed -- this is file I/O.
"""

import glob
import json
import os

from config.settings import (
    FEATURE_STORE_PATH,
    HASH_STORE_PATH,
    CLAIM_DUP_STORE_PATH,
    AUDIT_LOG_PATH,
    JSON_EXPORT_TABLE_PATH,
)


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
    try:
        if not os.path.exists(HASH_STORE_PATH):
            return 0
        with open(HASH_STORE_PATH) as f:
            data = json.load(f)
        count = len(data)
        with open(HASH_STORE_PATH, "w") as f:
            json.dump({}, f)
        return count
    except Exception:
        return 0


def clear_claim_dup_store() -> int:
    try:
        if not os.path.exists(CLAIM_DUP_STORE_PATH):
            return 0
        with open(CLAIM_DUP_STORE_PATH) as f:
            data = json.load(f)
        count = len(data)
        with open(CLAIM_DUP_STORE_PATH, "w") as f:
            json.dump({}, f)
        return count
    except Exception:
        return 0


def clear_audit_log() -> int:
    try:
        if not os.path.exists(AUDIT_LOG_PATH):
            return 0
        with open(AUDIT_LOG_PATH) as f:
            data = json.load(f)
        count = len(data)
        with open(AUDIT_LOG_PATH, "w") as f:
            json.dump([], f)
        return count
    except Exception:
        return 0


def clear_export_table() -> int:
    try:
        if not os.path.exists(JSON_EXPORT_TABLE_PATH):
            return 0
        with open(JSON_EXPORT_TABLE_PATH) as f:
            data = json.load(f)
        count = len(data)
        with open(JSON_EXPORT_TABLE_PATH, "w") as f:
            json.dump([], f)
        return count
    except Exception:
        return 0


# ── Stats helpers ─────────────────────────────────────────────────────────────

def get_cache_stats() -> dict:
    stats = {}

    parsed_files = glob.glob(os.path.join(FEATURE_STORE_PATH, "*.json"))
    parsed_bytes = sum(os.path.getsize(f) for f in parsed_files if os.path.exists(f))
    stats["parsed"] = {
        "files": len(parsed_files),
        "size_kb": round(parsed_bytes / 1024, 1),
    }

    try:
        with open(HASH_STORE_PATH) as f:
            hs = json.load(f)
        stats["hash_store"] = {"entries": len(hs)}
    except Exception:
        stats["hash_store"] = {"entries": 0}

    try:
        with open(CLAIM_DUP_STORE_PATH) as f:
            cd = json.load(f)
        stats["claim_dups"] = {"entries": len(cd)}
    except Exception:
        stats["claim_dups"] = {"entries": 0}

    try:
        with open(AUDIT_LOG_PATH) as f:
            al = json.load(f)
        stats["audit_log"] = {"entries": len(al)}
    except Exception:
        stats["audit_log"] = {"entries": 0}

    try:
        with open(JSON_EXPORT_TABLE_PATH) as f:
            et = json.load(f)
        stats["export_table"] = {"entries": len(et)}
    except Exception:
        stats["export_table"] = {"entries": 0}

    return stats


def _fmt_size(kb: float) -> str:
    if kb >= 1024:
        return f"{kb / 1024:.1f} MB"
    return f"{kb:.1f} KB"
