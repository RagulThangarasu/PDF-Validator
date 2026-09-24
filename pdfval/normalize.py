"""Text normalisation used for content comparison and title matching."""
from __future__ import annotations

import re
import unicodedata

# Always: invisible / rendering-only characters (non-breaking space, soft hyphen, zero-width space).
_INVISIBLE = str.maketrans({"\u00a0": " ", "\u00ad": "", "\u200b": ""})
# Only when content.normalize_typography = true: curly quotes and dash variants.
_TYPOGRAPHY = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2033": '"', "\u2032": "'",
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2212": "-",
})
_LEADER = re.compile(r"(\.\s*){3,}\d*$")  # TOC dot leaders (+ page number)
_NUMBERING = re.compile(r"^(\d+(\.\d+)*\.?|[A-Z]\.)\s+")


def clean(text: str, typography: bool = True) -> str:
    """NFKC (ligatures like ﬁ are glyph-level, not content) + invisible chars;
    typography=True also folds curly quotes and dash variants."""
    text = unicodedata.normalize("NFKC", text).translate(_INVISIBLE)
    if typography:
        text = text.translate(_TYPOGRAPHY)
    return re.sub(r"\s+", " ", text).strip()


def token(text: str, *, case_sensitive: bool, ignore: set[str], typography: bool = False) -> str:
    """Normalise a single word for the content diff. Punctuation is kept, so
    'down' vs 'down.' or 'details,see' vs 'details, see' are content changes.
    Returns '' for tokens that must be ignored (bullets, dot leaders)."""
    t = clean(text, typography)
    t = _LEADER.sub("", t)
    if not t or t in ignore or re.fullmatch(r"\.{3,}|…+", t):  # dot leaders only; a lone "." is content
        return ""
    return t if case_sensitive else t.lower()


def title(text: str) -> str:
    t = clean(text)
    t = _LEADER.sub("", t)
    t = _NUMBERING.sub("", t)
    return t.strip(" .:").lower()
