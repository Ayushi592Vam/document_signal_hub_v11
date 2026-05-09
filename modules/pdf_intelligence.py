"""
modules/pdf_intelligence.py  — v5

Key changes from v4:
  ─────────────────────────────────────────────────────────────────────────
  • NO MORE HARDCODED FIELD LISTS.
    All entity fields, signal types, type-specific fields, and LLM personas
    are now loaded from config/<doc_type>.yaml via modules/doc_config.py.

  • Sub-type detection:
    Within a doc type (e.g. FNOL), the config defines sub-types (auto /
    homeowners / commercial) with their own keyword sets.  doc_config
    detects the best sub-type from the document text and merges those
    extra fields into the entity prompt automatically.

  • Config-driven severity classification:
    _classify_severity_from_config() uses the YAML severity_triggers
    instead of the previous inline keyword lists in pdf_analysis.py.

  • Everything else (two-call architecture, JSON repair, debug mode,
    Azure DI index, validation pipeline) is unchanged from v4.
  ─────────────────────────────────────────────────────────────────────────

Architecture (unchanged):
  Call A — entities + signals        (standard model, max_tokens=3500)
  Call B — summary + type_specific   (standard model, max_tokens=1200)
  On-demand: run_validation()        (enhanced model, from Validation tab)
"""

from __future__ import annotations

import json
import os
import re
import textwrap
import hashlib

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG IMPORT  — graceful degradation if doc_config not yet installed
# ─────────────────────────────────────────────────────────────────────────────

try:
    from modules.doc_config import (   # type: ignore[import]
        load_config,
        detect_subtype,
        build_entity_field_list,
        build_type_specific_field_list,
        get_signal_types,
        get_role,
        get_severity_keywords,
        build_signal_detection_prompt,
    )
    _CONFIG_AVAILABLE = True
except ImportError:
    _CONFIG_AVAILABLE = False

    # ── Minimal stubs so the rest of the module runs even without doc_config ──
    def load_config(doc_type: str) -> dict:           # type: ignore[misc]
        return {}

    def detect_subtype(doc_type: str, text: str) -> None:  # type: ignore[misc]
        return None

    def build_entity_field_list(doc_type: str, subtype=None) -> str:  # type: ignore[misc]
        return _FALLBACK_ENTITIES.get(doc_type, _FALLBACK_ENTITIES["Legal"])

    def build_type_specific_field_list(doc_type: str) -> str:  # type: ignore[misc]
        return _FALLBACK_TYPE_SPECIFIC.get(doc_type, "")

    def get_signal_types(doc_type: str) -> str:       # type: ignore[misc]
        return "severity, legal_escalation, fraud_indicator, coverage_issue"

    def get_role(doc_type: str) -> str:               # type: ignore[misc]
        return f"expert insurance document analyst specialising in {doc_type} documents"

    def get_severity_keywords(doc_type: str) -> dict:  # type: ignore[misc]
        return {}

    def build_signal_detection_prompt(doc_type: str) -> str:
        return (
          f"Signal types to detect: "
          f"{get_signal_types(doc_type)}\n"
          "For each signal return: type, severity_level "
          "(Highly Severe|High|Moderate|Low), "
          "description, supporting_text (verbatim quote), "
          "trigger_matched, confidence."
    )    


# ── Fallback field lists (used only when doc_config import fails) ─────────────
_FALLBACK_ENTITIES = {
    "FNOL": (
        "Claim Number, Policy Number, Policyholder Name, Date of Loss, "
        "Time of Loss, Location of Loss, Cause of Loss, Description of Loss, "
        "Estimated Total Damage, Adjuster Name, Witness Name, Police Report Number, "
        "Any Injuries, Injury Description, Medical Facility"
    ),
    "Legal": (
        "Case Number, Filing Date, Last Refreshed, Filing Location, Filing Court, "
        "Judge, Category, Practice Area, Matter Type, Status, Case Last Update, "
        "Docket Prepared For, Line of Business, Docket, Circuit, Division, "
        "Cause of Loss, Cause of Action, Case Complaint Summary, "
        "Plaintiff Name, Plaintiff Attorney, Plaintiff Attorney Firm, "
        "Defendant Name, Defendant Attorney, Defendant Attorney Firm, "
        "Insurance Carrier, Policy Number, Coverage Type, "
        "Incident Date, Incident Location, Damages Sought"
    ),
    "Loss Run": (
        "Report Date, Policy Number, Policy Period Start, Policy Period End, "
        "Named Insured, Carrier, TPA Name, Line of Business, "
        "Total Claims Count, Open Claims Count, Closed Claims Count, "
        "Total Incurred, Total Paid, Total Reserve, Total Indemnity Paid, "
        "Total Medical Paid, Total Expense Paid, Largest Claim Amount, "
        "Average Claim Amount, Loss Ratio, Combined Ratio"
    ),
    "Medical": (
        "Patient Name, Patient Date of Birth, Patient Gender, Patient ID, "
        "Provider Name, Provider NPI, Provider Facility, Provider Address, "
        "Date of Service, Date of Injury, Primary Diagnosis, Primary ICD Code, "
        "Secondary Diagnoses, Procedure Codes, Treatment Description, "
        "Medications Prescribed, Total Charges, Amount Paid, Amount Denied, "
        "Adjustment Amount, Patient Responsibility, Insurance ID, Group Number, "
        "Authorization Number, Attending Physician, Referring Physician"
    ),
     "Underwriting": (
        "Submission Reference, Date of Submission, Requested Effective Date, "
        "Business Name, Business Address, NAICS / SIC Code, Years in Business, "
        "Policy Number, Coverage Type, Coverage Limit, Deductible, "
        "Prior Loss Indicator, Number of Prior Losses, Total Prior Loss Amount, "
        "Broker Name, Underwriter Name, Risk Description, Prior Carrier"
    ),
}
_FALLBACK_TYPE_SPECIFIC = {
    "FNOL":     "Severity, Litigation Risk, Fraud Indicator, Coverage Concern, Estimated Loss Amount, Recommended Next Step",
    "Legal":    "Severity, Litigation Stage, Coverage Issue, Estimated Exposure, Reservation of Rights, Recommended Defense Strategy",
    "Loss Run": "Portfolio Severity, Frequency Trend, Litigation Rate, Large Loss Count, Large Loss Threshold, Recommended Reserve Action",
    "Medical":  "Severity, Medical Complexity, Treatment Duration, Disability Type, MMI Status, Causation Opinion, Fraud Indicator, Recommended IME",
    "Underwriting": 
        "Risk Appetite Score, Bind / Decline Recommendation, CAT Exposure Flag, "
        "Adequacy Ratio, Estimated Annual Premium, Loss Ratio Trend, "
        "Referral Required, Pricing Adequacy",
}


