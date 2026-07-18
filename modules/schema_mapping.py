"""
modules/schema_mapping.py
Field-to-schema mapping, confidence scoring, claim-ID detection,
title-field extraction, and LLM-assisted field-map.

CHANGED:
  llm_map_unknown_fields() prompt no longer uses the term "unmapped" or asks
  the LLM to populate an "_unmapped" list.  Instead the LLM is instructed to
  map every column it can confidently handle; columns it cannot map are simply
  omitted from the result.  The concept of "unmapped columns" (used in
  the old UI panel) has been replaced throughout: a column is only considered
  unmapped / problematic when its VALUE is empty after all mapping passes,
  not merely because no schema field matched the column header.
"""

import re
import datetime

import streamlit as st

from config.settings import MIN_HEADER_MATCH
from modules.audit import _append_audit
from modules.llm import _llm_available, _llm_call


# ── Date parsing helper ───────────────────────────────────────────────────────

_DATE_PARSE_FORMATS = [
    "%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y",
    "%m-%d-%Y", "%d.%m.%Y", "%Y/%m/%d", "%B %d, %Y", "%b %d, %Y",
]

def _try_parse_date(value: str):
    v = str(value).strip()
    for fmt in _DATE_PARSE_FORMATS:
        try:
            return datetime.datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


# ── Utility ───────────────────────────────────────────────────────────────────

def detect_claim_id(row: dict, index: int | None = None) -> str:
    _CHECK_NO_KEYS = [
    "check number", "check no", "check #", "check num",
    "cheque number", "cheque no",
]

def detect_claim_id(row: dict, index: int | None = None) -> str:
    # Check-register rows key off Check Number, not Claim Number -- multiple
    # checks can reference the same claim, so keying on Claim Number would
    # make distinct checks collide as "the same record" in claim_dup_store.
    for k, v in row.items():
        name = str(k).lower().replace("_", " ").strip()
        if any(x in name for x in _CHECK_NO_KEYS):
            val = v.get("modified") or v.get("value")
            if val and str(val).strip():
                return str(val)
              
    keys = [
        "claim id", "claim_id", "claimid", "claim number", "claim no",
        "claim #", "claim ref", "claim reference", "file number", "record id",
        "claim number", "clm id", "clm no", "clm#", "loss ref",
    ]
    for k, v in row.items():
        name = str(k).lower().replace("_", " ").strip()
        if any(x in name for x in keys):
            val = v.get("modified") or v.get("value")
            if val and str(val).strip():
                return str(val)
    for k, v in row.items():
        k_norm = str(k).lower().replace("_", " ").strip()
        score  = max(_str_similarity(k_norm, kw) for kw in keys)
        if score >= 0.55:
            val = v.get("modified") or v.get("value")
            if val and str(val).strip() and len(str(val).strip()) >= 2:
                return str(val)
    import re as _re
    for k, v in row.items():
        val = str(v.get("modified") or v.get("value") or "").strip()
        if _re.match(r"^[A-Z]{2,5}[-_]?[A-Z0-9]{2,15}$", val, _re.IGNORECASE):
            return val
    if index is not None:
        return str(index + 1)
    return ""


def get_val(claim: dict, keys: list, default: str = "") -> str:
    for pk in keys:
        for k, v in claim.items():
            if pk.lower() in str(k).lower() or str(k).lower() in pk.lower():
                val = v.get("modified") or v.get("value") or ""
                if val:
                    return val
    for pk in keys:
        for k, v in claim.items():
            k_norm = str(k).lower().replace("_", " ").strip()
            pk_norm = pk.lower().replace("_", " ").strip()
            if _str_similarity(k_norm, pk_norm) >= 0.6:
                val = v.get("modified") or v.get("value") or ""
                if val:
                    return val
    return default


# ── Confidence engine ─────────────────────────────────────────────────────────

