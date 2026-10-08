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


def stacked_to_columns(u: Unit) -> list[Finding]:
    """The layout of a list broke into columns: text that prod stacks - an item's label with its description
    on the line below (“• Automatic adjustment (recommended)” over “Enable the V. O. function to …”) - sits
    side by side in stage, the label in one column and the description in the next. One finding per
    section, naming the pairs.

    A pair counts when both prod lines are outside tables, the second starts right under the first, and
    their stage lines are two different lines, level with each other, in separate columns."""
    lcfg = u.cfg["layout"]
    if not lcfg.get("check_stacked_to_columns", True):
        return []
    A, B = u.a, u.b
    votes: dict[int, Counter] = defaultdict(Counter)
    for i, j in u.pairs:
        votes[A.words[i].line][B.words[j].line] += 1
    # a prod line -> the stage line where its first matched word went (a line that wraps differently in
    # stage still starts where its text starts)
    first: dict[int, int] = {}
    for i, j in sorted(u.pairs):
        first.setdefault(A.words[i].line, B.words[j].line)
    lines = sorted(votes)
    pairs = []
    for la, lb in zip(lines, lines[1:]):
        a1, a2 = A.lines[la], A.lines[lb]
        size = max(a1.size, 1)
        if a1.page != a2.page or not 0 < a2.bbox[1] - a1.bbox[1] <= 2.2 * 1.3 * size:
            continue  # not the next line down
        if _side_by_side(a1.bbox, a2.bbox, 2) or _in_table(A, la) or _in_table(A, lb):
            continue  # already in columns in prod, or table rows (the table checks compare those)
        s1, s2 = first[la], first[lb]
        if s1 == s2 or votes[la][s1] < 1 or votes[lb][s2] < 2:
            continue  # one stage line (the text only wraps elsewhere), or too little matched to place it
        b1, b2 = B.lines[s1], B.lines[s2]
        s_size = max(b1.size, 1)
        if b1.page != b2.page or abs(b1.bbox[1] - b2.bbox[1]) > 0.6 * s_size or b2.bbox[0] < b1.bbox[2] - 2:
            continue  # stacked in stage too, or not to the right of the label
        if b1.block == b2.block and b2.bbox[0] - b1.bbox[2] < 2 * s_size:
            continue  # one stage line cut in two by a wide space
        if len(a1.text.split()) > lcfg.get("row_max_words", 8):
            continue  # the upper line is a label / list item, not a paragraph line
        # a column of its own in stage: the text beside the label goes on underneath at the same left edge
        # (“Slide to adjust brightness” / “as desired.”), or the label does (“Manual” / “adjustment”). The
        # end of a wrapped line set apart on the same row (“… USB 3.2” | “Gen 2 ports”) has neither.
        under = lambda li: any(l.page == B.lines[li].page and l is not B.lines[li]
                               and 0 < l.bbox[1] - B.lines[li].bbox[1] <= 2.2 * 1.3 * s_size
                               and abs(l.bbox[0] - B.lines[li].bbox[0]) <= 2 for l in B.lines[li + 1:li + 6])
        if not (b2.bbox[0] - b1.bbox[0] > 2 * s_size and (under(s2) or under(s1)) and
                not any(l.page == b1.page and abs(l.bbox[1] - b2.bbox[1]) > 0.6 * s_size and 0 < l.bbox[1] - b2.bbox[1] <= 2.2 * 1.3 * s_size
                        and l.bbox[0] < b2.bbox[0] - 2 and l.bbox[2] > b2.bbox[0] + 2 for l in B.lines[s2 + 1:s2 + 6])):
            continue
        pairs.append((la, lb, s1, s2))
    if len(pairs) < lcfg.get("stacked_to_columns_min", 2):
        return []  # one such pair may be a caption beside a picture; a list broken into columns has several
    text = lambda doc, li: doc.lines[li].text.strip()
    ex = "; ".join(f"“{text(A, la)}” above “{text(A, lb)[:40]}”" for la, lb, _, _ in pairs[:3])
    more = f" (and {len(pairs) - 3} more)" if len(pairs) > 3 else ""
    locs_a = [Loc(A.lines[x].page, A.lines[x].bbox) for la, lb, _, _ in pairs for x in (la, lb)]
    locs_b = [Loc(B.lines[x].page, B.lines[x].bbox) for _, _, s1, s2 in pairs for x in (s1, s2)]
    return [Finding(
        "layout", lcfg.get("severity", {}).get("stacked to columns", "error"),
        f"Layout broken in stage: text stacked in prod (p.{A.lines[pairs[0][0]].page + 1}) is side by side in columns "
        f"in stage (p.{B.lines[pairs[0][2]].page + 1}) - {len(pairs)} place(s): {ex}{more}",
        locs_a, locs_b,
        {"kind": "stacked to columns", "property": "layout", "places": len(pairs),
         "baseline_text": "stacked: " + "; ".join(f"“{text(A, la)}” above its text" for la, *_ in pairs[:4]),
         "candidate_text": "in columns: " + "; ".join(f"“{text(B, s1)}” beside “{text(B, s2)[:30]}”" for _, _, s1, s2 in pairs[:4])},
        types=["stacked to columns"],
        links=[(Loc(A.lines[la].page, A.lines[la].bbox), Loc(B.lines[s1].page, B.lines[s1].bbox)) for la, _, s1, _ in pairs])]


