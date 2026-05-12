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


try:
    from ui.pdf_analysis import _get_keyword_signal_confidence
except ImportError:
    # fallback if import path differs
    def _get_keyword_signal_confidence(keyword: str, severity: str, context: str = "") -> float:
        return 0.75
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
        "TRANSCRIPT RULE: This document may be a conversation transcript. "
        "Extract ONLY structured data fields (claim numbers, policy numbers, "
        "dates, names, addresses, phone numbers, amounts). "
        "Do NOT extract conversational phrases, questions, greetings, "
        "or any sentence that is part of dialogue as a field value. "
        "If a field value reads like a sentence someone spoke, OMIT it.\n\n"
        "PROVIDER NAME RULE: For 'Provider Name', prefer the name of the "
        "medical institution, hospital, clinic, or health system over an "
        "individual physician name where both are present. If only a physician "
        "name is present, use that."
        "'Not found', or cannot be found VERBATIM in the document text. "
        "Only return fields that have a real extracted value.\n\n"
        "YEARS IN BUSINESS RULE: 'Years in Business' must be a duration "
        "(e.g. '22 years', '5+ years'). Never extract a founding year like '2003' "
        "for this field — that belongs in 'Year Founded' or 'Year Established'.\n\n"
 
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
    PATCHED: Forces exhaustive scan, explicit domain coverage, higher recall.
    """
    from modules.doc_config import get_role, build_signal_detection_prompt  # keep your import
    role          = get_role(doc_type)
    signal_prompt = build_signal_detection_prompt(doc_type)
 
    return f"""
You are a {role} specialising in EXHAUSTIVE risk signal detection.
 
{signal_prompt}
 
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MANDATORY SIGNAL DOMAINS — CHECK EVERY ONE BEFORE RESPONDING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
For EVERY document you MUST actively scan for signals in ALL of these domains.
Do not stop after finding 1-2 signals. Scan each domain independently:
 
  1. INJURY / CLAIM SEVERITY
     Look for: fatality, death, hospitalization, surgery, fracture, permanent
     disability, catastrophic, ICU, amputation, paralysis, total loss,
     severe pain, multiple injuries, emergency room, trauma
 
  2. LITIGATION RISK
     Look for: attorney, lawyer, counsel, lawsuit, litigation, demand letter,
     complaint filed, court, legal action, pre-litigation, settlement demand,
     bad faith, class action, punitive damages
 
  3. RECOVERY / SUBROGATION OPPORTUNITY
     Look for: third party liable, subrogation, vendor fault, contractor,
     negligence of third party, shared liability, upstream, MSP, recovery
     potential, outsourced to
 
  4. SETTLEMENT POSTURE
     Look for: unwilling to settle, aggressive demand, refusal to settle,
     cooperative, high demand, low-ball offer, mediation, arbitration,
     demand value, settlement authority
 
  5. MEDICAL COMPLEXITY
     Look for: surgery required, specialist, physical therapy, chronic,
     multiple procedures, hospitalization, ongoing treatment, chiropractic,
     PTSD, pre-existing condition, treatment duration
 
 
 
  7. COVERAGE ADEQUACY / ISSUES
     Look for: reservation of rights, coverage denied, exclusion, gap,
     underinsured, coinsurance penalty, policy limit, inadequate coverage,
     coverage dispute
 

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ANTI-HALLUCINATION RULES (unchanged):
  1. supporting_text must be VERBATIM from the document (max 400 chars)
  2. trigger_matched must be the exact keyword that fired
  3. Only fire when explicit evidence is present in the text
  4. confidence: 0.90+ verbatim match, 0.70-0.89 equivalent phrase
  5. MULTIPLE SIGNALS ARE EXPECTED — a typical document should produce 3-8
  6. Do NOT stop scanning after finding the first signal per domain

SEVERITY_CLASSIFICATION_RULES:
  Highly Severe — any of: death, fatality, wrongful death, catastrophic loss,
                  permanent disability, amputation, paralysis, punitive damages,
                  bad faith, class action, criminal charges, fraud/misrepresentation
  High          — any of: surgery, hospitalization, serious injury, attorney involved,
                  active lawsuit/litigation, large financial exposure, reservation of rights,
                  subrogation opportunity, coverage denial, staged incident suspected
  Moderate      — any of: physical therapy, specialist referral, settlement discussion,
                  mediation/arbitration, delayed reporting, minor inconsistency,
                  ongoing treatment, reserve adequacy concern
  Low           — any of: minor injury, minor damage, no attorney, no litigation,
                  routine treatment, low financial exposure
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 
Return ONLY valid JSON — no markdown, no preamble, no explanation.
 
{{
  "signals": [
    {{
      "type":            "<signal_type exactly as listed in SIGNAL DETECTION RULES>",
      "severity_level":  "<Highly Severe|High|Moderate|Low>",
      "description":     "<plain-English explanation of why this signal fired>",
      "supporting_text": "<VERBATIM quote from document, max 800 chars, include full sentence and surrounding context>",
      "trigger_matched": "<exact keyword or phrase from the Triggers list>",
      "confidence":      <0.0-1.0>
    }}
  ]
}}
 