def _word_tokens(s: str) -> set:
    stopwords = {"of", "the", "a", "an", "and", "or", "to", "in", "for"}
    words = re.sub(r"[_/#+]", " ", s.lower()).split()
    return {w for w in words if len(w) > 1 and w not in stopwords}


def _str_similarity(a: str, b: str) -> float:
    a_tok, b_tok = _word_tokens(a), _word_tokens(b)
    if not a_tok or not b_tok:
        return 0.0
    if a_tok == b_tok:
        return 1.0
    return len(a_tok & b_tok) / len(a_tok | b_tok)


def _header_match_score(excel_col: str, schema_field: str, aliases: list) -> float:
    ec_norm = excel_col.lower().replace("_", " ").strip()
    for alias in aliases:
        if ec_norm == alias.lower():
            return 1.0
    best = max((_str_similarity(ec_norm, a.lower()) for a in aliases), default=0.0)
    return max(best, _str_similarity(ec_norm, schema_field.lower()))


def _value_quality_score(value: str, schema_field: str) -> float:
    if not value or not value.strip():
        return 0.0
    v, sf = value.strip(), schema_field.lower()
    if any(x in sf for x in ["date", "loss dt"]):
        for p in [
            r"\d{2}-\d{2}-\d{4}", r"\d{4}-\d{2}-\d{2}",
            r"\d{2}/\d{2}/\d{4}", r"\d{1,2}/\d{1,2}/\d{2,4}",
        ]:
            if re.fullmatch(p, v):
                return 1.0
        return 0.4
    if any(x in sf for x in ["incurred", "paid", "reserve", "amount", "deductible", "recovery"]):
        try:
            float(v.replace(",", "").replace("$", "").replace("(", "-").replace(")", ""))
        except ValueError:
            return 0.3
        return 1.0
    if any(x in sf for x in ["id", "number", "no", "code"]):
        return 0.9 if len(v) >= 2 else 0.5
    if "status" in sf:
        return 1.0 if v.lower() in {"open", "closed", "pending", "reopened", "denied", "settled"} else 0.7
    return 0.85 if len(v) > 0 else 0.0


# ── Title-field extractor (plain KV dict → schema-ready dict) ─────────────────

def extract_title_fields_from_kvs(title_kvs: dict) -> dict:
    result: dict = {}
    for canonical_key, info in title_kvs.items():
        value = str(info.get("value", "")).strip()
        if not value:
            continue
        result[canonical_key] = {
            "value":      value,
            "original":   info.get("original", value),
            "modified":   info.get("modified", value),
            "source":     info.get("source", "title_kv"),
            "excel_row":  info.get("excel_row", 0),
            "excel_col":  info.get("excel_col", 0),
            "title_text": value,
        }
    return result


# ── NEW: empty-value field detection ─────────────────────────────────────────

def get_empty_value_fields(claims: list, schema_name: str | None = None) -> list[str]:
    """
    Return column names whose values are empty across ALL supplied claim rows.

    This is the replacement for the old "unmapped columns" concept.  A column
    is only flagged here when every row in the sheet has an empty/None value
    for it — meaning no useful data was captured, regardless of whether the
    header was matched to a schema field.

    Used by the export panel and UI instead of the old unmapped-columns panel.
    """
    if not claims:
        return []
    all_keys: set[str] = set()
    for claim in claims:
        all_keys.update(claim.keys())

    empty_cols: list[str] = []
    for col in sorted(all_keys):
        values = [
            str(claim.get(col, {}).get("value", "") or "").strip()
            for claim in claims
        ]
        if all(v == "" for v in values):
            empty_cols.append(col)
    return empty_cols


# ── Schema mapper ─────────────────────────────────────────────────────────────

