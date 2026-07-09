"""
config/settings.py
Application-wide constants and session-state defaults.
"""

import os

# ── Feature-store paths ───────────────────────────────────────────────────────
FEATURE_STORE_PATH     = "/Volumes/documentsignalhub/raw_documents/files/feature_store"
HASH_STORE_PATH         = "/Volumes/documentsignalhub/raw_documents/files/hash_store.json"
CLAIM_DUP_STORE_PATH    = "/Volumes/documentsignalhub/raw_documents/files/claim_dup_store.json"
AUDIT_LOG_PATH          = "/Volumes/documentsignalhub/raw_documents/files/audit_log.json"
JSON_EXPORT_TABLE_PATH  = "/Volumes/documentsignalhub/raw_documents/files/json_export_table.json"

# ── Claim-level duplicate store ───────────────────────────────────────────────
#CLAIM_DUP_STORE_PATH   = "feature_store/claim_dup_store.json"

#os.makedirs(FEATURE_STORE_PATH, exist_ok=True)
#os.makedirs("feature_store", exist_ok=True)

# ── Config directory ──────────────────────────────────────────────────────────
CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config")

# ── Session-state defaults ────────────────────────────────────────────────────
SESSION_DEFAULTS: dict = {
    "sheet_names": [],
    "conf_threshold":      80,
    "use_conf_threshold":  False,
    "active_schema":       None,
    "schema_popup_target": None,
    "schema_popup_tab":    "required",
}

# ── Date-format → strftime map ────────────────────────────────────────────────
DATE_FMT_MAP: dict[str, str] = {
    "YYYY-MM-DD": "%Y-%m-%d",
    "MM/DD/YYYY": "%m/%d/%Y",
    "MM-DD-YYYY": "%m-%d-%Y",
    "DD/MM/YYYY": "%d/%m/%Y",
    "DD-MM-YYYY": "%d-%m-%Y",
}

# ── Minimum header-match score for rule-based field mapping ───────────────────
MIN_HEADER_MATCH: float = 0.65
