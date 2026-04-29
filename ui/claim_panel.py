"""
ui/claim_panel.py
Middle column: claim header, schema/plain field rows, custom-field adder,
and sheet-totals section.

CHANGE: Sheet Totals panel is now rendered directly below the sheet title
banner (above the per-claim review section) so the overall sheet summary
is visible before the user drills into individual claim details.
"""

import datetime
import re


import streamlit as st

from modules.audit import _append_audit
from modules.field_history import _record_field_history
from modules.normalization import _best_standard_name, auto_normalize_field
from modules.schema_mapping import get_val, map_claim_to_schema
from ui.field_row import render_field_row, _is_date_field, _validate_date
from ui.dialogs import show_eye_popup
from ui.claim_dup_panel import render_claim_dup_panel


# ── Column-header HTML helper ─────────────────────────────────────────────────

def _col_hdr(label: str) -> str:
    return (
        f"<div style='font-size:var(--sz-xs);font-weight:700;color:var(--t1);"
        f"text-transform:uppercase;letter-spacing:1.4px;font-family:var(--font-head);"
        f"padding-bottom:5px;border-bottom:1px solid var(--b0);margin-bottom:5px;'>"
        f"{label}</div>"
    )


# ── Schema mode ───────────────────────────────────────────────────────────────

def _render_schema_mode(
    curr_claim, curr_claim_id, active, selected_sheet,
    excel_path, uploaded_name, SCHEMAS,
    _llm_map_result, _field_dup_index, _claim_dup_results,
    use_conf, conf_thresh, active_schema,
):
    schema_def    = SCHEMAS[active_schema]
    mapped        = map_claim_to_schema(curr_claim, active_schema, active.get("title_fields", {}), _llm_map_result)
    # Collect all existing claim IDs in the sheet for duplicate check
    from modules.schema_mapping import detect_claim_id as _detect_id
    _all_claim_ids = [_detect_id(row, i) for i, row in enumerate(active.get("data", []))]
    custom_flds   = st.session_state.get(f"custom_fields_{active_schema}", [])
    display_flds  = list(schema_def["required_fields"]) + [
        f for f in custom_flds if f not in schema_def["required_fields"]
    ]
    low_conf  = [sf for sf in display_flds if sf in mapped and mapped[sf]["confidence"] < conf_thresh and use_conf]
    missing   = [sf for sf in schema_def["required_fields"] if sf not in mapped]

    if missing:
        st.markdown(
            f"<div style='background:var(--red-g);border:1px solid rgba(248,113,113,0.3);"
            f"border-radius:6px;padding:8px 12px;margin-bottom:8px;font-size:var(--sz-body);"
            f"color:var(--red);font-family:var(--font);'>"
            f"⚠ {len(missing)} mandatory field(s) not mapped: {', '.join(missing)}</div>",
            unsafe_allow_html=True,
        )
    if low_conf:
        st.markdown(
            f"<div style='background:var(--yellow-g);border:1px solid rgba(245,200,66,0.3);"
            f"border-radius:6px;padding:8px 12px;margin-bottom:8px;font-size:var(--sz-body);"
            f"color:var(--yellow);font-family:var(--font);'>"
            f"⚡ {len(low_conf)} field(s) below threshold ({conf_thresh}%): {', '.join(low_conf)}</div>",
            unsafe_allow_html=True,
        )

    # ── Date order conflict warnings ─────────────────────────────────────────
    _date_warnings_shown = set()
    for _sf, _mdata in mapped.items():
        _warn = _mdata.get("date_order_warning")
        if _warn and _warn not in _date_warnings_shown:
            st.markdown(
                f"<div style='background:rgba(248,113,113,0.08);border:1px solid rgba(248,113,113,0.35);"
                f"border-left:4px solid #f87171;border-radius:7px;padding:10px 14px;margin-bottom:8px;'>"
                f"<div style='font-size:11px;font-weight:700;color:#f87171;font-family:monospace;"
                f"text-transform:uppercase;letter-spacing:1px;margin-bottom:4px;'>⚠ Date Order Conflict</div>"
                f"<div style='font-size:12px;color:#e8e7ff;'>{_warn}</div>"
                f"</div>",
                unsafe_allow_html=True,
            )
            _date_warnings_shown.add(_warn)

    # Header row
    if use_conf:
        hc = st.columns([1.8, 1.4, 1.6, 1.8, 0.45, 0.45, 0.45, 0.40], gap="small")
        for ci, lbl in enumerate(["Schema Field", "Confidence", "Extracted", "Modified"]):
            with hc[ci]: st.markdown(_col_hdr(lbl), unsafe_allow_html=True)
    else:
        hc = st.columns([1.8, 1.8, 1.8, 0.45, 0.45, 0.45, 0.40], gap="small")
        for ci, lbl in enumerate(["Schema Field", "Extracted", "Modified"]):
            with hc[ci]: st.markdown(_col_hdr(lbl), unsafe_allow_html=True)

    for schema_field in display_flds:
        if schema_field not in mapped:
            is_req = schema_field in schema_def["required_fields"]
            _bg, _br, _fc, _lbl = (
                ("var(--red-g)",  "rgba(248,113,113,0.3)", "var(--red)",  "MANDATORY · NOT FOUND")
                if is_req else
                ("var(--s0)",     "var(--b0)",              "var(--t3)", "OPTIONAL · NOT IN SHEET")
            )
            st.markdown(
                f"<div style='display:flex;align-items:center;gap:8px;background:{_bg};"
                f"border:1px solid {_br};border-radius:6px;padding:6px 10px;margin:2px 0;'>"
                f"<span style='color:{_fc};font-size:var(--sz-sm);font-weight:700;"
                f"text-transform:uppercase;font-family:var(--font);'>{schema_field}</span>"
                f"<span style='background:var(--s1);color:{_fc};font-size:9px;border-radius:4px;"
                f"padding:1px 5px;border:1px solid {_br};font-family:var(--mono);'>{_lbl}</span></div>",
                unsafe_allow_html=True,
            )
            continue

        m             = mapped[schema_field]
        conf          = m["confidence"]
        excel_f       = m["excel_field"]
        info          = m["info"]
        is_req        = m["is_required"]
        is_title_src  = m.get("from_title", False)

        ek = f"edit_{selected_sheet}_{curr_claim_id}_schema_{schema_field}"
        mk = f"mod_{selected_sheet}_{curr_claim_id}_schema_{schema_field}"
        xk = f"chk_{selected_sheet}_{curr_claim_id}_{schema_field}"

        render_field_row(
            schema_field=schema_field, info=info,
            mk=mk, ek=ek, xk=xk,
            is_req=is_req, conf=conf, excel_f=excel_f,
            is_title_sourced=is_title_src,
            selected_sheet=selected_sheet, curr_claim_id=curr_claim_id,
            active=active, excel_path=excel_path, uploaded_name=uploaded_name,
            active_schema=active_schema,
            use_conf=use_conf, conf_thresh=conf_thresh,
            open_eye_popup=show_eye_popup,
            all_claim_ids=_all_claim_ids,
        )


