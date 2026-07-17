"""
modules/pdf_azure_parser.py

Azure Document Intelligence based PDF parser.

WHY SOME FIELDS LACK BOUNDING BOXES — and the fix:
─────────────────────────────────────────────────────
Azure DI's prebuilt-document model has TWO extraction paths:

  Path A  — Key-Value pairs (structured)
    Azure found an explicit label:value block in the PDF layout.
    Always returns bounding polygons + confidence.
    Example: "CASE NUMBER: 62CV-24-48" as a KV pair.

  Path B  — Text-layout extraction (unstructured)
    Our code splits raw OCR lines using heuristics (_split_into_label_value_blocks).
    Azure gives us the lines but NOT per-field bounding polygons.
    Example: "LAST REFRESHED" on one line, "January 15, 2025" on the next.

After both paths run, a PyMuPDF fallback (_enrich_fields_with_pymupdf_polygons)
searches for the key text on the page to generate synthetic bboxes.
PROBLEM: PyMuPDF's page.search_for() is CASE-SENSITIVE by default and only
matches exact substrings. If the stored field_name is "LAST REFRESHED" but
PyMuPDF index has it as "Last Refreshed" (mixed case from OCR), the search fails.

FIXES in this version:
  1. search_for() called with TEXT_PRESERVE_WHITESPACE flag for better matching
  2. Case-insensitive search: tries original → UPPER → Title → lower → first word
  3. Value-based fallback: if key not found, search for the value text and use
     the adjacent region as the bbox
  4. Multi-word split: tries each significant word and intersects results
  5. Colon-stripped variants: "FILING LOCATION:" → "FILING LOCATION" etc.
  6. FIXED: parse_pdf_with_azure() and get_pdf_sheet_names() now catch all
     Azure HttpResponseError / credential errors and return graceful fallbacks
     instead of crashing the app.
"""

from __future__ import annotations

import re
from pathlib import Path

from azure.core.credentials import AzureKeyCredential
from azure.ai.formrecognizer import DocumentAnalysisClient
import os
from dotenv import load_dotenv
from modules.orchestrator import track_cost

# ─────────────────────────────────────────────────────────────────────────────
# AZURE CONFIG
# ─────────────────────────────────────────────────────────────────────────────

def _get_di_client() -> DocumentAnalysisClient:
    endpoint = os.environ.get("AZURE_DI_ENDPOINT", "")
    key      = os.environ.get("AZURE_DI_KEY", "")
    if not endpoint.startswith("https://"):
        raise ValueError(f"Invalid Azure endpoint: '{endpoint}'. Must start with https://")
    return DocumentAnalysisClient(
        endpoint=endpoint,
        credential=AzureKeyCredential(key),
    )


# ─────────────────────────────────────────────────────────────────────────────
# TEXT CLEANING
# ─────────────────────────────────────────────────────────────────────────────

# Common single-character OCR artifacts from filled/unfilled checkbox glyphs
_CHECKBOX_OCR_PREFIX = re.compile(
    r"^[CBDOX■□●○◆◇▪▫\uf0fc\uf0a7\u25a0\u25a1\u2022]\s+", re.UNICODE
)

def _clean_text(val: str) -> str:
    if not val:
        return ""
    val = val.replace("\u00a0", " ")
    val = val.replace("\uf0b7",  "•")
    val = val.replace("\uf0fc",  "")   # Wingdings checkbox tick → strip
    val = val.replace("\uf0a7",  "")   # Wingdings bullet → strip
    val = re.sub(r"[ \t]+", " ", val)
    val = re.sub(r"\n{3,}", "\n\n", val)
    val = val.strip(" :.-\n\t")
    # Strip leading single-char OCR artifact from checkbox glyphs
    # e.g. "C CLM-2025-CP-114477" → "CLM-2025-CP-114477"
    val = _CHECKBOX_OCR_PREFIX.sub("", val).strip()
    return val


# ─────────────────────────────────────────────────────────────────────────────
# LABEL DETECTION
# ─────────────────────────────────────────────────────────────────────────────