def map_claim_to_schema(
    claim: dict,
    schema_name: str,
    title_fields: dict | None = None,
    llm_field_map: dict | None = None,
) -> dict:
    from config.schemas import SCHEMAS
    if schema_name not in SCHEMAS:
        return {}
    schema        = SCHEMAS[schema_name]
    aliases       = schema.get("field_aliases", {})
    accepted      = schema["accepted_fields"]
    title_fields  = title_fields or {}
    llm_field_map = llm_field_map or {}

    llm_reverse: dict[str, str] = {}
    for src_col, schema_field in llm_field_map.get("mappings", {}).items():
        if schema_field not in llm_reverse:
            llm_reverse[schema_field] = src_col

    result: dict        = {}
    used_excel_cols: set = set()

    for schema_field in accepted:
        field_aliases                         = aliases.get(schema_field, [schema_field.lower()])
        best_excel_col, best_header_sc, best_info = None, 0.0, None

        for excel_col, info in claim.items():
            if excel_col in used_excel_cols:
                continue
            h_sc = _header_match_score(excel_col, schema_field, field_aliases)
            if h_sc > best_header_sc:
                best_header_sc, best_excel_col, best_info = h_sc, excel_col, info

        if best_header_sc >= MIN_HEADER_MATCH and best_info is not None:
            val  = best_info.get("modified", best_info.get("value", ""))
            v_sc = _value_quality_score(val, schema_field)
            conf = round(best_header_sc * 0.40 * 100 + v_sc * 0.60 * 100)
            result[schema_field] = {
                "excel_field":  best_excel_col,
                "value":        val,
                "header_score": round(best_header_sc * 100),
                "value_score":  round(v_sc * 100),
                "confidence":   conf,
                "is_required":  schema_field in schema["required_fields"],
                "info":         best_info,
                "from_title":   False,
                "llm_mapped":   False,
            }
            used_excel_cols.add(best_excel_col)

        elif schema_field in llm_reverse:
            src_col = llm_reverse[schema_field]
            if src_col in claim:
                info = claim[src_col]
                val  = info.get("modified", info.get("value", ""))
                v_sc = _value_quality_score(val, schema_field)
                conf = round(0.75 * 0.40 * 100 + v_sc * 0.60 * 100)
                result[schema_field] = {
                    "excel_field":  src_col,
                    "value":        val,
                    "header_score": 75,
                    "value_score":  round(v_sc * 100),
                    "confidence":   conf,
                    "is_required":  schema_field in schema["required_fields"],
                    "info":         info,
                    "from_title":   False,
                    "llm_mapped":   True,
                }
                used_excel_cols.add(src_col)

        elif schema_field in title_fields:
            tf   = title_fields[schema_field]
            val  = tf.get("value", "")
            v_sc = _value_quality_score(val, schema_field)
            conf = min(95, round(1.0 * 0.40 * 100 + v_sc * 0.60 * 100))
            result[schema_field] = {
                "excel_field":  f"[title row {tf['excel_row']}]",
                "value":        val,
                "header_score": 100,
                "value_score":  round(v_sc * 100),
                "confidence":   conf,
                "is_required":  schema_field in schema["required_fields"],
                "info":         tf,
                "from_title":   True,
                "llm_mapped":   False,
            }

    # ── Cross-field date validation ──────────────────────────────────────────
    _date_pairs = [
        ("Loss Date",    "Date Reported"),
        ("Loss Date",    "Date Closed"),
        ("Date Reported","Date Closed"),
    ]
    for earlier_field, later_field in _date_pairs:
        if earlier_field not in result or later_field not in result:
            continue
        _early_val = result[earlier_field].get("value", "")
        _late_val  = result[later_field].get("value", "")
        _early_dt  = _try_parse_date(_early_val)
        _late_dt   = _try_parse_date(_late_val)
        if _early_dt and _late_dt and _late_dt < _early_dt:
            _warn_msg = (
                f"⚠ Date order conflict: '{later_field}' ({_late_val}) "
                f"is before '{earlier_field}' ({_early_val}). "
                f"Please verify these fields are mapped correctly."
            )
            result[earlier_field]["confidence"]    = max(10, result[earlier_field]["confidence"] - 30)
            result[later_field]["confidence"]      = max(10, result[later_field]["confidence"] - 30)
            result[earlier_field]["date_order_warning"] = _warn_msg
            result[later_field]["date_order_warning"]   = _warn_msg

    return result







