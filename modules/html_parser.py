
"""
modules/html_parser.py
Parses uploaded .html/.htm documents into the shape consumed by
run_pdf_intelligence() -- mirrors how parse_txt_file()'s output is used
in app2.py (parsed -> run_pdf_intelligence(parsed)).

ASSUMPTION: the exact dict shape below is inferred from usage, not from
parse_txt_file's source. Share modules/parsing.py's parse_txt_file() to
confirm/adjust field names if this doesn't match exactly.

Uses BeautifulSoup4 if available (handles nested tags, tables, entities
correctly); falls back to a regex-based tag stripper if bs4 isn't
installed, so this doesn't hard-fail on a missing dependency.
"""

import re

try:
    from bs4 import BeautifulSoup
    _BS4_AVAILABLE = True
except ImportError:
    _BS4_AVAILABLE = False


def _strip_tags_fallback(html: str) -> str:
    """Regex-based fallback text extraction if beautifulsoup4 isn't installed."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|tr|li|h[1-6])>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_text(html: str) -> str:
    if _BS4_AVAILABLE:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = soup.get_text(separator="\n")
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
    return _strip_tags_fallback(html)


def parse_html_file(file_bytes: bytes, filename: str) -> dict:
    """
    Returns the same shape expected by run_pdf_intelligence() --
    mirrors parse_txt_file()'s usage pattern in app2.py.
    """
    try:
        html = file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        html = file_bytes.decode("latin-1", errors="replace")

    full_text = _extract_text(html)

    return {
        "full_text": full_text,
        "filename":  filename,
        "source":    "html",
    }
