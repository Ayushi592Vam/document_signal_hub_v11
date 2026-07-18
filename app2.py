"""
TPA Loss Run Parser — Main Entry Point
Orchestrates all modules and renders the Streamlit UI.
"""

import os
from dotenv import load_dotenv, find_dotenv
from modules.orchestrator import run_with_lineage

_app_dir  = os.path.dirname(os.path.abspath(__file__))
_env_path = os.path.join(_app_dir, ".env")
if os.path.exists(_env_path):
    load_dotenv(_env_path, override=True)
else:
    _found = find_dotenv(usecwd=True)
    if _found:
        load_dotenv(_found, override=True)

import streamlit as st

from config.settings import SESSION_DEFAULTS
from config.schemas import SCHEMAS, _CONFIG_LOAD_STATUS
from modules.audit import _append_audit
from modules.storage import (
    _load_hash_store, _save_hash_store,
    _compute_file_sha256, _compute_sheet_sha256,
    _load_from_feature_store, _save_to_feature_store,
)
from modules.file_utils import (
    get_sheet_names, get_sheet_dimensions,
    extract_merged_cell_metadata, extract_totals_row,
)
from modules.parsing import extract_from_excel, parse_txt_file
from modules.normalization import (
    auto_normalize_on_schema_activate,
    normalize_str,
    rename_columns_to_standard,
)
from modules.schema_mapping import (
    map_claim_to_schema, extract_title_fields,
    _has_unknown_fields, llm_map_unknown_fields,
    detect_claim_id,
)
from modules.enrichment import enrich_claim_cause_of_loss
from modules.dup_detection import _build_field_value_index
from modules.claim_dup_store import check_and_register_claims, get_claim_dup_result
from modules.export import (
    build_mapped_records_for_export,
    to_standard_json, to_guidewire_json, to_duck_creek_json,
    clean_duplicate_fields, _sanitize_for_json,
)
from modules.pdf_azure_parser import parse_pdf_with_azure
from modules.json_export_table import _append_json_export
from ui.styles import GLOBAL_CSS
from ui.topbar import render_topbar
from ui.file_card import render_file_card
from ui.sheet_card import render_sheet_card
from ui.nav_panel import render_nav_panel
from ui.claim_panel import render_claim_panel
from ui.export_panel import render_export_panel
from ui.claim_dup_panel import render_claim_dup_panel
from ui.dialogs import (
    show_settings_dialog,
    show_schema_fields_dialog,
    show_field_history_dialog,
    show_eye_popup,
    show_cache_manager_dialog,
    show_claim_journey_dialog,
)

import tempfile
import datetime
from modules.word_parser import parse_word
from modules.pdf_intelligence import run_pdf_intelligence


# ── PDF analysis panel — guarded import ──────────────────────────────────────
try:
    from ui.pdf_analysis import render_pdf_analysis_panel
    _PDF_PANEL_AVAILABLE = True
except ImportError as _pdf_panel_err:
    _PDF_PANEL_AVAILABLE = False
    _pdf_panel_err_msg   = str(_pdf_panel_err)


# ════════════════════════════════════════════════════════════════════════════
# PDF doc types that use the intelligence panel instead of claims panel
# FIX: "Loss Run" added — pdf_intelligence now handles all 4 document types
# ════════════════════════════════════════════════════════════════════════════
_PDF_INTELLIGENCE_TYPES = {"FNOL", "Legal", "Medical", "Loss Run", "Underwriting"}

PARSER_REGISTRY = {
    ".xlsx": {"module": "modules.parsing",         "function": "extract_from_excel",   "label": "Excel"},
    ".csv":  {"module": "modules.parsing",         "function": "extract_from_excel",   "label": "CSV"},
    ".pdf":  {"module": "modules.pdf_azure_parser","function": "parse_pdf_with_azure", "label": "PDF (Azure DI)"},
    ".docx": {"module": "modules.word_parser",     "function": "parse_word",           "label": "Word"},
    ".txt":  {"module": "modules.parsing",         "function": "parse_txt_file",       "label": "Text/Transcript"},
    ".html": {"module": "modules.html_parser",     "function": "parse_html_file",      "label": "HTML"},
    ".htm":  {"module": "modules.html_parser",     "function": "parse_html_file",      "label": "HTML"},
}
_SUPPORTED_UPLOAD_TYPES = sorted({ext.lstrip(".") for ext in PARSER_REGISTRY})

# ════════════════════════════════════════════════════════════════════════════
# Azure error banner helper
# ════════════════════════════════════════════════════════════════════════════

def _check_and_show_azure_error(stop_on_error: bool = True) -> bool:
    """
    Pop any Azure DI error stored by parse_pdf_with_azure() and show it as a
    Streamlit error banner. Returns True if an error was shown.
    If stop_on_error=True, calls st.stop() after showing the banner.
    """
    azure_err = st.session_state.pop("_azure_di_error", None)
    if not azure_err:
        return False

    st.error(
        f"**⚠️ Azure Document Intelligence Error**\n\n"
        f"{azure_err}\n\n"
        f"**Checklist:**\n"
        f"- `AZURE_DI_ENDPOINT` is set to your Form Recognizer endpoint "
        f"(e.g. `https://your-resource.cognitiveservices.azure.com/`)\n"
        f"- `AZURE_DI_KEY` is a valid, non-expired key\n"
        f"- The Azure resource has not exceeded its quota\n"
        f"- The uploaded file is a valid, non-corrupted PDF\n\n"
        f"Go to **Manage app → Secrets** on Streamlit Cloud to update credentials."
    )
    if stop_on_error:
        st.stop()
    return True


