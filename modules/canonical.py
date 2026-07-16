"""
modules/canonical.py

Canonical field-record format used across all parser outputs.

DESIGN DECISION (stated explicitly, not silently assumed): the
canonical shape chosen is the one Excel/CSV already produce today,
since it's the most information-rich and every downstream consumer
(claim_dup_store, export.py, schema_mapping.py) already expects it:

    {
      "<field_name>": {
        "value":       <raw extracted value>,
        "modified":    <current value, editable by reviewer>,
        "confidence":  <0.0-1.0>,
        "source_type": "<excel|pdf|word|txt|html>",
        "source_ref":  <dict of source-specific location info>,
        ... all original parser-specific keys are preserved too ...
      },
      ...
    }

Word documents are already converted to this shape by app2.py's
_word_fields_to_row(). PDF/TXT/HTML entities from pdf_intelligence.py
use a different key set (azure_di_key, source_text, bounding_polygon,
confidence, etc. alongside value) -- to_canonical_fields() below adapts
those into the shape above WITHOUT discarding the extra PDF-specific
keys the UI relies on for bounding-box rendering. This is additive
normalization, not a replacement -- nothing that currently reads the
original keys breaks.
"""


def to_canonical_fields(raw_fields: dict, source_type: str) -> dict:
    """
    Normalizes any parser's field dict into the canonical shape.

    raw_fields: a dict of {field_name: field_data}, where field_data is
                either a dict (most parsers) or a bare scalar (rare).
    source_type: one of "excel", "pdf", "word", "txt", "html" -- used
                 as the canonical source_type unless the field itself
                 already specifies one.

    Non-destructive: extra source-specific keys (bounding_polygon,
    azure_di_key, excel_row, source_page, etc.) are preserved alongside
    the canonical value/modified/confidence/source_type keys.
    """
    canonical: dict = {}

    for fname, fdata in raw_fields.items():
        if not isinstance(fdata, dict):
            canonical[fname] = {
                "value":       str(fdata),
                "modified":    str(fdata),
                "confidence":  0.0,
                "source_type": source_type,
                "source_ref":  {},
            }
            continue

        value      = fdata.get("value", "")
        modified   = fdata.get("modified", value)
        confidence = float(fdata.get("confidence", 0.0) or 0.0)

        # Everything that isn't one of the three canonical keys becomes
        # part of source_ref, for callers that want a clean "extra
        # metadata" bucket -- but is also kept at the top level below,
        # so nothing that currently reads e.g. field["bounding_polygon"]
        # directly needs to change.
        source_ref = {
            k: v for k, v in fdata.items()
            if k not in ("value", "modified", "confidence")
        }

        merged = dict(fdata)
        merged["value"]       = value
        merged["modified"]    = modified
        merged["confidence"]  = confidence
        merged["source_type"] = fdata.get("source_type", source_type)
        merged["source_ref"]  = source_ref

        canonical[fname] = merged

    return canonical


def is_canonical(field_data: dict) -> bool:
    """Quick check for whether a field dict already has the canonical keys."""
    return isinstance(field_data, dict) and all(
        k in field_data for k in ("value", "modified", "confidence", "source_type")
    )