# ── Plain mode ────────────────────────────────────────────────────────────────

def _render_plain_mode(
    curr_claim, curr_claim_id, active, selected_sheet,
    excel_path, uploaded_name,
    _llm_map_result, _field_dup_index, _claim_dup_results,
    use_conf, conf_thresh,
):
    # Fields that should never appear in a TPA loss run context
    # These are PII fields or irrelevant fields that may appear in source data
    _EXCLUDED_FIELD_KEYWORDS = {
        "date of birth", "dob", "birth date", "birthdate", "date_of_birth",
        "social security", "ssn", "tax id", "ein", "passport",
        "driver license", "drivers license", "license number",
        "bank account", "routing number", "credit card",
        "personal email", "home address", "home phone",
        "gender", "race", "ethnicity", "marital status", "religion",
        "salary", "wage rate", "hourly rate",
    }

    def _should_exclude_field(field_name: str) -> bool:
        fn = field_name.lower().replace("_", " ").strip()
        return any(kw in fn for kw in _EXCLUDED_FIELD_KEYWORDS)

    _plain_col_rename = active.get("col_rename_log", {})
    _llm_plain_map    = {}
    if not _plain_col_rename and active.get("data"):
        from modules.schema_mapping import _has_unknown_fields, llm_map_unknown_fields
        from modules.llm import _llm_available
        _file_hash = st.session_state.get("current_file_hash", "")
        _llm_plain_cache_key = f"_llm_fieldmap_{_file_hash}_{selected_sheet}_plain"
        if _llm_available():
            _llm_plain_result = st.session_state.get(_llm_plain_cache_key)
            if _llm_plain_result is None:
                if _has_unknown_fields(list(active["data"][0].keys()), "Guidewire"):
                    _llm_plain_result = llm_map_unknown_fields(
                        active["data"][:5], "Guidewire", selected_sheet + "_plain"
                    )
                    st.session_state[_llm_plain_cache_key] = _llm_plain_result or {}
                else:
                    st.session_state[_llm_plain_cache_key] = {}
            _llm_plain_map = (st.session_state.get(_llm_plain_cache_key) or {}).get("mappings", {})

    def _display_field_name(raw_col: str) -> tuple[str, bool, bool]:
        if raw_col in _plain_col_rename:
            return _plain_col_rename[raw_col], True, False
        if raw_col in _llm_plain_map:
            return _llm_plain_map[raw_col], False, True
        std = _best_standard_name(raw_col)
        if std:
            return std, True, False
        return raw_col, False, False

    from modules.schema_mapping import _value_quality_score

    def _plain_conf(field_display: str, val: str) -> int:
        if not val:
            return 0
        return round(_value_quality_score(val, field_display) * 100)

    # Headers
    if use_conf:
        hc = st.columns([1.8, 1.2, 1.8, 1.8, 0.5, 0.5, 0.5, 0.4])
        for ci, lbl in enumerate(["Field", "Conf", "Extracted", "Modified"]):
            with hc[ci]: st.markdown(_col_hdr(lbl), unsafe_allow_html=True)
    else:
        hc = st.columns([2, 2.4, 2.4, 0.5, 0.5, 0.5, 0.5])
        for ci, lbl in enumerate(["Field", "Extracted Value", "Modified Value"]):
            with hc[ci]: st.markdown(_col_hdr(lbl), unsafe_allow_html=True)

    for field, info in curr_claim.items():
        # Skip fields that are irrelevant or contain PII not needed for loss runs
        if _should_exclude_field(field):
            continue

        ek = f"edit_{selected_sheet}_{curr_claim_id}_{field}"
        xk = f"chk_{selected_sheet}_{curr_claim_id}_{field}"
        mk = f"mod_{selected_sheet}_{curr_claim_id}_{field}"
        from modules.schema_mapping import detect_claim_id as _did
        _all_ids_plain = [_did(row, i) for i, row in enumerate(active.get("data", []))]
        if ek not in st.session_state: st.session_state[ek] = False
        if mk not in st.session_state: st.session_state[mk] = info.get("value", "")
        if xk not in st.session_state: st.session_state[xk] = True

        _cur_val_p = st.session_state.get(mk, info.get("value", "")) or ""
        _dot_p = "<span style='color:var(--yellow);margin-left:4px;font-size:8px;'>●</span>" if _cur_val_p != info["value"] else ""

        disp_name, _was_rule, _was_llm = _display_field_name(field)

        # ── Field label: show STD badge for rule-renamed fields only.
        # LLM-mapped fields show their display name + raw subtitle — no badge.
        _renamed_badge  = ""
        _orig_raw_title = ""

        if _was_rule and disp_name != field:
            # Standardised by rule — show subtle STD badge
            _renamed_badge = (
                f"<span style='font-size:9px;background:rgba(52,211,153,0.12);"
                f"border:1px solid rgba(52,211,153,0.3);border-radius:3px;color:#34d399;"
                f"padding:0 4px;margin-left:3px;font-family:monospace;' "
                f"title='Standardised from: {field}'>STD</span>"
            )
            _orig_raw_title = field
        elif _was_llm and disp_name != field:
            # LLM-mapped — show raw source name as subtitle only, no badge
            _orig_raw_title = field

        _plain_field_label = (
            f"<div style='min-height:40px;display:flex;flex-direction:column;justify-content:center;'>"
            f"<div style='color:var(--t0);font-size:var(--sz-body);font-weight:600;"
            f"text-transform:uppercase;letter-spacing:0.8px;font-family:var(--font-head);"
            f"display:flex;align-items:center;flex-wrap:wrap;gap:2px;'>"
            f"{disp_name}{_renamed_badge}{_dot_p}</div>"
            + (
                f"<div style='font-size:9px;color:var(--t4);font-family:monospace;"
                f"margin-top:1px;'>raw: {_orig_raw_title}</div>"
                if _orig_raw_title else ""
            )
            + "</div>"
        )

        _pconf     = _plain_conf(disp_name, _cur_val_p) if use_conf else 0
        _pconf_col = (
            "var(--green)" if _pconf >= 80 else "var(--yellow)" if _pconf >= 50 else "var(--red)"
        )
        _pconf_html = (
            f"<div style='min-height:40px;display:flex;flex-direction:column;"
            f"justify-content:center;gap:4px;'>"
            f"<span style='background:{_pconf_col}20;border:1px solid {_pconf_col};"
            f"border-radius:20px;padding:2px 8px;font-size:11px;color:{_pconf_col};"
            f"font-weight:600;font-family:monospace;'>{_pconf}%</span>"
            f"<div style='background:var(--s1);border-radius:4px;height:4px;width:80%;'>"
            f"<div style='background:{_pconf_col};height:4px;border-radius:4px;width:{_pconf}%;'>"
            f"</div></div></div>"
        ) if use_conf else ""

        def _plain_edit_col(_field=field, _mk=mk, _ek=ek):
            _plain_display_val = st.session_state.get(
                _mk, info.get("modified", info.get("value", ""))
            ) or ""
            _err_key = f"err_{_mk}"
            if st.session_state[_ek]:
                with st.form(
                    key=f"form_{selected_sheet}_{curr_claim_id}_{_field}", border=False
                ):
                    nv        = st.text_input("m", value=_plain_display_val, label_visibility="collapsed")
                    submitted = st.form_submit_button("", use_container_width=False)
                    if submitted:
                        from ui.field_row import _is_claim_id_field as _is_cid2
                        if _is_cid2(_field):
                            _nv_s = nv.strip()
                            _orig_id = info.get("value", "").strip()
                            _other_ids = [c for c in _all_ids_plain if c != _orig_id]
                            if _nv_s in _other_ids:
                                st.session_state[_err_key] = f"'{_nv_s}' already exists. Claim Number must be unique."
                            else:
                                st.session_state.pop(_err_key, None)
                                old_val = _plain_display_val
                                st.session_state[_mk] = nv
                                active["data"][st.session_state.selected_idx][_field]["modified"] = nv
                                st.session_state[_ek] = False
                                _record_field_history(selected_sheet, curr_claim_id, _field, old_val, nv)
                                _append_audit({"event":"FIELD_EDITED","timestamp":datetime.datetime.now().isoformat(),"filename":uploaded_name,"sheet":selected_sheet,"claim_id":curr_claim_id,"field":_field,"original":info["value"],"new_value":nv})
                                st.rerun()
                        elif _is_date_field(_field):
                            is_valid, err_msg = _validate_date(nv)
                            if not is_valid:
                                st.session_state[_err_key] = err_msg
                            else:
                                st.session_state.pop(_err_key, None)
                                old_val = _plain_display_val
                                st.session_state[_mk] = nv
                                active["data"][st.session_state.selected_idx][_field]["modified"] = nv
                                st.session_state[_ek] = False
                                _record_field_history(selected_sheet, curr_claim_id, _field, old_val, nv)
                                _append_audit({
                                    "event":     "FIELD_EDITED",
                                    "timestamp": datetime.datetime.now().isoformat(),
                                    "filename":  uploaded_name,
                                    "sheet":     selected_sheet,
                                    "claim_id":  curr_claim_id,
                                    "field":     _field,
                                    "original":  info["value"],
                                    "new_value": nv,
                                })
                                st.rerun()
                        else:
                            st.session_state.pop(_err_key, None)
                            old_val = _plain_display_val
                            st.session_state[_mk] = nv
                            active["data"][st.session_state.selected_idx][_field]["modified"] = nv
                            st.session_state[_ek] = False
                            _record_field_history(selected_sheet, curr_claim_id, _field, old_val, nv)
                            _append_audit({
                                "event":     "FIELD_EDITED",
                                "timestamp": datetime.datetime.now().isoformat(),
                                "filename":  uploaded_name,
                                "sheet":     selected_sheet,
                                "claim_id":  curr_claim_id,
                                "field":     _field,
                                "original":  info["value"],
                                "new_value": nv,
                            })
                            st.rerun()
                err = st.session_state.get(_err_key)
                if err:
                    st.markdown(
                        f"<div style='color:#f87171;font-size:11px;padding:4px 6px;"
                        f"background:rgba(248,113,113,0.1);border-radius:4px;margin-top:2px;'>"
                        f"⚠ {err}</div>",
                        unsafe_allow_html=True,
                    )
            else:
                st.session_state.pop(_err_key, None)
                st.text_input(
                    "m", value=_plain_display_val,
                    key=f"disp_plain_{_mk}", label_visibility="collapsed", disabled=True,
                )
        if use_conf:
            cl, cc, co, cm, ce, cb, cx = st.columns([1.8, 1.2, 1.8, 1.8, 0.5, 0.5, 0.4], gap="small")
            with cl: st.markdown(_plain_field_label, unsafe_allow_html=True)
            with cc: st.markdown(_pconf_html, unsafe_allow_html=True)
            with co:
                st.text_input(
                    "o", value=info["value"],
                    key=f"orig_{selected_sheet}_{curr_claim_id}_{field}",
                    label_visibility="collapsed", disabled=True,
                )
            with cm: _plain_edit_col()
            with ce:
                if st.button("👁", key=f"eye_{selected_sheet}_{curr_claim_id}_{field}", use_container_width=True):
                    show_eye_popup(field, info, excel_path, selected_sheet)
            with cb:
                from ui.field_row import _is_claim_id_field as _is_cid
                if _is_cid(field):
                    _lk = f"_claim_id_edit_warn_{mk}"
                    if not st.session_state[ek]:
                        if st.button("🔒", key=f"ed_{selected_sheet}_{curr_claim_id}_{field}_v{st.session_state.get(f'_v_{mk}',0)}", use_container_width=True, help="Primary key — edit with caution"):
                            st.session_state[_lk] = True
                        if st.session_state.get(_lk):
                            st.markdown("<div style='background:rgba(245,200,66,0.1);border:1px solid rgba(245,200,66,0.4);border-radius:6px;padding:6px 8px;font-size:10px;color:#f5c842;'>⚠ Primary key.<br>Duplicates not allowed.</div>", unsafe_allow_html=True)
                            if st.button("Proceed", key=f"ed_confirm_{selected_sheet}_{curr_claim_id}_{field}", use_container_width=True):
                                st.session_state[_lk] = False
                                st.session_state[ek] = True
                                st.rerun()
                    else:
                        st.markdown("<div style='height:38px;display:flex;align-items:center;justify-content:center;color:var(--yellow);font-size:11px;border:1px solid rgba(245,200,66,0.4);border-radius:6px;'>↵</div>", unsafe_allow_html=True)
                elif not st.session_state[ek]:
                    if st.button("✏", key=f"ed_{selected_sheet}_{curr_claim_id}_{field}_v{st.session_state.get(f'_v_{mk}',0)}", use_container_width=True):
                        st.session_state[ek] = True
                        st.rerun()
                else:
                    st.markdown("<div style='height:38px;display:flex;align-items:center;justify-content:center;color:var(--green);font-size:11px;border:1px solid var(--b0);border-radius:6px;'>↵</div>", unsafe_allow_html=True)
            with cx:
                st.markdown("<div style='height:8px;'></div>", unsafe_allow_html=True)
                st.checkbox("", key=xk, label_visibility="collapsed")
        else:
            cl, co, cm, ce, cb, cx = st.columns([2, 2.4, 2.4, 0.5, 0.5, 0.5], gap="small")
            with cl: st.markdown(_plain_field_label, unsafe_allow_html=True)
            with co:
                st.text_input(
                    "o", value=info["value"],
                    key=f"orig_{selected_sheet}_{curr_claim_id}_{field}",
                    label_visibility="collapsed", disabled=True,
                )
            with cm: _plain_edit_col()
            with ce:
                if st.button("👁", key=f"eye_{selected_sheet}_{curr_claim_id}_{field}", use_container_width=True):
                    show_eye_popup(field, info, excel_path, selected_sheet)
            with cb:
                if not st.session_state[ek]:
                    if st.button("✏", key=f"ed_{selected_sheet}_{curr_claim_id}_{field}_v{st.session_state.get(f'_v_{mk}',0)}", use_container_width=True):
                        st.session_state[ek] = True
                        st.rerun()
                else:
                    st.markdown("<div style='height:38px;display:flex;align-items:center;justify-content:center;color:var(--green);font-size:11px;border:1px solid var(--b0);border-radius:6px;'>↵</div>", unsafe_allow_html=True)
            with cx:
                st.markdown("<div style='height:8px;'></div>", unsafe_allow_html=True)
                st.checkbox("", key=xk, label_visibility="collapsed")