# ─────────────────────────────────────────────────────────────────────────────
# MODEL ROUTING  — internal only, never surfaced in the UI
# ─────────────────────────────────────────────────────────────────────────────

def _deployment_standard() -> str:
    return os.environ.get("OPENAI_DEPLOYMENT_NAME", "gpt-4o-mini")


def _deployment_enhanced() -> str:

    return os.environ.get("OPENAI_DEPLOYMENT_NAME_ENHANCED", "gpt-5.4")



# ─────────────────────────────────────────────────────────────────────────────
# AZURE OPENAI CLIENT
# ─────────────────────────────────────────────────────────────────────────────

def _get_openai_client():
    try:
        from openai import AzureOpenAI
        return AzureOpenAI(
            azure_endpoint=os.environ.get("OPENAI_DEPLOYMENT_ENDPOINT", ""),
            api_key=os.environ.get("OPENAI_API_KEY", ""),
            api_version=os.environ.get("OPENAI_API_VERSION", "2024-12-01-preview"),
        )
    except Exception:
        return None


# ── NEW: separate client for the enhanced (gpt-5.4) model ────────────────────
def _get_openai_client_enhanced():
    try:
        from openai import AzureOpenAI
        endpoint = os.environ.get("OPENAI_DEPLOYMENT_ENDPOINT_ENHANCED") \
                   or os.environ.get("OPENAI_DEPLOYMENT_ENDPOINT", "")
        api_key  = os.environ.get("OPENAI_API_KEY_ENHANCED") \
                   or os.environ.get("OPENAI_API_KEY", "")
        
        # ── TEMP DEBUG — remove after fixing ─────────────────────────────────
        print(f"[ENHANCED CLIENT] endpoint={endpoint[:40] if endpoint else 'MISSING'}")
        print(f"[ENHANCED CLIENT] key={'SET' if api_key else 'MISSING'}")
        print(f"[ENHANCED CLIENT] model={_deployment_enhanced()}")
        # ─────────────────────────────────────────────────────────────────────
        
        return AzureOpenAI(
            azure_endpoint=endpoint,
            api_key=api_key,
            api_version=os.environ.get("OPENAI_API_VERSION", "2024-12-01-preview"),
        )
    except Exception as e:
        print(f"[ENHANCED CLIENT] FAILED: {e}")   # ← TEMP DEBUG
        return None


# ─────────────────────────────────────────────────────────────────────────────
# JSON REPAIR  — handle truncated responses from token-limit hits
# ─────────────────────────────────────────────────────────────────────────────

def _repair_json(raw: str) -> str:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    raw = raw.strip()

    try:
        json.loads(raw)
        return raw
    except json.JSONDecodeError:
        pass

    stack: list[str] = []
    in_str = False
    escape = False
    for ch in raw:
        if escape:
            escape = False
            continue
        if ch == "\\" and in_str:
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in ("{", "["):
            stack.append("}" if ch == "{" else "]")
        elif ch in ("}", "]"):
            if stack and stack[-1] == ch:
                stack.pop()

    if in_str:
        raw += '"'
    raw = re.sub(r",\s*$", "", raw.rstrip())
    closing = "".join(reversed(stack))
    repaired = raw + closing

    try:
        json.loads(repaired)
        return repaired
    except json.JSONDecodeError:
        pass

    return raw



def _llm_call(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 3500,
    label: str = "llm_call",
    use_enhanced: bool = False,
) -> dict | None:
    print(f"[_llm_call] label={label} use_enhanced={use_enhanced}")
    client = _get_openai_client_enhanced() if use_enhanced else _get_openai_client()
    if not client:
        _debug_store(label, "ERROR: no client (check OPENAI env vars)")
        print(f"[LLM CALL] No client for label={label} use_enhanced={use_enhanced}")
        return None

    model = _deployment_enhanced() if use_enhanced else _deployment_standard()

    token_param = (
        {"max_completion_tokens": max_tokens}
        if use_enhanced else
        {"max_tokens": max_tokens}
    )

    try:
        response = client.chat.completions.create(
            model=model,
            temperature=0.0,
            seed=42,                          # ← OpenAI honours this for determinism
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            **token_param,
        )

        # ── LLM Cost Logging ──────────────────────────────────────────────
        try:
            import streamlit as st
            import datetime
            usage = getattr(response, "usage", None)
            if usage:
                if "_llm_cost_log" not in st.session_state:
                    st.session_state["_llm_cost_log"] = []
                st.session_state["_llm_cost_log"].append({
                    "task":              label,
                    "model":             model,
                    "prompt_tokens":     usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "timestamp":         datetime.datetime.now().isoformat(),
                    "doc_name":          (
                        st.session_state.get("_file_name")
                        or st.session_state.get("last_uploaded", "").split("_")[0]
                        or ""
                    ),
                })
        except Exception:
            pass  # never let cost logging break the main flow


        raw = response.choices[0].message.content or ""
        if not raw.strip().endswith("}"):
            _debug_store(label + "_TRUNCATED", raw)
        _debug_store(label, raw)
        repaired = _repair_json(raw)
        return json.loads(repaired)

    except json.JSONDecodeError as e:
        _debug_store(label + "_parse_error", str(e))
        print(f"[LLM CALL JSON ERROR] {label}: {e}")
        return None
    except Exception as e:
        _debug_store(label + "_error", str(e))
        print(f"[LLM CALL ERROR] {label}: {e}")
        return None

def _debug_store(key: str, value: str) -> None:
    if os.environ.get("PDF_INTEL_DEBUG", "0") != "1":
        return
    try:
        import streamlit as st
        bucket = st.session_state.setdefault("_pdf_intel_debug", {})
        bucket[key] = value
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — TEXT EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────


