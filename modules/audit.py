"""
modules/audit.py
Append-only audit log -- now backed by a Delta table via the SQL
warehouse (modules/delta_io.py) instead of a flat JSON file on the
Volume. See modules/volume_io.py's docstring for why the old JSON
approach existed; this replaces it for audit_log specifically.
"""

import datetime
import json

from modules.delta_io import append_row, read_rows
from modules.guardrails import redact_details_dict

_TABLE = "documentsignalhub.feature_store.audit_log"
_CORE_KEYS = {"event", "timestamp", "filename", "start_time", "end_time",
              "duration_ms", "status", "error"}


def _load_audit_log() -> list:
    rows = read_rows(_TABLE, limit=5000)
    out = []
    for r in rows:
        entry = {"event": r["event"], "timestamp": r["event_time"], "filename": r["filename"]}
        try:
            entry.update(json.loads(r["details"] or "{}"))
        except Exception:
            pass
        out.append(entry)
    return out


def _append_audit(entry: dict) -> None:
    details = {k: v for k, v in entry.items() if k not in _CORE_KEYS}
    details = redact_details_dict(details)
    clean_entry = {k: v for k, v in entry.items() if k in _CORE_KEYS}
    clean_entry.update(details)

    append_row(_TABLE, {
        "event":      clean_entry.get("event", ""),
        "event_time": clean_entry.get("timestamp", datetime.datetime.now().isoformat()),
        "filename":   clean_entry.get("filename", ""),
        "details":    json.dumps({k: v for k, v in clean_entry.items()
                                   if k not in ("event", "timestamp", "filename")}),
    })


def _append_audit_with_duration(entry: dict) -> None:
    if "start_time" not in entry or "end_time" not in entry:
        _append_audit(entry)
        return
    _append_audit(entry)


def _log_error(stage: str, filename: str, error: Exception | str, context: dict | None = None) -> None:
    entry = {
        "event":     "ERROR",
        "timestamp": datetime.datetime.now().isoformat(),
        "filename":  filename,
        "stage":     stage,
        "error":     str(error),
    }
    if context:
        entry.update(context)
    _append_audit(entry)
