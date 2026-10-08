"""Placement check: where each graphic sits relative to its text.

For every image pair matched by the assets check (position + appearance), the
graphic's relationship to the surrounding text is derived on each side:

  inline – it shares a text line (vertical overlap with the line), between the
           word before it and the word after it on that line
  block  – it sits on its own between lines, after the last word above it

The relationship is mapped through the content alignment and compared, so it
works for any graphic and any wording, with no special cases:
  * inline in prod, own line in stage (an icon dropped below its sentence) or
    the reverse
  * inline in both, but after a different word
  * a block figure placed after different text
Reported in the images category (type 'placement').

Containment: the box a graphic is drawn inside (a note / tip / warning panel, a
framed callout – any filled or framed shape that also holds text) is identified on
each side by the text in it. "Inside the note with “NOTE: …” in prod, outside it in
stage" (or the reverse) is reported as type 'image outside box'.
"""
from __future__ import annotations

from functools import lru_cache

import pymupdf

from ..model import Doc, Finding, Image, Loc
from . import Aligner, Unit, locs, snippet
from .assets import alignment, icon_max


INLINE_MAX_LINES = 2.5  # a graphic taller than this many text lines is a figure, not an inline graphic


def relation(doc: Doc, rng: tuple[int, int], im: Image) -> tuple[str, int | None, int | None]:
    """('inline', word before, word after) or ('block', word above, word below).

    Inline = the graphic shares a text line (vertical overlap) AND a word of that
    line is within 1.5 em horizontally, i.e. it is part of the text flow – not
    merely level with text in another table cell. Only content words count
    (bullets and other ignored tokens are skipped)."""
    x0, y0, x1, y1 = im.bbox
    words = [i for i in range(*rng) if doc.words[i].norm]
    same_line = []
    for i in words:
        w = doc.words[i]
        if w.page != im.page:
            continue
        ln = doc.lines[w.line]
        ov = min(y1, ln.bbox[3]) - max(y0, ln.bbox[1])
        if ov > 0.5 * min(y1 - y0, ln.bbox[3] - ln.bbox[1]):
            same_line.append(i)
    before = [i for i in same_line if doc.words[i].bbox[2] <= x0 + 2]
    after = [i for i in same_line if doc.words[i].bbox[0] >= x1 - 2]
    b = max(before, key=lambda i: doc.words[i].bbox[2]) if before else None
    a = min(after, key=lambda i: doc.words[i].bbox[0]) if after else None
    near = lambda i, gap: i is not None and gap <= 1.5 * doc.words[i].style.size
    # an inline graphic is icon-sized (a key, a button glyph in a sentence); a picture taller than a few text
    # lines standing beside a list or paragraph is a figure next to the text, never part of its line
    line_h = max((doc.words[i].style.size for i in same_line), default=10) * 1.3
    tall = (y1 - y0) > INLINE_MAX_LINES * line_h
    if not tall and (near(b, x0 - (doc.words[b].bbox[2] if b is not None else 0)) or
                     near(a, (doc.words[a].bbox[0] if a is not None else 0) - x1)):
        return ("inline", b, a)
    above = [i for i in words if (doc.words[i].page, doc.words[i].bbox[3]) <= (im.page, y0 + 2)]
    below = [i for i in words if (doc.words[i].page, doc.words[i].bbox[1]) >= (im.page, y1 - 2)]
    return ("block", max(above) if above else None, min(below) if below else None)


def _in_table_test(u: Unit):
    """im -> True when the picture's centre lies in a table grid on its page (either document). Uses the
    table finder's result the extractor already cached for the page (tables._raw), so it costs nothing."""
    from . import tables as tables_mod

    def test(doc: Doc, im: Image) -> bool:
        cx, cy = (im.bbox[0] + im.bbox[2]) / 2, (im.bbox[1] + im.bbox[3]) / 2
        return any(len(rows) >= 2 and b[0] <= cx <= b[2] and b[1] <= cy <= b[3]
                   for _, b, rows, _ in tables_mod._raw(doc, im.page))
    return test


def _last_paired(i: int | None, paired: dict, lo: int) -> int | None:
    """Nearest word at or before i that has a counterpart on the other side."""
    while i is not None and i >= lo:
        if i in paired:
            return i
        i -= 1
    return None


def _word(doc: Doc, i: int | None) -> str:
    return f"“{doc.words[i].text}”" if i is not None else "the start of the section"