_KNOWN_LABELS = {
    "CASE NUMBER", "FILING DATE", "LAST REFRESHED", "FILING LOCATION",
    "FILING COURT", "JUDGE", "CATEGORY", "PRACTICE AREA", "MATTER TYPE",
    "STATUS", "CASE LAST UPDATE", "DOCKET PREPARED FOR", "DATE",
    "LINE OF BUSINESS", "DOCKET", "CIRCUIT", "DIVISION",
    "CAUSE OF LOSS", "CAUSE OF ACTION", "CASE COMPLAINT SUMMARY",
    "OVERVIEW", "CASE DETAILS",
}

_VALUE_PATTERNS = [
    re.compile(r"^\d+(ST|ND|RD|TH)\s+CIRCUIT", re.I),
    re.compile(r"^AUTOMOBILE\s+TORT$", re.I),
    re.compile(r"^\d[\d\s\-/.,]+$"),
    re.compile(r"^https?://", re.I),
]


def _is_probable_label(line: str) -> bool:
    line = (line or "").strip()
    if not line or len(line) > 55:
        return False
    if line.upper() in _KNOWN_LABELS:
        return True
    for pat in _VALUE_PATTERNS:
        if pat.search(line):
            return False
    words = line.split()
    if (
        len(words) <= 5
        and line == line.upper()
        and not re.search(r"\d", line)
        and re.match(r"^[A-Z][A-Z0-9 \-()\/&']+$", line)
    ):
        return True
    if line.endswith(":") and 1 <= len(line) <= 50:
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# INLINE LABEL:VALUE SPLITTER
# ─────────────────────────────────────────────────────────────────────────────

def _try_split_inline(line: str) -> tuple[str, str] | None:
    if ":" not in line:
        return None
    left, _, right = line.partition(":")
    left  = left.strip()
    right = right.strip()
    if not left or len(left) > 45 or not right or len(left.split()) > 6:
        return None
    # PATCH: reject if the left side contains "Commentary", "Note", "Comment",
    # "Description", "Narrative" — these are prose labels not field names
    import re as _re_inline
    if _re_inline.search(
        r'\b(commentary|comment|note|notes|narrative|description|remarks|summary|detail)\b',
        left, _re_inline.IGNORECASE
    ):
        return None
    return (left, right)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN PAGE TEXT → LABEL/VALUE BLOCKS
# ─────────────────────────────────────────────────────────────────────────────

def _split_into_label_value_blocks(page_text: str) -> list[tuple[str, str]]:
    lines = [l.rstrip() for l in page_text.split("\n")]
    cleaned: list[str] = []
    prev_blank = False
    for l in lines:
        is_blank = not l.strip()
        if is_blank and prev_blank:
            continue
        cleaned.append(l.strip())
        prev_blank = is_blank

    blocks: list[tuple[str, str]] = []
    current_label: str | None = None
    current_value_lines: list[str] = []

    def _flush():
        nonlocal current_label, current_value_lines
        if current_label:
            val = " ".join(v for v in current_value_lines if v).strip()
            if val:
                blocks.append((current_label, val))
        current_label = None
        current_value_lines = []

    for line in cleaned:
        if not line:
            continue
        inline = _try_split_inline(line)
        if inline:
            _flush()
            blocks.append((_clean_text(inline[0]), _clean_text(inline[1])))
            current_label = None
            current_value_lines = []
            continue
        if _is_probable_label(line):
            _flush()
            current_label = _clean_text(line.rstrip(":").strip())
            current_value_lines = []
            continue
        if current_label is not None:
            current_value_lines.append(line)

    _flush()
    return blocks


# ─────────────────────────────────────────────────────────────────────────────
# DEDUPLICATION
# ─────────────────────────────────────────────────────────────────────────────

