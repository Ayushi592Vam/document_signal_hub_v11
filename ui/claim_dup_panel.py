"""
ui/claim_dup_panel.py
Renders the claim-level duplicate detection panel.
Shows a before/after diff when a claim was seen in a previous upload.
"""

import streamlit as st
from modules.claim_dup_store import get_claim_dup_result, clear_claim_dup_entry


def render_claim_dup_panel(claim_id: str, dup_results: dict, selected_sheet: str) -> None:
    """
    Renders the duplicate panel just below the claim header.
    Only shows anything if the claim was seen in a previous upload.
    """
    result = get_claim_dup_result(claim_id, dup_results)
    if not result:
        return

    prev_filename   = result.get("prev_filename", "unknown file")
    prev_sheet      = result.get("prev_sheet", "unknown sheet")
    prev_date       = result.get("prev_date", "unknown date")
    changes         = result.get("changes", {})
    unchanged_count = result.get("unchanged_count", 0)
    changed_count   = result.get("changed_count", 0)
    new_fields      = result.get("new_fields", {})

    # ── Top banner ─────────────────────────────────────────────────────────
    if changed_count == 0:
        banner_color  = "#b45309"
        banner_bg     = "#ffffff"
        banner_border = "#fde68a"
        banner_icon   = "⚠"
        banner_title  = "Duplicate Claim — No Changes Detected"
        banner_sub    = (
            f"Claim <b>{claim_id}</b> was previously seen in "
            f"<code>{prev_filename}</code> / <code>{prev_sheet}</code> "
            f"on <b>{prev_date}</b>. All {unchanged_count} field(s) are identical."
        )
    else:
        banner_color  = "#b91c1c"
        banner_bg     = "#ffffff"
        banner_border = "#fecaca"
        banner_icon   = "🔄"
        banner_title  = f"Duplicate Claim — {changed_count} Field(s) Changed"
        banner_sub    = (
            f"Claim <b>{claim_id}</b> previously seen in "
            f"<code>{prev_filename}</code> / <code>{prev_sheet}</code> "
            f"on <b>{prev_date}</b>. "
            f"<b style='color:{banner_color};'>{changed_count} field(s) changed</b>, "
            f"{unchanged_count} unchanged."
        )

    st.markdown(
        f"<div style='background:{banner_bg};border:1px solid {banner_border};"
        f"border-left:4px solid {banner_color};border-radius:8px;"
        f"padding:12px 16px;margin-bottom:10px;'>"
        f"<div style='font-size:13px;font-weight:700;color:{banner_color};"
        f"font-family:monospace;text-transform:uppercase;letter-spacing:1px;"
        f"margin-bottom:6px;'>{banner_icon} {banner_title}</div>"
        f"<div style='font-size:13px;color:#000000;font-family:sans-serif;"
        f"line-height:1.6;'>{banner_sub}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )

    # ── Clear button ───────────────────────────────────────────────────────
    if st.button(
        "🗑  Clear duplicate history for this claim",
        key=f"clear_dup_{claim_id}",
        help="Remove this claim from the duplicate store so it won't be flagged next time",
    ):
        # Single-row Delta delete -- no whole-store load/mutate/save needed
        # now that claim_dup_store is Delta-backed rather than a JSON blob.
        clear_claim_dup_entry(claim_id)

        _dup_key = f"_claim_dup_results_{selected_sheet}"
        if _dup_key in st.session_state:
            results_in_state = st.session_state[_dup_key]
            if claim_id in results_in_state:
                results_in_state[claim_id] = {"is_duplicate": False}

        st.success(f"✅ Duplicate history cleared for claim {claim_id}")
        st.rerun()

    if not changes:
        return

    # ── Toggle for before/after diff ───────────────────────────────────────
    _diff_key = f"show_diff_{claim_id}"
    if _diff_key not in st.session_state:
        st.session_state[_diff_key] = True

    toggle_label = "▲ Hide Changes" if st.session_state[_diff_key] else "▼ Show Before / After Changes"
    if st.button(toggle_label, key=f"toggle_diff_{claim_id}", use_container_width=False):
        st.session_state[_diff_key] = not st.session_state[_diff_key]
        st.rerun()

    if not st.session_state[_diff_key]:
        return

    # ── Before / After diff table header ──────────────────────────────────
    st.markdown(
        "<div style='display:grid;grid-template-columns:1.8fr 2fr 2fr;"
        "gap:8px;padding:8px 12px;"
        "background:#f1f5f9;"           # light slate header
        "border-radius:6px 6px 0 0;"
        "border:1px solid #cbd5e1;"
        "margin-top:8px;'>"
        "<div style='font-size:11px;font-weight:700;color:#000000;"
        "text-transform:uppercase;letter-spacing:1.2px;font-family:monospace;'>Field</div>"
        "<div style='font-size:11px;font-weight:700;color:#b91c1c;"
        "text-transform:uppercase;letter-spacing:1.2px;font-family:monospace;'>Before</div>"
        "<div style='font-size:11px;font-weight:700;color:#15803d;"
        "text-transform:uppercase;letter-spacing:1.2px;font-family:monospace;'>After</div>"
        "</div>",
        unsafe_allow_html=True,
    )

    # ── Rows ───────────────────────────────────────────────────────────────
    for idx, (field, diff) in enumerate(changes.items()):
        before_val = diff.get("before", "") or "(empty)"
        after_val  = diff.get("after",  "") or "(empty)"
        row_bg     = "#ffffff" if idx % 2 == 0 else "#f8fafc"

        st.markdown(
            f"<div style='display:grid;grid-template-columns:1.8fr 2fr 2fr;"
            f"gap:8px;padding:8px 12px;background:{row_bg};"
            f"border-left:1px solid #cbd5e1;border-right:1px solid #cbd5e1;"
            f"border-bottom:1px solid #cbd5e1;'>"
            # field name
            f"<div style='font-size:12px;font-weight:700;color:#000000;"
            f"font-family:monospace;text-transform:uppercase;letter-spacing:0.5px;"
            f"display:flex;align-items:center;'>{field}</div>"
            # before
            f"<div style='font-size:12px;color:#b91c1c;font-family:monospace;"
            f"background:#fef2f2;border:1px solid #fecaca;"
            f"border-radius:4px;padding:4px 8px;word-break:break-all;'>{before_val}</div>"
            # after
            f"<div style='font-size:12px;color:#15803d;font-family:monospace;"
            f"background:#f0fdf4;border:1px solid #bbf7d0;"
            f"border-radius:4px;padding:4px 8px;word-break:break-all;'>{after_val}</div>"
            f"</div>",
            unsafe_allow_html=True,
        )

    # ── Bottom border ──────────────────────────────────────────────────────
    st.markdown(
        "<div style='height:4px;background:#f1f5f9;border-radius:0 0 6px 6px;"
        "border:1px solid #cbd5e1;border-top:none;margin-bottom:12px;'></div>",
        unsafe_allow_html=True,
    )

    # ── Unchanged fields summary ───────────────────────────────────────────
    if unchanged_count > 0:
        unchanged_fields = [
            f for f in new_fields
            if f not in changes and new_fields.get(f)
        ]
        pills = "".join(
            f"<span style='display:inline-block;"
            f"background:#f1f5f9;"          # light grey pill
            f"border:1px solid #cbd5e1;border-radius:4px;padding:2px 8px;"
            f"font-size:11px;color:#000000;margin:2px 3px;"
            f"font-family:monospace;'>{f}</span>"
            for f in unchanged_fields[:12]
        )
        more = (
            f"<span style='font-size:11px;color:#374151;margin-left:4px;'>"
            f"+{unchanged_count - 12} more</span>"
            if unchanged_count > 12 else ""
        )
        st.markdown(
            f"<div style='background:#ffffff;border:1px solid #cbd5e1;"
            f"border-radius:6px;padding:8px 12px;margin-bottom:12px;'>"
            f"<div style='font-size:11px;color:#374151;font-family:monospace;"
            f"text-transform:uppercase;letter-spacing:1px;margin-bottom:5px;'>"
            f"✓ {unchanged_count} unchanged field(s)</div>"
            f"<div>{pills}{more}</div>"
            f"</div>",
            unsafe_allow_html=True,
        )
