"""
modules/audit.py
Append-only audit log helpers (Delta-backed).

Same public functions as before (_load_audit_log, _save_audit_log,
_append_audit) so nothing else in the codebase needs to change --
only the storage underneath is different now.
"""

import datetime
import json

from pyspark.sql import Row

AUDIT_TABLE = "documentsignalhub.feature_store.audit_log"

# Fields that get their own real column in the table; everything else
# an event carries (file_hash, sheets, sheet, sheet_hash, claim_rows,
# sheet_type, total_rows, total_cols, col_renames, claim_id,
# prev_filename, changed_fields, etc.) goes into `details` as JSON.
_CORE_KEYS = {"event", "timestamp", "filename"}


def _load_audit_log() -> list:
    """Returns the full audit log as a list of dicts, oldest first --
    same shape callers previously got from audit.json."""
    df = spark.table(AUDIT_TABLE).orderBy("event_time")
    log = []
    for r in df.collect():
        entry = {
            "event": r.event,
            "timestamp": r.event_time.isoformat(),
            "filename": r.filename,
        }
        if r.details:
            entry.update(json.loads(r.details))
        log.append(entry)
    return log


def _save_audit_log(log: list) -> None:
    """Full overwrite -- used by clear_audit_log() to wipe the table.
    Not used on the normal hot path; see _append_audit for that."""
    spark.sql(f"DELETE FROM {AUDIT_TABLE}")
    if not log:
        return
    rows = []
    for entry in log:
        details = {k: v for k, v in entry.items() if k not in _CORE_KEYS}
        ts = entry.get("timestamp")
        rows.append(Row(
            event=entry.get("event"),
            event_time=datetime.datetime.fromisoformat(ts) if ts else datetime.datetime.now(),
            filename=entry.get("filename"),
            details=json.dumps(details),
        ))
    spark.createDataFrame(rows).write.format("delta").mode("append").saveAsTable(AUDIT_TABLE)


def _append_audit(entry: dict) -> None:
    """Single-event append -- the hot path, called on every
    parse/ingest event throughout the app."""
    details = {k: v for k, v in entry.items() if k not in _CORE_KEYS}
    ts = entry.get("timestamp")
    row = Row(
        event=entry.get("event"),
        event_time=datetime.datetime.fromisoformat(ts) if ts else datetime.datetime.now(),
        filename=entry.get("filename"),
        details=json.dumps(details),
    )
    spark.createDataFrame([row]).write.format("delta").mode("append").saveAsTable(AUDIT_TABLE)
