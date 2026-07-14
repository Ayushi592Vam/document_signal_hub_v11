"""
modules/classification_router.py

Routes document classification decisions with a cheap keyword pre-pass
before falling back to the LLM classifier, and applies confidence-based
routing to flag low-confidence documents for manual review instead of
silently auto-processing them.

WHY THIS EXISTS: pdf_intelligence.py already had a call site expecting
this module -- `from modules.classification_router import
route_classification` -- but the file itself didn't exist anywhere in
the codebase, meaning every PDF/TXT/HTML upload would crash with
ModuleNotFoundError the moment run_pdf_intelligence() ran. This is a
new, functioning implementation of that missing module.

Return shape from route_classification():
    {
        "classification": "<FNOL|Legal|Loss Run|Medical|Underwriting>",
        "confidence":      <0.0-1.0>,
        "source":          "<keyword|llm|fallback>",
        "route":           "<auto_process|manual_review>",
    }
"""

from __future__ import annotations

from modules.doc_config import score_doc_type

# ── Tunable thresholds ────────────────────────────────────────────────────

# Keyword pre-pass: skip the LLM call entirely when one doc type's keyword
# score is both high enough and clearly ahead of the runner-up. This is
# the doc_config.score_doc_type() function that existed in the codebase
# but wasn't called from anywhere -- a zero-cost classification signal
# that was sitting unused.
KEYWORD_SKIP_MIN_SCORE  = 4
KEYWORD_SKIP_MIN_MARGIN = 3

# Below this confidence, route to manual_review instead of auto_process,
# regardless of which classification was returned or which method
# produced it.
MIN_CONFIDENCE_AUTO = 0.65

# Keyword-derived confidence is capped below the LLM's own ceiling, since
# a keyword hit-count is a cruder signal than an LLM reading the document.
# This keeps a keyword-only classification from ever outranking a
# genuinely uncertain LLM read.
KEYWORD_MAX_CONFIDENCE = 0.80


def _keyword_prepass(full_text: str) -> tuple[str | None, float]:
    """
    Runs the zero-cost keyword scorer across all configured doc types.
    Returns (best_doc_type, confidence) if one type is a clear, dominant
    winner; otherwise (None, 0.0) so the caller falls back to the LLM.
    """
    scores = score_doc_type(full_text)
    if not scores:
        return None, 0.0

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top_type, top_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0

    if top_score < KEYWORD_SKIP_MIN_SCORE:
        return None, 0.0
    if (top_score - second_score) < KEYWORD_SKIP_MIN_MARGIN:
        return None, 0.0

    # Confidence scales with how dominant the win is, capped below the
    # LLM's ceiling since a keyword count is a cruder signal than an LLM
    # actually reading the document.
    margin_ratio = (top_score - second_score) / max(top_score, 1)
    confidence = min(KEYWORD_MAX_CONFIDENCE, 0.55 + 0.25 * margin_ratio)

    return top_type, round(confidence, 2)


def route_classification(full_text: str, llm_classify_fn) -> dict:
    """
    Two-stage classification:
      1. Cheap keyword pre-pass (modules.doc_config.score_doc_type). If one
         doc type wins decisively, skip the LLM call entirely -- saves the
         cost and latency of a full classification call for documents
         that are unambiguous by keyword signal alone.
      2. Otherwise, call llm_classify_fn(full_text) -- the existing
         classify_document() in pdf_intelligence.py.

    Either way, confidence-based routing decides whether the document is
    auto-processed or flagged for manual review (result["needs_manual_review"]
    in run_pdf_intelligence()).
    """
    if not full_text or not full_text.strip():
        return {
            "classification": "Legal",
            "confidence":      0.0,
            "source":          "fallback",
            "route":           "manual_review",
        }

    kw_type, kw_confidence = _keyword_prepass(full_text)
    if kw_type is not None:
        route = "auto_process" if kw_confidence >= MIN_CONFIDENCE_AUTO else "manual_review"
        return {
            "classification": kw_type,
            "confidence":      kw_confidence,
            "source":          "keyword",
            "route":           route,
        }

    llm_result = llm_classify_fn(full_text) or {}
    classification = llm_result.get("classification", "Legal")
    confidence      = float(llm_result.get("confidence", 0.5))
    route = "auto_process" if confidence >= MIN_CONFIDENCE_AUTO else "manual_review"

    return {
        "classification": classification,
        "confidence":      confidence,
        "source":          "llm",
        "route":           route,
    }