def extract_full_text_from_parsed(parsed: dict) -> str:
    """
    Extract full text from a parsed document dict.
 
    Supports multiple source formats:
      • PDF via Azure DI  — pages[n]["raw_text"]
      • TXT transcript    — pages[n]["text"] OR pages[n]["content"]
                            OR a top-level "text" / "content" key
                            OR a top-level "full_text" key
    """
    # ── Fast path: top-level full_text already assembled (e.g. parse_txt_file) ──
    if parsed.get("full_text"):
        return str(parsed["full_text"]).strip()
 
    # ── Top-level "text" or "content" (some TXT parsers emit this) ───────────
    for _top_key in ("text", "content", "transcript"):
        if parsed.get(_top_key):
            return str(parsed[_top_key]).strip()
 
    # ── Page-by-page (Azure DI PDF format) ───────────────────────────────────
    parts: list[str] = []
    for page in parsed.get("pages", []):
        # Try every plausible key name
        raw = (
            page.get("raw_text")
            or page.get("text")
            or page.get("content")
            or page.get("transcript")
            or ""
        ).strip()
        if raw:
            page_num = page.get("page_num", page.get("page", len(parts) + 1))
            parts.append(f"[PAGE {page_num}]\n{raw}")
 
    return "\n\n".join(parts)

def _build_azure_di_index_from_parsed(parsed: dict) -> dict:
    index: dict[str, dict] = {}
    for page in parsed.get("pages", []):
        for field in page.get("fields", []):
            fname = (field.get("field_name") or "").strip()
            if not fname:
                continue
            existing = index.get(fname)
            new_conf = float(field.get("confidence", 0.0))
            if existing is None or new_conf > float(existing.get("confidence", 0.0)):
                index[fname] = {
                    "value":            field.get("value", ""),
                    "confidence":       new_conf,
                    "bounding_polygon": field.get("bounding_polygon"),
                    "source_page":      field.get("source_page", page.get("page_num", 1)),
                    "page_width":       field.get("page_width",  8.5),
                    "page_height":      field.get("page_height", 11.0),
                }
    return index


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — CLASSIFICATION
# ─────────────────────────────────────────────────────────────────────────────

_CLASSIFICATION_SYSTEM = textwrap.dedent("""
You are a senior insurance document analyst. Classify the document into exactly one of:
  - FNOL        : First Notice of Loss — initial claim intake / notification
  - Legal       : Court documents, complaints, dockets, attorney correspondence, settlements
  - Loss Run    : Tabular claims history, TPA loss run, portfolio reports
  - Medical     : Medical records, bills, EOBs, treatment notes, IMEs
  - Underwriting : Insurance submissions, risk assessments, underwriting applications,
                   broker submissions, coverage requests, risk surveys


Respond ONLY with valid JSON. No preamble.

{
  "classification": "<FNOL|Legal|Loss Run|Medical|Underwriting>",
  "confidence": <0.0–1.0>,
  "reasoning": "<2-3 sentences>",
  "ambiguities": "<mixed signals or empty string>"
}
""").strip()


def classify_document(full_text: str) -> dict:
    result = _llm_call(
        system_prompt=_CLASSIFICATION_SYSTEM,
        user_prompt=f"Classify this document:\n\n{full_text[:3000]}",
        max_tokens=400,
        label="classify",
        use_enhanced=False,
    )


    # ── Simple fallback: just return the default, no broken retry ────────────

    if not result:
        return {
            "classification": "Legal",
            "confidence": 0.5,
            "reasoning": "LLM unavailable — defaulted to Legal.",
            "ambiguities": "",
        }
    return result


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — PROMPT BUILDERS (now config-driven, not hardcoded)
# ─────────────────────────────────────────────────────────────────────────────

_ENTITIES_SCHEMA = """
Return ONLY valid JSON — no markdown, no preamble.

IMPORTANT:
  • "azure_di_key" must be the EXACT key from the azure_di_fields dict provided
    (copy character-for-character). Set to null if not in that dict.
  • "value" must be the EXACT text from the document (do not paraphrase).
  • "confidence": 0.95+ explicit, 0.70–0.94 implied, <0.70 uncertain.
  • Extract ONLY the fields listed. Omit any not found in the document.
  • Keep source_text to a short 1-line snippet or omit to save tokens.

{
  "entities": {
    "<SEMANTIC_LABEL>": {
      "azure_di_key": "<exact Azure DI field name or null>",
      "value":        "<exact value>",
      "source_text":  "<short verbatim snippet, optional>",
      "confidence":   <0.0–1.0>
    }
  },
  "signals": [
    {
      "type":           "<signal_type>",
      "severity_level": "<Highly Severe|High|Moderate|Low>",
      "description":    "<plain-English explanation>",
      "supporting_text":"<verbatim quote, keep short>"
    }
  ]
}
"""

_SUMMARY_SCHEMA = """
Return ONLY valid JSON — no markdown, no preamble.

{
  "summary": "<200-word max factual summary>",
  "type_specific": {
    "<FIELD_NAME>": {
      "azure_di_key": "<exact Azure DI field name or null>",
      "value":        "<exact value>",
      "confidence":   <0.0–1.0>
    }
  },
  "judge": {
    "classification_reasoning": "<why this doc type>",
    "signal_validation":        "<are signals credible?>",
    "data_quality":             "<what is well-extracted vs missing>",
    "recommendations":          "<what a claims handler should do next>"
  }
}
"""

_VALIDATION_SCHEMA = """
Return ONLY valid JSON — no markdown, no preamble.

{
  "extraction_accuracy": {
    "score": <0–100>,
    "verdict": "<Pass|Fail|Review>",
    "findings": "<detailed assessment>",
    "missed_fields": ["<field>"],
    "incorrect_fields": [{"field": "<name>", "extracted": "<val>", "expected": "<val>"}]
  },
  "signal_credibility": {
    "score": <0–100>,
    "verdict": "<Credible|Questionable|Unsupported>",
    "findings": "<assessment>",
    "false_positives": ["<signal>"],
    "missed_signals": ["<signal>"]
  },
  "coverage_analysis": {
    "score": <0–100>,
    "verdict": "<Adequate|Gaps Identified|Critical Gaps>",
    "findings": "<assessment>",
    "gaps": ["<gap>"]
  },
  "overall_validation": {
    "score": <0–100>,
    "verdict": "<Validated|Needs Review|Failed>",
    "confidence": <0.0–1.0>,
    "summary": "<2-3 sentence assessment>",
    "recommended_actions": ["<action>"]
  }
}
"""

_GROUNDING_RULES = """
ANTI-HALLUCINATION RULES — MANDATORY:
  1. VERBATIM ONLY: Every value you extract must appear VERBATIM in the
     document text provided. Do not paraphrase, infer, or synthesise values.
  2. NO INVENTION: If a field is not explicitly present in the document,
     OMIT it entirely. Never guess or approximate.
  3. NO EXTERNAL KNOWLEDGE: Do not use your training knowledge to fill gaps.
     Only use what is written in this specific document.
  4. QUOTE TEST: Before including any value, ask yourself:
     "Can I find this exact string in the document text?" If no → omit it.
  5. DATE RULE: Never calculate or infer dates. Only extract dates that
     appear as explicit calendar values (e.g. "2024-01-15", "January 15 2024").
  6. CONFIDENCE HONESTY: If you are less than 70% certain a value is
     verbatim from the document, set confidence < 0.70.
     If you cannot verify it at all, omit the field.
  7. NO ASSUMPTIONS: "Unknown", "N/A", "Not stated" are NOT valid extracted
     values — they are admissions of absence. Omit instead.
"""