# ── Custom field adder ────────────────────────────────────────────────────────

def _render_custom_field_adder(curr_claim_id, selected_sheet, uploaded_name):
    _user_fields_key = f"user_added_fields_{selected_sheet}_{curr_claim_id}"
    _add_counter_key = f"add_field_counter_{selected_sheet}_{curr_claim_id}"
    if _user_fields_key not in st.session_state: st.session_state[_user_fields_key] = []
    if _add_counter_key not in st.session_state: st.session_state[_add_counter_key] = 0
    _ctr = st.session_state[_add_counter_key]

    st.markdown("<hr>", unsafe_allow_html=True)
    st.markdown(
        "<div style='display:flex;align-items:center;gap:10px;margin-bottom:10px;'>"
        "<span style='font-size:var(--sz-xs);font-weight:600;color:var(--purple);"
        "text-transform:uppercase;letter-spacing:2px;font-family:var(--mono);'>+ Add Custom Field</span>"
        "<span style='flex:1;height:1px;background:linear-gradient(90deg,"
        "rgba(167,139,250,0.4),transparent);'></span></div>",
        unsafe_allow_html=True,
    )

    _user_fields = st.session_state[_user_fields_key]
    if _user_fields:
        uc1, uc2, uc3, uc4 = st.columns([2, 3, 0.6, 0.6])
        with uc1:
            st.markdown(
                "<div style='font-size:var(--sz-xs);font-weight:600;color:var(--t3);"
                "text-transform:uppercase;letter-spacing:1.6px;font-family:var(--mono);"
                "padding-bottom:5px;border-bottom:1px solid var(--b0);margin-bottom:6px;'>"
                "Custom Field</div>",
                unsafe_allow_html=True,
            )
        with uc2:
            st.markdown(
                "<div style='font-size:var(--sz-xs);font-weight:600;color:var(--t3);"
                "text-transform:uppercase;letter-spacing:1.6px;font-family:var(--mono);"
                "padding-bottom:5px;border-bottom:1px solid var(--b0);margin-bottom:6px;'>"
                "Value</div>",
                unsafe_allow_html=True,
            )

        for uf_idx, uf in enumerate(_user_fields):
            uf_name = uf["name"]
            uf_mk   = f"uf_mod_{selected_sheet}_{curr_claim_id}_{uf_name}_{uf_idx}"
            uf_ek   = f"uf_edit_{selected_sheet}_{curr_claim_id}_{uf_name}_{uf_idx}"
            if uf_mk not in st.session_state: st.session_state[uf_mk] = uf.get("value", "")
            if uf_ek not in st.session_state: st.session_state[uf_ek] = False
            uc1b, uc2b, uc3b, uc4b = st.columns([2, 3, 0.6, 0.6], gap="small")
            with uc1b:
                st.markdown(
                    f"<div style='min-height:40px;display:flex;align-items:center;gap:4px;"
                    f"color:var(--purple);font-size:var(--sz-xs);font-weight:600;"
                    f"text-transform:uppercase;letter-spacing:1px;font-family:var(--mono);'>"
                    f"{uf_name}</div>",
                    unsafe_allow_html=True,
                )
            with uc2b:
                if st.session_state[uf_ek]:
                    with st.form(key=f"uf_form_{selected_sheet}_{curr_claim_id}_{uf_name}_{uf_idx}", border=False):
                        new_uf_val = st.text_input("v", value=st.session_state[uf_mk], label_visibility="collapsed")
                        if st.form_submit_button("", use_container_width=False):
                            st.session_state[uf_mk] = new_uf_val
                            st.session_state[_user_fields_key][uf_idx]["value"] = new_uf_val
                            st.session_state[uf_ek] = False; st.rerun()
                else:
                    st.text_input("v", key=uf_mk, label_visibility="collapsed", disabled=True)
                    st.session_state[_user_fields_key][uf_idx]["value"] = st.session_state.get(uf_mk, "")
            with uc3b:
                if not st.session_state[uf_ek]:
                    if st.button("✏", key=f"uf_ed_{selected_sheet}_{curr_claim_id}_{uf_name}_{uf_idx}", use_container_width=True):
                        st.session_state[uf_ek] = True; st.rerun()
                else:
                    st.markdown("<div style='height:38px;display:flex;align-items:center;justify-content:center;color:var(--green);font-size:11px;border:1px solid var(--b0);border-radius:6px;'>↵</div>", unsafe_allow_html=True)
            with uc4b:
                if st.button("🗑", key=f"uf_del_{selected_sheet}_{curr_claim_id}_{uf_name}_{uf_idx}", use_container_width=True):
                    st.session_state[_user_fields_key].pop(uf_idx); st.rerun()
        st.markdown("<div style='height:4px;'></div>", unsafe_allow_html=True)

    st.markdown("<div class='add-field-panel'>", unsafe_allow_html=True)
    af1, af2, af3 = st.columns([1.8, 2.5, 0.8], gap="small")
    with af1:
        new_field_name = st.text_input(
            "Field name",
            key=f"nf_name_{selected_sheet}_{curr_claim_id}_{_ctr}",
            placeholder="e.g. Internal Notes",
            label_visibility="collapsed",
        )
    with af2:
        new_field_value = st.text_input(
            "Field value",
            key=f"nf_val_{selected_sheet}_{curr_claim_id}_{_ctr}",
            placeholder="Enter value…",
            label_visibility="collapsed",
        )
    with af3:
        if st.button(
            "＋ Add",
            key=f"add_field_go_{selected_sheet}_{curr_claim_id}_{_ctr}",
            use_container_width=True,
            type="primary",
        ):
            fname = new_field_name.strip()
            if fname:
                existing_names = {f["name"] for f in st.session_state[_user_fields_key]}
                if fname not in existing_names:
                    st.session_state[_user_fields_key].append({"name": fname, "value": new_field_value.strip()})
                    st.session_state[_add_counter_key] = _ctr + 1
                    _append_audit({
                        "event":     "FIELD_ADDED",
                        "timestamp": datetime.datetime.now().isoformat(),
                        "filename":  uploaded_name,
                        "sheet":     selected_sheet,
                        "claim_id":  curr_claim_id,
                        "field":     fname,
                        "value":     new_field_value.strip(),
                    })
                    st.rerun()
                else:
                    st.warning(f"Field '{fname}' already exists.")
            else:
                st.warning("Please enter a field name.")
    st.markdown("</div>", unsafe_allow_html=True)