# ════════════════════════════════════════════════════════════════════════════
# Private helpers
# ════════════════════════════════════════════════════════════════════════════

def _extract_pdf_pages(pdf_path: str) -> list[str]:
    errors: list[str] = []
    try:
        from pypdf import PdfReader
        reader = PdfReader(pdf_path)
        pages  = [p.extract_text() or "" for p in reader.pages]
        if any(p.strip() for p in pages):
            return pages
    except ImportError:
        errors.append("pypdf not installed")
    except Exception as e:
        errors.append(f"pypdf: {e}")
    try:
        import PyPDF2
        with open(pdf_path, "rb") as fh:
            reader = PyPDF2.PdfReader(fh)
            pages  = [p.extract_text() or "" for p in reader.pages]
        if any(p.strip() for p in pages):
            return pages
    except ImportError:
        errors.append("PyPDF2 not installed")
    except Exception as e:
        errors.append(f"PyPDF2: {e}")
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            pages = [p.extract_text() or "" for p in pdf.pages]
        if any(p.strip() for p in pages):
            return pages
    except ImportError:
        errors.append("pdfplumber not installed")
    except Exception as e:
        errors.append(f"pdfplumber: {e}")
    try:
        from pdfminer.high_level import extract_pages
        page_texts = []
        for page_layout in extract_pages(pdf_path):
            buf = []
            for el in page_layout:
                if hasattr(el, "get_text"):
                    buf.append(el.get_text())
            page_texts.append("".join(buf))
        if any(p.strip() for p in page_texts):
            return page_texts
    except Exception as e:
        errors.append(f"pdfminer: {e}")
    if errors:
        st.warning(
            "PDF extraction failed:\n" + "\n".join(f"• {e}" for e in errors)
            + "\n\nFix: `pip install pypdf pdfplumber pdfminer.six`"
        )
    return []


def _word_fields_to_row(fields: list[dict]) -> dict:
    row = {}
    for f in fields:
        field_name = f.get("field_name", "").strip()
        if not field_name:
            continue
        val = str(f.get("value", "") or "").strip()
        row[field_name] = {
            "value": val, "modified": val,
            "confidence": float(f.get("confidence", 0.85)),  # was missing entirely
            "source_type":  "word",
            "source_block": f.get("source_block"),
            "source_para":  f.get("source_para"),
            "source_table": f.get("source_table"),
            "source_row":   f.get("source_row"),
            "source_col":   f.get("source_col"),
            "source_text":  f.get("source_text", ""),
            "excel_row": None, "excel_col": None,
        }
    from modules.canonical import to_canonical_fields
    return to_canonical_fields(row, source_type="word")


def _parse_pdf(file_path: str):
    """
    Parse a PDF via Azure Document Intelligence.
    Returns (data, sheet_type, None, azure_result).
    Never raises — errors are stored in session state by parse_pdf_with_azure().
    """
    from modules.pdf_azure_parser import parse_pdf_with_azure
    result = parse_pdf_with_azure(file_path)
    data   = []
    for page in result.get("pages", []):
        page_num = page.get("page_num", 1)
        row = {}
        for f in page.get("fields", []):
            field_name = (f.get("field_name") or "").strip()
            if not field_name:
                continue
            val = str(f.get("value", "") or "").strip()
            if not val:
                continue
            excel_row = f.get("excel_row") or page_num
            row[field_name] = {
                "value":            val,
                "modified":         val,
                "original":         val,
                "excel_row":        int(excel_row),
                "excel_col":        None,
                "source_type":      "pdf",
                "source_page":      int(f.get("source_page", page_num)),
                "source_text":      f.get("source_text", f"{field_name}: {val}"),
                "bounding_polygon": f.get("bounding_polygon"),
                "page_width":       f.get("page_width",  8.5),
                "page_height":      f.get("page_height", 11.0),
                "confidence":       float(f.get("confidence", 0.0)),
                "_pdf_raw":         True,
                "source_block":     None,
                "source_para":      None,
                "source_table":     None,
                "source_row":       None,
                "source_col":       None,
            }
        if row:
            data.append(row)
    return data, "PDF", None, result


def _doc_type_enum_to_label(doc_type_enum) -> str | None:
    """Convert a doc-type enum to a human-readable label."""
    if doc_type_enum is None:
        return None
    try:
        return doc_type_enum.value.replace("_", " ").title()
    except Exception:
        return str(doc_type_enum)


# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(layout="wide", page_title=" Document Signal Hub", page_icon="🛡️")
st.markdown(GLOBAL_CSS, unsafe_allow_html=True)


for _k, _v in SESSION_DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

if "_session_start" not in st.session_state:
    st.session_state["_session_start"] = datetime.datetime.utcnow().isoformat()

