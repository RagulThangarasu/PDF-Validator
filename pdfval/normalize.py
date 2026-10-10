"""Text normalisation used for content comparison and title matching."""
from __future__ import annotations

import re
import unicodedata

# Always: invisible / rendering-only characters (non-breaking space, soft hyphen, zero-width space), and
# the hyphens that print exactly like "-": U+2010 hyphen, U+2011 non-breaking hyphen (AEM's “low‑speed”
# vs InDesign's “low-speed” is the same text). En / em dashes are another mark: only with normalize_typography.
_INVISIBLE = str.maketrans({"\u00a0": " ", "\u00ad": "", "\u200b": "", "\u2010": "-", "\u2011": "-"})
# Only when content.normalize_typography = true: curly quotes and dash variants.
_TYPOGRAPHY = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2033": '"', "\u2032": "'",
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2212": "-",
})
# content.ignore_quote_style = true (default): only the quotes / apostrophes ("haven’t" = "haven't")
_QUOTE_STYLE = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2033": '"',
})
# a double quote and a single quote / apostrophe are the same kind of mark to a reviewer ("BenQ.com" vs 'BenQ.com')
_QUOTE_TYPE = str.maketrans({'"': "'"})
_LEADER = re.compile(r"(\.\s*){3,}\d*$")  # TOC dot leaders (+ page number)
_NUMBERING = re.compile(r"^(\d+(\.\d+)*\.?|[A-Z]\.)\s+")


# Scripts written without spaces between words (Chinese, Japanese kana, Thai, Lao, Myanmar, Khmer,
# CJK and full-width punctuation): each character is compared as its own word, so a missing or
# changed character is reported as exactly that, not as the whole sentence changed.
NOSPACE = ("\u0E00-\u0EFF\u1000-\u109F\u1780-\u17FF\u3000-\u303F\u3040-\u30FF\u31F0-\u31FF"
           "\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF\uFF00-\uFFEF")
_NOSPACE_RE = re.compile(f"[{NOSPACE}]")


def nospace_char(c: str) -> bool:
    return bool(_NOSPACE_RE.match(c))


def join_words(words) -> str:
    """Text of consecutive words with their real spacing (no space between CJK/Thai characters),
    NFKC-folded for display (Arabic presentation forms -> letters)."""
    words = list(words)
    out = []
    for k, w in enumerate(words):
        out.append(w.text)
        nxt = words[k + 1] if k + 1 < len(words) else None
        # a line break between two CJK/Thai characters is not a space ("液晶面 / 板" reads "液晶面板")
        # continues on the next line down (glyph boxes of two lines may overlap: compare the centres)
        wrapped = nxt is not None and nxt.line != w.line and \
            (nxt.bbox[1] + nxt.bbox[3]) / 2 - (w.bbox[1] + w.bbox[3]) / 2 > 0.5 * (w.bbox[3] - w.bbox[1])
        glued = w.space_after == 0 or (w.space_after is None and wrapped
                                       and nospace_char(w.text[-1:]) and nospace_char(nxt.text[:1])) \
            or (wrapped and w.text.endswith("\u00ad"))  # "But­" | "ton’s": one word hyphenated at the line break
        if not glued:
            out.append(" ")
    return clean("".join(out), typography=False)  # keep ’ vs ' visible: it may be the difference


_WIDE = re.compile("[\u3000-\u303F\uFF00-\uFFEF]")
_KEEP_WIDTH = re.compile("[\u3000-\u303F\uFF00-\uFFEF]+|[^\u3000-\u303F\uFF00-\uFFEF]+")


def clean(text: str, typography: bool | str = True) -> str:
    """NFKC (ligatures like ﬁ are glyph-level, not content) + invisible chars;
    typography=True also folds curly quotes and dash variants, "quotes" the quotes / apostrophes only."""
    # NFKC everywhere except CJK and full-width punctuation/letters: "，" vs "," or "（" vs "(" is a
    # visible difference in Chinese/Japanese text, not a glyph variant
    text = _KEEP_WIDTH.sub(lambda m: m.group() if _WIDE.match(m.group()) else unicodedata.normalize("NFKC", m.group()),
                           text).translate(_INVISIBLE)
    if typography == "quotes":
        text = text.translate(_QUOTE_STYLE).translate(_QUOTE_TYPE)
    elif typography:
        text = text.translate(_TYPOGRAPHY).translate(_QUOTE_TYPE)
    return re.sub(r"\s+", " ", text).strip()


