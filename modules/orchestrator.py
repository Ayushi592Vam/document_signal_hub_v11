"""
modules/orchestrator.py
Lightweight orchestration wrapper -- does NOT replace app2.py's control
flow. It wraps individual steps (parsing, enrichment, LLM calls) so
lineage timing, retry, and output validation apply without a full
rewrite of the ingestion flow.

Five responsibilities:
  1. run_with_lineage()  -- wraps a call, records start/end timestamps
                             and a DECOMPOSED execution-cost breakdown
                             (parser time / API time / orchestration
                             overhead) to the audit log, instead of one
                             opaque duration_ms number.
  2. track_cost()        -- context manager used *inside* a
                             run_with_lineage()-wrapped call to attribute
                             a sub-step's wall-clock time to a specific
                             cost category. See its docstring for usage.
  3. with_retry()        -- generic retry wrapper. See docstring for
                             the retry policy and why it's shaped this way.
  4. classify_route()    -- confidence-based routing stub, superseded
                             by modules/classification_router.py for
                             the actual doc-type routing decision; kept
                             here as a generic reusable threshold check.
  5. summarize_cost_breakdown() -- aggregation function. Reads lineage
                             entries back out of the audit log and rolls
                             up parser/api/orchestration time -- in total
                             and per event -- so a dashboard (e.g. the
                             Cost Intelligence tab) can render a real
                             breakdown instead of a single aggregate
                             duration figure.
"""

import datetime
import threading
import time
from collections import defaultdict
from contextlib import contextmanager

from modules.audit import _append_audit_with_duration
from modules.guardrails import validate_extraction_output

CONFIDENCE_ROUTING_THRESHOLD = 0.55

# Thread-local stack of "frames" -- one per active run_with_lineage() call
# on this thread. Each frame accumulates category_totals so that a
# track_cost() call attributes time to whichever run_with_lineage() call
# is innermost/active at that moment. A stack (not a single frame) is
# required because lineage calls can nest -- e.g. FILE_ENRICHED wrapping
# a function that itself calls run_with_lineage() again for a sub-step.
_lineage_ctx = threading.local()


def _frame_stack() -> list:
    if not hasattr(_lineage_ctx, "stack"):
        _lineage_ctx.stack = []
    return _lineage_ctx.stack