# ── Totals section renderer ───────────────────────────────────────────────────

def _render_totals_section(totals_data: dict) -> None:
    """
    Render the Sheet Totals section.

    Works whether totals_data came from an Excel totals row ("source": "excel_row")
    or was computed on-the-fly from claim data ("source": "computed").
    Always renders when aggregated is non-empty.
    """
    agg = totals_data.get("aggregated", {})
    if not agg:
        return

    source      = totals_data.get("source", "computed")
    source_label = (
        "<span style='font-size:9px;background:rgba(52,211,153,0.12);"
        "border:1px solid rgba(52,211,153,0.3);border-radius:3px;color:#34d399;"
        "padding:1px 5px;margin-left:6px;font-family:monospace;'>FROM EXCEL ROW</span>"
        if source == "excel_row" else
        "<span style='font-size:9px;background:rgba(96,165,250,0.12);"
        "border:1px solid rgba(96,165,250,0.3);border-radius:3px;color:#60a5fa;"
        "padding:1px 5px;margin-left:6px;font-family:monospace;'></span>"
    )

    st.markdown("<hr>", unsafe_allow_html=True)
    st.markdown(
        f"<p class='section-lbl' style='display:flex;align-items:center;'>"
        f"Sheet Totals{source_label}</p>",
        unsafe_allow_html=True,
    )

    t_cols = st.columns(min(4, len(agg)))
    for idx, (k, v) in enumerate(agg.items()):
        with t_cols[idx % len(t_cols)]:
            st.markdown(
                f"<div style='background:#f8f9fc;border:1px solid #d0d6e8;"
                f"border-top:3px solid #0a9e6a;border-radius:8px;"
                f"padding:12px 16px;margin-bottom:8px;"
                f"box-shadow:0 1px 4px rgba(0,0,0,0.06);'>"
                f"<div style='font-size:10px;font-weight:800;color:#4a5578;"
                f"text-transform:uppercase;font-family:monospace;"
                f"letter-spacing:1.2px;margin-bottom:6px;'>{k}</div>"
                f"<div style='font-size:16px;font-weight:700;color:#0a9e6a;"
                f"font-family:monospace;'>{v:,.2f}</div></div>",
                unsafe_allow_html=True,
            )


