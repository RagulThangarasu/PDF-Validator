"""Captions of pictures set side by side: in prod the captions of one row of pictures stand level with each
other (“Projector” | “Remote controls with batteries” | “Power cord” under three pictures). Stage must keep
them level - there each caption can end up right under its own picture, one high and one low, when the
pictures of the row differ in height. Compared row by row: how far each caption moved from prod to stage; a
caption that moved clearly more or less than the others of its row is out of line.

Only captions: a short line with a picture right above it, and other such lines beside it. Running text in
columns is not looked at (it reflows), nor a row whose captions stage no longer sets side by side."""
from __future__ import annotations

from collections import defaultdict
from statistics import median

from ..model import Finding, Loc
from . import Unit


_THINGS: dict = {}


def _things(doc, page: int) -> list[tuple]:
    """Anything a caption can stand under: every placed image and every drawing (rules left out - a table's
    row lines are not a picture), whatever its size."""
    key = (doc.path, page)
    if key not in _THINGS:
        out = [tuple(x.bbox) for x in doc.images if x.page == page]
        try:
            import pymupdf
            pg = pymupdf.open(doc.path)[page]
            out += [tuple(i["bbox"]) for i in pg.get_image_info()]
            ds = [d for d in pg.get_drawings() if d["rect"].width >= 3 and d["rect"].height >= 3]
            out += [tuple(r) for r in pg.cluster_drawings(drawings=ds)] if ds else []
        except Exception:
            pass
        _THINGS[key] = [b for b in out if b[2] - b[0] >= 12 and b[3] - b[1] >= 12
                        and (b[2] - b[0]) * (b[3] - b[1]) < 0.5 * doc.pages[page].width * doc.pages[page].height]
    return _THINGS[key]


def _captions(doc, page: int, lines: dict, pics: list, max_words: int, reach: float) -> dict:
    out = {}  # caption line -> its picture
    for li, ws in lines.items():
        ln = doc.lines[li]
        if ln.page != page or len(ws) > max_words:
            continue
        x0, y0, x1, y1 = ln.bbox
        # a picture right above the line (over the same stretch), nearer than any other text over that stretch
        above = [p for p in pics if p[3] <= y0 + 3 and y0 - p[3] <= reach and min(p[2], x1) - max(p[0], x0) > 0.3 * min(x1 - x0, p[2] - p[0])]
        if not above:
            continue
        near = max(p[3] for p in above)
        if any(o != li and doc.lines[o].page == page and near - 2 <= doc.lines[o].bbox[1] and doc.lines[o].bbox[3] <= y0 + 1
               and min(doc.lines[o].bbox[2], x1) - max(doc.lines[o].bbox[0], x0) > 0 for o in lines):
            continue  # the second line of a caption: its first line stands for it
        out[li] = max(above, key=lambda p: p[3])
    return out


def check(u: Unit) -> list[Finding]:
    cfg = u.cfg.get("caption_rows") or {}
    if not cfg.get("enabled", True):
        return []
    A, B = u.a, u.b
    a2b = dict(u.pairs)
    lines: dict = defaultdict(list)
    for i in range(*u.a_range):
        if A.words[i].norm:
            lines[A.words[i].line].append(i)
    out = []
    for page in sorted({A.lines[li].page for li in lines}):
        pics = _things(A, page)
        if len(pics) < 2:
            continue
        over = _captions(A, page, lines, pics, cfg.get("max_words", 8), cfg.get("reach_pt", 60))
        caps = sorted(over,
                      key=lambda li: (A.lines[li].bbox[1] + A.lines[li].bbox[3]) / 2)
        rows, row = [], []
        for li in caps:
            b = A.lines[li].bbox
            if row and (b[1] + b[3]) / 2 - (A.lines[row[0]].bbox[1] + A.lines[row[0]].bbox[3]) / 2 > 0.9 * (b[3] - b[1]):
                rows.append(row)
                row = []
            row.append(li)
        rows.append(row)
        for row in rows:
            # side by side in prod, each found in stage
            row = sorted(row, key=lambda li: A.lines[li].bbox[0])
            row = [li for k, li in enumerate(row) if k == 0 or A.lines[li].bbox[0] >= A.lines[row[k - 1]].bbox[2] + 4]
            # (one picture over several short texts is a figure over a table's header cells, not a row of captions)
            if len({over[li] for li in row}) < len(row):
                continue
            got = [(li, B.words[a2b[i]].line) for li in row for i in [k for k in lines[li] if k in a2b][:1]]
            if len(got) < 2:
                continue
            sb = [B.lines[lj] for _, lj in got]
            if len({s.page for s in sb}) != 1:
                continue
            order = sorted(sb, key=lambda s: s.bbox[0])
            if any(b.bbox[0] < a.bbox[2] + 4 for a, b in zip(order, order[1:])):
                continue  # stage does not set them side by side (stacked): another layout, not a row out of line
            h = median(A.lines[li].bbox[3] - A.lines[li].bbox[1] for li, _ in got)
            moved = [B.lines[lj].bbox[1] - A.lines[li].bbox[1] for li, lj in got]
            mid = median(moved)
            off = [(li, lj, d - mid) for (li, lj), d in zip(got, moved) if abs(d - mid) > cfg.get("tolerance_lines", 1.5) * h]
            if len(got) == 2 and abs(moved[0] - moved[1]) > cfg.get("tolerance_lines", 1.5) * h:
                off = [(got[1][0], got[1][1], moved[1] - moved[0])]
            if not off:
                continue
            names = ", ".join(f"“{A.lines[li].text.strip()}”" for li, _ in got)
            what = "; ".join(f"“{A.lines[li].text.strip()}” sits {abs(d):.0f}pt {'higher' if d < 0 else 'lower'}" for li, _, d in off)
            out.append(Finding(
                "layout", cfg.get("severity", "warning"),
                f"Picture captions not level in stage: {names} stand in one row in prod; in stage {what} than the "
                f"others of the row (prod p.{page + 1} ↔ stage p.{sb[0].page + 1})",
                [Loc(page, tuple(A.lines[li].bbox)) for li, _ in got], [Loc(B.lines[lj].page, tuple(B.lines[lj].bbox)) for _, lj in got],
                {"property": "caption row", "captions": len(got), "off": [round(d, 1) for _, _, d in off]},
                types=["caption row"]))
    return out
