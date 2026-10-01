"""Text drawn in fonts that have no Unicode map.

A PDF shows Chinese / Japanese / Korean text through CID fonts; with the "Identity" ordering the
codes in the page are glyph numbers, and only the font's ToUnicode table says which character each
one is. When that table is missing or covers a few glyphs only, the text extracts as the glyph
numbers ("竵냉╈乄" where the page shows "包裝內容"): every word differs from the stage, the diff
reports the whole manual as changed and the viewer cannot align the two documents.

The glyphs are read back from the page: each page with such text is OCR'd once with character
boxes, every unmapped glyph takes the character read at its place, and each glyph (font + code)
is decided by a vote over all its occurrences in the document, so one misread does not change it.
"""
from __future__ import annotations

import html
import unicodedata
import re
import shutil
import subprocess
import tempfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pymupdf

_SUBSET = re.compile(r"^[A-Z]{6}\+")
_CACHE: dict[tuple, dict] = {}

# script of a font by its name -> tesseract language
_LANG_HINTS = [
    (r"tc\b|-tc|hant|\bhk|mingliu|pmingliu|jhenghei|dfkai|kaiu|big5|b5-h|ming", "chi_tra"),
    (r"sc\b|-sc|hans|simsun|simhei|yahei|songti|\bgb|fangsong|kaiti|dengxian", "chi_sim"),
    (r"jp\b|-jp|gothic|mincho|meiryo|yugo|hiragino|kozgo|kozmin|msgothic|ipa", "jpn"),
    (r"kr\b|-kr|malgun|gulim|batang|dotum|nanum|gungsuh", "kor"),
]
_FULLWIDTH = {c: chr(ord(c) + 0xFEE0) for c in ",:;()!?"}


def _base(name: str) -> str:
    return _SUBSET.sub("", name or "")


def unmapped_fonts(pdf) -> set[str]:
    """CID fonts whose codes are glyph numbers ("Identity" ordering): only a ToUnicode table can
    say which character each glyph is. Base names, as text extraction reports them."""
    out: set[str] = set()
    seen = set()
    for page in pdf:
        for f in page.get_fonts(full=True):
            xref, subtype = f[0], f[2]
            if subtype != "Type0" or xref in seen:
                continue
            seen.add(xref)
            m = re.search(r"(\d+) 0 R", pdf.xref_get_key(xref, "DescendantFonts")[1] or "")
            if not m:
                continue
            dx = int(m.group(1))
            if "Identity" in (pdf.xref_get_key(dx, "CIDSystemInfo/Ordering")[1] or ""):
                out.add(_base(pdf.xref_get_key(dx, "BaseFont")[1].lstrip("/")))
    return out


def _suspects(pdf, fonts: set) -> tuple[list[set], set]:
    """The fonts to read from the page: those whose characters the text trace reports as U+FFFD
    (no Unicode; text extraction shows the glyph number instead) at least half the time. Their
    ToUnicode is missing or broken - one naming a handful of glyphs maps some of them to the
    wrong character ("維" where the page shows "可") - so none of their characters is trusted.
    Also returns per page the origins of the unmapped characters."""
    pos: list[set] = []
    total, unmapped = Counter(), Counter()
    for page in pdf:
        here = set()
        for sp in page.get_texttrace():
            f = sp["font"]
            for c in sp["chars"]:
                if c[0] < 0 or chr(c[0]).isspace():
                    continue
                total[f] += 1
                if c[0] == 0xFFFD:
                    unmapped[f] += 1
                    here.add((round(c[2][0], 1), round(c[2][1], 1)))
        pos.append(here)
    # only fonts that are mostly unreadable: a stray unmapped glyph in a working font (an Arabic
    # ligature, a ﬁ) is one shaped glyph for several characters, not a character to read back
    distrust = {f for f in total if unmapped[f] >= 0.5 * total[f] and unmapped[f] >= 5}
    return pos, distrust


