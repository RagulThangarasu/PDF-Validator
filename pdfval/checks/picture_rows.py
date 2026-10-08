"""Picture grids: pictures that sit side by side on one row in prod (a 2 x 2 or three-column grid of product
views, package contents) must sit side by side in stage - and pictures prod stacks one under the other must not
be set in a row. The pictures are the pairs the assets check matched (the same picture on both sides); icons
are left out. One finding per prod row / stack that is laid out differently.
"""
from __future__ import annotations

from ..model import Finding, Loc
from . import Unit, assets


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
