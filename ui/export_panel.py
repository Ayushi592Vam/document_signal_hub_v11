"""
ui/export_panel.py
Right-column export panel: schema badge, confidence bar, CoL enrichment result,
live JSON preview, standard export, schema export, merged-regions display.

v9 change: Live JSON preview now shows ONLY raw JSON code (clean card view removed).
"""

import json
import re

import streamlit as st

from modules.audit import _append_audit
from modules.export import (
    build_mapped_records_for_export,
    to_standard_json, to_guidewire_json, to_duck_creek_json,
    clean_duplicate_fields, _sanitize_for_json,
)
from modules.json_export_table import _append_json_export
from modules.schema_mapping import map_claim_to_schema, detect_claim_id
from modules.storage import _save_to_feature_store
from modules.orchestrator import run_with_lineage
from modules.document_metadata import record_metadata

import datetime
from ui.dialogs import show_claim_journey_dialog


# ─────────────────────────────────────────────────────────────────────────────
# MAIN PANEL
# ─────────────────────────────────────────────────────────────────────────────

def _build_cost_metadata(doc_name: str) -> dict:
    log = st.session_state.get("_llm_cost_log", [])
    doc_log = [e for e in log if e.get("doc_name", "") == doc_name]
    if not doc_log:
        return {}
    from ui.pdf_analysis import _compute_cost
    total_cost = sum(_compute_cost(e) for e in doc_log)
    return {
        "calls":              len(doc_log),
        "prompt_tokens":      sum(int(e.get("prompt_tokens", 0)) for e in doc_log),
        "completion_tokens":  sum(int(e.get("completion_tokens", 0)) for e in doc_log),
        "total_cost_usd":     round(total_cost, 6),
        "models_used":        list({e.get("model", "") for e in doc_log}),
    }
    
