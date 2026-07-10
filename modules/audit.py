"""
modules/audit.py
Append-only audit log helpers -- backed by a Unity Catalog Volume via the
Databricks Files API (see modules/volume_io.py for why plain open()
against /Volumes doesn't work reliably in a Databricks App).

UPDATE: now supports start_time/end_time for duration-bearing events
(SHEET_PARSED, FILE_ENRICHED, etc.) alongside the original single
`timestamp` for instantaneous events (FILE_INGESTED). Also applies
redaction to sensitive field values before writing to the log.
"""

from config.settings import AUDIT_LOG_PATH
from modules.volume_io import load_json, save_json
from modules.guardrails import redact_details_dict

_CORE_KEYS = {"event", "timestamp", "filename", "start_time", "end_time",
              "duration_ms", "status", "error"}


def _load_audit_log() -> list:
    return load_json(AUDIT_LOG_PATH, default=[])


def _save_audit_log(log: list) -> None:
    save_json(AUDIT_LOG_PATH, log)


def _append_audit(entry: dict) -> None:
    """Original single-timestamp append -- unchanged behavior for
    instantaneous events. Redacts sensitive field values in `details`
    before writing."""
    details = {k: v for k, v in entry.items() if k not in _CORE_KEYS}
    details = redact_details_dict(details)
    clean_entry = {k: v for k, v in entry.items() if k in _CORE_KEYS}
    clean_entry.update(details)

    log = _load_audit_log()
    log.append(clean_entry)
    _save_audit_log(log)


def _append_audit_with_duration(entry: dict) -> None:
    """
    For duration-bearing events: entry should include start_time,
    end_time, and optionally duration_ms/status/error. Falls back to
    _append_audit() if start_time/end_time aren't both present.
    """
    if "start_time" not in entry or "end_time" not in entry:
        _append_audit(entry)
        return
    _append_audit(entry)  # same storage path -- redaction + core-key split applies identically