def token(text: str, *, case_sensitive: bool, ignore: set[str], typography: bool | str = False) -> str:
    """Normalise a single word for the content diff. Punctuation is kept, so
    'down' vs 'down.' or 'details,see' vs 'details, see' are content changes.
    Returns '' for tokens that must be ignored (bullets, dot leaders)."""
    t = clean(text, typography)
    t = _LEADER.sub("", t)
    if not t or t in ignore or re.fullmatch(r"\.{3,}|…+", t):  # dot leaders only; a lone "." is content
        return ""
    return t if case_sensitive else t.lower()


def fold_labels(doc, labels: list[str]) -> int:
    """Callout labels are one word whatever their case, colon or plural: "Tips", "TIPS:", "Tip:"
    and "TIP" are the same label (the house style differs between InDesign and AEM Guides, the
    content does not). Every occurrence is folded, not only label positions, so a word that wraps
    to the start of a line on one side only is still equal on both. Returns the number folded."""
    known = {l.lower() for l in labels}
    n = 0
    for w in doc.words:
        if not w.norm:
            continue
        base = re.sub(r"[^\w]+$", "", clean(w.text)).lower()
        if base not in known and base.endswith("s") and base[:-1] in known:
            base = base[:-1]
        if base in known:
            w.norm = f"<label:{base}>"
            n += 1
    return n


def fold_label_icons(doc) -> int:
    """A callout icon drawn as a single real character ("i" for an info circle, bold, oversized)
    right before the label it introduces - "i Note" - is part of the icon's artwork, not its
    wording: a DOM-based candidate has no such character (its icon is an image/SVG), so comparing
    it as a word always reports it missing. Taken out of the comparison (word.norm = "", as a
    bullet glyph already is) whenever it directly precedes a word folded to <label:...> by
    fold_labels: on the same page, immediately to its left and vertically overlapping it (own
    line index is unreliable here - an icon's oversized glyph often lands its own line even
    though it visually sits beside the label). The label itself still compares normally. Returns
    the number cleared."""
    n = 0
    for i, w in enumerate(doc.words):
        if i == 0 or not w.norm.startswith("<label:"):
            continue
        prev = doc.words[i - 1]
        if prev.page != w.page or not prev.norm or prev.norm.startswith("<label:"):
            continue
        overlap = min(prev.bbox[3], w.bbox[3]) - max(prev.bbox[1], w.bbox[1])
        if (len(prev.norm) == 1 and prev.norm.isalpha()
                and 0 <= w.bbox[0] - prev.bbox[2] < 1.5 * (prev.bbox[3] - prev.bbox[1]) and overlap > 0):
            prev.norm = ""
            n += 1
    return n


_CLOSE_QUOTE = ('"', "”", "’", "'", "»")


def fold_xref_pages(doc) -> int:
    """Cross-reference page numbers are the PDF template's: InDesign writes
    `see "Controls and functions" on page 12.` where AEM Guides writes `see "Controls and
    functions".` - and the number shifts with every layout change anyway (like TOC page numbers).
    Every "on page N" is taken out of the comparison - after a quoted title or a plain linked one
    ("Projection dimensions on page 17") - and the punctuation after N stays with the word before.
    Returns the number folded."""
    ws, n = doc.words, 0
    for k in range(1, len(ws) - 2):
        prev, on, page, num = ws[k - 1], ws[k], ws[k + 1], ws[k + 2]
        if not (on.norm and page.norm and num.norm) or on.norm.lower() != "on" or page.norm.lower() != "page":
            continue
        # "20for" / "58and": stage sets the page number against the next word ("on page 20for more information")
        m = re.fullmatch(r"(\d{1,4})([.,;:)]*)([A-Za-z][\w.,;:)]*)?", num.norm)
        if not m or not prev.norm:
            continue
        prev.norm += m.group(2)
        on.norm = page.norm = ""
        num.norm = m.group(3) or ""   # the glued word stays in the comparison ("for")
        n += 1
    # "(See page 12)" / "see page 12 - 14": the same reference written without a title - the page number is
    # the template's as well; "see" stays
    for k in range(1, len(ws) - 1):
        see, page, num = ws[k - 1], ws[k], ws[k + 1]
        if not (page.norm and num.norm) or page.norm.lower() != "page" or see.norm.lower().lstrip("(") not in ("see", "to"):
            continue
        m = re.fullmatch(r"(\d{1,4})([.,;:)]*)", num.norm)
        if not m:
            continue
        see.norm += m.group(2)
        page.norm = num.norm = ""
        n += 1
    return n


def title(text: str) -> str:
    t = clean(text)
    t = _LEADER.sub("", t)
    t = _NUMBERING.sub("", t)
    return t.strip(" .:").lower()
