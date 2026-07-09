"""
modules/audit.py
Append-only audit log helpers -- backed by a Unity Catalog Volume via the
Databricks Files API (see modules/volume_io.py for why plain open()
against /Volumes doesn't work reliably in a Databricks App).
"""

from config.settings import AUDIT_LOG_PATH
from modules.volume_io import load_json, save_json


def _load_audit_log() -> list:
    return load_json(AUDIT_LOG_PATH, default=[])


def _save_audit_log(log: list) -> None:
    save_json(AUDIT_LOG_PATH, log)


def _append_audit(entry: dict) -> None:
    log = _load_audit_log()
    log.append(entry)
    _save_audit_log(log)