if "claim_dup_migrated_v2" not in st.session_state:
    try:
        from modules.claim_dup_store import _load_claim_dup_store, _save_claim_dup_store
        _dup_store = _load_claim_dup_store()
        _cleaned   = {
            cid: snap for cid, snap in _dup_store.items()
            if snap.get("fields") and
            sum(1 for v in snap["fields"].values() if str(v).strip()) /
            max(len(snap["fields"]), 1) >= 0.3
        }
        if len(_cleaned) != len(_dup_store):
            _save_claim_dup_store(_cleaned)
    except Exception:
        pass
    st.session_state["claim_dup_migrated_v2"] = True

if "focus_field" not in st.session_state:
    st.session_state.focus_field = None

_settings_clicked = render_topbar(SCHEMAS, _CONFIG_LOAD_STATUS)
if _settings_clicked:
    show_settings_dialog(SCHEMAS, _CONFIG_LOAD_STATUS)

if st.session_state.get("schema_popup_target"):
    _target = st.session_state["schema_popup_target"]
    st.session_state["schema_popup_target"] = None
    show_schema_fields_dialog(_target, SCHEMAS)

if st.session_state.get("_open_cache_manager"):
    st.session_state["_open_cache_manager"] = False
    show_cache_manager_dialog()

if st.session_state.get("_open_journey_dialog"):
    _jd = st.session_state["_open_journey_dialog"]
    show_claim_journey_dialog(
        claim_id=_jd["claim_id"],
        curr_claim=_jd["curr_claim"],
        selected_sheet=_jd["selected_sheet"],
        active_schema=_jd.get("active_schema"),
        _llm_map_result=_jd.get("_llm_map_result", {}),
    )

_, col_sheet_dropdown = st.columns([6.8, 1.2])

uploaded = st.file_uploader(
    "Upload Excel/CSV/PDF/TXT/DOCX/HTML",
    type=_SUPPORTED_UPLOAD_TYPES,
    accept_multiple_files=False,
    key="main_uploader",
)

if not uploaded:
    st.stop()

if "tmpdir" not in st.session_state:
    st.session_state.tmpdir = tempfile.mkdtemp()

file_ext   = os.path.splitext(uploaded.name)[1].lower()

if file_ext not in PARSER_REGISTRY:
    st.error(f"⚠ Unsupported file type: {file_ext}")
    st.stop()
    
excel_path = os.path.join(st.session_state.tmpdir, f"input{file_ext}")

