"""
modules/orchestrator.py
Lightweight orchestration wrapper -- does NOT replace app2.py's control
flow. It wraps individual steps (parsing, enrichment, LLM calls) so
lineage timing, retry, and output validation apply without a full
rewrite of the ingestion flow.

Three responsibilities:
  1. run_with_lineage()  -- wraps a call, records start/end timestamps
                             and duration to the audit log.
  2. with_retry()        -- generic retry wrapper. See docstring for
                             the retry policy and why it's shaped this way.
  3. classify_route()    -- confidence-based routing stub, superseded
                             by modules/classification_router.py for
                             the actual doc-type routing decision; kept
                             here as a generic reusable threshold check.
"""

import datetime
import time

from modules.audit import _append_audit_with_duration
from modules.guardrails import validate_extraction_output

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


def with_retry(
    fn,
    max_attempts: int = 2,
    backoff_seconds: float = 1.5,
    retry_on_empty: bool = True,
    retryable_exceptions: tuple = (Exception,),
):
    """
    Generic retry wrapper.

    RETRY POLICY (stated explicitly -- this is a design decision, not a
    silent default):
      - Retries when `fn()` returns a falsy/None value, since this
        codebase's LLM call wrapper (_llm_call in pdf_intelligence.py)
        already catches its own exceptions and returns None on failure
        rather than raising -- so "empty result" IS the failure signal
        for those calls, not an exception.
      - Also retries on any exception matching retryable_exceptions,
        for callers that do raise (e.g. network-level failures that
        escape the inner try/except).
      - max_attempts=2 by default: one retry, not an open-ended loop --
        bounded specifically to avoid runaway LLM cost from retries.
      - Exponential backoff: backoff_seconds * (2 ** attempt_index).

    Does NOT retry on a genuinely successful-but-empty result if
    retry_on_empty=False is passed -- use this for calls where an empty
    dict/list is a valid, expected outcome rather than a failure.

    Usage:
        result = with_retry(lambda: _llm_call(system_prompt=..., ...), max_attempts=2)
    """
    last_result = None
    last_exc = None

    for attempt in range(max_attempts):
        try:
            result = fn()
            if result or not retry_on_empty:
                return result
            last_result = result
        except retryable_exceptions as exc:
            last_exc = exc

        if attempt < max_attempts - 1:
            time.sleep(backoff_seconds * (2 ** attempt))

    if last_exc is not None:
        raise last_exc
    return last_result


def classify_route(confidence: float | None) -> str:
    """Generic confidence-based routing check. classification_router.py
    has the real doc-type routing logic; this is kept as a reusable
    threshold helper for other confidence-gated decisions."""
    if confidence is None:
        return "manual_review"
    return "auto_process" if confidence >= CONFIDENCE_ROUTING_THRESHOLD else "manual_review"


def validated_extraction(fn, *args, **kwargs) -> tuple[dict, bool, str]:
    """Runs an extraction function and validates its output shape.
    Returns (result, ok, reason)."""
    result = fn(*args, **kwargs)
    ok, reason = validate_extraction_output(result)
    return result, ok, reason
