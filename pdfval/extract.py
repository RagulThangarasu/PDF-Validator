"""PDF -> Doc model (words with style + geometry, lines, images, outline).

Uses PyMuPDF `rawdict` so every word carries the font/size/colour of the span
that drew it and a precise bounding box.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

import pymupdf

from . import normalize
from .model import Doc, Image, Line, PageInfo, Style, Word

_SUBSET = re.compile(r"^[A-Z]{6}\+")
_WEIGHTS = [
    ("thin", 100), ("extralight", 200), ("ultralight", 200), ("light", 300),
    ("book", 400), ("regular", 400), ("medium", 500), ("semibold", 600),
    ("demibold", 600), ("extrabold", 800), ("ultrabold", 800), ("bold", 700),
    ("black", 900), ("heavy", 900),
]


def parse_font(name: str, flags: int) -> tuple[str, int, bool]:
    name = _SUBSET.sub("", name)
    family, _, variant = name.partition("-")
    if not variant and "," in family:
        family, _, variant = family.partition(",")
    v = variant.lower().replace(" ", "")
    weight = next((w for tok, w in _WEIGHTS if tok in v), 400)
    if weight == 400 and flags & 16:
        weight = 700
    italic = "italic" in v or "oblique" in v or bool(flags & 2)
    return family, weight, italic


def _style(span) -> Style:
    family, weight, italic = parse_font(span["font"], span["flags"])
    return Style(family, weight, italic, round(span["size"], 1), f"#{span['color']:06x}")


def _line_words(line) -> list[tuple[str, list[float], Style, int | None]]:
    """Split a rawdict line into words; a word may cross span boundaries.
    The 4th item is the number of whitespace chars after the word (None at line end)."""
    out, chars, box, style, gap = [], [], None, None, 0
    for span in line["spans"]:
        st = _style(span)
        for ch in span["chars"]:
            c = ch["c"]
            if c.isspace():
                if chars:
                    out.append(["".join(chars), box, style, None])
                    chars, box, gap = [], None, 0
                gap = 1 if (c == "\t" or gap < 0) else gap + 1  # a tab is one separator ("1.<tab>Place")
                if c == "\t":
                    gap = -1  # stays a single gap however many spaces follow
                continue
            x0, y0, x1, y1 = ch["bbox"]
            if not chars:
                if out:
                    out[-1][3] = 1 if gap < 0 else gap
                chars, box, style, gap = [c], [x0, y0, x1, y1], st, 0
            else:
                chars.append(c)
                box = [min(box[0], x0), min(box[1], y0), max(box[2], x1), max(box[3], y1)]
    if chars:
        out.append(["".join(chars), box, style, None])
    return [tuple(w) for w in out]


def load(path: str, label: str, cfg: dict) -> Doc:
    ecfg, ccfg = cfg["extract"], cfg["content"]
    ignore_re = [re.compile(p) for p in ecfg.get("ignore_patterns", [])]
    ignore_tokens = set(ccfg.get("ignore_tokens", []))
    case = ccfg.get("case_sensitive", True)
    typo = ccfg.get("normalize_typography", False)

    pdf = pymupdf.open(path)
    pages: list[PageInfo] = []
    raw_lines: list[tuple[int, tuple, str, list, int]] = []  # page, bbox, text, words, block no
    images: list[Image] = []

    for pno, page in enumerate(pdf):
        pages.append(PageInfo(page.rect.width, page.rect.height))
        clip = page.trimbox if ecfg.get("use_trimbox", True) else page.rect
        seen = set()
        data = page.get_text("rawdict", clip=clip, sort=True)
        for bno, block in enumerate(data["blocks"]):
            for line in block.get("lines", []):
                words = _line_words(line)
                if not words:
                    continue
                text = " ".join(w[0] for w in words)
                key = (text, tuple(round(v) for v in line["bbox"]))
                if key in seen:  # duplicate overprinted text (fake bold, slugs)
                    continue
                seen.add(key)
                raw_lines.append((pno, tuple(line["bbox"]), text, words, bno))
        for info in page.get_image_info():
            r = pymupdf.Rect(info["bbox"]) & clip
            if r.width > 8 and r.height > 8:
                images.append(Image(pno, tuple(r)))

    # --- drop running headers/footers: same text (digits masked) at same y on many pages
    removed = set()
    if ecfg.get("strip_repeating", True):
        counts: dict[tuple, set] = defaultdict(set)
        for i, (pno, bbox, text, _, _) in enumerate(raw_lines):
            k = (re.sub(r"\d+", "#", normalize.clean(text).lower()), round(bbox[1] / pages[pno].height * 100))
            counts[k].add(pno)
        limit = max(3, ecfg.get("repeat_threshold", 0.3) * len(pages))
        hot = {k for k, v in counts.items() if len(v) >= limit}
        for i, (pno, bbox, text, _, _) in enumerate(raw_lines):
            k = (re.sub(r"\d+", "#", normalize.clean(text).lower()), round(bbox[1] / pages[pno].height * 100))
            if k in hot:
                removed.add(i)
    for i, (_, _, text, _, _) in enumerate(raw_lines):
        if any(r.search(text) for r in ignore_re):
            removed.add(i)

    # --- build final word/line lists in reading order
    words: list[Word] = []
    lines: list[Line] = []
    for i, (pno, bbox, text, wl, bno) in enumerate(raw_lines):
        if i in removed:
            continue
        li = len(lines)
        lines.append(Line(pno, bbox, text, max(w[2].size for w in wl), len(words), (pno, bno)))
        for k, (t, box, st, gap) in enumerate(wl):
            words.append(Word(t, normalize.token(t, case_sensitive=case, ignore=ignore_tokens, typography=typo),
                              pno, tuple(box), st, li, line_start=(k == 0), space_after=gap))

    if ccfg.get("ignore_toc_page_numbers", True):
        # "Title ........ 17": the page number shifts whenever layout/page size differs.
        # ("Title.....17" glued together is already stripped by normalize.token.)
        for ln in lines:
            if re.search(r"\.{4,}", ln.text):
                last = ln.first_word
                while last + 1 < len(words) and words[last + 1].line == words[ln.first_word].line:
                    last += 1
                if words[last].text.isdigit():
                    words[last].norm = ""

    if ccfg.get("dehyphenate", True):
        for i in range(len(words) - 1):
            w, n = words[i], words[i + 1]
            if (w.norm.endswith("-") and len(w.norm) > 1 and n.line_start
                    and n.norm[:1].islower()):
                # keep the hyphen: right for compounds ("third-party"); a pure line-break
                # hyphen ("config-uration") is recognised by the content check as a match
                w.norm, n.norm = w.norm + n.norm, ""

    doc = Doc(path, label, pages, words, lines, images,
              [(lvl, t, p) for lvl, t, p in pdf.get_toc(simple=True)],
              removed_lines=len(removed))
    _measure(doc)
    return doc


def _measure(doc: Doc) -> None:
    """Body font size and per-parity content box (left/right margins)."""
    sizes = Counter()
    for w in doc.words:
        sizes[w.style.size] += len(w.text)
    doc.body_size = sizes.most_common(1)[0][0] if sizes else 10.0

    lefts: dict[int, Counter] = {0: Counter(), 1: Counter()}
    rights: dict[int, list] = {0: [], 1: []}
    for ln in doc.lines:
        if abs(ln.size - doc.body_size) < 0.6 and len(ln.text) > 3:
            lefts[ln.page % 2][round(ln.bbox[0])] += len(ln.text)
            rights[ln.page % 2].append(ln.bbox[2])
    for pno, p in enumerate(doc.pages):
        par = pno % 2
        lc = lefts[par] or lefts[1 - par]
        rc = sorted(rights[par] or rights[1 - par])
        p.left = lc.most_common(1)[0][0] if lc else p.width * 0.1
        p.right = rc[int(len(rc) * 0.95)] if rc else p.width * 0.9
