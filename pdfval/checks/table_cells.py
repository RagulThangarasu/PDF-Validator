"""Table data, cell by cell: “Text in another order” inside a table.

The text diff reads a table as a stream of words. Tables full of the same short values (a column of
“v” check marks, “24, 25, 30” frame rates) then pair the words of one table with those of another
table (or row) that looks alike, and report a “Reordered: v v v v” that says nothing about the data.

Here a prod table is matched to its stage table by its cells (same rows x columns, most cells
equal, in document order), and the rows a reordered finding touches are compared cell by cell -
the text of each cell, not where it sits in the cell (alignment, wrapping, spacing):
  every cell the same  -> the data is all there: no finding
  cells differ         -> the finding becomes “Table cell differs”, highlighting those cells
A finding outside tables, or whose table has no stage table of the same shape, is left as it is.
"""
from __future__ import annotations

import re

import pymupdf

from ..model import Doc, Finding, Loc

_CELLS: dict[tuple, list] = {}  # (path, page) -> [table]: table = {"page", "bbox", "rows": [[cell]]}


def _loose(t: str) -> str:
    return re.sub(r"[\W_]+", "", t).lower()


def _page_tables(doc: Doc, page: int) -> list[dict]:
    """The page's tables as cell grids. A cell: {"bbox", "text"} (the words printed in it, by word
    centre), or None where a spanning cell covers the grid position."""
    key = (doc.path, page)
    if key in _CELLS:
        return _CELLS[key]
    out = []
    try:
        pdf = pymupdf.open(doc.path)
        # the page's own words (x0, y0, x1, y1, text, ...): every value printed in the cell
        words = [w for w in pdf[page].get_text("words") if w[4].strip()]
        for tb in pdf[page].find_tables().tables:
            if tb.col_count < 2 or tb.row_count < 2:
                continue
            rows = []
            for r in tb.rows:
                row = []
                for c in r.cells:
                    if c is None:
                        row.append(None)
                        continue
                    ws = [w for w in words if c[0] <= (w[0] + w[2]) / 2 <= c[2] and c[1] <= (w[1] + w[3]) / 2 <= c[3]]
                    row.append({"bbox": tuple(c), "text": " ".join(w[4] for w in ws)})
                rows.append(row)
            out.append({"page": page, "bbox": tuple(tb.bbox), "rows": rows})
    except Exception:
        out = []
    _CELLS[key] = out
    return out


def _key(cell) -> str | None:
    return None if cell is None else _loose(cell["text"])


def _shape(t: dict) -> tuple:
    return tuple(tuple(c is None for c in r) for r in t["rows"])


def _score(ta: dict, tb: dict) -> float:
    cells = [(a, b) for ra, rb in zip(ta["rows"], tb["rows"]) for a, b in zip(ra, rb) if a is not None]
    return sum(_key(a) == _key(b) for a, b in cells) / len(cells) if cells else 0.0


def _match(tables_a: list[dict], tables_b: list[dict], min_score: float) -> dict[int, dict]:
    """prod table -> its stage table: the same grid (rows x columns, spanning cells), the most cells
    equal; among equally good ones the next one in document order (identical tables repeat per model)."""
    out, used, nxt = {}, set(), 0
    for k, ta in enumerate(tables_a):
        cands = [(m, _score(ta, tb)) for m, tb in enumerate(tables_b)
                 if m not in used and len(tb["rows"]) == len(ta["rows"]) and _shape(tb) == _shape(ta)]
        cands = [(m, s) for m, s in cands if s >= min_score]
        if not cands:
            continue
        best = max(s for _, s in cands)
        m = min((m for m, s in cands if s >= best - 1e-9), key=lambda m: (m < nxt, abs(m - nxt)))
        out[k] = tables_b[m]
        used.add(m)
        nxt = m + 1
    return out


def _overlaps(box, cell) -> bool:
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return cell[0] - 1 <= cx <= cell[2] + 1 and cell[1] - 1 <= cy <= cell[3] + 1