def _trace_lines(page, raw: dict, distrust: set) -> list[list[tuple]]:
    """The page's characters as lines of (font, code, bbox, origin, size, unreadable), left to
    right. Geometry from the text trace: text extraction gives an unmapped glyph its glyph
    number as the character, and a number that happens to be a combining mark ("ി", "ꦼ") gets
    no width and ends the line there - the trace keeps every glyph's real box. The code is the
    character text extraction reports at that spot, so it keys the same glyph there."""
    codes = {}
    for block in raw["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                for ch in span["chars"]:
                    codes[(span["font"], round(ch["origin"][0], 1), round(ch["origin"][1], 1))] = ch["c"]
    chars = []
    for sp in page.get_texttrace():
        f, size = sp["font"], sp["size"]
        for ucs, gid, origin, bbox in sp["chars"]:
            code = codes.get((f, round(origin[0], 1), round(origin[1], 1)))
            if code is None:
                code = chr(gid) if ucs == 0xFFFD else chr(ucs) if ucs >= 0 else None
            if code is None or code.isspace():
                continue
            box = tuple(bbox)
            if box[2] - box[0] < 0.1 * size:
                box = (box[0], box[1], box[0] + size, box[3])
            chars.append((f, code, box, tuple(origin), size, f in distrust))
    chars.sort(key=lambda c: (c[3][1], c[3][0]))
    rows: list[list] = []
    for c in chars:  # same baseline
        if rows and abs(c[3][1] - rows[-1][0][3][1]) <= 0.3 * max(c[4], rows[-1][0][4]):
            rows[-1].append(c)
        else:
            rows.append([c])
    out = []
    for row in rows:  # split at a wide gap (columns, table cells)
        row.sort(key=lambda c: c[2][0])
        cur = [row[0]]
        for c in row[1:]:
            if c[2][0] - cur[-1][2][2] > 1.5 * max(c[4], cur[-1][4]):
                out.append(cur)
                cur = []
            cur.append(c)
        out.append(cur)
    return out


def _is_bad(pno: int, font: str, ch: dict, suspects) -> bool:
    _, distrust = suspects
    return not ch["c"].isspace() and font in distrust


def _lang(fonts, cfg_lang: str | None) -> str:
    have = set(_langs())
    if cfg_lang and cfg_lang != "auto":
        want = cfg_lang.split("+")
    else:
        names = " ".join(fonts).lower()
        want = [lang for pat, lang in _LANG_HINTS if re.search(pat, names)][:1] or ["chi_tra", "chi_sim"]
        want.append("eng")
    return "+".join(x for x in want if x in have)


_LANGS: list[str] | None = None


def _langs() -> list[str]:
    global _LANGS
    if _LANGS is None:
        try:
            r = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=20)
            _LANGS = [x.strip() for x in r.stdout.splitlines()[1:] if x.strip()]
        except Exception:
            _LANGS = []
    return _LANGS


def _ocr_chars(path: str, pno: int, lang: str, dpi: int, clip=None, psm: int = 3) -> list[tuple[str, tuple, float]]:
    """Characters tesseract reads on a page (or the clip of it): (char, bbox in points, confidence)."""
    with tempfile.TemporaryDirectory() as tmp:
        png = Path(tmp) / "p.png"
        with pymupdf.open(path) as pdf:
            pdf[pno].get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, clip=clip).save(png)
        try:
            subprocess.run(["tesseract", str(png), str(Path(tmp) / "o"), "-l", lang, "--psm", str(psm),
                            "-c", "hocr_char_boxes=1", "hocr"], capture_output=True, timeout=300)
            hocr = (Path(tmp) / "o.hocr").read_text(encoding="utf-8")
        except Exception:
            return []
    k = 72.0 / dpi
    ox, oy = (clip[0], clip[1]) if clip else (0.0, 0.0)
    out = []
    for x0, y0, x1, y1, conf, ch in re.findall(
            r"x_bboxes (\d+) (\d+) (\d+) (\d+); x_conf ([\d.]+)'>([^<]*)", hocr):
        ch = html.unescape(ch).strip()
        if len(ch) == 1:
            out.append((ch, (ox + int(x0) * k, oy + int(y0) * k, ox + int(x1) * k, oy + int(y1) * k), float(conf)))
    return out