_upload_fingerprint = f"{uploaded.name}_{uploaded.file_id}"
if st.session_state.get("last_uploaded") != _upload_fingerprint:
    _file_bytes = uploaded.getvalue()
    from modules.guardrails import validate_upload
    _ok, _reason = validate_upload(_file_bytes, uploaded.name, file_ext)
    if not _ok:
        st.error(f"⚠ Upload rejected: {_reason}")
        st.stop()
    with open(excel_path, "wb") as f:
        f.write(_file_bytes)
    st.session_state.last_uploaded = _upload_fingerprint
    st.session_state["_file_name"] = uploaded.name

    if file_ext == ".pdf":
        from modules.pdf_azure_parser import get_pdf_sheet_names
        st.session_state.sheet_names = run_with_lineage(
        "FILE_PARSED",
        uploaded.name,
        get_pdf_sheet_names,
        excel_path,
     )

        # ── Show Azure error banner and stop if the parse failed ──────────────
        # parse_pdf_with_azure() stores the error in session state; we surface
        # it here as a user-friendly banner rather than letting the app crash.
        _check_and_show_azure_error(stop_on_error=True)

        # Clear intelligence cache so it re-runs for the new file
        st.session_state.pop("_pdf_intelligence", None)
        st.session_state.pop("_pdf_intelligence_file", None)
        st.session_state.pop("_pdf_parsed_raw", None)
        # Clear all page-specific ADI lookup caches
        for _k in list(st.session_state.keys()):
            if _k.startswith("_adi_lookup"):
                st.session_state.pop(_k, None)
    elif file_ext == ".docx":
        st.session_state.sheet_names = ["Document"]

    # ADD this alongside your existing elif blocks:
    elif file_ext == ".txt":

        file_bytes = open(excel_path, "rb").read()
    
        parsed = run_with_lineage(
            "FILE_PARSED",
            uploaded.name,
            parse_txt_file,
            file_bytes,
            uploaded.name,
        )
    
        intelligence = run_with_lineage(
            "FILE_ENRICHED",
            uploaded.name,
            run_pdf_intelligence,
            parsed,
        )
    
        intelligence["source"] = "txt"
    
        st.session_state["_pdf_intelligence"] = intelligence
        st.session_state["_pdf_intelligence_file"] = excel_path
        st.session_state["sheet_names"] = ["Transcript"]

    elif file_ext in (".html", ".htm"):

        file_bytes = open(excel_path, "rb").read()
    
        from modules.html_parser import parse_html_file
    
        parsed = run_with_lineage(
            "FILE_PARSED",
            uploaded.name,
            parse_html_file,
            file_bytes,
            uploaded.name,
        )
    
        intelligence = run_with_lineage(
            "FILE_ENRICHED",
            uploaded.name,
            run_pdf_intelligence,
            parsed,
        )
    
        intelligence["source"] = "html"
    
        st.session_state["_pdf_intelligence"] = intelligence
        st.session_state["_pdf_intelligence_file"] = excel_path
        st.session_state["sheet_names"] = ["Document"]
    
    else:
       st.session_state.sheet_names = get_sheet_names(excel_path)
    
    st.session_state.sheet_cache  = {}
    st.session_state.selected_idx = 0
    st.session_state.focus_field  = None

    # Clear all per-file session keys
    for key in list(st.session_state.keys()):
        if (
            key.startswith("_rendered_") 
            or key.startswith("_claim_dup_results_") or key.startswith("mod_")
            or key.startswith("edit_") or key.startswith("_fv_")
            or key.startswith("_v_") or key.startswith("err_")
            or key.startswith("disp_") or key.startswith("_frozen_")
            or key.startswith("chk_") or key.startswith("_chk_")
            or key.startswith("_col_") or key.startswith("show_live_")
            or key.startswith("_std_json") or key.startswith("_schema_export")
            or key.startswith("user_added_") or key.startswith("_claim_id_edit_warn_")
            or key.startswith("_pdf_edit") or key.startswith("_pdf_edit_hist")
            or key == "_open_journey_dialog"
        ):
            del st.session_state[key]
            
    

    
    file_hash = _compute_file_sha256(excel_path)
    file_ext  = os.path.splitext(excel_path)[1].lower()

    # Reset LLM cost tracking ONLY for a genuinely new/different file
    # Same file re-upload keeps the existing cost log so it stays visible
    _prev_hash = st.session_state.get("_llm_cost_file_hash", "")
    if file_hash != _prev_hash:
        st.session_state["_llm_usage_log"] = []
        st.session_state["_llm_totals"] = {
            "prompt_tokens": 0, "output_tokens": 0,
            "total_tokens": 0, "cost_usd": 0.0, "calls": 0,
        }
        st.session_state["_llm_cost_file_hash"] = file_hash

    if file_ext == ".txt":
    # .txt files don't have sheets — use file-level hash as the sheet hash
        sheet_hashes = {
           st.session_state.sheet_names[0]: _compute_file_sha256(excel_path)
    }
    elif file_ext in [".docx", ".pdf", ".csv", ".html", ".htm"]:
        sheet_hashes = {
           st.session_state.sheet_names[0]: _compute_sheet_sha256(
            excel_path, st.session_state.sheet_names[0]
        )
    }
    else:
        sheet_hashes = {
            sn: _compute_sheet_sha256(excel_path, sn)
            for sn in st.session_state.sheet_names
        }
    st.session_state["current_file_hash"] = file_hash
    st.session_state["sheet_hashes"]      = sheet_hashes

    hash_store = _load_hash_store()
    if file_hash in hash_store:
        st.session_state["is_duplicate_file"]    = True
        st.session_state["duplicate_first_seen"] = hash_store[file_hash]["first_seen"]
        st.session_state["duplicate_orig_name"]  = hash_store[file_hash]["filename"]
    else:
        st.session_state["is_duplicate_file"]    = False
        st.session_state["duplicate_first_seen"] = None
        hash_store[file_hash] = {
            "filename":     uploaded.name,
            "first_seen":   datetime.datetime.now().isoformat(),
            "sheet_hashes": sheet_hashes,
        }
        _save_hash_store(hash_store)
        _append_audit({
            "event":     "FILE_INGESTED",
            "timestamp": datetime.datetime.now().isoformat(),
            "filename":  uploaded.name,
            "file_hash": file_hash,
            "sheets":    st.session_state.sheet_names,
        })
        from modules.document_metadata import record_metadata
        record_metadata(
            document_id=file_hash, filename=uploaded.name,
            parser_used=file_ext.lstrip("."), status="ingested",
            tags=[f"ext:{file_ext.lstrip('.')}"],
        )

    _sheet_hash_index = {}
    for _fh, _fdata in hash_store.items():
        if _fh == file_hash or not isinstance(_fdata, dict):
            continue
        for _sn, _sh in _fdata.get("sheet_hashes", {}).items():
            _sheet_hash_index[_sh] = {
                "filename":   _fdata.get("filename", "unknown"),
                "sheet_name": _sn,
                "first_seen": _fdata.get("first_seen", "unknown"),
                "file_hash":  _fh,
            }
    st.session_state["sheet_dup_info"] = {
        sn: _sheet_hash_index.get(sh) for sn, sh in sheet_hashes.items()
    }
else:
    file_hash    = st.session_state.get("current_file_hash", "")
    sheet_hashes = st.session_state.get("sheet_hashes", {})

is_dup         = st.session_state.get("is_duplicate_file", False)
sheet_dup_info = st.session_state.get("sheet_dup_info", {})

# ── Guard: ensure sheet_names is always initialized ──────────────────────────
if "sheet_names" not in st.session_state:
    st.session_state.sheet_names = get_sheet_names(excel_path)


render_file_card(
    uploaded, excel_path, file_hash, is_dup,
    sheet_dup_info, st.session_state.sheet_names,
)

from modules.document_metadata import get_metadata
_doc_meta = get_metadata(file_hash)
if _doc_meta:
    with st.expander(f"📜 Processing History — v{_doc_meta.get('version', 1)}"):
        for _evt in _doc_meta.get("status_history", []):
            st.markdown(f"- **{_evt.get('status','')}** via `{_evt.get('parser_used','')}` — {_evt.get('timestamp','')[:19].replace('T',' ')}")
        if _doc_meta.get("tags"):
            st.caption("Tags: " + ", ".join(_doc_meta["tags"]))