def _entities_system(doc_type: str, subtype: str | None = None) -> str:
    role          = get_role(doc_type)
    entity_fields = build_entity_field_list(doc_type, subtype)
    # signal_types  = get_signal_types(doc_type)

    

    subtype_note = (
        f"\nThis document appears to be a {subtype.upper()} sub-type of {doc_type}. "
        f"Pay special attention to the additional fields listed above.\n"
    ) if subtype else ""


    

    checkbox_rule = textwrap.dedent("""
CHECKBOX FIELDS — CRITICAL RULE:
  • Checkboxes appear as filled (■ ● ✓ ☑ or similar) or unfilled (□ ○ ☐).
  • For any field that lists checkbox options (e.g. Cause of Loss, Property Type),
    extract ONLY the label(s) next to FILLED/CHECKED boxes.
  • Do NOT list unchecked options. If no box is filled, return an empty string.
  • Example: "■ Fire □ Explosion □ Wind" → value = "Fire"
  • Example: "■ Fire ■ Explosion □ Wind" → value = "Fire, Explosion"
""").strip()

    # ADD THIS:
    empty_rule = (
        "CRITICAL: OMIT any field where the value is empty, null, 'N/A', "
        "'Not found', or cannot be found in the document text. "
        "Only return fields that have a real extracted value.\n\n"
        "PROVIDER NAME RULE: For 'Provider Name', prefer the name of the "
        "medical institution, hospital, clinic, or health system over an "
        "individual physician name where both are present. If only a physician "
        "name is present, use that."
        "'Not found', or cannot be found VERBATIM in the document text. "
        "Only return fields that have a real extracted value.\n\n"

        "DATE FIELDS — STRICT RULE:\n"
        "  • NEVER infer, calculate, or assume any date.\n"
        "  • 'Date of Loss' must appear as an explicit date in the document "
        "(e.g. '2023-10-01', 'October 1, 2023', 'yesterday' is NOT acceptable).\n"
        "  • 'Date Filed' / 'Date of Report' must appear as an explicit date "
        "in the document. Do NOT use today's date, call date, or any implied date.\n"
        "  • 'Time of Loss' must appear as an explicit time in the document "
        "(e.g. '17:30', '5:30 PM'). Do NOT infer from context.\n"
        "  • If the document says 'yesterday' or 'last night' without giving an "
        "explicit calendar date, OMIT the field entirely.\n"
        "  • If no explicit date or time is stated in the document text, "
        "OMIT the field — do not guess, do not calculate, do not infer.\n\n"

        "PROVIDER NAME RULE: For 'Provider Name', prefer the name of the "
        "medical institution, hospital, clinic, or health system over an "
        "individual physician name where both are present. If only a physician "
        "name is present, use that.\n\n"

        "CRITICAL: OMIT any field where the value is empty, null, 'N/A', "
        "'Not found', or cannot be found in the document text. "
        "Only return fields that have a real extracted value.\n\n"

    )

    # Updated schema — entities only, no signals section
    entities_only_schema = """
Return ONLY valid JSON — no markdown, no preamble.

{
  "entities": {
    "<SEMANTIC_LABEL>": {
      "azure_di_key": "<exact Azure DI field name or null>",
      "value":        "<exact value from document>",
      "source_text":  "<short verbatim snippet, optional>",
      "confidence":   <0.0–1.0>
    }
  }
}
"""

    return textwrap.dedent(f"""
You are a {role}.
{subtype_note}

{_GROUNDING_RULES}

Extract ONLY these entity fields (skip any not present in the document):
{entity_fields}

{checkbox_rule}

{empty_rule}

DO NOT extract signals — signals are handled separately.

{entities_only_schema}
""").strip()


def _signals_system(doc_type: str) -> str:
    """
    Dedicated system prompt for the signals-only LLM call (Call B).
    Uses build_signal_detection_prompt() from doc_config for
    YAML-driven, keyword-specific signal detection guidance.
    All signal extraction uses gpt-4o-mini (use_enhanced=False).
    """
    role          = get_role(doc_type)
    signal_prompt = build_signal_detection_prompt(doc_type)

    return textwrap.dedent(f"""
You are a {role} specialising in risk signal detection.

{signal_prompt}

{_GROUNDING_RULES}

Return ONLY valid JSON — no markdown, no preamble, no explanation.

{{
  "signals": [
    {{
      "type":            "<signal_type exactly as listed above>",
      "severity_level":  "<Highly Severe|High|Moderate|Low>",
      "description":     "<plain-English explanation of why this signal fired>",
      "supporting_text": "<VERBATIM quote from document, max 120 chars>",
      "trigger_matched": "<exact keyword or phrase from the Triggers list>",
      "confidence":      <0.0-1.0>
    }}
  ]
}}

If no signals are detected, return: {{"signals": []}}
""").strip()


def _summary_system(doc_type: str) -> str:
    """Build the summary+judge system prompt from config."""
    role     = get_role(doc_type)
    ts_fields = build_type_specific_field_list(doc_type)
    return textwrap.dedent(f"""
You are a {role}.

For type_specific, extract ONLY these assessment fields (skip any not present):
{ts_fields}

{_SUMMARY_SCHEMA}
""").strip()



# def _validation_system(doc_type: str, subtype: str | None = None) -> str:
#     role          = get_role(doc_type)
#     entity_fields = build_entity_field_list(doc_type, subtype)
#     signal_types  = get_signal_types(doc_type)
#     return textwrap.dedent(f"""
# You are a senior {role} performing rigorous quality validation of AI-extracted insurance document data.

# Your task is to critically evaluate:
# 1. EXTRACTION ACCURACY — were the right fields extracted with correct values?
# 2. SIGNAL CREDIBILITY — are the detected risk signals supported by the document?
# 3. COVERAGE ANALYSIS — are there gaps, omissions, or coverage concerns missed?
# 4. OVERALL VALIDATION — holistic assessment with recommended actions.

# Expected fields for a {doc_type}{f' ({subtype})' if subtype else ''} document:
# {entity_fields}