If no signals are detected after exhaustive scan, return: {{"signals": []}}
""".strip()


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



def _keyword_extract_signals(full_text: str, doc_type: str) -> list[dict]:
    """
    PATCHED: NEW FUNCTION
    Pure keyword-based signal extraction using YAML config + extended keyword map.
    Serves as Layer 1 of the fallback chain — fast, zero LLM cost, high recall.
 
    Returns a list of signal dicts compatible with the signals schema.
    Each signal is tagged with _source="keyword" for UI display.
    """
    import re as _re
 
    text_lower = full_text.lower()
 
    # ── Extended keyword map: (signal_type, severity, keyword, description) ──
    # Organized by the 7 domains from the Value Momentum slide
    _EXTENDED_KEYWORDS: list[tuple[str, str, str, str]] = [
 
        # ── 1. INJURY / CLAIM SEVERITY ────────────────────────────────────
        ("severity", "Highly Severe", "fatality",           "Fatality referenced in document"),
        ("severity", "Highly Severe", "fatal",              "Fatal injury or event described"),
        ("severity", "Highly Severe", "death",              "Death of a party referenced"),
        ("severity", "Highly Severe", "deceased",           "Deceased party mentioned"),
        ("severity", "Highly Severe", "wrongful death",     "Wrongful death claim identified"),
        ("severity", "Highly Severe", "catastrophic",       "Catastrophic injury or loss described"),
        ("severity", "Highly Severe", "permanent disability","Permanent disability indicated"),
        ("severity", "Highly Severe", "permanent impairment","Permanent impairment noted"),
        ("severity", "Highly Severe", "total loss",         "Total loss of property or vehicle"),
        ("severity", "Highly Severe", "paralysis",          "Paralysis condition referenced"),
        ("severity", "Highly Severe", "amputation",         "Amputation referenced"),
        ("severity", "Highly Severe", "traumatic brain",    "Traumatic brain injury identified"),
        ("severity", "Highly Severe", "spinal cord",        "Spinal cord injury referenced"),
        ("severity", "High",          "surgery",            "Surgical procedure documented"),
        ("severity", "High",          "surgical",           "Surgical intervention referenced"),
        ("severity", "High",          "hospitalized",       "Hospitalization reported"),
        ("severity", "High",          "hospitalization",    "Hospitalization referenced"),
        ("severity", "High",          "fracture",           "Fracture injury identified"),
        ("severity", "High",          "multiple fractures", "Multiple fractures reported"),
        ("severity", "High",          "icu",                "ICU admission or treatment noted"),
        ("severity", "High",          "intensive care",     "Intensive care referenced"),
        ("severity", "High",          "emergency room",     "Emergency room visit documented"),
        ("severity", "High",          "severe pain",        "Severe pain reported"),
        ("severity", "High",          "severe injury",      "Severe injury documented"),
        ("severity", "High",          "significant injury",  "Significant injury referenced"),
        ("severity", "High",          "nerve damage",       "Nerve damage identified"),
        ("severity", "High",          "herniated",          "Herniated disc or injury noted"),
        ("severity", "High",          "torn",               "Torn ligament or tissue noted"),
        ("severity", "Moderate",      "moderate injury",    "Moderate injury documented"),
        ("severity", "Moderate",      "physical therapy",   "Physical therapy prescribed"),
        ("severity", "Moderate",      "ongoing treatment",  "Ongoing medical treatment noted"),
        ("severity", "Moderate",      "restricted activity","Activity restriction documented"),
        ("severity", "Moderate",      "chiropractic",       "Chiropractic treatment referenced"),
        ("severity", "Moderate",      "emergency",          "Emergency event referenced"),
        ("severity", "Moderate",      "airbag deployed",    "Airbag deployment noted"),
        ("severity", "Moderate",      "contusion",          "Contusion injury documented"),
        ("severity", "Moderate",      "laceration",         "Laceration documented"),
        ("severity", "Moderate",      "sprain",             "Sprain injury documented"),
        ("severity", "Low",           "minor injury",       "Minor injury reported"),
        ("severity", "Low",           "first aid",          "First aid treatment only"),
        ("severity", "Low",           "no injuries",        "No injuries reported"),
 
        # ── 2. LITIGATION RISK ─────────────────────────────────────────────
        ("legal_escalation", "Highly Severe", "criminal charges",   "Criminal charges referenced"),
        ("legal_escalation", "Highly Severe", "class action",       "Class action litigation identified"),
        ("legal_escalation", "Highly Severe", "punitive damages",   "Punitive damages sought"),
        ("legal_escalation", "Highly Severe", "bad faith",          "Bad faith allegation present"),
        ("legal_escalation", "Highly Severe", "wrongful death suit", "Wrongful death suit filed"),
        ("legal_escalation", "High",          "attorney involved",  "Attorney involvement noted"),
        ("legal_escalation", "High",          "attorney retained",  "Attorney retained by claimant"),
        ("legal_escalation", "High",          "counsel retained",   "Legal counsel retained"),
        ("legal_escalation", "High",          "attorney",           "Attorney referenced in document"),
        ("legal_escalation", "High",          "lawsuit filed",      "Lawsuit has been filed"),
        ("legal_escalation", "High",          "lawsuit",            "Lawsuit referenced"),
        ("legal_escalation", "High",          "litigation initiated","Litigation initiated"),
        ("legal_escalation", "High",          "litigation",         "Litigation referenced"),
        ("legal_escalation", "High",          "demand letter",      "Formal demand letter issued"),
        ("legal_escalation", "High",          "legal representation","Legal representation noted"),
        ("legal_escalation", "High",          "complaint filed",    "Formal complaint filed"),
        ("legal_escalation", "High",          "plaintiff",          "Plaintiff party identified"),
        ("legal_escalation", "High",          "defendant",          "Defendant party identified"),
        ("legal_escalation", "High",          "negligence",         "Negligence allegation present"),
        ("legal_escalation", "High",          "damages sought",     "Damages are being sought"),
        ("legal_escalation", "High",          "liability",          "Liability language detected"),
        ("legal_escalation", "High",          "court",              "Court proceedings referenced"),
        ("legal_escalation", "Moderate",      "attorney involvement","Attorney involvement at early stage"),
        ("legal_escalation", "Moderate",      "settlement demand",  "Settlement demand received"),
        ("legal_escalation", "Moderate",      "mediation",          "Mediation process referenced"),
        ("legal_escalation", "Moderate",      "arbitration",        "Arbitration referenced"),
        ("legal_escalation", "Moderate",      "deposition",         "Deposition activity noted"),
        ("legal_escalation", "Moderate",      "pre-litigation",     "Pre-litigation indicators present"),
        ("legal_escalation", "Moderate",      "considering attorney","Claimant considering attorney"),
 
        # ── 3. RECOVERY / SUBROGATION OPPORTUNITY ─────────────────────────
        ("recovery_subrogation", "High", "third party liable",    "Third party liability identified"),
        ("recovery_subrogation", "High", "subrogation",           "Subrogation opportunity present"),
        ("recovery_subrogation", "High", "subrogation potential", "Potential subrogation identified"),
        ("recovery_subrogation", "High", "vendor responsible",    "Vendor responsibility identified"),
        ("recovery_subrogation", "High", "contractor fault",      "Contractor fault identified"),
        ("recovery_subrogation", "High", "subcontractor",         "Subcontractor involvement noted"),
        ("recovery_subrogation", "High", "third party fault",     "Third party at fault"),
        ("recovery_subrogation", "High", "shared negligence",     "Shared negligence identified"),
        ("recovery_subrogation", "High", "upstream liability",    "Upstream liability identified"),
        ("recovery_subrogation", "High", "outsourced to",         "Outsourced service involved"),
        ("recovery_subrogation", "High", "msp",                   "Medicare Secondary Payer involvement"),
        ("recovery_subrogation", "Moderate", "partial liability",  "Partial liability identified"),
        ("recovery_subrogation", "Moderate", "contributory",       "Contributory negligence noted"),
        ("recovery_subrogation", "Moderate", "possible recovery",  "Possible recovery identified"),
        ("recovery_subrogation", "High", "she hit me",       "Third party at fault — claimant states other party caused collision"),
        ("recovery_subrogation", "High", "he hit me",        "Third party at fault — claimant states other party caused collision"),
        ("recovery_subrogation", "High", "hit me",           "Third party impact reported — subrogation opportunity"),
        ("recovery_subrogation", "High", "at fault",         "Fault attributed to third party"),
        ("recovery_subrogation", "High", "rear ended",       "Rear-end collision — third party fault likely"),
        ("recovery_subrogation", "High", "rear impact",      "Rear impact — third party fault indicators present"),
        ("recovery_subrogation", "High", "other driver",     "Other driver involvement noted"),
        ("recovery_subrogation", "High", "fully stopped",    "Claimant was stationary — supports third party fault"),
        ("recovery_subrogation", "High", "i was stopped",    "Claimant was stationary at time of impact"),
        ("recovery_subrogation", "High", "her fault",        "Fault attributed to third party"),
        ("recovery_subrogation", "High", "his fault",        "Fault attributed to third party"),
 
        # ── 4. SETTLEMENT POSTURE ──────────────────────────────────────────
        ("risk_appetite", "High", "unwilling to settle",   "Claimant unwilling to settle"),
        ("risk_appetite", "High", "aggressive demand",     "Aggressive settlement demand"),
        ("risk_appetite", "High", "refusal to settle",     "Refusal to settle documented"),
        ("risk_appetite", "High", "high demand",           "High settlement demand noted"),
        ("risk_appetite", "High", "demand value",          "Demand value specified"),
        ("risk_appetite", "High", "non-renewing",          "Non-renewal due to loss activity"),
        ("risk_appetite", "High", "reason for marketing",  "Reason for market change noted"),
        ("risk_appetite", "Moderate", "cooperative",        "Cooperative settlement posture"),
        ("risk_appetite", "Moderate", "settlement discussion","Settlement discussions ongoing"),
 
        # ── 5. MEDICAL COMPLEXITY ──────────────────────────────────────────
        ("medical_complexity", "Highly Severe", "permanent disability", "Permanent disability identified"),
        ("medical_complexity", "Highly Severe", "permanent impairment", "Permanent impairment noted"),
        ("medical_complexity", "Highly Severe", "paralysis",            "Paralysis condition noted"),
        ("medical_complexity", "Highly Severe", "traumatic brain injury","Traumatic brain injury"),
        ("medical_complexity", "Highly Severe", "spinal cord injury",    "Spinal cord injury"),
        ("medical_complexity", "High",          "surgery",              "Surgery required"),
        ("medical_complexity", "High",          "surgical procedure",   "Surgical procedure documented"),
        ("medical_complexity", "High",          "hospitalization",      "Hospitalization required"),
        ("medical_complexity", "High",          "multiple procedures",  "Multiple medical procedures"),
        ("medical_complexity", "High",          "specialist referral",  "Specialist referral needed"),
        ("medical_complexity", "High",          "chronic condition",    "Chronic condition identified"),
        ("medical_complexity", "High",          "pre-existing condition","Pre-existing condition noted"),
        ("medical_complexity", "High",          "ptsd",                 "PTSD diagnosed or claimed"),
        ("medical_complexity", "High",          "treatment duration",   "Extended treatment duration"),
        ("medical_complexity", "Moderate",      "physical therapy",     "Physical therapy prescribed"),
        ("medical_complexity", "Moderate",      "chiropractic",         "Chiropractic treatment noted"),
        ("medical_complexity", "Moderate",      "ongoing treatment",    "Ongoing treatment required"),
        ("medical_complexity", "Moderate",      "follow-up required",   "Follow-up care required"),
        ("medical_complexity", "Moderate",      "medication prescribed", "Medication prescribed"),
 

 
        # ── 7. COVERAGE ADEQUACY / ISSUES ─────────────────────────────────
        ("coverage_adequacy", "High", "reservation of rights", "Reservation of rights issued"),
        ("coverage_adequacy", "High", "coverage denied",       "Coverage denial noted"),
        ("coverage_adequacy", "High", "coverage denial",       "Coverage denied"),
        ("coverage_adequacy", "High", "policy exclusion",      "Policy exclusion referenced"),
        ("coverage_adequacy", "High", "coverage dispute",      "Coverage dispute identified"),
        ("coverage_adequacy", "High", "underinsured",          "Underinsurance identified"),
        ("coverage_adequacy", "High", "coverage gap",          "Coverage gap identified"),
        ("coverage_adequacy", "High", "inadequate limits",     "Inadequate policy limits"),
        ("coverage_adequacy", "High", "coinsurance penalty",   "Coinsurance penalty applicable"),
        ("coverage_adequacy", "Moderate", "limits review",     "Policy limits require review"),
        ("coverage_adequacy", "Moderate", "coverage concern",  "Coverage concern identified"),
        ("coverage_adequacy", "Moderate", "exclusion",         "Policy exclusion referenced"),
 
        # ── UNDERWRITING-SPECIFIC ──────────────────────────────────────────
        ("risk_severity", "Highly Severe", "catastrophic exposure", "Catastrophic exposure identified"),
        ("risk_severity", "Highly Severe", "zone ae",               "FEMA Zone AE flood risk"),
        ("risk_severity", "Highly Severe", "cat zone",              "CAT zone exposure identified"),
        ("risk_severity", "High",          "fire",                  "Fire hazard or loss identified"),
        ("risk_severity", "High",          "theft",                 "Theft loss identified"),
        ("risk_severity", "High",          "electrical arc",        "Electrical arc hazard noted"),
        ("risk_severity", "High",          "high hazard",           "High hazard classification"),
        ("risk_severity", "High",          "open loss",             "Open/reserved loss present"),
        ("risk_severity", "Moderate",      "wind damage",           "Wind damage documented"),
        ("risk_severity", "Moderate",      "water damage",          "Water damage documented"),
        
        # ── 8. REPUTATION RISK ─────────────────────────────────────────────
        ("reputation_risk", "Highly Severe", "bad faith",           "Bad faith claim against carrier"),
        ("reputation_risk", "Highly Severe", "extra-contractual",   "Extra-contractual liability exposure"),
        ("reputation_risk", "Highly Severe", "punitive damages",    "Punitive damages sought against carrier"),
        ("reputation_risk", "Highly Severe", "regulatory complaint","Regulatory complaint filed"),
        ("reputation_risk", "Highly Severe", "department of insurance", "DOI involvement indicated"),
        ("reputation_risk", "High",          "unfair claims",       "Unfair claims practices allegation"),
        ("reputation_risk", "High",          "good faith",          "Good faith handling questioned"),
        ("reputation_risk", "High",          "media",               "Media exposure or press coverage risk"),
        ("reputation_risk", "High",          "news coverage",       "News coverage of claim or incident"),
        ("reputation_risk", "High",          "social media",        "Social media exposure identified"),
        ("reputation_risk", "High",          "class action",        "Class action reputational exposure"),
        ("reputation_risk", "High",          "doi complaint",       "Department of Insurance complaint"),
        ("reputation_risk", "High",          "market conduct",      "Market conduct examination referenced"),
        ("reputation_risk", "Moderate",      "complaint filed",     "Formal complaint against carrier process"),
        ("reputation_risk", "Moderate",      "escalated",           "Claim escalated — handling scrutiny"),
        ("reputation_risk", "Moderate",      "public record",       "Matter entered public record"),
    ]
 
    # ── Find all matches, deduplicate by (type, trigger) ──────────────────
    seen_keys: set[str] = set()
    signals: list[dict] = []
 
    for sig_type, severity, keyword, description in _EXTENDED_KEYWORDS:
        dedup_key = f"{sig_type}:{keyword.lower()}"
        if dedup_key in seen_keys:
            continue
 
        kw_lower = keyword.lower()
        import re as _re
        if not _re.search(r'\b' + _re.escape(kw_lower) + r'\b', text_lower):
            continue
 
        seen_keys.add(dedup_key)
 
        # ── Extract context snippet ──────────────────────────────────────
        idx = text_lower.find(kw_lower)
        snippet_start = max(0, idx - 150)   # more context before
        snippet_end   = min(len(full_text), idx + len(keyword) + 300)
        # Walk back to word boundary
        while snippet_start > 0 and full_text[snippet_start - 1] not in (' ', '\n', '.', ','):
            snippet_start -= 1
 
        snippet = full_text[snippet_start:snippet_end].strip().replace("\n", " ")
        # Truncate to 120 chars for the schema
        if len(snippet) > 800:
           snippet = snippet[:797] + "…"
 
        # ── Negation check — skip if keyword appears in a negating context ──
        _NEGATION_PATTERNS = [
            r"i(?:'m| am) fine",
            r"(?:no|not|without|denies?|denied|none|wasn't|was not) " + _re.escape(kw_lower),
            r"no injuries",
            r"not injured",
            r"wasn't (?:in|hurt|injured)",
            r"was not (?:in|hurt|injured)",
        ]
        _is_negated = any(
            _re.search(pat, snippet.lower())
            for pat in _NEGATION_PATTERNS
        )
        if _is_negated:
            continue

        
        signals.append({
            "type":            sig_type,
            "severity_level":  severity,
            "description":     description,
            "supporting_text": snippet,
            "trigger_matched": keyword,
            "confidence": _get_keyword_signal_confidence(
                keyword, severity, context=snippet
            ),
            "_source":         "keyword",
        })
 
    return signals

def _regex_extract_from_text(full_text: str) -> dict:
    """
    Fast regex-based extraction for structured patterns that
    appear reliably in FNOL/transcript text documents.
    Catches dates, times, IDs, phone numbers, emails etc.
    without any LLM call.
    """
    import re
    results = {}

    _PATTERNS = {
        "Claim Number":        r'\b(claim\s*(?:number|no|#)[:\s#]*([A-Z0-9\-]{5,20}))',
        "Policy Number":       r'\b(policy\s*(?:number|no|#)[:\s#]*([A-Z0-9\-]{5,20}))',
        "Date of Loss":        r'\b(date\s*of\s*loss[:\s]*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}|\w+ \d{1,2},?\s*\d{4}))',
        "Time of Loss":        r'\b(time\s*of\s*(?:loss|incident)[:\s]*(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?))',
        "Phone Number":        r'\b((?:phone|cell|mobile|contact)[:\s]*(\(?\d{3}\)?[\s\-\.]\d{3}[\s\-\.]\d{4}))',
        "Email":               r'\b([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})',
        "Police Report Number":r'\b(police\s*report\s*(?:number|no|#)[:\s#]*([A-Z0-9\-]{4,20}))',
        "Location of Loss":    r'\b((?:location|address|place)\s*of\s*(?:loss|incident)[:\s]*([^\n]{5,80}))',
        "Adjuster":            r'\b(adjuster[:\s]*([A-Z][a-z]+ [A-Z][a-z]+))',
        "Cause of Loss":       r'\b(cause\s*of\s*(?:loss|incident)[:\s]*([^\n]{3,60}))',
    }

    for field_name, pattern in _PATTERNS.items():
        try:
            match = re.search(pattern, full_text, re.IGNORECASE)
            if match:
                # Last capture group is the value
                value = match.group(match.lastindex).strip().rstrip(".,;")
                if value and len(value) >= 2:
                    results[field_name] = {
                        "value":      value,
                        "confidence": 0.92,
                        "source_text": match.group(0)[:100],
                        "azure_di_key": None,
                        "_source": "regex",
                    }
        except Exception:
            continue

    return results


def _spacy_extract_from_text(full_text: str) -> dict:
    """
    Use spaCy NER to extract person names, orgs, dates, locations
    from unstructured text. Zero LLM cost.
    """
    try:
        import spacy
        nlp = spacy.load("en_core_web_sm")
    except Exception:
        return {}

    doc     = nlp(full_text[:10000])
    results = {}
    seen    = set()

    _LABEL_MAP = {
        "PERSON":  "Claimant Name",
        "ORG":     "Organization",
        "GPE":     "Location",
        "LOC":     "Location of Loss",
        "DATE":    "Date of Loss",
        "TIME":    "Time of Loss",
        "MONEY":   "Estimated Damage",
        "CARDINAL":"Reference Number",
    }

    for ent in doc.ents:
        mapped = _LABEL_MAP.get(ent.label_)
        if not mapped or mapped in seen:
            continue
        value = ent.text.strip()
        if len(value) < 2:
            continue
        seen.add(mapped)
        results[mapped] = {
            "value":       value,
            "confidence":  0.80,
            "source_text": full_text[max(0,ent.start_char-30):ent.end_char+30],
            "azure_di_key": None,
            "_source": "spacy",
        }

    return results    

def _llm_enrich_signals(
    full_text: str,
    doc_type: str,
    keyword_signals: list[dict],
) -> list[dict]:
    """
    PATCHED: NEW FUNCTION
    Layer 2 of the signal fallback chain.
    LLM reviews keyword-detected signals and adds any missed ones.
    Uses a focused prompt to avoid redundancy with existing signals.
    """
    if not keyword_signals:
        # If keywords found nothing, run a full LLM scan instead
        existing_summary = "No keyword signals were detected."
    else:
        sig_lines = []
        for s in keyword_signals[:15]:   # cap to avoid prompt bloat
            sig_lines.append(
                f"  - [{s['severity_level']}] {s['type']}: {s['description']}"
            )
        existing_summary = "\n".join(sig_lines)
 
    system = f"""
