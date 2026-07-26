"""
ui/field_row.py
Reusable helper that renders a single editable field row
(label | confidence | original | modified | eye | edit | history | checkbox).
"""
 
import datetime
import re
import streamlit as st
 
from modules.audit import _append_audit
from modules.field_history import _record_field_history
 
 
# ── Date validation ───────────────────────────────────────────────────────────
 
_DATE_FORMATS = [
    ("%m/%d/%Y", "MM/DD/YYYY"),
    ("%d/%m/%Y", "DD/MM/YYYY"),
    ("%Y-%m-%d", "YYYY-MM-DD"),
    ("%d-%m-%Y", "DD-MM-YYYY"),
    ("%m-%d-%Y", "MM-DD-YYYY"),
    ("%d.%m.%Y", "DD.MM.YYYY"),
    ("%Y/%m/%d", "YYYY/MM/DD"),
    ("%B %d, %Y", "Month DD, YYYY"),
    ("%b %d, %Y", "Mon DD, YYYY"),
]
 
_DATE_KEYWORDS = [
    "date", "dob", " dt", "dt_", "loss_dt", "incident",
    "reported", "opened", "closed", "settled", "received",
]
 
 
def _is_date_field(field_name: str) -> bool:
    fn = field_name.lower()
    return any(kw in fn for kw in _DATE_KEYWORDS)
 
 
def _validate_date(value: str) -> tuple[bool, str]:
    v = value.strip()
    if not v:
        return True, ""
    for fmt, label in _DATE_FORMATS:
        try:
            dt = datetime.datetime.strptime(v, fmt)
            if dt.year < 1900 or dt.year > 2100:
                return False, f"Year {dt.year} is out of range (1900–2100)."
            return True, ""
        except ValueError:
            continue
    formats_hint = "MM/DD/YYYY, YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY"
    return False, f"'{v}' is not a valid date. Use formats like {formats_hint}."
 
 
# ── Confidence colours ────────────────────────────────────────────────────────
 
def _conf_colors(conf: int, use_conf: bool, conf_thresh: int) -> tuple[str, str, str]:
    if not use_conf:
        return "var(--t3)", "var(--b0)", "var(--bg)"
    if conf < conf_thresh:
        return "var(--red)", "rgba(248,113,113,0.3)", "var(--red-g)"
    if conf < 75:
        return "var(--yellow)", "rgba(245,200,66,0.3)", "var(--yellow-g)"
    if conf < 88:
        return "var(--yellow)", "var(--b0)", "var(--bg)"
    return "var(--green)", "var(--b0)", "var(--bg)"
 
 
# ── Main render function ──────────────────────────────────────────────────────
 
# Claim ID field keywords — these are primary key fields that need protection
_CLAIM_ID_KEYWORDS = [
    "claim number", "claim id", "claim_id", "claim no", "claim#",
    "claim ref", "file number", "file no", "claimid", "clm id",
]

def _is_claim_id_field(field_name: str) -> bool:
    fn = field_name.lower().replace("_", " ").strip()
    return any(kw in fn for kw in _CLAIM_ID_KEYWORDS)
 
 
