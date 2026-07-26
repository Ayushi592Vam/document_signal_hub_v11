"""
modules/delta_io.py
Delta table read/write via the SQL warehouse, using the same app
service-principal identity modules/volume_io.py already uses for the
Files API -- just pointed at a SQL warehouse instead of a Volume.

Append-only tables (audit_log, llm_cost_log, adi_cost_log) go through
append_row()/append_rows(). hash_store and claim_dup_store are keyed
lookups, not append-only logs -- these use upsert_row()/get_row()/
delete_row() for single-key operations, and delete_all_rows() for the
cache-clear buttons that wipe an entire table.
"""

import os
from databricks import sql
from databricks.sdk.core import Config

_cfg = None


def _get_config() -> Config:
    global _cfg
    if _cfg is None:
        _cfg = Config()
    return _cfg


def _get_connection():
    warehouse_id = os.environ.get("DATABRICKS_WAREHOUSE_ID", "")
    if not warehouse_id:
        raise RuntimeError(
            "DATABRICKS_WAREHOUSE_ID is not set -- add the SQL warehouse "
            "as an App resource in the Databricks Apps UI first."
        )
    cfg = _get_config()
    return sql.connect(
        server_hostname=cfg.host,
        http_path=f"/sql/1.0/warehouses/{warehouse_id}",
        credentials_provider=lambda: cfg.authenticate,
    )


def append_row(table: str, row: dict) -> None:
    """Insert one row. Values are passed as bind parameters -- never
    string-formatted into the SQL -- regardless of what a filename or
    details string contains."""
    append_rows(table, [row])


def append_rows(table: str, rows: list[dict]) -> None:
    """Batch insert -- one Delta commit for the whole list instead of
    one commit per row. This matters more on Delta than it did on JSON:
    every single-row INSERT creates its own small file/commit, and Delta
    tables degrade under a high rate of tiny writes ("small file
    problem"). Always prefer this over calling append_row() in a loop."""
    if not rows:
        return
    cols = list(rows[0].keys())
    col_list = ", ".join(cols)
    placeholders = ", ".join(f"%({c})s" for c in cols)
    query = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"
    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.executemany(query, rows)
    finally:
        conn.close()


def read_rows(table: str, where: str = "", limit: int | None = None) -> list[dict]:
    query = f"SELECT * FROM {table}"
    if where:
        query += f" WHERE {where}"
    if limit:
        query += f" LIMIT {limit}"
    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(query)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def count_rows(table: str, where: str = "") -> int:
    query = f"SELECT COUNT(*) FROM {table}"
    if where:
        query += f" WHERE {where}"
    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(query)
            return cur.fetchone()[0]
    finally:
        conn.close()

def upsert_row(table: str, key_col: str, row: dict) -> None:
    """
    Upsert a single row by key_col using Delta MERGE INTO. Used for
    KEYED stores (hash_store keyed by file_hash, claim_dup_store keyed
    by dup_key) where the record for a given key must be REPLACED, not
    appended -- unlike audit_log/cost logs, which are pure append-only
    and never need this.
    """
    cols = list(row.keys())
    set_clause   = ", ".join(f"t.{c} = s.{c}" for c in cols if c != key_col)
    insert_cols  = ", ".join(cols)
    insert_vals  = ", ".join(f"s.{c}" for c in cols)
    select_cols  = ", ".join(f"%({c})s AS {c}" for c in cols)

    query = f"""
        MERGE INTO {table} AS t
        USING (SELECT {select_cols}) AS s
        ON t.{key_col} = s.{key_col}
        WHEN MATCHED THEN UPDATE SET {set_clause}
        WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals})
    """
    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(query, row)
    finally:
        conn.close()


def get_row(table: str, key_col: str, key_val: str) -> dict | None:
    """Single-key point lookup -- one indexed row read, not a full-table
    scan. Prefer this over read_rows() whenever you only need one key."""
    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {table} WHERE {key_col} = %({key_col})s", {key_col: key_val})
            cols = [d[0] for d in cur.description]
            row = cur.fetchone()
            return dict(zip(cols, row)) if row else None
    finally:
        conn.close()


def delete_row(table: str, key_col: str, key_val: str) -> None:
    """
    Single-key point delete -- removes exactly one row by key_col,
    rather than the entire table (see delete_all_rows() below for that).

    Used by per-record "clear this one item" UI actions -- e.g. the
    claim_dup_panel.py "Clear duplicate history for this claim" button --
    where wiping the whole claim_dup_store table would be destructive
    overkill for what the user actually asked for.
    """
    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {table} WHERE {key_col} = %({key_col})s",
                {key_col: key_val},
            )
    finally:
        conn.close()


def delete_all_rows(table: str) -> None:
    """Full-table delete -- used by the cache-clear buttons. Creates a
    new Delta table version rather than mutating rows in place."""
    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {table}")
    finally:
        conn.close()
