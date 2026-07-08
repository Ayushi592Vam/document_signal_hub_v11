"""
modules/audit.py
Append-only audit log helpers (Delta-backed via SQL Warehouse).
"""

import datetime
import json

from modules.db_connection import get_connection

AUDIT_TABLE = "documentsignalhub.feature_store.audit_log"
_CORE_KEYS = {"event", "timestamp", "filename"}


def _load_audit_log() -> list:
    log = []
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT event, event_time, filename, details FROM {AUDIT_TABLE} ORDER BY event_time")
        for row in cur.fetchall():
            entry = {
                "event": row.event,
                "timestamp": row.event_time.isoformat(),
                "filename": row.filename,
            }
            if row.details:
                entry.update(json.loads(row.details))
            log.append(entry)
    return log


def _save_audit_log(log: list) -> None:
    """Full overwrite -- used by clear_audit_log()."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(f"DELETE FROM {AUDIT_TABLE}")
        for entry in log:
            details = {k: v for k, v in entry.items() if k not in _CORE_KEYS}
            ts = entry.get("timestamp")
            cur.execute(
                f"INSERT INTO {AUDIT_TABLE} (event, event_time, filename, details) "
                f"VALUES (:event, :event_time, :filename, :details)",
                {
                    "event": entry.get("event"),
                    "event_time": datetime.datetime.fromisoformat(ts) if ts else datetime.datetime.now(),
                    "filename": entry.get("filename"),
                    "details": json.dumps(details),
                },
            )


def _append_audit(entry: dict) -> None:
    """Single-event append -- the hot path, called on every parse/ingest event."""
    details = {k: v for k, v in entry.items() if k not in _CORE_KEYS}
    ts = entry.get("timestamp")
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {AUDIT_TABLE} (event, event_time, filename, details) "
            f"VALUES (:event, :event_time, :filename, :details)",
            {
                "event": entry.get("event"),
                "event_time": datetime.datetime.fromisoformat(ts) if ts else datetime.datetime.now(),
                "filename": entry.get("filename"),
                "details": json.dumps(details),
            },
        )