# ── Main render function ──────────────────────────────────────────────────────

def render_claim_panel(
    *,
    curr_claim, curr_claim_id, active,
    selected_sheet, excel_path, merged_meta, totals_data,
    title_fields, uploaded_name, SCHEMAS,
    _llm_map_result, _field_dup_index, _claim_dup_results,
):
    use_conf    = st.session_state.get("use_conf_threshold", False)
    conf_thresh = st.session_state.get("conf_threshold", 80) if use_conf else 0
    active_schema = st.session_state.get("active_schema", None)

    # ── Always ensure totals are populated from claim data if not already ─────
    # If the caller passed an empty / Excel-only totals dict that has no numbers,
    # fall back to computing totals from the loaded claim rows right here so the
    # section always appears.
    if not totals_data or not totals_data.get("aggregated"):
        from modules.file_utils import compute_totals_from_claims
        totals_data = compute_totals_from_claims(active.get("data", []))

    # ── Sheet title banner ────────────────────────────────────────────────────
    # Primary source: title_fields (KV pairs extracted from pre-header rows).
    # Fallback: merged_meta (for files that use merged cells for titles).
    # Both sources are combined so nothing is lost regardless of file layout.

    # Pull display values from title_fields (already canonical keys)
    _tf_tpa      = (title_fields.get("TPA Name")      or {}).get("value", "")
    _tf_subtitle = (title_fields.get("Sheet Title")   or {}).get("value", "")
    _tf_sheet    = (title_fields.get("Sheet Name")    or {}).get("value", "")
    _tf_reinsurer   = (title_fields.get("Reinsurer")     or {}).get("value", "")
    _tf_treaty      = (title_fields.get("Treaty")        or {}).get("value", "")
    _tf_cedant      = (title_fields.get("Cedant")        or {}).get("value", "")
    _tf_valuation   = (title_fields.get("Valuation Date") or {}).get("value", "")
    _tf_report_date = (title_fields.get("Report Date")   or {}).get("value", "")

    # Fallback: read main/sub title from merged-cell TITLE regions
    _mc_main = _mc_sub = ""
    for _, m in sorted(
        [(k, v) for k, v in merged_meta.items() if v.get("value") and v.get("type") == "TITLE"],
        key=lambda x: (x[1]["row_start"], x[1]["col_start"]),
    ):
        if not _mc_main:   _mc_main = m["value"]
        elif not _mc_sub:  _mc_sub  = m["value"]

    # Resolve final display values — title_fields wins over merged cells
    _banner_main = _tf_tpa      or _mc_main or ""
    _banner_sub  = _tf_subtitle or _mc_sub  or ""

    # Collect metadata rows: only include fields that have a value
    _meta_rows = [
        ("Reinsurer",      _tf_reinsurer),
        ("Treaty",         _tf_treaty),
        ("Cedant",         _tf_cedant),
        ("Valuation Date", _tf_valuation),
        ("Report Date",    _tf_report_date),
    ]
    _meta_rows = [(k, v) for k, v in _meta_rows if v]

    if _banner_main or _banner_sub or _meta_rows:
        # Build metadata rows HTML
        _meta_html = ""
        if _meta_rows:
            _pills = "".join(
                f"<span style='display:inline-flex;align-items:baseline;gap:5px;"
                f"background:#ffffff;border:1px solid #d0d6e8;"
                f"border-radius:5px;padding:4px 10px;margin:3px 4px 3px 0;'>"
                f"<span style='font-size:9px;color:#4a5578;font-family:monospace;"
                f"text-transform:uppercase;letter-spacing:.8px;font-weight:700;"
                f"white-space:nowrap;'>{k}:</span>"
                f"<span style='font-size:12px;color:#0f1117;font-family:monospace;"
                f"font-weight:500;'>{v}</span></span>"
                for k, v in _meta_rows
            )
            _meta_html = (
                f"<div style='display:flex;flex-wrap:wrap;gap:0;margin-top:8px;"
                f"padding-top:8px;border-top:1px solid #e8ecf4;'>{_pills}</div>"
            )

        _title_inner = ""
        if _banner_main:
            _title_inner += (
                f"<div style='font-size:15px;font-weight:700;color:#0f1117;"
                f"font-family:var(--font-head);margin-bottom:4px;line-height:1.3;'>"
                f"{_banner_main}</div>"
            )
        if _banner_sub:
            _title_inner += (
                f"<div style='font-size:12px;color:#1a6fd8;font-family:monospace;"
                f"font-weight:500;margin-bottom:6px;'>{_banner_sub}</div>"
            )
        if _meta_rows:
            _pills = "".join(
                f"<span style='display:inline-flex;align-items:baseline;gap:5px;"
                f"background:#ffffff;border:1px solid #d0d6e8;"
                f"border-radius:5px;padding:4px 10px;margin:3px 4px 3px 0;'>"
                f"<span style='font-size:9px;color:#4a5578;font-family:monospace;"
                f"text-transform:uppercase;letter-spacing:.8px;font-weight:700;"
                f"white-space:nowrap;'>{k}:</span>"
                f"<span style='font-size:12px;color:#0f1117;font-family:monospace;"
                f"font-weight:500;'>{v}</span></span>"
                for k, v in _meta_rows
            )
            _title_inner += (
                f"<div style='display:flex;flex-wrap:wrap;gap:0;margin-top:8px;"
                f"padding-top:8px;border-top:1px solid #e8ecf4;'>{_pills}</div>"
            )
        st.markdown(
            f"<div style='background:#f8f9fc;border:1px solid #d0d6e8;"
            f"border-left:4px solid #1a6fd8;border-radius:10px;"
            f"padding:14px 18px;margin-bottom:12px;"
            f"box-shadow:0 1px 4px rgba(0,0,0,0.06);'>"
            f"<div style='font-size:9px;font-weight:800;color:#4a5578;"
            f"font-family:monospace;text-transform:uppercase;"
            f"letter-spacing:1.5px;margin-bottom:8px;'>📋 Sheet Title</div>"
            f"{_title_inner}"
            f"</div>",
            unsafe_allow_html=True,
        )

    # ── Sheet-level totals ────────────────────────────────────────────────────
    # Rendered here, directly below the sheet title banner and above the
    # per-claim review panel, so the overall summary is always visible
    # without scrolling past individual claim fields.
    _render_totals_section(totals_data)

    # ── Claim header ──────────────────────────────────────────────────────────
    head_left, head_right = st.columns([3, 1])
    with head_left:
        st.markdown(
            "<p class='section-lbl' style='font-weight:800;color:#0f1117;"
            "margin-bottom:6px;'>Review Details</p>",
            unsafe_allow_html=True,
        )
        h_name   = get_val(curr_claim, ["Insured Name", "Name", "Claimant", "TPA_NAME"], "Unknown Entity")
        h_date   = get_val(curr_claim, ["Loss Date", "Date", "LOSS_DATE"], "N/A")
        h_status = get_val(curr_claim, ["Status", "CLAIM_STATUS"], "Submitted")
        h_total  = get_val(curr_claim, ["Total Incurred", "Incurred", "Total", "Amount", "TOTAL_INCURRED"], "$0")
        _s_up = str(h_status).upper()
        _sc   = "#0a9e6a" if "CLOS" in _s_up else "#c99a00" if "OPEN" in _s_up else "#1a6fd8"
        _sb   = "#e6f9f2" if "CLOS" in _s_up else "#fffbeb" if "OPEN" in _s_up else "#e8f0fe"
        st.markdown(
            f"<div style='background:#f8f9fc;border:1px solid #d0d6e8;"
            f"border-left:4px solid #1a6fd8;border-radius:10px;"
            f"padding:14px 18px;margin-bottom:8px;"
            f"box-shadow:0 1px 4px rgba(0,0,0,0.06);'>"
            f"<div style='display:flex;justify-content:space-between;"
            f"align-items:flex-start;flex-wrap:wrap;gap:12px;'>"
            f"<div>"
            f"<div style='font-size:9px;font-weight:800;color:#4a5578;"
            f"font-family:monospace;text-transform:uppercase;"
            f"letter-spacing:1.2px;margin-bottom:4px;'>Claim ID</div>"
            f"<div style='font-size:20px;font-weight:700;color:#0f1117;"
            f"font-family:var(--font-head);letter-spacing:-0.3px;'>{curr_claim_id}</div>"
            f"<div style='font-size:12px;color:#4a5578;margin-top:3px;'>"
            f"{h_name} — {h_date}</div>"
            f"</div>"
            f"<div style='display:flex;gap:20px;align-items:flex-start;'>"
            f"<div>"
            f"<div style='font-size:9px;font-weight:800;color:#4a5578;"
            f"font-family:monospace;text-transform:uppercase;"
            f"letter-spacing:1.2px;margin-bottom:4px;'>Status</div>"
            f"<span style='display:inline-block;background:{_sb};"
            f"border:1px solid {_sc}60;border-radius:6px;padding:4px 14px;"
            f"font-size:12px;font-weight:700;color:{_sc};"
            f"font-family:monospace;'>{h_status}</span>"
            f"</div>"
            f"<div>"
            f"<div style='font-size:9px;font-weight:800;color:#4a5578;"
            f"font-family:monospace;text-transform:uppercase;"
            f"letter-spacing:1.2px;margin-bottom:4px;'>Total Incurred</div>"
            f"<div style='font-size:20px;font-weight:700;color:#0a9e6a;"
            f"font-family:monospace;'>{h_total}</div>"
            f"</div></div></div></div>",
            unsafe_allow_html=True,
        )
    with head_right:
        st.markdown("<p class='section-lbl' style='text-align:right;'>Export Selection</p>", unsafe_allow_html=True)
        b1, b2 = st.columns([1, 1])
        with b1:
            if st.button("✔ All", key=f"all_{selected_sheet}_{curr_claim_id}", use_container_width=True):
                for fld in curr_claim:
                    st.session_state[f"chk_{selected_sheet}_{curr_claim_id}_{fld}"] = True
                _active_s = st.session_state.get("active_schema")
                if _active_s:
                    from config.schemas import SCHEMAS
                    if _active_s in SCHEMAS:
                        for sf in SCHEMAS[_active_s].get("accepted_fields", []):
                            st.session_state[f"chk_{selected_sheet}_{curr_claim_id}_{sf}"] = True
                st.rerun()
        with b2:
            if st.button("✘ None", key=f"none_{selected_sheet}_{curr_claim_id}", use_container_width=True):
                for fld in curr_claim:
                    st.session_state[f"chk_{selected_sheet}_{curr_claim_id}_{fld}"] = False
                _active_s = st.session_state.get("active_schema")
                if _active_s:
                    from config.schemas import SCHEMAS
                    if _active_s in SCHEMAS:
                        for sf in SCHEMAS[_active_s].get("accepted_fields", []):
                            st.session_state[f"chk_{selected_sheet}_{curr_claim_id}_{sf}"] = False
                st.rerun()

    st.markdown("<hr>", unsafe_allow_html=True)

    # ── Claim-level duplicate panel ───────────────────────────────────────────
    render_claim_dup_panel(curr_claim_id, _claim_dup_results, selected_sheet)

    # ── Field grid ────────────────────────────────────────────────────────────
    if active_schema and active_schema in SCHEMAS:
        _render_schema_mode(
            curr_claim, curr_claim_id, active, selected_sheet,
            excel_path, uploaded_name, SCHEMAS,
            _llm_map_result, _field_dup_index, _claim_dup_results,
            use_conf, conf_thresh, active_schema,
        )
    else:
        _render_plain_mode(
            curr_claim, curr_claim_id, active, selected_sheet,
            excel_path, uploaded_name,
            _llm_map_result, _field_dup_index, _claim_dup_results,
            use_conf, conf_thresh,
        )

    # ── Custom field adder ────────────────────────────────────────────────────
    _render_custom_field_adder(curr_claim_id, selected_sheet, uploaded_name)