# with col_sheet_dropdown:
#     st.markdown("<div style='margin-top:20px;'></div>", unsafe_allow_html=True)
#     selected_sheet = st.selectbox(
#         "Sheet", st.session_state.sheet_names, index=0, label_visibility="collapsed"
#     )

with col_sheet_dropdown:
    if file_ext not in (".pdf", ".txt"):
        st.markdown("<div style='margin-top:20px;'></div>", unsafe_allow_html=True)
        selected_sheet = st.selectbox(
            "Sheet", st.session_state.sheet_names, index=0, label_visibility="collapsed"
        )
    else:
        selected_sheet = st.session_state.sheet_names[0]

st.markdown("<hr>", unsafe_allow_html=True)

# ── Sheet cache / parse ───────────────────────────────────────────────────────
sh_hash = sheet_hashes.get(selected_sheet, "")

if selected_sheet not in st.session_state.sheet_cache:
    _cached = _load_from_feature_store(sh_hash)

    if _cached:
        _records = _cached.get("records", {})
        _data = []
        for cid, rec in _records.items():
            row = {}
            for fld, fd in rec.items():
                if isinstance(fd, dict) and "value" in fd:
                    row[fld] = {
                        "value":            fd["value"],
                        "modified":         fd.get("modified", fd["value"]),
                        "excel_row":        fd.get("excel_row"),
                        "excel_col":        fd.get("excel_col"),
                        "original":         fd.get("original", fd["value"]),
                        "source_text":      fd.get("source_text", ""),
                        "source_type":      fd.get("source_type", ""),
                        "source_page":      fd.get("source_page"),
                        "_pdf_raw":         fd.get("_pdf_raw", False),
                        "bounding_polygon": fd.get("bounding_polygon"),
                        "page_width":       fd.get("page_width",  8.5),
                        "page_height":      fd.get("page_height", 11.0),
                        "confidence":       float(fd.get("confidence", 0.0)),
                    }
            if row:
                _data.append(row)
        if not _data:
            _cached = None

    if not _cached:
        with st.spinner(f"Reading '{selected_sheet}'…"):

            # ── PDF branch ────────────────────────────────────────────────────
            if file_ext == ".pdf":
                all_pages_data, sheet_type, _doc_type_enum, _azure_result = run_with_lineage(
                "FILE_PARSED", uploaded.name, _parse_pdf, excel_path
            )

                # Show Azure error banner if parse failed (non-fatal — keep going
                # with empty data so the rest of the UI doesn't crash)
                _check_and_show_azure_error(stop_on_error=False)

                # Store raw parsed result for page-wise entity display
                st.session_state["_pdf_parsed_raw"] = _azure_result

                try:
                    selected_page_num = int(selected_sheet.replace("Page", "").strip())
                except Exception:
                    selected_page_num = 1

                if 1 <= selected_page_num <= len(all_pages_data):
                    data = [all_pages_data[selected_page_num - 1]]
                else:
                    data = []

                merged_meta     = {}
                totals_data     = {}
                _col_rename_log = {}
                total_rows      = len(data)
                total_cols      = len(data[0]) if data else 0
                _title_flds     = {}
                if data and isinstance(data[0], dict):
                    for k, v in list(data[0].items())[:8]:
                        _title_flds[k] = {
                            "value":    v.get("value", "") if isinstance(v, dict) else str(v),
                            "modified": v.get("modified", v.get("value", "")) if isinstance(v, dict) else str(v),
                        }

                # ── Run PDF intelligence pipeline (only once per file) ─────────
                _intel_file_key = "_pdf_intelligence_file"
                _intel_key      = "_pdf_intelligence"
                if st.session_state.get(_intel_file_key) != excel_path:
                    with st.spinner("🧠 Running document analysis…"):
                        try:
                            from modules.pdf_intelligence import run_pdf_intelligence
                            from modules.pdf_azure_parser import parse_pdf_with_azure
                            _parsed_for_intel = run_with_lineage("FILE_PARSED", uploaded.name, parse_pdf_with_azure, excel_path)
                            _check_and_show_azure_error(stop_on_error=False)
                            _intel = run_with_lineage("FILE_ENRICHED", uploaded.name, run_pdf_intelligence, _parsed_for_intel)
                            st.session_state[_intel_key]      = _intel
                            st.session_state[_intel_file_key] = excel_path
                        except Exception as _e:
                            from modules.audit import _log_error
                            _log_error("PDF_INTELLIGENCE", uploaded.name, _e)
                            st.session_state[_intel_key]      = {}
                            st.session_state[_intel_file_key] = excel_path

            # ── DOCX branch ───────────────────────────────────────────────────
            elif file_ext == ".docx":
                word_result     = run_with_lineage("FILE_PARSED", uploaded.name, parse_word, excel_path, llm_client=None)
                parsed_rows     = [_word_fields_to_row(word_result.get("fields", []))]
                data            = parsed_rows
                sheet_type      = "WORD_DOCUMENT"
                merged_meta     = {}
                totals_data     = {}
                total_rows      = len(parsed_rows)
                total_cols      = len(parsed_rows[0]) if parsed_rows else 0
                _col_rename_log = {}
                _doc_type_enum  = None
                _title_flds     = {
                    f["field_name"]: {
                        "value":    f.get("value", ""),
                        "modified": f.get("value", "")
                    }
                    for f in word_result.get("fields", [])[:8]
                    if f.get("field_name")
                }
                if not selected_sheet:
                    selected_sheet = "Document"
                try:
                    sh_hash = _compute_sheet_sha256(excel_path, selected_sheet)
                except Exception:
                    sh_hash = None
                sheet_hashes = {selected_sheet: sh_hash}


            # ── TXT / Transcript branch ───────────────────────────────────────────────────
            elif file_ext == ".txt":
            # Reuse the already-parsed result stored during upload
                
                data            = []   # entities are served from _pdf_intelligence, not data rows
                sheet_type      = "TXT_TRANSCRIPT"
                merged_meta     = {}
                totals_data     = {}
                total_rows      = 0
                total_cols      = 0
                _col_rename_log = {}
                _doc_type_enum  = None
                _title_flds     = {}   

            # ── Excel / CSV branch ────────────────────────────────────────────
            else:
                _doc_type_enum = None
                _excel_result  = run_with_lineage("FILE_PARSED", uploaded.name, extract_from_excel, excel_path, selected_sheet)
                data           = _excel_result[0]
                sheet_type     = _excel_result[1]
                _title_kvs_raw = _excel_result[2] if len(_excel_result) > 2 else {}

                if not data:
                    st.warning(f"No data found in sheet '{selected_sheet}'.")
                    st.stop()

                merged_meta = extract_merged_cell_metadata(excel_path, selected_sheet)
                totals_data = extract_totals_row(excel_path, selected_sheet)
                total_rows, total_cols = get_sheet_dimensions(excel_path, selected_sheet)

                # PATCH 6A — cache merged cells for Transformation Journey
                st.session_state[f"_merged_meta_{selected_sheet}"] = merged_meta

                for row in data:
                    for fld, inf in row.items():
                        if "value" in inf and isinstance(inf["value"], str):
                            inf["value"] = normalize_str(inf["value"])
                        inf["modified"] = inf.get("value", "")

                _title_flds = extract_title_fields(merged_meta)
                if _title_kvs_raw:
                    for _tk, _tv in _title_kvs_raw.items():
                        if _tk not in _title_flds:
                            _title_flds[_tk] = _tv

                # PATCH 6B — cache title fields for Transformation Journey
                st.session_state[f"_title_fields_{selected_sheet}"] = _title_flds

                data, _col_rename_log = rename_columns_to_standard(data)

            st.session_state.sheet_cache[selected_sheet] = {
                "data":           data,
                "merged_meta":    merged_meta,
                "totals":         totals_data,
                "title_fields":   _title_flds,
                "sheet_type":     sheet_type,
                "total_rows":     total_rows,
                "total_cols":     total_cols,
                "sheet_hash":     sh_hash,
                "col_rename_log": _col_rename_log,
                "doc_type":       _doc_type_enum.value if _doc_type_enum else None,
                "doc_label":      _doc_type_enum_to_label(_doc_type_enum),
            }
            st.session_state.selected_idx = 0
            st.session_state.focus_field  = None

            _append_audit({
                "event":       "SHEET_PARSED",
                "timestamp":   datetime.datetime.now().isoformat(),
                "filename":    uploaded.name,
                "sheet":       selected_sheet,
                "sheet_hash":  sh_hash,
                "claim_rows":  len(data),
                "sheet_type":  sheet_type,
                "total_rows":  total_rows,
                "total_cols":  total_cols,
                "col_renames": _col_rename_log,
            })

            _std_for_store = {}
            for i, row in enumerate(data):
                cid = detect_claim_id(row, i)
                _std_for_store[cid] = {
                    fld: {**inf, "modified": inf.get("modified", inf.get("value", ""))}
                    for fld, inf in row.items()
                }
            _save_to_feature_store(sh_hash, selected_sheet, {
                "records":        _std_for_store,
                "sheet_type":     sheet_type,
                "total_rows":     total_rows,
                "total_cols":     total_cols,
                "sheet_name":     selected_sheet,
                "col_rename_log": _col_rename_log,
            })

    else:
        # ── Sheet loaded from feature store cache ─────────────────────────────
        data            = _data
        sheet_type      = _cached.get("sheet_type", "UNKNOWN")
        total_rows      = _cached.get("total_rows", 0)
        total_cols      = _cached.get("total_cols", 0)
        merged_meta     = {}
        totals_data     = {}
        _title_flds     = {}
        _col_rename_log = {}
        try:
            merged_meta = extract_merged_cell_metadata(excel_path, selected_sheet)
            totals_data = extract_totals_row(excel_path, selected_sheet)
            _title_flds = extract_title_fields(merged_meta)
            # PATCH 6C — cache for Transformation Journey (cache-load path)
            st.session_state[f"_merged_meta_{selected_sheet}"] = merged_meta
            st.session_state[f"_title_fields_{selected_sheet}"] = _title_flds
        except Exception:
            pass

        st.session_state.sheet_cache[selected_sheet] = {
            "data":           data,
            "merged_meta":    merged_meta,
            "totals":         totals_data,
            "title_fields":   _title_flds,
            "sheet_type":     sheet_type,
            "total_rows":     total_rows,
            "total_cols":     total_cols,
            "sheet_hash":     sh_hash,
            "col_rename_log": _col_rename_log,
            "_from_cache":    True,
            "doc_type":       None,
            "doc_label":      None,
        }
        st.session_state.selected_idx = 0
        st.session_state.focus_field  = None

        _append_audit({
            "event":      "SHEET_LOADED_FROM_CACHE",
            "timestamp":  datetime.datetime.now().isoformat(),
            "filename":   uploaded.name,
            "sheet":      selected_sheet,
            "sheet_hash": sh_hash,
        })

    _cur_schema = st.session_state.get("active_schema")
    if _cur_schema and _cur_schema in SCHEMAS:
        auto_normalize_on_schema_activate(
            st.session_state.sheet_cache[selected_sheet]["data"],
            _cur_schema, selected_sheet,
        )
        st.session_state.sheet_cache[selected_sheet]["_normalized_for"] = _cur_schema