def check(u: Unit) -> list[Finding]:
    pcfg = u.cfg["layout"]
    if not pcfg.get("check_placement", True):  # image alignment: always compared with prod (not CSS)
        return []
    sev = pcfg.get("severity", {}).get("placement", "warning")
    al = Aligner(u)
    findings = []
    in_table = _in_table_test(u)
    for x, y in u.image_pairs:
        if in_table(u.a, x) or in_table(u.b, y):
            continue  # a picture in a table cell (status LEDs): cells, not lines of text - not judged here
        if icon_max(u.a, x, u.cfg["assets"]) or icon_max(u.b, y, u.cfg["assets"]):
            continue  # icons: only compared for look, size, broken/missing and pixelation - not reflow
        ra, rb = relation(u.a, u.a_range, x), relation(u.b, u.b_range, y)
        kind_a, prev_a, next_a = ra
        kind_b, prev_b, next_b = rb
        # compare the nearest *matched* word before the graphic on each side, so extra or
        # changed words next to it (already reported by the content check) don't count as a move
        pa = _last_paired(prev_a, al.a2b, u.a_range[0])
        pb = _last_paired(prev_b, al.b2a, u.b_range[0])
        # same text directly next to the graphic on both sides = same placement, even if the
        # surrounding rows/cells were extracted in a different order
        txt = lambda d, i: d.words[i].norm if i is not None else None
        same_neighbours = ((prev_a is not None and txt(u.a, prev_a) == txt(u.b, prev_b))
                           or (next_a is not None and txt(u.a, next_a) == txt(u.b, next_b)))
        msg = None
        # a centred graphic only dropping to / off its own line (no change of neighbouring text) is a
        # layout reflow, not a real placement issue - unless it landed next to different text (handled below)
        centred = pcfg.get("ignore_center_placement", True) and same_neighbours and \
            (alignment(u.a, x.page, x.bbox) == "centred" or alignment(u.b, y.page, y.bbox) == "centred")
        if kind_a != kind_b and not centred:
            if kind_a == "inline":
                pos = (f"after {_word(u.a, prev_a)}" if prev_a is not None else "at the start of its line") + \
                      (f" and before {_word(u.a, next_a)}" if next_a is not None else "")
                msg = (f"Inline graphic dropped out of its line: in prod it sits in the text {pos}; "
                       f"in stage it is on its own line below {_word(u.b, prev_b)}")
            else:
                pos = f"after {_word(u.b, prev_b)}" if prev_b is not None else "at the start of its line"
                msg = (f"Graphic moved into a text line: in prod it is on its own line below {_word(u.a, prev_a)}; "
                       f"in stage it sits inline {pos}")
        elif not same_neighbours and pa is not None and pb is not None and al.a2b[pa] != pb \
                and not (kind_a != "inline" and icon_max(u.a, x, u.cfg["assets"]) and icon_max(u.b, y, u.cfg["assets"])) \
                and not (kind_a != "inline" and (_beside_text(u.a, x) or _beside_text(u.b, y))):
            # (a figure with text beside it - picture left, list right - has no single "text before it": each
            # PDF reads the two columns in its own order, so the word before it is not a placement difference)
            # (a small icon on its own - a status LED in a table cell - is not judged by the text before it:
            # rows of identical icons pair with the wrong row. An icon moving into / out of a sentence is.)
            where = "inline after" if kind_a == "inline" else "placed after"
            msg = (f"Graphic {where} different text: after {_word(u.a, pa)} in prod, "
                   f"after {_word(u.b, pb)} in stage (prod's {_word(u.a, pa)} is elsewhere in stage)")
        if msg:
            # only the picture is boxed on each side (the text it follows is named in the message)
            findings.append(Finding(
                "assets", sev, msg,
                [Loc(x.page, x.bbox)], [Loc(y.page, y.bbox)],
                {"kind": "placement", "property": "placement", "baseline": kind_a, "candidate": kind_b},
                types=["placement"]))
    findings += _containment(u, al, sev, in_table)
    return findings


def _beside_text(doc: Doc, im) -> bool:
    """Text runs beside the picture (left or right of it, level with it): a figure set next to a list or
    paragraph, not between paragraphs."""
    x0, y0, x1, y1 = im.bbox
    hits = 0
    for ln in doc.lines:
        if ln.page != im.page:
            continue
        cy = (ln.bbox[1] + ln.bbox[3]) / 2
        if y0 <= cy <= y1 and (ln.bbox[0] >= x1 - 2 or ln.bbox[2] <= x0 + 2):
            hits += 1
            if hits >= 2:
                return True
    return False


