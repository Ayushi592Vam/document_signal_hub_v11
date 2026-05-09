"""
ui/pdf_analysis.py  — v12

Changes from v11:
  ─────────────────────────────────────────────────────────────────────────
  FEATURE A — Delete button in Entities tab
    A 🗑 delete button is now shown beside the ✏️ edit and 👁 eye buttons
    for each extracted field. Clicking it removes the field from:
      • sheet_cache (raw Azure DI data)
      • _pdf_intelligence entities
      • _pdf_edits and _pdf_edit_hist
      • _intel_entities_ session cache
    A confirmation step (second click) prevents accidental deletion.
    User-added fields are marked with a distinct red trash icon.

  FEATURE B — User-added field popup uses a friendly "manually added" notice
    When the 👁 eye button is clicked on a field that was manually added
    by the user (_user_added=True), the popup now shows a clear
    "Manually Added Field" card instead of the LLM/ADI extraction warning.
    The no-bbox popup already had a user-added branch in _field_justification,
    but the heading still showed generic extraction details — this is now
    fully overridden with a dedicated user-added card.

  FEATURE C — LLM Cost Dashboard tab
    A new "💰 LLM Cost" tab is added after the Validation tab. It reads
    token usage data stored in st.session_state["_llm_cost_log"] (a list
    of dicts with keys: task, model, prompt_tokens, completion_tokens,
    timestamp). If the key is absent the tab shows an empty state with
    instructions on how to wire it up. A summary card + per-task breakdown
    table + running cost chart are rendered.

    Pricing constants (per 1K tokens) are defined in _LLM_PRICING and can
    be overridden via environment variables LLM_INPUT_PRICE and
    LLM_OUTPUT_PRICE.
  ─────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import datetime
import json
import os
import re

import streamlit as st


# ─────────────────────────────────────────────────────────────────────────────
# DOC-CONFIG IMPORT  — graceful degradation if not yet installed
# ─────────────────────────────────────────────────────────────────────────────

try:
    from modules.doc_config import (     # type: ignore[import]
        get_doc_type_meta  as _cfg_doc_type_meta,
        load_config        as _cfg_load,
    )
    _DOC_CONFIG_AVAILABLE = True
except ImportError:
    _DOC_CONFIG_AVAILABLE = False
    def _cfg_doc_type_meta(doc_type: str) -> dict:  # type: ignore[misc]
        return {}
    def _cfg_load(doc_type: str) -> dict:           # type: ignore[misc]
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

_DOC_TYPE_META_FALLBACK: dict[str, dict] = {
    "FNOL":     {"icon": "🚨", "color": "#dc2626", "bg": "rgba(220,38,38,0.06)"},
    "Legal":    {"icon": "⚖️",  "color": "#7c3aed", "bg": "rgba(124,58,237,0.06)"},
    "Loss Run": {"icon": "📊", "color": "#059669", "bg": "rgba(5,150,105,0.06)"},
    "Medical":  {"icon": "🏥", "color": "#2563eb", "bg": "rgba(37,99,235,0.06)"},
}

_SIGNAL_META_FALLBACK: dict[str, dict] = {
    "severity":           {"icon": "🔴", "label": "Severity",           "color": "#dc2626"},
    "legal_escalation":   {"icon": "⚖️",  "label": "Legal Escalation",   "color": "#7c3aed"},
    "fraud_indicator":    {"icon": "🚩", "label": "Fraud Indicator",    "color": "#d97706"},
    "medical_complexity": {"icon": "🏥", "label": "Medical Complexity", "color": "#2563eb"},
    "coverage_issue":     {"icon": "📋", "label": "Coverage Issue",     "color": "#b45309"},
}

_TAXONOMY = {
    "Highly Severe": {"color": "#dc2626", "bg": "rgba(220,38,38,0.06)",  "icon": "🔥"},
    "High":          {"color": "#ea580c", "bg": "rgba(234,88,12,0.06)",  "icon": "🔴"},
    "Moderate":      {"color": "#ca8a04", "bg": "rgba(202,138,4,0.06)",  "icon": "🟡"},
    "Low":           {"color": "#16a34a", "bg": "rgba(22,163,74,0.06)",  "icon": "🟢"},
}

# ── LLM Pricing (per 1K tokens) ───────────────────────────────────────────────
# Override via env vars LLM_INPUT_PRICE_PER_1K and LLM_OUTPUT_PRICE_PER_1K
# REPLACE the existing _LLM_PRICING dict with:
_LLM_PRICING: dict[str, dict[str, float]] = {
    # ── GPT-5.4 family (March 2026) ──────────────────────────────────────
    "gpt-5.4":         {"input": 0.0025,  "output": 0.015},
    "gpt-5.4-mini":    {"input": 0.00075, "output": 0.0045},
    "gpt-5.4-nano":    {"input": 0.0002,  "output": 0.00125},
    "gpt-5.4-pro":     {"input": 0.03,    "output": 0.18},
    # ── GPT-4.1 family ───────────────────────────────────────────────────
    "gpt-4.1":         {"input": 0.002,   "output": 0.008},
    "gpt-4.1-mini":    {"input": 0.0004,  "output": 0.0016},
    "gpt-4.1-nano":    {"input": 0.0001,  "output": 0.0004},
    # ── GPT-4o family ────────────────────────────────────────────────────
    "gpt-4o":          {"input": 0.005,   "output": 0.015},
    "gpt-4o-mini":     {"input": 0.00015, "output": 0.0006},
    # ── GPT-4 legacy ─────────────────────────────────────────────────────
    "gpt-4-turbo":     {"input": 0.01,    "output": 0.03},
    "gpt-4":           {"input": 0.03,    "output": 0.06},
    "gpt-35-turbo":    {"input": 0.0005,  "output": 0.0015},
    "gpt-3.5-turbo":   {"input": 0.0005,  "output": 0.0015},
    # ── Reasoning models ─────────────────────────────────────────────────
    "o3":              {"input": 0.002,   "output": 0.008},
    "o4-mini":         {"input": 0.0011,  "output": 0.0044},
    # ── Claude ───────────────────────────────────────────────────────────
    "claude-sonnet-4": {"input": 0.003,   "output": 0.015},
    "claude-3-5-sonnet": {"input": 0.003, "output": 0.015},
    "claude-3-haiku":  {"input": 0.00025, "output": 0.00125},
    # ── Default fallback ─────────────────────────────────────────────────
    "default":         {
        "input":  float(os.environ.get("LLM_INPUT_PRICE_PER_1K",  "0.0025")),
        "output": float(os.environ.get("LLM_OUTPUT_PRICE_PER_1K", "0.015")),
    },
}

# ── Light-theme colour tokens ─────────────────────────────────────────────────
_BG      = "#ffffff"
_BG2     = "#f8f9fa"
_BG3     = "#f1f3f5"
_BORDER  = "#e2e8f0"
_BORDER2 = "#cbd5e1"
_TXT     = "#0f172a"
_TXT2    = "#1e293b"
_LBL     = "#64748b"
_LBL2    = "#94a3b8"

_UPLOADER_PLUS_CSS = """
<style>
[data-testid="stFileUploaderDropzone"] > div > button:last-of-type,
[data-testid="stFileUploaderDropzone"] button[title="Add files"] {
    display: none !important;
}
</style>
"""

_DIALOG_CSS = """
<style>

/* Force white background and black text in all st.dialog modals */

div[data-testid="stDialog"] > div > div {
    background-color: #ffffff !important;
    color: #0f172a !important;
}
div[data-testid="stDialog"] * {
    color: #0f172a !important;
}

div[data-testid="stDialog"] [data-testid="stMarkdownContainer"] * {
    color: #0f172a !important;
}

div[data-testid="stDialog"] code,
div[data-testid="stDialog"] pre {
    background-color: #f8f9fa !important;
    color: #0f172a !important;
}
div[data-testid="stDialog"] input,
div[data-testid="stDialog"] textarea {
    background-color: #ffffff !important;
    color: #0f172a !important;
}

div[data-testid="stDialog"] [data-testid="stVerticalBlock"] {
    background-color: #ffffff !important;
}
div[data-testid="stDialog"] [data-baseweb="modal"] {
    background-color: #ffffff !important;
}

div[data-testid="stDialog"] [data-testid="stAlert"] {
    background-color: #f8f9fa !important;
    color: #0f172a !important;
}
div[data-testid="stDialog"] [data-testid="stAlert"] * {
    color: #0f172a !important;
}

div[data-testid="stDialog"] [data-testid="stExpander"] {
    background-color: #ffffff !important;
    color: #0f172a !important;
}

div[data-testid="stDialog"] [data-testid="stCode"] {
    background-color: #f8f9fa !important;
    color: #0f172a !important;
}

div[data-testid="stToast"],
div[data-testid="stToast"] > div,
div[data-testid="stToast"] > div > div {
    background-color: #ffffff !important;
    color: #0f172a !important;
    border: 1px solid #e2e8f0 !important;
    box-shadow: 0 4px 12px rgba(0,0,0,0.15) !important;
    border-radius: 8px !important;
}

div[data-testid="stToast"] * {
    color: #0f172a !important;
    background-color: #ffffff !important;
}

div[data-testid="stToast"] > div {
    background-color: #ffffff !important;
}

div[data-testid="stToast"] [data-testid="stMarkdownContainer"],
div[data-testid="stToast"] [data-testid="stMarkdownContainer"] p {
    color: #0f172a !important;
    background-color: #ffffff !important;
}

div[data-testid="stToast"] svg {
    fill: #16a34a !important;
    color: #16a34a !important;
}

div[data-testid="stToast"] button {
    color: #64748b !important;
    background-color: transparent !important;
}
div[data-testid="stToast"] button:hover {
    color: #0f172a !important;
}

div[data-testid="stToast"] button {
    color: #64748b !important;
    background-color: transparent !important;
}
div[data-testid="stToast"] button:hover {
    color: #0f172a !important;
}