# ── Active sheet context ──────────────────────────────────────────────────────
active       = st.session_state.sheet_cache[selected_sheet]
data         = active["data"]
merged_meta  = active.get("merged_meta", {})
totals_data  = active.get("totals", {})
title_fields = active.get("title_fields", {})
sheet_type   = active.get("sheet_type", "UNKNOWN")
total_rows   = active.get("total_rows", 0)
total_cols   = active.get("total_cols", 0)
sh_hash      = active.get("sheet_hash", "")
_from_cache  = active.get("_from_cache", False)


# ── Ensure PDF intelligence runs even when sheet data loaded from cache ───────
if file_ext == ".pdf" and st.session_state.get("_pdf_intelligence_file") != excel_path:
    with st.spinner("🧠 Running document analysis…"):
        try:
            from modules.pdf_intelligence import run_pdf_intelligence
            from modules.pdf_azure_parser import parse_pdf_with_azure
            _parsed_for_intel = run_with_lineage("FILE_PARSED", uploaded.name, parse_pdf_with_azure, excel_path)
            _check_and_show_azure_error(stop_on_error=False)
            _intel = run_with_lineage("FILE_ENRICHED", uploaded.name, run_pdf_intelligence, _parsed_for_intel)
            st.session_state["_pdf_intelligence"]      = _intel
            st.session_state["_pdf_intelligence_file"] = excel_path
        except Exception as _e:
            from modules.audit import _log_error
            _log_error("PDF_INTELLIGENCE", uploaded.name, _e)
            st.session_state["_pdf_intelligence"]      = {}
            st.session_state["_pdf_intelligence_file"] = excel_path


