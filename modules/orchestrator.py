"""
modules/orchestrator.py
Lightweight orchestration wrapper -- does NOT replace app2.py's control
flow. It wraps individual steps (parsing, enrichment) so lineage timing
and output validation apply without a full rewrite of the ingestion flow.

Two responsibilities:
  1. run_with_lineage()  -- wraps a call, records start/end timestamps
                             and duration to the audit log. This is the
                             lineage mechanism referenced in the migration
                             doc: Unity Catalog's native Lineage tab
                             tracks table/notebook/job relationships, not
                             arbitrary Volume file writes, so lineage for
                             this app is implemented as audit events.
  2. classify_route()     -- confidence-based routing stub. Wire in the
                             real classification confidence once the
                             classifier code (doc_config.py /
                             pdf_intelligence.py) is available.
"""

import datetime
import time

from modules.audit import _append_audit_with_duration
from modules.guardrails import validate_extraction_output

# Below this confidence, a document routes to manual review instead of
# full auto-processing. Placeholder until wired to the real classifier.
CONFIDENCE_ROUTING_THRESHOLD = 0.55


def run_with_lineage(event_name: str, filename: str, fn, *args, **kwargs):
    """
    Wraps any callable with lineage timing.

    Usage:
        result = run_with_lineage("FILE_ENRICHED", filename, run_pdf_intelligence, parsed)
    """
    start = datetime.datetime.now()
    start_perf = time.perf_counter()
    error = None
    try:
        result = fn(*args, **kwargs)
        return result
    except Exception as exc:
        error = str(exc)
        raise
    finally:
        end = datetime.datetime.now()
        duration_ms = round((time.perf_counter() - start_perf) * 1000, 1)
        _append_audit_with_duration({
            "event":       event_name,
            "filename":    filename,
            "start_time":  start.isoformat(),
            "end_time":    end.isoformat(),
            "duration_ms": duration_ms,
            "status":      "error" if error else "success",
            **({"error": error} if error else {}),
        })


def classify_route(confidence: float | None) -> str:
    """
    Confidence-based routing decision: "auto_process" or "manual_review".
    Placeholder threshold check -- not the classifier itself.
    """
    if confidence is None:
        return "manual_review"
    return "auto_process" if confidence >= CONFIDENCE_ROUTING_THRESHOLD else "manual_review"


def validated_extraction(fn, *args, **kwargs) -> tuple[dict, bool, str]:
    """Runs an extraction function and validates its output shape.
    Returns (result, ok, reason)."""
    result = fn(*args, **kwargs)
    ok, reason = validate_extraction_output(result)
    return result, ok, reason
