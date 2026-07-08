"""
modules/db_connection.py
Shared Databricks SQL Warehouse connection helper.

Used by audit.py, claim_dup_store.py, storage.py, json_export_table.py,
and cache_manager.py -- anywhere that needs to read/write the Delta tables
in documentsignalhub.feature_store.

Auth: relies on the app's own service principal identity (set up when you
attached the SQL warehouse as a resource to this Databricks App). No token
handling needed -- Config() auto-detects credentials from the app's
environment.

Requires DATABRICKS_WAREHOUSE_HTTP_PATH to be set as an environment
variable on the app (Connection details tab of your SQL warehouse).
"""

import os

from databricks import sql
from databricks.sdk.core import Config

_HTTP_PATH = os.getenv("DATABRICKS_WAREHOUSE_HTTP_PATH", "")


def get_connection():
    """Returns a new connection to the SQL warehouse. Use as a context
    manager: `with get_connection() as conn: ...`"""
    cfg = Config()
    return sql.connect(
        server_hostname=cfg.host,
        http_path=_HTTP_PATH,
        credentials_provider=lambda: cfg.authenticate,
    )
