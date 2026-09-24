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
"""
from __future__ import annotations

from ..model import Doc, Finding, Image, Loc
from . import Aligner, Unit, locs


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
    if near(b, x0 - (doc.words[b].bbox[2] if b is not None else 0)) or \
            near(a, (doc.words[a].bbox[0] if a is not None else 0) - x1):
        return ("inline", b, a)
    above = [i for i in words if (doc.words[i].page, doc.words[i].bbox[3]) <= (im.page, y0 + 2)]
    below = [i for i in words if (doc.words[i].page, doc.words[i].bbox[1]) >= (im.page, y1 - 2)]
    return ("block", max(above) if above else None, min(below) if below else None)


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
    if not pcfg.get("check_placement", True):
        return []
    sev = pcfg.get("severity", {}).get("placement", "warning")
    al = Aligner(u)
    findings = []
    for x, y in u.image_pairs:
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
        if kind_a != kind_b:
            if kind_a == "inline":
                pos = (f"after {_word(u.a, prev_a)}" if prev_a is not None else "at the start of its line") + \
                      (f" and before {_word(u.a, next_a)}" if next_a is not None else "")
                msg = (f"Inline graphic dropped out of its line: in prod it sits in the text {pos}; "
                       f"in stage it is on its own line below {_word(u.b, prev_b)}")
            else:
                pos = f"after {_word(u.b, prev_b)}" if prev_b is not None else "at the start of its line"
                msg = (f"Graphic moved into a text line: in prod it is on its own line below {_word(u.a, prev_a)}; "
                       f"in stage it sits inline {pos}")
        elif not same_neighbours and pa is not None and pb is not None and al.a2b[pa] != pb:
            where = "inline after" if kind_a == "inline" else "placed after"
            msg = (f"Graphic {where} different text: after {_word(u.a, pa)} in prod, "
                   f"after {_word(u.b, pb)} in stage (prod's {_word(u.a, pa)} is elsewhere in stage)")
        if msg:
            a_ctx = [i for i in (prev_a,) if i is not None]
            b_ctx = [i for i in (prev_b,) if i is not None]
            findings.append(Finding(
                "assets", sev, msg,
                [Loc(x.page, x.bbox)] + locs(u.a, a_ctx), [Loc(y.page, y.bbox)] + locs(u.b, b_ctx),
                {"kind": "placement", "property": "placement", "baseline": kind_a, "candidate": kind_b},
                types=["placement"]))
    return findings
