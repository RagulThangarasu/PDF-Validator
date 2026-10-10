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


# Symbol fonts store a picture under a letter: in Wingdings 3 the arrow keys ▲ ▼ ◄ ► are the
# letters p q t u, so the text reads "Press p/q" where the page shows "Press ▲/▼". Compared as
# the symbol the reader sees.
_SYMBOL_FONTS = {
    "wingdings3": {"p": "▲", "q": "▼", "t": "◄", "u": "►"},
}


def _symbol_map(font: str) -> dict | None:
    return _SYMBOL_FONTS.get(re.sub(r"[^a-z0-9]", "", _SUBSET.sub("", font).lower()))


def _style(span) -> Style:
    family, weight, italic = parse_font(span["font"], span["flags"])
    return Style(family, weight, italic, round(span["size"], 1), f"#{span['color']:06x}")


def _line_words(line, decode: dict | None = None) -> list[tuple[str, list[float], Style, int | None]]:
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
        symbols = _symbol_map(span["font"])
        size, oy = span["size"], span["origin"][1]
        # by geometry only: the PDF's own superscript flag is a guess and marks font-change spans
        smaller = size < 0.85 * base_size
        pos = "^" if smaller and oy < base_y - 0.15 * base_size else \
              "_" if smaller and oy > base_y + 0.1 * base_size else "."
        for ch in span["chars"]:
            c = ch["c"]
            if symbols:
                c = symbols.get(c, c)
            if decode:  # a glyph of a font without a Unicode map, read back by OCR (glyphs.py)
                c = decode.get((span["font"], c), c)
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
                if cur[2] != st and not any(ch.isalnum() for ch in cur[0]):
                    # the word so far is only leading punctuation (an opening quote, a bracket) glued
                    # from a different span than the word itself ("“Pairing", quote in Regular, "Pairing"
                    # in Bold): its style must be the word's own, not the punctuation's, or a bold/plain
                    # difference on the word is missed (or wrongly reported) by whatever glued onto its front
                    cur[2] = st
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


def _rejoin(lines: list) -> list:
    """Lines of a block with fonts read back by glyphs.py: text extraction took a glyph number
    that happens to be a combining mark ("ി") for a zero-width character and ended the line after
    it, mid-row. Such a glyph gets an em of width back and the row is one line again."""
    out = []
    for line in lines:
        for span in line["spans"]:
            for ch in span["chars"]:
                x0, y0, x1, y1 = ch["bbox"]
                if x1 - x0 < 0.1 * span["size"] and not ch["c"].isspace():
                    ch["bbox"] = (x0, y0, x0 + span["size"], y1)
        if out and line["spans"] and out[-1]["spans"]:
            prev = out[-1]
            size = max(prev["spans"][-1]["size"], line["spans"][0]["size"])
            last = prev["spans"][-1]["chars"][-1]["bbox"] if prev["spans"][-1]["chars"] else prev["bbox"]
            first = line["spans"][0]["chars"][0]["bbox"] if line["spans"][0]["chars"] else line["bbox"]
            if abs(prev["spans"][-1]["origin"][1] - line["spans"][0]["origin"][1]) <= 0.2 * size \
                    and -0.2 * size <= first[0] - last[2] <= 0.6 * size:
                prev["spans"] = prev["spans"] + line["spans"]
                b = prev["bbox"]
                prev["bbox"] = (min(b[0], line["bbox"][0]), min(b[1], line["bbox"][1]),
                                max(b[2], line["bbox"][2]), max(b[3], line["bbox"][3]))
                continue
        out.append(dict(line))
    return out


_RTL = re.compile("[\u0590-\u08FF\uFB1D-\uFDFF\uFE70-\uFEFF]")