# ── Determine if this PDF needs the intelligence panel ────────────────────────
_intelligence    = st.session_state.get("_pdf_intelligence", {})
_pdf_doc_type    = _intelligence.get("doc_type", "") if file_ext in (".pdf", ".txt") else ""
_use_intel_panel = (
    (file_ext == ".pdf" and _pdf_doc_type in _PDF_INTELLIGENCE_TYPES)
    or file_ext in (".txt", ".html", ".htm")
)

if file_ext in (".pdf", ".txt", ".html", ".htm") and _intelligence:
    from modules.document_metadata import record_metadata
    record_metadata(
        document_id=file_hash, filename=uploaded.name,
        parser_used=file_ext.lstrip("."), status="classified",
        confidence_score=_intelligence.get("classification", {}).get("confidence"),
        tags=[f"doctype:{_intelligence.get('doc_type', 'unknown')}"],
    )


# ── Auto-normalize ────────────────────────────────────────────────────────────
_active_schema_now = st.session_state.get("active_schema")
_normalized_for    = active.get("_normalized_for")
if (
    _active_schema_now and _active_schema_now in SCHEMAS
    and _normalized_for != _active_schema_now
):
    auto_normalize_on_schema_activate(data, _active_schema_now, selected_sheet)
    active["_normalized_for"] = _active_schema_now


# ── LLM field-map (Excel/CSV only) ───────────────────────────────────────────
_llm_map_result = {}
_llm_map_ran    = False
_llm_map_count  = 0

if data and file_ext != ".pdf":
    _sample_keys = list(data[0].keys())
    _ref_schema  = (
        _active_schema_now
        if (_active_schema_now and _active_schema_now in SCHEMAS)
        else "Guidewire"
    )
    _needs_llm_map = _has_unknown_fields(_sample_keys, _ref_schema)
    if _needs_llm_map:
        _llm_map_result = llm_map_unknown_fields(data[:5], _ref_schema, selected_sheet)
        _llm_map_count  = len(_llm_map_result.get("mappings", {}))
        _llm_map_ran    = _llm_map_count > 0
        if _llm_map_ran:
            active["_llm_field_map"] = _llm_map_result
            _llm_mappings    = _llm_map_result.get("mappings", {})
            _already_renamed = active.get("_llm_renamed", False)
            if _llm_mappings and not _already_renamed:
                active["data"], _extra_renames = rename_columns_to_standard(
                    active["data"], llm_map=_llm_map_result
                )
                data = active["data"]
                active["_llm_renamed"] = True
    else:
        active.pop("_llm_field_map", None)

_llm_map_result = active.get("_llm_field_map", {})