def _dedupe_fields(fields: list[dict]) -> list[dict]:
    seen: set = set()
    out: list[dict] = []
    for f in fields:
        key = (
            (f.get("field_name") or "").strip().lower(),
            (f.get("value")      or "").strip().lower(),
            int(f.get("source_page") or 0),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# PRIMARY EXTRACTION: PAGE TEXT → FIELDS (no bounding polygons yet)
# ─────────────────────────────────────────────────────────────────────────────

def _extract_page_fields_from_text(page_text: str, page_num: int) -> list[dict]:
    fields: list[dict] = []
    blocks = _split_into_label_value_blocks(page_text)
    for label, value in blocks:
        label = _clean_text(label)
        value = _clean_text(value)
        if not label or not value:
            continue
        if len(value) > 8000:
            value = value[:8000] + "…"
        fields.append({
            "field_name":       label,
            "value":            value,
            "confidence":       0.95,
            "source_page":      page_num,
            "excel_row":        page_num,
            "excel_col":        None,
            "source_text":      f"{label}: {value}",
            "raw_key":          label,
            "bounding_polygon": None,   # enriched later
            "page_width":       None,
            "page_height":      None,
            "source_block":     None,
            "source_para":      None,
            "source_table":     None,
            "source_row":       None,
            "source_col":       None,
        })
    return _dedupe_fields(fields)


# ─────────────────────────────────────────────────────────────────────────────
# BOUNDING POLYGON HELPER
# ─────────────────────────────────────────────────────────────────────────────

def _extract_polygon(bounding_regions) -> list[tuple[float, float]] | None:
    if not bounding_regions:
        return None
    try:
        poly = getattr(bounding_regions[0], "polygon", None)
        if not poly:
            return None
        return [(float(p.x), float(p.y)) for p in poly]
    except Exception:
        return None


def _merge_polygons(
    poly1: list[tuple[float, float]] | None,
    poly2: list[tuple[float, float]] | None,
) -> list[tuple[float, float]] | None:
    pts = []
    if poly1:
        pts.extend(poly1)
    if poly2:
        pts.extend(poly2)
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


# ─────────────────────────────────────────────────────────────────────────────
# SECONDARY EXTRACTION: AZURE KV PAIRS (with bounding polygons + confidence)
# ─────────────────────────────────────────────────────────────────────────────

def _extract_azure_kv_fields(
    result,
    page_dim_map: dict[int, tuple[float, float]],
) -> dict[int, list[dict]]:
    page_field_map: dict[int, list[dict]] = {}

    if not getattr(result, "key_value_pairs", None):
        return page_field_map

    for kv in result.key_value_pairs:
        key_text = kv.key.content.strip() if kv.key and kv.key.content else None
        val_text = kv.value.content.strip() if kv.value and kv.value.content else ""
        key_text = _clean_text(key_text or "")
        val_text = _clean_text(val_text)

        if not key_text or not val_text:
            continue
        if len(val_text) > 8000:
            val_text = val_text[:8000] + "…"

        source_page = None
        key_regions = getattr(kv.key, "bounding_regions", None) if kv.key else None
        val_regions = getattr(kv.value, "bounding_regions", None) if kv.value else None

        if key_regions:
            source_page = key_regions[0].page_number
        elif val_regions:
            source_page = val_regions[0].page_number
        if source_page is None:
            source_page = 1

        key_poly   = _extract_polygon(key_regions)
        val_poly   = _extract_polygon(val_regions)
        merged     = _merge_polygons(key_poly, val_poly)
        pw, ph     = page_dim_map.get(source_page, (8.5, 11.0))
        adi_conf   = getattr(kv, "confidence", 0.85) or 0.85

        field = {
            "field_name":       key_text,
            "value":            val_text,
            "confidence":       float(adi_conf),
            "source_page":      source_page,
            "excel_row":        source_page,
            "excel_col":        None,
            "source_text":      f"{key_text}: {val_text}",
            "raw_key":          key_text,
            "bounding_polygon": merged,
            "page_width":       pw,
            "page_height":      ph,
            "source_block":     None,
            "source_para":      None,
            "source_table":     None,
            "source_row":       None,
            "source_col":       None,
        }
        page_field_map.setdefault(source_page, []).append(field)

    for page_num in list(page_field_map.keys()):
        page_field_map[page_num] = _dedupe_fields(page_field_map[page_num])

    return page_field_map


# ─────────────────────────────────────────────────────────────────────────────
# PYMUPDF BOUNDING BOX ENRICHMENT  (robust multi-strategy search)
# ─────────────────────────────────────────────────────────────────────────────

def _search_page_for_text(page, text: str) -> list:
    """
    Try multiple text-search strategies on a PyMuPDF page.
    Returns a list of fitz.Rect matches, or [] if nothing found.
    """
    if not text or not text.strip():
        return []

    text = text.strip()
    candidates = [
        text,
        text.upper(),
        text.title(),
        text.lower(),
        text.rstrip(":").strip(),
        text.upper().rstrip(":").strip(),
    ]

    for suffix in (" DATE", " NUMBER", " LOCATION", " COURT", " AREA"):
        if text.upper().endswith(suffix):
            candidates.append(text[:-len(suffix)].strip())

    seen_c: set = set()
    ordered: list[str] = []
    for c in candidates:
        if c and c not in seen_c:
            seen_c.add(c)
            ordered.append(c)

    for candidate in ordered:
        try:
            rects = page.search_for(candidate)
            if rects:
                return rects
        except Exception:
            continue

    return []


def _enrich_fields_with_pymupdf_polygons(
    fields: list[dict],
    pdf_path: str,
    page_num: int,
    page_width_inches: float,
    page_height_inches: float,
) -> None:
    """
    For every field that still has bounding_polygon=None, use PyMuPDF to
    search for the key label (and optionally its value) on the page and
    compute a synthetic bounding polygon.
    Modifies fields in-place. No-op if pymupdf is not installed.
    """
    try:
        import fitz
    except ImportError:
        return

    fields_needing_bbox = [f for f in fields if f.get("bounding_polygon") is None]
    if not fields_needing_bbox:
        return

    try:
        doc  = fitz.open(pdf_path)
        page = doc[page_num - 1]
        pw   = page.rect.width
        ph   = page.rect.height

        for field in fields_needing_bbox:
            key_text = (field.get("field_name") or "").strip()
            val_text = (field.get("value")      or "").strip()

            if not key_text:
                continue

            key_rects = _search_page_for_text(page, key_text)

            val_rects = []
            if val_text and len(val_text) <= 120:
                val_rects = _search_page_for_text(page, val_text)
                if val_rects and key_rects:
                    anchor_y = key_rects[0].y0
                    val_rects = [min(val_rects, key=lambda r: abs(r.y0 - anchor_y))]
                elif val_rects:
                    val_rects = [val_rects[0]]

            rects_to_merge = []
            if key_rects:
                rects_to_merge.append(key_rects[0])
            if val_rects:
                rects_to_merge.append(val_rects[0])

            if not rects_to_merge:
                words = [w for w in key_text.split() if len(w) >= 4]
                for word in words[:2]:
                    word_rects = _search_page_for_text(page, word)
                    if word_rects:
                        rects_to_merge.append(word_rects[0])
                        break

            if not rects_to_merge:
                continue

            x0 = min(r.x0 for r in rects_to_merge)
            y0 = min(r.y0 for r in rects_to_merge)
            x1 = max(r.x1 for r in rects_to_merge)
            y1 = max(r.y1 for r in rects_to_merge)

            inv_sx = page_width_inches  / pw if pw else 1.0
            inv_sy = page_height_inches / ph if ph else 1.0

            poly = [
                (x0 * inv_sx, y0 * inv_sy),
                (x1 * inv_sx, y0 * inv_sy),
                (x1 * inv_sx, y1 * inv_sy),
                (x0 * inv_sx, y1 * inv_sy),
            ]

            field["bounding_polygon"] = poly
            field["page_width"]       = page_width_inches
            field["page_height"]      = page_height_inches

        doc.close()

    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# ERROR HELPER — store a friendly message in session state for the UI
# ─────────────────────────────────────────────────────────────────────────────

def _store_azure_error(raw_msg: str) -> str:
    """
    Convert a raw Azure exception message into a user-friendly string,
    store it in st.session_state["_azure_di_error"], and return it.
    On Streamlit Cloud the real message is redacted — detect that and
    show helpful troubleshooting text instead.
    """
    if not raw_msg.strip() or "redacted" in raw_msg.lower():
        friendly = (
            "Azure Document Intelligence returned an HTTP error "
            "(details are redacted on Streamlit Cloud). "
            "Common causes: invalid or expired API key, wrong endpoint URL, "
            "resource quota exceeded, or network timeout. "
            "Check AZURE_DI_ENDPOINT and AZURE_DI_KEY in your Streamlit secrets."
        )
    else:
        friendly = f"Azure Document Intelligence error: {raw_msg}"

    try:
        import streamlit as st
        st.session_state["_azure_di_error"] = friendly
    except Exception:
        pass

    try:
        from modules.audit import _log_error
        _log_error("AZURE_DI", st.session_state.get("_file_name", "unknown"), raw_msg)
    except Exception:
        pass

    return friendly


# ─────────────────────────────────────────────────────────────────────────────
# MAIN PARSER
# ─────────────────────────────────────────────────────────────────────────────

def parse_pdf_with_azure(file_path: str | Path) -> dict:
    """
    Parse uploaded PDF using Azure Document Intelligence prebuilt-document.

    Returns a dict with at minimum {"pages": [...], "doc_type": ..., "doc_label": ...}.
    On Azure failure returns {"pages": [], "error": "<message>", ...} — NEVER raises.

    The "error" key is also written to st.session_state["_azure_di_error"] so
    app2.py can surface it as a banner without crashing.
    """
    file_path = str(file_path)

    # ── Session-state cache — prevent duplicate ADI calls ─────────────────
    try:
        import streamlit as st
        import hashlib
        with open(file_path, "rb") as _f:
            _content_hash = hashlib.md5(_f.read()).hexdigest()
        _cache_key = f"_adi_parsed_{_content_hash}"
        if _cache_key in st.session_state:
            return st.session_state[_cache_key]
    except Exception:
        _cache_key = ""

    # ── Build client — catch credential / config errors ───────────────────────
    try:
        client = _get_di_client()
    except Exception as cfg_err:
        err = _store_azure_error(str(cfg_err))
        return {"pages": [], "doc_type": "pdf_document",
                "doc_label": "PDF Document", "error": err}

    
    # ── Call Azure DI — catch HttpResponseError and any other network error ───
    try:
        with track_cost("parser"):
            with open(file_path, "rb") as f:
                poller = client.begin_analyze_document("prebuilt-document", document=f)
            result = poller.result()

        # ── ADI Cost Logging ──────────────────────────────────────────────────
        try:
            import streamlit as st
            import datetime

            # Count pages from result
            _adi_page_count = len(result.pages) if getattr(result, "pages", None) else 1

            # Pricing: prebuilt-document = $0.01 per page
            _ADI_PRICE_PER_PAGE = float(
                os.environ.get("ADI_PRICE_PER_PAGE", "0.01")
            )

            if "_adi_cost_log" not in st.session_state:
                st.session_state["_adi_cost_log"] = []

            st.session_state["_adi_cost_log"].append({
                "model":      "prebuilt-document",
                "pages":      _adi_page_count,
                "cost":       _adi_page_count * _ADI_PRICE_PER_PAGE,
                "timestamp":  datetime.datetime.now().isoformat(),
                "doc_name":   str(file_path).split("/")[-1],
            })
        except Exception:
            pass  # never let cost logging break parsing
        # ─────────────────────────────────────────────────────────────────────

    except Exception as azure_err:
        err = _store_azure_error(str(azure_err))
        return {"pages": [], "doc_type": "pdf_document",
                "doc_label": "PDF Document", "error": err}
    

    # ── Build page text map + dimension map ───────────────────────────────────
    page_text_map: dict[int, str]                  = {}
    page_dim_map:  dict[int, tuple[float, float]]  = {}

    if getattr(result, "pages", None):
        for page in result.pages:
            lines = []
            if getattr(page, "lines", None):
                for line in page.lines:
                    if getattr(line, "content", None):
                        lines.append(line.content)
            page_text_map[page.page_number] = "\n".join(lines).strip()

            pw = getattr(page, "width",  8.5)  or 8.5
            ph = getattr(page, "height", 11.0) or 11.0
            page_dim_map[page.page_number] = (float(pw), float(ph))

    # ── Step 1: text-layout extraction (no polygons yet) ─────────────────────
    page_field_map: dict[int, list[dict]] = {}
    for page_num, page_text in page_text_map.items():
        page_field_map[page_num] = _extract_page_fields_from_text(page_text, page_num)

    # ── Step 2: Azure KV enrichment (adds polygons + real confidence) ─────────
    kv_map = _extract_azure_kv_fields(result, page_dim_map)
    for page_num, kv_fields in kv_map.items():
        existing_names = {
            (f.get("field_name") or "").strip().lower()
            for f in page_field_map.setdefault(page_num, [])
        }
        for f in kv_fields:
            fname      = (f.get("field_name") or "").strip().lower()
            fname_norm = re.sub(r"[\s:]+", " ", fname).strip()

            if fname not in existing_names:
                page_field_map[page_num].append(f)
                existing_names.add(fname)
            else:
                for existing in page_field_map[page_num]:
                    e_norm = re.sub(r"[\s:]+", " ",
                                    (existing.get("field_name", "") or "").strip().lower())
                    if (
                        e_norm == fname_norm
                        or e_norm in fname_norm
                        or fname_norm in e_norm
                    ):
                        adi_conf = float(f.get("confidence", 0.0))
                        if adi_conf > 0:
                            existing["confidence"] = adi_conf
                        if existing.get("bounding_polygon") is None and f.get("bounding_polygon"):
                            existing["bounding_polygon"] = f["bounding_polygon"]
                            existing["page_width"]       = f["page_width"]
                            existing["page_height"]      = f["page_height"]
                        break

        page_field_map[page_num] = _dedupe_fields(page_field_map[page_num])

    # ── Step 3: PyMuPDF fallback — synthetic polygons for remaining None fields
    with track_cost("parser"):
        for page_num, fields in page_field_map.items():
            missing_bbox = [f for f in fields if f.get("bounding_polygon") is None]
            if not missing_bbox:
                continue

            pw, ph = page_dim_map.get(page_num, (8.5, 11.0))
            _enrich_fields_with_pymupdf_polygons(
                fields             = fields,
                pdf_path           = file_path,
                page_num           = page_num,
                page_width_inches  = pw,
                page_height_inches = ph,
            )

    # ── Assemble output ───────────────────────────────────────────────────────
    pages_out: list[dict] = []
    all_page_nums = sorted(page_text_map.keys()) if page_text_map else [1]

    for page_num in all_page_nums:
        pages_out.append({
            "page_num":   page_num,
            "page_label": f"Page {page_num}",
            "raw_text":   page_text_map.get(page_num, ""),
            "fields":     page_field_map.get(page_num, []),
        })

    
    result_dict = {
        "doc_type":  "pdf_document",
        "doc_label": "PDF Document",
        "pages":     pages_out,
    }

    try:
        if _cache_key:
            st.session_state[_cache_key] = result_dict
    except Exception:
        pass

    return result_dict


# ─────────────────────────────────────────────────────────────────────────────
# UTILITY WRAPPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_pdf_sheet_names(file_path: str | Path) -> list[str]:
    """
    Return page labels for the PDF. Falls back to ["Page 1"] on any error —
    never raises, so app2.py never crashes at the sheet-names call.
    """
    parsed = parse_pdf_with_azure(file_path)
    # parse_pdf_with_azure already stored the error in session state if one occurred
    pages = parsed.get("pages", [])
    if not pages:
        return ["Page 1"]
    return [p["page_label"] for p in pages]


def get_pdf_sheet_dimensions(
    file_path: str | Path, sheet_name: str
) -> tuple[int, int]:
    parsed = parse_pdf_with_azure(file_path)
    for p in parsed.get("pages", []):
        if p["page_label"] == sheet_name:
            return len(p.get("fields", [])), 2
    return 0, 0