def validate_reordered(ran, A: Doc, B: Doc, cfg: dict) -> None:
    """Reordered text inside a table: dropped when every cell of the rows it touches holds the same
    data in the stage table, else turned into a “Table cell differs” finding on the cells that differ."""
    tcfg = cfg.get("tables", {})
    if not tcfg.get("validate_reordered_cells", True) or A.raw_tables is not None or B.raw_tables is not None:
        return
    todo = [(u, f) for u, fs, _ in ran for f in fs if "reordered" in (f.types or []) and f.baseline]
    if not todo:
        return
    # the tables of every page the sections with such findings span (stage tables may sit a page or
    # two away: another model's tables in between)
    pa, pb = set(), set()
    for u, _ in todo:
        for doc, rng, pages in ((A, u.a_range, pa), (B, u.b_range, pb)):
            if rng[1] > rng[0]:
                lo, hi = doc.words[rng[0]].page, doc.words[rng[1] - 1].page
                pages.update(p for p in range(lo - 1, hi + 2) if 0 <= p < len(doc.pages))
    tables_a = [t for p in sorted(pa) for t in _page_tables(A, p)]
    tables_b = [t for p in sorted(pb) for t in _page_tables(B, p)]
    if not tables_a or not tables_b:
        return
    pair = _match(tables_a, tables_b, tcfg.get("cell_match_ratio", 0.6))

    def where(loc: Loc):
        for k, t in enumerate(tables_a):
            if t["page"] != loc.page:
                continue
            for ri, row in enumerate(t["rows"]):
                for ci, c in enumerate(row):
                    if c is not None and _overlaps(loc.bbox, c["bbox"]):
                        return k, ri
        return None

    drop, add = set(), []
    for u, f in todo:
        spots = [where(l) for l in f.baseline]
        if not all(spots):
            continue  # (partly) outside a table: the text diff's finding stands
        rows = sorted(set(spots))
        if not all(k in pair for k, _ in rows):
            continue  # no stage table of the same grid: the table checks report that
        diffs = []
        for k, ri in rows:
            ta, tb = tables_a[k], pair[k]
            label = next((c["text"] for c in ta["rows"][ri] if c is not None and c["text"]), "")
            for ci, (a, b) in enumerate(zip(ta["rows"][ri], tb["rows"][ri])):
                if a is not None and _key(a) != _key(b):
                    head = _column(ta, ri, ci)
                    diffs.append((a, b, ta["page"], tb["page"], label, head))
        drop.add(id(f))
        if not diffs:
            continue  # all the data is in the stage cells: only the reading order differed
        show = lambda c: f"“{c['text']}”" if c and c["text"] else "(empty)"
        ex = "; ".join(f"row “{lab}”{f', column “{h}”' if h else ''}: {show(a)} → {show(b)}"
                       for a, b, _, _, lab, h in diffs[:4])
        more = f" (and {len(diffs) - 4} more)" if len(diffs) > 4 else ""
        a_locs = [Loc(pa_, a["bbox"]) for a, _, pa_, _, _, _ in diffs]
        b_locs = [Loc(pb_, b["bbox"]) for _, b, _, pb_, _, _ in diffs if b is not None]
        add.append((u, f, Finding(
            "tables", tcfg.get("cell_differs_severity", "error"),
            f"Table cell differs in stage ({len(diffs)} cell(s)): {ex}{more}", a_locs, b_locs,
            {"kind": "cell differs", "cells": len(diffs),
             "baseline_text": " | ".join(a["text"] for a, *_ in diffs),
             "candidate_text": " | ".join((b or {}).get("text", "") for _, b, *_ in diffs)},
            types=["cell differs"],
            links=[(Loc(pa_, a["bbox"]), Loc(pb_, b["bbox"])) for a, b, pa_, pb_, _, _ in diffs if b is not None])))
    for u, fs, _ in ran:
        new = [g for x, f, g in add if x is u]
        if any(id(f) in drop for f in fs) or new:
            fs[:] = [f for f in fs if id(f) not in drop] + new


def _column(t: dict, ri: int, ci: int) -> str:
    """The column's heading: the cells above the row in this column, at most the table's first three
    rows (“8 bit / 24, 25, 30” under “YCbCr 4:2:2”); under a spanning cell, the cell it spans from."""
    parts = []
    for row in t["rows"][:min(3, ri)]:
        c = row[ci] if ci < len(row) else None
        if c is None:
            c = next((row[j] for j in range(ci - 1, -1, -1) if row[j] is not None), None)
        if c and _loose(c["text"]):
            parts.append(c["text"].replace("\n", " "))
    return " / ".join(dict.fromkeys(parts))
