"""
modules/volume_io.py
Read/write JSON files in a Unity Catalog Volume via the Databricks Files
REST API, instead of plain os/open() filesystem calls.

WHY: Databricks Apps containers don't reliably FUSE-mount /Volumes/... as
a real filesystem path -- this is a known platform gap, not a bug in our
code. Plain open()/os.makedirs() against Volume paths can raise
PermissionError: [Errno 13] Permission denied: '/Volumes'. The Files API
sidesteps this entirely since it never touches the local filesystem --
it's a REST call authenticated with the app's own service principal
(same Config() pattern used elsewhere in this app).
"""

import io
import json

from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config
from databricks.sdk.errors import NotFound

_client = None


def _get_client() -> WorkspaceClient:
    global _client
    if _client is None:
        _client = WorkspaceClient(config=Config())
    return _client


def load_json(volume_path: str, default):
    """Reads a JSON file from a Volume path. Returns `default` if the
    file doesn't exist yet or can't be parsed -- mirrors the old
    try/except-around-open() behavior."""
    try:
        resp = _get_client().files.download(volume_path)
        raw = resp.contents.read()
        return json.loads(raw.decode("utf-8"))
    except NotFound:
        return default
    except Exception:
        return default


def save_json(volume_path: str, data) -> None:
    """Writes `data` as JSON to a Volume path, overwriting if it exists."""
    payload = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    _get_client().files.upload(volume_path, io.BytesIO(payload), overwrite=True)