# Expected signal types: {signal_types}

# ═══════════════════════════════════════════════════════
# CRITICAL RULES — MUST FOLLOW BEFORE EVALUATING:
# ═══════════════════════════════════════════════════════

# EXTRACTION ACCURACY:
#   • A field is CORRECT if its value appears verbatim or as a clear
#     abbreviation/equivalent in the DOCUMENT TEXT. Do NOT penalise for
#     formatting differences (e.g. "$3,250,000.00" vs "THREE MILLION..."),
#     abbreviations ("N.D. Ill." = "Northern District of Illinois"),
#     or partial matches where the extracted value is a valid subset of a
#     longer document string.
#   • Only mark a field INCORRECT if the document text EXPLICITLY shows a
#     DIFFERENT value for that field. Never invent an "expected" value from
#     your own knowledge — only use what is in the provided document text.
#   • "incorrect_fields" must ONLY contain entries where you can directly
#     quote the conflicting evidence from the document text. If you cannot
#     quote contradicting text, do NOT include it.
#   • If a field is absent from the document entirely, list it in
#     missed_fields — not incorrect_fields.

# SIGNAL CREDIBILITY:
#   • Only flag a signal as a false positive if the document text clearly
#     contradicts it. Severity signals (death, fatality, injury) are credible
#     if mentioned anywhere in the document.

# COVERAGE ANALYSIS:
#   • Only list a field in "gaps" if it is expected for this doc type AND
#     genuinely absent from both the extraction AND the document text.

# SCORING:
#   • Start at 100. Deduct ONLY for confirmed errors backed by document evidence.
#   • Never deduct for formatting differences or absent-but-plausible fields.

# {_VALIDATION_SCHEMA}
# """).strip()


def _validation_system(doc_type: str, subtype: str | None = None) -> str:
    role          = get_role(doc_type)
    entity_fields = build_entity_field_list(doc_type, subtype)
    signal_types  = get_signal_types(doc_type)
    return textwrap.dedent(f"""
You are a senior {role} performing rigorous quality validation of AI-extracted insurance document data.

Your task is to critically evaluate:
1. EXTRACTION ACCURACY — were the right fields extracted with correct values?
2. SIGNAL CREDIBILITY — are the detected risk signals supported by the document?
3. COVERAGE ANALYSIS — are there gaps, omissions, or coverage concerns missed?
4. OVERALL VALIDATION — holistic assessment with recommended actions.

Expected fields for a {doc_type}{f' ({subtype})' if subtype else ''} document:
{entity_fields}

Expected signal types: {signal_types}

═══════════════════════════════════════════════════════
CRITICAL RULES — MUST FOLLOW BEFORE EVALUATING:
═══════════════════════════════════════════════════════

EXTRACTION ACCURACY:
  • A field is CORRECT if its value appears verbatim or as a clear
    abbreviation/equivalent in the DOCUMENT TEXT. Do NOT penalise for
    formatting differences (e.g. "$3,250,000.00" vs "THREE MILLION..."),
    abbreviations ("N.D. Ill." = "Northern District of Illinois"),
    or partial matches where the extracted value is a valid subset of a
    longer document string.
  • Only mark a field INCORRECT if the document text EXPLICITLY shows a
    DIFFERENT value for that field. Never invent an "expected" value from
    your own knowledge — only use what is in the provided document text.
  • "incorrect_fields" must ONLY contain entries where you can directly
    quote the conflicting evidence from the document text. If you cannot
    quote contradicting text, do NOT include it.
  • If a field is absent from the document entirely, list it in
    missed_fields — not incorrect_fields.

SIGNAL CREDIBILITY:
  • Only flag a signal as a false positive if the document text clearly
    contradicts it. Severity signals (death, fatality, injury) are credible
    if mentioned anywhere in the document.
  • FIELD MAPPING RULE — MOST IMPORTANT: The extracted system may map a
    document column or label to a semantic field name that differs from
    the raw document label. For example, "Your Share" in the document
    may be correctly mapped to "Amount Denied", or "Ins. Paid" may be
    mapped to "Amount Paid". DO NOT penalise for this — the mapping
    is intentional and correct if the value matches what is in that
    column/cell in the document.
  • Only mark a field INCORRECT if the document EXPLICITLY labels the
    same field with a DIFFERENT value. If multiple dollar amounts appear
    in the document, do NOT assume a different amount is "more correct"
    unless the document explicitly labels it with the exact same field name.
  • "incorrect_fields" must ONLY contain entries where you can directly
    quote contradicting text from the document using the SAME field label.
    If you cannot find the exact label in the document text contradicting
    the extraction, do NOT include it in incorrect_fields.
  • If a field is absent from the document entirely, list it in
    missed_fields — not incorrect_fields.
  • NEVER invent an "expected" value from your own interpretation of
    what a field "should" contain — only use what is explicitly labelled
    in the document text with that exact field name.
  • PHONE NUMBER RULE: Phone numbers are often formatted differently in
    documents vs extractions (e.g. "(404) 555-0199" vs "404-555-0199"
    vs "4045550199"). These are ALL the same value. NEVER mark a phone
    number as incorrect due to formatting differences. Only mark incorrect
    if the digits themselves are different.
  • EMPTY EXPECTED VALUE RULE: If you cannot determine what the correct
    value SHOULD be from the document text, you MUST leave incorrect_fields
    empty for that field. Never produce an incorrect_fields entry where
    "expected" is blank, null, or uncertain. An entry with no expected
    value is not evidence of an error — omit it entirely.

SIGNAL CREDIBILITY:
  • Only flag a signal as a false positive if the document text clearly
    contradicts it.


COVERAGE ANALYSIS:
  • Only list a field in "gaps" if it is expected for this doc type AND
    genuinely absent from both the extraction AND the document text.

SCORING:
  • Start at 100. Deduct ONLY for confirmed errors backed by document evidence.

  • Never deduct for formatting differences or absent-but-plausible fields.

  • Never deduct for field mapping differences or formatting variations.


{_VALIDATION_SCHEMA}
""").strip()


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — TWO-CALL ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