_NUM = __import__("re").compile(r"^(\d{1,3})[.)]$")


def numbered_alignment(u: Unit) -> list[Finding]:
    """The numbers of one numbered list line up in prod (1. 2. 3. at the same left edge) and do not in stage: an
    item set further in or out than the items before it (“1.” at the margin, “2.” - “5.” indented). Compared
    through the matched numbers; a list prod itself sets at different indents is left alone."""
    lcfg = u.cfg["layout"]
    if not lcfg.get("check_numbered_alignment", True):
        return []
    A, B = u.a, u.b
    items = []  # (number, prod word, stage word) for every matched list number that starts its prod line
    for i, j in sorted(u.pairs):
        wa, wb = A.words[i], B.words[j]
        m = _NUM.match(wa.text)
        if not m or not _NUM.match(wb.text):
            continue
        la = A.lines[wa.line]
        if abs(wa.bbox[0] - la.bbox[0]) > 1.0:
            continue  # not the first word of its line: a number inside a sentence
        items.append((int(m.group(1)), i, j))
    runs, run = [], []
    for it in items:  # consecutive numbers at one left edge on one prod page: one list
        if run and it[0] == run[-1][0] + 1 and A.words[it[1]].page == A.words[run[-1][1]].page \
                and abs(A.words[it[1]].bbox[0] - A.words[run[-1][1]].bbox[0]) <= 2.0:
            run.append(it)
        else:
            if len(run) >= 2:
                runs.append(run)
            run = [it]
    if len(run) >= 2:
        runs.append(run)
    findings = []
    for run in runs:
        size = max(B.words[run[0][2]].style.size, 1)
        xs = [B.words[j].bbox[0] for _, _, j in run]
        if len({B.words[j].page for _, _, j in run}) != 1:
            continue  # the list goes over a page / slice break in stage
        tol = lcfg.get("numbered_alignment_em", 0.6) * size
        ref = sorted(xs)[len(xs) // 2]  # where most of the numbers sit
        odd = [(n, j, x - ref) for (n, _, j), x in zip(run, xs) if abs(x - ref) > tol]
        if not odd or max(xs) - min(xs) > 8 * size:
            continue  # aligned, or the items are in different columns (a reflow, not a misalignment)
        text = "; ".join(f"“{n}.” {abs(d):.0f} pt {'right' if d > 0 else 'left'} of the others" for n, _, d in odd[:4])
        findings.append(Finding(
            "layout", lcfg.get("severity", {}).get("list alignment", "warning"),
            f"List numbers not aligned in stage: {run[0][0]}.–{run[-1][0]}. start at one left edge in prod "
            f"(p.{A.words[run[0][1]].page + 1}); in stage (p.{B.words[run[0][2]].page + 1}) {text}",
            [Loc(A.words[i].page, A.lines[A.words[i].line].bbox) for _, i, _ in run],
            [Loc(B.words[j].page, B.lines[B.words[j].line].bbox) for _, _, j in run],
            {"kind": "list alignment", "property": "list alignment",
             "baseline_text": f"{run[0][0]}.–{run[-1][0]}. aligned at one left edge",
             "candidate_text": text},
            types=["list alignment"],
            links=[(Loc(A.words[i].page, A.lines[A.words[i].line].bbox), Loc(B.words[j].page, B.lines[B.words[j].line].bbox)) for _, i, j in run]))
    return findings


def check(u: Unit) -> list[Finding]:
    return _row_alignment(u) + stacked_to_columns(u) + numbered_alignment(u)


def _row_alignment(u: Unit) -> list[Finding]:
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