@contextmanager
def track_cost(category: str):
    """
    Wrap a sub-step inside a run_with_lineage()-wrapped call to attribute
    its wall-clock time to a specific execution-cost category instead of
    letting it fall into the generic "orchestration" bucket.

    Only two categories are meaningful right now: "parser" and "api".
    Anything else you pass is still recorded but summarize_cost_breakdown()
    only knows to roll up these two by name -- everything NOT wrapped in
    track_cost() (retry backoff sleeps, JSON parsing/validation, dict
    reshaping, session-state bookkeeping) is counted as orchestration
    overhead automatically, as a remainder -- not something you opt into.

    Usage (inside a function passed to run_with_lineage, e.g. inside
    modules/pdf_intelligence.py's run_pdf_intelligence()):

        with track_cost("parser"):
            azure_result = parse_pdf_with_azure(path)

        with track_cost("api"):
            response = _llm_call(system_prompt=..., ...)

    If called outside any active run_with_lineage() frame (standalone
    script, unit test, etc.) this still executes the wrapped block --
    it just has no frame to record the measurement into, so the timing
    is silently discarded rather than raising.
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000
        stack = _frame_stack()
        if stack:
            stack[-1]["category_totals"][category] += elapsed_ms


def run_with_lineage(event_name: str, filename: str, fn, *args, **kwargs):
    """
    Wraps any callable with lineage timing AND a decomposed execution-cost
    breakdown.

    WHY THE BREAKDOWN EXISTS: previously this recorded a single aggregate
    duration_ms and nothing else, so a slow FILE_ENRICHED event gave no
    signal on WHY it was slow -- Azure DI parsing time, LLM API round-trip
    time, and this module's own glue code were all invisible inside one
    number. Now three numbers are recorded per lineage entry:

      - parser_ms         -- total time spent inside track_cost("parser")
                              blocks nested anywhere inside fn().
      - api_ms             -- total time spent inside track_cost("api")
                              blocks nested anywhere inside fn().
      - orchestration_ms  -- duration_ms - parser_ms - api_ms, clamped
                              to >= 0. This is *everything else*: retry
                              backoff sleeps, JSON parsing/validation,
                              dict reshaping, session-state bookkeeping.
                              It's computed as a remainder rather than
                              tracked explicitly so the three numbers
                              always sum to duration_ms exactly, even if
                              a caller forgets to instrument a new
                              sub-step with track_cost().

    NOTE: until call sites are instrumented with track_cost("parser") /
    track_cost("api") around their Azure DI calls and LLM calls, both
    will read 0 and 100% of duration_ms will show up as orchestration --
    that's expected, not a bug. Instrumentation is opt-in per call site.

    Nesting: if fn() itself calls another run_with_lineage()-wrapped
    function, each call gets its OWN frame and its own audit entry with
    its own breakdown. track_cost() only ever attributes to the innermost
    active frame, so time is never double-counted across nested lineage
    calls.

    Usage (call signature unchanged from before):
        result = run_with_lineage("FILE_ENRICHED", filename, run_pdf_intelligence, parsed)
    """
    start = datetime.datetime.now()
    start_perf = time.perf_counter()
    error = None

    frame = {"category_totals": defaultdict(float)}
    stack = _frame_stack()
    stack.append(frame)

    try:
        result = fn(*args, **kwargs)
        return result
    except Exception as exc:
        error = str(exc)
        raise
    finally:
        # Pop first, always -- even on exception -- so a failed call never
        # leaves a stale frame that would swallow a sibling call's timings.
        stack.pop()

        end = datetime.datetime.now()
        duration_ms = round((time.perf_counter() - start_perf) * 1000, 1)

        parser_ms = round(frame["category_totals"].get("parser", 0.0), 1)
        api_ms    = round(frame["category_totals"].get("api", 0.0), 1)
        # Remainder, clamped at 0 -- guards against float rounding drift
        # when parser_ms + api_ms comes out a hair over duration_ms.
        orchestration_ms = round(max(0.0, duration_ms - parser_ms - api_ms), 1)

        _append_audit_with_duration({
            "event":            event_name,
            "filename":         filename,
            "start_time":       start.isoformat(),
            "end_time":         end.isoformat(),
            "duration_ms":      duration_ms,
            "parser_ms":        parser_ms,
            "api_ms":           api_ms,
            "orchestration_ms": orchestration_ms,
            "status":           "error" if error else "success",
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


def _empty_cost_summary() -> dict:
    return {
        "call_count": 0, "total_ms": 0.0,
        "parser_ms": 0.0, "api_ms": 0.0, "orchestration_ms": 0.0,
        "parser_pct": 0.0, "api_pct": 0.0, "orchestration_pct": 0.0,
        "by_event": {},
    }


def summarize_cost_breakdown(
    filename: str | None = None,
    event_name: str | None = None,
) -> dict:
    """
    Aggregation function for the execution-cost breakdown recorded by
    run_with_lineage(). Reads lineage entries back out of the audit log
    (via modules.audit._load_audit_log -- the same helper the
    Transformation Journey tab already uses) and rolls them up into
    parser / api / orchestration totals. A dashboard should call this
    rather than re-deriving totals from raw audit rows itself.

    Args:
        filename:   optional -- restrict to lineage entries for one file
                    (matches the "filename" field exactly).
        event_name: optional -- restrict to one event type (e.g.
                    "FILE_ENRICHED") to isolate a single pipeline stage.

    Returns a dict:
        {
          "call_count":        int,    # matching lineage entries found
          "total_ms":          float,
          "parser_ms":         float,
          "api_ms":            float,
          "orchestration_ms":  float,
          "parser_pct":        float,  # % of total_ms
          "api_pct":           float,
          "orchestration_pct": float,
          "by_event": {
              "<event_name>": {
                  "call_count": int, "total_ms": float,
                  "parser_ms": float, "api_ms": float,
                  "orchestration_ms": float,
              }, ...
          },
        }

    Entries logged BEFORE this breakdown existed (no "parser_ms" key)
    are skipped rather than treated as 0 parser/api time -- counting
    them as 0 would silently and misleadingly inflate the orchestration
    share for historical data. If nothing matches (including "no audit
    log available"), returns an all-zero summary rather than raising, so
    callers can render an empty state without a try/except.
    """
    try:
        from modules.audit import _load_audit_log
    except ImportError:
        return _empty_cost_summary()

    try:
        entries = _load_audit_log()
    except Exception:
        return _empty_cost_summary()

    totals = {"parser_ms": 0.0, "api_ms": 0.0, "orchestration_ms": 0.0, "total_ms": 0.0}
    call_count = 0
    by_event: dict[str, dict] = {}

    for e in entries:
        if "duration_ms" not in e or "parser_ms" not in e:
            continue  # pre-breakdown entry -- see docstring
        if filename is not None and e.get("filename") != filename:
            continue
        if event_name is not None and e.get("event") != event_name:
            continue

        call_count += 1
        p = float(e.get("parser_ms", 0.0))
        a = float(e.get("api_ms", 0.0))
        o = float(e.get("orchestration_ms", 0.0))
        d = float(e.get("duration_ms", p + a + o))

        totals["parser_ms"] += p
        totals["api_ms"] += a
        totals["orchestration_ms"] += o
        totals["total_ms"] += d

        ev = e.get("event", "UNKNOWN")
        bucket = by_event.setdefault(ev, {
            "call_count": 0, "total_ms": 0.0,
            "parser_ms": 0.0, "api_ms": 0.0, "orchestration_ms": 0.0,
        })
        bucket["call_count"] += 1
        bucket["total_ms"] += d
        bucket["parser_ms"] += p
        bucket["api_ms"] += a
        bucket["orchestration_ms"] += o

    if call_count == 0:
        return _empty_cost_summary()

    total_ms_safe = totals["total_ms"] or 1.0  # guard divide-by-zero

    return {
        "call_count":        call_count,
        "total_ms":          round(totals["total_ms"], 1),
        "parser_ms":         round(totals["parser_ms"], 1),
        "api_ms":            round(totals["api_ms"], 1),
        "orchestration_ms":  round(totals["orchestration_ms"], 1),
        "parser_pct":        round(totals["parser_ms"] / total_ms_safe * 100, 1),
        "api_pct":           round(totals["api_ms"] / total_ms_safe * 100, 1),
        "orchestration_pct": round(totals["orchestration_ms"] / total_ms_safe * 100, 1),
        "by_event": {
            ev: {
                "call_count":       b["call_count"],
                "total_ms":         round(b["total_ms"], 1),
                "parser_ms":        round(b["parser_ms"], 1),
                "api_ms":           round(b["api_ms"], 1),
                "orchestration_ms": round(b["orchestration_ms"], 1),
            }
            for ev, b in by_event.items()
        },
    }