def _column_order(lines: list, width: float, tables: list, rules=None) -> list:
    """Read a page set in two columns one column at a time. Text extraction goes by height, so
    it interleaves the columns ("9. Filmmaker", then "21. Picture mode" from the right column,
    then "Switches Picture Mode ..."), and the words no longer line up with a one-column stage.
    A gutter is a vertical line in the middle of the page (30-70 % of its width) that no text
    line crosses while a good amount of text sits on both sides of it, side by side. Lines that
    cross it (a full-width heading, a note) and tables split the page into bands; inside each
    band the left column is read before the right one. Pages without such a gutter keep their order.
    rules(): the y of the page's horizontal rules across the gutter (a callable, read only when a gutter is
    found). Such a rule ends a band: a table drawn with rules only between its rows ("Possible cause |
    Remedy", no vertical lines) is read row by row - else its left column ran on into the heading under the
    table ("…been correctly selected. Blurred image", then "key. Select the correct input …")."""
    if len(lines) < 8:
        return lines
    in_table = lambda b: any(t[0] - 1 <= (b[0] + b[2]) / 2 <= t[2] + 1 and t[1] - 1 <= (b[1] + b[3]) / 2 <= t[3] + 1
                             for t in tables)
    body = [ln for ln in lines if not in_table(ln[1])]
    best = None
    for x in range(int(width * 0.3), int(width * 0.7), 2):
        left = [ln for ln in body if ln[1][2] <= x]
        right = [ln for ln in body if ln[1][0] >= x]
        cross = [ln for ln in body if ln[1][0] < x < ln[1][2]]
        lc, rc = sum(len(ln[2]) for ln in left), sum(len(ln[2]) for ln in right)
        if lc < 120 or rc < 120 or len(cross) > 0.15 * len(body):
            continue
        # side by side: the two columns share most of their height
        ly, ry = (min(ln[1][1] for ln in left), max(ln[1][3] for ln in left)), (min(ln[1][1] for ln in right), max(ln[1][3] for ln in right))
        overlap = min(ly[1], ry[1]) - max(ly[0], ry[0])
        if overlap < 0.5 * min(ly[1] - ly[0], ry[1] - ry[0]):
            continue
        score = min(lc, rc) - 40 * len(cross)
        if best is None or score > best[0]:
            best = (score, x)
    if best is None:
        return lines
    x = best[1]
    # a band ends at a line crossing the gutter, a table, a heading (clearly larger type than the page's
    # text) and at a gap across the whole page (a full-width picture between two lists): "1.-14. | 15.-25."
    # above a picture and "26.-30." below it are two bands, not one long left column
    heights = sorted(ln[1][3] - ln[1][1] for ln in lines)
    typical = heights[len(heights) // 2] if heights else 12
    heading = lambda ln: ln[1][3] - ln[1][1] >= 1.35 * typical and len(ln[2]) <= 80
    spans = lambda ln: in_table(ln[1]) or ln[1][0] < x < ln[1][2] or heading(ln)
    out, band = [], []

    cuts = sorted(rules(x)) if rules else []

    def flush():
        # lines of one row by their middle, not their top: an icon-font word ("⇥ key.") sits a little higher
        # than the text beside it and must not be read before "Select the correct input signal with the"
        row = lambda ln: round(((ln[1][1] + ln[1][3]) / 2) / (0.6 * typical))
        band.sort(key=lambda ln: (ln[1][0] >= x, row(ln), ln[1][0]))
        out.extend(band)
        band.clear()
    bottom = None
    for ln in sorted(lines, key=lambda ln: (ln[1][1], ln[1][0])):
        if spans(ln):
            flush()
            out.append(ln)
            bottom = None
            continue
        if bottom is not None and ln[1][1] - bottom > 2.5 * typical:  # nothing in either column across this gap
            flush()
        elif bottom is not None and any(bottom - 1 <= c <= ln[1][1] + 1 for c in cuts):  # a rule across both columns
            flush()
        band.append(ln)
        bottom = max(bottom if bottom is not None else ln[1][3], ln[1][3])
    flush()
    return out


# (a list number too: “3.” stored after “Power button”, a hair above it, read “Power button 3. Control keys 4.” -
# every number one item late, and the last one left over as extra text)
_MARKER_ONLY = re.compile(r"^(?:[•◦▪▫‣⁃●○■□–—\-·∙]|\(?(?:\d{1,3}|[A-Za-z])[.)])$")


def _markers_first(lines: list) -> list:
    """A bullet stored as a line of its own, a hair lower than its item's text (“•” at y 142.6 beside
    “The projector does not …” at y 141.6), is read before that text, not after it - else it lands mid-
    sentence (“…mount components/ • equipment.”) and the sentence no longer matches the other side."""
    out = list(lines)
    for k in range(1, len(out)):
        pno, b, text = out[k][0], out[k][1], out[k][2].strip()
        if not _MARKER_ONLY.match(text):
            continue
        # the item's first line: among the few lines read just before the marker, the one level with it that
        # starts right of where the marker starts (an item of several lines is read whole before its number;
        # a two-digit number may reach a little into its text: “13.” ending 3pt past where the text begins)
        for back in range(1, 6):
            if k - back < 0:
                break
            p_pno, pb, ptext = out[k - back][0], out[k - back][1], out[k - back][2].strip()
            if pno != p_pno or _MARKER_ONLY.match(ptext):
                break
            overlap = min(b[3], pb[3]) - max(b[1], pb[1])
            if overlap > 0.5 * min(b[3] - b[1], pb[3] - pb[1]):
                if b[0] < pb[0] and b[2] <= pb[0] + max(2, 0.35 * (b[2] - b[0])):
                    out.insert(k - back, out.pop(k))
                break
            if pb[3] <= b[1] or (back > 1 and out[k - back][4] != out[k - 1][4]):
                break  # a line above the marker / of another block: not this item
    return out


def _turned_order(lines: list, turned: dict) -> list:
    """Text set on its side (a table header turned 90° so “3D side-by-side” fits a narrow column) reads along
    the turn: its lines follow each other sideways, not downwards - right to left when the text runs down the
    page, left to right when it runs up. Read top-down / left-right the cell came out backwards (“by-side
    side- 3D”) and never matched the same header set upright. Only the lines of one block (one cell) are put
    in order; everything else keeps its place."""
    out, n = list(lines), 0
    while n < len(out):
        d = turned.get((out[n][0], out[n][1]))
        m = n + 1
        if d is not None:
            while m < len(out) and out[m][4] == out[n][4] and turned.get((out[m][0], out[m][1]), 0) * d > 0:
                m += 1
            out[n:m] = sorted(out[n:m], key=lambda ln: ((-ln[1][0] if d > 0 else ln[1][0]), ln[1][1] if d > 0 else -ln[1][1]))
        n = m
    return out


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
                # the cell a line starts in (its left edge), not the one its centre falls in: a cell spanning two
                # grid columns (“Sets on/off …” over Function | sub-item) holds short lines (“NOTE”, “switch.”)
                # whose centre sits in the first column and long ones whose centre sits in the second - by
                # centre the short lines were read before the cell's first line
                lx = x0 + 2
                c = min(range(len(grid)), key=lambda g: 0 if grid[g][0] - 1 <= lx <= grid[g][1] + 1
                        else min(abs(lx - grid[g][0]), abs(lx - grid[g][1]))) if grid else 0
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


# a page number as printed: "12", "- 12 -", "Page 12", "12 / 60", "12 of 60", roman "iv"
_PAGE_NO = re.compile(r"^\W*(?:page\s*)?(?:\d{1,4}|[ivxlc]{1,7})(?:\s*(?:/|of)\s*\d{1,4})?\W*$", re.I)


_GLUE = re.compile(r"(?<=;)(?=[\w(])"            # 3A;15V      -> 3A;  15V
                   r"|(?<=[\w)\]])(?==)"            # panel)= OPS= -> panel)  =
                   r"|(?<==)(?=[\w(])"               # =5V          -> =  5V
                   r"|(?<=[A-Za-z],)(?=[A-Za-z])")   # details,see  -> details,  see  (not 1,000)


def _glued_parts(t: str) -> list[str]:
    """The words a token joins with punctuation and no space; [t] when it joins none (or is a URL / path)."""
    if len(t) < 3 or "://" in t or t.startswith(("www.", "/")) or "@" in t:
        return [t]
    parts = [p for p in _GLUE.split(t) if p]
    return parts if len(parts) > 1 else [t]


def load(path: str, label: str, cfg: dict, reference: str | None = None) -> Doc:
    """reference: the other document of the comparison, to read glyphs of fonts without a
    Unicode map by their shape (glyphs.py)."""
    ecfg, ccfg = cfg["extract"], cfg["content"]
    ignore_re = [re.compile(p) for p in ecfg.get("ignore_patterns", [])]
    ignore_tokens = set(ccfg.get("ignore_tokens", []))
    case = ccfg.get("case_sensitive", True)
    # ’ vs ' (and “ ” vs ") is a font / typesetting choice, not a content change: ignored by default;
    # normalize_typography also folds the dash variants (– — vs -)
    split_glued = ccfg.get("split_glued_punctuation", True)
    typo = True if ccfg.get("normalize_typography", False) else "quotes" if ccfg.get("ignore_quote_style", True) else False

    pdf = pymupdf.open(path)
    from . import glyphs
    try:
        decode = glyphs.decoder(pdf, path, cfg, reference)
    except Exception:  # reading glyphs back is a repair: never let it stop the extraction
        decode = {}
    pages: list[PageInfo] = []
    raw_lines: list[tuple[int, tuple, str, list, int]] = []  # page, bbox, text, words, block no
    images: list[Image] = []

    for pno, page in enumerate(pdf):
        pages.append(PageInfo(page.rect.width, page.rect.height))
        clip = page.trimbox if ecfg.get("use_trimbox", True) else page.rect
        seen = set()
        turned: dict = {}  # lines set on their side (a table header turned 90° to fit a narrow column)
        first = len(raw_lines)
        data = page.get_text("rawdict", clip=clip, sort=True)
        if decode:
            glyphs.restore(page, data, decode)
        for bno, block in enumerate(data["blocks"]):
            for line in (_rejoin(block.get("lines", [])) if decode else block.get("lines", [])):
                words = _line_words(line, decode)
                if not words:
                    continue
                text = "".join(w[0] + ("" if w[3] == 0 else " ") for w in words).rstrip()
                key = (text, tuple(round(v) for v in line["bbox"]))
                if key in seen:  # duplicate overprinted text (fake bold, slugs)
                    continue
                seen.add(key)
                raw_lines.append((pno, tuple(line["bbox"]), text, words, bno))
                if abs(line.get("dir", (1, 0))[1]) > 0.9:
                    turned[(pno, tuple(line["bbox"]))] = line["dir"][1]
        found = None
        if ecfg.get("table_reading_order", True):
            from .checks import tables as tmod
            found = tmod.detect(page)
            tmod._RAW[(path, pno)] = found  # the table check reuses the detection
        if ecfg.get("column_reading_order", True):
            def rules(x, page=page):
                # a table's row rule is often drawn cell by cell (two segments meeting at the column line):
                # segments on one y that touch make one rule
                segs: dict = {}
                for d in page.get_drawings():
                    r = d["rect"]
                    if r.height <= 2 and r.width >= 20:
                        segs.setdefault(round(r.y0 * 2) / 2, []).append((r.x0, r.x1))
                out = []
                for y, xs in segs.items():
                    xs.sort()
                    lo, hi = xs[0]
                    for a0, a1 in xs[1:]:
                        if a0 <= hi + 2:
                            hi = max(hi, a1)
                        else:
                            if lo < x - 20 and hi > x + 20:
                                out.append(y)
                            lo, hi = a0, a1
                    if lo < x - 20 and hi > x + 20:
                        out.append(y)
                return out
            raw_lines[first:] = _column_order(raw_lines[first:], page.rect.width, [f[1] for f in found or []], rules)
        if found is not None:
            raw_lines[first:] = _table_order(raw_lines[first:], found)
        raw_lines[first:] = _markers_first(raw_lines[first:])
        if turned:
            raw_lines[first:] = _turned_order(raw_lines[first:], turned)
        for info in page.get_image_info():
            box = pymupdf.Rect(info["bbox"])
            r = box & clip
            if r.width > 8 and r.height > 8:
                a, b, c, d = (info.get("transform") or (1, 0, 0, 1, 0, 0))[:4]
                upright = abs(b) < 1e-6 and abs(c) < 1e-6  # rotated/sheared: box shape is not comparable
                px = info.get("width", 0) / max(info.get("height", 0), 1)
                stretch = (box.width / max(box.height, 1e-6)) / px if upright and px else 1.0
                images.append(Image(pno, tuple(r), stretch=stretch, px=(info.get("width", 0), info.get("height", 0))))

    # --- drop running headers/footers: same text (digits masked) at same y on many pages
    removed = set()
    if ecfg.get("strip_repeating", True):
        counts: dict[tuple, set] = defaultdict(set)
        for i, (pno, bbox, text, _, _) in enumerate(raw_lines):
            k = (re.sub(r"\d+", "#", normalize.clean(text).lower()), round(bbox[1] / pages[pno].height * 100))
            counts[k].add(pno)
        limit = max(3, ecfg.get("repeat_threshold", 0.3) * len(pages))
        hot = {k for k, v in counts.items() if len(v) >= limit}
        # only in the page's top / bottom band: a running header / footer sits there. In the body of the page
        # the same text at the same height on many pages is content - the cells of a timing table
        # ("640x480 | 60 | V | V" on every page), a form repeated per model - and stripping it loses rows
        strip_band = ecfg.get("strip_band", 0.15)
        in_band = lambda pno, b: b[1] >= (1 - strip_band) * pages[pno].height or b[3] <= strip_band * pages[pno].height
        def in_table(pno, b) -> bool:
            """Inside a table of the page: a row at the top / bottom of a page is content, however often the
            same row text ("720x576 | 50 | V | V") sits at that height in the document."""
            try:
                from .checks import tables as tmod
                cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
                return any(t[1][0] - 1 <= cx <= t[1][2] + 1 and t[1][1] - 1 <= cy <= t[1][3] + 1
                           for t in tmod._RAW.get((path, pno)) or [])
            except Exception:
                return False

        # a list marker on a line of its own ("•", "–", "3.", "b)") is never a running header: a list that runs
        # over many pages starts each of them with one at the same height, and stripping it turned up as a
        # bullet / list number "missing" (a page number is a bare number, without the full stop / bracket)
        marker = re.compile(r"^(?:[^\w\s]{1,2}|\(?(?:\d{1,3}|[A-Za-z])[.)])$")
        for i, (pno, bbox, text, _, _) in enumerate(raw_lines):
            if not in_band(pno, bbox) or in_table(pno, bbox) or marker.match(text.strip()):
                continue
            t, h = re.sub(r"\d+", "#", normalize.clean(text).lower()), round(bbox[1] / pages[pno].height * 100)
            # a step either side counts too: the front matter may print its page number a few points higher
            if any((t, h + d) in hot for d in (0, -1, 1)):
                removed.add(i)
        # the rest of a running header / footer: text on the same line as a removed page number, in the
        # page's top or bottom band - "5  Important safety instructions" names the chapter, so it
        # repeats on that chapter's pages only, too few to count as repeating on its own
        band = ecfg.get("header_footer_band", 0.12)
        edge = lambda pno, b: b[1] >= (1 - band) * pages[pno].height or b[3] <= band * pages[pno].height
        marks = defaultdict(list)
        for i in removed:
            pno, bbox = raw_lines[i][0], raw_lines[i][1]
            if edge(pno, bbox):
                marks[pno].append(bbox)
        beside, seen_on = [], defaultdict(set)
        for i, (pno, bbox, text, _, _) in enumerate(raw_lines):
            if i in removed or not edge(pno, bbox):
                continue
            cy = (bbox[1] + bbox[3]) / 2
            if any(abs(cy - (m[1] + m[3]) / 2) <= max(3.0, (m[3] - m[1]) / 2) for m in marks[pno]):
                key = re.sub(r"\d+", "#", normalize.clean(text).lower())
                beside.append((i, key))
                seen_on[key].add(pno)
        # only text that runs beside the page number on several pages, or names a chapter of the PDF's
        # bookmarks (the footer of a one-page chapter): a one-off line there may be content
        chapters = {re.sub(r"\d+", "#", normalize.clean(t).lower()) for _, t, _ in pdf.get_toc(simple=True)}
        removed.update(i for i, key in beside if len(seen_on[key]) >= 2 or key.strip("# ") in chapters)
    # the stripped header / footer lines, kept for the header / footer comparison (checks/footer.py);
    # a bare page number in the band counts even when it did not repeat enough to be stripped
    band_f = ecfg.get("header_footer_band", 0.12)
    furniture = []
    lowest, highest = defaultdict(float), defaultdict(lambda: 1e9)  # the outermost text line of each page
    for i, (pno, bbox, text, _, _) in enumerate(raw_lines):
        if not any(r.search(text) for r in ignore_re):
            lowest[pno], highest[pno] = max(lowest[pno], bbox[1]), min(highest[pno], bbox[3])

    def bare_page_no(pno, bbox, text, where) -> bool:
        """A page number on its own: the page's lowest (footer) / highest (header) line, not a list marker."""
        t = normalize.clean(text)
        outer = bbox[1] >= lowest[pno] - 1 if where == "footer" else bbox[3] <= highest[pno] + 1
        return outer and bool(_PAGE_NO.match(t)) and not t.endswith(".") and not re.fullmatch(r"[IVXLC]+", t)

    for i, (pno, bbox, text, wl, _) in enumerate(raw_lines):
        H = pages[pno].height
        where = "footer" if bbox[1] >= (1 - band_f) * H else "header" if bbox[3] <= band_f * H else None
        if where and (i in removed or bare_page_no(pno, bbox, text, where)) and not any(r.search(text) for r in ignore_re):
            furniture.append({"page": pno, "band": where, "text": normalize.clean(text), "bbox": tuple(bbox),
                              "words": [(t, tuple(box), st) for t, box, st, _, _ in wl], "stripped": i in removed})
    # a bare number that was not stripped is a page number only where the document prints its page numbers:
    # at the same height on at least 3 other pages (not a callout number in a drawing near the page edge)
    height = lambda f: round(f["bbox"][1] / pages[f["page"]].height * 100)
    spots = defaultdict(int)
    for f in furniture:
        if _PAGE_NO.match(f["text"]):
            spots[(f["band"], height(f))] += 1
    furniture = [f for f in furniture if f["stripped"] or sum(spots[(f["band"], height(f) + d)] for d in (-1, 0, 1)) >= 4]
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
            # a word glued to its neighbour by punctuation ("3A;15V", "panel)=", "OPS=") is compared as the
            # words it joins, with no space between them: the other PDF's "3A; 15V" then lines up word for word
            # and the missing space is one "word gap" difference, not text missing + text added
            parts = _glued_parts(t) if split_glued else [t]
            if len(parts) == 1:
                words.append(Word(t, normalize.token(t, case_sensitive=case, ignore=ignore_tokens, typography=typo),
                                  pno, tuple(box), st, li, line_start=(k == 0), space_after=gap,
                                  script=script if script.strip(".") else ""))
                continue
            x, width = box[0], (box[2] - box[0]) / max(len(t), 1)
            for n, part in enumerate(parts):
                x1 = x + width * len(part)
                words.append(Word(part, normalize.token(part, case_sensitive=case, ignore=ignore_tokens, typography=typo),
                                  pno, (x, box[1], x1, box[3]), st, li, line_start=(k == 0 and n == 0),
                                  space_after=gap if n == len(parts) - 1 else 0,
                                  script=script if script.strip(".") else ""))
                x = x1

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
        # A word cut at a line break continues at the left of ITS OWN column on a later line - not simply
        # at the next word in reading order. Across a table (or any two-column page) a "line" runs through
        # every cell of the row, so the next word in the stream belongs to the cell beside it: joining
        # blindly both leaves the real halves apart ("con-" | "nect") and glues a neighbour's word onto the
        # stub ("pass-" + "cannot" -> "pass-cannot"). The sentence then matches nothing on the web page,
        # which writes it whole, and a table cell that IS on the page is reported as data missing.
        last_on_line: dict[int, int] = {}
        for i, w in enumerate(words):
            last_on_line[w.line] = i
        for i, w in enumerate(words):
            # only a hyphen the line break itself put there: the word has to end its own line
            if not (w.norm.endswith("-") and len(w.norm) > 1 and last_on_line.get(w.line) == i):
                continue
            reach = 4 * max(1.0, w.bbox[3] - w.bbox[1])  # a few lines down, no further
            for k in range(i + 1, len(words)):
                n = words[k]
                if n.page != w.page or n.bbox[1] - w.bbox[1] > reach:
                    break
                if not n.norm or n.bbox[1] <= w.bbox[1]:
                    continue  # beside the break, not below it: another cell of the same row
                # the rest of the word starts the next line of the same column, so it can never begin
                # further right than the point the line broke at
                if n.line_start and n.bbox[0] <= w.bbox[0] and n.norm[:1].islower():
                    # keep the hyphen: right for compounds ("third-party"); a pure line-break
                    # hyphen ("config-uration") is recognised by the content check as a match
                    w.norm, n.norm = w.norm + n.norm, ""
                    break

    doc = Doc(path, label, pages, words, lines, images,
              [(lvl, t, p) for lvl, t, p in pdf.get_toc(simple=True)],
              removed_lines=len(removed), furniture=furniture)
    # where each bookmark lands on its page (y in page points; None when the PDF only names the page)
    try:
        doc.outline_to = [(e[3].get("to").y if e[3].get("kind") == pymupdf.LINK_GOTO and e[3].get("to") is not None
                           and e[3]["to"].y > 0 else None) for e in pdf.get_toc(simple=False)]
    except Exception:
        doc.outline_to = []
    doc.decoded = decode.get("__stats__") if decode else None
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