</style>
"""


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL KEYWORD SYNTHESIS — fallback when LLM returns signals=[]
# ─────────────────────────────────────────────────────────────────────────────

_SIGNAL_KEYWORDS: dict[str, list[tuple[str, str, str, str]]] = {
    "Legal": [
        ("legal_escalation", "High",          "litigation",   "Document references active litigation or legal proceedings."),
        ("legal_escalation", "High",          "lawsuit",      "Lawsuit referenced in document."),
        ("legal_escalation", "Highly Severe", "criminal",     "Criminal charges or proceedings referenced."),
        ("legal_escalation", "High",          "court",        "Court involvement indicated in document."),
        ("legal_escalation", "Moderate",      "attorney",     "Attorney or legal representation mentioned."),
        ("legal_escalation", "Moderate",      "complaint",    "Formal complaint has been filed."),
        ("legal_escalation", "High",          "damages",      "Claim for damages identified."),
        ("legal_escalation", "High",          "negligence",   "Allegation of negligence present."),
        ("legal_escalation", "Highly Severe", "fraud",        "Fraud allegation detected."),
        ("legal_escalation", "Moderate",      "settlement",   "Settlement discussion referenced."),
        ("legal_escalation", "High",          "liability",    "Liability language detected in document."),
        ("legal_escalation", "Moderate",      "deposition",   "Deposition activity referenced."),
        ("coverage_issue",   "Moderate",      "exclusion",    "Policy exclusion referenced."),
        ("coverage_issue",   "High",          "denied",       "Coverage denial language detected."),
        ("coverage_issue",   "High",          "reservation",  "Reservation of rights language detected."),
    ],
    "FNOL": [
        ("severity",           "High",          "fatality",       "Fatality or death indicated in report."),
        ("severity",           "High",          "hospitalized",   "Hospitalization reported."),
        ("severity",           "Highly Severe", "total loss",     "Total loss of vehicle or property indicated."),
        ("fraud_indicator",    "High",          "inconsistent",   "Inconsistency in reported details detected."),
        ("legal_escalation",   "High",          "attorney",       "Attorney representation noted at FNOL stage."),
        ("medical_complexity", "Moderate",      "surgery",        "Surgical procedure mentioned."),
        ("medical_complexity", "High",          "permanent",      "Permanent injury or disability indicated."),
    ],
    "Medical": [
        ("medical_complexity", "High",          "surgery",        "Surgical procedure documented."),
        ("medical_complexity", "Highly Severe", "permanent",      "Permanent disability or injury noted."),
        ("medical_complexity", "High",          "chronic",        "Chronic condition identified."),
        ("medical_complexity", "Moderate",      "specialist",     "Specialist referral indicated."),
        ("fraud_indicator",    "High",          "inconsistent",   "Medical record inconsistency detected."),
    ],
    "Loss Run": [
        ("severity",       "High",     "open",      "Open claims identified in loss run."),
        ("fraud_indicator","Moderate", "frequency", "High claim frequency may indicate systemic issue."),
        ("coverage_issue", "Moderate", "reserve",   "Large reserve amounts noted."),
    ],
}

_SIGNAL_KEYWORDS_GENERIC: list[tuple[str, str, str, str]] = [
    ("fraud_indicator",  "High",          "fraud",        "Fraud keyword detected in document."),
    ("fraud_indicator",  "High",          "misrepresent", "Misrepresentation language detected."),
    ("fraud_indicator",  "High",          "false claim",  "False claim language detected."),
    ("legal_escalation", "Highly Severe", "criminal",     "Criminal reference detected."),
    ("severity",         "High",          "death",        "Reference to death in document."),
    ("severity",         "High",          "deceased",     "Deceased party mentioned."),
    ("severity",         "High",          "fatal",        "Fatality language detected."),
]


def _synthesize_signals_from_entities(intelligence: dict) -> list[dict]:
    doc_type  = intelligence.get("doc_type", "")
    full_text = (intelligence.get("full_text", "") or "").lower()
    entities  = intelligence.get("analysis", {}).get("entities", {})

    entity_blob = " ".join(
        str(v.get("value", "")) if isinstance(v, dict) else str(v)
        for v in entities.values()
    ).lower()
    corpus = full_text + " " + entity_blob

    keyword_rules = list(_SIGNAL_KEYWORDS.get(doc_type, [])) + _SIGNAL_KEYWORDS_GENERIC
    seen: set[str] = set()
    signals: list[dict] = []

    for sig_type, severity, keyword, description in keyword_rules:
        dedup_key = f"{sig_type}:{keyword}"
        if dedup_key in seen:
            continue
        if keyword.lower() in corpus:
            seen.add(dedup_key)
            idx = corpus.find(keyword.lower())

            # Find clean word boundary to avoid mid-word cuts
            snippet_start = max(0, idx - 80)
            while snippet_start > 0 and corpus[snippet_start - 1] not in (' ', '\n', '.'):
                snippet_start -= 1

            snippet = corpus[snippet_start: idx + 200].strip().replace("\n", " ")
            signals.append({
                "type":            sig_type,
                "severity_level":  severity,
                "description":     description,
                "supporting_text": snippet,
                "_synthesized":    True,
            })

    return signals


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG-AWARE META HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _get_doc_type_meta(doc_type: str) -> dict:
    if _DOC_CONFIG_AVAILABLE:
        cfg_meta = _cfg_doc_type_meta(doc_type)
        if cfg_meta.get("icon"):
            return cfg_meta
    return _DOC_TYPE_META_FALLBACK.get(
        doc_type,
        {"icon": "📄", "color": "#64748b", "bg": "rgba(100,116,139,0.06)"},
    )


def _get_signal_meta(signal_type: str, doc_type: str = "") -> dict:
    if signal_type in _SIGNAL_META_FALLBACK:
        return _SIGNAL_META_FALLBACK[signal_type]
    label = signal_type.replace("_", " ").title()
    return {"icon": "⚠️", "label": label, "color": "#6b7280"}


# ─────────────────────────────────────────────────────────────────────────────
# STYLE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _conf_badge(conf: float) -> str:
    pct = int(conf * 100)
    c   = "#16a34a" if pct >= 80 else "#ca8a04" if pct >= 60 else "#dc2626"
    bg  = "#f0fdf4" if pct >= 80 else "#fefce8" if pct >= 60 else "#fef2f2"
    return (
        f"<span style='background:{bg};border:1px solid {c}40;border-radius:20px;"
        f"padding:1px 8px;font-size:10px;color:{c};font-weight:700;"
        f"font-family:monospace;'>{pct}%</span>"
    )


def _score_badge(score: int) -> str:
    c  = "#16a34a" if score >= 80 else "#ca8a04" if score >= 60 else "#dc2626"
    bg = "#f0fdf4" if score >= 80 else "#fefce8" if score >= 60 else "#fef2f2"
    return (
        f"<span style='background:{bg};border:1px solid {c}40;border-radius:20px;"
        f"padding:2px 10px;font-size:11px;color:{c};font-weight:700;"
        f"font-family:monospace;'>{score}/100</span>"
    )


def _verdict_badge(verdict: str) -> str:
    vmap = {
        "Pass":            ("#16a34a", "#f0fdf4"),
        "Validated":       ("#16a34a", "#f0fdf4"),
        "Credible":        ("#16a34a", "#f0fdf4"),
        "Adequate":        ("#16a34a", "#f0fdf4"),
        "Fail":            ("#dc2626", "#fef2f2"),
        "Failed":          ("#dc2626", "#fef2f2"),
        "Unsupported":     ("#dc2626", "#fef2f2"),
        "Critical Gaps":   ("#dc2626", "#fef2f2"),
        "Review":          ("#ca8a04", "#fefce8"),
        "Needs Review":    ("#ca8a04", "#fefce8"),
        "Questionable":    ("#ca8a04", "#fefce8"),
        "Gaps Identified": ("#ca8a04", "#fefce8"),
    }
    c, bg = vmap.get(verdict, ("#64748b", "#f8fafc"))
    return (
        f"<span style='background:{bg};border:1px solid {c}40;border-radius:6px;"
        f"padding:3px 12px;font-size:11px;color:{c};font-weight:700;"
        f"font-family:monospace;'>{verdict}</span>"
    )


def _subtype_badge(subtype: str) -> str:
    return (
        f"<span style='background:#f1f5f9;border:1px solid #cbd5e1;"
        f"border-radius:20px;padding:2px 10px;font-size:10px;"
        f"color:#475569;font-family:monospace;font-weight:600;"
        f"white-space:nowrap;'>sub-type: {subtype}</span>"
    )


def _section_header(title: str, subtitle: str = "") -> str:
    sub = (
        f"<span style='font-size:10px;color:{_LBL};font-family:monospace;'>{subtitle}</span>"
        if subtitle else ""
    )
    return (
        f"<div style='display:flex;align-items:center;gap:10px;margin-bottom:14px;'>"
        f"<div style='font-size:11px;font-weight:700;color:{_TXT2};font-family:monospace;"
        f"text-transform:uppercase;letter-spacing:1.5px;white-space:nowrap;'>{title}</div>"
        f"{sub}"
        f"<div style='flex:1;height:1px;background:linear-gradient(90deg,{_BORDER},{_BG});'>"
        f"</div></div>"
    )


def _card(content: str, border_color: str = _BORDER, bg: str = _BG) -> str:
    return (
        f"<div style='background:{bg};border:1px solid {border_color};"
        f"border-radius:8px;padding:14px 16px;margin-bottom:10px;'>{content}</div>"
    )


def _source_snippet(source_text: str) -> str:
    if not source_text:
        return ""
    return (
        f"<div style='font-size:10px;color:{_LBL};font-family:monospace;"
        f"background:{_BG2};border-left:2px solid {_BORDER2};padding:4px 8px;"
        f"margin-top:5px;border-radius:0 4px 4px 0;font-style:italic;'>"
        f"📄 {source_text}</div>"
    )


# ─────────────────────────────────────────────────────────────────────────────
# KEY NORMALISATION
# ─────────────────────────────────────────────────────────────────────────────

def _nk(s: str) -> str:
    if not s:
        return ""
    s = str(s).strip()
    s = re.sub(r'\([^)]*\)', '', s)
    s = s.rstrip(':').strip()
    return re.sub(r"[\s\-_:./]+", "_", s.lower()).strip("_")


def _match_score(a: str, b: str) -> float:
    if a == b:
        return 1.0
    shorter = min(len(a), len(b))
    longer  = max(len(a), len(b))
    if longer == 0:
        return 0.0
    if (a in b or b in a) and shorter / longer >= 0.60:
        return shorter / longer
    a_words = set(a.split("_"))
    b_words = set(b.split("_"))
    _STOP = {"a", "of", "to", "in", "on", "by", "at", "id", "no",
             "the", "and", "or"}
    a_sig = a_words - _STOP - {w for w in a_words if len(w) <= 1}
    b_sig = b_words - _STOP - {w for w in b_words if len(w) <= 1}
    if not a_sig or not b_sig:
        return 0.0
    inter   = len(a_sig & b_sig)
    union   = len(a_sig | b_sig)
    jaccard = inter / union if union else 0.0
    shorter_sig = a_sig if len(a_sig) <= len(b_sig) else b_sig
    longer_sig  = b_sig if len(a_sig) <= len(b_sig) else a_sig
    if shorter_sig and shorter_sig <= (a_sig & b_sig):
            coverage = len(shorter_sig) / max(len(longer_sig), 1)
            if len(shorter_sig) >= 2 or coverage >= 0.50:
                return 0.60
    return jaccard if inter >= 1 and jaccard >= 0.40 else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# AZURE DI LOOKUP TABLE
# ─────────────────────────────────────────────────────────────────────────────

def _build_azure_lookup() -> dict[str, list[dict]]:
    cache_key = "_adi_lookup"
    if cache_key in st.session_state:
        return st.session_state[cache_key]

    lookup: dict[str, list[dict]] = {}

    intel = st.session_state.get("_pdf_intelligence", {})
    for az_name, az_info in intel.get("azure_di_index", {}).items():
        if not isinstance(az_info, dict):
            continue
        norm = _nk(az_name)
        lookup.setdefault(norm, []).append(az_info)

    for _, sheet_data in st.session_state.get("sheet_cache", {}).items():
        for page_dict in sheet_data.get("data", []):
            if not isinstance(page_dict, dict):
                continue
            for az_name, az_info in page_dict.items():
                if not isinstance(az_info, dict):
                    continue
                norm = _nk(az_name)
                existing_pages = {e.get("source_page") for e in lookup.get(norm, [])}
                new_page = az_info.get("source_page", 1)
                if new_page not in existing_pages:
                    lookup.setdefault(norm, []).append(az_info)

    if lookup:
        st.session_state[cache_key] = lookup
    return lookup

def _sig_tokens(s: str) -> set:
    return {
        t for t in re.sub(r"[^\w\s]", " ", (s or "").lower()).split()
        if len(t) >= 3 and t not in {"the", "and", "for", "from", "with", "that"}
    }


def _find_azure_match(
    entity_name: str,
    lookup: dict,
    hint_page: int | None = None,
    llm_value: str | None = None,
) -> dict | None:
    def _nk(s: str) -> str:
        if not s:
            return ""
        s = str(s).strip()
        s = re.sub(r'\([^)]*\)', '', s)
        s = s.rstrip(':').strip()
        return re.sub(r"[\s\-_:./]+", "_", s.lower()).strip("_")

    def _match_score(a: str, b: str) -> float:
        if a == b:
            return 1.0
        shorter = min(len(a), len(b))
        longer  = max(len(a), len(b))
        if longer == 0:
            return 0.0
        if (a in b or b in a) and shorter / longer >= 0.60:
            return shorter / longer
        a_words = set(a.split("_"))
        b_words = set(b.split("_"))
        _STOP = {"a", "of", "to", "in", "on", "by", "at", "id", "no",
                 "the", "and", "or"}
        a_sig = a_words - _STOP - {w for w in a_words if len(w) <= 1}
        b_sig = b_words - _STOP - {w for w in b_words if len(w) <= 1}
        if not a_sig or not b_sig:
            return 0.0
        inter   = len(a_sig & b_sig)
        union   = len(a_sig | b_sig)
        jaccard = inter / union if union else 0.0
        shorter_sig = a_sig if len(a_sig) <= len(b_sig) else b_sig
        longer_sig  = b_sig if len(a_sig) <= len(b_sig) else a_sig
        if shorter_sig and shorter_sig <= (a_sig & b_sig):
            coverage = len(shorter_sig) / max(len(longer_sig), 1)
            if len(shorter_sig) >= 2 or coverage >= 0.50:
                return 0.60
        return jaccard if inter >= 1 and jaccard >= 0.40 else 0.0

    en = _nk(entity_name)
    best_entries: list[dict] = []
    best_score: float = 0.0

    for az_norm, entries in lookup.items():
        score = _match_score(en, az_norm)
        if score > best_score:
            best_score   = score
            best_entries = entries
        elif score == best_score and score > 0:
            best_entries = best_entries + entries

    en_word_count = len([w for w in en.split("_") if w])
    threshold = 0.50 if en_word_count <= 2 else 0.60
    if best_score < threshold or not best_entries:
        return None

    if len(best_entries) == 1:
        candidate = best_entries[0]
    elif hint_page is not None:
        page_match = next((e for e in best_entries if e.get("source_page") == hint_page), None)
        candidate  = page_match or max(best_entries, key=lambda e: float(e.get("confidence", 0.0)))
    else:
        candidate = max(best_entries, key=lambda e: float(e.get("confidence", 0.0)))

    if llm_value and candidate:
        llm_toks = _sig_tokens(llm_value)
        az_toks  = _sig_tokens(str(candidate.get("value", "")))
        if llm_toks and az_toks and not llm_toks & az_toks:
            return None

    if llm_value and candidate and candidate.get("bounding_polygon"):
        polygon    = candidate["bounding_polygon"]
        page_w_in  = candidate.get("page_width",  8.5)
        page_h_in  = candidate.get("page_height", 11.0)
        source_page = candidate.get("source_page", 1)

        import streamlit as st  # type: ignore
        tmpdir   = st.session_state.get("tmpdir", "")
        pdf_path = None
        if tmpdir:
            for ext in (".pdf", ".PDF"):
                c = os.path.join(tmpdir, f"input{ext}")
                if os.path.exists(c):
                    pdf_path = c
                    break

        if pdf_path:
            try:
                import fitz
                doc  = fitz.open(pdf_path)
                if 1 <= source_page <= len(doc):
                    page   = doc[source_page - 1]
                    pw_pts = page.rect.width
                    ph_pts = page.rect.height
                    sx = pw_pts / page_w_in if page_w_in > 0 else 72.0
                    sy = ph_pts / page_h_in if page_h_in > 0 else 72.0

                    xs = [p[0] * sx for p in polygon]
                    ys = [p[1] * sy for p in polygon]
                    clip = fitz.Rect(min(xs), min(ys), max(xs), max(ys))
                    clip = fitz.Rect(
                        max(0, clip.x0 - 15), max(0, clip.y0 - 5),
                        min(pw_pts, clip.x1 + 15), min(ph_pts, clip.y1 + 5)
                    )
                    region_text = page.get_text("text", clip=clip).strip()
                    region_toks = _sig_tokens(region_text)
                    llm_toks    = _sig_tokens(llm_value)

                    if llm_toks and region_toks and not llm_toks & region_toks:
                        candidate = dict(candidate)
                        candidate["bounding_polygon"] = None
                doc.close()
            except Exception:
                pass

    return candidate


def _try_pymupdf_bbox_for_entity(
    field_info: dict,
    page_num: int,
    shared_doc=None,
) -> None:
    try:
        import fitz
    except ImportError:
        return

    val_text = (field_info.get("value") or "").strip()
    if not val_text:
        return

    val_toks = _sig_tokens(val_text)
    words    = val_text.split()

    _own_doc = False
    if shared_doc is not None:
        doc = shared_doc
    else:
        pdf_path = None
        import streamlit as st  # type: ignore
        tmpdir = st.session_state.get("tmpdir", "")
        if tmpdir:
            for ext in (".pdf", ".PDF"):
                c = os.path.join(tmpdir, f"input{ext}")
                if os.path.exists(c):
                    pdf_path = c
                    break
        if not pdf_path:
            return
        try:
            doc = fitz.open(pdf_path)
            _own_doc = True
        except Exception:
            return

    candidates: list[str] = []
    for n in (len(words), 6, 4, 2):
        if n <= len(words):
            s = " ".join(words[:n])
            if s not in candidates:
                candidates.append(s)
    for c in list(candidates):
        candidates.append(c.upper())

    try:
        if page_num < 1 or page_num > len(doc):
            if _own_doc:
                doc.close()
            return

        page      = doc[page_num - 1]
        pw        = page.rect.width
        ph        = page.rect.height
        page_w_in = field_info.get("page_width",  8.5)
        page_h_in = field_info.get("page_height", 11.0)

        best_rect  = None
        best_score = -1

        for cand in candidates:
            rects = page.search_for(cand)
            if not rects:
                continue

            for r in rects:
                pad = fitz.Rect(r.x0 - 20, r.y0 - 5, r.x1 + 20, r.y1 + 5)
                nearby_words = page.get_text("words", clip=pad)
                nearby_text  = " ".join(w[4] for w in nearby_words)
                nearby_toks  = _sig_tokens(nearby_text)

                if val_toks:
                    overlap = len(val_toks & nearby_toks) / len(val_toks)
                else:
                    overlap = 1.0

                score = overlap * len(cand)
                if score > best_score:
                    best_score = score
                    best_rect  = r

            if best_rect and best_score >= len(cand) * 0.5:
                break

        if best_rect and (best_score > 0 or not val_toks):
            inv_sx = page_w_in / pw if pw else 1.0
            inv_sy = page_h_in / ph if ph else 1.0
            r = best_rect
            field_info["bounding_polygon"] = [
                (r.x0 * inv_sx, r.y0 * inv_sy),
                (r.x1 * inv_sx, r.y0 * inv_sy),
                (r.x1 * inv_sx, r.y1 * inv_sy),
                (r.x0 * inv_sx, r.y1 * inv_sy),
            ]
            field_info["page_width"]    = page_w_in
            field_info["page_height"]   = page_h_in
            field_info["_pymupdf_bbox"] = True

        if _own_doc:
            doc.close()
    except Exception:
        if _own_doc:
            try:
                doc.close()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# CONFIDENCE LOOKUP
# ─────────────────────────────────────────────────────────────────────────────

def _lookup_confidence(field_name: str, field_info: dict) -> float:
    direct = field_info.get("confidence")
    if direct is not None and float(direct) > 0:
        return float(direct)
    if field_info.get("bounding_polygon"):
        return 0.85
    return 0.0


def _bbox_covers_too_much(polygon, page_w, page_h, max_fraction=0.25) -> bool:
    if not polygon or not page_w or not page_h:
        return False
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    area = (max(xs) - min(xs)) * (max(ys) - min(ys))
    return area / (page_w * page_h) > max_fraction


# ─────────────────────────────────────────────────────────────────────────────
# LLM ENTITIES ENRICHED WITH AZURE DI
# ─────────────────────────────────────────────────────────────────────────────

def _get_intelligence_entities(selected_sheet: str) -> list[tuple[str, dict]]:
    _cache_key = f"_intel_entities_{selected_sheet}"
    if _cache_key in st.session_state:
        return st.session_state[_cache_key]

    intel    = st.session_state.get("_pdf_intelligence", {})
    analysis = intel.get("analysis", {})
    entities = analysis.get("entities", {})

    _from_call_b = False
    if not entities:
        ts = analysis.get("type_specific", {})
        if ts:
            entities = ts
            _from_call_b = True
        else:
            return []

    az_lookup = _build_azure_lookup()
    eds       = _edits()
    _tmpdir  = st.session_state.get("tmpdir", "")
    _pdf_path_shared: str | None = None
    _fitz_doc_shared = None
    if _tmpdir:
        for _ext in (".pdf", ".PDF"):
            _c = os.path.join(_tmpdir, f"input{_ext}")
            if os.path.exists(_c):
                _pdf_path_shared = _c
                break
    try:
        import fitz as _fitz
        if _pdf_path_shared:
            _fitz_doc_shared = _fitz.open(_pdf_path_shared)
    except Exception:
        _fitz_doc_shared = None

    out: list[tuple[str, dict]] = []

    for entity_name, entity_data in entities.items():
        if not isinstance(entity_data, dict):
            continue

    # ───────── PATCH: damage text normalization ─────────
        value = str(entity_data.get("value", "")).lower()

        if "damage" in entity_name.lower():
            has_number = any(char.isdigit() for char in value)
            has_keywords = any(k in value for k in ["dent", "scratch", "broken", "frame", "paint"])

            if not has_number or has_keywords:
               entity_name = "damage description"
    # ───────────────────────────────────────────────────

        llm_value = entity_data.get("value", "")
        llm_conf  = float(entity_data.get("confidence", 0.0))    
        if not isinstance(entity_data, dict):
            continue

        llm_value = entity_data.get("value", "")
        llm_conf  = float(entity_data.get("confidence", 0.0))

        field_info: dict = {
            "_source": intel.get("source", ""),
            "value":              llm_value,
            "modified":           eds.get(entity_name, llm_value),
            "confidence":         llm_conf,
            "source_text":        entity_data.get("source_text", ""),
            "source_page":        1,
            "page_width":         8.5,
            "page_height":        11.0,
            "bounding_polygon":   None,
            "_adi_confidence":    0.0,
            "_from_intelligence": True,
            "_from_call_b":       _from_call_b,
            "_adi_matched":       False,
            "_adi_matched_key":   None,
            "_pymupdf_bbox":      False,
            "_user_added":        bool(
            entity_data.get("_user_added", False)
            or _is_user_added_in_cache(entity_name, selected_sheet)
        ),
        }

        adi_key_hint = entity_data.get("azure_di_key")

        az_match = _find_azure_match(entity_name, az_lookup, llm_value=llm_value)

        if az_match:
            adi_conf = float(az_match.get("confidence", 0.0))
            field_info["bounding_polygon"] = az_match.get("bounding_polygon")

            field_info["source_page"]  = az_match.get("source_page", 1)
            field_info["page_width"]   = az_match.get("page_width",  8.5)
            field_info["page_height"]  = az_match.get("page_height", 11.0)
            field_info["_adi_matched"] = True
            field_info["_adi_matched_key"] = az_match.get("_field_name", entity_name)

            if adi_conf > 0:
                field_info["confidence"]      = adi_conf
                field_info["_adi_confidence"] = adi_conf

            if not llm_value and az_match.get("value"):
                az_val = az_match["value"]
                field_info["value"]    = az_val
                field_info["modified"] = eds.get(entity_name, az_val)

            if _bbox_covers_too_much(
                field_info["bounding_polygon"],
                field_info["page_width"],
                field_info["page_height"],
            ):
                field_info["bounding_polygon"] = None

        if adi_key_hint and field_info["bounding_polygon"] is None:
            adi_norm = _nk(adi_key_hint)
            entries  = az_lookup.get(adi_norm, [])
            if entries:
                best = max(entries, key=lambda e: float(e.get("confidence", 0.0)))
                if best.get("bounding_polygon"):
                    field_info["bounding_polygon"] = best["bounding_polygon"]
                    field_info["source_page"]  = best.get("source_page", field_info["source_page"])
                    field_info["page_width"]   = best.get("page_width",  field_info["page_width"])
                    field_info["page_height"]  = best.get("page_height", field_info["page_height"])
                    field_info["_adi_matched"] = True
                    field_info["_adi_matched_key"] = adi_key_hint
                    if float(best.get("confidence", 0.0)) > 0:
                        field_info["confidence"]      = float(best["confidence"])
                        field_info["_adi_confidence"] = float(best["confidence"])

        if field_info["bounding_polygon"] is None and llm_value and len(llm_value) > 6:
            snippet = " ".join(llm_value.split()[:6]).lower()
            for entries in az_lookup.values():
                for entry in entries:
                    az_val = str(entry.get("value", "")).lower()
                    if snippet in az_val and entry.get("bounding_polygon"):
                        field_info["bounding_polygon"] = entry["bounding_polygon"]
                        field_info["source_page"]  = entry.get("source_page", field_info["source_page"])
                        field_info["page_width"]   = entry.get("page_width",  field_info["page_width"])
                        field_info["page_height"]  = entry.get("page_height", field_info["page_height"])
                        field_info["_adi_matched"] = True
                        if float(entry.get("confidence", 0.0)) > 0:
                            field_info["confidence"]      = float(entry["confidence"])
                            field_info["_adi_confidence"] = float(entry["confidence"])
                        break
                if field_info["bounding_polygon"]:
                    break

        if field_info["bounding_polygon"] is None and field_info.get("value"):
            _try_pymupdf_bbox_for_entity(
                field_info, field_info["source_page"], shared_doc=_fitz_doc_shared
            )

        # ── POST-EXTRACTION: reject dates not verbatim in full_text ──────────
        _DATE_FIELD_KEYWORDS = {
            "date filed", "date of loss", "date of report", "incident date",
            "loss date", "time of loss", "date of incident", "date reported",
            "filing date", "report date",
        }
        _fn_lower = entity_name.strip().lower()
        if any(kw in _fn_lower for kw in _DATE_FIELD_KEYWORDS):
            _raw_val = (field_info.get("value") or "").strip()
            if _raw_val:
                _full_text = (
                    st.session_state.get("_pdf_intelligence", {})
                    .get("full_text", "")
                )
                if _raw_val.lower() not in _full_text.lower():
                    import re as _re_date
                    _date_variants = [_raw_val]
                    _date_only = _re_date.sub(
                        r"\s+\d{1,2}:\d{2}.*$", "", _raw_val
                    ).strip()
                    if _date_only != _raw_val:
                        _date_variants.append(_date_only)
                    
                    _found = any(
                        v.lower() in _full_text.lower()
                        for v in _date_variants
                        if len(v) >= 4
                    )
                    if not _found:
                        continue
        # ── END DATE FILTER ──────────────────────────────────────────────────

        out.append((entity_name, field_info))
        


    seen_values: dict[str, str] = {}
    seen_names:  set[str]       = set()
    deduped: list[tuple[str, dict]] = []
    for fname, finfo in out:
        val      = (finfo.get("value") or "").strip()
        nname    = _nk(fname)
        nval     = val.lower().strip()

        if not nval or nname not in seen_names:
            if nval and nval in seen_values:
                existing_name = seen_values[nval]
                if _match_score(_nk(existing_name), nname) >= 0.40:
                    seen_names.add(nname)
                    continue
            if nval:
                seen_values[nval] = fname
            seen_names.add(nname)
            deduped.append((fname, finfo))

    out = deduped

    if _fitz_doc_shared:
        try:
            _fitz_doc_shared.close()
        except Exception:
            pass

    st.session_state[_cache_key] = out
    return out


# ─────────────────────────────────────────────────────────────────────────────
# TRACEABILITY HELPER
# ─────────────────────────────────────────────────────────────────────────────

def _extraction_source_label(field_info: dict) -> tuple[str, str, str]:
    is_user_added  = field_info.get("_user_added", False)
    from_call_b    = field_info.get("_from_call_b", False)
    adi_matched    = field_info.get("_adi_matched", False)
    adi_conf       = field_info.get("_adi_confidence", 0.0)
    adi_key        = field_info.get("_adi_matched_key") or ""
    pymupdf        = field_info.get("_pymupdf_bbox", False)
    from_intel     = field_info.get("_from_intelligence", False)
    llm_value      = field_info.get("value", "")
    source_page    = field_info.get("source_page", 1)
    conf_pct       = int(float(field_info.get("confidence", 0.0)) * 100)

    if is_user_added:
        return ("✏️", "Manually Added by User",
                "This field was injected manually via the Add New Field form.")
    
    fname = str(st.session_state.get("_file_name", "")).lower()

    is_txt = ".txt" in fname
    is_pdf = ".pdf" in fname

    if is_txt:
       return (
         "Text extraction",
         "Value extracted from unstructured text.",
         "",
    )
    if from_intel and not from_call_b:
        if adi_matched and adi_conf > 0:
            return (
                "📄",
                "Azure DI — confidence + bbox",
                f"Bounding box and confidence ({conf_pct}%) sourced from Azure Document Intelligence "
                f"field match: '{adi_key}', page {source_page}.",
            )
        elif adi_matched:
            return (
                "📄",
                "Azure DI bbox located",
                f"Bounding polygon located via Azure DI "
                f"field name match ('{adi_key}', page {source_page}), no ADI confidence score.",
            )
        elif pymupdf:
            return (
                "🔍",
                "PyMuPDF text search bbox",
                f"No Azure DI match found — "
                f"bounding box located by searching PDF text layer for value '{llm_value[:40]}' "
                f"on page {source_page} using PyMuPDF.",
            )
        else:
            return (
                "🤖",
                "LLM — no bounding box",
                "Value extracted by LLM during the entities+signals pass. "
                "No matching Azure DI field or PyMuPDF text region was found.",
            )

    if from_intel and from_call_b:
        return (
            "🧩",
            "LLM — type-specific field",
            "Entities from Call A were empty or unavailable. This value was extracted "
            "during the summary+type_specific pass as a fallback. "
            "Field lists may be less precise than Call A extractions.",
        )

    if adi_matched:
        return (
            "📄",
            "Azure Document Intelligence only",
            f"This field was not extracted by the LLM. Value comes directly from "
            f"Azure DI key-value extraction (field '{adi_key}', page {source_page}, "
            f"confidence {conf_pct}%).",
        )

    return (
        "📋",
        "Azure DI raw field (sheet cache)",
        f"Field sourced from raw Azure DI sheet cache. Page {source_page}.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# FIX 3 — WHY THIS FIELD EXISTS
# ─────────────────────────────────────────────────────────────────────────────

def _field_justification(field_name: str, field_info: dict) -> tuple[str, str]:
    from_intel  = field_info.get("_from_intelligence", False)
    from_call_b = field_info.get("_from_call_b", False)
    adi_matched = field_info.get("_adi_matched", False)
    adi_key     = field_info.get("_adi_matched_key") or ""
    is_user     = field_info.get("_user_added", False)
    value       = field_info.get("value", "")

    fn_lower = field_name.lower()
    if is_user:
        why_extracted = (
            "This field was manually added by a reviewer and is not derived "
            "from automated extraction. It exists because a user chose to record "
            "this information explicitly."
        )
    elif any(k in fn_lower for k in ("cause of loss", "cause_of_loss")):
        why_extracted = (
            "Cause of Loss is a mandatory field in every insurance claim workflow. "
            "It determines coverage applicability, reserve assignment, and litigation "
            "risk scoring. The AI extracts it from every document regardless of whether "
            "it appears as a labelled field, because an absent cause-of-loss is itself "
            "a flag for manual review."
        )
    elif any(k in fn_lower for k in ("policy", "policy number", "policy_number")):
        why_extracted = (
            "Policy number is a primary key for all downstream claim processing, "
            "duplicate detection, and coverage verification. It is always extracted "
            "even from narrative text where it may not be labelled explicitly."
        )
    elif any(k in fn_lower for k in ("date of loss", "loss date", "incident date")):
        why_extracted = (
            "Date of Loss anchors the entire claim timeline — it determines which "
            "policy period applies, statute of limitations, and reporting deadlines. "
            "It is extracted from all document types."
        )
    elif any(k in fn_lower for k in ("claimant", "plaintiff", "insured")):
        why_extracted = (
            "Party identification is fundamental to claim intake. The name of the "
            "claimant, plaintiff, or insured is extracted to link this document to "
            "the correct claim record and to flag potential duplicate claims."
        )
    elif any(k in fn_lower for k in ("damage", "loss amount", "incurred", "reserve")):
        why_extracted = (
            "Financial exposure fields (damage estimates, reserves, incurred amounts) "
            "directly drive severity classification and large-loss escalation rules. "
            "They are extracted from all document types including legal filings where "
            "amounts may appear in prayer-for-relief sections rather than form fields."
        )
    elif any(k in fn_lower for k in ("attorney", "counsel", "law firm", "lawyer")):
        why_extracted = (
            "Attorney or legal counsel information is a primary litigation signal. "
            "Its presence triggers legal escalation scoring and may affect settlement "
            "authority levels. It is always extracted when present."
        )
    elif from_intel and not from_call_b:
        why_extracted = (
            f"The AI identified '{field_name}' as a relevant field for this document "
            f"type based on its training on insurance document schemas. "
            f"This field type commonly appears in claims of this category and "
            f"was included because its value ({value[:60] + '…' if len(value) > 60 else value!r}) "
            f"was found in the document text."
        )
    elif from_call_b:
        why_extracted = (
            f"'{field_name}' is a type-specific assessment field for this document "
            f"category. It was generated during the summary/assessment pass (Call B) "
            f"to provide structured metadata beyond raw entity extraction."
        )
    else:
        why_extracted = (
            f"'{field_name}' was identified by Azure Document Intelligence as a "
            f"labelled field in the document structure. Azure DI detected it as a "
            f"key-value pair in the PDF layout."
        )

    if is_user:
        why_no_bbox = (
            "Manually added fields never have a bounding box because they were not "
            "extracted from document coordinates — they were typed in directly."
        )
    elif from_intel and not adi_matched:
        why_no_bbox = (
            "The LLM extracted this value from the document text, but Azure Document "
            "Intelligence did not return a matching key-value pair with spatial coordinates "
            "for this field. This can happen when:\n"
            "• The value was inferred from narrative prose rather than a labelled field\n"
            "• The field label in the document does not match any Azure DI key variant\n"
            "• Azure DI processed this as part of a paragraph block, not a form field\n"
            "• PyMuPDF text search could not locate the value string on the page\n\n"
            "The extracted value is still valid — only the visual highlight is unavailable."
        )
    elif adi_matched and not field_info.get("bounding_polygon"):
        why_no_bbox = (
            f"Azure DI matched this field (key: '{adi_key}') but the matched entry "
            f"in the Azure DI index does not contain bounding polygon coordinates. "
            f"This typically means Azure DI extracted the text but classified it as "
            f"a layout/paragraph element rather than a structured key-value pair, "
            f"so no per-field bounding region was recorded."
        )
    else:
        why_no_bbox = (
            "No spatial coordinates are available for this field. This field was "
            "extracted through text analysis rather than structured form recognition, "
            "so the exact document location cannot be highlighted."
        )

    return why_extracted, why_no_bbox


# ─────────────────────────────────────────────────────────────────────────────
# SESSION-STATE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _get_raw_fields(selected_sheet: str) -> list[tuple[str, dict]]:
    data = (
        st.session_state
        .get("sheet_cache", {})
        .get(selected_sheet, {})
        .get("data", [])
    )
    seen: set  = set()
    out:  list = []
    for page_dict in data:
        if isinstance(page_dict, dict):
            for fname, finfo in page_dict.items():
                if fname not in seen:
                    seen.add(fname)
                    out.append((fname, finfo))
    return out


def _get_all_pages_fields() -> dict[str, dict[str, str]]:
    eds          = _edits()
    cache        = st.session_state.get("sheet_cache", {})
    sheet_names  = st.session_state.get("sheet_names", list(cache.keys()))
    sheet_hashes = st.session_state.get("sheet_hashes", {})
    all_pages: dict[str, dict[str, str]] = {}

    def _extract_kv(data: list) -> dict[str, str]:
        kv: dict[str, str] = {}
        seen: set = set()
        for page_dict in data:
            if isinstance(page_dict, dict):
                for fname, finfo in page_dict.items():
                    if fname not in seen:
                        seen.add(fname)
                        kv[fname] = eds.get(
                            fname,
                            (finfo.get("modified", finfo.get("value", ""))
                             if isinstance(finfo, dict) else str(finfo))
                        )
        return kv

    for sname in sheet_names:
        if sname in cache:
            kv = _extract_kv(cache[sname].get("data", []))
            if kv:
                all_pages[sname] = kv
            continue
        sh_hash = sheet_hashes.get(sname, "")
        if not sh_hash:
            continue
        try:
            from modules.storage import _load_from_feature_store  # type: ignore[import]
            fs = _load_from_feature_store(sh_hash)
            if not fs:
                continue
            kv = {}
            for _cid, rec in fs.get("records", {}).items():
                for fld, fd in rec.items():
                    if fld not in kv and isinstance(fd, dict) and "value" in fd:
                        kv[fld] = eds.get(fld, fd.get("modified", fd.get("value", "")))
            if kv:
                all_pages[sname] = kv
        except Exception:
            pass

    return all_pages


def _edits() -> dict:
    if "_pdf_edits" not in st.session_state:
        st.session_state["_pdf_edits"] = {}
    return st.session_state["_pdf_edits"]


def _edit_history() -> dict:
    if "_pdf_edit_hist" not in st.session_state:
        st.session_state["_pdf_edit_hist"] = {}
    return st.session_state["_pdf_edit_hist"]


def _sync_edit(field_name: str, new_value: str, selected_sheet: str) -> None:
    eds  = _edits()
    hist = _edit_history()
    old  = eds.get(field_name)

    data = (
        st.session_state.get("sheet_cache", {})
        .get(selected_sheet, {})
        .get("data", [])
    )
    for page_dict in data:
        if isinstance(page_dict, dict) and field_name in page_dict:
            if old is None:
                old = page_dict[field_name].get("modified", page_dict[field_name].get("value", ""))
            page_dict[field_name]["modified"] = new_value
            break

    intel    = st.session_state.get("_pdf_intelligence", {})
    entities = intel.get("analysis", {}).get("entities", {})
    if field_name in entities and isinstance(entities[field_name], dict):
        if old is None:
            old = entities[field_name].get("value", "")

    eds[field_name] = new_value

    if field_name not in hist:
        hist[field_name] = []
    if not hist[field_name] or hist[field_name][-1]["to"] != new_value:
        hist[field_name].append({
            "timestamp": datetime.datetime.now().isoformat(),
            "from":      old or "",
            "to":        new_value,
        })

    st.session_state.pop("_adi_lookup", None)
    for k in list(st.session_state.keys()):
        if k.startswith("_intel_entities_"):
            st.session_state.pop(k, None)


# ─────────────────────────────────────────────────────────────────────────────
# FEATURE A — DELETE FIELD HELPER
# ─────────────────────────────────────────────────────────────────────────────

def _delete_field(field_name: str, selected_sheet: str) -> None:
    """
    Remove a field from all storage locations:
      • sheet_cache (raw Azure DI data)
      • _pdf_intelligence entities
      • _pdf_edits and _pdf_edit_hist
      • _intel_entities_ session cache keys
    """
    # 1. Remove from sheet_cache
    cache = st.session_state.get("sheet_cache", {})
    sheet_data = cache.get(selected_sheet, {})
    for page_dict in sheet_data.get("data", []):
        if isinstance(page_dict, dict) and field_name in page_dict:
            del page_dict[field_name]

    # 2. Remove from _pdf_intelligence entities
    intel    = st.session_state.get("_pdf_intelligence", {})
    entities = intel.get("analysis", {}).get("entities", {})
    entities.pop(field_name, None)
    # Also check type_specific
    ts = intel.get("analysis", {}).get("type_specific", {})
    ts.pop(field_name, None)

    # 3. Remove from edits and history
    st.session_state.get("_pdf_edits", {}).pop(field_name, None)
    st.session_state.get("_pdf_edit_hist", {}).pop(field_name, None)

    # 4. Clear cached entity lists so they rebuild without this field
    st.session_state.pop("_adi_lookup", None)
    for k in list(st.session_state.keys()):
        if k.startswith("_intel_entities_"):
            st.session_state.pop(k, None)

    # 5. Clear edit-mode state for this field
    em_key = "_pdf_edit_mode_fields"
    if em_key in st.session_state:
        st.session_state[em_key].discard(field_name)

def _is_user_added_in_cache(field_name: str, selected_sheet: str) -> bool:
    """Check sheet_cache to see if a field was manually added by a user."""
    data = (
        st.session_state
        .get("sheet_cache", {})
        .get(selected_sheet, {})
        .get("data", [])
    )
    for page_dict in data:
        if isinstance(page_dict, dict) and field_name in page_dict:
            finfo = page_dict[field_name]
            if isinstance(finfo, dict):
                return bool(finfo.get("_user_added", False))
    return False

# ─────────────────────────────────────────────────────────────────────────────
# BOUNDING BOX POPUP  (FIX A + FIX B applied here)
# ─────────────────────────────────────────────────────────────────────────────

def _render_bbox_content(field_name: str, field_info: dict, pdf_path: str) -> None:
    import streamlit as st
    import re as _re

    bounding_polygon = field_info.get("bounding_polygon")
    source_page      = int(field_info.get("source_page", 1))
    page_width       = float(field_info.get("page_width",  8.5))
    page_height      = float(field_info.get("page_height", 11.0))
    extracted_value  = field_info.get("value", "")
    confidence       = _lookup_confidence(field_name, field_info)
    conf_pct         = int(confidence * 100)
    conf_hex         = "#16a34a" if conf_pct >= 80 else "#ca8a04" if conf_pct >= 60 else "#dc2626"
    conf_rgb         = (
        (0.09, 0.64, 0.26) if conf_pct >= 80 else
        (0.79, 0.54, 0.02) if conf_pct >= 60 else
        (0.86, 0.15, 0.15)
    )

    def _digits_only(s: str) -> str:
        return _re.sub(r"\D", "", s)

    # ── Field info header ─────────────────────────────────────────────────────
    st.markdown(
        f"<div style='background:{_BG};border:1px solid {_BORDER};"
        f"border-radius:8px;padding:14px 16px;margin-bottom:14px;'>"
        f"<div style='display:flex;justify-content:space-between;align-items:flex-start;'>"
        f"<div>"
        f"<div style='font-size:9px;color:{_LBL};font-family:monospace;"
        f"text-transform:uppercase;letter-spacing:1px;'>Extracted Field</div>"
        f"<div style='font-size:16px;font-weight:700;color:#7c3aed;"
        f"font-family:monospace;margin-top:2px;'>{field_name}</div>"
        f"</div>"
        f"<div style='text-align:right;'>"
        f"<div style='font-size:9px;color:{_LBL};font-family:monospace;"
        f"text-transform:uppercase;letter-spacing:1px;'>Extraction Confidence</div>"
        f"<div style='font-size:28px;font-weight:800;color:{conf_hex};"
        f"font-family:monospace;margin-top:2px;'>"
        f"{'N/A' if conf_pct == 0 else f'{conf_pct}%'}</div>"
        f"</div></div>"
        f"<div style='height:1px;background:{_BORDER};margin:10px 0;'></div>"
        f"<div style='font-size:9px;color:{_LBL};font-family:monospace;"
        f"text-transform:uppercase;letter-spacing:1px;'>Extracted Value</div>"
        f"<div style='font-size:13px;color:{_TXT};font-family:monospace;"
        f"background:{_BG2};padding:7px 10px;border-radius:4px;margin-top:4px;"
        f"word-break:break-word;border:1px solid {_BORDER};'>"
        f"{extracted_value or '—'}</div>"
        f"<div style='margin-top:8px;font-size:10px;color:{_LBL};font-family:monospace;'>"
        f"Source: Page {source_page} &nbsp;·&nbsp; "
        f"Bounding box: {'✓ available' if bounding_polygon else '✗ not available'}"
        f"</div></div>",
        unsafe_allow_html=True,
    )

    if not bounding_polygon:
        st.warning(
            "⚠ No bounding-box coordinates for this field.\n\n"
            "Azure DI did not return a precise region for this key-value pair."
        )
        return

    if not pdf_path or not os.path.exists(pdf_path):
        st.error("❌ PDF file not accessible for rendering.")
        return

    try:
        import fitz

        doc         = fitz.open(pdf_path)
        total_pages = len(doc)

        if source_page < 1 or source_page > total_pages:
            st.error(f"Page {source_page} out of range ({total_pages} total).")
            doc.close()
            return

        page    = doc[source_page - 1]
        pw_pts  = page.rect.width
        ph_pts  = page.rect.height

        sx = pw_pts / page_width   if page_width  > 0 else 72.0
        sy = ph_pts / page_height  if page_height > 0 else 72.0

        pts = [(x * sx, y * sy) for x, y in bounding_polygon]
        xs  = [p[0] for p in pts]
        ys  = [p[1] for p in pts]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)

        x0 = max(0.0, min(x0, pw_pts))
        y0 = max(0.0, min(y0, ph_pts))
        x1 = max(0.0, min(x1, pw_pts))
        y1 = max(0.0, min(y1, ph_pts))
        if x1 - x0 < 4:
            x1 = x0 + max(4.0, pw_pts * 0.05)
        if y1 - y0 < 4:
            y1 = y0 + max(4.0, ph_pts * 0.02)

        val_str      = (extracted_value or "").strip()
        val_lower    = val_str.lower()
        val_toks     = _sig_tokens(val_str)
        val_digits   = _digits_only(val_str)
        is_boolean   = val_lower in {"yes", "no", "true", "false"}
        is_short     = len(val_str) <= 40
        corrected    = False
        rect_is_wrong = False
        display_page = source_page

        if is_boolean and val_str:
            adi_centre_x = (x0 + x1) / 2.0
            adi_centre_y = (y0 + y1) / 2.0

            val_rects = (
                page.search_for(val_str)
                or page.search_for(val_str.upper())
                or page.search_for(val_str.lower())
            )

            best_r = None

            if val_rects:
                line_h = val_rects[0].height if val_rects[0].height > 0 else 12.0

                nearby = [
                    r for r in val_rects
                    if abs((r.y0 + r.y1) / 2.0 - adi_centre_y) <= line_h * 3
                ]

                if nearby:
                    def _dist(r):
                        cx = (r.x0 + r.x1) / 2.0
                        cy = (r.y0 + r.y1) / 2.0
                        return ((cx - adi_centre_x) ** 2 + (cy - adi_centre_y) ** 2) ** 0.5
                    best_r = min(nearby, key=_dist)
                else:
                    label_variants = [
                        field_name,
                        field_name.upper(),
                        field_name.title(),
                        " ".join(w.capitalize() for w in field_name.split()),
                    ]
                    label_anchor_y = None
                    for lv in label_variants:
                        lrects = page.search_for(lv)
                        if lrects:
                            label_anchor_y = (lrects[0].y0 + lrects[0].y1) / 2.0
                            break

                    if label_anchor_y is None:
                        label_words = [w for w in field_name.split() if len(w) > 4]
                        word_ys = []
                        for lw in label_words:
                            wr = page.search_for(lw)
                            if wr:
                                word_ys.append((wr[0].y0 + wr[0].y1) / 2.0)
                        if word_ys:
                            label_anchor_y = sum(word_ys) / len(word_ys)

                    if label_anchor_y is not None:
                        same_row = [
                            r for r in val_rects
                            if abs((r.y0 + r.y1) / 2.0 - label_anchor_y) <= line_h * 1.5
                        ]
                        if same_row:
                            best_r = min(same_row,
                                         key=lambda r: abs((r.y0 + r.y1) / 2.0 - label_anchor_y))
                        else:
                            best_r = min(val_rects,
                                         key=lambda r: abs((r.y0 + r.y1) / 2.0 - adi_centre_y))
                    else:
                        best_r = min(val_rects,
                                     key=lambda r: abs((r.y0 + r.y1) / 2.0 - adi_centre_y))

            if best_r is not None:
                x0, y0, x1, y1 = best_r.x0, best_r.y0, best_r.x1, best_r.y1
                corrected = True

            row_pad = pw_pts * 0.15
            x0 = max(0.0,    x0 - row_pad)
            x1 = min(pw_pts, x1 + row_pad)
            y0 = max(0.0,    y0 - 8)
            y1 = min(ph_pts, y1 + 8)

        elif val_str:
            _bbox_w = x1 - x0
            _bbox_h = y1 - y0
            _pad_x  = 4.0 if _bbox_w < 8 else 0.0
            _pad_y  = 4.0 if _bbox_h < 8 else 0.0
            check_clip    = fitz.Rect(
                max(0, x0 - _pad_x), max(0, y0 - _pad_y),
                min(pw_pts, x1 + _pad_x), min(ph_pts, y1 + _pad_y)
            )
            region_text   = page.get_text("text", clip=check_clip).strip()
            region_lower  = region_text.lower()
            region_digits = _digits_only(region_text)

            if is_short:
                if val_digits and len(val_digits) >= 7 and len(val_str) <= 20:
                    rect_is_wrong = val_digits not in region_digits
                elif len(val_str) >= 8:
                    _date_only_check = ""
                    if " - " in val_str:
                        _date_only_check = val_str.split(" - ")[0].strip().lower()
                    import re as _re3
                    _date_only_check = _date_only_check or _re3.sub(
                        r"\s+\d{1,2}:\d{2}\s*(AM|PM)?$", "", val_str,
                        flags=_re3.IGNORECASE
                    ).strip().lower()
                    rect_is_wrong = (
                        val_lower not in region_lower
                        and (not _date_only_check or _date_only_check not in region_lower)
                    )
                else:
                    rect_is_wrong = val_lower not in region_lower
            else:
                if val_toks:
                    overlap = len(val_toks & _sig_tokens(region_text)) / len(val_toks)
                    rect_is_wrong = overlap < 0.30
                else:
                    rect_is_wrong = False

            if rect_is_wrong:
                pages_to_search = (
                    [source_page - 1]
                    + [i for i in range(total_pages) if i != source_page - 1]
                )

                words = val_str.split()
                if is_short:
                    import re as _re2

                    date_only = val_str
                    if " - " in val_str:
                        date_only = val_str.split(" - ")[0].strip()
                    date_only = _re2.sub(
                        r"\s+\d{1,2}:\d{2}\s*(AM|PM)?$", "", date_only,
                        flags=_re2.IGNORECASE
                    ).strip()

                    search_candidates: list[str] = []

                    phone_stripped = _re2.sub(
                        r"\s*[\(\-\.]?\d{3}[\)\-\.\s]\s*\d{3}[\-\.\s]\d{4}.*$",
                        "", val_str
                    ).strip().rstrip(" -–—")
                    if phone_stripped and phone_stripped != val_str and len(phone_stripped) > 6:
                        search_candidates.append(phone_stripped)

                    search_candidates += [val_str, val_str.upper(), val_str.lower()]

                    if date_only and date_only != val_str:
                        search_candidates.append(date_only)
                        search_candidates.append(date_only.upper())

                    seen_c: set[str] = set()
                    search_candidates = [
                        c for c in search_candidates
                        if not (c in seen_c or seen_c.add(c))  # type: ignore[func-returns-value]
                    ]

                else:
                    search_candidates = []
                    for n in (len(words), 8, 6, 4):
                        if n <= len(words):
                            phrase = " ".join(words[:n])
                            if phrase not in search_candidates:
                                search_candidates.append(phrase)

                best_r      = None
                best_score  = -1
                best_page_i = source_page - 1

                for page_i in pages_to_search:
                    search_page = doc[page_i]
                    sp_w = search_page.rect.width
                    sp_h = search_page.rect.height

                    for cand in search_candidates:
                        rects = search_page.search_for(cand)
                        if not rects:
                            rects = search_page.search_for(cand.upper())
                        for r in rects:
                            pad    = fitz.Rect(
                                max(0, r.x0 - 20), max(0, r.y0 - 5),
                                min(sp_w, r.x1 + 20), min(sp_h, r.y1 + 5)
                            )
                            nearby        = search_page.get_text("text", clip=pad)
                            nearby_digits = _digits_only(nearby)

                            if is_short:
                                if val_digits and len(val_digits) >= 7 and len(val_str) <= 20:
                                    score = 2.0 if val_digits in nearby_digits else 0.0
                                elif len(val_str) >= 8:
                                    nearby_l = nearby.lower()
                                    if val_lower in nearby_l:
                                        score = 2.0
                                    elif date_only and date_only.lower() in nearby_l:
                                        score = 2.0
                                    else:
                                        score = 0.0
                                else:
                                    score = 1.0 if val_lower in nearby.lower() else 0.0
                            else:
                                nearby_toks = _sig_tokens(nearby)
                                score = len(val_toks & nearby_toks) / max(len(val_toks), 1)

                            if score > best_score:
                                best_score  = score
                                best_r      = r
                                best_page_i = page_i

                        if best_r and best_score >= 1.0:
                            break
                    if best_r and best_score >= 1.0:
                        break

                if best_r and best_score >= 2.0:
                    if best_page_i != source_page - 1:
                        page         = doc[best_page_i]
                        pw_pts       = page.rect.width
                        ph_pts       = page.rect.height
                        display_page = best_page_i + 1

                    new_x0 = best_r.x0
                    new_y0 = best_r.y0
                    new_x1 = best_r.x1
                    new_y1 = best_r.y1

                    if not is_short and len(words) > 6:
                        line_h      = best_r.height or 12
                        expand_up   = min(line_h * 3, new_y0)
                        new_y0     -= expand_up
                        expand_down = min(line_h * 8, ph_pts - new_y1)
                        new_y1     += expand_down
                        new_x0      = max(0, new_x0 - 40)
                        new_x1      = min(pw_pts, pw_pts - 20)

                    _expand_page = doc[best_page_i]
                    _line_tol    = (best_r.height or 12) * 0.6
                    _ux0 = new_x0
                    _uy0 = new_y0
                    _ux1 = new_x1
                    _uy1 = new_y1
                    _br_cy = (best_r.y0 + best_r.y1) / 2.0
                    for _tok in val_str.replace(" - ", " ").replace("—", " ").split():
                        if len(_tok) < 2:
                            continue
                        for _tr in _expand_page.search_for(_tok):
                            _tr_cy = (_tr.y0 + _tr.y1) / 2.0
                            if abs(_tr_cy - _br_cy) <= _line_tol:
                                _ux0 = min(_ux0, _tr.x0)
                                _uy0 = min(_uy0, _tr.y0)
                                _ux1 = max(_ux1, _tr.x1)
                                _uy1 = max(_uy1, _tr.y1)
                    new_x0, new_y0, new_x1, new_y1 = _ux0, _uy0, _ux1, _uy1

                    x0, y0, x1, y1 = new_x0, new_y0, new_x1, new_y1
                    corrected = True

        if rect_is_wrong and not corrected:
            st.warning(
                "⚠ The stored bounding box does not match the extracted value, "
                "and the value could not be located in the PDF text layer. "
                "This typically means the value was inferred by the LLM from context "
                "rather than read from a labelled field. The extracted value is still valid."
            )
            doc.close()
            return

        bbox = fitz.Rect(x0, y0, x1, y1)

        shape = page.new_shape()
        shape.draw_rect(bbox)
        shape.finish(
            color=conf_rgb,
            fill=(*conf_rgb, 0.22),
            fill_opacity=0.28,
            width=2.5,
        )
        shape.commit()

        box_w = x1 - x0
        box_h = y1 - y0
        is_wide_field = box_w > pw_pts * 0.5
        pad_x = 10.0 if is_wide_field else max(80.0, min(120.0, box_w * 1.5))
        pad_y = max(60.0, min(100.0, box_h * 2.0))
        crop  = fitz.Rect(
            max(0.0,    x0 - pad_x),
            max(0.0,    y0 - pad_y),
            min(pw_pts, x1 + pad_x),
            min(ph_pts, y1 + pad_y),
        )

        pix_zoom = page.get_pixmap(matrix=fitz.Matrix(3.0, 3.0), clip=crop)

        correction_note = ""
        if corrected and display_page != source_page:
            correction_note = (
                f" &nbsp;<span style='color:#ca8a04;font-size:10px;'>"
                f"⚡ value found on page {display_page} (stored bbox was page {source_page})</span>"
            )
        elif corrected:
            correction_note = (
                " &nbsp;<span style='color:#ca8a04;font-size:10px;'>"
                "⚡ bbox auto-corrected to value location</span>"
            )

        st.markdown(
            f"<div style='font-size:11px;font-weight:700;color:{_TXT2};"
            f"font-family:monospace;text-transform:uppercase;letter-spacing:1.5px;"
            f"margin-bottom:8px;'>🔍 Zoomed View — Page {display_page}"
            + correction_note
            + "</div>",
            unsafe_allow_html=True,
        )

        if conf_pct > 0:
            c_pill  = "#16a34a" if conf_pct >= 80 else "#ca8a04" if conf_pct >= 60 else "#dc2626"
            bg_pill = "#f0fdf4" if conf_pct >= 80 else "#fefce8" if conf_pct >= 60 else "#fef2f2"
            st.markdown(
                f"<div style='display:inline-block;background:{bg_pill};"
                f"border:1px solid {c_pill}60;border-radius:20px;"
                f"padding:4px 14px;font-size:11px;font-weight:700;"
                f"color:{c_pill};font-family:monospace;margin-bottom:8px;'>"
                f"🎯 {conf_pct}% confidence</div>",
                unsafe_allow_html=True,
            )

        st.image(pix_zoom.tobytes("png"), use_container_width=True)

        with st.expander("📄 Full Page View"):
            pix_full = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
            st.image(pix_full.tobytes("png"), use_container_width=True)

        doc.close()

    except ImportError:
        st.error("**PyMuPDF required.** Install: `pip install pymupdf`")
    except Exception as exc:
        st.error(f"Could not render PDF page: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# FEATURE B — USER-ADDED FIELD POPUP CONTENT
# ─────────────────────────────────────────────────────────────────────────────

def _render_user_added_field_content(field_name: str, field_info: dict) -> None:
    """
    Shown when 👁 is clicked on a field that was manually added by the user.
    Replaces the generic no-bbox/LLM extraction warning with a friendly card.
    """
    import html as _html
    value      = (field_info.get("value") or "").strip()
    modified   = (field_info.get("modified") or value).strip()
    hist       = _edit_history()
    changes    = hist.get(field_name, [])
    added_ts   = changes[0].get("timestamp", "")[:19].replace("T", " ") if changes else "this session"

    st.markdown(
        f"<div style='background:#f0fdf4;border:2px solid #16a34a30;"
        f"border-left:5px solid #16a34a;border-radius:10px;"
        f"padding:18px 20px;margin-bottom:16px;'>"
        f"<div style='display:flex;align-items:center;gap:12px;margin-bottom:12px;'>"
        f"<span style='font-size:28px;'>✏️</span>"
        f"<div>"
        f"<div style='font-size:14px;font-weight:800;color:#15803d;"
        f"font-family:monospace;'>Manually Added Field</div>"
        f"<div style='font-size:11px;color:#166534;margin-top:2px;'>"
        f"This field was not extracted by AI — it was added directly by a reviewer.</div>"
        f"</div></div>"
        f"<div style='height:1px;background:#bbf7d0;margin:10px 0;'></div>"
        f"<div style='display:grid;grid-template-columns:1fr 1fr;gap:12px;'>"
        f"<div>"
        f"<div style='font-size:9px;color:#166534;font-family:monospace;"
        f"text-transform:uppercase;letter-spacing:1px;margin-bottom:4px;'>Field Name</div>"
        f"<div style='font-size:13px;font-weight:700;color:#14532d;"
        f"font-family:monospace;background:#dcfce7;padding:6px 10px;"
        f"border-radius:5px;border:1px solid #bbf7d0;'>{_html.escape(field_name)}</div>"
        f"</div>"
        f"<div>"
        f"<div style='font-size:9px;color:#166534;font-family:monospace;"
        f"text-transform:uppercase;letter-spacing:1px;margin-bottom:4px;'>Added</div>"
        f"<div style='font-size:12px;color:#14532d;font-family:monospace;"
        f"background:#dcfce7;padding:6px 10px;border-radius:5px;border:1px solid #bbf7d0;'>"
        f"⏱ {_html.escape(added_ts)}</div>"
        f"</div></div>"
        f"</div>",
        unsafe_allow_html=True,
    )

    # Value card
    st.markdown(
        f"<div style='background:{_BG};border:1px solid {_BORDER};"
        f"border-radius:8px;padding:14px 16px;margin-bottom:12px;'>"
        f"<div style='font-size:9px;color:{_LBL};font-family:monospace;"
        f"text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;'>Field Value</div>"
        f"<div style='font-size:14px;color:{_TXT};font-family:monospace;"
        f"background:{_BG2};padding:8px 12px;border-radius:5px;"
        f"border:1px solid {_BORDER};word-break:break-word;'>"
        f"{_html.escape(modified) if modified else '—'}"
        f"</div>"
        + (
            f"<div style='margin-top:8px;font-size:11px;color:#2563eb;"
            f"font-family:monospace;background:#eff6ff;padding:6px 10px;"
            f"border-radius:4px;border:1px solid #bfdbfe;'>"
            f"✏ Value was edited after addition — original: "
            f"<span style='text-decoration:line-through;color:#64748b;'>"
            f"{_html.escape(value)}</span></div>"
            if modified != value and value else ""
        )
        + f"</div>",
        unsafe_allow_html=True,
    )

    # Edit history
    if changes:
        st.markdown(
            f"<div style='background:{_BG};border:1px solid {_BORDER};"
            f"border-radius:8px;padding:14px 16px;margin-bottom:12px;'>"
            f"<div style='font-size:9px;color:{_LBL};font-family:monospace;"
            f"text-transform:uppercase;letter-spacing:1px;margin-bottom:8px;'>"
            f"📋 Edit History ({len(changes)} event(s))</div>",
            unsafe_allow_html=True,
        )
        for i, ch in enumerate(changes):
            ts   = ch.get("timestamp", "")[:19].replace("T", " ")
            from_v = _html.escape(ch.get("from", "—"))
            to_v   = _html.escape(ch.get("to", "—"))
            label  = "Added" if i == 0 else f"Edit #{i}"
            st.markdown(
                f"<div style='display:flex;gap:10px;align-items:center;padding:5px 0;"
                f"border-bottom:1px solid {_BORDER};'>"
                f"<span style='font-size:10px;color:#7c3aed;font-family:monospace;"
                f"background:#f3e8ff;border:1px solid #e9d5ff;border-radius:4px;"
                f"padding:2px 8px;white-space:nowrap;'>{label}</span>"
                f"<span style='font-size:10px;color:{_LBL};font-family:monospace;"
                f"white-space:nowrap;'>⏱ {ts}</span>"
                f"<span style='font-size:11px;color:{_TXT};font-family:monospace;"
                f"word-break:break-word;'>{from_v} → <strong>{to_v}</strong></span>"
                f"</div>",
                unsafe_allow_html=True,
            )
        st.markdown("</div>", unsafe_allow_html=True)

    # Info box
    st.markdown(
        f"<div style='background:#f0f9ff;border:1px solid #bae6fd;"
        f"border-left:4px solid #0284c7;border-radius:8px;"
        f"padding:14px 16px;'>"
        f"<div style='font-size:10px;font-weight:700;color:#0369a1;"
        f"font-family:monospace;text-transform:uppercase;letter-spacing:1px;"
        f"margin-bottom:6px;'>ℹ What does this mean?</div>"
        f"<div style='font-size:12px;color:#0c4a6e;line-height:1.8;'>"
        f"Manually added fields are <strong>not extracted from the document</strong> — "
        f"they exist purely because a reviewer entered them. They appear in the Entities "
        f"tab alongside extracted fields and are included in JSON exports and summaries. "
        f"No bounding box or confidence score is available since there is no document "
        f"region to reference. You can edit or delete this field at any time from the "
        f"Entities tab."
        f"</div></div>",
        unsafe_allow_html=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# NO-BBOX POPUP
# ─────────────────────────────────────────────────────────────────────────────

def _render_no_bbox_content(field_name: str, field_info: dict) -> None:
    # FEATURE B: Route user-added fields to the dedicated user-added popup
    if field_info.get("_user_added"):
        _render_user_added_field_content(field_name, field_info)
        return

    _esl_result = _extraction_source_label(field_info)
    icon          = _esl_result[0] if len(_esl_result) > 0 else "📋"
    source_label  = _esl_result[1] if len(_esl_result) > 1 else "Unknown"
    source_detail = _esl_result[2] if len(_esl_result) > 2 else ""
    why_extracted, why_no_bbox        = _field_justification(field_name, field_info)
    value                             = field_info.get("value", "")
    confidence                        = _lookup_confidence(field_name, field_info)
    conf_pct                          = int(confidence * 100)
    source_page                       = field_info.get("source_page", 1)

    st.markdown(
        f"<div style='background:#fef9c3;border:1px solid #fde047;"
        f"border-left:4px solid #ca8a04;border-radius:8px;"
        f"padding:14px 16px;margin-bottom:16px;'>"
        f"<div style='font-size:11px;font-weight:700;color:#92400e;"
        f"font-family:monospace;text-transform:uppercase;letter-spacing:1px;"
        f"margin-bottom:6px;'>⚠ No Bounding Box Available</div>"
        f"<div style='font-size:13px;font-weight:700;color:{_TXT};"
        f"font-family:monospace;'>{field_name}</div>"
        f"<div style='font-size:12px;color:{_TXT2};margin-top:4px;"
        f"background:{_BG2};padding:6px 10px;border-radius:4px;"
        f"font-family:monospace;word-break:break-word;border:1px solid {_BORDER};'>"
        f"{value or '—'}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )

    st.markdown(
        f"<div style='background:{_BG};border:1px solid {_BORDER};"
        f"border-radius:8px;padding:14px 16px;margin-bottom:12px;'>"
        f"<div style='font-size:10px;font-weight:700;color:#7c3aed;"
        f"font-family:monospace;text-transform:uppercase;letter-spacing:1px;"
        f"margin-bottom:8px;'>❓ Why was this field extracted?</div>"
        f"<div style='font-size:12px;color:{_TXT2};line-height:1.8;"
        f"white-space:pre-wrap;'>{why_extracted}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )

    st.markdown(
        f"<div style='background:{_BG};border:1px solid {_BORDER};"
        f"border-radius:8px;padding:14px 16px;margin-bottom:12px;'>"
        f"<div style='font-size:10px;font-weight:700;color:#2563eb;"
        f"font-family:monospace;text-transform:uppercase;letter-spacing:1px;"
        f"margin-bottom:8px;'>⚙ How was it extracted?</div>"
        f"<div style='display:flex;align-items:flex-start;gap:10px;margin-bottom:8px;'>"
        f"<span style='font-size:18px;'>{icon}</span>"
        f"<div>"
        f"<div style='font-size:12px;font-weight:700;color:#2563eb;"
        f"font-family:monospace;'>{source_label}</div>"
        f"<div style='font-size:11px;color:{_LBL};margin-top:4px;"
        f"line-height:1.7;'>{source_detail}</div>"
        f"</div></div>"
        + (
            f"<div style='font-size:10px;color:{_LBL};font-family:monospace;"
            f"margin-top:4px;'>Confidence: {_conf_badge(confidence) if conf_pct > 0 else 'N/A'} "
            f"&nbsp;·&nbsp; Page: {source_page}</div>"
        )
        + f"</div>",
        unsafe_allow_html=True,
    )

    st.markdown(
        f"<div style='background:#fef2f2;border:1px solid #fecaca;"
        f"border-left:4px solid #dc2626;border-radius:8px;"
        f"padding:14px 16px;margin-bottom:12px;'>"
        f"<div style='font-size:10px;font-weight:700;color:#dc2626;"
        f"font-family:monospace;text-transform:uppercase;letter-spacing:1px;"
        f"margin-bottom:8px;'>📍 Why is there no bounding box?</div>"
        f"<div style='font-size:12px;color:{_TXT2};line-height:1.8;"
        f"white-space:pre-wrap;'>{why_no_bbox}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )

    _is_txt_src = st.session_state.get("_pdf_intelligence", {}).get("source") == "txt"
    if _is_txt_src:
        full_text   = st.session_state.get("_pdf_intelligence", {}).get("full_text", "")
        src_snippet = field_info.get("source_text", "")

        if not src_snippet and value and full_text:
            idx = full_text.lower().find(value.lower())
            if idx >= 0:
                src_snippet = full_text[max(0, idx - 120): idx + len(value) + 120].strip()

        if src_snippet:
            safe_snippet = (
                src_snippet
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            st.markdown(
                f"<div style='background:#f0f9ff;border:1px solid #bae6fd;"
                f"border-left:4px solid #0284c7;border-radius:8px;"
                f"padding:14px 16px;margin-bottom:12px;'>"
                f"<div style='font-size:10px;font-weight:700;color:#0369a1;"
                f"font-family:monospace;text-transform:uppercase;letter-spacing:1px;"
                f"margin-bottom:8px;'>📝 Transcript Context</div>"
                f"<div style='font-size:12px;color:#0c4a6e;font-family:monospace;"
                f"line-height:1.7;white-space:pre-wrap;word-break:break-word;"
                f"font-style:italic;'>...{safe_snippet}...</div>"
                f"</div>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                f"<div style='background:#f0f9ff;border:1px solid #bae6fd;"
                f"border-left:4px solid #0284c7;border-radius:8px;"
                f"padding:12px 16px;margin-bottom:12px;'>"
                f"<div style='font-size:11px;color:#0369a1;font-family:monospace;'>"
                f"📝 Extracted from transcript text — no specific line snippet available."
                f"</div></div>",
                unsafe_allow_html=True,
            )
    _esl_result2 = _extraction_source_label(field_info)
    icon2   = _esl_result2[0] if len(_esl_result2) > 0 else "📋"
    label2  = _esl_result2[1] if len(_esl_result2) > 1 else "Unknown"
    detail2 = _esl_result2[2] if len(_esl_result2) > 2 else ""
    st.markdown(
        f"<div style='background:{_BG2};border:1px solid {_BORDER};"
        f"border-radius:8px;padding:14px 16px;'>"
        f"<div style='font-size:10px;font-weight:700;color:{_LBL};"
        f"font-family:monospace;text-transform:uppercase;letter-spacing:1px;"
        f"margin-bottom:8px;'>🔄 Transformation Journey Summary</div>"
        f"<div style='display:flex;flex-direction:column;gap:6px;'>"
        f"<div style='display:flex;gap:10px;align-items:flex-start;'>"
        f"<div style='width:20px;height:20px;border-radius:50%;background:#16a34a20;"
        f"border:2px solid #16a34a;display:flex;align-items:center;justify-content:center;"
        f"font-size:9px;font-weight:700;color:#16a34a;flex-shrink:0;'>1</div>"
        f"<div style='font-size:11px;color:{_TXT2};font-family:monospace;'>"
        f"<span style='color:#16a34a;font-weight:700;'>Azure DI parsed the PDF</span>"
        f" — raw text and KV pairs extracted</div></div>"
        f"<div style='display:flex;gap:10px;align-items:flex-start;'>"
        f"<div style='width:20px;height:20px;border-radius:50%;background:#7c3aed20;"
        f"border:2px solid #7c3aed;display:flex;align-items:center;justify-content:center;"
        f"font-size:9px;font-weight:700;color:#7c3aed;flex-shrink:0;'>2</div>"
        f"<div style='font-size:11px;color:{_TXT2};font-family:monospace;'>"
        f"<span style='color:#7c3aed;font-weight:700;'>{icon2} {label2}</span>"
        f" — {detail2}</div></div>"
        f"<div style='display:flex;gap:10px;align-items:flex-start;'>"
        f"<div style='width:20px;height:20px;border-radius:50%;background:#dc262620;"
        f"border:2px solid #dc2626;display:flex;align-items:center;justify-content:center;"
        f"font-size:9px;font-weight:700;color:#dc2626;flex-shrink:0;'>3</div>"
        f"<div style='font-size:11px;color:{_TXT2};font-family:monospace;'>"
        f"<span style='color:#dc2626;font-weight:700;'>Bounding box lookup failed</span>"
        f" — no spatial coordinates found in Azure DI index or PyMuPDF text search</div></div>"
        f"</div></div>",
        unsafe_allow_html=True,
    )

# ─────────────────────────────────────────────────────────────────────────────
# TXT TRANSCRIPT HIGHLIGHT RENDERER
# ─────────────────────────────────────────────────────────────────────────────
 
def _render_txt_highlight_content(field_name: str, field_info: dict) -> None:
    # ── PATCH: route user-added fields to dedicated popup ─────────────────
    if field_info.get("_user_added"):
        _render_user_added_field_content(field_name, field_info)
        return
    # ─────────────────────────────────────────────────────────────────────
    import re as _re
    import html as _html
 
    value      = (field_info.get("value") or "").strip()
    source_txt = (field_info.get("source_text") or "").strip()
    confidence = _lookup_confidence(field_name, field_info)
    conf_pct   = int(confidence * 100)
    conf_hex   = "#16a34a" if conf_pct >= 80 else "#ca8a04" if conf_pct >= 60 else "#dc2626"
 
    full_text  = (
        st.session_state.get("_pdf_intelligence", {}).get("full_text", "")
        or ""
    ).strip()
 
    st.markdown(
        f"<div style='background:{_BG};border:1px solid {_BORDER};"
        f"border-radius:8px;padding:14px 16px;margin-bottom:14px;'>"
        f"<div style='display:flex;justify-content:space-between;align-items:flex-start;'>"
        f"<div>"
        f"<div style='font-size:9px;color:{_LBL};font-family:monospace;"
        f"text-transform:uppercase;letter-spacing:1px;'>Extracted Field</div>"
        f"<div style='font-size:16px;font-weight:700;color:#7c3aed;"
        f"font-family:monospace;margin-top:2px;'>{_html.escape(field_name)}</div>"
        f"</div>"
        f"<div style='text-align:right;'>"
        f"<div style='font-size:9px;color:{_LBL};font-family:monospace;"
        f"text-transform:uppercase;letter-spacing:1px;'>Confidence</div>"
        f"<div style='font-size:28px;font-weight:800;color:{conf_hex};"
        f"font-family:monospace;margin-top:2px;'>"
        f"{'N/A' if conf_pct == 0 else f'{conf_pct}%'}</div>"
        f"</div></div>"
        f"<div style='height:1px;background:{_BORDER};margin:10px 0;'></div>"
        f"<div style='font-size:9px;color:{_LBL};font-family:monospace;"
        f"text-transform:uppercase;letter-spacing:1px;'>Extracted Value</div>"
        f"<div style='font-size:13px;color:{_TXT};font-family:monospace;"
        f"background:{_BG2};padding:7px 10px;border-radius:4px;margin-top:4px;"
        f"word-break:break-word;border:1px solid {_BORDER};'>"
        f"{_html.escape(value) if value else '—'}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )
 
    if not value:
        st.info("No value extracted for this field — nothing to highlight.")
        return
 
    if not full_text:
        st.warning("Full transcript text not available in session state.")
        return
 
    def _find_matches(text: str, query: str) -> list[tuple[int, int]]:
        if not query.strip():
            return []
        try:
            pattern = _re.compile(_re.escape(query.strip()), _re.IGNORECASE)
            return [(m.start(), m.end()) for m in pattern.finditer(text)]
        except Exception:
            return []
 
    matches = _find_matches(full_text, value)
 
    if not matches and ("," in value or " - " in value):
        for segment in _re.split(r"[,\-–—]", value):
            seg = segment.strip()
            if len(seg) >= 4:
                matches = _find_matches(full_text, seg)
                if matches:
                    break
 
    if not matches:
        words = sorted(
            [w for w in _re.split(r"\\W+", value) if len(w) >= 5],
            key=len, reverse=True,
        )
        for word in words[:3]:
            matches = _find_matches(full_text, word)
            if matches:
                break
 
    CONTEXT = 250
 
    if not matches:
        st.warning(
            f"⚠ Could not locate **{_html.escape(value)}** in the transcript text. "
            f"The value may have been inferred by the LLM from context rather than "
            f"appearing verbatim."
        )
        if source_txt:
            safe = _html.escape(source_txt)
            st.markdown(
                f"<div style='background:#f0f9ff;border:1px solid #bae6fd;"
                f"border-left:4px solid #0284c7;border-radius:8px;"
                f"padding:14px 16px;margin-top:12px;'>"
                f"<div style='font-size:10px;font-weight:700;color:#0369a1;"
                f"font-family:monospace;text-transform:uppercase;letter-spacing:1px;"
                f"margin-bottom:8px;'>📝 Stored Source Snippet</div>"
                f"<div style='font-size:12px;color:#0c4a6e;font-family:monospace;"
                f"line-height:1.7;white-space:pre-wrap;word-break:break-word;'>"
                f"{safe}</div></div>",
                unsafe_allow_html=True,
            )
        return
 
    st.markdown(
        f"<div style='background:#f0fdf4;border:1px solid #bbf7d0;"
        f"border-radius:6px;padding:8px 14px;margin-bottom:14px;"
        f"font-size:11px;font-family:monospace;color:#16a34a;'>"
        f"✓ Found <strong>{len(matches)}</strong> occurrence(s) in transcript"
        f"</div>",
        unsafe_allow_html=True,
    )
 
    st.markdown(
        f"<div style='font-size:11px;font-weight:700;color:{_TXT2};"
        f"font-family:monospace;text-transform:uppercase;letter-spacing:1.5px;"
        f"margin-bottom:10px;'>📄 Transcript Context</div>",
        unsafe_allow_html=True,
    )
 
    display_matches: list[tuple[int, int]] = []
    for start, end in matches:
        if display_matches and start - display_matches[-1][1] < 100:
            display_matches[-1] = (display_matches[-1][0], max(end, display_matches[-1][1]))
        else:
            display_matches.append((start, end))
 
    for i, (start, end) in enumerate(display_matches[:5]):
        ctx_start = max(0, start - CONTEXT)
        ctx_end   = min(len(full_text), end + CONTEXT)
 
        before = _html.escape(full_text[ctx_start:start])
        matched = _html.escape(full_text[start:end])
        after  = _html.escape(full_text[end:ctx_end])
 
        ellipsis_before = "…" if ctx_start > 0 else ""
        ellipsis_after  = "…" if ctx_end < len(full_text) else ""
 
        occurrence_label = f"Occurrence {i+1}" if len(display_matches) > 1 else "Match"
 
        st.markdown(
            f"<div style='background:{_BG};border:1px solid {_BORDER};"
            f"border-left:4px solid #ca8a04;border-radius:8px;"
            f"padding:14px 16px;margin-bottom:10px;'>"
            f"<div style='font-size:9px;font-weight:700;color:#b45309;"
            f"font-family:monospace;text-transform:uppercase;letter-spacing:1px;"
            f"margin-bottom:8px;'>{occurrence_label} · line ~{full_text[:start].count(chr(10)) + 1}</div>"
            f"<div style='font-size:12px;color:{_TXT};font-family:monospace;"
            f"line-height:1.8;white-space:pre-wrap;word-break:break-word;'>"
            f"<span style='color:{_LBL};'>{ellipsis_before}{before}</span>"
            f"<mark style='background:#fef08a;color:#713f12;padding:1px 3px;"
            f"border-radius:3px;font-weight:700;'>{matched}</mark>"
            f"<span style='color:{_LBL};'>{after}{ellipsis_after}</span>"
            f"</div></div>",
            unsafe_allow_html=True,
        )
 
    if len(display_matches) > 5:
        st.markdown(
            f"<div style='font-size:11px;color:{_LBL};font-family:monospace;"
            f"text-align:center;padding:8px;'>… and {len(display_matches) - 5} more occurrence(s)</div>",
            unsafe_allow_html=True,
        )
 
    with st.expander("📋 View full transcript"):
        result_parts = []
        prev = 0
        for start, end in matches:
            result_parts.append(_html.escape(full_text[prev:start]))
            result_parts.append(
                f"<mark style='background:#fef08a;color:#713f12;padding:1px 3px;"
                f"border-radius:3px;font-weight:700;'>"
                f"{_html.escape(full_text[start:end])}</mark>"
            )
            prev = end
        result_parts.append(_html.escape(full_text[prev:]))
        highlighted_full = "".join(result_parts)
 
        st.markdown(
            f"<div style='font-size:11px;color:{_TXT};font-family:monospace;"
            f"line-height:1.9;white-space:pre-wrap;word-break:break-word;"
            f"background:{_BG2};border:1px solid {_BORDER};border-radius:6px;"
            f"padding:14px 16px;max-height:500px;overflow-y:auto;'>"
            f"{highlighted_full}</div>",
            unsafe_allow_html=True,
        )


# ─────────────────────────────────────────────────────────────────────────────
# DIALOG DEFINITIONS
# ─────────────────────────────────────────────────────────────────────────────

_HAS_DIALOG = hasattr(st, "dialog")

if _HAS_DIALOG:
        @st.dialog("📍 Field Location in Document", width="large")
        def _bbox_popup(field_name: str, field_info: dict, pdf_path: str) -> None:
            _render_bbox_content(field_name, field_info, pdf_path)
 
        @st.dialog("🔍 Field Extraction Details", width="large")
        def _no_bbox_popup(field_name: str, field_info: dict) -> None:
            _render_no_bbox_content(field_name, field_info)
 
        @st.dialog("📄 Field in Transcript", width="large")
        def _txt_highlight_popup(field_name: str, field_info: dict) -> None:
            _render_txt_highlight_content(field_name, field_info)

        @st.dialog("✏️ Manually Added Field", width="large")
        def _user_added_popup(field_name: str, field_info: dict) -> None:
            _render_user_added_field_content(field_name, field_info)
 
else:
        def _bbox_popup(field_name: str, field_info: dict, pdf_path: str) -> None:
            with st.expander(f"📍 {field_name}", expanded=True):
                _render_bbox_content(field_name, field_info, pdf_path)
 
        def _no_bbox_popup(field_name: str, field_info: dict) -> None:
            with st.expander(f"🔍 {field_name} — Extraction Details", expanded=True):
                _render_no_bbox_content(field_name, field_info)
 
        def _txt_highlight_popup(field_name: str, field_info: dict) -> None:
            with st.expander(f"📄 {field_name} — Transcript", expanded=True):
                _render_txt_highlight_content(field_name, field_info)

        def _user_added_popup(field_name: str, field_info: dict) -> None:
            with st.expander(f"✏️ {field_name} — Manually Added", expanded=True):
                _render_user_added_field_content(field_name, field_info)


# ─────────────────────────────────────────────────────────────────────────────
# TAB 1 — ENTITIES
# ─────────────────────────────────────────────────────────────────────────────

def _render_entities_tab(
    intelligence: dict,
    selected_sheet: str,
    pdf_path: str | None,
) -> None:
    intel_fields = _get_intelligence_entities(selected_sheet)
    eds          = _edits()

    if not intel_fields:
        intel    = st.session_state.get("_pdf_intelligence", {})
        analysis = intel.get("analysis", {})

        has_intel = bool(intel)
        has_summ  = bool(analysis.get("summary", "").strip())
        has_ents  = bool(analysis.get("entities"))
        has_ts    = bool(analysis.get("type_specific"))
        has_sigs  = bool(analysis.get("signals"))
        doc_type  = intel.get("doc_type", "")

        def _pill(label: str, ok: bool) -> str:
            c  = "#16a34a" if ok else "#dc2626"
            bg = "#f0fdf4" if ok else "#fef2f2"
            return (
                f"<span style='background:{bg};border:1px solid {c}40;"
                f"border-radius:20px;padding:3px 10px;font-size:10px;"
                f"color:{c};font-family:monospace;'>"
                f"{'✓' if ok else '✗'} {label}</span>"
            )

        st.markdown(
            f"<div style='background:{_BG};border:1px solid #fde68a;"
            f"border-left:4px solid #ca8a04;border-radius:8px;"
            f"padding:14px 16px;margin-bottom:12px;'>"
            f"<div style='font-size:11px;font-weight:700;color:#b45309;"
            f"font-family:monospace;margin-bottom:10px;'>"
            f"⚠ LLM entity extraction returned 0 fields</div>"
            f"<div style='display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px;'>"
            f"{_pill('Intelligence ran', has_intel)}"
            f"{_pill('Summary', has_summ)}"
            f"{_pill('Entities', has_ents)}"
            f"{_pill('Type fields', has_ts)}"
            f"{_pill('Signals', has_sigs)}"
            f"{_pill('Doc type: ' + doc_type if doc_type else 'Doc type', bool(doc_type))}"
            f"</div>"
            f"<div style='font-size:11px;color:{_LBL};font-family:monospace;line-height:1.6;'>"
            f"<b style='color:#b45309;'>Most likely cause:</b> The LLM's JSON response "
            f"exceeded the token limit and was truncated mid-object. "
            f"Try setting <code>PDF_INTEL_DEBUG=1</code> to diagnose."
            f"</div></div>",
            unsafe_allow_html=True,
        )

        debug_data = st.session_state.get("_pdf_intel_debug", {})
        if debug_data:
            with st.expander("🔬 Debug Output"):
                for key, val in debug_data.items():
                    st.markdown(
                        f"<div style='font-size:10px;font-weight:700;color:#7c3aed;"
                        f"font-family:monospace;margin-bottom:4px;"
                        f"text-transform:uppercase;'>{key}</div>",
                        unsafe_allow_html=True,
                    )
                    st.code(val[:3000] if len(val) > 3000 else val, language="json")
        elif intel:
            st.markdown(
                f"<div style='font-size:10px;color:{_LBL};font-family:monospace;"
                f"margin-bottom:8px;'>💡 Set env var <code>PDF_INTEL_DEBUG=1</code> "
                f"and re-run to capture raw LLM responses.</div>",
                unsafe_allow_html=True,
            )

        col_btn, _ = st.columns([2, 5])
        with col_btn:
            if st.button("🔄 Re-run AI Analysis", use_container_width=True,
                         key="_rerun_intelligence_btn"):
                for key in ("_pdf_intelligence", "_pdf_intelligence_file",
                            "_adi_lookup", "_pdf_intel_debug", "_pdf_summary_override"):
                    st.session_state.pop(key, None)
                st.rerun()

        raw = _get_raw_fields(selected_sheet)
        if not raw:
            st.info("No fields extracted for this page yet.")
            return

        st.markdown(
            f"<div style='font-size:11px;color:{_LBL};font-family:monospace;"
            f"margin:8px 0 12px 0;background:{_BG2};border:1px solid {_BORDER};"
            f"border-radius:6px;padding:8px 12px;'>"
            f"📋 Falling back to raw Azure Document Intelligence fields.</div>",
            unsafe_allow_html=True,
        )
        intel_fields = raw

    import re as _re_nb
    _EMPTY_PLACEHOLDERS_COUNT = {
        "not found", "n/a", "na", "none", "unknown", "not available",
        "not stated", "not specified", "not provided", "unspecified",
        "see narrative", "not applicable", "-", "—", "",
    }
    _non_bool_fields = [
        (fn, fi) for fn, fi in intel_fields
        if (fi.get("value") or "").strip().lower() not in {"yes", "no", "true", "false"}
        and not (
            _re_nb.search(r"\d{3}[\)\-\.\s]\s*\d{3}[\-\.\s]\d{4}", fi.get("value") or "")
            and len(fi.get("value") or "") > 20
        )
        and (fi.get("value") or "").strip().lower() not in _EMPTY_PLACEHOLDERS_COUNT
    ]

    bbox_count = sum(1 for _, fi in _non_bool_fields if fi.get("bounding_polygon"))
    adi_count  = sum(
        1 for _, fi in _non_bool_fields
        if fi.get("azure_di_key") or fi.get("_adi_confidence", 0) > 0
    )

    st.markdown(
        _section_header(
            "Extracted Entities",
            (
                f"{len(_non_bool_fields)} field(s) · "
                f"{adi_count} matched · "
                f"{bbox_count} with bounding box"
            ),
        ),
        unsafe_allow_html=True,
    )

    _HDR = (
        f"font-size:10px;font-weight:700;font-family:monospace;"
        f"text-transform:uppercase;letter-spacing:1.5px;"
        f"padding:6px 4px;border-bottom:1px solid {_BORDER};"
    )
    # FEATURE A: Added a 4th column for Delete button (narrower action column)
    h1, h2, h3, h4 = st.columns([2.5, 3.5, 3.0, 1.5])
    h1.markdown(f"<div style='{_HDR}color:{_LBL};'>Field Name</div>",
                unsafe_allow_html=True)
    h2.markdown(f"<div style='{_HDR}color:#059669;'>Extracted</div>",
                unsafe_allow_html=True)
    h3.markdown(f"<div style='{_HDR}color:#2563eb;'>Modified</div>",
                unsafe_allow_html=True)
    h4.markdown(f"<div style='{_HDR}color:{_LBL};text-align:center;'>Actions</div>",
                unsafe_allow_html=True)

    st.markdown("<div style='height:4px;'></div>", unsafe_allow_html=True)

    _EM_KEY = "_pdf_edit_mode_fields"
    if _EM_KEY not in st.session_state:
        st.session_state[_EM_KEY] = set()

    # FEATURE A: Track delete confirmation state
    _DEL_CONFIRM_KEY = "_pdf_delete_confirm"
    if _DEL_CONFIRM_KEY not in st.session_state:
        st.session_state[_DEL_CONFIRM_KEY] = set()

    _bbox_pending_name:     str | None  = None
    _bbox_pending_info:     dict | None = None
    _no_bbox_pending_name:  str | None  = None
    _no_bbox_pending_info:  dict | None = None
    _txt_pending_name:      str | None  = None
    _txt_pending_info:      dict | None = None
    _user_pending_name:     str | None  = None
    _user_pending_info:     dict | None = None

    for field_name, field_info in intel_fields:
        extracted  = field_info.get("value", "")

        if (extracted or "").strip().lower() in {"yes", "no", "true", "false"}:
            continue

        import re as _re_skip
        if (
            _re_skip.search(r"\d{3}[\)\-\.\s]\s*\d{3}[\-\.\s]\d{4}", extracted or "")
            and len(extracted or "") > 20
        ):
            continue

        _EMPTY_PLACEHOLDERS = {
            "not found", "n/a", "na", "none", "unknown", "not available",
            "not stated", "not specified", "not provided", "unspecified",
            "see narrative", "not applicable", "-", "—", "",
        }
        if (extracted or "").strip().lower() in _EMPTY_PLACEHOLDERS:
            continue

        _is_txt_source = st.session_state.get("_pdf_intelligence", {}).get("source") == "txt"
        _doc_type_cur  = st.session_state.get("_pdf_intelligence", {}).get("doc_type", "")
        if _is_txt_source and _doc_type_cur == "FNOL":
           _FNOL_TXT_SKIP = {"policyholder name", "policyholder_name", "policy holder name"}
           if field_name.strip().lower() in _FNOL_TXT_SKIP:
              continue

        modified    = eds.get(field_name, field_info.get("modified", extracted))
        in_edit     = field_name in st.session_state[_EM_KEY]
        in_del_conf = field_name in st.session_state[_DEL_CONFIRM_KEY]
        has_bbox    = bool(field_info.get("bounding_polygon"))
        is_changed  = modified != extracted
        is_user     = field_info.get("_user_added", False)
        is_unverified = field_info.get("_unverified", False)
        confidence  = _lookup_confidence(field_name, field_info)
        conf_pct    = int(confidence * 100)

        # FEATURE A: Highlight user-added fields with a subtle green left border
        row_bg    = "#f0fdf4" if is_user else _BG
        row_border = "#bbf7d0" if is_user else _BORDER

        c1, c2, c3, c4 = st.columns([2.5, 3.5, 3.0, 1.5])

        with c1:
            user_badge = (
                f"<span style='font-size:9px;color:#16a34a;border:1px solid #bbf7d0;"
                f"border-radius:10px;padding:1px 5px;background:#f0fdf4;"
                f"font-family:monospace;margin-left:5px;'>✏ user</span>"
                if is_user else ""
            )

            unverified_badge = (
                f"<span style='font-size:9px;color:#dc2626;border:1px solid #fecaca;"
                f"border-radius:10px;padding:1px 5px;background:#fef2f2;"
                f"font-family:monospace;margin-left:5px;' "
                f"title='Value could not be verified verbatim in document text'>"
                f"⚠ unverified</span>"
                if is_unverified else ""
            )

            st.markdown(
                f"<div style='font-size:12px;font-weight:600;color:{_TXT};"
                f"font-family:monospace;padding:6px 4px 2px 4px;line-height:1.4;"
                f"word-break:break-word;'>{field_name}{user_badge}{unverified_badge}</div>"  # ← add unverified_badge
                + (f"<div style='padding:0 4px 6px 4px;'>{_conf_badge(confidence)}</div>"
                   if conf_pct > 0 else ""),
                unsafe_allow_html=True,
            )
            

        with c2:
            st.markdown(
                f"<div style='font-size:12px;color:{_TXT};font-family:monospace;"
                f"background:{_BG2};border:1px solid {_BORDER};"
                f"padding:7px 10px;border-radius:5px;min-height:34px;"
                f"line-height:1.5;white-space:pre-wrap;word-break:break-word;'>"
                f"{extracted if extracted else f'<span style=\"color:{_LBL2};\">—</span>'}"
                f"</div>",
                unsafe_allow_html=True,
            )

        with c3:
            if in_edit:
                st.text_input(
                    "modified_value", value=modified,
                    key=f"_pmv_{field_name}", label_visibility="collapsed",
                )
            else:
                _badge = (
                    f"<span style='margin-left:6px;font-size:9px;color:#2563eb;"
                    f"border:1px solid #2563eb;border-radius:10px;padding:1px 5px;"
                    f"white-space:nowrap;background:#eff6ff;'>✏ edited</span>"
                    if is_changed else ""
                )
                _bg_css = (
                    f"color:{_TXT};background:#eff6ff;border:1px solid #bfdbfe;"
                    if is_changed else
                    f"color:{_TXT};background:{_BG2};border:1px solid {_BORDER};"
                )
                st.markdown(
                    f"<div style='font-size:12px;font-family:monospace;{_bg_css}"
                    f"padding:7px 10px;border-radius:5px;min-height:34px;"
                    f"line-height:1.5;white-space:pre-wrap;word-break:break-word;'>"
                    f"{modified if modified else f'<span style=\"color:{_LBL2};\">—</span>'}"
                    f"{_badge}</div>",
                    unsafe_allow_html=True,
                )

        # FEATURE A: Action buttons — edit | eye | delete
        with c4:
            if in_del_conf:
                # Confirmation row: show "Sure?" + confirm/cancel
                conf_c1, conf_c2 = st.columns(2)
                with conf_c1:
                    if st.button("✓", key=f"_pbtn_delconfirm_{field_name}",
                                 help="Confirm delete",
                                 use_container_width=True):
                        st.session_state[_DEL_CONFIRM_KEY].discard(field_name)
                        _delete_field(field_name, selected_sheet)
                        st.toast(f'🗑 Field "{field_name}" deleted.')
                        st.rerun()
                with conf_c2:
                    if st.button("✕", key=f"_pbtn_delcancel_{field_name}",
                                 help="Cancel delete",
                                 use_container_width=True):
                        st.session_state[_DEL_CONFIRM_KEY].discard(field_name)
                        st.rerun()
                st.markdown(
                    f"<div style='font-size:9px;color:#dc2626;font-family:monospace;"
                    f"text-align:center;margin-top:2px;'>Delete?</div>",
                    unsafe_allow_html=True,
                )
            else:
                btn_edit, btn_eye, btn_del = st.columns(3)
                with btn_edit:
                    lbl = "💾" if in_edit else "✏️"
                    if st.button(lbl, key=f"_pbtn_edit_{field_name}",
                                 help="Save" if in_edit else "Edit",
                                 use_container_width=True):
                        if in_edit:
                            saved = st.session_state.get(f"_pmv_{field_name}", modified)
                            _sync_edit(field_name, saved, selected_sheet)
                            st.session_state[_EM_KEY].discard(field_name)
                        else:
                            st.session_state[_EM_KEY].add(field_name)
                        st.rerun()

                with btn_eye:
                    _is_txt = st.session_state.get("_pdf_intelligence", {}).get("source") == "txt"
                    if is_user:
                        # FEATURE B: User-added fields get dedicated popup
                        tip = "View field details — manually added by user"
                        if st.button("👁", key=f"_pbtn_eye_{field_name}", help=tip,
                                     use_container_width=True):
                            _user_pending_name = field_name
                            _user_pending_info = field_info
                    elif _is_txt:
                        tip = "View in transcript — highlights where this value appears"
                        if st.button("👁", key=f"_pbtn_eye_{field_name}", help=tip,
                                     use_container_width=True):
                            _txt_pending_name = field_name
                            _txt_pending_info = field_info
                    else:
                        if has_bbox:
                            tip = f"View field location in document · Confidence: {conf_pct}%"
                        else:
                            tip = "No bounding box — click to understand why this field was extracted"
                        if st.button("👁", key=f"_pbtn_eye_{field_name}", help=tip,
                                     use_container_width=True):
                            if has_bbox:
                                _bbox_pending_name = field_name
                                _bbox_pending_info = field_info
                            else:
                                _no_bbox_pending_name = field_name
                                _no_bbox_pending_info = field_info

                with btn_del:
                    del_color = "#dc2626" if is_user else "#6b7280"
                    del_tip   = "Delete this field (manually added)" if is_user else "Delete this field"
                    if st.button("🗑", key=f"_pbtn_del_{field_name}",
                                 help=del_tip,
                                 use_container_width=True):
                        st.session_state[_DEL_CONFIRM_KEY].add(field_name)
                        st.rerun()

        st.markdown(
            f"<div style='height:1px;background:{_BORDER};margin:2px 0 4px 0;'></div>",
            unsafe_allow_html=True,
        )

    if _bbox_pending_name and _bbox_pending_info is not None:
        _bbox_popup(_bbox_pending_name, _bbox_pending_info, pdf_path or "")
    elif _no_bbox_pending_name and _no_bbox_pending_info is not None:
        _no_bbox_popup(_no_bbox_pending_name, _no_bbox_pending_info)
    elif _txt_pending_name and _txt_pending_info is not None:
        _txt_highlight_popup(_txt_pending_name, _txt_pending_info)
    elif _user_pending_name and _user_pending_info is not None:
        _user_added_popup(_user_pending_name, _user_pending_info)

    # ── Add New Field ─────────────────────────────────────────────────────────
    st.markdown("<div style='margin-top:20px;'></div>", unsafe_allow_html=True)
    st.markdown(
        f"<div style='height:1px;background:linear-gradient(90deg,{_BORDER2},{_BG});"
        f"margin-bottom:16px;'></div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        _section_header("Add New Field", "manually inject a custom key-value pair"),
        unsafe_allow_html=True,
    )

    _ANF_KEY = "_pdf_add_field_open"
    if _ANF_KEY not in st.session_state:
        st.session_state[_ANF_KEY] = False

    if not st.session_state[_ANF_KEY]:
        anf_col, _ = st.columns([2, 5])
        with anf_col:
            if st.button(
                "＋  Add New Field",
                key="_anf_open_btn",
                help="Manually add a custom field and value to the extracted entities",
                use_container_width=True,
            ):
                st.session_state[_ANF_KEY] = True
                st.rerun()
    else:
        st.markdown(
            f"<div style='background:{_BG2};border:1px solid {_BORDER2};"
            f"border-left:4px solid #7c3aed;border-radius:8px;"
            f"padding:16px 18px;margin-bottom:10px;'>"
            f"<div style='font-size:10px;font-weight:700;color:#7c3aed;"
            f"font-family:monospace;text-transform:uppercase;letter-spacing:1.5px;"
            f"margin-bottom:12px;'>✏ New Field</div>",
            unsafe_allow_html=True,
        )

        nf_col1, nf_col2 = st.columns([1, 1])
        with nf_col1:
            st.markdown(
                f"<div style='font-size:10px;font-weight:700;color:{_LBL};"
                f"font-family:monospace;text-transform:uppercase;letter-spacing:1px;"
                f"margin-bottom:4px;'>Field Name</div>",
                unsafe_allow_html=True,
            )
            new_field_name = st.text_input(
                "new_field_name_input", value="",
                placeholder="e.g. Policy Number",
                key="_anf_name", label_visibility="collapsed",
            )
        with nf_col2:
            st.markdown(
                f"<div style='font-size:10px;font-weight:700;color:{_LBL};"
                f"font-family:monospace;text-transform:uppercase;letter-spacing:1px;"
                f"margin-bottom:4px;'>Field Value</div>",
                unsafe_allow_html=True,
            )
            new_field_value = st.text_input(
                "new_field_value_input", value="",
                placeholder="e.g. POL-2024-00123",
                key="_anf_value", label_visibility="collapsed",
            )

        st.markdown("</div>", unsafe_allow_html=True)

        btn_save, btn_cancel, _ = st.columns([1.2, 1, 4.8])
        with btn_save:
            if st.button("💾 Save Field", key="_anf_save_btn", use_container_width=True):
                fname = (new_field_name or "").strip()
                fval  = (new_field_value or "").strip()

                if not fname:
                    st.error("Field name cannot be empty.")
                elif fname in dict(intel_fields or []):
                    st.error(f'Field "{fname}" already exists. Edit it in the table above.')
                else:
                    cache      = st.session_state.get("sheet_cache", {})
                    sheet_data = cache.get(selected_sheet, {})
                    pages      = sheet_data.get("data", [])
                    new_entry  = {
                        "value":              fval,
                        "modified":           fval,
                        "confidence":         1.0,
                        "source_text":        "",
                        "source_page":        1,
                        "page_width":         8.5,
                        "page_height":        11.0,
                        "bounding_polygon":   None,
                        "_adi_confidence":    0.0,
                        "_from_intelligence": True,
                        "_user_added":        True,
                    }
                    if pages:
                        pages[0][fname] = new_entry
                    else:
                        sheet_data["data"] = [{fname: new_entry}]
                        cache[selected_sheet] = sheet_data

                    intel    = st.session_state.get("_pdf_intelligence", {})
                    entities = intel.get("analysis", {}).get("entities", {})
                    entities[fname] = {
                       "value":        fval,
                       "confidence":   1.0,
                       "source_text":  "",
                       "_user_added":  True,          # ← ADD THIS
                    }

                    _sync_edit(fname, fval, selected_sheet)
                    st.session_state.pop("_adi_lookup", None)
                    st.session_state[_ANF_KEY] = False
                    st.session_state.pop("_anf_name",  None)
                    st.session_state.pop("_anf_value", None)
                    st.toast(f'✅ Field "{fname}" added!')
                    st.rerun()

        with btn_cancel:
            if st.button("✕ Cancel", key="_anf_cancel_btn", use_container_width=True):
                st.session_state[_ANF_KEY] = False
                st.session_state.pop("_anf_name",  None)
                st.session_state.pop("_anf_value", None)
                st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# SUMMARY HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _get_intelligence_kv(selected_sheet: str) -> dict[str, str]:
    intel_fields = _get_intelligence_entities(selected_sheet)
    eds          = _edits()

    if not intel_fields:
        intel_fields = _get_raw_fields(selected_sheet)

    _EMPTY_PLACEHOLDERS = {
        "not found", "n/a", "na", "none", "unknown", "not available",
        "not stated", "not specified", "not provided", "unspecified",
        "see narrative", "not applicable", "-", "—", "",
    }

    import re as _re_kv
    _FNOL_TXT_SKIP = {"policyholder name", "policyholder_name", "policy holder name"}
    _is_txt_source = st.session_state.get("_pdf_intelligence", {}).get("source") == "txt"
    _doc_type_cur  = st.session_state.get("_pdf_intelligence", {}).get("doc_type", "")

    kv: dict[str, str] = {}
    for fname, finfo in intel_fields:
        val = eds.get(fname, finfo.get("modified", finfo.get("value", "")))

        if (val or "").strip().lower() in _EMPTY_PLACEHOLDERS:
            continue

        if (val or "").strip().lower() in {"yes", "no", "true", "false"}:
            continue

        if (
            _re_kv.search(r"\d{3}[\)\-\.\s]\s*\d{3}[\-\.\s]\d{4}", val or "")
            and len(val or "") > 20
        ):
            continue

        if _is_txt_source and _doc_type_cur == "FNOL":
            if fname.strip().lower() in _FNOL_TXT_SKIP:
                continue

        kv[fname] = val
    return kv


def _regenerate_summary(intelligence: dict, selected_sheet: str) -> str | None:
    doc_type  = intelligence.get("doc_type", "Legal")
    full_text = intelligence.get("full_text", "")

    current_kv = _get_intelligence_kv(selected_sheet)
    eds        = _edits()
    hist       = _edit_history()

    field_lines = []
    for fname, val in current_kv.items():
        orig = ""
        if fname in hist and hist[fname]:
            orig = hist[fname][0].get("from", "")
        if orig and orig != val:
            field_lines.append(f"  {fname}: {val}  [was: {orig}]")
        else:
            field_lines.append(f"  {fname}: {val}")

    fields_block = "\n".join(field_lines) if field_lines else "(no fields extracted)"

    system_prompt = (
        f"You are a senior insurance document analyst. "
        f"Generate a concise factual summary (max 200 words) of this {doc_type} insurance "
        f"document reflecting CURRENT field values. Write natural prose — no field names. "
        f"Return ONLY the summary text with no preamble."
    )
    user_prompt = (
        f"Document type: {doc_type}\n\n"
        f"CURRENT FIELD VALUES:\n{fields_block}\n\n"
        f"ORIGINAL DOCUMENT TEXT (context only):\n{full_text[:4000]}"
        + ("\n[... truncated ...]" if len(full_text) > 4000 else "")
    )

    try:
        from openai import AzureOpenAI
        client = AzureOpenAI(
            azure_endpoint=os.environ.get("OPENAI_DEPLOYMENT_ENDPOINT", ""),
            api_key=os.environ.get("OPENAI_API_KEY", ""),
            api_version=os.environ.get("OPENAI_API_VERSION", "2024-12-01-preview"),
        )
        deployment = os.environ.get("OPENAI_DEPLOYMENT_NAME", "gpt-4o-mini")
        response   = client.chat.completions.create(
            model=deployment,
            max_tokens=400,
            temperature=0.0,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
        )
        raw = (response.choices[0].message.content or "").strip()
        raw = re.sub(r"^```.*?```$", "", raw, flags=re.DOTALL).strip()
        return raw if raw else None
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# TAB 2 — SUMMARY
# ─────────────────────────────────────────────────────────────────────────────

def _render_summary_tab(intelligence: dict, selected_sheet: str) -> None:
    doc_type = intelligence.get("doc_type", "Legal")
    clf      = intelligence.get("classification", {})
    meta     = _get_doc_type_meta(doc_type)
    conf     = clf.get("confidence", 0.5)

    _SUMM_KEY = "_pdf_summary_override"
    summary   = st.session_state.get(_SUMM_KEY) or intelligence.get("analysis", {}).get("summary", "")

    st.markdown(
        f"<div style='background:{meta['bg']};border:1px solid {meta['color']}30;"
        f"border-left:4px solid {meta['color']};border-radius:8px;"
        f"padding:14px 18px;margin-bottom:16px;'>"
        f"<div style='display:flex;align-items:center;gap:12px;'>"
        f"<span style='font-size:28px;'>{meta['icon']}</span>"
        f"<div>"
        f"<div style='font-size:20px;font-weight:800;color:{meta['color']};"
        f"font-family:monospace;text-transform:uppercase;letter-spacing:2px;'>{doc_type}</div>"
        f"<div style='font-size:11px;color:{_LBL};margin-top:2px;'>"
        f"Classification confidence: {_conf_badge(conf)}</div>"
        f"</div></div></div>",
        unsafe_allow_html=True,
    )

    eds  = _edits()
    hist = _edit_history()
    changed = [(fn, h) for fn, h in hist.items() if h]
    is_regenerated = bool(st.session_state.get(_SUMM_KEY))

    btn_col, status_col = st.columns([2, 6])
    with btn_col:
        regen_label    = "🔄 Re-regenerate Summary" if is_regenerated else "🔄 Regenerate with Edits"
        regen_disabled = not changed
        if st.button(
            regen_label,
            key="_regen_summary_btn",
            help="Regenerate summary using edited field values" if not regen_disabled else "Make edits in the Entities tab first",
            disabled=regen_disabled,
            use_container_width=True,
        ):
            with st.spinner("Regenerating summary…"):
                new_summary = _regenerate_summary(intelligence, selected_sheet)
            if new_summary:
                st.session_state[_SUMM_KEY] = new_summary
                summary = new_summary
                st.toast("✅ Summary regenerated!")
                st.rerun()
            else:
                st.error("Could not regenerate summary — LLM unavailable.")

    with status_col:
        if is_regenerated:
            st.markdown(
                f"<div style='font-size:11px;color:#16a34a;font-family:monospace;"
                f"padding-top:8px;'>✓ Showing regenerated summary · based on edited values</div>",
                unsafe_allow_html=True,
            )
        elif changed:
            st.markdown(
                f"<div style='font-size:11px;color:#ca8a04;font-family:monospace;"
                f"padding-top:8px;'>⚠ {len(changed)} field(s) edited — click Regenerate to update summary</div>",
                unsafe_allow_html=True,
            )

    if st.session_state.get(_SUMM_KEY):
        if st.button("↩ Reset to original summary", key="_reset_summary_btn"):
            st.session_state.pop(_SUMM_KEY, None)
            st.rerun()

    st.markdown(_section_header("Document Summary"), unsafe_allow_html=True)

    if summary:
        annotated = summary
        if not is_regenerated:
            for fname, new_val in eds.items():
                changes = hist.get(fname, [])
                if not changes:
                    continue
                old_val = changes[0].get("from", "")
                if old_val and old_val != new_val and old_val in annotated:
                    annotated = annotated.replace(
                        old_val,
                        f"<span style='background:#eff6ff;color:#2563eb;"
                        f"border-radius:3px;padding:0 3px;font-weight:600;"
                        f"border-bottom:2px solid #2563eb;'"
                        f"title='Edited from: {old_val}'>{new_val}</span>",
                        1,
                    )

        border_color = "#16a34a" if is_regenerated else meta["color"]
        label_html   = (
            f"<div style='font-size:9px;font-weight:700;color:#16a34a;"
            f"font-family:monospace;text-transform:uppercase;letter-spacing:1px;"
            f"margin-bottom:6px;'>✓ Regenerated — uses your edited values</div>"
            if is_regenerated else ""
        )
        st.markdown(
            f"<div style='background:{_BG2};border:1px solid {border_color}30;"
            f"border-radius:8px;padding:16px 20px;font-size:13px;color:{_TXT};"
            f"line-height:1.9;'>{label_html}{annotated}</div>",
            unsafe_allow_html=True,
        )

        if changed and not is_regenerated:
            rows_html = ""
            for fname, fchanges in changed:
                old_v = fchanges[0].get("from", "—")
                new_v = eds.get(fname, fchanges[-1].get("to", "—"))
                rows_html += (
                    f"<div style='display:grid;grid-template-columns:180px 1fr auto 1fr;"
                    f"gap:8px;padding:6px 0;border-bottom:1px solid {_BORDER};align-items:center;'>"
                    f"<span style='font-size:11px;font-weight:600;color:{_TXT};"
                    f"font-family:monospace;'>{fname}</span>"
                    f"<span style='font-size:11px;color:{_LBL};font-family:monospace;"
                    f"text-decoration:line-through;word-break:break-word;'>{old_v}</span>"
                    f"<span style='font-size:13px;color:{_LBL};'>→</span>"
                    f"<span style='font-size:11px;color:#2563eb;font-family:monospace;"
                    f"font-weight:600;word-break:break-word;'>{new_v}</span>"
                    f"</div>"
                )
            st.markdown(
                f"<div style='background:#fffbeb;border:1px solid #fde68a;"
                f"border-radius:8px;padding:12px 16px;margin-top:12px;'>"
                f"<div style='font-size:10px;font-weight:700;color:#b45309;"
                f"font-family:monospace;text-transform:uppercase;letter-spacing:1px;"
                f"margin-bottom:6px;'>⚠ {len(changed)} Pending Edit(s)</div>"
                f"<div style='font-size:9px;color:{_LBL};font-family:monospace;"
                f"margin-bottom:8px;'>These edits are not yet in the summary. "
                f"Click \"Regenerate with Edits\" to update it.</div>"
                f"{rows_html}</div>",
                unsafe_allow_html=True,
            )
    else:
        st.info("No summary generated.")

    full_text  = intelligence.get("full_text", "")
    page_count = intelligence.get("page_count", 0)
    subtype    = intelligence.get("analysis", {}).get("detected_subtype") or ""
    st.markdown(
        f"<div style='display:flex;gap:14px;margin-top:14px;flex-wrap:wrap;'>"
        + "".join(
            f"<div style='background:{_BG2};border:1px solid {_BORDER};"
            f"border-radius:6px;padding:8px 14px;'>"
            f"<div style='font-size:9px;color:{_LBL};font-family:monospace;"
            f"text-transform:uppercase;letter-spacing:1px;'>{lbl}</div>"
            f"<div style='font-size:14px;font-weight:700;color:#2563eb;"
            f"font-family:monospace;margin-top:2px;'>{val}</div></div>"
            for lbl, val in [
                ("Pages",      page_count),
                ("Words",      len(full_text.split())),
                ("Characters", len(full_text)),
                ("Doc Type",   doc_type),
            ] + ([("Sub-type", subtype)] if subtype else [])
        )
        + "</div>",
        unsafe_allow_html=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# TAB 3 — SIGNALS
# ─────────────────────────────────────────────────────────────────────────────

def _classify_severity(sig: dict, doc_type: str = "") -> str:
    llm = (sig.get("severity_level") or "").strip().title()
    if llm in _TAXONOMY:
        return llm
    try:
        from modules.pdf_intelligence import classify_severity_from_config  # type: ignore[import]
        return classify_severity_from_config(sig, doc_type)
    except ImportError:
        stype = sig.get("type", "")
        if stype in ("severity", "legal_escalation"):
            return "High"
        if stype in ("coverage_issue", "medical_complexity"):
            return "Moderate"
        return "Low"


def _render_signals_tab(intelligence: dict) -> None:
    raw_llm_signals = intelligence.get("analysis", {}).get("signals", [])
    doc_type        = intelligence.get("doc_type", "")

    synthesized = False
    if raw_llm_signals:
        signals = raw_llm_signals
    else:
        signals     = _synthesize_signals_from_entities(intelligence)
        synthesized = bool(signals)

    st.markdown(
        _section_header(
            "Signal Detection",
            f"{len(signals)} signal(s) detected"
            + (" · keyword synthesized" if synthesized else""),
        ),
        unsafe_allow_html=True,
    )

    if not signals:
        st.markdown(
            _card(
                f"<div style='color:#16a34a;font-size:13px;font-family:monospace;'>"
                f"✓ No significant signals detected.</div>",
                border_color="#bbf7d0", bg="#f0fdf4",
            ),
            unsafe_allow_html=True,
        )
        return

    grouped: dict[str, list[dict]] = {lv: [] for lv in _TAXONOMY}
    for sig in signals:
        grouped[_classify_severity(sig, doc_type)].append(sig)

    pills = "".join(
        f"<span style='background:{_TAXONOMY[lv]['bg']};"
        f"border:1px solid {_TAXONOMY[lv]['color']}40;border-radius:20px;"
        f"padding:4px 12px;font-size:11px;font-weight:700;"
        f"color:{_TAXONOMY[lv]['color']};font-family:monospace;'>"
        f"{_TAXONOMY[lv]['icon']} {lv} ({len(sigs)})</span>"
        for lv, sigs in grouped.items()
        if sigs
    )
    if pills:
        st.markdown(
            f"<div style='display:flex;flex-wrap:wrap;gap:8px;margin-bottom:18px;'>"
            f"{pills}</div>",
            unsafe_allow_html=True,
        )

    for level in ["Highly Severe", "High", "Moderate", "Low"]:
        group_sigs = grouped.get(level, [])
        if not group_sigs:
            continue
        tax = _TAXONOMY[level]
        tc  = tax["color"]

        st.markdown(
            f"<div style='display:flex;align-items:center;gap:8px;margin:16px 0 8px 0;'>"
            f"<span style='font-size:16px;'>{tax['icon']}</span>"
            f"<span style='font-size:12px;font-weight:700;color:{tc};"
            f"font-family:monospace;text-transform:uppercase;letter-spacing:1.2px;'>{level}</span>"
            f"<div style='flex:1;height:1px;background:{tc}30;'></div>"
            f"<span style='font-size:10px;color:{tc};font-family:monospace;"
            f"background:{tax['bg']};border:1px solid {tc}30;border-radius:10px;"
            f"padding:1px 8px;'>{len(group_sigs)} signal(s)</span>"
            f"</div>",
            unsafe_allow_html=True,
        )

        for sig in group_sigs:
            sig_type = sig.get("type", "unknown")
            m = _get_signal_meta(sig_type, doc_type)
            c = m["color"]
            source_badge = (
                f"<span style='font-size:9px;color:#92400e;background:#fffbeb;"
                f"border:1px solid #fde68a;border-radius:10px;"
                f"padding:1px 7px;font-family:monospace;margin-left:6px;'>"
                f"keyword</span>"
                if sig.get("_synthesized") else
                f"<span style='font-size:9px;color:#1e40af;background:#eff6ff;"
                f"border:1px solid #bfdbfe;border-radius:10px;"
                f"padding:1px 7px;font-family:monospace;margin-left:6px;'>"
                f"AI</span>"
            )

            supporting_text = sig.get("supporting_text", "")
            supporting_html = ""
            if supporting_text:
                safe_text = (
                    supporting_text
                    .replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                    .replace('"', "&quot;")
                )
                supporting_html = (
                    f"<div style='font-size:11px;color:{_LBL};font-family:monospace;"
                    f"background:{_BG2};border-left:2px solid {_BORDER2};padding:6px 10px;"
                    f"border-radius:0 4px 4px 0;font-style:italic;"
                    f"white-space:pre-wrap;word-break:break-word;"
                    f"overflow-wrap:anywhere;margin-top:6px;'>"
                    f"📄 &ldquo;{safe_text}&rdquo;</div>"
                )

                # Add these after existing badge HTML in the signal card:

                trigger_html = ""
                if sig.get("trigger_matched"):
                    trigger_html = (
                        f"<div style='font-size:10px;color:#0369a1;"
                        f"background:#e0f2fe;border:1px solid #bae6fd;"
                        f"border-radius:4px;padding:3px 10px;"
                        f"display:inline-block;margin-top:6px;"
                        f"font-family:monospace;'>"
                        f"🎯 Trigger: <strong>{sig['trigger_matched']}</strong></div>"
                    )

                conf_html = ""
                sig_conf = float(sig.get("confidence", 0))
                if sig_conf > 0:
                    conf_html = (
                        f"&nbsp;&nbsp;{_conf_badge(sig_conf)}"
                    )

                unverified_html = ""
                if sig.get("_unverified"):
                    unverified_html = (
                        f"<span style='font-size:9px;color:#dc2626;"
                        f"background:#fef2f2;border:1px solid #fecaca;"
                        f"border-radius:10px;padding:1px 7px;"
                        f"font-family:monospace;margin-left:6px;'>"
                        f"⚠ unverified</span>"
                    )

            st.markdown(
                f"<div style='background:{_BG};border:1px solid {_BORDER};"
                f"border-left:4px solid {tc};border-radius:8px;"
                f"padding:12px 16px;margin-bottom:8px;'>"
                f"<div style='display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap;'>"
                f"<span style='font-size:14px;'>{m['icon']}</span>"
                f"<span style='font-size:11px;font-weight:700;color:{c};"
                f"font-family:monospace;text-transform:uppercase;letter-spacing:1px;'>"
                f"{m['label']}</span>"
                f"{source_badge}"
                f"<span style='margin-left:auto;font-size:9px;color:{tc};"
                f"background:{tax['bg']};border:1px solid {tc}30;border-radius:10px;"
                f"padding:1px 7px;font-family:monospace;white-space:nowrap;'>"
                f"{tax['icon']} {level}</span>"
                f"</div>"
                f"<div style='font-size:13px;color:{_TXT};line-height:1.7;margin-bottom:6px;'>"
                f"{sig.get('description', '')}</div>"
                + supporting_html
                + "</div>",
                unsafe_allow_html=True,
            )



# ─────────────────────────────────────────────────────────────────────────────
# TAB 4 — RAW JSON
# ─────────────────────────────────────────────────────────────────────────────

def _render_raw_json_tab(intelligence: dict, selected_sheet: str) -> None:
    eds  = _edits()
    hist = _edit_history()

    intel_kv = _get_intelligence_kv(selected_sheet)

    st.markdown(
        _section_header(
            "Extracted Key-Value Pairs",
            f"{len(intel_kv)} fields · modifications applied",
        ),
        unsafe_allow_html=True,
    )

    if not intel_kv:
        st.info("No extracted fields available. Run AI analysis first.")
        return

    edited_count = sum(1 for fn in intel_kv if fn in eds and eds[fn] != (
        next((fi.get("value", "") for nm, fi in _get_intelligence_entities(selected_sheet)
              if nm == fn), "")
    ))

    if edited_count:
        st.markdown(
            f"<div style='background:#eff6ff;border:1px solid #bfdbfe;"
            f"border-radius:6px;padding:8px 14px;margin-bottom:12px;"
            f"font-size:11px;font-family:monospace;color:#2563eb;'>"
            f"✏ {edited_count} field(s) show modified values below</div>",
            unsafe_allow_html=True,
        )

    st.code(json.dumps(intel_kv, indent=2, ensure_ascii=False), language="json")

    changed = [(fn, h) for fn, h in hist.items() if h and fn in intel_kv]
    if changed:
        rows_html = ""
        for fname, fchanges in changed:
            orig    = fchanges[0].get("from", "—")
            current = eds.get(fname, fchanges[-1].get("to", "—"))
            rows_html += (
                f"<div style='display:grid;grid-template-columns:180px 1fr auto 1fr;"
                f"gap:8px;padding:6px 0;border-bottom:1px solid {_BORDER};align-items:center;'>"
                f"<span style='font-size:11px;font-weight:600;color:{_TXT};"
                f"font-family:monospace;'>{fname}</span>"
                f"<span style='font-size:11px;color:{_LBL};font-family:monospace;"
                f"text-decoration:line-through;word-break:break-word;'>{orig}</span>"
                f"<span style='font-size:13px;color:{_LBL};'>→</span>"
                f"<span style='font-size:11px;color:#16a34a;font-family:monospace;"
                f"font-weight:600;word-break:break-word;'>{current}</span>"
                f"</div>"
            )
        with st.expander(f"📋 {len(changed)} modified field(s)"):
            st.markdown(
                f"<div style='background:{_BG};border:1px solid {_BORDER};"
                f"border-radius:8px;padding:12px 16px;'>"
                f"<div style='display:grid;grid-template-columns:180px 1fr auto 1fr;"
                f"gap:8px;padding-bottom:6px;border-bottom:1px solid {_BORDER};margin-bottom:4px;'>"
                f"<span style='font-size:9px;color:{_LBL};font-family:monospace;"
                f"text-transform:uppercase;letter-spacing:1px;'>Field</span>"
                f"<span style='font-size:9px;color:#dc2626;font-family:monospace;"
                f"text-transform:uppercase;letter-spacing:1px;'>Original</span>"
                f"<span></span>"
                f"<span style='font-size:9px;color:#16a34a;font-family:monospace;"
                f"text-transform:uppercase;letter-spacing:1px;'>Modified</span>"
                f"</div>{rows_html}</div>",
                unsafe_allow_html=True,
            )

    full_json_str = json.dumps(intel_kv, indent=2, ensure_ascii=False)
    st.markdown(
        f"<div style='font-size:11px;color:{_LBL};font-family:monospace;margin:10px 0;'>"
        f"⬇ {len(intel_kv)} fields · modified values included</div>",
        unsafe_allow_html=True,
    )

    c1, c2 = st.columns(2)
    with c1:
        st.download_button(
            "📥 Download JSON",
            data=full_json_str,
            file_name="extracted_fields.json",
            mime="application/json",
            use_container_width=True,
        )
    with c2:
        if st.button("📋 Copy to clipboard", use_container_width=True):
            st.toast("Copied!")
            st.session_state["_json_clipboard"] = full_json_str

    full_text = intelligence.get("full_text", "")
    if full_text:
        st.markdown("<div style='margin-top:16px;'></div>", unsafe_allow_html=True)
        with st.expander("📄 Full extracted text"):
            st.text_area("raw_text_area", value=full_text, height=300,
                         label_visibility="collapsed")
            st.download_button(
                "📥 Download raw text", data=full_text,
                file_name="extracted_text.txt", mime="text/plain",
                use_container_width=True,
            )


# ─────────────────────────────────────────────────────────────────────────────
# TAB 5 — TRANSFORMATION JOURNEY
# ─────────────────────────────────────────────────────────────────────────────

def _render_journey_tab(
    intelligence: dict,
    selected_sheet: str,
    uploaded_name: str,
) -> None:
    uploaded = str(uploaded_name).lower()

    is_txt = ".txt" in uploaded
    is_pdf = ".pdf" in uploaded

    source = str(intelligence.get("source", "")).lower()
    uploaded = str(uploaded_name).lower()

    is_txt = ("txt" in source) or uploaded.endswith(".txt")
    _is_txt_card = is_txt 
    parse_msg = (
    "Azure Document Intelligence parsed the PDF. Fields extracted to sheet cache."
    if is_pdf else
    "Unstructured text extraction"
)

    entity_msg = (
        "Extracted entities + signals from raw text."
        if is_txt else
        "Azure DI bounding boxes matched by field name similarity."
    )


    intel_fields   = _get_intelligence_entities(selected_sheet)
    raw_fields     = _get_raw_fields(selected_sheet)
    display_fields = intel_fields if intel_fields else raw_fields

    eds           = _edits()
    hist          = _edit_history()
    session_start = st.session_state.get("_session_start", "")
    now_str       = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    changed_fields   = [(fn, fi) for fn, fi in display_fields if fn in hist and hist[fn]]
    unchanged_fields = [(fn, fi) for fn, fi in display_fields if fn not in hist or not hist[fn]]
    edit_count       = len(changed_fields)

    last_edit_ts = ""
    if edit_count:
        all_ts = [ch["timestamp"] for fn, _ in changed_fields
                  for ch in hist.get(fn, []) if ch.get("timestamp")]
        if all_ts:
            last_edit_ts = max(all_ts)[:19].replace("T", " ")

    st.markdown(
        f"<div style='background:{_BG2};border:1px solid {_BORDER};"
        f"border-radius:10px;padding:16px 20px;margin-bottom:20px;'>"
        f"<div style='font-size:10px;font-weight:700;color:#b45309;"
        f"font-family:monospace;text-transform:uppercase;letter-spacing:2px;"
        f"margin-bottom:14px;'>⚡ Pipeline Trace</div>"
        f"<div style='display:grid;grid-template-columns:160px 1fr;gap:8px;"
        f"padding:8px 0;border-bottom:1px solid {_BORDER};align-items:start;'>"
        f"<span style='font-size:10px;font-weight:700;color:#b45309;"
        f"font-family:monospace;text-transform:uppercase;'>"
        f"{'📝 Text Extraction — TXT Parsed' if _is_txt_card else '📄 Azure DI — PDF Parsed'}"
        f"</span>"
        f"<span style='font-size:11px;color:{_TXT2};font-family:monospace;'>"
        f"{entity_msg}"
        f"<span style='color:{_LBL};'>{session_start[:19].replace('T',' ') if session_start else now_str}</span>"
        f"</span></div>"
        f"<div style='display:grid;grid-template-columns:160px 1fr;gap:8px;"
        f"padding:8px 0;border-bottom:1px solid {_BORDER};align-items:start;'>"
        f"<span style='font-size:10px;font-weight:700;color:#7c3aed;"
        f"font-family:monospace;text-transform:uppercase;letter-spacing:.8px;'>"
        f"Entities + signals</span>"
        f"<span style='font-size:11px;color:{_TXT2};font-family:monospace;'>"
        
        f"→ {parse_msg} &nbsp;"
        f"<div style='display:grid;grid-template-columns:160px 1fr;gap:8px;"
        f"padding:8px 0;align-items:start;'>"
        f"<span style='font-size:10px;font-weight:700;color:#2563eb;"
        f"font-family:monospace;text-transform:uppercase;letter-spacing:.8px;'>"
        f"✏️ USER EDITS</span>"
        f"<span style='font-size:11px;color:{_TXT2};font-family:monospace;'>"
        + (
            f"→ {edit_count} field(s) manually updated &nbsp;"
            f"<span style='color:{_LBL};'>{last_edit_ts}</span>"
            if edit_count else
            f"→ <span style='color:{_LBL};'>No edits made this session</span>"
        )
        + f"</span></div></div>",
        unsafe_allow_html=True,
    )

    st.markdown(_section_header("Field Transformation Timeline"), unsafe_allow_html=True)

    def _step_circle(n: int, color: str) -> str:
        return (
            f"<div style='width:26px;height:26px;border-radius:50%;"
            f"background:{color}15;border:2px solid {color};"
            f"display:flex;align-items:center;justify-content:center;"
            f"font-size:11px;font-weight:700;color:{color};"
            f"font-family:monospace;flex-shrink:0;'>{n}</div>"
        )

    def _source_step_html(step_n: int, field_info: dict, step1_ts: str) -> str:
        _esl_result = _extraction_source_label(field_info)
        icon   = _esl_result[0] if len(_esl_result) > 0 else "📋"
        label  = _esl_result[1] if len(_esl_result) > 1 else "Unknown"
        detail = _esl_result[2] if len(_esl_result) > 2 else ""

        field_name_str = field_info.get("_field_name_hint", "")
        raw_value      = field_info.get("value", "")

        _llm_other_indicators = {
            "other", "n/a", "unknown", "not specified", "not stated",
            "unspecified", "see narrative", "various",
        }
        _is_col = (
            "cause" in field_name_str.lower()
            or "cause of loss" in field_name_str.lower()
        )
        _llm_assigned_other = (
            _is_col
            and raw_value.strip().lower() in _llm_other_indicators
            and not field_info.get("_adi_matched", False)
        )
        if _llm_assigned_other:
            icon   = "🤖"
            label  = "LLM MAPPED — AI inference (value: Other)"
            detail = (
                f"The Cause of Loss column was empty in the source document. "
                f"The LLM assigned the value \"{raw_value}\" because no explicit "
                f"peril keyword was found in the document text. "
                f"This is an AI-inferred fallback — not a value read directly from the document. "
                f"Review the document narrative to confirm or override this classification."
            )

        if field_info.get("_user_added"):
            step_color = "#7c3aed"
        elif _llm_assigned_other:
            step_color = "#ca8a04"
        elif field_info.get("_from_call_b"):
            step_color = "#ca8a04"
        elif field_info.get("_adi_matched") and field_info.get("_adi_confidence", 0) > 0:
            step_color = "#059669"
        elif field_info.get("_adi_matched"):
            step_color = "#0284c7"
        elif field_info.get("_pymupdf_bbox"):
            step_color = "#0284c7"
        else:
            step_color = "#7c3aed"

        adi_key  = field_info.get("_adi_matched_key", "")
        adi_conf = int(float(field_info.get("_adi_confidence", 0.0)) * 100)
        conf_str = f" · ADI conf {adi_conf}%" if adi_conf > 0 else ""
        key_str  = f" · ADI key: '{adi_key}'" if adi_key else ""

        return (
            f"<div style='display:flex;gap:12px;margin-bottom:10px;'>"
            f"{_step_circle(step_n, step_color)}"
            f"<div style='flex:1;'>"
            f"<div style='display:flex;justify-content:space-between;align-items:center;"
            f"margin-bottom:4px;'>"
            f"<span style='font-size:10px;font-weight:700;color:{step_color};"
            f"font-family:monospace;text-transform:uppercase;letter-spacing:.8px;'>"
            f"{icon} {label}</span>"
            f"<span style='font-size:9px;color:{_LBL};font-family:monospace;'>"
            f"⏱ {step1_ts} · {key_str}{conf_str}</span>"
            f"</div>"
            f"</div></div>"
        )

    def _field_card(fname: str, finfo: dict) -> None:
        finfo = dict(finfo)
        finfo["_field_name_hint"] = fname

        extracted = finfo.get("value", "")
        if not extracted or not extracted.strip():
           return
        changes   = hist.get(fname, [])
        is_mod    = bool(changes)
        border    = "#fde68a" if is_mod else _BORDER
        bg        = "#fffbeb" if is_mod else _BG
        mod_badge = (
            f"<span style='margin-left:8px;font-size:9px;font-weight:700;"
            f"color:#b45309;background:#fef9c3;border:1px solid #fde047;"
            f"border-radius:10px;padding:2px 8px;font-family:monospace;'>"
            f"MODIFIED</span>"
            if is_mod else ""
        )
        src_page = finfo.get("source_page", "")
        src_text = finfo.get("source_text", "")
        step1_ts = session_start[:19].replace("T", " ") if session_start else now_str

        html = (
            f"<div style='background:{bg};border:1px solid {border};"
            f"border-radius:10px;padding:16px 18px;margin-bottom:12px;'>"
            f"<div style='font-size:12px;font-weight:700;color:{_TXT};"
            f"font-family:monospace;text-transform:uppercase;letter-spacing:1px;"
            f"margin-bottom:14px;'>{fname}{mod_badge}</div>"
            f"<div style='display:flex;gap:12px;margin-bottom:10px;'>"
            f"{_step_circle(1,'#16a34a')}"
            f"<div style='flex:1;'>"
            f"<div style='display:flex;justify-content:space-between;align-items:center;"
            f"margin-bottom:4px;'>"
            f"<span style='font-size:10px;font-weight:700;color:#16a34a;"
            f"font-family:monospace;text-transform:uppercase;'>"
            f"{'📝 Text Extraction — TXT Parsed' if st.session_state.get('_pdf_intelligence', {}).get('source') == 'txt' else '📄 Azure DI — PDF Parsed'}"
            f"</span>"
            f"<span style='font-size:9px;color:{_LBL};font-family:monospace;'>"
            f"⏱ {step1_ts} </span>"
            f"</div>"
            f"<div style='font-size:11px;color:{_LBL};font-family:monospace;"
            f"margin-bottom:5px;'>{'Entities and signals extracted from unstructured transcript text.' if _is_txt_card else 'Raw text and key-value pairs extracted from PDF.'}"
            + (f" Source: page {src_page}." if src_page else "")
            + f"</div>"
            f"<div style='background:{_BG2};border:1px solid {_BORDER};border-radius:5px;"
            f"padding:8px 12px;font-size:12px;color:{_TXT};font-family:monospace;"
            f"word-break:break-word;min-height:32px;'>"
            f"{extracted if extracted else f'<span style=\"color:{_LBL2};\">—</span>'}"
            f"</div>"
            + (
                f"<div style='font-size:10px;color:{_LBL};font-family:monospace;"
                f"background:{_BG2};border-left:2px solid {_BORDER2};padding:4px 8px;"
                f"margin-top:5px;border-radius:0 4px 4px 0;font-style:italic;'>"
                f"📄 {src_text}</div>"
                if src_text else ""
            )
            + f"</div></div>"
        )

        _is_txt_src = st.session_state.get("_pdf_intelligence", {}).get("source") == "txt"
        if not _is_txt_src:
           html += _source_step_html(2, finfo, step1_ts)

        for i, ch in enumerate(changes):
            ts     = (ch.get("timestamp", "")[:19] or "").replace("T", " ")
            from_v = ch.get("from", "")
            to_v   = ch.get("to", "")
            html += (
                f"<div style='display:flex;gap:12px;margin-bottom:8px;'>"
                f"{_step_circle(i+3,'#ca8a04')}"
                f"<div style='flex:1;'>"
                f"<div style='display:flex;justify-content:space-between;align-items:center;"
                f"margin-bottom:6px;'>"
                f"<span style='font-size:10px;font-weight:700;color:#b45309;"
                f"font-family:monospace;text-transform:uppercase;'>✏️ User Edit #{i+1}</span>"
                f"<span style='font-size:9px;color:{_LBL};font-family:monospace;'>"
                f"⏱ {ts} · _sync_edit()</span>"
                f"</div>"
                f"<div style='display:flex;gap:10px;align-items:center;'>"
                f"<div style='flex:1;background:#fef2f2;border:1px solid #fecaca;"
                f"border-radius:5px;padding:7px 12px;font-size:12px;"
                f"color:#dc2626;font-family:monospace;word-break:break-word;'>"
                f"FROM: {from_v or '—'}</div>"
                f"<span style='font-size:16px;color:{_LBL};'>→</span>"
                f"<div style='flex:1;background:#f0fdf4;border:1px solid #bbf7d0;"
                f"border-radius:5px;padding:7px 12px;font-size:12px;"
                f"color:#16a34a;font-family:monospace;word-break:break-word;'>"
                f"TO: {to_v or '—'}</div>"
                f"</div></div></div>"
            )

        html += "</div>"
        st.markdown(html, unsafe_allow_html=True)

    for fname, finfo in changed_fields:
        _field_card(fname, finfo)

    if unchanged_fields:
        with st.expander(f"📋 {len(unchanged_fields)} unchanged field(s)"):
            for fname, finfo in unchanged_fields:
                _field_card(fname, finfo)

    st.markdown("<div style='margin-top:24px;'></div>", unsafe_allow_html=True)
    st.markdown(_section_header("Audit Log"), unsafe_allow_html=True)

    _EVENT_META = {
        "FILE_INGESTED":           {"color": "#16a34a", "icon": "📄"},
        "SHEET_PARSED":            {"color": "#2563eb", "icon": "🔍"},
        "SHEET_LOADED_FROM_CACHE": {"color": "#7c3aed", "icon": "💾"},
    }

    try:
        from modules.audit import _load_audit_log  # type: ignore[import]
        full_log = _load_audit_log()

        def _is_cur(e: dict) -> bool:
            ts = e.get("timestamp", "")
            return not ts or not session_start or ts >= session_start

        def _is_rel(e: dict) -> bool:
            return (
                uploaded_name in (e.get("filename") or "")
                or "PDF" in (e.get("event") or "").upper()
                or "pdf" in (e.get("sheet_type") or "").lower()
            )

        cur_log  = [e for e in full_log if _is_cur(e) and _is_rel(e)]
        hist_log = [e for e in full_log if not _is_cur(e) and _is_rel(e)]

        def _log_row(entry: dict, idx: int, prefix: str) -> None:
            ts    = (entry.get("timestamp", "")[:19] or "").replace("T", " ")
            event = entry.get("event", "UNKNOWN")
            em    = _EVENT_META.get(event, {"color": "#6b7280", "icon": "●"})
            c     = em["color"]
            parts = []
            if entry.get("sheet"):
                parts.append(entry["sheet"])
            if entry.get("sheet_type"):
                parts.append(entry["sheet_type"])
            if entry.get("claim_rows"):
                parts.append(f"{entry['claim_rows']} rows")
            detail = " · ".join(parts)
            st.markdown(
                f"<div style='background:{_BG2};border:1px solid {_BORDER};"
                f"border-left:3px solid {c};border-radius:6px;"
                f"padding:9px 14px;margin-bottom:4px;"
                f"display:flex;align-items:center;gap:12px;'>"
                f"<span style='font-size:9px;font-weight:700;color:{c};"
                f"font-family:monospace;background:{c}12;border:1px solid {c}30;"
                f"border-radius:4px;padding:2px 8px;white-space:nowrap;"
                f"text-transform:uppercase;'>{em['icon']} {event}</span>"
                f"<span style='font-size:10px;color:{_LBL};font-family:monospace;"
                f"white-space:nowrap;'>· {ts}</span>"
                f"<span style='font-size:11px;color:{_TXT2};font-family:monospace;'>"
                f"· {detail}</span>"
                f"</div>",
                unsafe_allow_html=True,
            )

        if not cur_log:
            st.info("No log entries for this session.")
        else:
            for i, e in enumerate(reversed(cur_log[-30:])):
                _log_row(e, i, "cur")

        if hist_log:
            st.markdown("<div style='margin-top:12px;'></div>", unsafe_allow_html=True)
            _hk = "_audit_show_hist"
            show_h = st.session_state.get(_hk, False)
            if st.button(
                "🕑 Hide previous history" if show_h else "🕑 View history",
                key="toggle_audit_hist",
            ):
                st.session_state[_hk] = not show_h
                st.rerun()
            if show_h:
                for i, e in enumerate(reversed(hist_log[-30:])):
                    _log_row(e, i, "hist")

    except Exception as exc:
        st.warning(f"Could not load audit log: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# TAB 6 — VALIDATION
# ─────────────────────────────────────────────────────────────────────────────

def _render_validation_dimension(icon: str, title: str, data: dict, color: str) -> None:
    score    = data.get("score",   0)
    verdict  = data.get("verdict", "Review")
    findings = data.get("findings", "")

    st.markdown(
        f"<div style='background:{_BG};border:1px solid {_BORDER};"
        f"border-left:4px solid {color};border-radius:10px;"
        f"padding:18px 20px;margin-bottom:14px;'>"
        f"<div style='display:flex;align-items:center;gap:10px;margin-bottom:4px;'>"
        f"<span style='font-size:20px;'>{icon}</span>"
        f"<span style='font-size:13px;font-weight:700;color:{_TXT};"
        f"font-family:monospace;text-transform:uppercase;letter-spacing:1px;'>{title}</span>"
        f"<div style='margin-left:auto;display:flex;align-items:center;gap:8px;'>"
        f"{_score_badge(score)}"
        f"{_verdict_badge(verdict)}"
        f"</div></div>"
        f"<div style='height:1px;background:{_BORDER};margin:10px 0;'></div>"
        f"<div style='font-size:13px;color:{_TXT2};line-height:1.8;'>{findings}</div>",
        unsafe_allow_html=True,
    )

    missed    = data.get("missed_fields") or data.get("missed_signals") or data.get("gaps") or []
    incorrect = data.get("incorrect_fields", [])
    false_pos = data.get("false_positives", [])

    if missed:
        items_html = "".join(
            f"<span style='background:#fef2f2;border:1px solid #fecaca;"
            f"border-radius:4px;padding:2px 8px;font-size:11px;"
            f"color:#dc2626;font-family:monospace;margin:2px;'>{m}</span>"
            for m in missed
        )
        st.markdown(
            f"<div style='margin-top:8px;'>"
            f"<div style='font-size:9px;font-weight:700;color:{_LBL};"
            f"font-family:monospace;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px;'>"
            f"Missing / Not Detected</div>"
            f"<div style='display:flex;flex-wrap:wrap;gap:4px;'>{items_html}</div>"
            f"</div>",
            unsafe_allow_html=True,
        )

    if incorrect:
        rows = "".join(
            f"<div style='display:grid;grid-template-columns:140px 1fr auto 1fr;"
            f"gap:8px;padding:5px 0;border-bottom:1px solid {_BORDER};align-items:start;'>"
            f"<span style='font-size:11px;font-weight:600;color:{_TXT};"
            f"font-family:monospace;word-break:break-word;'>{item.get('field','')}</span>"
            f"<span style='font-size:11px;color:#dc2626;font-family:monospace;"
            f"word-break:break-word;text-decoration:line-through;'>{item.get('extracted','')}</span>"
            f"<span style='color:{_LBL};font-size:12px;'>→</span>"
            f"<span style='font-size:11px;color:#16a34a;font-family:monospace;"
            f"word-break:break-word;'>{item.get('expected','')}</span>"
            f"</div>"
            for item in incorrect
        )
        st.markdown(
            f"<div style='margin-top:10px;background:{_BG2};border:1px solid {_BORDER};"
            f"border-radius:6px;padding:10px 14px;'>"
            f"<div style='font-size:9px;font-weight:700;color:{_LBL};"
            f"font-family:monospace;text-transform:uppercase;letter-spacing:1px;"
            f"margin-bottom:8px;'>Incorrect Extractions</div>"
            f"{rows}</div>",
            unsafe_allow_html=True,
        )

    if false_pos:
        items_html = "".join(
            f"<span style='background:#fffbeb;border:1px solid #fde68a;"
            f"border-radius:4px;padding:2px 8px;font-size:11px;"
            f"color:#b45309;font-family:monospace;margin:2px;'>{fp}</span>"
            for fp in false_pos
        )
        st.markdown(
            f"<div style='margin-top:8px;'>"
            f"<div style='font-size:9px;font-weight:700;color:{_LBL};"
            f"font-family:monospace;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px;'>"
            f"Potential False Positives</div>"
            f"<div style='display:flex;flex-wrap:wrap;gap:4px;'>{items_html}</div>"
            f"</div>",
            unsafe_allow_html=True,
        )

    st.markdown("</div>", unsafe_allow_html=True)

def _render_validation_tab(intelligence: dict, selected_sheet: str) -> None:
    try:
        from modules.storage import _load_validation_result, _save_validation_result
        _STORAGE_AVAILABLE = True
    except ImportError:
        _STORAGE_AVAILABLE = False
        def _load_validation_result(h): return None
        def _save_validation_result(h, r): pass

    st.markdown(_section_header("Document Validation"), unsafe_allow_html=True)

    st.markdown(
        f"<div style='background:#f0f9ff;border:1px solid #bae6fd;"
        f"border-left:4px solid #0284c7;border-radius:8px;"
        f"padding:14px 18px;margin-bottom:18px;'>"
        f"<div style='font-size:13px;font-weight:600;color:#0369a1;"
        f"margin-bottom:4px;'>✅ Deep Validation</div>"
        f"<div style='font-size:12px;color:#0c4a6e;line-height:1.7;'>"
        f"This validation evaluates extraction accuracy, signal credibility, and coverage "
        f"completeness using an enhanced reasoning pass over the extracted data. "
        f"Run on demand — results are cached for this session."
        f"</div></div>",
        unsafe_allow_html=True,
    )

    _VAL_KEY    = "_pdf_validation_result"
    _VAL_FP_KEY = "_pdf_validation_fp"

    import hashlib as _hashlib
    _full_text_for_hash = intelligence.get("full_text", "")
    _doc_hash = _hashlib.sha256(
        (intelligence.get("doc_type", "") + "|" + _full_text_for_hash).encode("utf-8")
    ).hexdigest()

    existing = st.session_state.get(_VAL_KEY)
    if existing and st.session_state.get(_VAL_FP_KEY) != _doc_hash:
        existing = None
        st.session_state.pop(_VAL_KEY, None)

    if not existing:
        _disk_result = _load_validation_result(_doc_hash)
        if _disk_result:
            existing = _disk_result
            st.session_state[_VAL_KEY]    = existing
            st.session_state[_VAL_FP_KEY] = _doc_hash

    st.session_state[_VAL_FP_KEY] = _doc_hash
    

    btn_label = "🔄 Re-run Validation" if existing else "▶ Run Validation"
    run_col, _ = st.columns([2, 5])
    with run_col:
        run_clicked = st.button(btn_label, key="_run_validation_btn",
                                use_container_width=True)

    if run_clicked:
        intel    = st.session_state.get("_pdf_intelligence", {})
        doc_type = intel.get("doc_type", "Legal")
        full_text = intel.get("full_text", "")
        analysis  = intel.get("analysis", {})
        extracted_entities = analysis.get("entities", {})
        detected_signals   = analysis.get("signals", [])
        azure_di_index     = intel.get("azure_di_index", {})

        _EMPTY_PLACEHOLDERS_VAL = {
            "not found", "n/a", "na", "none", "unknown", "not available",
            "not stated", "not specified", "not provided", "unspecified",
            "see narrative", "not applicable", "-", "—", "",
        }
        _displayed_kv    = _get_intelligence_kv(selected_sheet)
        _all_entities    = analysis.get("entities", {})
        extracted_entities = {
            k: v for k, v in _all_entities.items()
            if k in _displayed_kv
            and isinstance(v, dict)
            and (v.get("value") or "").strip().lower()
                not in _EMPTY_PLACEHOLDERS_VAL
        }
       

        if not full_text and not extracted_entities:
            st.error("No extracted data to validate. Run AI analysis first.")
            return

        with st.spinner("Running validation…"):
            try:
                from modules.pdf_intelligence import run_validation  # type: ignore[import]
                result = run_validation(
                    full_text=full_text,
                    doc_type=doc_type,
                    extracted_entities=extracted_entities,
                    detected_signals=detected_signals,
                    azure_di_fields=azure_di_index,
                )
                st.session_state[_VAL_KEY]    = result
                st.session_state[_VAL_FP_KEY] = _doc_hash
                _save_validation_result(_doc_hash, result)
                existing = result
                st.toast("✅ Validation complete!")
            except ImportError:
                st.error("pdf_intelligence module not found. Cannot run validation.")
                return
            except Exception as exc:
                st.error(f"Validation failed: {exc}")
                return

    if not existing:
        st.markdown(
            f"<div style='background:{_BG2};border:1px solid {_BORDER};"
            f"border-radius:8px;padding:24px;text-align:center;"
            f"color:{_LBL};font-family:monospace;font-size:12px;'>"
            f"Click <strong>▶ Run Validation</strong> to start deep quality evaluation."
            f"</div>",
            unsafe_allow_html=True,
        )
        return

    overall    = existing.get("overall_validation", {})
    ov_score   = overall.get("score",   0)
    ov_verdict = overall.get("verdict", "Review")
    ov_summary = overall.get("summary", "")
    ov_color   = "#16a34a" if ov_score >= 80 else "#ca8a04" if ov_score >= 60 else "#dc2626"
    ov_bg      = "#f0fdf4" if ov_score >= 80 else "#fffbeb" if ov_score >= 60 else "#fef2f2"

    st.markdown(
        f"<div style='background:{ov_bg};border:2px solid {ov_color}30;"
        f"border-radius:12px;padding:20px 24px;margin-bottom:22px;'>"
        f"<div style='display:flex;align-items:center;gap:16px;margin-bottom:10px;'>"
        f"<div style='text-align:center;'>"
        f"<div style='font-size:42px;font-weight:900;color:{ov_color};"
        f"font-family:monospace;line-height:1;'>{ov_score}</div>"
        f"<div style='font-size:10px;color:{_LBL};font-family:monospace;'>/ 100</div>"
        f"</div>"
        f"<div style='flex:1;'>"
        f"<div style='display:flex;align-items:center;gap:8px;margin-bottom:6px;'>"
        f"<span style='font-size:16px;font-weight:800;color:{_TXT};'>Overall Validation Score</span>"
        f"{_verdict_badge(ov_verdict)}"
        f"</div>"
        f"<div style='font-size:12px;color:{_TXT2};line-height:1.7;'>{ov_summary}</div>"
        f"</div></div>"
        f"<div style='height:6px;background:{_BORDER};border-radius:3px;overflow:hidden;'>"
        f"<div style='height:100%;width:{ov_score}%;background:{ov_color};"
        f"border-radius:3px;transition:width 0.5s;'></div></div>"
        f"</div>",
        unsafe_allow_html=True,
    )

    dims = [
        ("extraction_accuracy", "🎯", "Extraction",  "#2563eb"),
        ("signal_credibility",  "⚡", "Signals",     "#7c3aed"),
        ("coverage_analysis",   "📋", "Coverage",    "#059669"),
    ]
    pills_html = ""
    for key, icon, label, color in dims:
        d  = existing.get(key, {})
        s  = d.get("score", 0)
        v  = d.get("verdict", "—")
        c2 = "#16a34a" if s >= 80 else "#ca8a04" if s >= 60 else "#dc2626"
        pills_html += (
            f"<div style='background:{_BG2};border:1px solid {_BORDER};"
            f"border-radius:8px;padding:12px 16px;flex:1;min-width:150px;'>"
            f"<div style='font-size:18px;margin-bottom:4px;'>{icon}</div>"
            f"<div style='font-size:10px;color:{_LBL};font-family:monospace;"
            f"text-transform:uppercase;letter-spacing:1px;'>{label}</div>"
            f"<div style='font-size:24px;font-weight:800;color:{c2};"
            f"font-family:monospace;margin:4px 0;'>{s}</div>"
            f"<div style='font-size:10px;color:{_LBL};font-family:monospace;'>{v}</div>"
            f"</div>"
        )

    st.markdown(
        f"<div style='display:flex;gap:12px;flex-wrap:wrap;margin-bottom:22px;'>"
        f"{pills_html}</div>",
        unsafe_allow_html=True,
    )

    st.markdown(_section_header("Dimension Details"), unsafe_allow_html=True)
    _render_validation_dimension("🎯", "Extraction Accuracy",
                                  existing.get("extraction_accuracy", {}), "#2563eb")
    _render_validation_dimension("⚡", "Signal Credibility",
                                  existing.get("signal_credibility", {}),  "#7c3aed")
    _render_validation_dimension("📋", "Coverage Analysis",
                                  existing.get("coverage_analysis", {}),   "#059669")

    actions = overall.get("recommended_actions", [])
    if actions:
        st.markdown(_section_header("Recommended Actions"), unsafe_allow_html=True)
        for i, action in enumerate(actions, 1):
            st.markdown(
                f"<div style='background:{_BG2};border:1px solid {_BORDER};"
                f"border-radius:6px;padding:10px 16px;margin-bottom:6px;"
                f"display:flex;gap:12px;align-items:flex-start;'>"
                f"<span style='font-size:11px;font-weight:700;color:#ffffff;"
                f"background:#0284c7;border-radius:50%;width:20px;height:20px;"
                f"display:flex;align-items:center;justify-content:center;"
                f"flex-shrink:0;font-family:monospace;'>{i}</span>"
                f"<span style='font-size:13px;color:{_TXT};line-height:1.6;'>{action}</span>"
                f"</div>",
                unsafe_allow_html=True,
            )

    st.markdown("<div style='margin-top:16px;'></div>", unsafe_allow_html=True)
    _, clr_col = st.columns([5, 2])
    with clr_col:
        if st.button("🗑 Clear validation results", key="_clear_validation_btn",
                     use_container_width=True):
            st.session_state.pop(_VAL_KEY, None)
            st.session_state.pop(_VAL_FP_KEY, None)
            _save_validation_result(_doc_hash, {})
            st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# FEATURE C — TAB 7: LLM COST DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────

def _get_llm_price(model: str) -> tuple[float, float]:
    """Return (input_price_per_1k, output_price_per_1k) for a model name."""
    model_lower = model.lower()
    for key, prices in _LLM_PRICING.items():
        if key == "default":
            continue
        if key in model_lower:
            return prices["input"], prices["output"]
    return _LLM_PRICING["default"]["input"], _LLM_PRICING["default"]["output"]


def _compute_cost(entry: dict) -> float:
    model = entry.get("model", "")
    pt    = int(entry.get("prompt_tokens", 0))
    ct    = int(entry.get("completion_tokens", 0))
    ip, op = _get_llm_price(model)
    return (pt / 1000 * ip) + (ct / 1000 * op)


def _render_llm_cost_tab() -> None:
    st.markdown(_section_header("LLM Cost Dashboard", "token usage & estimated spend"), unsafe_allow_html=True)

    log: list[dict] = st.session_state.get("_llm_cost_log", [])

    # ── Empty state ───────────────────────────────────────────────────────────
    if not log:
        st.markdown(
            f"<div style='background:{_BG2};border:1px solid {_BORDER};"
            f"border-radius:10px;padding:28px 24px;text-align:center;'>"
            f"<div style='font-size:32px;margin-bottom:8px;'>💰</div>"
            f"<div style='font-size:14px;font-weight:700;color:{_TXT};"
            f"font-family:monospace;margin-bottom:8px;'>No cost data available yet</div>"
            f"<div style='font-size:12px;color:{_LBL};line-height:1.8;max-width:480px;margin:0 auto;'>"
            f"To populate this dashboard, your LLM call wrapper should append entries to<br>"
            f"<code style='background:{_BG3};padding:2px 6px;border-radius:3px;"
            f"font-size:11px;border:1px solid {_BORDER};'>st.session_state[\"_llm_cost_log\"]</code>"
            f" after each API call.<br><br>"
            f"Each entry should be a dict with:<br>"
            f"<code style='background:{_BG3};padding:4px 10px;border-radius:4px;"
            f"font-size:10px;display:inline-block;margin-top:6px;text-align:left;"
            f"border:1px solid {_BORDER};line-height:1.9;'>"
            f"task, model, prompt_tokens, completion_tokens, timestamp (opt), doc_name (opt)"
            f"</code>"
            f"</div></div>",
            unsafe_allow_html=True,
        )
        with st.expander("📋 How to wire up cost logging"):
            st.code(
                '''# Add this to your LLM call wrapper (e.g. in modules/pdf_intelligence.py)
import streamlit as st

def _log_llm_cost(task: str, model: str, response) -> None:
    usage = getattr(response, "usage", None)
    if not usage:
        return
    if "_llm_cost_log" not in st.session_state:
        st.session_state["_llm_cost_log"] = []
    st.session_state["_llm_cost_log"].append({
        "task":             task,
        "model":            model,
        "prompt_tokens":    usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "timestamp":        datetime.datetime.now().isoformat(),
        "doc_name":         st.session_state.get("_file_name", ""),
    })

response = client.chat.completions.create(...)
_log_llm_cost("Entity Extraction", deployment, response)
''',
                language="python",
            )
        adi_log_early: list[dict] = st.session_state.get("_adi_cost_log", [])
        if adi_log_early:
            st.markdown("<div style='margin-top:20px;'></div>", unsafe_allow_html=True)
            st.markdown(_section_header("Azure Document Intelligence Cost", "per-page processing"), unsafe_allow_html=True)
            adi_total_pages = sum(int(e.get("pages", 0)) for e in adi_log_early)
            adi_total_cost  = sum(float(e.get("cost", 0)) for e in adi_log_early)
            st.markdown(
                f"<div style='background:{_BG2};border:1px solid #bae6fd;"
                f"border-left:4px solid #0284c7;border-radius:8px;padding:14px 16px;'>"
                f"<div style='font-size:12px;font-family:monospace;color:{_TXT};'>"
                f"📄 {len(adi_log_early)} ADI call(s) · {adi_total_pages} page(s) · "
                f"<strong>${adi_total_cost:.4f}</strong> estimated ADI cost</div>"
                f"</div>",
                unsafe_allow_html=True,
            )
        return

    # ── Define all data up front ──────────────────────────────────────────────
    current_doc = st.session_state.get("_file_name", "")
    adi_log: list[dict] = st.session_state.get("_adi_cost_log", [])

    doc_log = [e for e in log if e.get("doc_name", "") == current_doc] if current_doc else []

    if doc_log:
        doc_prompt     = sum(int(e.get("prompt_tokens", 0))     for e in doc_log)
        doc_completion = sum(int(e.get("completion_tokens", 0)) for e in doc_log)
        doc_tokens     = doc_prompt + doc_completion
        doc_cost       = sum(_compute_cost(e) for e in doc_log)
        doc_calls      = len(doc_log)
    else:
        doc_prompt = doc_completion = doc_tokens = doc_cost = doc_calls = 0

    adi_log_cur   = [e for e in adi_log if e.get("doc_name", "") == current_doc]
    adi_cur_pages = sum(int(e.get("pages", 0))   for e in adi_log_cur)
    adi_cur_cost  = sum(float(e.get("cost", 0))  for e in adi_log_cur)

    total_prompt     = sum(int(e.get("prompt_tokens", 0))     for e in log)
    total_completion = sum(int(e.get("completion_tokens", 0)) for e in log)
    total_tokens     = total_prompt + total_completion
    total_cost       = sum(_compute_cost(e) for e in log)
    call_count       = len(log)
    avg_cost         = total_cost / call_count if call_count else 0.0

    adi_total_pages = sum(int(e.get("pages", 0))   for e in adi_log)
    adi_total_cost  = sum(float(e.get("cost", 0))  for e in adi_log)
    adi_calls       = len(adi_log)

    # ── Hero header ───────────────────────────────────────────────────────────
    st.markdown(
        f"<div style='background:linear-gradient(135deg,#eff6ff 0%,#dbeafe 50%,#bfdbfe 100%);"
        f"border:1px solid #93c5fd;border-radius:12px;padding:24px 28px;margin-bottom:24px;"
        f"box-shadow:0 4px 24px rgba(30,27,75,0.18);'>"
        f"<div style='display:flex;align-items:center;justify-content:space-between;'>"
        f"<div>"
        f"<div style='font-size:22px;font-weight:900;color:#1e40af;"
        f"font-family:monospace;letter-spacing:2px;'>💰 COST INTELLIGENCE</div>"
        f"<div style='font-size:12px;color:#1d4ed8;font-family:monospace;margin-top:4px;'>"
        f"Real-time token usage & spend tracking · {call_count} API call(s) this session</div>"
        f"</div>"
        f"<div style='background:#ffffff;border:1px solid #93c5fd;"
        f"border-radius:8px;padding:8px 16px;text-align:right;'>"
        f"<div style='font-size:10px;color:#1d4ed8;font-family:monospace;"
        f"text-transform:uppercase;letter-spacing:1px;'>Session Total</div>"
        f"<div style='font-size:28px;font-weight:900;color:#059669;"
        f"font-family:monospace;'>${total_cost:.4f}</div>"
        f"</div></div></div>",
        unsafe_allow_html=True,
    )

    # ── Current file section ──────────────────────────────────────────────────
    if doc_log or adi_log_cur:
        st.markdown(
            f"<div style='font-size:11px;font-weight:800;color:#1e293b;"
            f"font-family:monospace;text-transform:uppercase;letter-spacing:2px;"
            f"margin-bottom:10px;'>📄 Current File — {current_doc}</div>",
            unsafe_allow_html=True,
        )

    if doc_log:
        dk1, dk2, dk3, dk4 = st.columns(4)
        for col, icon, label, value, sub, grad, border in [
            (dk1, "🔁", "LLM CALLS",     str(doc_calls),   "this file",
             "linear-gradient(135deg,#eff6ff,#dbeafe)", "#2563eb"),
            (dk2, "🪙", "PROMPT TOKENS", f"{doc_prompt:,}", "input tokens",
             "linear-gradient(135deg,#f5f3ff,#ede9fe)", "#7c3aed"),
            (dk3, "📤", "OUTPUT TOKENS", f"{doc_completion:,}", "completion tokens",
             "linear-gradient(135deg,#fdf4ff,#fae8ff)", "#a855f7"),
            (dk4, "💰", "FILE COST",     f"${doc_cost:.4f}", f"total tokens: {doc_tokens:,}",
             "linear-gradient(135deg,#fefce8,#fef9c3)", "#ca8a04"),
        ]:
            with col:
                st.markdown(
                    f"<div style='background:{grad};border:1px solid {border}30;"
                    f"border-radius:10px;padding:16px;text-align:center;"
                    f"box-shadow:0 2px 8px {border}15;margin-bottom:4px;'>"
                    f"<div style='font-size:20px;margin-bottom:4px;'>{icon}</div>"
                    f"<div style='font-size:9px;color:#334155;font-family:monospace;"
                    f"text-transform:uppercase;letter-spacing:1.5px;font-weight:700;'>{label}</div>"
                    f"<div style='font-size:22px;font-weight:900;color:{border};"
                    f"font-family:monospace;margin:6px 0;'>{value}</div>"
                    f"<div style='font-size:10px;color:#475569;font-family:monospace;'>{sub}</div>"
                    f"</div>",
                    unsafe_allow_html=True,
                )

    if adi_log_cur:
        ac1, ac2, ac3 = st.columns(3)
        for col, icon, label, value, sub, grad, border in [
            (ac1, "📄", "ADI CALLS",     str(len(adi_log_cur)), "this file",
             "linear-gradient(135deg,#e0f2fe,#bae6fd)", "#0284c7"),
            (ac2, "📃", "ADI PAGES",     str(adi_cur_pages),
             f"@ ${float(os.environ.get('ADI_PRICE_PER_PAGE','0.01')):.3f}/page",
             "linear-gradient(135deg,#f5f3ff,#ede9fe)", "#7c3aed"),
            (ac3, "💰", "ADI FILE COST", f"${adi_cur_cost:.4f}",
             f"LLM+ADI: ${doc_cost + adi_cur_cost:.4f}",
             "linear-gradient(135deg,#f0fdf4,#dcfce7)", "#059669"),
        ]:
            with col:
                st.markdown(
                    f"<div style='background:{grad};border:1px solid {border}30;"
                    f"border-radius:10px;padding:16px;text-align:center;"
                    f"box-shadow:0 2px 8px {border}15;margin-bottom:4px;'>"
                    f"<div style='font-size:20px;margin-bottom:4px;'>{icon}</div>"
                    f"<div style='font-size:9px;color:#334155;font-family:monospace;"
                    f"text-transform:uppercase;letter-spacing:1.5px;font-weight:700;'>{label}</div>"
                    f"<div style='font-size:22px;font-weight:900;color:{border};"
                    f"font-family:monospace;margin:6px 0;'>{value}</div>"
                    f"<div style='font-size:10px;color:#475569;font-family:monospace;'>{sub}</div>"
                    f"</div>",
                    unsafe_allow_html=True,
                )

    if doc_log or adi_log_cur:
        st.markdown(
            f"<div style='height:1px;background:linear-gradient(90deg,#bfdbfe,transparent);"
            f"margin:12px 0 24px 0;'></div>",
            unsafe_allow_html=True,
        )

    # ── Session KPI cards ─────────────────────────────────────────────────────
    st.markdown(
        f"<div style='font-size:11px;font-weight:800;color:#1e293b;"
        f"font-family:monospace;text-transform:uppercase;letter-spacing:2px;"
        f"margin-bottom:10px;'>🕐 Session Total</div>",
        unsafe_allow_html=True,
    )
    k1, k2, k3, k4 = st.columns(4)
    for col, icon, label, value, sub, grad, border in [
        (k1, "🔁", "LLM CALLS",    str(call_count),       "this session",
         "linear-gradient(135deg,#eff6ff,#dbeafe)", "#2563eb"),
        (k2, "🪙", "TOTAL TOKENS", f"{total_tokens:,}",
         f"↑{total_prompt:,} in · ↓{total_completion:,} out",
         "linear-gradient(135deg,#f5f3ff,#ede9fe)", "#7c3aed"),
        (k3, "💵", "LLM COST",     f"${total_cost:.4f}",  f"≈ {total_cost*100:.2f}¢",
         "linear-gradient(135deg,#f0fdf4,#dcfce7)", "#16a34a"),
        (k4, "📊", "AVG / CALL",   f"${avg_cost:.4f}",
         f"avg tokens: {total_tokens//call_count if call_count else 0:,}",
         "linear-gradient(135deg,#fffbeb,#fef3c7)", "#d97706"),
    ]:
        with col:
            st.markdown(
                f"<div style='background:{grad};border:1px solid {border}30;"
                f"border-radius:10px;padding:16px;text-align:center;"
                f"box-shadow:0 2px 8px {border}15;margin-bottom:4px;'>"
                f"<div style='font-size:20px;margin-bottom:4px;'>{icon}</div>"
                f"<div style='font-size:9px;color:#334155;font-family:monospace;"
                f"text-transform:uppercase;letter-spacing:1.5px;font-weight:700;'>{label}</div>"
                f"<div style='font-size:24px;font-weight:900;color:{border};"
                f"font-family:monospace;margin:6px 0;'>{value}</div>"
                f"<div style='font-size:10px;color:#475569;font-family:monospace;'>{sub}</div>"
                f"</div>",
                unsafe_allow_html=True,
            )

    # ── Session ADI cards ─────────────────────────────────────────────────────
    if adi_log:
        st.markdown("<div style='margin-top:20px;'></div>", unsafe_allow_html=True)
        st.markdown(_section_header("Azure Document Intelligence Cost", "per-page processing"), unsafe_allow_html=True)
        a1, a2, a3 = st.columns(3)
        for col, icon, label, value, sub, grad, border in [
            (a1, "📄", "ADI API CALLS", str(adi_calls),
             "calls to prebuilt-document model",
             "linear-gradient(135deg,#e0f2fe,#bae6fd)", "#0284c7"),
            (a2, "📃", "TOTAL PAGES",   str(adi_total_pages),
             f"@ ${float(os.environ.get('ADI_PRICE_PER_PAGE','0.01')):.3f}/page",
             "linear-gradient(135deg,#f5f3ff,#ede9fe)", "#7c3aed"),
            (a3, "💰", "ADI EST. COST", f"${adi_total_cost:.4f}",
             f"LLM+ADI combined: ${total_cost + adi_total_cost:.4f}",
             "linear-gradient(135deg,#f0fdf4,#dcfce7)", "#059669"),
        ]:
            with col:
                st.markdown(
                    f"<div style='background:{grad};border:1px solid {border}30;"
                    f"border-radius:10px;padding:16px;text-align:center;"
                    f"box-shadow:0 2px 8px {border}15;margin-bottom:12px;'>"
                    f"<div style='font-size:20px;margin-bottom:4px;'>{icon}</div>"
                    f"<div style='font-size:9px;color:#334155;font-family:monospace;"
                    f"text-transform:uppercase;letter-spacing:1.5px;font-weight:700;'>{label}</div>"
                    f"<div style='font-size:24px;font-weight:900;color:{border};"
                    f"font-family:monospace;margin:6px 0;'>{value}</div>"
                    f"<div style='font-size:10px;color:#475569;font-family:monospace;'>{sub}</div>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
        for e in reversed(adi_log[-10:]):
            e_ts   = (e.get("timestamp", "")[:19] or "").replace("T", " ")
            e_doc  = e.get("doc_name", "")
            e_pg   = int(e.get("pages", 0))
            e_cost = float(e.get("cost", 0))
            st.markdown(
                f"<div style='background:#f8fafc;border:1px solid #e2e8f0;"
                f"border-left:4px solid #0284c7;border-radius:6px;"
                f"padding:10px 14px;margin-bottom:6px;"
                f"display:flex;align-items:center;gap:12px;flex-wrap:wrap;'>"
                f"<span style='font-size:10px;font-weight:700;color:#0284c7;"
                f"font-family:monospace;background:#e0f2fe;border:1px solid #bae6fd;"
                f"border-radius:4px;padding:2px 8px;'>Azure DI</span>"
                f"<span style='font-size:10px;color:#64748b;font-family:monospace;'>📄 {e_doc}</span>"
                f"<span style='font-size:10px;color:#64748b;font-family:monospace;'>{e_pg} page(s)</span>"
                f"<span style='font-size:10px;color:#64748b;font-family:monospace;'>⏱ {e_ts}</span>"
                f"<span style='margin-left:auto;font-size:11px;font-weight:700;"
                f"color:#059669;background:#f0fdf4;border:1px solid #bbf7d060;"
                f"border-radius:12px;padding:2px 10px;font-family:monospace;'>"
                f"${e_cost:.4f}</span>"
                f"</div>",
                unsafe_allow_html=True,
            )

    st.markdown("<div style='margin-top:24px;'></div>", unsafe_allow_html=True)

    # ── Current file per-task table ───────────────────────────────────────────
    if doc_log:
        from collections import defaultdict as _dd
        doc_task_groups: dict = _dd(list)
        for e in doc_log:
            doc_task_groups[e.get("task", "Unknown")].append(e)
        doc_sorted = sorted(doc_task_groups.items(),
                            key=lambda x: sum(_compute_cost(e) for e in x[1]), reverse=True)
        _TASK_COLORS_DOC = ["#2563eb","#7c3aed","#059669","#dc2626",
                            "#ca8a04","#0284c7","#db2777","#16a34a"]
        doc_task_color_map: dict = {}
        for i, (tn, _) in enumerate(doc_sorted):
            doc_task_color_map[tn] = _TASK_COLORS_DOC[i % len(_TASK_COLORS_DOC)]

        header_cells_doc = "".join(
            f"<th style='padding:12px 16px;text-align:left;font-size:12px;"
            f"font-weight:700;color:#ffffff;font-family:monospace;"
            f"text-transform:uppercase;letter-spacing:1px;"
            f"border-right:1px solid #334155;white-space:nowrap;'>{col}</th>"
            for col in ["Task","Model","Calls","Prompt Tokens","Compl. Tokens","Cost","% of File"]
        )

        rows_doc = ""
        for i, (task_name, entries) in enumerate(doc_sorted):
            t_pt   = sum(int(e.get("prompt_tokens", 0))     for e in entries)
            t_ct   = sum(int(e.get("completion_tokens", 0)) for e in entries)
            t_cost = sum(_compute_cost(e) for e in entries)
            t_n    = len(entries)
            models = list(dict.fromkeys(e.get("model", "?") for e in entries))
            mstr   = ", ".join(models[:2]) + ("..." if len(models) > 2 else "")
            pct    = (t_cost / doc_cost * 100) if doc_cost > 0 else 0
            row_bg = "#ffffff" if i % 2 == 0 else "#f8fafc"
            cc     = "#16a34a" if t_cost < 0.01 else "#d97706" if t_cost < 0.05 else "#dc2626"
            pc     = "#16a34a" if pct < 30 else "#d97706" if pct < 60 else "#dc2626"
            tc     = doc_task_color_map.get(task_name, "#64748b")
            rows_doc += (
                f"<tr style='background:{row_bg};'>"
                f"<td style='padding:13px 16px;border-bottom:1px solid #e2e8f0;border-right:1px solid #e2e8f0;'>"
                f"<div style='font-size:14px;font-weight:700;color:#0f172a;font-family:monospace;'>{task_name}</div>"
                f"<div style='height:3px;background:#e2e8f0;border-radius:2px;width:80%;margin-top:6px;overflow:hidden;'>"
                f"<div style='height:100%;width:{pct:.0f}%;background:{tc};border-radius:2px;'></div></div></td>"
                f"<td style='padding:13px 16px;border-bottom:1px solid #e2e8f0;border-right:1px solid #e2e8f0;font-size:13px;font-weight:500;color:#334155;font-family:monospace;'>{mstr}</td>"
                f"<td style='padding:13px 16px;border-bottom:1px solid #e2e8f0;border-right:1px solid #e2e8f0;font-size:15px;font-weight:800;color:#2563eb;font-family:monospace;text-align:center;'>{t_n}</td>"
                f"<td style='padding:13px 16px;border-bottom:1px solid #e2e8f0;border-right:1px solid #e2e8f0;font-size:14px;font-weight:600;color:#475569;font-family:monospace;text-align:right;'>{t_pt:,}</td>"
                f"<td style='padding:13px 16px;border-bottom:1px solid #e2e8f0;border-right:1px solid #e2e8f0;font-size:14px;font-weight:600;color:#475569;font-family:monospace;text-align:right;'>{t_ct:,}</td>"
                f"<td style='padding:13px 16px;border-bottom:1px solid #e2e8f0;border-right:1px solid #e2e8f0;font-size:15px;font-weight:800;color:{cc};font-family:monospace;text-align:right;'>${t_cost:.4f}</td>"
                f"<td style='padding:13px 16px;border-bottom:1px solid #e2e8f0;font-size:14px;font-weight:700;color:{pc};font-family:monospace;text-align:right;'>{pct:.1f}%</td>"
                f"</tr>"
            )

        total_row_doc = (
            f"<tr style='background:#1e293b;'>"
            f"<td style='padding:14px 16px;border-right:1px solid #334155;font-size:14px;font-weight:900;color:#ffffff;font-family:monospace;text-transform:uppercase;'>Total</td>"
            f"<td style='padding:14px 16px;border-right:1px solid #334155;'></td>"
            f"<td style='padding:14px 16px;border-right:1px solid #334155;font-size:15px;font-weight:900;color:#93c5fd;font-family:monospace;text-align:center;'>{doc_calls}</td>"
            f"<td style='padding:14px 16px;border-right:1px solid #334155;font-size:14px;font-weight:700;color:#93c5fd;font-family:monospace;text-align:right;'>{doc_prompt:,}</td>"
            f"<td style='padding:14px 16px;border-right:1px solid #334155;font-size:14px;font-weight:700;color:#93c5fd;font-family:monospace;text-align:right;'>{doc_completion:,}</td>"
            f"<td style='padding:14px 16px;border-right:1px solid #334155;font-size:15px;font-weight:900;color:#4ade80;font-family:monospace;text-align:right;'>${doc_cost:.4f}</td>"
            f"<td style='padding:14px 16px;font-size:14px;font-weight:700;color:#4ade80;font-family:monospace;text-align:right;'>100%</td>"
            f"</tr>"
        )

        st.markdown(
            f"<div style='margin-bottom:20px;'>"
            f"<div style='font-size:13px;font-weight:800;color:#1e293b;"
            f"font-family:monospace;text-transform:uppercase;letter-spacing:2px;"
            f"margin-bottom:14px;'>Per-Task Breakdown — Current File</div>"
            f"<div style='border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;'>"
            f"<table style='width:100%;border-collapse:collapse;'>"
            f"<thead><tr style='background:#1e293b;'>{header_cells_doc}</tr></thead>"
            f"<tbody>{rows_doc}</tbody>"
            f"<tfoot>{total_row_doc}</tfoot>"
            f"</table></div></div>",
            unsafe_allow_html=True,
        )

        st.markdown(
            f"<div style='height:1px;background:linear-gradient(90deg,#bfdbfe,transparent);"
            f"margin-bottom:24px;'></div>",
            unsafe_allow_html=True,
        )

    # ── Session per-task table ────────────────────────────────────────────────
    from collections import defaultdict
    task_groups: dict[str, list[dict]] = defaultdict(list)
    for e in log:
        task_groups[e.get("task", "Unknown")].append(e)
    sorted_tasks = sorted(task_groups.items(),
                          key=lambda x: sum(_compute_cost(e) for e in x[1]), reverse=True)

    header_cells = "".join(
        f"<th style='padding:12px 16px;text-align:left;font-size:12px;"
        f"font-weight:700;color:#ffffff;font-family:monospace;"
        f"text-transform:uppercase;letter-spacing:1px;"
        f"border-right:1px solid #334155;white-space:nowrap;'>{col}</th>"
        for col in ["Task","Model","Calls","Prompt Tokens","Compl. Tokens","Cost","% of Total"]
    )

    rows_html = ""
    for i, (task_name, entries) in enumerate(sorted_tasks):
        t_prompt     = sum(int(e.get("prompt_tokens", 0))     for e in entries)
        t_completion = sum(int(e.get("completion_tokens", 0)) for e in entries)
        t_cost       = sum(_compute_cost(e) for e in entries)
        t_calls      = len(entries)
        model_names  = list(dict.fromkeys(e.get("model", "?") for e in entries))
        model_str    = ", ".join(model_names[:2]) + ("..." if len(model_names) > 2 else "")
        pct          = (t_cost / total_cost * 100) if total_cost > 0 else 0
        row_bg       = "#ffffff" if i % 2 == 0 else "#f8fafc"
        cost_color   = "#16a34a" if t_cost < 0.01 else "#d97706" if t_cost < 0.05 else "#dc2626"
        pct_color    = "#16a34a" if pct < 30 else "#d97706" if pct < 60 else "#dc2626"
        rows_html += (
            f"<tr style='background:{row_bg};'>"
            f"<td style='padding:13px 16px;border-bottom:1px solid #e2e8f0;border-right:1px solid #e2e8f0;'>"
            f"<div style='font-size:14px;font-weight:700;color:#0f172a;font-family:monospace;'>{task_name}</div>"
            f"<div style='height:3px;background:#e2e8f0;border-radius:2px;width:80%;margin-top:6px;overflow:hidden;'>"
            f"<div style='height:100%;width:{pct:.0f}%;background:{pct_color};border-radius:2px;'></div></div></td>"
            f"<td style='padding:13px 16px;border-bottom:1px solid #e2e8f0;border-right:1px solid #e2e8f0;font-size:13px;font-weight:500;color:#334155;font-family:monospace;'>{model_str}</td>"
            f"<td style='padding:13px 16px;border-bottom:1px solid #e2e8f0;border-right:1px solid #e2e8f0;font-size:15px;font-weight:800;color:#2563eb;font-family:monospace;text-align:center;'>{t_calls}</td>"
            f"<td style='padding:13px 16px;border-bottom:1px solid #e2e8f0;border-right:1px solid #e2e8f0;font-size:14px;font-weight:600;color:#475569;font-family:monospace;text-align:right;'>{t_prompt:,}</td>"
            f"<td style='padding:13px 16px;border-bottom:1px solid #e2e8f0;border-right:1px solid #e2e8f0;font-size:14px;font-weight:600;color:#475569;font-family:monospace;text-align:right;'>{t_completion:,}</td>"
            f"<td style='padding:13px 16px;border-bottom:1px solid #e2e8f0;border-right:1px solid #e2e8f0;font-size:15px;font-weight:800;color:{cost_color};font-family:monospace;text-align:right;'>${t_cost:.4f}</td>"
            f"<td style='padding:13px 16px;border-bottom:1px solid #e2e8f0;font-size:14px;font-weight:700;color:{pct_color};font-family:monospace;text-align:right;'>{pct:.1f}%</td>"
            f"</tr>"
        )

    total_row = (
        f"<tr style='background:#1e293b;'>"
        f"<td style='padding:14px 16px;border-right:1px solid #334155;font-size:14px;font-weight:900;color:#ffffff;font-family:monospace;text-transform:uppercase;letter-spacing:1px;'>Total</td>"
        f"<td style='padding:14px 16px;border-right:1px solid #334155;'></td>"
        f"<td style='padding:14px 16px;border-right:1px solid #334155;font-size:15px;font-weight:900;color:#93c5fd;font-family:monospace;text-align:center;'>{call_count}</td>"
        f"<td style='padding:14px 16px;border-right:1px solid #334155;font-size:14px;font-weight:700;color:#93c5fd;font-family:monospace;text-align:right;'>{total_prompt:,}</td>"
        f"<td style='padding:14px 16px;border-right:1px solid #334155;font-size:14px;font-weight:700;color:#93c5fd;font-family:monospace;text-align:right;'>{total_completion:,}</td>"
        f"<td style='padding:14px 16px;border-right:1px solid #334155;font-size:15px;font-weight:900;color:#4ade80;font-family:monospace;text-align:right;'>${total_cost:.4f}</td>"
        f"<td style='padding:14px 16px;font-size:14px;font-weight:700;color:#4ade80;font-family:monospace;text-align:right;'>100%</td>"
        f"</tr>"
    )

    st.markdown(
        f"<div style='margin-bottom:20px;'>"
        f"<div style='font-size:13px;font-weight:800;color:#1e293b;"
        f"font-family:monospace;text-transform:uppercase;letter-spacing:2px;"
        f"margin-bottom:14px;'>Per-Task Breakdown — Session</div>"
        f"<div style='border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;'>"
        f"<table style='width:100%;border-collapse:collapse;'>"
        f"<thead><tr style='background:#1e293b;'>{header_cells}</tr></thead>"
        f"<tbody>{rows_html}</tbody>"
        f"<tfoot>{total_row}</tfoot>"
        f"</table></div></div>",
        unsafe_allow_html=True,
    )

    st.markdown("<div style='margin-top:24px;'></div>", unsafe_allow_html=True)

    # ── Pricing reference ─────────────────────────────────────────────────────
    with st.expander("💡 Pricing reference (per 1K tokens) — updated April 2026"):
        _DISPLAY_MODELS = [
            ("🚀", "GPT-5.4",         "gpt-5.4",         "#7c3aed", "#f5f3ff"),
            ("⚡", "GPT-5.4 Mini",    "gpt-5.4-mini",    "#7c3aed", "#f5f3ff"),
            ("📐", "GPT-4.1",         "gpt-4.1",         "#2563eb", "#eff6ff"),
            ("📐", "GPT-4.1 Mini",    "gpt-4.1-mini",    "#2563eb", "#eff6ff"),
            ("🌀", "GPT-4o",          "gpt-4o",          "#0284c7", "#e0f2fe"),
            ("🌀", "GPT-4o Mini",     "gpt-4o-mini",     "#0284c7", "#e0f2fe"),
            ("🧠", "o3",              "o3",              "#059669", "#f0fdf4"),
            ("🧠", "o4-mini",         "o4-mini",         "#059669", "#f0fdf4"),
            ("🤖", "Claude Sonnet 4", "claude-sonnet-4", "#dc2626", "#fef2f2"),
            ("🤖", "Claude Haiku 3",  "claude-3-haiku",  "#dc2626", "#fef2f2"),
        ]
        cols_per_row = 2
        rows = [_DISPLAY_MODELS[i:i+cols_per_row] for i in range(0, len(_DISPLAY_MODELS), cols_per_row)]
        for row in rows:
            col_objs = st.columns(cols_per_row)
            for col_obj, (icon, label, model_key, color, bg) in zip(col_objs, row):
                prices = _LLM_PRICING.get(model_key, _LLM_PRICING["default"])
                with col_obj:
                    st.markdown(
                        f"<div style='background:{bg};border-radius:14px;padding:20px 24px;"
                        f"margin-bottom:14px;box-shadow:0 4px 16px {color}22;border:1px solid {color}30;'>"
                        f"<div style='display:flex;align-items:center;gap:12px;margin-bottom:16px;'>"
                        f"<div style='width:44px;height:44px;border-radius:50%;background:{color};"
                        f"display:flex;align-items:center;justify-content:center;font-size:20px;"
                        f"flex-shrink:0;box-shadow:0 2px 8px {color}40;'>{icon}</div>"
                        f"<div><div style='font-size:16px;font-weight:800;color:#0f172a;"
                        f"font-family:monospace;line-height:1.2;'>{label}</div>"
                        f"<div style='font-size:10px;color:#64748b;font-family:monospace;"
                        f"margin-top:2px;'>{model_key}</div></div></div>"
                        f"<div style='display:grid;grid-template-columns:1fr 1fr;gap:10px;'>"
                        f"<div style='background:#ffffff;border:1px solid {color}30;"
                        f"border-top:3px solid #16a34a;border-radius:10px;padding:14px 12px;text-align:center;'>"
                        f"<div style='font-size:9px;font-weight:800;color:#166534;font-family:monospace;"
                        f"text-transform:uppercase;letter-spacing:1.5px;margin-bottom:8px;'>📥 Input</div>"
                        f"<div style='font-size:22px;font-weight:900;color:#15803d;font-family:monospace;"
                        f"line-height:1;'>${prices['input']:.5f}</div>"
                        f"<div style='font-size:9px;color:#4ade80;font-family:monospace;margin-top:6px;"
                        f"background:#f0fdf4;border-radius:20px;padding:2px 8px;display:inline-block;'>"
                        f"per 1K tokens</div></div>"
                        f"<div style='background:#ffffff;border:1px solid {color}30;"
                        f"border-top:3px solid #dc2626;border-radius:10px;padding:14px 12px;text-align:center;'>"
                        f"<div style='font-size:9px;font-weight:800;color:#991b1b;font-family:monospace;"
                        f"text-transform:uppercase;letter-spacing:1.5px;margin-bottom:8px;'>📤 Output</div>"
                        f"<div style='font-size:22px;font-weight:900;color:#dc2626;font-family:monospace;"
                        f"line-height:1;'>${prices['output']:.5f}</div>"
                        f"<div style='font-size:9px;color:#f87171;font-family:monospace;margin-top:6px;"
                        f"background:#fef2f2;border-radius:20px;padding:2px 8px;display:inline-block;'>"
                        f"per 1K tokens</div></div>"
                        f"</div></div>",
                        unsafe_allow_html=True,
                    )

    # ── Clear button ──────────────────────────────────────────────────────────
    st.markdown("<div style='margin-top:16px;'></div>", unsafe_allow_html=True)
    _, clr_col = st.columns([5, 2])
    with clr_col:
        if st.button("🗑 Clear cost log", key="_clear_cost_log_btn", use_container_width=True):
            st.session_state["_llm_cost_log"] = []
            st.session_state["_adi_cost_log"] = []
            st.toast("Cost log cleared.")
            st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def render_pdf_analysis_panel(
    intelligence: dict,
    uploaded_name: str,
    selected_sheet: str,
) -> None:
    st.session_state["_file_name"] = uploaded_name

    st.markdown(_UPLOADER_PLUS_CSS + _DIALOG_CSS, unsafe_allow_html=True)

    doc_type = intelligence.get("doc_type", "Legal")
    meta     = _get_doc_type_meta(doc_type)
    subtype  = intelligence.get("analysis", {}).get("detected_subtype") or ""

    # REPLACE the fingerprint change block in render_pdf_analysis_panel:
    _intel_fp = (
        uploaded_name
        + "|" + str(intelligence.get("page_count", 0))
        + "|" + str(intelligence.get("doc_type", ""))
        + "|" + str(len(intelligence.get("full_text", "")))
        + "|" + intelligence.get("full_text", "")[:80]
    )
    _prev_fp = st.session_state.get("_pdf_analysis_intel_fp")
    if _prev_fp != _intel_fp:
        for _stale_key in (
            "_adi_lookup",
            "_adi_lookup_file",
            "_pdf_analysis_current_file",
            "_pdf_summary_override",
            "_pdf_intel_debug",
            "_pdf_edits",
            "_pdf_edit_hist",
            "_pdf_edit_mode_fields",
            "_pdf_delete_confirm",
        ):
            st.session_state.pop(_stale_key, None)
        for _k in list(st.session_state.keys()):
            if _k.startswith("_intel_entities_"):
                st.session_state.pop(_k, None)
        st.session_state["_pdf_analysis_intel_fp"]     = _intel_fp
        st.session_state["_pdf_analysis_current_file"] = uploaded_name

    _tmpdir  = st.session_state.get("tmpdir", "")
    pdf_path: str | None = None
    if _tmpdir:
        for _ext in (".pdf", ".PDF"):
            _c = os.path.join(_tmpdir, f"input{_ext}")
            if os.path.exists(_c):
                pdf_path = _c
                break

    subtype_html = f"&nbsp;&nbsp;{_subtype_badge(subtype)}" if subtype else ""
    st.markdown(
        f"<div style='background:{meta['bg']};border:1px solid {meta['color']}30;"
        f"border-radius:10px;padding:13px 18px;margin-bottom:14px;'>"
        f"<div style='display:flex;align-items:center;gap:12px;'>"
        f"<span style='font-size:22px;'>{meta['icon']}</span>"
        f"<div>"
        f"<div style='display:flex;align-items:center;gap:8px;flex-wrap:wrap;'>"
        f"<div style='font-size:14px;font-weight:700;color:{meta['color']};"
        f"font-family:monospace;text-transform:uppercase;letter-spacing:1.5px;'>"
        f"{doc_type} Document Analysis</div>"
        f"{subtype_html}"
        f"</div>"
        f"<div style='font-size:11px;color:{_LBL};margin-top:3px;'>"
        f"📄 {uploaded_name} · {selected_sheet} · "
        f"{intelligence.get('page_count', 0)} page(s)</div>"
        f"</div></div></div>",
        unsafe_allow_html=True,
    )

    intel_fields    = _get_intelligence_entities(selected_sheet)
    _entities_count = len(intel_fields) if intel_fields else len(_get_raw_fields(selected_sheet))

    _raw_signals   = intelligence.get("analysis", {}).get("signals", [])
    _synth_signals = _synthesize_signals_from_entities(intelligence) if not _raw_signals else []
    _signals_count = len(_raw_signals) if _raw_signals else len(_synth_signals)
    _signals_label = (
        f"⚡ Signals ({_signals_count}✦)"
        if (not _raw_signals and _synth_signals) else
        f"⚡ Signals ({_signals_count})"
    )

    # FEATURE C: Show cost badge in tab label if cost data exists
    _cost_log    = st.session_state.get("_llm_cost_log", [])
    _total_cost  = sum(_compute_cost(e) for e in _cost_log)
    _cost_label  = (
        f"💰 Cost Intelligence"
        if _cost_log else
        "💰 Cost Intelligence"
    )

    tabs = st.tabs([
        f"🔍 Entities",
        "📝 Summary",
        _signals_label,
        "📄 Raw JSON",
        "🔄 Transformation Journey",
        "✅ AI Assistant",
        _cost_label,                    # FEATURE C: new tab
    ])

    with tabs[0]:
        _render_entities_tab(intelligence, selected_sheet, pdf_path)
    with tabs[1]:
        _render_summary_tab(intelligence, selected_sheet)
    with tabs[2]:
        _render_signals_tab(intelligence)
    with tabs[3]:
        _render_raw_json_tab(intelligence, selected_sheet)
    with tabs[4]:
        _render_journey_tab(intelligence, selected_sheet, uploaded_name)
    with tabs[5]:
        _render_validation_tab(intelligence, selected_sheet)
    with tabs[6]:
        _render_llm_cost_tab() 