You are a senior insurance risk analyst performing signal detection on a {doc_type} document.
 
ALREADY DETECTED SIGNALS (do not duplicate these):
{existing_summary}
 
YOUR TASK:
1. Read the document text carefully.
2. Identify ANY risk signals NOT already in the list above.
3. Focus especially on:
   - Severity signals (injury level, property damage extent)
   - Litigation risk (attorney, lawsuits, demands)
   - Recovery/subrogation opportunities (third party fault)
   - Settlement posture (aggressive vs cooperative)
   - Medical complexity
   - Coverage issues
 
RULES:
- Only return NEW signals not covered by the existing list
- supporting_text must be verbatim from the document (max 800 chars)
- If no new signals found, return empty signals array
- Be specific — reference exact document language
 
Return ONLY valid JSON:
{{
  "signals": [
    {{
      "type":            "<signal_type>",
      "severity_level":  "<Highly Severe|High|Moderate|Low>",
      "description":     "<plain English explanation>",
      "supporting_text": "<VERBATIM quote, max 800 chars>",
      "trigger_matched": "<keyword or phrase that triggered this>",
      "confidence":      <0.0-1.0>
    }}
  ]
}}
""".strip()
 
    user = (
        f"Document type: {doc_type}\n"
        f"Find additional risk signals not already detected.\n\n"
        f"--- DOCUMENT TEXT ---\n{full_text[:8000]}"
    )
 
    result = _llm_call(
        system_prompt=system,
        user_prompt=user,
        max_tokens=2000,
        label="signals_enrichment",
        use_enhanced=False,
    )
 
    if not result:
        return []
 
    new_signals = result.get("signals") or []
 
    # Tag enrichment signals
    for sig in new_signals:
        sig["_source"] = "llm_enriched"
 
    return new_signals


def _reclassify_signals_by_context(signals: list[dict], full_text: str) -> list[dict]:
    """
    Post-process signals to reclassify based on surrounding context.

    Key fix: "liability" / "negligence" / "damages" keywords that appear in a
    third-party-fault context (rear-end, "she hit me", "I was stopped", etc.)
    should be recovery_subrogation, not legal_escalation.
    """
    import re as _re
    text_lower = full_text.lower()

    _THIRD_PARTY_FAULT_PHRASES = [
        "she hit me", "he hit me", "they hit me",
        "hit me", "ran into me", "rear ended", "rear impact",
        "at fault", "her fault", "his fault", "their fault",
        "other driver", "other vehicle", "other party",
        "pushed me into", "pushed you into",
        "she's at fault", "he's at fault",
        "i was stopped", "fully stopped", "was stationary",
        "ran a red", "ran the red", "ran a stop",
        "wrong lane", "wrong side",
        "third party", "subrogation",
        # Add: generic "assume she/he at fault" language
        "assume she", "assume he", "i assume",
    ]

    # Phrases that confirm genuine litigation (keep as legal_escalation)
    _LITIGATION_CONFIRMED_PHRASES = [
        "filed suit", "lawsuit filed", "attorney retained",
        "counsel retained", "legal action", "demand letter",
        "punitive", "bad faith", "complaint filed", "court filing",
        "class action", "pre-litigation",
    ]

    # Triggers that should be reclassified when third-party context present
    _RECLASSIFY_TRIGGERS = {
        "liability", "negligence", "damages", "damages sought",
        "third party",
    }

    reclassified: list[dict] = []

    for sig in signals:
        sig = dict(sig)
        trigger  = (sig.get("trigger_matched") or "").lower().strip()
        sig_type = sig.get("type", "")
        support  = (sig.get("supporting_text") or "").lower()

        # Only attempt reclassification for legal_escalation signals whose
        # trigger is in the ambiguous set
        if sig_type == "legal_escalation" and trigger in _RECLASSIFY_TRIGGERS:
            # Use both the supporting snippet AND the broader document text
            context = support + " " + text_lower[:5000]

            has_third_party = any(
                phrase in context for phrase in _THIRD_PARTY_FAULT_PHRASES
            )
            has_confirmed_litigation = any(
                phrase in context for phrase in _LITIGATION_CONFIRMED_PHRASES
            )

            if has_third_party and not has_confirmed_litigation:
                sig["type"]           = "recovery_subrogation"
                sig["severity_level"] = "High"
                sig["description"]    = (
                    "Third-party fault indicated — potential subrogation opportunity. "
                    + sig.get("description", "")
                )
                sig["_reclassified_from"] = "legal_escalation"

        reclassified.append(sig)

    return reclassified

def _llm_filter_entities(entities: dict, doc_type: str, full_text: str) -> dict:
    """
    Uses a single fast LLM call to filter out semantically incorrect
    key-value pairs. Returns only the fields that are valid.
    Runs after _verify_entities_against_text().
    """
    if not entities:
        return entities

    # Build a compact list of field:value pairs for the LLM to review
    pairs = []
    for fname, fdata in list(entities.items())[:40]:  # cap to 40 fields
        if not isinstance(fdata, dict):
            continue
        val = (fdata.get("value") or "").strip()
        if val:
            # Truncate long values so prompt stays small
            pairs.append(f'  "{fname}": "{val[:120]}"')

    if not pairs:
        return entities

    pairs_str = "\n".join(pairs)

    system = f"""You are a data quality validator for {doc_type} insurance documents.

