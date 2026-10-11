"""Picture grids: pictures that sit side by side on one row in prod (a 2 x 2 or three-column grid of product
views, package contents) must sit side by side in stage - and pictures prod stacks one under the other must not
be set in a row. The pictures are the pairs the assets check matched (the same picture on both sides); icons
are left out. One finding per prod row / stack that is laid out differently.
"""
from __future__ import annotations

from ..model import Finding, Loc
from . import Unit, assets, snippet


def _same_row(a, b) -> bool:
    """Two picture boxes on one page that overlap vertically (by half the smaller one) and not horizontally."""
    if a.page != b.page:
        return False
    ya, yb = a.bbox, b.bbox
    over = min(ya[3], yb[3]) - max(ya[1], yb[1])
    return over > 0.5 * min(ya[3] - ya[1], yb[3] - yb[1]) and (ya[2] <= yb[0] + 2 or yb[2] <= ya[0] + 2)


def _rows(items: list, key) -> list[list]:
    """Group into rows (connected by _same_row), in reading order."""
    rows: list[list] = []
    for it in sorted(items, key=lambda it: (key(it).page, key(it).bbox[1], key(it).bbox[0])):
        for row in rows:
            if any(_same_row(key(it), key(o)) for o in row):
                row.append(it)
                break
        else:
            rows.append([it])
    return rows


def check(u: Unit) -> list[Finding]:
    lcfg, acfg = u.cfg["layout"], u.cfg["assets"]
    if not lcfg.get("check_picture_rows", True):
        return []
    pairs = [(x, y) for x, y in getattr(u, "image_pairs", []) or []
             if not assets.icon_max(u.a, x, acfg) and not assets.icon_max(u.b, y, acfg)]
    if len(pairs) < 2:
        return []
    sev = lcfg.get("severity", {}).get("picture row", "error")
    findings, done = [], set()

    def report(group: list, prod_side: bool) -> None:
        a_rows = len(_rows(group, lambda p: p[0]))
        b_rows = len(_rows(group, lambda p: p[1]))
        n = len(group)
        if prod_side:
            msg = (f"Layout differs in stage: {n} pictures side by side on one row in prod (p.{group[0][0].page + 1}) "
                   f"take {b_rows} rows in stage (p.{group[0][1].page + 1}) - the picture grid is broken")
            data = {"baseline_text": f"{n} pictures in 1 row", "candidate_text": f"the same {n} pictures in {b_rows} rows"}
        else:
            msg = (f"Layout differs in stage: {n} pictures set side by side on one row in stage (p.{group[0][1].page + 1}) "
                   f"take {a_rows} rows in prod (p.{group[0][0].page + 1})")
            data = {"baseline_text": f"{n} pictures in {a_rows} rows", "candidate_text": f"the same {n} pictures in 1 row"}
        findings.append(Finding(
            "layout", sev, msg, [Loc(x.page, x.bbox) for x, _ in group], [Loc(y.page, y.bbox) for _, y in group],
            {"kind": "picture row", "property": "layout", "pictures": n, **data}, types=["picture row"],
            links=[(Loc(x.page, x.bbox), Loc(y.page, y.bbox)) for x, y in group]))

    # a prod row whose pictures are not on one row in stage
    for row in _rows(pairs, lambda p: p[0]):
        if len(row) >= 2 and len(_rows(row, lambda p: p[1])) > 1:
            report(row, True)
            done.update(id(p) for p in row)
    # a stage row whose pictures prod stacks
    for row in _rows(pairs, lambda p: p[1]):
        row = [p for p in row if id(p) not in done]
        if len(row) >= 2 and len(_rows(row, lambda p: p[0])) == len(row):
            report(row, False)
    return findings