def analyse_document(
    full_text: str,
    doc_type: str,
    azure_di_fields: dict[str, dict] | None = None,
) -> dict:
    """
    Three-call analysis — all on gpt-4o-mini:
      Call A — entities only        (max_tokens=2000)
      Call B — signals only         (max_tokens=1500) ← dedicated
      Call C — summary + judge      (max_tokens=1200)

    Sub-type is auto-detected from the document text via doc_config.
    """
    # Detect sub-type so we can add sub-type-specific fields to the prompt
    subtype = detect_subtype(doc_type, full_text)
    _debug_store("detected_subtype", subtype or "none")

    # Compact name→value map for LLM (no bbox data)
    adi_kv: dict[str, str] = {}
    if azure_di_fields:
        for fname, fdata in azure_di_fields.items():
            v = fdata.get("value", "")
            if v:
                adi_kv[fname] = str(v)[:200]

    text_a = full_text[:3000] + ("\n\n[... document truncated ...]" if len(full_text) > 3000 else "")
    text_b = text_a

    adi_listing = ""
    if adi_kv:
        lines = [f'  "{k}": "{v[:50]}"' for k, v in list(adi_kv.items())[:30]]
        adi_listing = (
            "\n\n--- AZURE DOCUMENT INTELLIGENCE FIELDS (use exact key names as azure_di_key) ---\n{\n"
            + ",\n".join(lines)
            + "\n}"
        )

    # # ── Call A: entities + signals ────────────────────────────────────────────
    # user_a = (
    #     f"Document type: {doc_type}"
    #     + (f" / Sub-type: {subtype}" if subtype else "")
    #     + f"\nExtract entities and detect signals."
    #     + adi_listing
    #     + f"\n\n--- DOCUMENT TEXT ---\n{text_a}"
    # )
    # result_a = _llm_call(
    #     system_prompt=_entities_system(doc_type, subtype),
    #     user_prompt=user_a,
    #     max_tokens=2500,
    #     label="entities_signals",
    #     use_enhanced=False,
    # )

    # # Retry with reduced input if Call A failed
    # if result_a is None:
    #     _debug_store("entities_signals_retry_triggered", "Call A returned None")
    #     result_a = _llm_call(
    #         system_prompt=_entities_system(doc_type, subtype),
    #         user_prompt=user_a[:int(len(user_a) * 0.6)],
    #         max_tokens=2500,
    #         label="entities_signals_retry",
    #         use_enhanced=False,
    #     )

    # # ── Call B: summary + type_specific + judge ───────────────────────────────
    # user_b = (
    #     f"Document type: {doc_type}"
    #     + (f" / Sub-type: {subtype}" if subtype else "")
    #     + f"\nGenerate a summary and assessment."
    #     + adi_listing
    #     + f"\n\n--- DOCUMENT TEXT ---\n{text_b}"
    # )
    # result_b = _llm_call(
    #     system_prompt=_summary_system(doc_type),
    #     user_prompt=user_b,
    #     max_tokens=1200,
    #     label="summary_judge",
    #     use_enhanced=False,
    # )

    # # ── Merge ──────────────────────────────────────────────────────────────────
    # entities      = {}
    # signals       = []
    # summary       = ""
    # type_specific = {}
    # judge         = {}

    # if result_a:
    #     entities = result_a.get("entities") or {}
    #     signals  = result_a.get("signals")  or []
    #     # ── LAYER 4: Verify extracted values exist in source text ─────────
    #     entities = _verify_entities_against_text(entities, full_text)
    #     for _, ed in entities.items():
    #         if isinstance(ed, dict):
    #             ed.setdefault("azure_di_key", None)

    # if result_b:
    #     summary       = result_b.get("summary")       or ""
    #     type_specific = result_b.get("type_specific") or {}
    #     judge         = result_b.get("judge")         or {}


    # ── CALL A — Entities only (gpt-4o-mini) ─────────────────────────────────
    user_a = (
        f"Document type: {doc_type}"
        + (f" / Sub-type: {subtype}" if subtype else "")
        + "\nExtract entities only — do NOT extract signals in this call."
        + adi_listing
        + f"\n\n--- DOCUMENT TEXT ---\n{text_a}"
    )
    result_a = _llm_call(
        system_prompt=_entities_system(doc_type, subtype),
        user_prompt=user_a,
        max_tokens=2000,
        label="entities",
        use_enhanced=False,   # gpt-4o-mini
    )

    # Retry with reduced input if Call A failed
    if result_a is None:
        _debug_store("entities_retry_triggered", "Call A returned None")
        result_a = _llm_call(
            system_prompt=_entities_system(doc_type, subtype),
            user_prompt=user_a[:int(len(user_a) * 0.6)],
            max_tokens=2000,
            label="entities_retry",
            use_enhanced=False,
        )

    # ── CALL B — Signals only (gpt-4o-mini, YAML-driven) ─────────────────────
    user_b_signals = (
        f"Document type: {doc_type}"
        + (f" / Sub-type: {subtype}" if subtype else "")
        + "\nDetect risk signals ONLY. Do not extract entity fields."
        + f"\n\n--- DOCUMENT TEXT ---\n{full_text[:6000]}"
    )
    result_b = _llm_call(
        system_prompt=_signals_system(doc_type),
        user_prompt=user_b_signals,
        max_tokens=2500,   # ← enough for 6-7 detailed signals
        label="signals",
        use_enhanced=False,
    )

    # ── CALL C — Summary + type_specific + judge (gpt-4o-mini) ───────────────
    user_c = (
        f"Document type: {doc_type}"
        + (f" / Sub-type: {subtype}" if subtype else "")
        + "\nGenerate a summary and assessment."
        + adi_listing
        + f"\n\n--- DOCUMENT TEXT ---\n{text_a}"
    )
    result_c = _llm_call(
        system_prompt=_summary_system(doc_type),
        user_prompt=user_c,
        max_tokens=1200,
        label="summary_judge",
        use_enhanced=False,   # gpt-4o-mini
    )

    # ── Merge all three results ───────────────────────────────────────────────
    entities      = {}
    signals       = []
    summary       = ""
    type_specific = {}
    judge         = {}

    if result_a:
        entities = result_a.get("entities") or {}
        entities = _verify_entities_against_text(entities, full_text)
        for _, ed in entities.items():
            if isinstance(ed, dict):
                ed.setdefault("azure_di_key", None)

    if result_b:
        signals = result_b.get("signals") or []
        signals = _validate_signals_against_text(signals, full_text)

    if result_c:
        summary       = result_c.get("summary")       or ""
        type_specific = result_c.get("type_specific") or {}
        judge         = result_c.get("judge")         or {}


    if not entities and not signals and not summary:
        return _empty_analysis(doc_type)

    judge.setdefault("classification_reasoning", "")
    judge.setdefault("signal_validation", "")
    judge.setdefault("data_quality", "")
    judge.setdefault("recommendations", "")

    return {
        "summary":       summary,
        "entities":      entities,
        "signals":       signals,
        "type_specific": type_specific,
        "judge":         judge,
        "detected_subtype": subtype,  # stored for downstream use
    }


