"""
modules/cache_manager.py
Centralised cache-clearing utilities.

Layer status after tonight's migration:
  1. Streamlit session state       — unchanged, in-memory
  2. Feature store (parsed sheets) — unchanged, Volume/JSON (Files API)
  3. Hash store                    — Delta (documentsignalhub.feature_store.hash_store)
  4. Claim dup store                — Delta (documentsignalhub.feature_store.claim_dup_store)
  5. Audit log                      — Delta (documentsignalhub.feature_store.audit_log)
  6. LLM / ADI cost logs             — Delta
  7. JSON export table               — still Volume/JSON, not migrated
"""

from config.settings import FEATURE_STORE_PATH, JSON_EXPORT_TABLE_PATH
from modules.volume_io import load_json, save_json, _get_client
from modules.delta_io import count_rows, delete_all_rows

_HASH_STORE_TABLE   = "documentsignalhub.feature_store.hash_store"
_CLAIM_DUP_TABLE    = "documentsignalhub.feature_store.claim_dup_store"
_AUDIT_LOG_TABLE    = "documentsignalhub.feature_store.audit_log"
_LLM_COST_TABLE     = "documentsignalhub.feature_store.llm_cost_log"
_ADI_COST_TABLE     = "documentsignalhub.feature_store.adi_cost_log"


# ── Individual clear functions ────────────────────────────────────────────────

def clear_session_cache(session_state) -> int:
    KEEP_KEYS = {
        "conf_threshold", "use_conf_threshold", "active_schema",
        "schema_popup_target", "schema_popup_tab",
        "tmpdir", "last_uploaded", "sheet_names", "sheet_cache",
        "selected_idx", "focus_field",
        "current_file_hash", "sheet_hashes", "sheet_dup_info",
        "is_duplicate_file", "duplicate_first_seen", "duplicate_orig_name",
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
    try:
        count = count_rows(_HASH_STORE_TABLE)
        delete_all_rows(_HASH_STORE_TABLE)
        return count
    except Exception:
        return 0


def clear_claim_dup_store() -> int:
    try:
        count = count_rows(_CLAIM_DUP_TABLE)
        delete_all_rows(_CLAIM_DUP_TABLE)
        return count
    except Exception:
        return 0


def clear_audit_log() -> int:
    try:
        count = count_rows(_AUDIT_LOG_TABLE)
        delete_all_rows(_AUDIT_LOG_TABLE)
        return count
    except Exception:
        return 0


def clear_export_table() -> int:
    data = load_json(JSON_EXPORT_TABLE_PATH, default=[])
    count = len(data)
    save_json(JSON_EXPORT_TABLE_PATH, [])
    return count


def clear_llm_cost_log() -> int:
    try:
        count = count_rows(_LLM_COST_TABLE)
        delete_all_rows(_LLM_COST_TABLE)
        return count
    except Exception:
        return 0


def clear_adi_cost_log() -> int:
    try:
        count = count_rows(_ADI_COST_TABLE)
        delete_all_rows(_ADI_COST_TABLE)
        return count
    except Exception:
        return 0


# ── Stats helpers ─────────────────────────────────────────────────────────────

def get_cache_stats() -> dict:
    stats = {}

    try:
        entries = list(_get_client().files.list_directory_contents(FEATURE_STORE_PATH))
        json_entries = [e for e in entries if e.path.endswith(".json")]
        stats["parsed"] = {"files": len(json_entries), "size_kb": 0.0}
    except Exception:
        stats["parsed"] = {"files": 0, "size_kb": 0.0}

    for key, table in [
        ("hash_store",   _HASH_STORE_TABLE),
        ("claim_dups",   _CLAIM_DUP_TABLE),
        ("audit_log",    _AUDIT_LOG_TABLE),
        ("llm_cost_log", _LLM_COST_TABLE),
        ("adi_cost_log", _ADI_COST_TABLE),
    ]:
        try:
            stats[key] = {"entries": count_rows(table)}
        except Exception:
            stats[key] = {"entries": 0}

    stats["export_table"] = {"entries": len(load_json(JSON_EXPORT_TABLE_PATH, default=[]))}

    return stats


def _fmt_size(kb: float) -> str:
    if kb >= 1024:
        return f"{kb / 1024:.1f} MB"
    return f"{kb:.1f} KB"
