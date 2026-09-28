"""PDF -> Doc model (words with style + geometry, lines, images, outline).

Uses PyMuPDF `rawdict` so every word carries the font/size/colour of the span
that drew it and a precise bounding box.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

import pymupdf

import unicodedata

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
    The 4th item is the number of whitespace chars after the word (None at line end, 0 = glued).
    In scripts written without spaces (Chinese, Japanese, Thai, ...) every character is a word
    of its own; combining marks (Thai vowels and tones) stay with their base character."""
    out: list[list] = []
    cur: list | None = None  # [text, box, style, gap, script]
    # the line's main baseline and size (most characters): a smaller span above / below it is
    # a superscript / subscript ("Cr+6" with "+6" raised, "H2O" with "2" lowered)
    weight: Counter = Counter()
    for span in line["spans"]:
        weight[(round(span["origin"][1], 1), round(span["size"], 1))] += len(span["chars"])
    (base_y, base_size) = weight.most_common(1)[0][0] if weight else (0.0, 0.0)
    gap = 0  # whitespace seen since the last word (-1 = a tab)
    for span in line["spans"]:
        st = _style(span)
        size, oy = span["size"], span["origin"][1]
        # by geometry only: the PDF's own superscript flag is a guess and marks font-change spans
        smaller = size < 0.85 * base_size
        pos = "^" if smaller and oy < base_y - 0.15 * base_size else \
              "_" if smaller and oy > base_y + 0.1 * base_size else "."
        for ch in span["chars"]:
            c = ch["c"]
            if c.isspace():
                if cur:
                    out.append(cur)
                    cur = None
                gap = 1 if (c == "\t" or gap < 0) else gap + 1  # a tab is one separator ("1.<tab>Place")
                if c == "\t":
                    gap = -1  # stays a single gap however many spaces follow
                continue
            x0, y0, x1, y1 = ch["bbox"]
            single = normalize.nospace_char(c)
            if cur and unicodedata.category(c) == "Mn":  # combining mark: part of the current character
                glue = True
            else:
                glue = cur is not None and not single and not normalize.nospace_char(cur[0][-1])
            if glue:
                cur[0] += c
                cur[4] += pos
                b = cur[1]
                cur[1] = [min(b[0], x0), min(b[1], y0), max(b[2], x1), max(b[3], y1)]
                continue
            if cur:  # a script change or a single character: glued to the previous word
                cur[3] = 0
                out.append(cur)
            elif out:
                out[-1][3] = 1 if gap < 0 else gap
            cur, gap = [c, [x0, y0, x1, y1], st, None, pos], 0
    if cur:
        out.append(cur)
    for w in out[:-1]:
        if w[3] is None:
            w[3] = 1
    if out:
        out[-1][3] = None
    return [tuple(w) for w in out]


_RTL = re.compile("[\u0590-\u08FF\uFB1D-\uFDFF\uFE70-\uFEFF]")


def _table_order(lines: list, found: list) -> list:
    """Read every table row by row, cell by cell (right to left in Arabic/Hebrew tables), and
    top to bottom inside a cell. Text extraction goes by height, so a label that wraps over two
    lines ("液晶面 / 板") comes after the rest of its row on one side and before it on the other,
    and the two sides no longer line up. Lines outside tables keep their order."""
    if not found:
        return lines
    def where(ln):
        x0, y0, x1, y1 = ln[1]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        for k, (_, tb, rows, grid) in enumerate(found):
            if tb[0] - 1 <= cx <= tb[2] + 1 and tb[1] - 1 <= cy <= tb[3] + 1:
                r = next((n for n, (rb, _) in enumerate(rows) if rb[1] - 1 <= cy <= rb[3] + 1), None)
                c = min(range(len(grid)), key=lambda g: 0 if grid[g][0] - 1 <= cx <= grid[g][1] + 1
                        else min(abs(cx - grid[g][0]), abs(cx - grid[g][1]))) if grid else 0
                return k, (r if r is not None else len(rows)), c
        return None
    placed = [where(ln) for ln in lines]
    rtl = {}
    for ln, w in zip(lines, placed):
        if w:
            rtl[w[0]] = rtl.get(w[0], 0) + (1 if _RTL.search(ln[2]) else -1)
    out, done = [], set()
    for n, (ln, w) in enumerate(zip(lines, placed)):
        if w is None:
            out.append(ln)
        elif w[0] not in done:
            done.add(w[0])
            flip = rtl.get(w[0], 0) > 0
            members = [(p, m) for m, p in zip(lines, placed) if p and p[0] == w[0]]
            members.sort(key=lambda pm: (pm[0][1], -pm[0][2] if flip else pm[0][2], pm[1][1][1], pm[1][1][0]))
            out.extend(m for _, m in members)
    return out


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
        first = len(raw_lines)
        data = page.get_text("rawdict", clip=clip, sort=True)
        for bno, block in enumerate(data["blocks"]):
            for line in block.get("lines", []):
                words = _line_words(line)
                if not words:
                    continue
                text = "".join(w[0] + ("" if w[3] == 0 else " ") for w in words).rstrip()
                key = (text, tuple(round(v) for v in line["bbox"]))
                if key in seen:  # duplicate overprinted text (fake bold, slugs)
                    continue
                seen.add(key)
                raw_lines.append((pno, tuple(line["bbox"]), text, words, bno))
        if ecfg.get("table_reading_order", True):
            from .checks import tables as tmod
            found = tmod.detect(page)
            tmod._RAW[(path, pno)] = found  # the table check reuses the detection
            raw_lines[first:] = _table_order(raw_lines[first:], found)
        for info in page.get_image_info():
            box = pymupdf.Rect(info["bbox"])
            r = box & clip
            if r.width > 8 and r.height > 8:
                a, b, c, d = (info.get("transform") or (1, 0, 0, 1, 0, 0))[:4]
                upright = abs(b) < 1e-6 and abs(c) < 1e-6  # rotated/sheared: box shape is not comparable
                px = info.get("width", 0) / max(info.get("height", 0), 1)
                stretch = (box.width / max(box.height, 1e-6)) / px if upright and px else 1.0
                images.append(Image(pno, tuple(r), stretch=stretch))

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
        for k, (t, box, st, gap, script) in enumerate(wl):
            words.append(Word(t, normalize.token(t, case_sensitive=case, ignore=ignore_tokens, typography=typo),
                              pno, tuple(box), st, li, line_start=(k == 0), space_after=gap, script=script if script.strip(".") else ""))

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
