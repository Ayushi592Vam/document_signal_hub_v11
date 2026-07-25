"""
modules/llm.py
Thin wrapper around the Azure OpenAI chat-completions endpoint.
All LLM calls in the application go through _llm_call().

Token tracking + cost accumulation written to st.session_state["_llm_usage_log"],
plus persisted via modules.orchestrator.record_llm_cost() for cross-session
cost logging (Delta-backed llm_cost_log table).
"""

import json
import os
import urllib.error
import urllib.request

# ── Pricing table (USD per 1 000 tokens) ─────────────────────────────────────
# Update these if your deployment uses a different model.
_PRICE_INPUT_PER_1K  = float(os.environ.get("LLM_PRICE_INPUT",  "0.00015"))   # gpt-4o-mini default
_PRICE_OUTPUT_PER_1K = float(os.environ.get("LLM_PRICE_OUTPUT", "0.00060"))


def _llm_available() -> bool:
    return (
        bool(os.environ.get("OPENAI_API_KEY", "").strip())
        and bool(os.environ.get("OPENAI_DEPLOYMENT_ENDPOINT", "").strip())
    )


def _llm_call(prompt: str, max_tokens: int = 300, call_purpose: str = "general") -> str:
    """
    Makes a single chat-completion call and records token usage in
    st.session_state["_llm_usage_log"] so the nav panel can display costs.

    call_purpose is a short human-readable tag, e.g.:
      "field_mapping"   — llm_map_unknown_fields()
      "cause_of_loss"   — enrich_claim_cause_of_loss()
      "general"         — anything else
    """
    endpoint = os.environ.get("OPENAI_DEPLOYMENT_ENDPOINT", "").rstrip("/")
    api_key  = os.environ.get("OPENAI_API_KEY", "")
    api_ver  = os.environ.get("OPENAI_API_VERSION", "2024-12-01-preview")
    model    = os.environ.get("OPENAI_DEPLOYMENT_NAME", "gpt-4o-mini")
    url      = f"{endpoint}/openai/deployments/{model}/chat/completions?api-version={api_ver}"

    payload = json.dumps({
        "messages":    [{"role": "user", "content": prompt}],
        "max_tokens":  max_tokens,
        "temperature": 0.0,
    }).encode()

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "api-key": api_key},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode())

    # ── Extract token counts from response ────────────────────────────────────
    usage          = body.get("usage", {})
    prompt_tok     = int(usage.get("prompt_tokens",     0))
    completion_tok = int(usage.get("completion_tokens", 0))
    total_tok      = int(usage.get("total_tokens",      prompt_tok + completion_tok))
    cost_usd       = (
        prompt_tok     / 1000 * _PRICE_INPUT_PER_1K
        + completion_tok / 1000 * _PRICE_OUTPUT_PER_1K
    )

    # ── Persist to session_state (safe even outside a Streamlit callback) ─────
    try:
        import streamlit as st
        import datetime

        if "_llm_usage_log" not in st.session_state:
            st.session_state["_llm_usage_log"] = []

        _usage_entry = {
            "ts":             datetime.datetime.now().strftime("%H:%M:%S"),
            "purpose":        call_purpose,
            "model":          model,
            "prompt_tokens":  prompt_tok,
            "output_tokens":  completion_tok,
            "total_tokens":   total_tok,
            "cost_usd":       cost_usd,
        }
        st.session_state["_llm_usage_log"].append(_usage_entry)

        # Also keep running totals for quick display
        totals = st.session_state.setdefault("_llm_totals", {
            "prompt_tokens": 0, "output_tokens": 0,
            "total_tokens": 0, "cost_usd": 0.0, "calls": 0,
        })
        totals["prompt_tokens"]  += prompt_tok
        totals["output_tokens"]  += completion_tok
        totals["total_tokens"]   += total_tok
        totals["cost_usd"]       += cost_usd
        totals["calls"]          += 1

        # Cross-session cost logging (Delta-backed llm_cost_log table)
        from modules.orchestrator import record_llm_cost
        record_llm_cost({
            "ts":             datetime.datetime.now().strftime("%H:%M:%S"),
            "purpose":        call_purpose,
            "model":          model,
            "prompt_tokens":  prompt_tok,
            "output_tokens":  completion_tok,
            "total_tokens":   total_tok,
            "cost_usd":       cost_usd,
            "log_date":       datetime.date.today().isoformat(),
        })

    except Exception:
        pass   # Never let tracking break the actual LLM call

    return body["choices"][0]["message"]["content"]