def _norm_read(ch: str, box, size: float) -> str:
    if ch in _FULLWIDTH and (box[2] - box[0]) >= 0.75 * size:
        return _FULLWIDTH[ch]  # a full-width comma / colon / bracket read as the ASCII one
    return ch


def _ocr_glyph(path: str, pno: int, box, lang: str, dpi: int = 600) -> str | None:
    """One glyph cut out of its page, read as a single character."""
    with tempfile.TemporaryDirectory() as tmp:
        png = Path(tmp) / "g.png"
        x0, y0, x1, y1 = box
        pad = 0.25 * max(x1 - x0, y1 - y0)
        with pymupdf.open(path) as pdf:
            pdf[pno].get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY,
                                clip=pymupdf.Rect(x0 - pad, y0 - pad, x1 + pad, y1 + pad)).save(png)
        try:
            r = subprocess.run(["tesseract", str(png), "-", "-l", lang, "--psm", "10"],
                               capture_output=True, text=True, timeout=60)
        except Exception:
            return None
    t = re.sub(r"\s+", "", r.stdout)
    if len(t) > 1:  # "拿;": specks around the glyph read as punctuation
        core = [c for c in t if c.isalnum()]
        t = core[0] if len(core) == 1 else t
    return t if len(t) == 1 else None


def _align(glyphs: list, reads: list, known: dict) -> list:
    """Pair a line's glyphs with the OCR characters read on it, both in reading order (an
    edit-distance alignment). OCR character boxes drift by up to a character, so position
    only guides: a glyph whose character is known (a mapped one, or one decided on other
    lines) pairs with a read of that character, the unknown ones fill the places between.
    Each read is used once, so two glyphs never both take the same neighbouring character."""
    n, m = len(glyphs), len(reads)
    if not n or not m:
        return [None] * n
    size = max(g[3] for g in glyphs) or 1.0
    gx = [(g[2][0] + g[2][2]) / 2 for g in glyphs]
    rx = [(r[1][0] + r[1][2]) / 2 for r in reads]
    want = [known.get((g[0], g[1])) if g[4] else g[1] for g in glyphs]
    gap = 0.8
    INF = float("inf")
    cost = [[INF] * (m + 1) for _ in range(n + 1)]
    back = [[0] * (m + 1) for _ in range(n + 1)]
    cost[0][0] = 0.0
    for i in range(n + 1):
        for j in range(m + 1):
            c = cost[i][j]
            if c == INF:
                continue
            if i < n and j < m:
                d = abs(gx[i] - rx[j]) / size
                if d < 2.0:
                    same = want[i] is not None and want[i] in (reads[j][0], _norm_read(reads[j][0], glyphs[i][2], size))
                    d = 0.3 * d + (0.0 if same else 1.0 if want[i] is not None else 0.35)
                    if c + d < cost[i + 1][j + 1]:
                        cost[i + 1][j + 1], back[i + 1][j + 1] = c + d, 1
            if i < n and c + gap < cost[i + 1][j]:
                cost[i + 1][j], back[i + 1][j] = c + gap, 2
            if j < m and c + gap < cost[i][j + 1]:
                cost[i][j + 1], back[i][j + 1] = c + gap, 3
    out: list = [None] * n
    i, j = n, m
    while i or j:
        k = back[i][j]
        if k == 1:
            i, j = i - 1, j - 1
            out[i] = reads[j]
        elif k == 2:
            i -= 1
        else:
            j -= 1
    return out


_GRID = 32


