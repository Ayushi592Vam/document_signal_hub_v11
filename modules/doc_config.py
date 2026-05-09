"""
modules/doc_config.py  — v1

Loads per-document-type YAML configs from config/ and provides helpers used
by pdf_intelligence.py to build dynamic, non-hardcoded LLM prompts.

Key responsibilities:
  1. Load + cache YAML configs for FNOL / Legal / Loss Run / Medical
  2. Detect sub-type within a doc type from document text
  3. Build the entity field list for the LLM prompt
     (core entities + sub-type entities merged, no duplication)
  4. Build the type_specific field list for the summary prompt
  5. Expose signal severity trigger lists for the Signals tab
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────────────────
# PATH RESOLUTION
# The config directory is looked up relative to THIS file first, then as an
# absolute path, then from CWD.  This makes it work locally, in Docker, and
# on Streamlit Cloud without any env-var ceremony.
# ─────────────────────────────────────────────────────────────────────────────

def _find_config_dir() -> Path:
    candidates = [
        Path(__file__).parent.parent / "config",   # modules/../config
        Path(__file__).parent / "config",           # modules/config
        Path.cwd() / "config",                      # ./config
        Path(os.environ.get("DOC_CONFIG_DIR", "")), # explicit override
    ]
    for c in candidates:
        if c.is_dir():
            return c
    # Fall back — we'll return empty configs if files not found
    return candidates[0]


_CONFIG_DIR = _find_config_dir()

# Canonical mapping: doc_type string → yaml filename stem
_CONFIG_FILES: dict[str, str] = {
    "FNOL":     "fnol",
    "Legal":    "legal",
    "Medical":  "medical",
    "Loss Run": "loss_run",
    "Underwriting": "underwriting", 
}


# ─────────────────────────────────────────────────────────────────────────────
# LOADER
# ─────────────────────────────────────────────────────────────────────────────

@lru_cache(maxsize=8)
def load_config(doc_type: str) -> dict[str, Any]:
    """
    Load and return the YAML config for `doc_type`.
    Returns an empty dict if the file is missing or YAML is not installed.
    Result is cached in-process.
    """
    if not _YAML_AVAILABLE:
        return {}
    stem = _CONFIG_FILES.get(doc_type, doc_type.lower().replace(" ", "_"))
    path = _CONFIG_DIR / f"{stem}.yaml"
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def reload_all_configs() -> None:
    """Clear the LRU cache so configs are re-read from disk (useful in dev)."""
    load_config.cache_clear()


# ─────────────────────────────────────────────────────────────────────────────
# SUB-TYPE DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def detect_subtype(doc_type: str, text: str) -> str | None:
    """
    Scan document text for sub-type keywords and return the best-matching
    sub-type key, or None if the config has no sub-types or none match.
    """
    cfg = load_config(doc_type)
    subtype_kw: dict[str, list[str]] = cfg.get("subtype_keywords", {})
    if not subtype_kw:
        return None

    text_lower = text.lower()
    scores: dict[str, int] = {}
    for subtype, keywords in subtype_kw.items():
        scores[subtype] = sum(1 for kw in keywords if kw.lower() in text_lower)

    if not scores:
        return None
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] > 0 else None


# ─────────────────────────────────────────────────────────────────────────────
# ENTITY FIELD LIST BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def _entity_to_display(entity: dict[str, Any]) -> str:
    """Convert a YAML entity dict to a single display-friendly string."""
    name = entity.get("name", "")
    aliases = entity.get("aliases", [])
    if aliases:
        # Include first 3 aliases to keep prompts short
        alias_str = " / ".join(aliases[:3])
        return f"{name} (also: {alias_str})"
    return name


def build_entity_field_list(doc_type: str, subtype: str | None = None) -> str:
    """
    Return a newline-separated list of field names + aliases for the LLM
    entities prompt.  Merges core entities with sub-type entities.
    Each field appears only once.
    """
    cfg = load_config(doc_type)
    core_entities: list[dict] = cfg.get("entities", [])
    subtype_entities: list[dict] = []
    if subtype:
        subtypes: dict[str, list[dict]] = cfg.get("subtypes", {})
        subtype_entities = subtypes.get(subtype, [])

    seen: set[str] = set()
    lines: list[str] = []

    for ent in core_entities + subtype_entities:
        name = ent.get("name", "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        lines.append(_entity_to_display(ent))

    return "\n".join(lines) if lines else "(no fields configured)"


def get_required_fields(doc_type: str, subtype: str | None = None) -> list[str]:
    """Return just the names of required fields."""
    cfg = load_config(doc_type)
    core: list[dict] = cfg.get("entities", [])
    sub: list[dict] = []
    if subtype:
        sub = cfg.get("subtypes", {}).get(subtype, [])
    return [e["name"] for e in core + sub if e.get("required", False)]


# ─────────────────────────────────────────────────────────────────────────────
# TYPE-SPECIFIC (ASSESSMENT) FIELD LIST BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def build_type_specific_field_list(doc_type: str) -> str:
    """
    Return a newline-separated list of assessment fields for the summary prompt.
    """
    cfg = load_config(doc_type)
    fields: list[dict] = cfg.get("type_specific", [])
    if not fields:
        return "(no assessment fields configured)"
    lines = []
    for f in fields:
        name = f.get("name", "").strip()
        desc = f.get("description", "").strip()
        if not name:
            continue
        lines.append(f"{name}: {desc}" if desc else name)
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL CONFIG HELPERS
# ─────────────────────────────────────────────────────────────────────────────

# def get_signal_types(doc_type: str) -> str:
#     """Return comma-separated signal types for the LLM prompt."""
#     cfg = load_config(doc_type)
#     types: list[str] = cfg.get("signals", {}).get("types", [])
#     if not types:
#         return "severity, legal_escalation, fraud_indicator, coverage_issue"
#     return ", ".join(types)

def get_signal_types(doc_type: str) -> str:
    config = load_config(doc_type)
    signals = config.get("signals", {})
    if signals:
        return ", ".join(signals.keys())
    # existing fallback
    return config.get("signal_types", 
        "severity, legal_escalation, fraud_indicator, coverage_issue")


def get_severity_keywords(doc_type: str) -> dict[str, list[str]]:
    """Return severity trigger keyword dict for signal classification."""
    cfg = load_config(doc_type)
    return cfg.get("signals", {}).get("severity_triggers", {})


# ─────────────────────────────────────────────────────────────────────────────
# ROLE / PERSONA
# ─────────────────────────────────────────────────────────────────────────────

def get_role(doc_type: str) -> str:
    """Return the LLM system persona for this doc type."""
    cfg = load_config(doc_type)
    role = cfg.get("role", "")
    if role:
        return role.strip()
    # Sensible fallback if config missing
    return f"expert insurance document analyst specialising in {doc_type} documents"


# ─────────────────────────────────────────────────────────────────────────────
# DOC TYPE META (for UI rendering — mirrors _DOC_TYPE_META in pdf_analysis.py)
# ─────────────────────────────────────────────────────────────────────────────

def get_doc_type_meta(doc_type: str) -> dict[str, str]:
    """Return icon / color / bg for UI display."""
    cfg = load_config(doc_type)
    return {
        "icon":  cfg.get("icon",  "📄"),
        "color": cfg.get("color", "#64748b"),
        "bg":    cfg.get("bg",    "rgba(100,116,139,0.06)"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLASSIFICATION HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_all_classification_keywords() -> dict[str, list[str]]:
    """Return {doc_type: [keywords]} for all configured doc types."""
    result: dict[str, list[str]] = {}
    for doc_type in _CONFIG_FILES:
        cfg = load_config(doc_type)
        result[doc_type] = cfg.get("classification_keywords", [])
    return result


def score_doc_type(text: str) -> dict[str, int]:
    """
    Quick keyword scoring for all doc types — useful as a first-pass
    classification before calling the LLM.
    Returns {doc_type: score}.
    """
    text_lower = text.lower()
    scores: dict[str, int] = {}
    for doc_type, keywords in get_all_classification_keywords().items():
        scores[doc_type] = sum(1 for kw in keywords if kw.lower() in text_lower)
    return scores


# ─────────────────────────────────────────────────────────────────────────────
# FULL CONFIG SUMMARY (for debugging)
# ─────────────────────────────────────────────────────────────────────────────

def config_summary() -> dict[str, Any]:
    """Return a summary of loaded configs (field counts, etc.)."""
    summary = {}
    for doc_type in _CONFIG_FILES:
        cfg = load_config(doc_type)
        core = cfg.get("entities", [])
        subtypes = cfg.get("subtypes", {})
        summary[doc_type] = {
            "core_fields": len(core),
            "subtypes": {k: len(v) for k, v in subtypes.items()},
            "type_specific_fields": len(cfg.get("type_specific", [])),
            "signal_types": cfg.get("signals", {}).get("types", []),
            "has_role": bool(cfg.get("role")),
        }
    return summary



def build_signal_detection_prompt(doc_type: str) -> str:
    """
    Build a rich, structured signal detection prompt from YAML config.
    Used by pdf_intelligence.py _signals_system() for dedicated signal Call B.
    """
    config = load_config(doc_type)
    signals_cfg = config.get("signals", {})

    if not signals_cfg:
        # Graceful fallback to old behavior
        return (
            f"Signal types to detect: {get_signal_types(doc_type)}\n"
            "For each signal: type, severity_level (Highly Severe|High|Moderate|Low), "
            "description, supporting_text (verbatim quote)."
        )

    lines = []
    lines.append("=" * 60)
    lines.append("SIGNAL DETECTION RULES — READ CAREFULLY BEFORE EXTRACTING")
    lines.append("=" * 60)
    lines.append("")
    lines.append(
        "Your task is to detect risk signals from the document text. "
        "Each signal must be grounded in explicit document language."
    )
    lines.append("")

    for sig_type, sig_data in signals_cfg.items():
        label  = sig_data.get("label", sig_type)
        impact = sig_data.get("business_impact", "")
        icon   = sig_data.get("icon", "⚠️")

        lines.append(f"{'─' * 50}")
        lines.append(f"{icon}  SIGNAL: {sig_type}")
        lines.append(f"    Label:           {label}")
        lines.append(f"    Business Impact: {impact}")
        lines.append(f"    Severity Levels:")

        for severity, sev_data in sig_data.get(
            "severity_triggers", {}
        ).items():
            level_label = severity.replace("_", " ").title()
            keywords    = sev_data.get("keywords", [])
            desc        = sev_data.get("description", "")
            kw_str      = ", ".join(f'"{k}"' for k in keywords)

            lines.append(f"")
            lines.append(f"      [{level_label}]")
            lines.append(f"        Triggers  : {kw_str}")
            lines.append(f"        Meaning   : {desc}")

        lines.append("")

    lines.append("=" * 60)
    lines.append("MANDATORY RULES:")
    lines.append("=" * 60)
    lines.append(
        "1. VERBATIM GROUNDING: supporting_text must be an EXACT quote "
        "from the document (max 120 chars). Never paraphrase."
    )
    lines.append(
        "2. TRIGGER MATCH: trigger_matched must be the exact keyword or "
        "phrase from the Triggers list above that fired this signal."
    )
    lines.append(
        "3. NO INFERENCE: Only fire a signal if the trigger keyword or "
        "a clear equivalent appears explicitly in the document text."
    )
    lines.append(
        "4. SEVERITY HONESTY: Use the severity level whose triggers best "
        "match. If unsure between two levels, use the lower one."
    )
    lines.append(
        "5. CONFIDENCE: 0.90+ if trigger keyword is verbatim, "
        "0.70-0.89 if equivalent phrase, below 0.70 if inferred."
    )
    lines.append(
        "6. NO ABSENCE SIGNALS: Do NOT fire a signal because something "
        "is missing. Only fire when explicit evidence is present."
    )
    lines.append(
        "7. MULTIPLE SIGNALS OK: Different signal types can fire from "
        "the same document section."
    )
    lines.append("")

    return "\n".join(lines)    