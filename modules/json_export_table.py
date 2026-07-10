"""
modules/json_export_table.py
Tracks every generated JSON export (upsert by filename+sheet+type).

UPDATE: now carries `cost_metadata` alongside each export record, so
LLM token/cost data persists with the data it produced instead of living
only in ephemeral st.session_state["_llm_cost_log"].
"""

from modules.volume_io import load_json, save_json
from config.settings import JSON_EXPORT_TABLE_PATH


def _load_json_export_table() -> list:
    return load_json(JSON_EXPORT_TABLE_PATH, default=[])


def _save_json_export_table(table: list) -> None:
    save_json(JSON_EXPORT_TABLE_PATH, table)


def _append_json_export(entry: dict, cost_metadata: dict | None = None) -> None:
    """
    entry: the existing export record shape (filename, sheet, timestamp,
    type, record_count, json).
    cost_metadata: optional dict, e.g.
        {
          "calls": 3,
          "prompt_tokens": 1450,
          "completion_tokens": 320,
          "total_cost_usd": 0.0071,
          "models_used": ["gpt-4.1-mini"],
        }
    Pass st.session_state.get("_llm_cost_log", []) filtered to entries
    for this doc_name, summed into the shape above, at the call site.
    """
    if cost_metadata is not None:
        entry = {**entry, "cost_metadata": cost_metadata}

    table = _load_json_export_table()
    for existing in table:
        if (
            existing.get("filename") == entry.get("filename")
            and existing.get("sheet") == entry.get("sheet")
            and existing.get("type") == entry.get("type")
        ):
            existing.update(entry)
            _save_json_export_table(table)
            return
    table.append(entry)
    _save_json_export_table(table)