# ── Unknown-field detection + LLM field mapper ────────────────────────────────
def extract_title_fields(merged_meta: dict) -> dict:
    found: dict = {}
    title_rows  = sorted(
        [v for v in merged_meta.values() if v.get("value") and v["type"] in ("TITLE", "HEADER")],
        key=lambda x: (x["row_start"], x["col_start"]),
    )
    for m in title_rows:
        text, r, c = str(m["value"]).strip(), m["excel_row"], m["excel_col"]
        region_key = f"R{m['row_start']}C{m['col_start']}"  # unique key per region

        def _info(val, src_key=None):
            return {
                "value":       val,
                "original":    val,
                "modified":    val,
                "source":      "title_row",
                "excel_row":   r,
                "excel_col":   c,
                "title_text":  text,
                "region_key":  src_key or region_key,
                "region_type": m.get("type", "TITLE"),
                "cell_range":  f"R{m['row_start']}C{m['col_start']}→R{m['row_end']}C{m['col_end']}",
                "span_cols":   m.get("span_cols", 1),
            }

        # ── Regex-based semantic extraction (existing logic) ──────────────────
        matched_semantically = False

        pol = re.search(r'Policy\s*(?:#|No\.?|Number)?\s*[:\-]\s*([A-Z0-9][A-Z0-9\-/\.]+)', text, re.IGNORECASE)
        if pol and "Policy Number" not in found:
            found["Policy Number"] = _info(pol.group(1).strip())
            matched_semantically = True

        ins = re.search(r'Insured\s*[:\-]\s*([^\|;]+)', text, re.IGNORECASE)
        if ins and "Insured Name" not in found:
            found["Insured Name"] = _info(ins.group(1).strip())
            matched_semantically = True

        carr = re.search(r'Carrier\s*[:\-]\s*([^\|;]+)', text, re.IGNORECASE)
        if carr:
            val = carr.group(1).strip()
            for k in ("Carrier", "Carrier Name"):
                if k not in found:
                    found[k] = _info(val)
            matched_semantically = True

        state = re.search(r'State\s*[:\-]\s*([^\|;]+)', text, re.IGNORECASE)
        if state:
            val = state.group(1).strip()
            for k in ("State", "Jurisdiction", "State Code"):
                if k not in found:
                    found[k] = _info(val)
            matched_semantically = True

        # Underwriting period checked first — if matched, skip generic period
        # regex entirely to avoid creating duplicate date fields from same cell.
        uw_period = re.search(
            r'Underwriting\s+Period\s*[:\-]?\s*'
            r'(\d{1,2}\s+\w+\s+\d{4}|\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})'
            r'[\s\u2013\u2014\-to]+'
            r'(\d{1,2}\s+\w+\s+\d{4}|\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})',
            text, re.IGNORECASE,
        )
        if uw_period:
            s, e = uw_period.group(1).strip(), uw_period.group(2).strip()
            for k, v in [
                ("Underwriting Period Start", s),
                ("Underwriting Period End", e),
            ]:
                if k not in found:
                    found[k] = _info(v)
            matched_semantically = True
        else:
            # Only run generic period regex if NOT an underwriting period cell
            period = re.search(
                r'Period\s*[:\-]?\s*(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4}|\d{1,2}\s+\w+\s+\d{4})'
                r'[\s\u2013\u2014\-to]+'
                r'(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4}|\d{1,2}\s+\w+\s+\d{4})',
                text, re.IGNORECASE,
            )
            if period:
                s, e = period.group(1).strip(), period.group(2).strip()
                for k, v in [
                    ("Policy Period Start", s),
                    ("Policy Period End", e),
                ]:
                    if k not in found:
                        found[k] = _info(v)
                matched_semantically = True

        # Contract / reference number  e.g. "CONTRACT NUMBER:PLIC/25"
        contract = re.search(
            r'Contract\s*(?:No\.?|Number|#|Ref)?\s*[:\-]\s*([A-Z0-9][A-Z0-9\-/\.]+)',
            text, re.IGNORECASE,
        )
        if contract and "Contract Number" not in found:
            found["Contract Number"] = _info(contract.group(1).strip())
            matched_semantically = True

        

        lob_map = [
            (r"workers[\'\'\u2019\s\-]*compensation", "Workers Compensation"),
            (r"workers[\s\-]*comp\b", "Workers Compensation"),
            (r"\bW\.?C\.?\b(?:\s+loss|\s+claim|\s+run)?", "Workers Compensation"),
            (r"commercial\s+general\s+liability", "Commercial General Liability"),
            (r"\bC\.?G\.?L\.?\b", "Commercial General Liability"),
            (r"commercial\s+auto(?:mobile|motive)?", "Commercial Auto"),
            (r"commercial\s+prop(?:erty)?", "Commercial Property"),
            (r"professional\s+liability", "Professional Liability"),
            (r"\bE\.?\s*&\s*O\.?\b", "Professional Liability"),
            (r"general\s+liability|\bG\.?L\.?\b", "General Liability"),
        ]
        for pattern, lob_val in lob_map:
            if re.search(pattern, text, re.IGNORECASE) and "Line of Business" not in found:
                found["Line of Business"] = _info(lob_val)
                matched_semantically = True
                break

        # ── RAW FALLBACK: store every unmatched merged region as-is ──────────
        if not matched_semantically:
            kv_split = re.match(r'^([^:\-]{3,40})[:\-]\s*(.+)$', text, re.IGNORECASE)
            if kv_split:
                # "KEY: VALUE" pattern — use the key part as field name
                raw_key = kv_split.group(1).strip().title()
                raw_val = kv_split.group(2).strip()
            else:
                # No separator — this is a plain label/name cell (e.g. "AR LLC",
                # "Print Excess Ins. Co"). Use region position as the key so we
                # don't use the cell VALUE as the field name (avoids duplicates
                # like "AR LLC" appearing as both a value AND a key).
                _region_type_label = m.get("type", "TITLE").title()
                raw_key = f"{_region_type_label} [{region_key}]"
                raw_val = text

            # Avoid overwriting semantic fields with the same name
            store_key = raw_key if raw_key not in found else f"{raw_key} [{region_key}]"
            found[store_key] = _info(raw_val)

    return found