def _bitmap(page, box, origin, size: float):
    """A glyph as a _GRID x _GRID ink map of its em box (advance width x the font size, on the
    baseline; a little in from the sides, where a tightly set neighbour reaches in), blurred so
    a bolder weight or a slightly different position of the same character still compares
    close, while a raised small "°" stays apart from a low "。"."""
    import numpy as np
    from PIL import Image as PImage, ImageFilter
    x0, x1 = box[0], box[2] if box[2] - box[0] > 0.1 * size else box[0] + size  # no advance width: an em
    inset = 0.04 * (x1 - x0)
    y0, y1 = origin[1] - 0.9 * size, origin[1] + 0.2 * size
    zoom = 64 / max(size, 1.0)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=pymupdf.Rect(x0 + inset, y0, x1 - inset, y1),
                          colorspace=pymupdf.csGRAY)
    g = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).astype(np.float32)
    if g.size == 0:
        return None
    # ink = what differs from the background: dark text on white, white text on a dark bar alike
    edge = np.concatenate([g[0], g[-1], g[:, 0], g[:, -1]])
    a = np.abs(g - float(np.median(edge)))
    if (a > 60).sum() < 4:
        return None
    im = PImage.fromarray(a.clip(0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(2.4))
    v = np.asarray(im.resize((_GRID, _GRID), PImage.BILINEAR), np.float32).ravel()
    v -= v.mean()
    n = float(np.linalg.norm(v))
    return v / n if n else None


def _family(font: str) -> str:
    """Typeface of a font name: "NotoSansTC-Bold" and "NotoSansTC-Regular" are one family."""
    return re.sub(r"[-,_ ].*$", "", _base(font)).lower()


def _blank(pdf, occ) -> bool:
    """No ink in the glyph's em box."""
    pno, _, box, origin, size = occ
    x0, x1 = box[0], box[2] if box[2] - box[0] > 0.1 * size else box[0] + size
    inset = 0.1 * (x1 - x0)
    pix = pdf[pno].get_pixmap(matrix=pymupdf.Matrix(4, 4), colorspace=pymupdf.csGRAY,
                              clip=pymupdf.Rect(x0 + inset, origin[1] - 0.85 * size, x1 - inset, origin[1] + 0.15 * size))
    return pix.width > 0 and pix.height > 0 and min(pix.samples) > 200 and max(pix.samples) - min(pix.samples) < 30


def _atlas(ref: str):
    """Every character of the reference document once (its largest drawing), as glyph bitmaps:
    (chars, font families, matrix). The reference is the other document of the comparison: the
    same typeface drawn with a working Unicode map."""
    import numpy as np
    best: dict[tuple, tuple] = {}
    with pymupdf.open(ref) as pdf:
        sus = _suspects(pdf, unmapped_fonts(pdf))
        for pno, page in enumerate(pdf):
            for block in page.get_text("rawdict")["blocks"]:
                for line in block.get("lines", []):
                    for span in line["spans"]:
                        bold = "bold" in span["font"].lower() or bool(span["flags"] & 16)
                        for ch in span["chars"]:
                            c = ch["c"]
                            if c.isspace() or unicodedata.category(c).startswith("C") \
                                    or _is_bad(pno, span["font"], ch, sus):
                                continue
                            k = (c, bold, _family(span["font"]))
                            if k not in best or span["size"] > best[k][3]:
                                best[k] = (pno, tuple(ch["bbox"]), tuple(ch["origin"]), span["size"])
        chars, fams, rows = [], [], []
        for (c, bold, fam), (pno, box, origin, size) in best.items():
            v = _bitmap(pdf[pno], box, origin, size)
            if v is not None:
                chars.append(c), fams.append(fam), rows.append(v)
    return chars, fams, (np.stack(rows) if rows else None)


def _by_shape(pdf, glyphs_at: dict, table: dict, ref: str) -> tuple[int, set, set]:
    """Correct OCR misreads by shape: a glyph that looks like a character of the reference
    document, clearly more than like any other, is that character. A character the reference
    does not have keeps its OCR read, so a real difference is never matched away. Against the
    same typeface a close match decides; against another typeface (the reference sets the text
    in Noto, the prod table in MingLiU) near twins (未 / 末) look alike, so there the match must
    stand well clear of the runner-up and of the OCR read. Returns how many glyphs changed, the
    glyphs the shape settles (character confirmed or corrected) and those whose OCR read it
    rules out (the read character, drawn in the same typeface in the reference, looks nothing
    like the glyph)."""
    import numpy as np
    chars, fams, M = _atlas(ref)
    if M is None:
        return 0, set(), set()
    fams = np.array(fams)
    changed, settled, wrong = 0, set(), set()
    for k, (pno, font, box, origin, size) in glyphs_at.items():
        v = _bitmap(pdf[pno], box, origin, size)
        if v is None:
            continue
        same = fams == _family(font)
        # (candidates, least similarity, lead over the runner-up, lead over the OCR read)
        tier = (same, 0.95, 0.03, 0.04) if same.any() else (np.ones(len(chars), bool), 0.9, 0.05, 0.06)
        pool, min_sim, lead, over_ocr = tier
        sims = np.where(pool, M @ v, -1.0)
        best: dict[str, float] = {}  # per character, whichever weight it is drawn in
        for n in np.argsort(-sims)[:12]:
            best.setdefault(chars[n], float(sims[n]))
        ranked = sorted(best.items(), key=lambda kv: -kv[1])
        top, score = ranked[0]
        runner = ranked[1][1] if len(ranked) > 1 else -1.0
        cur = table.get(k)
        if score >= min_sim and cur == top:
            settled.add(k)
        mine = [n for n, c in enumerate(chars) if c == cur and same[n]]
        if mine and float(sims[mine].max()) < 0.6:
            wrong.add(k)
        if score < min_sim or cur == top or score - runner < lead:
            continue
        # the OCR read stands when it looks about as alike (a near-identical pair of characters)
        now = [n for n, c in enumerate(chars) if c == cur and pool[n]]
        if now and float(sims[now].max()) >= score - over_ocr:
            continue
        table[k] = top
        settled.add(k)
        wrong.discard(k)
        changed += 1
    return changed, settled, wrong


def decoder(pdf, path: str, cfg: dict, reference: str | None = None) -> dict:
    """{(font, code): character} for the glyphs of fonts without a Unicode map, read by OCR.
    Empty when the document has none, OCR is switched off or tesseract is missing."""
    ecfg = cfg.get("extract", {})
    if not ecfg.get("decode_unmapped_glyphs", True):
        return {}
    fonts = unmapped_fonts(pdf)
    if not fonts or not shutil.which("tesseract"):
        return {}
    key = (path, Path(path).stat().st_mtime, tuple(sorted(fonts)), reference)
    if key in _CACHE:
        return _CACHE[key]
    raw = [page.get_text("rawdict") for page in pdf]
    sus = _suspects(pdf, fonts)
    if not sus[1]:
        _CACHE[key] = {}
        return {}
    # every line holding an unmapped glyph: all its glyphs (the mapped ones anchor the alignment)
    todo: dict[int, list] = defaultdict(list)
    at: dict[tuple, list] = {}  # glyph -> where it is drawn
    for pno, page in enumerate(pdf):
        for line in _trace_lines(page, raw[pno], sus[1]):
            glyphs = [(f, code, box, size, bad) for f, code, box, origin, size, bad in line]
            for f, code, box, origin, size, bad in line:
                if bad:
                    at.setdefault((f, code), []).append((pno, f, box, origin, size))
            if any(g[4] for g in glyphs):
                box = (min(g[2][0] for g in glyphs), min(g[2][1] for g in glyphs),
                       max(g[2][2] for g in glyphs), max(g[2][3] for g in glyphs))
                todo[pno].append((box, glyphs))
    if not todo:
        _CACHE[key] = {}
        return {}
    # a glyph that draws nothing is a space (the font's space glyph, unmapped like the rest)
    blank = {k for k, occ in at.items() if _blank(pdf, max(occ, key=lambda o: o[4]))}
    todo = {p: [(box, [g for g in gs if (g[0], g[1]) not in blank]) for box, gs in page_lines]
            for p, page_lines in todo.items()}
    at = {k: v for k, v in at.items() if k not in blank}
    lang = _lang(sus[1], ecfg.get("ocr_lang"))
    if not lang:
        return {}
    dpi = ecfg.get("decode_dpi", 300)
    with ThreadPoolExecutor(max_workers=4) as ex:
        reads = dict(zip(todo, ex.map(lambda p: _ocr_chars(path, p, lang, dpi), todo)))
    lines = []
    for pno, page_lines in todo.items():
        for (x0, y0, x1, y1), glyphs in page_lines:
            h = y1 - y0
            on = sorted((r for r in reads[pno]
                         if y0 - 0.25 * h <= (r[1][1] + r[1][3]) / 2 <= y1 + 0.25 * h
                         and r[1][2] > x0 - h and r[1][0] < x1 + h), key=lambda r: r[1][0])
            lines.append((glyphs, on))
    # first the lines read with as many characters as they have glyphs (paired in order), then
    # every line aligned against what those decided, twice
    table: dict = {}
    for rnd in range(3):
        votes: dict[tuple, Counter] = defaultdict(Counter)
        for glyphs, on in lines:
            if rnd == 0:
                if len(on) != len(glyphs):
                    continue
                hits = on
            else:
                hits = _align(glyphs, on, table)
            for g, hit in zip(glyphs, hits):
                font, code, box, size, bad = g
                if bad and hit:
                    ch = _norm_read(hit[0], box, size)
                    wide = (box[2] - box[0]) >= 0.75 * size
                    # a Latin letter read for a full-width (CJK) glyph is a misread of a character
                    w = 0.3 if wide and ch.isascii() and ch.isalnum() else 1.0
                    votes[(font, code)][ch] += w * (1 + hit[2] / 100)
        table = {**table, **{k: v.most_common(1)[0][0] for k, v in votes.items()}} if rnd < 2 else \
            {k: v.most_common(1)[0][0] for k, v in votes.items()}
    table.update({k: " " for k in blank})
    # the shape of each glyph against the other document's characters (same typeface there)
    first = {k: max(v, key=lambda o: o[4]) for k, v in at.items()}
    shaped, settled, wrong = 0, set(), set()
    if reference and Path(reference).suffix.lower() == ".pdf" and Path(reference).exists():
        try:
            shaped, settled, wrong = _by_shape(pdf, first, table, reference)
        except Exception:
            shaped, settled, wrong = 0, set(), set()
    for k in wrong:  # a read the shape rules out does not count
        votes[k].pop(table.get(k), None)
    # glyphs the shape did not settle and read once, read differently or not at all: read again,
    # their line cut out of the page and read in the document's own script only (no Latin letters
    # mistaken for strokes) at a size the reader likes, and the glyph cut out on its own; those
    # reads count as several occurrences
    where = {}
    for glyphs, _ in lines:
        for g in glyphs:
            if g[4] and ((g[0], g[1]) not in where or g[3] > where[(g[0], g[1])][3]):
                where[(g[0], g[1])] = g
    unsure = []
    for k, g in where.items():
        if k in settled:
            continue
        v = votes.get(k)
        if not v or k in wrong:
            unsure.append(k)
            continue
        (top, n), total = v.most_common(1)[0], sum(v.values())
        if n < 0.6 * total or total < 1.9 or (top.isascii() and top.isalnum() and (g[2][2] - g[2][0]) >= 0.75 * g[3]):
            unsure.append(k)
    pages = {k: first[k][0] for k in where}
    single_lang = lang.split("+")[0]
    doubt = set(unsure)
    again = [(p, box, gs) for p, page_lines in todo.items() for box, gs in page_lines
             if any(g[4] and (g[0], g[1]) in doubt for g in gs)]
    clip = lambda b: pymupdf.Rect(b[0] - 3, b[1] - 2, b[2] + 3, b[3] + 2)
    line_dpi = lambda b: int(min(800, max(300, 72 * 40 / max(b[3] - b[1], 1))))  # text ~40 px high
    with ThreadPoolExecutor(max_workers=6) as ex:
        alone = list(ex.map(lambda k: _ocr_glyph(path, pages[k], where[k][2], single_lang), unsure))
        reread = list(ex.map(lambda t: _ocr_chars(path, t[0], single_lang, line_dpi(t[1]), clip(t[1]), psm=7), again))
    for k, ch in zip(unsure, alone):
        if ch:
            votes[k][_norm_read(ch, where[k][2], where[k][3])] += 2.0
    for (_, _, gs), on in zip(again, reread):
        for g, hit in zip(gs, _align(gs, sorted(on, key=lambda r: r[1][0]), table)):
            if g[4] and hit and (g[0], g[1]) in doubt:
                votes[(g[0], g[1])][_norm_read(hit[0], g[2], g[3])] += 2.5
    for k in doubt:
        if votes.get(k):
            table[k] = votes[k].most_common(1)[0][0]
        elif k in wrong:
            table[k] = "\ufffd"  # unreadable: shown as such rather than as a character it is not
    bad = [(g[0], g[1]) for lines in todo.values() for _, gs in lines for g in gs if g[4]]
    table["__stats__"] = {"fonts": sorted(sus[1]), "glyphs": len(set(bad)) + len(blank), "decoded": len(table), "by_shape": shaped,
                          "chars": len(bad), "lang": lang}
    _CACHE[key] = table
    return table


def restore(page, data: dict, decode: dict) -> None:
    """Put back the glyphs of the read-back fonts that text extraction dropped (a glyph number it
    cannot use as a character: the "≧" of "Ra ≧ 95" is drawn but missing from the text), into
    the rawdict `data` of the page, on the line at their baseline, in reading order."""
    fonts = set((decode.get("__stats__") or {}).get("fonts", []))
    if not fonts:
        return
    lines = [ln for b in data["blocks"] for ln in b.get("lines", [])]
    have = {(round(ch["origin"][0], 1), round(ch["origin"][1], 1))
            for ln in lines for sp in ln["spans"] for ch in sp["chars"]}
    for sp in page.get_texttrace():
        if sp["font"] not in fonts:
            continue
        size = sp["size"]
        for ucs, gid, origin, bbox in sp["chars"]:
            key = (round(origin[0], 1), round(origin[1], 1))
            if key in have or (ucs >= 0 and chr(ucs).isspace()):
                continue
            code = chr(gid) if ucs == 0xFFFD or ucs < 0 else chr(ucs)
            if decode.get((sp["font"], code), code).isspace():
                continue
            box = tuple(bbox)
            if box[2] - box[0] < 0.1 * size:
                box = (box[0], box[1], box[0] + size, box[3])
            ch = {"c": code, "origin": tuple(origin), "bbox": box, "synthetic": False}
            line = next((ln for ln in lines if ln["spans"]
                         and abs(ln["spans"][0]["origin"][1] - origin[1]) <= 0.3 * size
                         and ln["bbox"][0] - 1.5 * size <= origin[0] <= ln["bbox"][2] + 1.5 * size), None)
            if line is None:
                continue
            have.add(key)
            # into the span it sits inside, else as a span of its own between its neighbours
            for s in line["spans"]:
                xs = [c["origin"][0] for c in s["chars"]]
                if xs and xs[0] < origin[0] < xs[-1]:
                    s["chars"].insert(next(n for n, x in enumerate(xs) if x > origin[0]), ch)
                    break
            else:
                ref = next((s for s in line["spans"] if s["font"] == sp["font"]), line["spans"][0])
                new = {**ref, "font": sp["font"], "size": size, "origin": tuple(origin), "bbox": box, "chars": [ch]}
                at = next((n for n, s in enumerate(line["spans"]) if s["chars"] and s["chars"][0]["origin"][0] > origin[0]),
                          len(line["spans"]))
                line["spans"].insert(at, new)
            b = line["bbox"]
            line["bbox"] = (min(b[0], box[0]), min(b[1], box[1]), max(b[2], box[2]), max(b[3], box[3]))