def _empty_analysis(doc_type: str) -> dict:
    return {
        "summary": "Analysis unavailable — LLM could not be reached.",
        "entities": {},
        "signals": [],
        "type_specific": {},
        "judge": {
            "classification_reasoning": f"Classified as {doc_type}.",
            "signal_validation": "No signals detected.",
            "data_quality": "LLM unavailable — check OPENAI env vars and token quotas.",
            "recommendations": "Manual review required.",
        },
        "detected_subtype": None,
    }

# ─────────────────────────────────────────────────────────────────────────────
# LAYER 4 — POST-EXTRACTION HALLUCINATION VERIFIER
# ─────────────────────────────────────────────────────────────────────────────
def _validate_signals_against_text(signals: list, full_text: str) -> list:
    if not full_text or not signals:
        return signals

    text_lower = full_text.lower()
    validated  = []

    for sig in signals:
        supporting = (sig.get("supporting_text") or "").strip()
        trigger    = (sig.get("trigger_matched")  or "").strip()

        grounded = False

        # Check 1: supporting_text verbatim in document
        if supporting and len(supporting) >= 6:  # was 8
            if supporting.lower() in text_lower:
                grounded = True
            else:
                sup_toks = {
                    t for t in supporting.lower().split()
                    if len(t) >= 3  # was 4
                }
                doc_toks = set(text_lower.split())
                if sup_toks:
                    overlap = len(sup_toks & doc_toks) / len(sup_toks)
                    if overlap >= 0.40:  # was 0.60
                        grounded = True

        # Check 2: trigger keyword in document
        # ALSO check with punctuation stripped
        trigger_clean = re.sub(r"[—\-–]", " ", trigger).strip()
        trigger_found = bool(
            trigger and (
                trigger.lower() in text_lower
                or trigger_clean.lower() in text_lower
            )
        )
        if trigger_found and not grounded:
            grounded = True

        # NEW Check 3: any significant word from trigger in document
        if not grounded and trigger:
            trigger_words = {
                w for w in trigger.lower().split()
                if len(w) >= 5
            }
            if trigger_words and trigger_words & set(text_lower.split()):
                grounded = True

        if not grounded:
            sig = dict(sig)
            sig["confidence"]  = min(float(sig.get("confidence", 0.5)), 0.40)
            sig["_unverified"] = True

        if not trigger_found and trigger:
            sig = dict(sig)
            sig["_trigger_not_found"] = True

        validated.append(sig)

    return validated



def _verify_entities_against_text(entities: dict, full_text: str) -> dict:
    """
    Post-extraction guardrail: remove any entity whose value cannot be 
    found (even approximately) in the source document text.
    Prevents hallucinated values from reaching the UI.
    """
    if not full_text:
        return entities

    text_lower = full_text.lower()
    verified   = {}
    removed    = []

    # Fields exempt from verbatim check (assessed/computed values)
    _EXEMPT_FIELDS = {
        "severity", "litigation risk", "fraud indicator",
        "recommended next step", "coverage concern",
        "estimated loss amount", "medical complexity",
        "risk appetite score", "bind / decline recommendation",
        "cat exposure flag", "adequacy ratio", "estimated annual premium",
        "loss ratio trend", "referral required", "pricing adequacy",
    }

    for fname, fdata in entities.items():
        if not isinstance(fdata, dict):
            verified[fname] = fdata
            continue

        # Exempt assessed/type-specific fields
        if any(ex in fname.lower() for ex in _EXEMPT_FIELDS):
            verified[fname] = fdata
            continue

        value = (fdata.get("value") or "").strip()

        # LOOSENED: allow short values through (was < 3, now < 2)
        if not value or len(value) < 2:
            verified[fname] = fdata
            continue

        

        # Check 1: exact substring match
        if value.lower() in text_lower:
            verified[fname] = fdata
            continue

        # Check 2: significant token overlap (for multi-word values)
        val_tokens = set(re.sub(r"[^\w\s]", " ", value.lower()).split())
        doc_tokens = set(re.sub(r"[^\w\s]", " ", text_lower).split())
        sig_tokens = {t for t in val_tokens if len(t) >= 3}

        if sig_tokens:
            overlap = len(sig_tokens & doc_tokens) / len(sig_tokens)
            if overlap >= 0.50:
                verified[fname] = fdata
                continue

        # Check 3: digit sequence match (for IDs, phone numbers, amounts)
        val_digits = re.sub(r"\D", "", value)
        if len(val_digits) >= 4 and val_digits in re.sub(r"\D", "", full_text):
            verified[fname] = fdata
            continue

        # Failed all checks — mark as low confidence but keep with warning
        # (hard removal risks losing valid extractions on OCR-noisy docs)
        fdata_copy = dict(fdata)
        fdata_copy["confidence"]    = min(float(fdata.get("confidence", 0.5)), 0.45)
        fdata_copy["_unverified"]   = True
        verified[fname] = fdata_copy
        removed.append(fname)

    if removed:
        _debug_store("unverified_entities", json.dumps(removed))

    return verified
# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL SEVERITY CLASSIFICATION (config-driven, used by pdf_analysis.py)
# ─────────────────────────────────────────────────────────────────────────────

def classify_severity_from_config(sig: dict, doc_type: str) -> str:
    """
    Classify signal severity using YAML severity_triggers.
    Falls back to type-based heuristics if config unavailable.
    Called from pdf_analysis.py _classify_severity().
    """
    llm_level = (sig.get("severity_level") or "").strip().title()
    _VALID = {"Highly Severe", "High", "Moderate", "Low"}
    if llm_level in _VALID:
        return llm_level

    triggers = get_severity_keywords(doc_type)
    if triggers:
        desc = (
            sig.get("description", "") + " " + sig.get("supporting_text", "")
        ).lower()
        for level in ("highly_severe", "high", "moderate", "low"):
            keywords = triggers.get(level, [])
            if any(kw.lower() in desc for kw in keywords):
                return level.replace("_", " ").title()

    # Last-resort: type-based fallback
    stype = sig.get("type", "")
    if stype in ("severity", "legal_escalation"):
        return "High"
    if stype in ("coverage_issue", "medical_complexity"):
        return "Moderate"
    if stype == "fraud_indicator":
        return "Moderate"
    return "Low"


# ─────────────────────────────────────────────────────────────────────────────
# VALIDATION PIPELINE  (enhanced model — called on demand from Validation tab)
# ─────────────────────────────────────────────────────────────────────────────

