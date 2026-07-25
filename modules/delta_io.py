"""
modules/delta_io.py
Delta table read/write via the SQL warehouse, using the same app
service-principal identity modules/volume_io.py already uses for the
Files API -- just pointed at a SQL warehouse instead of a Volume.

Append-only tables (audit_log, llm_cost_log, adi_cost_log) go through
append_row()/append_rows(). hash_store and claim_dup_store are keyed
lookups, not append-only logs -- migrating those needs MERGE/upsert
logic and is intentionally NOT covered here; they remain on
modules/volume_io.py (JSON on the Volume) for now.
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