def render_export_panel(
    *,
    data, curr_claim, curr_claim_id, selected_sheet,
    sh_hash, file_hash, uploaded_name, SCHEMAS,
    merged_meta, totals_data, title_fields,
    _llm_map_result, sheet_type: str = "UNKNOWN",
):
    active_schema = st.session_state.get("active_schema", None)
    use_conf      = st.session_state.get("use_conf_threshold", False)
    conf_thresh   = st.session_state.get("conf_threshold", 80)

    st.markdown("<p class='section-lbl'>Export Format</p>", unsafe_allow_html=True)

    # Active schema badge
    if active_schema and active_schema in SCHEMAS:
        sc         = SCHEMAS[active_schema]
        cf_count   = len(st.session_state.get(f"custom_fields_{active_schema}", []))
        date_fmt_r = sc.get("date_format", "YYYY-MM-DD")
        st.markdown(
            f"<div style='background:var(--s0);border:1px solid {sc['color']}44;"
            f"border-left:2px solid {sc['color']};border-radius:7px;padding:10px 12px;margin-bottom:8px;'>"
            f"<div style='font-size:var(--sz-body);font-weight:700;color:{sc['color']};'>"
            f"{sc['icon']} {active_schema}</div>"
            f"<div style='font-size:var(--sz-xs);color:var(--t2);margin-top:2px;font-family:var(--mono);'>"
            f"{sc['version']}</div>"
            f"<div style='font-size:var(--sz-xs);color:var(--t3);margin-top:2px;font-family:var(--mono);'>"
            f"Date: {date_fmt_r} · Amounts: 2dp</div>"
            f"<div style='font-size:var(--sz-xs);color:var(--t2);margin-top:2px;font-family:var(--mono);'>"
            f"Fields: {len(sc['required_fields'])} req · {cf_count} custom</div></div>",
            unsafe_allow_html=True,
        )

    # Confidence bar
    if use_conf:
        _bc = "var(--green)" if conf_thresh >= 70 else "var(--yellow)" if conf_thresh >= 40 else "var(--red)"
        st.markdown(
            f"<div style='margin-bottom:10px;'>"
            f"<div style='font-size:var(--sz-xs);color:var(--t3);text-transform:uppercase;"
            f"font-weight:600;margin-bottom:3px;font-family:var(--mono);letter-spacing:1px;'>"
            f"Confidence Threshold</div>"
            f"<div style='display:flex;align-items:center;gap:8px;'>"
            f"<div class='conf-bar-wrap' style='flex:1;'>"
            f"<div class='conf-bar-fill' style='width:{conf_thresh}%;background:{_bc};'></div></div>"
            f"<span style='color:{_bc};font-size:var(--sz-body);font-weight:700;'>{conf_thresh}%</span>"
            f"</div></div>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            "<div style='font-size:10px;color:var(--t3);font-family:monospace;margin-bottom:8px;'>"
            "Confidence scoring off — enable in ⚙ Settings</div>",
            unsafe_allow_html=True,
        )

    # Cause of Loss enrichment result
    _col_enriched   = st.session_state.get(f"_col_enriched_{selected_sheet}_{curr_claim_id}", False)
    _col_summary_rp = st.session_state.get(f"_col_summary_{selected_sheet}_{curr_claim_id}")
    _col_val_rp     = None
    for _col_key_try in [
        f"mod_{selected_sheet}_{curr_claim_id}_schema_Cause of Loss",
        f"mod_{selected_sheet}_{curr_claim_id}_schema_Cause Of Loss",
        f"mod_{selected_sheet}_{curr_claim_id}_schema_Cause_of_Loss",
        f"mod_{selected_sheet}_{curr_claim_id}_Cause of Loss",
        f"mod_{selected_sheet}_{curr_claim_id}_Cause Of Loss",
        f"mod_{selected_sheet}_{curr_claim_id}_Cause_of_Loss",
        f"mod_{selected_sheet}_{curr_claim_id}_cause_of_loss",
    ]:
        v = st.session_state.get(_col_key_try)
        if v and str(v).strip():
            _col_val_rp = v; break
    if not _col_val_rp:
        for k, inf in curr_claim.items():
            if re.search(r"cause.?of.?loss", k, re.IGNORECASE):
                v = inf.get("modified") or inf.get("value", "")
                if v and len(str(v).strip()) > 2 and "." not in str(v):
                    _col_val_rp = str(v).strip(); break

    if _col_summary_rp or _col_val_rp:
        _panel_border = "rgba(52,211,153,0.3)" if _col_enriched else "rgba(79,156,249,0.3)"
        _panel_bg     = "rgba(52,211,153,0.07)" if _col_enriched else "rgba(79,156,249,0.07)"
        _panel_accent = "var(--green)" if _col_enriched else "var(--blue)"
        _panel_label  = "✓ Cause of Loss Identified" if _col_enriched else "Cause of Loss"
        st.markdown(
            f"<div style='background:{_panel_bg};border:1px solid {_panel_border};"
            f"border-left:3px solid {_panel_accent};border-radius:7px;padding:10px 12px;margin-bottom:10px;'>"
            f"<div style='font-size:10px;font-weight:700;color:{_panel_accent};font-family:var(--mono);"
            f"text-transform:uppercase;letter-spacing:1px;margin-bottom:5px;'>{_panel_label}</div>"
            + (
                f"<div style='font-size:var(--sz-sm);color:var(--blue);font-family:var(--mono);"
                f"font-weight:700;margin-bottom:6px;'>{_col_val_rp}</div>"
                if _col_val_rp else ""
            )
            + (
                f"<div style='font-size:var(--sz-xs);color:var(--t2);font-family:var(--font);"
                f"line-height:1.6;'>{_col_summary_rp}</div>"
                if _col_summary_rp else ""
            )
            + "</div>",
            unsafe_allow_html=True,
        )

    # LLM unmapped columns notice
    _llm_unmapped = (_llm_map_result or {}).get("_unmapped", [])
    if _llm_unmapped:
        st.markdown(
            f"<div style='background:rgba(248,113,113,0.07);border:1px solid rgba(248,113,113,0.25);"
            f"border-radius:6px;padding:8px 10px;margin-bottom:10px;'>"
            f"<div style='font-size:10px;color:var(--red);font-family:var(--mono);"
            f"text-transform:uppercase;letter-spacing:1px;margin-bottom:3px;'>Unmapped columns</div>"
            f"<div style='font-size:var(--sz-xs);color:var(--t3);font-family:var(--font);'>"
            + ", ".join(f"<code>{c}</code>" for c in _llm_unmapped[:6])
            + ("…" if len(_llm_unmapped) > 6 else "")
            + "</div></div>",
            unsafe_allow_html=True,
        )

    # ── Live JSON preview — RAW JSON ONLY ──────────────────────────────────────
    st.markdown("<hr>", unsafe_allow_html=True)
    _json_toggle_key = f"show_live_json_{selected_sheet}_{curr_claim_id}"
    if _json_toggle_key not in st.session_state:
        st.session_state[_json_toggle_key] = False
    _json_btn_label = "▲ Hide Preview" if st.session_state[_json_toggle_key] else "{ } Preview JSON"
    if st.button(_json_btn_label, key=f"json_toggle_btn_{selected_sheet}_{curr_claim_id}", use_container_width=True):
        st.session_state[_json_toggle_key] = not st.session_state[_json_toggle_key]

    if st.session_state[_json_toggle_key]:
        _rp_live_record: dict = {}
        _rp_schema = st.session_state.get("active_schema", None)

        if _rp_schema and _rp_schema in SCHEMAS:
            # Schema mode preview
            _rp_schema_def = SCHEMAS[_rp_schema]
            _rp_mapped     = map_claim_to_schema(curr_claim, _rp_schema, title_fields, _llm_map_result)
            _rp_cf         = st.session_state.get(f"custom_fields_{_rp_schema}", [])
            _rp_disp       = list(_rp_schema_def["required_fields"]) + [
                f for f in _rp_cf if f not in _rp_schema_def["required_fields"]
            ]
            for sf in _rp_disp:
                chk_key = f"chk_{selected_sheet}_{curr_claim_id}_{sf}"
                if st.session_state.get(chk_key, True) is False:
                    continue
                mk_schema = f"mod_{selected_sheet}_{curr_claim_id}_schema_{sf}"
                mk_plain  = f"mod_{selected_sheet}_{curr_claim_id}_{sf}"
                if st.session_state.get(mk_schema) is not None:
                    val = st.session_state[mk_schema]
                elif st.session_state.get(mk_plain) is not None:
                    val = st.session_state[mk_plain]
                elif sf in _rp_mapped:
                    val = _rp_mapped[sf].get("modified", _rp_mapped[sf].get("value", ""))
                else:
                    val = ""
                if val not in ("", None):
                    _rp_live_record[sf] = val

        else:
            # Plain mode preview
            for fld, inf in curr_claim.items():
                chk_key = f"chk_{selected_sheet}_{curr_claim_id}_{fld}"
                if st.session_state.get(chk_key, True) is False:
                    continue
                mk_rp = f"mod_{selected_sheet}_{curr_claim_id}_{fld}"
                val   = st.session_state.get(mk_rp)
                if val is None:
                    val = inf.get("modified", inf.get("value", ""))
                if val not in ("", None):
                    _rp_live_record[fld] = val

        # Custom fields — always included
        _uf_key = f"user_added_fields_{selected_sheet}_{curr_claim_id}"
        for uf in st.session_state.get(_uf_key, []):
            uf_idx_rp = st.session_state.get(_uf_key, []).index(uf)
            uf_mk_rp  = f"uf_mod_{selected_sheet}_{curr_claim_id}_{uf['name']}_{uf_idx_rp}"
            _rp_live_record[uf["name"]] = st.session_state.get(uf_mk_rp, uf["value"])

        # ── RAW JSON ONLY (v9 — clean card preview removed) ───────────────────
        if _rp_live_record:
            raw_json = json.dumps(_sanitize_for_json(_rp_live_record), indent=2, ensure_ascii=False)
            st.code(raw_json, language="json")
        else:
            st.markdown(
                "<div style='background:#f8f9fa;border:1px solid #e2e8f0;border-radius:8px;"
                "padding:16px;text-align:center;color:#94a3b8;font-size:12px;"
                "font-family:monospace;'>No fields selected for export</div>",
                unsafe_allow_html=True,
            )

    # Claim Journey
    if curr_claim_id:
        if st.button(
            "🔍 View Transformation Journey",
            use_container_width=True,
            key=f"journey_btn_{selected_sheet}_{curr_claim_id}",
            help="See how each field was extracted, mapped, and modified",
        ):
            show_claim_journey_dialog(
                claim_id=curr_claim_id,
                curr_claim=curr_claim,
                selected_sheet=selected_sheet,
                active_schema=st.session_state.get("active_schema"),
                _llm_map_result=_llm_map_result,
            )

    st.markdown("<hr>", unsafe_allow_html=True)

    # Standard export
    _sheet_meta = {"sheet_name": selected_sheet, "record_count": len(data)}
    st.markdown("<hr>", unsafe_allow_html=True)
    st.markdown("<p class='section-lbl'>📄 Standard Export</p>", unsafe_allow_html=True)

    if st.button(
        "⬇ Download Standard JSON",
        use_container_width=True, type="primary",
        key=f"export_std_json_{selected_sheet}",
    ):
        _std_export_data: dict = {}
        for i, row in enumerate(data):
            c_id = detect_claim_id(row, i)
            rec: dict = {}
            for fld, inf in row.items():
                if st.session_state.get(f"chk_{selected_sheet}_{c_id}_{fld}", True):
                    mk_key   = f"mod_{selected_sheet}_{c_id}_{fld}"
                    live_val = st.session_state.get(mk_key, None)
                    orig     = inf.get("value", "")
                    final    = live_val if live_val is not None else inf.get("modified", orig)
                    rec[fld] = {
                        "value": final, "original": orig, "edited": final != orig,
                        "excel_row": inf.get("excel_row"), "excel_col": inf.get("excel_col"),
                        "record_index": i,
                    }
            _uf_key = f"user_added_fields_{selected_sheet}_{c_id}"
            for uf in st.session_state.get(_uf_key, []):
                uf_idx_e = st.session_state.get(_uf_key, []).index(uf)
                uf_mk_e  = f"uf_mod_{selected_sheet}_{c_id}_{uf['name']}_{uf_idx_e}"
                rec[uf["name"]] = {
                    "value": st.session_state.get(uf_mk_e, uf["value"]),
                    "original": "", "edited": True, "user_added": True,
                    "excel_row": None, "excel_col": None, "record_index": i,
                }
            _std_export_data[c_id] = clean_duplicate_fields(rec)

        

        output = run_with_lineage(
            "FILE_EXPORTED", uploaded_name,
            lambda: _sanitize_for_json(to_standard_json(
                _std_export_data, _sheet_meta, totals_data, merged_meta, title_fields=title_fields,
            ))
        )
        record_metadata(
            document_id=file_hash, filename=uploaded_name,
            parser_used="export", status="exported", tags=["export:standard"],
        )
        json_str = json.dumps(output, indent=2, ensure_ascii=False)
        _save_to_feature_store(sh_hash, selected_sheet, output)
        st.session_state[f"_std_json_ready_{selected_sheet}"] = json_str
        _append_json_export({
            "filename": uploaded_name, "sheet": selected_sheet,
            "timestamp": datetime.datetime.now().isoformat(),
            "type": "Standard", "record_count": len(_std_export_data), "json": json_str,
        }, cost_metadata=_build_cost_metadata(uploaded_name))
        _append_audit({
            "event": "EXPORT_GENERATED", "timestamp": datetime.datetime.now().isoformat(),
            "filename": uploaded_name, "sheet": selected_sheet,
            "export_type": "Standard JSON", "records": len(_std_export_data),
        })

    if st.session_state.get(f"_std_json_ready_{selected_sheet}"):
        st.download_button(
            "📥 Save Standard JSON",
            data=st.session_state[f"_std_json_ready_{selected_sheet}"],
            file_name=f"{selected_sheet}_standard.json",
            mime="application/json",
            use_container_width=True,
            key=f"dl_std_json_{selected_sheet}",
        )

    # Schema export
    st.markdown("<hr>", unsafe_allow_html=True)
    st.markdown("<p class='section-lbl'>🔌 Schema Export</p>", unsafe_allow_html=True)
    _schema_sel = st.selectbox(
        "Schema export format",
        options=["— Select schema format —", "🔵 Guidewire (JSON)", "🟡 Duck Creek (JSON)"],
        key=f"schema_export_sel_{selected_sheet}",
        label_visibility="collapsed",
    )

    if _schema_sel and _schema_sel != "— Select schema format —":
        if st.button("⬇ Generate Export", use_container_width=True, key=f"schema_export_go_{selected_sheet}"):
            if "Guidewire" in _schema_sel:
                recs = build_mapped_records_for_export(data, "Guidewire", selected_sheet)
                _inject_user_fields(recs, selected_sheet)
                gj = run_with_lineage(
                    "FILE_EXPORTED",
                    uploaded_name,
                    lambda: _sanitize_for_json(
                        to_guidewire_json(
                            recs,
                            _sheet_meta,
                            title_fields=title_fields,
                            merged_meta=merged_meta,
                        )
                    )
                )

                record_metadata(
                    document_id=file_hash,
                    filename=uploaded_name,
                    parser_used="export",
                    status="exported",
                    tags=["export:guidewire"],
                )
                json_str = json.dumps(gj, indent=2, ensure_ascii=False)
                _save_to_feature_store(sh_hash, selected_sheet, gj)
                st.session_state[f"_schema_export_data_{selected_sheet}"] = {
                    "data": json_str, "filename": f"{selected_sheet}_Guidewire_ClaimCenter.json",
                    "mime": "application/json", "label": "📥 Save Guidewire JSON",
                }
                etype = "Guidewire JSON"; rec_count = len(recs)
            elif "Duck Creek" in _schema_sel:
                recs = build_mapped_records_for_export(data, "Duck Creek", selected_sheet)
                _inject_user_fields(recs, selected_sheet)
                dj = run_with_lineage(
                    "FILE_EXPORTED",
                    uploaded_name,
                    lambda: _sanitize_for_json(
                        to_duck_creek_json(
                            recs,
                            _sheet_meta,
                            title_fields=title_fields,
                            merged_meta=merged_meta,
                        )
                    )
                )

                record_metadata(
                    document_id=file_hash,
                    filename=uploaded_name,
                    parser_used="export",
                    status="exported",
                    tags=["export:duckcreek"],
                )

                json_str = json.dumps(dj, indent=2, ensure_ascii=False)
                _save_to_feature_store(sh_hash, selected_sheet, dj)
                st.session_state[f"_schema_export_data_{selected_sheet}"] = {
                    "data": json_str, "filename": f"{selected_sheet}_DuckCreek.json",
                    "mime": "application/json", "label": "📥 Save Duck Creek JSON",
                }
                etype = "Duck Creek JSON"; rec_count = len(recs)
            else:
                etype = "Unknown"; json_str = "{}"; rec_count = 0
            _append_json_export({
                "filename": uploaded_name, "sheet": selected_sheet,
                "timestamp": datetime.datetime.now().isoformat(),
                "type": etype, "record_count": rec_count, "json": json_str,
            })
            _append_audit({
                "event": "EXPORT_GENERATED", "timestamp": datetime.datetime.now().isoformat(),
                "filename": uploaded_name, "sheet": selected_sheet,
                "export_type": etype, "records": len(data),
            }, cost_metadata=_build_cost_metadata(uploaded_name))
            st.success("✅ Ready!")

    _exp_ready = st.session_state.get(f"_schema_export_data_{selected_sheet}")
    if _exp_ready:
        st.download_button(
            _exp_ready["label"], data=_exp_ready["data"],
            file_name=_exp_ready["filename"], mime=_exp_ready["mime"],
            use_container_width=True, key=f"dl_schema_export_{selected_sheet}",
        )

    # Merged regions
    st.markdown("<hr>", unsafe_allow_html=True)
    if merged_meta:
        st.markdown("<p class='section-lbl'>Merged Regions</p>", unsafe_allow_html=True)
        sorted_merges = sorted(
            [(k, v) for k, v in merged_meta.items() if v["value"]],
            key=lambda x: (x[1]["row_start"], x[1]["col_start"]),
        )
        for _, m in sorted_merges[:8]:
            type_color = (
                "var(--blue)"   if m["type"] == "TITLE"  else
                "var(--yellow)" if m["type"] == "HEADER" else
                "var(--t3)"
            )
            full_value = m["value"]
            st.markdown(
                f"<div style='background:var(--s0);border:1px solid var(--b0);"
                f"border-radius:6px;padding:6px 10px;margin-bottom:4px;'>"
                f"<div style='font-size:var(--sz-xs);color:{type_color};font-family:var(--mono);'>"
                f"{m['type']} · R{m['row_start']}C{m['col_start']}→R{m['row_end']}C{m['col_end']}</div>"
                f"<div style='font-size:var(--sz-body);color:var(--t0);margin-top:2px;"
                f"word-break:break-word;white-space:normal;'>"
                f"{full_value}</div></div>",
                unsafe_allow_html=True,
            )


# ── Helper ────────────────────────────────────────────────────────────────────

def _inject_user_fields(recs: list, selected_sheet: str) -> None:
    """Merge user-added custom fields into export records in place."""
    for rec in recs:
        _uf_key = f"user_added_fields_{selected_sheet}_{rec.get('_claim_id', '')}"
        for uf in st.session_state.get(_uf_key, []):
            uf_idx  = st.session_state.get(_uf_key, []).index(uf)
            uf_mk_e = f"uf_mod_{selected_sheet}_{rec.get('_claim_id', '')}_{uf['name']}_{uf_idx}"
            rec[uf["name"]] = {
                "value":      st.session_state.get(uf_mk_e, uf["value"]),
                "confidence": 100,
                "edited":     True,
                "original":   "",
                "user_added": True,
            }
