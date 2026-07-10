"""
modules/guardrails.py
Input validation, output validation, and PII-aware redaction for the
document ingestion pipeline.

Three responsibilities:
  1. Input guardrails  -- reject oversized files, catch extension/content
                           mismatches before parsing is attempted.
  2. Output guardrails -- a single checkpoint for validating LLM/Azure DI
                           output shape before it reaches the UI.
  3. Redaction          -- strip or mask likely-sensitive values before
                           they're written to the audit log.
"""

import re

MAX_FILE_SIZE_MB = 25

# Magic-byte signatures for cross-checking claimed extension vs actual
# content. TXT/HTML have no reliable magic bytes, so they're skipped.
_MAGIC_BYTES = {
    ".pdf":  [b"%PDF"],
    ".xlsx": [b"PK\x03\x04"],
    ".docx": [b"PK\x03\x04"],
    ".csv":  None,   # no reliable signature
    ".txt":  None,
    ".html": None,
    ".htm":  None,
}

# Field-name patterns whose values should be redacted before writing to
# the audit log. Extend this list as new sensitive field types appear.
_SENSITIVE_FIELD_PATTERNS = [
    r"claimant.*name", r"patient.*name", r"policyholder.*name",
    r"insured.*name", r"ssn", r"social.*security",
    r"date.*of.*birth", r"\bdob\b", r"phone", r"email",
    r"address",
]
_SENSITIVE_RE = re.compile("|".join(_SENSITIVE_FIELD_PATTERNS), re.IGNORECASE)


# ── Input guardrails ───────────────────────────────────────────────────────

def validate_upload(file_bytes: bytes, filename: str, file_ext: str) -> tuple[bool, str]:
    """
    Returns (ok, reason). reason is empty string if ok=True.
    Call this before any parsing is attempted.
    """
    size_mb = len(file_bytes) / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        return False, f"File is {size_mb:.1f} MB, exceeds the {MAX_FILE_SIZE_MB} MB limit."

    if len(file_bytes) == 0:
        return False, "File is empty."

    signatures = _MAGIC_BYTES.get(file_ext.lower())
    if signatures:
        if not any(file_bytes.startswith(sig) for sig in signatures):
            return False, (
                f"File content does not match its extension ({file_ext}). "
                f"This can happen if a file was renamed to spoof its type."
            )

    return True, ""


# ── Output guardrails ──────────────────────────────────────────────────────

def validate_extraction_output(result: dict, required_keys: list[str] | None = None) -> tuple[bool, str]:
    """
    Single checkpoint for validating LLM/Azure DI output shape before it
    reaches the UI. Returns (ok, reason).
    """
    if not isinstance(result, dict):
        return False, "Extraction result is not a dict."

    required_keys = required_keys or ["doc_type", "analysis"]
    missing = [k for k in required_keys if k not in result]
    if missing:
        return False, f"Extraction result missing required key(s): {', '.join(missing)}."

    return True, ""


# ── Redaction for audit logging ────────────────────────────────────────────

def is_sensitive_field(field_name: str) -> bool:
    return bool(_SENSITIVE_RE.search(field_name or ""))


def redact_value(value: str, keep_last: int = 2) -> str:
    """Masks a value, keeping only the last `keep_last` characters visible."""
    if not value:
        return value
    v = str(value)
    if len(v) <= keep_last:
        return "*" * len(v)
    return "*" * (len(v) - keep_last) + v[-keep_last:]


def redact_for_audit(field_name: str, value: str) -> str:
    """Returns the value unchanged unless the field name matches a
    sensitive pattern, in which case it's redacted before logging."""
    if is_sensitive_field(field_name):
        return redact_value(value)
    return value


def redact_details_dict(details: dict) -> dict:
    """Applies redact_for_audit() to every key/value pair in a details
    dict before it's persisted to the audit log."""
    return {k: redact_for_audit(k, v) if isinstance(v, str) else v for k, v in details.items()}