def render_field_row(
    *,
    schema_field: str,
    info: dict,
    mk: str,
    ek: str,
    xk: str,
    is_req: bool,
    conf: int,
    excel_f: str,
    is_title_sourced: bool,
    selected_sheet: str,
    curr_claim_id: str,
    active: dict,
    excel_path: str,
    uploaded_name: str,
    active_schema: str | None,
    use_conf: bool,
    conf_thresh: int,
    open_eye_popup,
    all_claim_ids: list | None = None,   # all existing claim IDs in the sheet
) -> None:
    conf_col, row_border, row_bg = _conf_colors(conf, use_conf, conf_thresh)
    is_claim_id = _is_claim_id_field(schema_field)
    all_claim_ids = all_claim_ids or []
 
    # ── State init ────────────────────────────────────────────────────────────
    if ek not in st.session_state:
        st.session_state[ek] = False
    if xk not in st.session_state:
        st.session_state[xk] = True
    if mk not in st.session_state:
        st.session_state[mk] = info.get("modified", info.get("value", ""))
 
    _cur_val = st.session_state.get(mk, info.get("modified", info.get("value", "")))
    _edited  = _cur_val != info.get("value", "")
    _dot     = "<span style='color:var(--yellow);font-size:8px;'>●</span> " if _edited else ""
 
    _badge_html = (
        "<span class='mandatory-asterisk' title='Mandatory'>*</span>"
        if is_req else "<span class='optional-badge'>OPT</span>"
    )
    _ink = "var(--t0)" if is_req else "var(--t1)"
 
    _field_label_html = (
        f"<div style='min-height:40px;display:flex;flex-direction:column;justify-content:center;"
        f"color:{_ink};font-size:var(--sz-body);font-weight:600;text-transform:uppercase;"
        f"letter-spacing:0.8px;font-family:var(--font-head);'>"
        f"<div style='display:flex;align-items:center;gap:3px;flex-wrap:wrap;line-height:1.6;'>"
        f"{_dot}{schema_field}{_badge_html}</div></div>"
    )
    _conf_html = (
        f"<div style='min-height:40px;display:flex;flex-direction:column;justify-content:center;gap:4px;'>"
        f"<span style='background:{conf_col}20;border:1px solid {conf_col};border-radius:20px;"
        f"padding:2px 10px;font-size:var(--sz-body);color:{conf_col};font-weight:600;"
        f"font-family:var(--mono);'>{conf}%</span>"
        f"<div style='background:var(--s1);border-radius:4px;height:4px;width:80%;'>"
        f"<div style='background:{conf_col};height:4px;border-radius:4px;width:{conf}%;'>"
        f"</div></div></div>"
    )
 
    st.markdown(
        f"<div style='border-left:2px solid {row_border};background:{row_bg};"
        f"border-radius:0 4px 4px 0;padding:2px 0 2px 4px;margin:1px 0;'></div>",
        unsafe_allow_html=True,
    )
 
    # ── Edit column ───────────────────────────────────────────────────────────
    def _save_field(nv, _display_val):
        """Shared save logic for both date and non-date fields."""
        err_key = f"err_{mk}"
        old_val = _display_val
        st.session_state[mk] = nv
        info["modified"] = nv
        if (
            not is_title_sourced
            and excel_f in active["data"][st.session_state.selected_idx]
        ):
            active["data"][st.session_state.selected_idx][excel_f]["modified"] = nv
        st.session_state[ek] = False
        st.session_state.pop(err_key, None)
        _record_field_history(selected_sheet, curr_claim_id, schema_field, old_val, nv)
        _append_audit({
            "event":     "FIELD_EDITED",
            "timestamp": datetime.datetime.now().isoformat(),
            "filename":  uploaded_name,
            "sheet":     selected_sheet,
            "claim_id":  curr_claim_id,
            "field":     schema_field,
            "original":  info.get("value", ""),
            "new_value": nv,
        })
        st.rerun()
 
    def _edit_col():
        _display_val = st.session_state.get(mk, info.get("modified", info.get("value", ""))) or ""
        err_key = f"err_{mk}"
 
        if st.session_state[ek]:
            with st.form(
                key=f"form_s_{selected_sheet}_{curr_claim_id}_{schema_field}",
                border=False,
            ):
                nv        = st.text_input("m", value=_display_val, label_visibility="collapsed")
                submitted = st.form_submit_button("", use_container_width=False)
                if submitted:
                    # Claim ID field — check for duplicates
                    if is_claim_id:
                        nv_stripped = nv.strip()
                        original_id = info.get("value", "").strip()
                        # Check if new value duplicates another existing claim
                        other_ids = [c for c in all_claim_ids if c != original_id]
                        if nv_stripped in other_ids:
                            st.session_state[err_key] = (
                                f"'{nv_stripped}' already exists in this sheet. "
                                f"Claim Number must be unique."
                            )
                        else:
                            _save_field(nv, _display_val)
                    elif _is_date_field(schema_field):
                        is_valid, err_msg = _validate_date(nv)
                        if not is_valid:
                            st.session_state[err_key] = err_msg
                        else:
                            _save_field(nv, _display_val)
                    else:
                        _save_field(nv, _display_val)
 
            err = st.session_state.get(err_key)
            if err:
                st.markdown(
                    f"<div style='color:#f87171;font-size:11px;padding:4px 6px;"
                    f"background:rgba(248,113,113,0.1);border-radius:4px;margin-top:2px;'>"
                    f"⚠ {err}</div>",
                    unsafe_allow_html=True,
                )
        else:
            st.session_state.pop(err_key, None)
            st.text_input(
                "m", value=_display_val,
                key=f"disp_{mk}", label_visibility="collapsed", disabled=True,
            )
 
    # ── Edit button ───────────────────────────────────────────────────────────
    def _edit_btn():
        if is_claim_id:
            # Claim Number is the primary key — show lock icon, allow edit with warning
            _lock_key = f"_claim_id_edit_warn_{mk}"
            if not st.session_state[ek]:
                if st.button(
                    "🔒",
                    key=f"ed_s_{selected_sheet}_{curr_claim_id}_{schema_field}",
                    use_container_width=False,
                    help="Claim Number is the primary key. Edit with caution.",
                ):
                    st.session_state[_lock_key] = True
                # ── FIXED: compact single button instead of vertical warning block ──
                if st.session_state.get(_lock_key):
                    if st.button(
                        "✏",
                        key=f"ed_s_confirm_{selected_sheet}_{curr_claim_id}_{schema_field}",
                        use_container_width=False,
                        help="⚠ Claim Number is the primary key. Editing may affect duplicate tracking. Duplicate values are not allowed.",
                    ):
                        st.session_state[_lock_key] = False
                        st.session_state[ek] = True
                        st.rerun()
            else:
                st.markdown(
                    "<div style='height:38px;display:flex;align-items:center;justify-content:center;"
                    "color:var(--yellow);font-size:11px;border:1px solid rgba(245,200,66,0.4);"
                    "border-radius:6px;'>↵</div>",
                    unsafe_allow_html=True,
                )
        elif not st.session_state[ek]:
            if st.button(
                "✏",
                key=f"ed_s_{selected_sheet}_{curr_claim_id}_{schema_field}",
                use_container_width=False,
                help="Edit field",
            ):
                st.session_state[ek] = True
                st.rerun()
        else:
            st.markdown(
                "<div style='height:38px;display:flex;align-items:center;justify-content:center;"
                "color:var(--green);font-size:11px;border:1px solid var(--b0);"
                "border-radius:6px;'>↵</div>",
                unsafe_allow_html=True,
            )
 
    # ── Layout ────────────────────────────────────────────────────────────────
    if use_conf:
        cl, cc, co, cm, ce, cb, cx = st.columns(
            [1.8, 1.4, 1.6, 1.8, 0.45, 0.45, 0.40], gap="small"
        )
        with cl: st.markdown(_field_label_html, unsafe_allow_html=True)
        with cc: st.markdown(_conf_html, unsafe_allow_html=True)
        with co:
            st.text_input(
                "o", value=info["value"],
                key=f"orig_{selected_sheet}_{curr_claim_id}_schema_{schema_field}",
                label_visibility="collapsed", disabled=True,
            )
        with cm: _edit_col()
        with ce:
            st.markdown("<div class='icon-btn-wrap'>", unsafe_allow_html=True)
            if st.button(
                "👁",
                key=f"eye_s_{selected_sheet}_{curr_claim_id}_{schema_field}",
                use_container_width=False,
            ):
                st.session_state["_eye_popup_request"] = {
                    "field": schema_field, "info": info,
                    "excel_path": excel_path, "sheet_name": selected_sheet,
                }
                st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)
        with cb:
            st.markdown("<div class='icon-btn-wrap'>", unsafe_allow_html=True)
            _edit_btn()
            st.markdown("</div>", unsafe_allow_html=True)
        with cx:
            st.markdown("<div style='height:8px;'></div>", unsafe_allow_html=True)
            st.checkbox("", key=xk, label_visibility="collapsed")
    else:
        cl, co, cm, ce, cb, cx = st.columns(
            [1.8, 1.8, 1.8, 0.45, 0.45, 0.40], gap="small"
        )
        with cl: st.markdown(_field_label_html, unsafe_allow_html=True)
        with co:
            st.text_input(
                "o", value=info["value"],
                key=f"orig_{selected_sheet}_{curr_claim_id}_schema_{schema_field}",
                label_visibility="collapsed", disabled=True,
            )
        with cm: _edit_col()
        with ce:
            st.markdown("<div class='icon-btn-wrap'>", unsafe_allow_html=True)
            if st.button(
                "👁",
                key=f"eye_s_{selected_sheet}_{curr_claim_id}_{schema_field}",
                use_container_width=False,
            ):
                open_eye_popup(schema_field, info, excel_path, selected_sheet)
            st.markdown("</div>", unsafe_allow_html=True)
        with cb:
            st.markdown("<div class='icon-btn-wrap'>", unsafe_allow_html=True)
            _edit_btn()
            st.markdown("</div>", unsafe_allow_html=True)
        with cx:
            st.markdown("<div style='height:8px;'></div>", unsafe_allow_html=True)
            st.checkbox("", key=xk, label_visibility="collapsed")
