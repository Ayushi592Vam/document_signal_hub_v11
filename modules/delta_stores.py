"""
Delta-table replacements for the feature_store JSON files.

These assume they run somewhere with an active Spark session (a Databricks
notebook, a Databricks App with Databricks Connect, or a job cluster) --
that's why everything is on hold until the warehouse/cluster credits issue
is resolved. Once it is, swap your JSON open()/json.load()/json.dump() calls
in modules/ for the matching function below.

Table names assume: documentsignalhub.feature_store.<table>
"""

import json
from datetime import datetime
from pyspark.sql import Row

CATALOG = "documentsignalhub"
SCHEMA = "feature_store"


# ---------- audit_log (replaces audit.json) ----------

def append_audit_event(event: str, filename: str, details: dict):
    """Replaces: appending a dict to the audit.json list."""
    row = Row(
        event=event,
        event_time=datetime.now(),
        filename=filename,
        details=json.dumps(details),
    )
    df = spark.createDataFrame([row])
    df.write.format("delta").mode("append").saveAsTable(
        f"{CATALOG}.{SCHEMA}.audit_log"
    )


def load_audit_events(filename: str = None):
    """Replaces: json.load(open('audit.json')). Returns list of dicts,
    same shape as before (details fields flattened back in)."""
    df = spark.table(f"{CATALOG}.{SCHEMA}.audit_log")
    if filename:
        df = df.filter(df.filename == filename)
    results = []
    for r in df.collect():
        record = {"event": r.event, "timestamp": r.event_time.isoformat(), "filename": r.filename}
        record.update(json.loads(r.details))
        results.append(record)
    return results


# ---------- hash_store (replaces hash_store.json) ----------

def check_hash_exists(file_hash: str):
    """Replaces: file_hash in hash_store_dict. Returns record dict or None."""
    df = spark.table(f"{CATALOG}.{SCHEMA}.hash_store").filter(
        f"file_hash = '{file_hash}'"
    )
    rows = df.collect()
    if not rows:
        return None
    r = rows[0]
    return {
        "filename": r.filename,
        "first_seen": r.first_seen.isoformat(),
        "sheet_hashes": json.loads(r.sheet_hashes),
    }


def add_hash_record(file_hash: str, filename: str, sheet_hashes: dict):
    """Replaces: hash_store_dict[file_hash] = {...}; save to json."""
    row = Row(
        file_hash=file_hash,
        filename=filename,
        first_seen=datetime.now(),
        sheet_hashes=json.dumps(sheet_hashes),
    )
    df = spark.createDataFrame([row])
    df.write.format("delta").mode("append").saveAsTable(
        f"{CATALOG}.{SCHEMA}.hash_store"
    )


# ---------- claim_dup_store (replaces claim_dup_store.json) ----------
# NOTE: your sample was empty ({}). This assumes a simple key->record shape
# matching hash_store's pattern. If claim_dup_store.py builds something
# different, share its structure once populated and I'll adjust.

def get_dup_record(dup_key: str):
    df = spark.table(f"{CATALOG}.{SCHEMA}.claim_dup_store").filter(
        f"dup_key = '{dup_key}'"
    )
    rows = df.collect()
    if not rows:
        return None
    return json.loads(rows[0].record)


def set_dup_record(dup_key: str, record: dict):
    row = Row(dup_key=dup_key, record=json.dumps(record), last_updated=datetime.now())
    df = spark.createDataFrame([row])
    df.write.format("delta").mode("append").saveAsTable(
        f"{CATALOG}.{SCHEMA}.claim_dup_store"
    )


# ---------- json_export_table (replaces json_export_table.json) ----------

def append_export_record(filename: str, sheet: str, export_type: str,
                          record_count: int, export_json: dict):
    """Replaces: appending a dict to json_export_table.json list."""
    row = Row(
        filename=filename,
        sheet=sheet,
        export_time=datetime.now(),
        export_type=export_type,
        record_count=record_count,
        export_json=json.dumps(export_json),
    )
    df = spark.createDataFrame([row])
    df.write.format("delta").mode("append").saveAsTable(
        f"{CATALOG}.{SCHEMA}.json_export_table"
    )


def get_export_records(filename: str = None):
    """Replaces: json.load(open('json_export_table.json'))."""
    df = spark.table(f"{CATALOG}.{SCHEMA}.json_export_table")
    if filename:
        df = df.filter(df.filename == filename)
    results = []
    for r in df.collect():
        results.append({
            "filename": r.filename,
            "sheet": r.sheet,
            "timestamp": r.export_time.isoformat(),
            "type": r.export_type,
            "record_count": r.record_count,
            "json": r.export_json,
        })
    return results