def _beside(box, wbox, gap: float = 2.0) -> bool:
    """A word that stands next to the picture: its middle inside the picture's height, and the word
    itself clear of the picture on the left or the right (text wrapped around a figure)."""
    mid = (wbox[1] + wbox[3]) / 2
    return box[1] <= mid <= box[3] and (wbox[2] <= box[0] + gap or wbox[0] >= box[2] - gap)


def _beside_words(doc, rng, page: int, box) -> list[int]:
    lo, hi = rng
    return [i for i in range(lo, min(hi, len(doc.words)))
            if doc.words[i].norm and doc.words[i].page == page and _beside(box, doc.words[i].bbox)]


def picture_text_rows(u: Unit) -> list[Finding]:
    """A picture and the text that belongs to it, side by side in prod ("1. Connect your camera …" in the
    right-hand column, the drawing in the left - a 2 x 2 grid of picture | step), must stay side by side in
    stage. Stage stacking the step text above or below its picture (one column) is a layout difference the
    reader sees on every page of the procedure - and no other check reports it: the picture rows above need
    two pictures on a row, and the row-alignment check treats text that stage stacks as a reflow.

    Judged on the words themselves: the prod words standing beside the picture, followed to their stage
    words through the content pairs, and asked whether they still stand beside the stage picture.
    """
    lcfg, acfg = u.cfg["layout"], u.cfg["assets"]
    if not lcfg.get("check_picture_text_rows", True):
        return []
    pairs = [(x, y) for x, y in (getattr(u, "image_pairs", []) or []) + (getattr(u, "drawn_pairs", []) or [])
             if not assets.icon_max(u.a, x, acfg) and not assets.icon_max(u.b, y, acfg)]
    if not pairs:
        return []
    min_words = lcfg.get("picture_text_min_words", 4)
    keep = lcfg.get("picture_text_min_share", 0.3)   # this share of the text still beside it = still a row
    sev = lcfg.get("severity", {}).get("text beside picture", "error")
    a2b = dict(u.pairs)
    b2a = {j: i for i, j in u.pairs}
    out = []
    for x, y in pairs:
        for prod_side in (True, False):
            src, dst = (u.a, u.b) if prod_side else (u.b, u.a)
            sbox, dbox = (x, y) if prod_side else (y, x)
            srng, drng = (u.a_range, u.b_range) if prod_side else (u.b_range, u.a_range)
            amap = a2b if prod_side else b2a
            idx = _beside_words(src, srng, sbox.page, sbox.bbox)
            if len(idx) < min_words:
                continue
            moved = [amap[i] for i in idx if i in amap]
            if len(moved) < max(min_words, 0.6 * len(idx)):
                continue  # not enough of this text found on the other side: nothing to judge
            still = [j for j in moved if dst.words[j].page == dbox.page and _beside(dbox.bbox, dst.words[j].bbox)]
            if len(still) >= keep * len(moved):
                continue
            where = "stage" if prod_side else "prod"
            text = snippet(src, idx[:14])
            out.append(Finding(
                "layout", sev,
                (f"Layout differs in {where}: the text beside the picture in "
                 f"{'prod' if prod_side else 'stage'} (p.{sbox.page + 1}) is stacked above / below it in {where} "
                 f"(p.{dbox.page + 1}) - picture and text no longer side by side: “{text}”"),
                [Loc(x.page, x.bbox), *[Loc(src.words[i].page, src.words[i].bbox) for i in (idx if prod_side else [])][:12]],
                [Loc(y.page, y.bbox), *[Loc(src.words[i].page, src.words[i].bbox) for i in ([] if prod_side else idx)][:12]],
                {"kind": "picture text row", "property": "layout", "words": len(idx),
                 "baseline_text": "picture and text side by side" if prod_side else "picture above / below its text",
                 "candidate_text": "picture above / below its text" if prod_side else "picture and text side by side"},
                types=["text beside picture"],
                links=[(Loc(x.page, x.bbox), Loc(y.page, y.bbox))]))
            break   # one finding per picture, whichever side lost the row
    return out