@lru_cache(maxsize=64)
def _shapes(path: str, page: int) -> tuple[tuple[float, float, float, float], ...]:
    """Tinted panels drawn on a page (note / tip / warning boxes, callouts): shapes filled
    with a colour other than the white page. Table cells and frames (white or outline
    only) are not boxes - tables are compared by the tables check. Empty for a page
    without text (a web page screenshot)."""
    try:
        pg = pymupdf.open(path)[page]
    except Exception:
        return ()
    if not pg.get_text("text").strip():
        return ()
    tinted, white = [], []
    for d in pg.get_drawings():
        r = d["rect"]
        fill = d.get("fill")
        if fill is None or r.width <= 1 or r.height <= 1:
            continue
        (tinted if any(round(c, 2) < 1 for c in fill) else white).append((r.x0, r.y0, r.x1, r.y1))
    area = lambda b: max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    cover = lambda t: sum(area((max(t[0], w[0]), max(t[1], w[1]), min(t[2], w[2]), min(t[3], w[3]))) for w in white)
    # a tinted shape mostly covered by white shapes is a table grid (the tint shows only as
    # the lines between white cells), not a note panel
    return tuple(t for t in tinted if cover(t) < 0.5 * area(t))


def _center_in(b: tuple, box) -> bool:
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return b[0] - 1 <= cx <= b[2] + 1 and b[1] - 1 <= cy <= b[3] + 1


def _panels(doc: Doc, page: int, rng: tuple[int, int]) -> list[tuple[tuple, list[int]]]:
    """Tinted panels on a page with the section's words inside them. A panel holding every
    word of the page is the page background, not a note."""
    page_words = [k for k in range(*rng) if doc.words[k].page == page and doc.words[k].norm]
    out = []
    for b in _shapes(doc.path, page):
        words = [k for k in page_words if _center_in(b, doc.words[k].bbox)]
        if words and len(words) < len(page_words):
            out.append((b, words))
    return out


def _region(doc: Doc, words: list[int], page: int, rng: tuple[int, int]) -> tuple[tuple, bool] | None:
    """Where a note's text is on this side: (the tinted panel holding most of it, True), or
    else (the area its lines cover, False) for a note drawn with rules or no frame at all.
    For the text area only the vertical extent counts: an icon or picture level with the
    note's lines (beside them, or in a table's image column) belongs to it."""
    on_page = [k for k in words if doc.words[k].page == page]
    if not on_page:
        return None
    for b, inside in _panels(doc, page, rng):
        if sum(k in set(inside) for k in on_page) >= 0.5 * len(on_page):
            return b, True
    boxes = [doc.lines[doc.words[k].line].bbox for k in on_page]
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)), False


def _containment(u: Unit, al: Aligner, sev: str, in_table=lambda d, im: False) -> list[Finding]:
    """A graphic inside a note / box on one side but outside the same note on the other.
    The note is known by its text: a tinted panel's words on one side, found through the
    content alignment on the other side, where the note may be drawn differently (a panel,
    rules above and below, or no frame) - so only the graphic's membership counts, not the
    note's styling."""
    out, done = [], set()
    # the same words on both sides, including those that differ only in case / punctuation
    # ("Tip" / "TIP:"), so a note's label line counts as part of the note
    a2b = {**dict(u.style_pairs), **al.a2b}
    b2a = {j: i for i, j in a2b.items()}
    sides = ((u.a, u.b, a2b, u.a_range, u.b_range, "prod", "stage"),
             (u.b, u.a, b2a, u.b_range, u.a_range, "stage", "prod"))
    for x, y in u.image_pairs:
        if in_table(u.a, x) or in_table(u.b, y):
            continue  # a table's shaded rows are not note boxes
        for k, (S, O, to_o, rs, ro, ns, no) in enumerate(sides):
            im_s, im_o = (x, y) if k == 0 else (y, x)
            for panel, words in _panels(S, im_s.page, rs):
                mapped = [to_o[w] for w in words if w in to_o]
                if not mapped or (id(x), id(y)) in done:
                    continue
                found = _region(O, mapped, im_o.page, ro)
                region = found[0] if found else None
                if found and not found[1]:  # text area: vertical extent only
                    region = (float("-inf"), region[1], float("inf"), region[3])
                in_s, in_o = _center_in(panel, im_s.bbox), region is not None and _center_in(region, im_o.bbox)
                if in_s == in_o:
                    continue
                done.add((id(x), id(y)))
                note = snippet(S, words, 10)
                stage_in = in_s if ns == "stage" else in_o
                msg = (f"Image moved into the note / box in stage: “{note}” – in prod the image is outside it"
                       if stage_in else
                       f"Image outside its note / box in stage: in prod it is inside the note “{note}”, in stage it is not")
                out.append(Finding(  # only the picture is boxed; the note is in the screenshot around it
                    "assets", sev, msg, [Loc(x.page, x.bbox)], [Loc(y.page, y.bbox)],
                    {"kind": "outside-box", "property": "placement"}, types=["image outside box"],
                    links=[(Loc(x.page, x.bbox), Loc(y.page, y.bbox))]))
    return out