def _has_unknown_fields(claim_keys: list, schema_name: str) -> bool:
    from config.schemas import SCHEMAS
    from modules.normalization import _best_standard_name
    if schema_name not in SCHEMAS:
        return False
    schema  = SCHEMAS[schema_name]
    aliases = schema.get("field_aliases", {})
    accepted = set(f.lower() for f in schema.get("accepted_fields", []))

    known_tokens: set = set()
    for field, als in aliases.items():
        known_tokens.add(field.lower())
        for a in als:
            known_tokens.add(a.lower())

    unrecognized = 0
    for k in claim_keys:
        k_norm = k.lower().replace("_", " ").strip()
        if k_norm in accepted:
            continue
        if _best_standard_name(k) is not None:
            continue
        if not any(_str_similarity(k_norm, tok) >= 0.65 for tok in known_tokens):
            unrecognized += 1

    return unrecognized >= 1


def llm_map_unknown_fields(sample_rows: list, schema_name: str, sheet_name: str) -> dict:
    """
    Ask the LLM to map source spreadsheet columns to known schema fields.

    CHANGED: the prompt no longer uses the word "unmapped" or asks the LLM
    to populate an _unmapped list.  Columns that cannot be confidently mapped
    are simply omitted from the result.  The "_skipped" key (replacing the
    old "_unmapped") is populated post-hoc from columns absent in mappings —
    it is not surfaced in any UI panel.
    """
    from config.schemas import SCHEMAS
    _file_hash = st.session_state.get("current_file_hash", "")
    cache_key = f"_llm_fieldmap_{_file_hash}_{sheet_name}_{schema_name}"
    if st.session_state.get(cache_key):
        return st.session_state[cache_key]
    if not _llm_available() or not sample_rows:
        st.session_state[cache_key] = {}
        return {}

    schema      = SCHEMAS.get(schema_name, {})
    accepted    = schema.get("accepted_fields", [])
    required    = schema.get("required_fields", [])
    sample_cols = list(sample_rows[0].keys()) if sample_rows else []

    sample_data: dict = {}
    for row in sample_rows[:3]:
        for k, v in row.items():
            val = str(v.get("value", "")).strip()
            if val and k not in sample_data:
                sample_data[k] = val

    sample_str   = "\n".join(f'  - "{col}": "{sample_data.get(col, "(empty)")}"' for col in sample_cols)
    accepted_str = "\n".join(
        f"  - {f}" + (" [REQUIRED]" if f in required else "") for f in accepted
    )

    # ── CHANGED prompt: no "unmapped" language, no _unmapped list ────────────
    prompt = (
        "You are an expert insurance data analyst. You are mapping source spreadsheet columns "
        "to a target schema for claims processing.\n\n"
        f"TARGET SCHEMA: {schema_name}\n"
        f"AVAILABLE SCHEMA FIELDS (map to these exact names):\n{accepted_str}\n\n"
        "SOURCE COLUMNS WITH SAMPLE VALUES:\n"
        f"{sample_str}\n\n"
        "TASK:\n"
        "For each source column that you can confidently identify, map it to the best matching "
        "schema field name.  Columns you are not confident about should simply be omitted from "
        "the result — do NOT list or comment on columns you cannot map.\n\n"
        "Rules:\n"
        "1. Only include a mapping if you are at least 60% confident\n"
        "2. Use the exact schema field name from the list above\n"
        "3. Do NOT map the same schema field twice\n"
        "4. Required fields should be mapped with highest priority\n"
        "5. Do NOT include any list of skipped or unresolved columns\n\n"
        'Reply ONLY with valid JSON (no markdown, no explanation):\n'
        '{\n'
        '  "mappings": {"source_col_name": "Schema Field Name", ...},\n'
        '  "_reasoning": {"source_col_name": "brief reason", ...}\n'
        "}"
    )
    try:
        raw    = _llm_call(prompt, max_tokens=600, call_purpose="field_mapping")
        raw    = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        result = __import__("json").loads(raw)

        valid_fields      = set(f.lower() for f in accepted)
        clean_mappings    = {}
        used_schema_fields: set = set()
        for src_col, schema_field in result.get("mappings", {}).items():
            if schema_field in accepted and schema_field not in used_schema_fields:
                clean_mappings[src_col] = schema_field
                used_schema_fields.add(schema_field)

        # _skipped = cols absent from mappings (internal only, not shown in UI)
        _skipped = [c for c in sample_cols if c not in clean_mappings]

        final = {
            "mappings":   clean_mappings,
            "_skipped":   _skipped,          # renamed from _unmapped; internal use only
            "_reasoning": result.get("_reasoning", {}),
        }
        st.session_state[cache_key] = final
        _append_audit({
            "event":      "LLM_FIELD_MAP",
            "timestamp":  datetime.datetime.now().isoformat(),
            "sheet":      sheet_name,
            "schema":     schema_name,
            "source_cols": sample_cols,
            "mappings":   clean_mappings,
        })
        return final
    except Exception as e:
        st.session_state[cache_key] = {"mappings": {}, "_skipped": sample_cols, "_error": str(e)}
        return st.session_state[cache_key]
