"""Row alignment: text that sits side by side on one row in prod - list items in two columns
("1. 90 degree USB-C adapter" beside "3. USB PD IN"), captions under a row of pictures ("GR10
Mobile Dock", "Quick start guide", "Safety statements", "Warranty card") - and that stage still
shows side by side, but no longer level: one of them is higher or lower than the others.

Only lines that start a text block count (a list item, a caption, a label; not the wrapped lines
of a paragraph), each paired with its stage line through the words the content check matched.
Text that stage stacks in one column instead is a reflow, not a misalignment, and is not reported.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from ..model import Finding, Loc
from . import Unit


def _block_starts(doc, rng) -> set[int]:
    """Lines that start a text block, and every line of a block that is a single row (captions under
    a row of pictures stored as one block: each caption is its own piece of the row)."""
    lines = sorted({doc.words[k].line for k in range(*rng) if doc.words[k].norm})
    out, prev = set(), None
    blocks: dict = defaultdict(list)
    for li in lines:
        blocks[doc.lines[li].block].append(li)
        if prev is None or doc.lines[li].block != doc.lines[prev].block:
            out.add(li)
        prev = li
    for ls in blocks.values():
        size = max(doc.lines[ls[0]].size, 1)
        if len(ls) > 1 and max(doc.lines[l].bbox[1] for l in ls) - min(doc.lines[l].bbox[1] for l in ls) <= 0.35 * size:
            out.update(ls)
    return out


def _norm(doc, li) -> str:
    return " ".join(doc.lines[li].text.lower().split())


def _side_by_side(a, b, gap: float) -> bool:
    """Two line boxes in separate columns: their x ranges do not overlap (by more than `gap`)."""
    return a[2] <= b[0] + gap or b[2] <= a[0] + gap


def _in_table(doc, li) -> bool:
    """Is the line inside a data table? Table rows are compared by the table checks. A grid that holds
    pictures (>= 30 % of it: pictures with their captions laid out in a table) is not a data table."""
    import pymupdf

    from .tables import _figure_rects, _raw
    x0, y0, x1, y1 = doc.lines[li].bbox
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    try:
        boxes = [pymupdf.Rect(t[1]) for t in _raw(doc, doc.lines[li].page)]
        figs = [pymupdf.Rect(r) for r in _figure_rects(doc, doc.lines[li].page)]
    except Exception:
        return False
    for b in boxes:
        if b.x0 - 2 <= cx <= b.x1 + 2 and b.y0 - 2 <= cy <= b.y1 + 2:
            covered = sum((f & b).get_area() for f in figs) / max(b.get_area(), 1)
            if covered < 0.3:
                return True
    return False


def check(u: Unit) -> list[Finding]:
    lcfg = u.cfg["layout"]
    if not lcfg.get("check_rows", True):
        return []
    tol_em = lcfg.get("row_tolerance_em", 0.6)
    A, B = u.a, u.b
    votes: dict[int, Counter] = defaultdict(Counter)
    for i, j in u.pairs:
        votes[A.words[i].line][B.words[j].line] += 1
    starts_a, starts_b = _block_starts(A, u.a_range), _block_starts(B, u.b_range)
    # prod block-start line -> its stage line (the stage line most of its words went to, also a block start)
    to_b = {}
    for la, c in votes.items():
        lb, n = c.most_common(1)[0]
        if la in starts_a and lb in starts_b and n >= 1:
            to_b[la] = lb
    # a line the word pairing did not place (read in another order): the one stage line with its text
    by_text: dict[str, list[int]] = defaultdict(list)
    for lb in starts_b:
        by_text[_norm(B, lb)].append(lb)
    for la in starts_a - set(to_b):
        hits = by_text.get(_norm(A, la), [])
        if len(hits) == 1 and len(_norm(A, la)) >= 3:
            to_b[la] = hits[0]
    # prod rows: block starts on one page, level with each other (tops within a third of a line), in separate columns
    by_page: dict[int, list[int]] = defaultdict(list)
    for la in to_b:
        by_page[A.lines[la].page].append(la)
    findings, done = [], set()
    for page, lines in by_page.items():
        lines.sort(key=lambda la: (A.lines[la].bbox[1], A.lines[la].bbox[0]))
        for la in lines:
            if la in done:
                continue
            size = max(A.lines[la].size, 1)
            row = [lb for lb in lines if abs(A.lines[lb].bbox[1] - A.lines[la].bbox[1]) <= 0.35 * size]
            row = [x for x in row if x == la or _side_by_side(A.lines[x].bbox, A.lines[la].bbox, 2)]
            done.update(row)
            # short items side by side - captions, list items, labels - outside tables; a long line beside a
            # label is a description that re-flows, not a row
            row = [x for x in row if len(A.lines[x].text.split()) <= lcfg.get("row_max_words", 8) and not _in_table(A, x)]
            if len(row) < 2:
                continue
            stage = [to_b[x] for x in row]
            # still side by side on one stage page (not stacked into one column: that is a reflow)
            if len({B.lines[s].page for s in stage}) != 1 or len(set(stage)) != len(stage) or not all(
                    _side_by_side(B.lines[s].bbox, B.lines[t].bbox, 2) for s in stage for t in stage if s != t) \
                    or any(_in_table(B, s) for s in stage):
                continue
            tops = [B.lines[s].bbox[1] for s in stage]
            s_size = max(B.lines[stage[0]].size, 1)
            # off by more than about half a line, but within a few lines: further apart, the other side of the
            # row belongs to other content (a reflowed description), not to this row
            if not tol_em * s_size < max(tops) - min(tops) <= lcfg.get("row_max_offset_lines", 3.5) * 1.3 * s_size:
                continue
            ref = sorted(tops)[len(tops) // 2]  # most of the row sits here
            off = [(x, s, B.lines[s].bbox[1] - ref) for x, s in zip(row, stage)]
            odd = [(x, s, d) for x, s, d in off if abs(d) > tol_em * s_size]
            text = lambda doc, li: doc.lines[li].text.strip()
            others = [x for x, s, d in off if abs(d) <= tol_em * s_size] or [row[0]]
            odd_txt = "; ".join(f"“{text(B, s)}” {abs(d):.0f} pt {'lower' if d > 0 else 'higher'}" for _, s, d in odd)
            findings.append(Finding(
                "layout", lcfg.get("severity", {}).get("row alignment", "warning"),
                f"Not aligned in stage: {', '.join(f'“{text(A, x)}”' for x in row)} are side by side on one row "
                f"in prod (p.{page + 1}); in stage (p.{B.lines[stage[0]].page + 1}) {odd_txt} than "
                f"{', '.join(f'“{text(A, x)}”' for x in others)}",
                [Loc(A.lines[x].page, A.lines[x].bbox) for x in row],
                [Loc(B.lines[s].page, B.lines[s].bbox) for s in stage],
                {"kind": "row alignment", "property": "row alignment", "lines": len(row),
                 "offsets_pt": [round(d, 1) for _, _, d in off]},
                types=["row alignment"],
                links=[(Loc(A.lines[x].page, A.lines[x].bbox), Loc(B.lines[s].page, B.lines[s].bbox)) for x, s in zip(row, stage)]))
    return findings