# ── Dup indexes (skip for intel PDFs) ────────────────────────────────────────
_field_dup_index   = {}
_claim_dup_results = {}

if not _use_intel_panel:
    _field_dup_index_key = f"_fdi_{selected_sheet}"
    if _field_dup_index_key not in st.session_state:
        st.session_state[_field_dup_index_key] = _build_field_value_index(data, selected_sheet)
    _field_dup_index = st.session_state[_field_dup_index_key]

    _claim_dup_key = f"_claim_dup_results_{selected_sheet}"
    if _claim_dup_key not in st.session_state:
        st.session_state[_claim_dup_key] = check_and_register_claims(
            data=data,
            sheet_name=selected_sheet,
            filename=uploaded.name,
            detect_claim_id_fn=detect_claim_id,
        )
    _claim_dup_results = st.session_state[_claim_dup_key]


# ── Sheet card ────────────────────────────────────────────────────────────────
# render_sheet_card(
#     selected_sheet, sheet_type, sh_hash, len(data),
#     total_rows, total_cols, len(merged_meta), totals_data,
#     len(title_fields), _from_cache, sheet_dup_info,
#     title_kvs=title_fields,
# )
# if _llm_map_ran:
#     from ui.sheet_card import render_llm_map_banner
#     render_llm_map_banner(_llm_map_result, _llm_map_count)

# ── Sheet card ────────────────────────────────────────────────────────────────
if file_ext not in (".pdf", ".txt"):
    render_sheet_card(
        selected_sheet, sheet_type, sh_hash, len(data),
        total_rows, total_cols, len(merged_meta), totals_data,
        len(title_fields), _from_cache, sheet_dup_info,
        title_kvs=title_fields,
    )
    if _llm_map_ran:
        from ui.sheet_card import render_llm_map_banner
        render_llm_map_banner(_llm_map_result, _llm_map_count)


# ════════════════════════════════════════════════════════════════════════════
# ROUTING: PDF intelligence panel vs standard claims panel
# ════════════════════════════════════════════════════════════════════════════

if _use_intel_panel:
    # ── FNOL / Legal / Medical / Loss Run PDF ─────────────────────────────────
    if _PDF_PANEL_AVAILABLE:
        render_pdf_analysis_panel(
            intelligence   = _intelligence,
            uploaded_name  = uploaded.name,
            selected_sheet = selected_sheet,
        )
    else:
        st.error(
            f"❌ PDF analysis panel could not be loaded: `{_pdf_panel_err_msg}`\n\n"
            "**Fix:** ensure `ui/pdf_analysis.py` exists and that "
            "`ui/__init__.py` is present (can be empty), then restart Streamlit."
        )

else:
    # ── Loss Run PDF (non-intelligence) / Excel / CSV / DOCX ──────────────────
    if not data:
        st.warning("No data found for this sheet.")
        st.stop()

    curr_claim = data[st.session_state.selected_idx]

    _frozen_id_key = f"_frozen_claim_id_{selected_sheet}_{st.session_state.selected_idx}"
    if _frozen_id_key not in st.session_state:
        st.session_state[_frozen_id_key] = detect_claim_id(curr_claim)
    curr_claim_id = st.session_state[_frozen_id_key]

    if enrich_claim_cause_of_loss(curr_claim, curr_claim_id, selected_sheet):
        st.rerun()

    col_nav, col_main, col_fmt = st.columns([1.2, 3.2, 1.4], gap="large")

    with col_nav:
        new_idx = render_nav_panel(data=data, selected_sheet=selected_sheet)
        if new_idx is not None and new_idx != st.session_state.selected_idx:
            _old_frozen = f"_frozen_claim_id_{selected_sheet}_{new_idx}"
            if _old_frozen in st.session_state:
                del st.session_state[_old_frozen]
            st.session_state.selected_idx = new_idx
            st.session_state.focus_field  = None
            st.session_state.pop("_open_journey_dialog", None)
            st.rerun()

    with col_main:
        render_claim_panel(
            curr_claim=curr_claim,
            curr_claim_id=curr_claim_id,
            active=active,
            selected_sheet=selected_sheet,
            excel_path=excel_path,
            merged_meta=merged_meta,
            totals_data=totals_data,
            title_fields=title_fields,
            uploaded_name=uploaded.name,
            SCHEMAS=SCHEMAS,
            _llm_map_result=_llm_map_result,
            _field_dup_index=_field_dup_index,
            _claim_dup_results=_claim_dup_results,
        )

    with col_fmt:
        render_export_panel(
            data=data,
            curr_claim=curr_claim,
            curr_claim_id=curr_claim_id,
            selected_sheet=selected_sheet,
            sh_hash=sh_hash,
            file_hash=file_hash,
            uploaded_name=uploaded.name,
            SCHEMAS=SCHEMAS,
            merged_meta=merged_meta,
            totals_data=totals_data,
            title_fields=title_fields,
            _llm_map_result=_llm_map_result,
        )

hide_st_style = """
<style>
            #MainMenu {visibility: hidden;}
            footer {visibility: hidden;}
            header {visibility: hidden;}
            /* This target's the 'Manage app' area specifically in some versions */
            div[data-testid="stStatusWidget"] {visibility: hidden;} 
</style>
            """
st.markdown(hide_st_style, unsafe_allow_html=True)        