def run_validation(
    full_text: str,
    doc_type: str,
    extracted_entities: dict,
    detected_signals: list,
    azure_di_fields: dict | None = None,
) -> dict:
    subtype = detect_subtype(doc_type, full_text)



    # ── TEMP DEBUG ────────────────────────────────────────────────
    print(f"[VALIDATION] Starting with model: {_deployment_enhanced()}")
    print(f"[VALIDATION] Client: {_get_openai_client_enhanced()}")
    # ─────────────────────────────────────────────────────────────


    entity_summary = json.dumps(
        {k: v.get("value", "") for k, v in extracted_entities.items() if isinstance(v, dict)},
        indent=2,
    )[:2000]

    signal_summary = json.dumps(
        [{"type": s.get("type"), "severity": s.get("severity_level"),
          "description": s.get("description", "")[:100]} for s in detected_signals],
        indent=2,
    )[:800]

    adi_summary = ""
    if azure_di_fields:
        lines = [f'  "{k}": "{str(v.get("value",""))[:60]}"'
                 for k, v in list(azure_di_fields.items())[:25]]
        adi_summary = "\n\nAZURE DI FIELDS:\n{\n" + ",\n".join(lines) + "\n}"

    user_prompt = (
        f"Document type: {doc_type}"
        + (f" / Sub-type: {subtype}" if subtype else "")
        + f"\n\nEXTRACTED ENTITIES:\n{entity_summary}\n\n"
        + f"DETECTED SIGNALS:\n{signal_summary}"
        + adi_summary
        + f"\n\n--- DOCUMENT TEXT (use this as ground truth for all comparisons) ---\n{full_text[:4000]}"
    )

    result = _llm_call(
        system_prompt=_validation_system(doc_type, subtype),
        user_prompt=user_prompt,
        max_tokens=3000,
        label="validation",
        use_enhanced=True,
    )

    if not result:
        return _empty_validation()

    result.setdefault("extraction_accuracy", _empty_validation_section("Review"))
    result.setdefault("signal_credibility",  _empty_validation_section("Review"))
    result.setdefault("coverage_analysis",   _empty_validation_section("Review"))
    result.setdefault("overall_validation",  _empty_validation_section("Review"))
    return result


def _empty_validation_section(verdict: str = "Review") -> dict:
    return {"score": 0, "verdict": verdict, "findings": "Validation unavailable."}


def _empty_validation() -> dict:
    return {
        "extraction_accuracy": _empty_validation_section(),
        "signal_credibility":  _empty_validation_section(),
        "coverage_analysis":   _empty_validation_section(),
        "overall_validation": {
            "score": 0,
            "verdict": "Failed",
            "confidence": 0.0,
            "summary": "Validation could not be completed — enhanced AI unavailable.",
            "recommended_actions": ["Check OPENAI environment variables and retry."],
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# MASTER PIPELINE
# ─────────────────────────────────────────────────────────────────────────────



def _file_hash(full_text: str, doc_type_hint: str = "") -> str:
    """Stable hash of document content — same file always produces same hash."""
    payload = f"{full_text.strip()}|{doc_type_hint}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def run_pdf_intelligence(parsed: dict, sheet_cache: dict | None = None) -> dict:
    full_text  = extract_full_text_from_parsed(parsed)
    page_count = len(parsed.get("pages", []))
    source     = parsed.get("source", "pdf")

    # ── LAYER 1: Content-hash cache ───────────────────────────────────────────
    try:
        import streamlit as st
        _fhash     = _file_hash(full_text)
        _cache_key = f"_intel_result_{_fhash}"

        if _cache_key in st.session_state:
           cached = st.session_state[_cache_key]
           if cached.get("_content_hash") == _fhash:
              saved_llm = cached.get("_saved_cost_log", [])
              saved_adi = cached.get("_saved_adi_cost_log", [])
              cur_llm   = st.session_state.get("_llm_cost_log", [])
              cur_adi   = st.session_state.get("_adi_cost_log", [])
              if len(saved_llm) > len(cur_llm):
                  st.session_state["_llm_cost_log"] = list(saved_llm)
              if len(saved_adi) > len(cur_adi):
                  st.session_state["_adi_cost_log"] = list(saved_adi)
              return cached
    except Exception:
        _fhash     = ""
        _cache_key = ""

    # ── Build Azure DI index ──────────────────────────────────────────────────
    azure_di_index = _build_azure_di_index_from_parsed(parsed)

    if source == "txt" and not azure_di_index:
        for page in parsed.get("pages", []):
            for field in page.get("fields", []):
                fname = (field.get("field_name") or "").strip()
                if not fname:
                    continue
                if fname not in azure_di_index:
                    azure_di_index[fname] = {
                        "value":            field.get("value", ""),
                        "confidence":       float(field.get("confidence", 0.80)),
                        "bounding_polygon": None,
                        "source_page":      1,
                        "page_width":       None,
                        "page_height":      None,
                    }

    # ── Classify + Analyse ────────────────────────────────────────────────────
    classification = classify_document(full_text)
    doc_type       = classification.get("classification", "Legal")
    analysis       = analyse_document(full_text, doc_type, azure_di_fields=azure_di_index)

    result = {
        "full_text":      full_text,
        "classification": classification,
        "analysis":       analysis,
        "page_count":     page_count,
        "doc_type":       doc_type,
        "azure_di_index": azure_di_index,
        "source":         source,
        "_content_hash":  _fhash,        # ← used by Layer 1 cache check
    }

    # For TXT: fallback entities if LLM returned empty
    if source == "txt" and not result["analysis"].get("entities"):
        fallback_entities: dict = {}
        for page in parsed.get("pages", []):
            for field in page.get("fields", []):
                fname = (field.get("field_name") or "").strip()
                fval  = (field.get("value") or "").strip()
                if fname and fval and fname not in fallback_entities:
                    fallback_entities[fname] = {
                        "value":      fval,
                        "confidence": float(field.get("confidence", 0.80)),
                        "source_text": field.get("source_text", ""),
                        "azure_di_key": None,
                    }
        if fallback_entities:
            result["analysis"]["entities"] = fallback_entities

    # ── Store in session cache ─────────────────────────────────────────────────

    try:
        if _cache_key:
          result["_saved_cost_log"]     = list(st.session_state.get("_llm_cost_log", []))
          result["_saved_adi_cost_log"] = list(st.session_state.get("_adi_cost_log", []))
          st.session_state[_cache_key]  = result
    except Exception:
        pass

    return result

    