You will receive a list of extracted key-value pairs from a document.
Your job: identify which field values are SEMANTICALLY INCORRECT for their field name.

A value is INCORRECT if:
- A "Date" field contains billing statement text, payment history, or long prose
- A "Patient" or "Name" field contains addresses, dollar amounts, or multiple sentences  
- A "Provider" field contains billing codes or full paragraph text
- Any short-label field (Date, Name, ID, Amount) contains a long paragraph (>50 words)
- The value is clearly from a completely different section of the document than the field name implies
- A "Years in Business" field contains a calendar year like "2003" or "1998" 
  instead of a duration like "22 years" or "Since 2003"

A value is CORRECT if:
- A date field has an actual date
- A name field has a person or org name  
- An amount field has a dollar value
- The value makes sense as an answer to "what is the [field name]?"

Return ONLY valid JSON — no explanation:
{{
  "keep": ["<field_name1>", "<field_name2>", ...],
  "remove": ["<field_name3>", ...]
}}

Only list fields in "remove" if you are highly confident the value is wrong.
When in doubt, keep the field."""

    user = (
        f"Document type: {doc_type}\n\n"
        f"Extracted fields to validate:\n{pairs_str}\n\n"
        f"Document context (first 500 chars): {full_text[:500]}"
    )

    result = _llm_call(
        system_prompt=system,
        user_prompt=user,
        max_tokens=600,
        label="entity_filter",
        use_enhanced=False,  # Use cheap model — this is a filter, not extraction
    )

    if not result:
        return entities  # If LLM fails, show everything (safe fallback)

    to_remove = set(result.get("remove") or [])
    if not to_remove:
        return entities

    return {
        fname: fdata
        for fname, fdata in entities.items()
        if fname not in to_remove
    }

# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — TWO-CALL ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

def analyse_document(
    full_text: str,
    doc_type: str,
    azure_di_fields: dict[str, dict] | None = None,
    source: str = "pdf",
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

    text_a = full_text[:8000] + ("\n\n[... document truncated ...]" if len(full_text) > 8000 else "")
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
        + "\\nDetect ALL risk signals across ALL domains. Be exhaustive — do not stop after 2-3 signals."
        + "\\nScan for: injury severity, litigation risk, recovery/subrogation, settlement posture, "
        + "medical complexity, and coverage issues."
        + f"\\n\\n--- DOCUMENT TEXT ---\\n{full_text[:10000]}"
    )
    result_b = _llm_call(
        system_prompt=_signals_system(doc_type),
        user_prompt=user_b_signals,
        max_tokens=3500,   # PATCHED: was 2500
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
        entities = _verify_entities_against_text(entities, full_text, source=source)

        # ── PATCH: LLM semantic filter ────────────────────────────────────
        entities = _llm_filter_entities(entities, doc_type, full_text)
        
        for _, ed in entities.items():
            if isinstance(ed, dict):
                ed.setdefault("azure_di_key", None)

    # ── HYBRID: regex + spaCy extraction for TXT sources ─────────────────
    if source == "txt":  # pass source down or check full_text length
        regex_entities = _regex_extract_from_text(full_text)
        spacy_entities = _spacy_extract_from_text(full_text)

        # Merge: LLM wins on conflicts, regex/spaCy fill gaps
        for fname, fdata in {**spacy_entities, **regex_entities}.items():
            norm = fname.strip().lower()
            already_covered = any(
                norm in (k.strip().lower()) 
                for k in entities.keys()
            )
            if not already_covered:
                entities[fname] = fdata           
    
    # LAYER 1: LLM dedicated signal call (Call B result)
    llm_signals = []
    if result_b:
        llm_signals = result_b.get("signals") or []
        llm_signals = _validate_signals_against_text(llm_signals, full_text)
        for s in llm_signals:
            s.setdefault("_source", "llm")

    # LAYER 2: Keyword extraction (always runs, zero LLM cost)
    keyword_signals = _keyword_extract_signals(full_text, doc_type)

    # LAYER 3: LLM enrichment (runs when LLM signals are sparse)
    enriched_signals = []
    total_llm_count = len([s for s in llm_signals if not s.get("_unverified")])

    if total_llm_count < 3:
        # LLM found fewer than 3 verified signals — enrich with keyword+LLM hybrid
        enriched_signals = _llm_enrich_signals(full_text, doc_type, keyword_signals)
        enriched_signals = _validate_signals_against_text(enriched_signals, full_text)

    # ── Merge all three layers, deduplicate by (type, trigger) ─────────────
    def _sig_dedup_key(s: dict) -> str:
        return f"{s.get('type','').lower()}:{s.get('trigger_matched','').lower()[:30]}"

    seen_sig_keys: set = set()
    merged_signals: list = []

    # Priority: LLM > Enriched > Keyword
    for sig in llm_signals + enriched_signals:
        k = _sig_dedup_key(sig)
        if k not in seen_sig_keys:
            seen_sig_keys.add(k)
            merged_signals.append(sig)

    # Add keyword signals that aren't already covered by LLM/enriched
    for sig in keyword_signals:
        k = _sig_dedup_key(sig)
        covered = False
        for existing_key in seen_sig_keys:
            existing_type = existing_key.split(":")[0]
            if existing_type == sig.get("type", "").lower():
                trigger_new = sig.get("trigger_matched", "").lower()
                if trigger_new and trigger_new[:10] in existing_key:
                    covered = True
                    break
        if not covered and k not in seen_sig_keys:
            seen_sig_keys.add(k)
            merged_signals.append(sig)

    signals = merged_signals

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
    

     # ── Import and apply signal deduplication ─────────────────────────────
    try:
        from ui.pdf_analysis import _semantic_dedup_signals, _consolidate_signals
        signals = _semantic_dedup_signals(signals)
        signals = _consolidate_signals(signals, doc_type)
    except ImportError:
        pass   # safe fallback — dedup also runs in the UI layer

    signals = _reclassify_signals_by_context(signals, full_text)

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
    """
    PATCHED: Much more permissive. Signals are never dropped — only confidence
    is reduced when verification fails. This ensures the UI always shows
    signals that the LLM detected, with a visual warning on unverified ones.
    """
    import re as _re
    if not full_text or not signals:
        return signals
 
    text_lower = full_text.lower()
    validated  = []
 
    for sig in signals:
        supporting = (sig.get("supporting_text") or "").strip()
        trigger    = (sig.get("trigger_matched")  or "").strip()
        desc       = (sig.get("description")      or "").strip()
 
        grounded = False
 
        # ── Check 1: supporting_text verbatim in document ──────────────────
        if supporting and len(supporting) >= 3:   # PATCHED: was 6/8
            if supporting.lower() in text_lower:
                grounded = True
            else:
                sup_toks = {
                    t for t in supporting.lower().split()
                    if len(t) >= 3
                }
                doc_toks = set(text_lower.split())
                if sup_toks:
                    overlap = len(sup_toks & doc_toks) / len(sup_toks)
                    if overlap >= 0.25:    # PATCHED: was 0.40
                        grounded = True
 
        # ── Check 2: trigger keyword in document ──────────────────────────
        if not grounded and trigger:
            trigger_clean = _re.sub(r"[—\-–]", " ", trigger).strip()
            trigger_lower = trigger.lower()
            trigger_clean_lower = trigger_clean.lower()
            if trigger_lower in text_lower or trigger_clean_lower in text_lower:
                grounded = True
 
        # ── Check 3: ANY significant word from trigger in document ─────────
        if not grounded and trigger:
            trigger_words = {
                w for w in trigger.lower().split()
                if len(w) >= 4
            }
            if trigger_words and trigger_words & set(text_lower.split()):
                grounded = True
 
        # ── Check 4: description keywords in document ──────────────────────
        # PATCHED: New check — use description words as additional evidence
        if not grounded and desc:
            desc_words = {
                w for w in desc.lower().split()
                if len(w) >= 5
                and w not in {"signal", "detected", "identified", "document",
                              "indicates", "referenced", "mentioned", "present",
                              "reported", "found", "noted"}
            }
            doc_words = set(text_lower.split())
            if desc_words:
                overlap = len(desc_words & doc_words) / len(desc_words)
                if overlap >= 0.30:   # 30% of significant desc words in doc
                    grounded = True
 
        # ── Check 5: signal type keyword heuristic ─────────────────────────
        # PATCHED: New check — if signal type matches obvious doc content, keep it
        _TYPE_KEYWORDS = {
            "severity":           ["injur", "pain", "fractur", "surg", "hospital",
                                   "fatal", "death", "disab", "trauma", "loss"],
            "legal_escalation":   ["attorney", "lawyer", "lawsuit", "court", "legal",
                                   "litigat", "demand", "complaint", "counsel"],
            "litigation_exposure":["attorney", "lawyer", "lawsuit", "court", "legal"],
            "fraud_indicator":    ["inconsist", "suspic", "staged", "false", "fraud",
                                   "misrepresent", "conflict", "unusual"],
            "medical_complexity": ["surg", "hospital", "therapy", "specialist",
                                   "chronic", "treatment", "procedure", "medic"],
            "recovery_subrogation":["third party", "subroga", "vendor", "contractor",
                                    "liable", "recover", "upstream"],
            "coverage_adequacy":  ["exclusion", "limit", "denial", "reserv", "gap",
                                   "underinsur", "coverage"],
            "coverage_issue":     ["exclusion", "denial", "reserv", "coverage"],
            "risk_severity":      ["injur", "loss", "damage", "severe", "hazard"],
            "risk_appetite":      ["non-renew", "disclose", "incomplete", "expir"],
        }
        if not grounded:
            sig_type = sig.get("type", "").lower()
            type_hints = _TYPE_KEYWORDS.get(sig_type, [])
            if any(hint in text_lower for hint in type_hints):
                grounded = True
 
        # ── Apply result — NEVER drop, only reduce confidence ──────────────
        # PATCHED: was conditionally dropping signals
        sig = dict(sig)   # make a copy
        if not grounded:
            sig["confidence"]  = min(float(sig.get("confidence", 0.5)), 0.45)
            sig["_unverified"] = True
        else:
            sig.pop("_unverified", None)
 
        validated.append(sig)
 
    return validated



def _verify_entities_against_text(entities: dict, full_text: str, source: str = "pdf") -> dict:
    # TXT sources are already clean text — skip aggressive verification
    if source == "txt":
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
    _CACHE_VERSION = "v4"
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
    # analysis       = analyse_document(full_text, doc_type, azure_di_fields=azure_di_index)
    analysis = analyse_document(full_text, doc_type, azure_di_fields=azure_di_index, source=source)

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

    