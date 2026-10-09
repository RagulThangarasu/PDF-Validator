"""Tables check: tables, rows and cells, end to end.

Row boxes come from PyMuPDF table detection; row *text* comes from the engine's
own words (detection returns empty/merged cells for tables without vertical
rules, e.g. InDesign tables with only horizontal rules). Cells are therefore
counted against the table's column grid (the x-ranges of the row where the
detector sees most cells, usually the header): a row's cells are the grid
columns its words occupy, so a spanning (merged) cell occupies fewer columns.

Rows are mapped prod -> stage by their words, in any order (two PDFs rarely
extract table cells in the same order): a prod row lives in the stage row that
contains ≥ row_match_ratio of its words and its first-cell label. Presence tests
ignore case and punctuation (wording changes are the content check's job), and
where a word is split at a hyphen or slash ("non-" / "condensing" wrapped in a cell).
A row whose cell text simply continues on the next line or page (the extra rows
carry no label of their own) is the same row, not a split or merged row.

From that mapping:
  rows    missing (critical) · extra · merged (2+ prod rows in one stage row) · split
  tables  missing (critical) · extra · merged · split · turned into plain text / from text
  cells   merged (fewer cells in a mapped row, e.g. a spanning cell) · split
  header  the prod table's header row is not in the stage table (nor in the part of
          it on the previous page)
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace

import pymupdf

from ..model import Doc, Finding, Loc
from . import Aligner, Unit, locs, snippet

_DOCS: dict[str, pymupdf.Document] = {}
_RAW: dict[tuple, list] = {}  # (path, page) -> [(table no, table bbox, [(row bbox, first cell bbox)], column grid)]
_RULES: dict[tuple, tuple[list, list]] = {}  # (path, page) -> (vertical, horizontal) border segments


@dataclass
class TRow:
    table: tuple  # (page, table no)
    page: int
    box: tuple
    idx: list[int]
    label: list[int]
    bag: Counter = field(default_factory=Counter)
    label_bag: Counter = field(default_factory=Counter)
    cells: int = 1
    cols: frozenset = frozenset()  # grid columns holding text
    own_label: bool = True  # the first cell holds text (else label is the first words of the row)


@dataclass
class TTable:
    key: tuple
    page: int
    bbox: tuple
    rows: list[TRow]


def _raw(doc: Doc, page: int) -> list:
    if doc.raw_tables is not None:  # structured source (web page): real <table>/<tr>/<td>
        return doc.raw_tables.get(page, [])
    key = (doc.path, page)
    if key not in _RAW:
        pdf = (_DOCS.get(doc.path) or _DOCS.setdefault(doc.path, pymupdf.open(doc.path)))
        _RAW[key] = detect(pdf[page])
    return _RAW[key]


def detect(page: pymupdf.Page) -> list:
    """Tables on a page: [(table no, table bbox, [(row bbox, first cell bbox)], column grid)].
    Also used by the extractor to read table text in row/cell order."""
    out = []
    try:
        for t, tb in enumerate(page.find_tables().tables):
            if tb.col_count >= 2:
                # column grid = x-ranges of the row where the detector sees the most cells
                # (usually the header: tables without vertical rules still have a shaded header)
                grid_row = max(tb.rows, key=lambda r: sum(1 for c in r.cells if c))
                grid = sorted((c[0], c[2]) for c in grid_row.cells if c)
                out.append((t, tuple(tb.bbox), [(tuple(r.bbox), next((tuple(c) for c in r.cells if c), None))
                                                 for r in tb.rows], grid))
    except Exception:
        pass
    try:
        out += _ruled(page, out)
    except Exception:
        pass
    return out


def _ruled(page: pymupdf.Page, found: list) -> list:
    """Tables drawn with horizontal rules only (“UI element | Description”: a shaded header bar and a thin
    line under each row, no vertical lines, no cell borders) - the grid detector finds at most the header
    bar. A stack of 3+ rules of the same width with text between them is a table: a row per band, the
    columns where the rows' text and icons start (a start shared by 2+ rows)."""
    drawings = page.get_drawings()
    segs: dict = {}
    bars = []
    for d in drawings:
        r = d["rect"]
        if r.height <= 2.5 and r.width >= 20:
            segs.setdefault(round((r.y0 + r.y1) / 4) * 2, []).append((r.x0, r.x1))
        elif d.get("fill") is not None and 10 <= r.height <= 45 and r.width >= 150 and max(d["fill"][:3]) < 0.97:
            bars.append(r)  # a header bar: its top and bottom edges are rules
    rules = []  # (y, x0, x1) with touching segments joined
    for y, xs in segs.items():
        xs.sort()
        lo, hi = xs[0]
        for a0, a1 in xs[1:] + [(10 ** 6, 10 ** 6)]:
            if a0 <= hi + 2:
                hi = max(hi, a1)
            else:
                if hi - lo >= 150:
                    rules.append((y, lo, hi))
                lo, hi = a0, a1
    for b in bars:
        rules += [(b.y0, b.x0, b.x1), (b.y1, b.x0, b.x1)]
    bar_tops = [(b.y0, b.x0, b.x1) for b in bars]
    lines = [(pymupdf.Rect(ln["bbox"]), "".join(sp["text"] for sp in ln["spans"]).strip())
             for blk in page.get_text("dict")["blocks"] for ln in blk.get("lines", [])]
    lines = [(r, t) for r, t in lines if t]
    icons = [pymupdf.Rect(i["bbox"]) for i in page.get_image_info() if pymupdf.Rect(i["bbox"]).width <= 60]
    taken = [pymupdf.Rect(t[1]) for t in found if len(t[2]) >= 2]
    out = []
    groups: dict = {}
    for y, x0, x1 in rules:
        key = next((k for k in groups if abs(k[0] - x0) <= 4 and abs(k[1] - x1) <= 4), (x0, x1))
        groups.setdefault(key, []).append(y)
    for (x0, x1), ys in groups.items():
        ys = sorted(set(round(y, 1) for y in ys))
        stack = [ys[0]]
        for y in ys[1:] + [None]:
            # one table: rules at most 160 pt apart; two rules a few points apart (the table's last line, then the
            # top line of a Tip / Note block under it) end it
            # a header bar starts a new table: the band above it (an “Output” heading between two tables) is no row
            new_bar = y is not None and any(abs(y - by) <= 2 and abs(bx0 - x0) <= 4 for by, bx0, _ in bar_tops)
            if y is not None and 6 <= y - stack[-1] <= 160 and not new_bar:
                stack.append(y)
                continue
            # a table starts at its header bar (rules alone also frame Tip / Note blocks)
            starts_ok = any(abs(stack[0] - by) <= 2 and abs(bx0 - x0) <= 4 for by, bx0, _ in bar_tops)
            if not starts_ok:
                stack = [y] if y is not None else []
                continue
            bands = [(a, b) for a, b in zip(stack, stack[1:]) if b - a >= 8
                     and any(x0 - 2 <= r.x0 and r.x1 <= x1 + 2 and a - 1 <= (r.y0 + r.y1) / 2 <= b + 1 for r, _ in lines)]
            box = pymupdf.Rect(x0, stack[0], x1, stack[-1])
            if len(bands) >= 2 and not any((box & t).get_area() > 0.5 * box.get_area() for t in taken):
                # column starts: text lines / icons starting at the same x in 2+ rows
                starts = []
                for a, b in bands:
                    xs_ = {round(r.x0) for r, _ in lines if x0 - 2 <= r.x0 <= x1 and a <= (r.y0 + r.y1) / 2 <= b}
                    xs_ |= {round(r.x0) for r in icons if x0 - 2 <= r.x0 <= x1 and a <= (r.y0 + r.y1) / 2 <= b}
                    starts.append(xs_)
                cand = sorted({x for st in starts for x in st})
                cols = []
                for x in cand:
                    n = sum(1 for st in starts if any(abs(x - z) <= 4 for z in st))
                    if n >= max(2, 0.4 * len(bands)) and (not cols or x - cols[-1] > 30):
                        cols.append(x)
                if len(cols) >= 2:
                    edges = [x0] + [c - 2 for c in cols[1:]] + [x1]
                    grid = [(edges[k], edges[k + 1]) for k in range(len(edges) - 1)]
                    rows = [((x0, a, x1, b), (x0, a, grid[0][1], b)) for a, b in bands]
                    out.append((100 + len(out), (x0, stack[0], x1, stack[-1]), rows, grid))
            stack = [y] if y is not None else []
    return out


def _loose(t: str) -> str:
    """Presence tests ignore case and punctuation ('Note' -> 'NOTE:' is a content change)."""
    return re.sub(r"[\W_]+", "", t).lower()


def _bag(doc: Doc, idx) -> Counter:
    # the letter / digit pieces of the words, split at every punctuation mark: where a cell wraps decides whether
    # "non-condensing" is one word or two, and a space set or left out before a bracket decides whether
    # "MP3(.mp3)" is one word or "MP3" + "(.mp3)" - the same row either way (a row of such words would otherwise
    # share no word with itself: reported missing in stage and extra in stage)
    return Counter(k for i in idx for k in re.findall(r"[^\W_]+", doc.words[i].norm.lower()))


def _inside(doc: Doc, rng, page: int, box) -> list[int]:
    out = []
    for i in range(*rng):
        w = doc.words[i]
        if w.page != page or not w.norm:
            continue
        cx, cy = (w.bbox[0] + w.bbox[2]) / 2, (w.bbox[1] + w.bbox[3]) / 2
        if box[0] <= cx <= box[2] and box[1] <= cy <= box[3]:
            out.append(i)
    return out


def _columns(doc: Doc, idx: list[int], grid: list[tuple]) -> frozenset:
    """Grid columns that hold text of this row (by word centre). A word that continues the line of
    the word before it (same line, a normal word gap) is in that word's cell: a cell spanning the
    row ("(When no menu has been activated)") runs across the grid's column line, it is one cell."""
    cols = set()
    prev, prev_col = None, None
    for i in sorted(idx):
        w = doc.words[i]
        if prev is not None and w.line == prev.line and 0 <= w.bbox[0] - prev.bbox[2] <= 1.5 * w.style.size:
            prev = w
            continue
        cx = (w.bbox[0] + w.bbox[2]) / 2
        prev_col = next((k for k, (x0, x1) in enumerate(grid) if x0 - 1 <= cx <= x1 + 1), None)
        if prev_col is not None:
            cols.add(prev_col)
        prev = w
    return frozenset(cols)


def _rows_run_together(rows: list[TRow], mapping: dict, other: Doc) -> bool:
    """The signature of 'table turned into plain text': on the other side, text from
    different rows of this table runs together on the same line. If every row's text
    still sits on lines of its own, the tabular structure survived (whatever the
    detector says)."""
    line_rows: dict[int, set] = defaultdict(set)
    words = 0
    for k, r in enumerate(rows):
        for i in r.idx:
            if i in mapping:
                line_rows[other.words[mapping[i]].line].add(k)
                words += 1
    if words < 4:
        return False
    mixed = sum(1 for rs in line_rows.values() if len(rs) >= 2)
    return mixed / max(len(line_rows), 1) >= 0.3


def _in_detected_table(doc: Doc, idx: list[int]) -> bool:
    """Do most of these words sit inside any table the detector found (data table or not)?"""
    inside = 0
    for i in idx:
        w = doc.words[i]
        cx, cy = (w.bbox[0] + w.bbox[2]) / 2, (w.bbox[1] + w.bbox[3]) / 2
        if any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for _, b, _, _ in _raw(doc, w.page)):
            inside += 1
    return bool(idx) and inside / len(idx) >= 0.6


def _tabular(doc: Doc, idx: list[int]) -> bool:
    """Is this text laid out as a table – two text lines side by side on the same
    row with a wide gap? Normal paragraphs never are. Tells 'no longer a table'
    from 'a table the detector could not see'."""
    lines = sorted({doc.words[i].line for i in idx})
    for a in lines:
        la = doc.lines[a]
        for b in lines:
            lb = doc.lines[b]
            if a < b and la.page == lb.page and min(la.bbox[3], lb.bbox[3]) - max(la.bbox[1], lb.bbox[1]) > 0 and \
                    min(abs(lb.bbox[0] - la.bbox[2]), abs(la.bbox[0] - lb.bbox[2])) > 3 * max(la.size, 6):
                return True
    return False


_DIAGRAM_TOKEN = re.compile(r"^\(?[\w=+.,:/×-]{1,12}\)?$")


def diagram_grid(texts: list[str], cells: int) -> bool:
    """A grid that is a figure, not a data table: every cell holds one short coordinate-like code in brackets
    (“(1.1)”, “(2.1)”, “(H+1.V=1)” - a video-wall layout drawn as boxes) or one small number (“1” … “9”, the
    displays of a wall numbered in order); no header, no words, no sentences. Its text is the figure's artwork."""
    texts = [t for t in texts if t.strip()]
    if not (2 <= len(texts) <= cells + 1) or not all(_DIAGRAM_TOKEN.match(t) for t in texts):
        return False
    bracketed = sum(t.startswith("(") or t.endswith(")") for t in texts) >= 0.6 * len(texts)
    numbered = len(texts) >= 3 and all(re.fullmatch(r"\d{1,2}", t) for t in texts)
    return bracketed or numbered


_DIAGRAMS: dict[tuple, list] = {}


def diagram_boxes(doc: Doc, page: int) -> list[tuple]:
    """The boxes of the page's diagram grids (see diagram_grid): detected "tables", and groups of drawn boxes
    the table detector does not report (a single row or column of boxes). A cell's code is its line of text
    joined (“(H” “=” “1.V” “=” “1)” is one code)."""
    key = (doc.path, page)
    if key in _DIAGRAMS:
        return _DIAGRAMS[key]

    def codes(box) -> tuple[list[str], int]:
        by_line: dict = {}
        n = 0
        for w in doc.words:
            if w.page == page and box[0] - 1 <= (w.bbox[0] + w.bbox[2]) / 2 <= box[2] + 1 \
                    and box[1] - 1 <= (w.bbox[1] + w.bbox[3]) / 2 <= box[3] + 1:
                by_line.setdefault(w.line, []).append(w.text)
                n += 1
        # one line may hold a row's cells side by side (“(1.1)” “(2.1)”): each bracketed code is its own
        out = []
        for ws in by_line.values():
            joined = "".join(ws)
            parts = re.findall(r"\([^()]{1,12}\)?", joined) if joined.count("(") > 1 else None
            out += parts if parts and "".join(parts) == joined else ([joined] if len(ws) > 1 and "(" in joined else ws)
        return out, n
    out = []
    for _, tbox, rows, grid in _raw(doc, page):
        texts, _n = codes(tbox)
        if diagram_grid(texts, max(len(texts), max(1, len(rows)) * max(1, len(grid)))):
            out.append(tuple(tbox))
    try:  # box groups that are no "table": a column / a row of boxes
        pdf = _DOCS.get(doc.path) or _DOCS.setdefault(doc.path, pymupdf.open(doc.path))
        pg = pdf[page]
        for r in pg.cluster_drawings():
            if r.width < 30 or r.height < 20 or r.get_area() > 0.6 * pg.rect.get_area():
                continue
            if any(abs(r.x0 - o[0]) < 6 and abs(r.y0 - o[1]) < 6 and abs(r.x1 - o[2]) < 6 and abs(r.y1 - o[3]) < 6 for o in out):
                continue
            texts, _n = codes(tuple(r))
            if diagram_grid(texts, len(texts)):
                out.append(tuple(r))
    except Exception:
        pass
    _DIAGRAMS[key] = out
    return out


def tables(doc: Doc, rng: tuple[int, int], loose: bool = False) -> list[TTable]:
    """loose: also the grids that are not a table of rows on their own - the part of a table that runs on over
    a page with one tall label cell beside several bordered parts (text in two columns, but only one column
    with text in two rows)."""
    pages = sorted({doc.words[i].page for i in range(*rng)})
    out = []
    for p in pages:
        figures = diagram_boxes(doc, p)
        for t, tbox, rows, grid in _raw(doc, p):
            if tuple(tbox) in figures:
                continue  # a layout diagram drawn as a grid: a figure, not a table to compare row by row
            trows, taken = [], set()
            for box, first in rows:
                # a row spans the whole table: the detector only reports the cells it can see
                # (e.g. just the shaded label column when a table has no vertical rules)
                box = (tbox[0], box[1], tbox[2], box[3])
                # a word belongs to one row: the detector can report nested/overlapping rows
                idx = [i for i in _inside(doc, rng, p, box) if i not in taken]
                if not idx:
                    continue
                taken.update(idx)
                first_idx = _inside(doc, rng, p, first) if first else []
                label = first_idx or idx[:3]
                cols = _columns(doc, idx, grid)
                trows.append(TRow((p, t), p, box, idx, label, _bag(doc, idx), _bag(doc, label), len(cols), cols,
                                  bool(first_idx) or not first))
            # a data table: ≥ 2 rows and ≥ 2 grid columns that hold text in ≥ 2 rows
            # (a bordered Note/Tip box is detected as icon | text: only one text column)
            used = Counter(c for r in trows for c in r.cols)
            if len(trows) >= 2 and sum(1 for n in used.values() if n >= 2) >= 2:
                out.append(TTable((p, t), p, tbox, trows))
            elif loose and trows and len(used) >= 2:
                out.append(TTable((p, t), p, tbox, trows))
    return out


def _contains(row: TRow, bag: Counter) -> float:
    n = sum(row.bag.values())
    return sum((row.bag & bag).values()) / n if n else 0.0


def _label_ok(row: TRow, bag: Counter) -> bool:
    n = sum(row.label_bag.values())
    need = 1.0 if n <= 2 else 0.6  # short labels exact; long (callout) labels tolerate a changed word
    return not n or sum((row.label_bag & bag).values()) / n >= need


def _map(rows_a: list[TRow], rows_b: list[TRow], thr: float, expect) -> dict[int, list[tuple[int, float]]]:
    """row -> rows on the other side containing it: [(row, containment)], best first.
    Among equally good candidates (identical header rows repeat in many tables) the
    one nearest the row's aligned position wins: expect(first word) -> word index."""
    out = {}
    for k, r in enumerate(rows_a):
        hits = [(m, _contains(r, s.bag)) for m, s in enumerate(rows_b) if _label_ok(r, s.bag)]
        hits = [h for h in hits if h[1] >= thr]
        if hits:
            best = max(c for _, c in hits)
            e = expect(r.idx[0])
            near = lambda m: abs(rows_b[m].idx[0] - e) if e is not None else 0
            # among the good candidates, the row most alike both ways first: “The touch positioning is
            # incorrect” is contained in “The touchscreen is not responding” too (the same advice), but its
            # own row is the one that holds nothing else
            n_r = sum(r.bag.values())
            dice = lambda m: 2 * sum((r.bag & rows_b[m].bag).values()) / max(1, n_r + sum(rows_b[m].bag.values()))
            hits.sort(key=lambda h: (h[1] < best - 0.15, -round(dice(h[0]), 2), near(h[0]), -h[1]))
        out[k] = hits
    return out


def _same_place(rows_a: list[TRow], rows_b: list[TRow], fwd: dict, back: dict, share: float = 0.8) -> None:
    """A row whose label changed ("液晶面板" -> "液晶螢幕") has no partner by label. When it is the only
    unmatched row between two matched neighbours on both sides and the rest of its cells are the
    same, it is the same row: the label change is reported by the content check, not as a split,
    missing or extra row."""
    for k, r in enumerate(rows_a):
        if fwd[k]:
            continue
        same_t = [j for j in range(len(rows_a)) if rows_a[j].table == r.table]
        prev = next((j for j in reversed(same_t) if j < k and fwd[j]), None)
        nxt = next((j for j in same_t if j > k and fwd[j]), None)
        if prev is None and nxt is None:
            continue
        lo = fwd[prev][0][0] if prev is not None else None
        hi = fwd[nxt][0][0] if nxt is not None else None
        a_between = [j for j in same_t if (prev is None or j > prev) and (nxt is None or j < nxt) and not fwd[j]]
        ref_t = rows_b[lo if lo is not None else hi].table
        b_between = [m for m, s in enumerate(rows_b) if s.table == ref_t and not back[m]
                     and (lo is None or m > lo) and (hi is None or m < hi)]
        if len(a_between) != 1 or len(b_between) != 1:
            continue
        s = rows_b[b_between[0]]
        va, vb = r.bag - r.label_bag, s.bag - s.label_bag
        n = sum(va.values())
        if (n == 0 and not vb) or (n and sum((va & vb).values()) / n >= share and r.cells == s.cells):
            fwd[k] = [(b_between[0], 1.0)]
            back[b_between[0]] = [(k, 1.0)]


def _split(r: TRow, rows_b: list[TRow], thr: float) -> list[int]:
    """Stage rows that together (but none alone) contain a prod row: the row that carries
    the prod row's label, plus rows among the next three that add more of its words
    (e.g. its continuation at the top of the next page, below a repeated header)."""
    lab_n = sum(r.label_bag.values())
    carries_label = lambda bag: not lab_n or sum((r.label_bag & bag).values()) / lab_n >= 0.5  # the label may split too
    heads = [m for m, s in enumerate(rows_b) if carries_label(s.bag) and _contains(r, s.bag) >= 0.2]
    for m in sorted(heads, key=lambda m: -_contains(r, rows_b[m].bag)):
        parts, union = [m], Counter(rows_b[m].bag)
        for nxt in range(m + 1, min(m + 4, len(rows_b))):  # skip rows that add nothing (a repeated header)
            grown = union | rows_b[nxt].bag
            if _contains(r, grown) > _contains(r, union):
                parts.append(nxt)
                union = grown
        if len(parts) >= 2 and _contains(r, union) >= thr:
            return parts
    return []


def _wrapped(head: TRow, rest: list[TRow]) -> bool:
    """rest only continues head's cells on the next line/page: no new first-cell label
    (an empty first cell, or the rest of head's own label, e.g. "(without glass)")."""
    return all(not r.own_label or not (r.label_bag - head.label_bag) for r in rest)


def _missing_header(t, ta, tb, dest, rows_a, missing_row, findings, add, tcfg, thr, A, B, al, rtext) -> None:
    """The first row of a prod table (its header) is not in the stage table the body went to.
    A prod table continued from the previous page has no header of its own; the stage table
    may carry the header in its part on the previous page."""
    k = next((n for n, x in enumerate(ta) if x is t), 0)
    if len(t.rows) < 3 or (k and _continuation(ta[k - 1], t, A)):
        return
    head = t.rows[0]
    # a note set right above the table ("Note  Actual screen and features may vary…") that the detector took
    # in as its first row: a callout, not the table's header (stage has it as a NOTE box of its own)
    lead = [A.words[i].text.strip() for i in head.idx[:2]]
    if any(_CALLOUT_LABEL.match(t_) or A.words[i].norm.startswith("<label:") for t_, i in zip(lead, head.idx[:2])):
        return
    first = min((x for x in tb if x.key in dest), key=lambda x: (x.page, x.bbox[1]))
    holders = [first]
    j = next(n for n, x in enumerate(tb) if x is first)
    if j and _continuation(tb[j - 1], first, B):
        holders.append(tb[j - 1])
    if any(_label_ok(head, s.bag) and _contains(head, s.bag) >= thr for x in holders for s in x.rows):
        return
    row_k = next((n for n, r in enumerate(rows_a) if r is head), None)
    if row_k in missing_row:  # one finding for the header, not a missing row as well
        findings.remove(missing_row.pop(row_k))
    add(tcfg.get("missing_header_severity", "error"),
        f"Table header missing in stage: prod table (p.{t.page + 1}) starts with the header row "
        f"“{rtext(A, head, 10)}”, the stage table (p.{first.page + 1}) has no such row",
        [Loc(head.page, head.box)], [Loc(first.page, first.rows[0].box)], "missing header")


def _longest_increasing(xs: list) -> set[int]:
    """Positions of one longest non-decreasing subsequence of xs (two prod rows merged into one stage
    row share its position)."""
    import bisect
    tails, tails_i, prev = [], [], [-1] * len(xs)
    for n, x in enumerate(xs):
        k = bisect.bisect_right(tails, x)
        if k == len(tails):
            tails.append(x)
            tails_i.append(n)
        else:
            tails[k], tails_i[k] = x, n
        prev[n] = tails_i[k - 1] if k else -1
    out, n = set(), tails_i[-1] if tails_i else -1
    while n >= 0:
        out.add(n)
        n = prev[n]
    return out


def _continuation(a: TTable, b: TTable, doc: Doc) -> bool:
    """b continues a across a page break (same table split by pagination, not by the author)."""
    return b.page == a.page + 1 and a.bbox[3] > 0.7 * doc.pages[a.page].height and b.bbox[1] < 0.3 * doc.pages[b.page].height


def _own_header(doc: Doc, a: TTable, b: TTable) -> bool:
    """b starts with a header row of its own - not a's header repeated on a continuation page: its first row
    is set like a's header row (the same background bar, another one than b's data rows) but says something
    else (“LED indicator on the Receiver | Status Description” after the table “LED indicator on the Button |
    Status Description”). b is then the next table, though it starts at the top of the next page."""
    if not a.rows or len(b.rows) < 2:
        return False
    ha, hb = a.rows[0], b.rows[0]
    if _alike(ha.bag, hb.bag) >= 0.9:
        return False  # the same header again: a continuation page
    try:
        bg_a, bg_b, bg_data = _background(doc, ha.page, ha.box), _background(doc, hb.page, hb.box), \
            _background(doc, b.rows[1].page, b.rows[1].box)
    except Exception:
        return False
    return bool(bg_a) and bg_a == bg_b and bg_b != bg_data


def _one_table(all_tables: list[TTable], parts: list[TTable], doc: Doc) -> bool:
    """The parts are one table running over page breaks. Parts on consecutive pages: each continues the
    one before it. Parts further apart (a table over p.43, p.44 and p.45 whose p.44 part is not among them -
    one tall row, not a data table of its own): the first ends low on its page, the last starts at the top
    of its page, and every page between them holds a detected table running from its top to its bottom."""
    if len(parts) < 2:
        return True
    for a, b in zip(parts, parts[1:]):
        if _own_header(doc, a, b):
            return False  # the next page starts another table, under a header of its own
        if _continuation(a, b, doc) or _continues(a, b, doc):
            continue
        if b.page <= a.page + 1:
            return False  # the same page, or the next one without running on: two tables
        if a.bbox[3] < 0.6 * doc.pages[a.page].height or b.bbox[1] > 0.3 * doc.pages[b.page].height:
            return False
        for pg in range(a.page + 1, b.page):
            H = doc.pages[pg].height
            if not any(t[1][1] <= 0.3 * H and t[1][3] >= 0.6 * H for t in _raw(doc, pg)):
                return False  # a page between them without the table going through it
    return True


def _alike(a: Counter, b: Counter) -> float:
    n = max(sum(a.values()), sum(b.values()))
    return sum((a & b).values()) / n if n else 0.0


def _continues(a: TTable, b: TTable, doc: Doc) -> bool:
    """b is a running onto the next page: b starts at the top of the next page with nothing above it (a heading
    or text above it makes it a new table) and a ends in the lower part of its page (a table broken early by
    a picture or a keep-with-next rule still ends below the middle)."""
    if b.page != a.page + 1 or a.bbox[3] < 0.6 * doc.pages[a.page].height or b.bbox[1] > 0.3 * doc.pages[b.page].height:
        return False
    return not any(w.page == b.page and w.norm and w.bbox[3] <= b.bbox[1] + 1 for w in doc.words)


def joined(ts: list[TTable], doc: Doc) -> tuple[list[TTable], list[TRow]]:
    """One table per table, not per page: a table running onto the next page (its part ends near the page
    bottom, the next part starts near the top) is one table, its parts in order. The header rows a
    continuation page repeats at its top (“Timing Support” / “PC/Video Signal Support” / “Resolution | Frame
    Frequency …”) are pagination, not data: dropped, so they are neither compared as rows nor reported as
    extra / missing / out of order. Returns (tables, the dropped header rows). Rows are copies (their table
    key is the whole table's); the per-page tables stay as they are for the checks that need the pages."""
    out: list[TTable] = []
    dropped: list[TRow] = []
    last = None  # the previous page part of the current table
    for t in ts:
        repeats = bool(out and t.rows and out[-1].rows and _alike(t.rows[0].bag, out[-1].rows[0].bag) >= 0.9)
        at_top = last is not None and t.page == last.page + 1 and t.bbox[1] <= 0.3 * doc.pages[t.page].height and \
            not any(w.page == t.page and w.norm and w.bbox[3] <= t.bbox[1] + 1 for w in doc.words)
        # a table goes on over a page break when it ends low on its page - or, wherever it ends (a tall row
        # moved on), when the next page starts with its header repeated
        if out and last is not None and (_continues(last, t, doc) or (repeats and at_top)):
            whole = out[-1]
            head = [r for r in whole.rows[:6]]
            k = 0  # leading rows of the new part that repeat the table's first rows (its header)
            while k < len(t.rows) - 1 and k < len(head) and _alike(t.rows[k].bag, head[k].bag) >= 0.9:
                k += 1
            ws = [doc.words[i] for i in t.rows[0].idx if doc.words[i].norm]
            if not k and ws and (sum(w.style.weight >= 500 for w in ws) / len(ws) >= 0.9 or _own_header(doc, whole, t)):
                # it starts with a header row of its own (“PD3226G/ PD2730S Settings” under “PD2706QN Settings”):
                # the next table, not this one continued
                out.append(TTable(t.key, t.page, t.bbox, [replace(r) for r in t.rows]))
                last = t
                continue
            dropped += t.rows[:k]
            rest = [replace(r, table=whole.key) for r in t.rows[k:]]
            # a row cut by the page break: the first row of the new part has nothing in its first cell (the
            # label stays on the previous page) - it is the last row of the previous part, continued
            if rest and not rest[0].own_label and whole.rows:
                last_row, cont = whole.rows[-1], rest.pop(0)
                whole.rows[-1] = replace(last_row, idx=last_row.idx + cont.idx, bag=last_row.bag + cont.bag,
                                         cols=last_row.cols | cont.cols)
            whole.rows += rest
            last = t
            continue
        out.append(TTable(t.key, t.page, t.bbox, [replace(r) for r in t.rows]))
        last = t
    return out, dropped


def _on_image(doc: Doc, t: "TTable") -> bool:
    """Most of this table's own area sits on an embedded raster image: a screenshot/UI mockup the detector
    framed as a table grid (its labels happen to line up in columns), not real table data. Raster images
    only - not is_data_table's vector-drawing check, which also catches a table's own border/background
    rectangle and would wrongly reject real tables too."""
    tb_ = pymupdf.Rect(t.bbox)
    area = tb_.get_area()
    if not area:
        return False
    pic = sum((pymupdf.Rect(im.bbox) & tb_).get_area() for im in doc.images if im.page == t.page)
    return pic >= 0.5 * area


def check(u: Unit) -> list[Finding]:
    tcfg = u.cfg["tables"]
    if not tcfg.get("enabled", True):
        return []
    thr = tcfg.get("row_match_ratio", 0.5)
    al = Aligner(u)
    A, B = u.a, u.b
    ta_pages, tb_pages = tables(A, u.a_range), tables(B, u.b_range)
    ta_pages = [t for t in ta_pages if not _on_image(A, t)]
    tb_pages = [t for t in tb_pages if not _on_image(B, t)]
    # rows are compared per whole table: a table split over pages is one, its repeated header rows left out
    ta, head_a = joined(ta_pages, A)
    tb, head_b = joined(tb_pages, B)
    rows_a = [r for t in ta for r in t.rows]
    rows_b = [r for t in tb for r in t.rows]
    text_a, text_b = _bag(A, range(*u.a_range)), _bag(B, range(*u.b_range))
    fwd, back = _map(rows_a, rows_b, thr, al.to_b), _map(rows_b, rows_a, thr, al.to_a)
    _same_place(rows_a, rows_b, fwd, back)
    # rows repeated in several tables of one document (column headers like "Model | SL4304 …")
    # say nothing about which table is which – ignored when relating tables and comparing cells
    def repeated(rows):
        seen = Counter(frozenset(r.bag.items()) for r in rows)
        return {id(r) for r in rows if seen[frozenset(r.bag.items())] >= 2}
    rep_a, rep_b = repeated(rows_a), repeated(rows_b)
    findings: list[Finding] = []
    rtext = lambda d, r, n=14: snippet(d, r.idx, n)

    def add(sev, msg, a_locs, b_locs, kind, critical=False, a_at=None, b_at=None, detail=None):
        findings.append(Finding("tables", sev, msg, a_locs, b_locs, {"kind": kind, **(detail or {})}, baseline_at=a_at,
                                candidate_at=b_at, critical=critical, types=[kind]))

    # ---- rows: missing / split / merged
    target: dict[int, list[int]] = defaultdict(list)  # stage row -> prod rows mapped into it
    missing_row: dict[int, Finding] = {}
    for k, r in enumerate(rows_a):
        if fwd[k]:
            target[fwd[k][0][0]].append(k)
            continue
        parts = _split(r, rows_b, thr)
        if parts and _wrapped(r, [rows_b[m] for m in parts[1:]]):
            continue  # the row's cell text continues on the next line/page in stage
        if parts:
            # the same prod row on both sides of a page break: one finding, its stage rows together
            same = next((f for f in findings if f.detail.get("kind") == "row split"
                         and f.message.startswith(f"Row split in stage: prod row “{rtext(A, r)}”")), None)
            if same is not None:
                same.baseline.append(Loc(r.page, r.box))
                same.candidate += [Loc(rows_b[m].page, rows_b[m].box) for m in parts]
                continue
            add(tcfg.get("split_row_severity", "warning"),
                f"Row split in stage: prod row “{rtext(A, r)}” is spread over {len(parts)} stage rows",
                [Loc(r.page, r.box)], [Loc(rows_b[m].page, rows_b[m].box) for m in parts], "row split")
            continue
        n = sum(r.bag.values())
        in_text = _label_ok(r, text_b) and n and sum((r.bag & text_b).values()) / n >= thr
        paired = sum(i in al.a2b for i in r.idx) / len(r.idx) >= thr
        if in_text or paired:
            continue  # the row's text exists in stage, just not inside a detected table row
        add(tcfg.get("missing_row_severity", "error"), f"Table row missing in stage: “{rtext(A, r)}” (prod p.{r.page + 1})",
            [Loc(r.page, r.box)], [], "missing row", critical=True, b_at=al.loc_in_b(r.idx[0]),
            # so a later pass can check the row is not just baked into a stage picture (a button /
            # legend bar drawn under a device mockup, read as a one-row table)
            detail={"baseline_text": snippet(A, r.idx)})
        missing_row[k] = findings[-1]
    for m, ks in target.items():
        ks = [k for k in ks if rows_a[k].table == rows_a[ks[0]].table]
        if len(ks) >= 2 and not all(rows_a[k].bag == rows_a[ks[0]].bag for k in ks) \
                and not _wrapped(rows_a[ks[0]], [rows_a[k] for k in ks[1:]]):
            s = rows_b[m]
            add(tcfg.get("merged_row_severity", "warning"),
                f"Rows merged in stage: {len(ks)} prod rows ({' | '.join('“' + rtext(A, rows_a[k], 6) + '”' for k in ks)}) "
                f"are one row in stage", [Loc(rows_a[k].page, rows_a[k].box) for k in ks], [Loc(s.page, s.box)], "rows merged")

    # ---- rows: extra in stage
    for m, s in enumerate(rows_b):
        if back[m] or m in target:
            continue
        n = sum(s.bag.values())
        if (_label_ok(s, text_a) and n and sum((s.bag & text_a).values()) / n >= thr) or \
                sum(i in al.b2a for i in s.idx) / len(s.idx) >= thr:
            continue
        add(tcfg.get("extra_row_severity", "warning"), f"Extra table row in stage: “{rtext(B, s)}” (stage p.{s.page + 1})",
            [], [Loc(s.page, s.box)], "extra row", a_at=al.loc_in_a(s.idx[0]))

    # ---- cells merged / split, reported once per prod table
    if tcfg.get("check_cells", True):
        cell_diffs: dict[tuple, list] = defaultdict(list)
        for k, r in enumerate(rows_a):
            if fwd[k] and len(target.get(fwd[k][0][0], [])) == 1:
                s = rows_b[fwd[k][0][0]]
                if s.cells != r.cells and min(s.cells, r.cells) >= 1 and max(s.cells, r.cells) >= 2 \
                        and id(r) not in rep_a:
                    cell_diffs[r.table].append((r, s))
        for key, diffs in cell_diffs.items():
            for kind, sel in (("cells merged", lambda r, s: s.cells < r.cells), ("cells split", lambda r, s: s.cells > r.cells)):
                ds = [(r, s) for r, s in diffs if sel(r, s)]
                if not ds:
                    continue
                ex = ", ".join(f"“{rtext(A, r, 4)}” {r.cells}→{s.cells}" for r, s in ds)
                add(tcfg.get("cells_severity", "warning"),
                    f"{'Cells merged' if kind == 'cells merged' else 'Cells split'} in stage: {len(ds)} row(s) have "
                    f"{'fewer' if kind == 'cells merged' else 'more'} cells than in prod (e.g. {ex})",
                    [Loc(r.page, r.box) for r, _ in ds], [Loc(s.page, s.box) for _, s in ds], kind)

    # ---- tables: missing / extra / merged / split / turned into text
    row_table_b = {m: rows_b[m].table for m in range(len(rows_b))}
    for t in ta:
        ks = [rows_a.index(r) for r in t.rows if id(r) not in rep_a]
        # a stage table counts as a destination only with a real share of the rows: a timing table's rows
        # (“640x480 60 V V V”) also read the same in the next model's table, a few stray matches are no split
        hits = Counter(row_table_b[fwd[k][0][0]] for k in ks if fwd[k] and id(rows_b[fwd[k][0][0]]) not in rep_b)
        need = max(3, 0.2 * sum(hits.values())) if hits else 0
        dest = {key for key, n in hits.items() if n >= need} or set(hits)
        tbag = Counter()
        for r in t.rows:
            tbag |= r.bag
        n = sum(tbag.values())
        if not dest and not ks:
            continue  # only repeated header rows: nothing to relate
        if not dest:
            mirror = [al.a2b[i] for r in t.rows for i in r.idx if i in al.a2b]
            if len(mirror) >= 4 and (_tabular(B, mirror) or _in_detected_table(B, mirror)
                                     or not _rows_run_together(t.rows, al.a2b, B)):
                continue  # still tabular in stage (columns / detected table / rows on their own lines)
            if n and sum((tbag & text_b).values()) / n >= thr:
                add(tcfg.get("table_to_text_severity", "warning"),
                    f"Table turned into plain text in stage (prod p.{t.page + 1}: “{rtext(A, t.rows[0], 8)}”)",
                    [Loc(t.page, t.bbox)], [], "table to text", b_at=al.loc_in_b(t.rows[0].idx[0]))
            else:
                add(tcfg.get("missing_table_severity", "error"),
                    f"Table missing in stage (prod p.{t.page + 1}, {len(t.rows)} rows: “{rtext(A, t.rows[0], 8)}”)",
                    [Loc(t.page, t.bbox)], [], "missing table", critical=True, b_at=al.loc_in_b(t.rows[0].idx[0]),
                    # the whole table's text, so a later pass can check it is not just baked into a stage
                    # picture (a device's on-screen-display panel set as a photo instead of a real table)
                    detail={"baseline_text": snippet(A, [i for r in t.rows for i in r.idx])})
                # its rows are part of this one finding, not each a "row missing" of their own
                for k in [k for k, r in enumerate(rows_a) if r.table == t.key and k in missing_row]:
                    if missing_row[k] in findings:
                        findings.remove(missing_row.pop(k))
        else:
            _missing_header(t, ta, tb, dest, rows_a, missing_row, findings, add, tcfg, thr, A, B, al, rtext)
        if len(dest) >= 2:
            bt = [x for x in tb if x.key in dest]
            if not _one_table(tb, bt, B):
                add(tcfg.get("table_split_severity", "warning"),
                    f"Table split in stage: prod table (p.{t.page + 1}, “{rtext(A, t.rows[0], 6)}”) is {len(dest)} tables in stage",
                    [Loc(t.page, t.bbox)], [Loc(x.page, x.bbox) for x in bt], "table split")
    src_n: dict[tuple, Counter] = defaultdict(Counter)
    for k, r in enumerate(rows_a):
        if fwd[k] and id(r) not in rep_a and id(rows_b[fwd[k][0][0]]) not in rep_b:
            src_n[row_table_b[fwd[k][0][0]]][r.table] += 1
    for t in tb:
        cnt = src_n.get(t.key, Counter())
        need = max(3, 0.2 * sum(cnt.values())) if cnt else 0
        # a prod table counts with a real share of the rows, or when all its rows went there (a small table
        # of two rows merged into the one above it)
        size = Counter(r.table for r in rows_a if id(r) not in rep_a)
        srcs = {key for key, n in cnt.items() if n >= need or n >= size.get(key, 0) >= 1}
        if len(srcs) >= 2:
            at = [x for x in ta if x.key in srcs]
            if not _one_table(ta, at, A):
                add(tcfg.get("table_merged_severity", "warning"),
                    f"Tables merged in stage: {len(srcs)} prod tables are one table in stage (stage p.{t.page + 1}, "
                    f"“{rtext(B, t.rows[0], 6)}”)", [Loc(x.page, x.bbox) for x in at], [Loc(t.page, t.bbox)], "tables merged")
        elif not srcs:
            mirror = [al.b2a[i] for r in t.rows for i in r.idx if i in al.b2a]
            cols = max((r.cells for r in t.rows), default=0)
            cell = _one_cell_box(A, mirror) if cols >= 2 and len(mirror) >= 2 else None
            if cell is not None:
                # prod holds the data in one cell (a one-column / one-cell table), stage splits it into columns
                add(tcfg.get("cells_severity", "warning"),
                    f"Cells split in stage: the data is one cell in prod (p.{cell[0] + 1}), a {cols}-column table in "
                    f"stage (stage p.{t.page + 1}, {len(t.rows)} row(s): “{rtext(B, t.rows[0], 8)}”)",
                    [Loc(cell[0], cell[1])], [Loc(t.page, t.bbox)], "cells split")
                continue
            if len(mirror) >= 4 and (_tabular(A, mirror) or _in_detected_table(A, mirror)
                                     or not _rows_run_together(t.rows, al.b2a, A)):
                continue  # already tabular in prod (columns / detected table / rows on their own lines)
            tbag = Counter()
            for r in t.rows:
                tbag |= r.bag
            n = sum(tbag.values())
            if n and sum((tbag & text_a).values()) / n >= thr:
                add(tcfg.get("text_to_table_severity", "info"),
                    f"Text turned into a table in stage (stage p.{t.page + 1}: “{rtext(B, t.rows[0], 8)}”)",
                    [], [Loc(t.page, t.bbox)], "text to table", a_at=al.loc_in_a(t.rows[0].idx[0]))
            else:
                add(tcfg.get("extra_table_severity", "warning"),
                    f"Extra table in stage (stage p.{t.page + 1}, {len(t.rows)} rows: “{rtext(B, t.rows[0], 8)}”)",
                    [], [Loc(t.page, t.bbox)], "extra table", a_at=al.loc_in_a(t.rows[0].idx[0]))
    # ---- rows in another order: each prod table's rows, in prod order, should come in the same order in
    # stage. The rows outside the longest run that keeps the order are the ones that moved.
    if tcfg.get("check_row_order", True):
        for t in ta:
            seq = [(k, fwd[k][0][0]) for k, r in enumerate(rows_a)
                   if r.table == t.key and fwd[k] and id(r) not in rep_a and id(rows_b[fwd[k][0][0]]) not in rep_b]
            keep = _longest_increasing([m for _, m in seq])
            moved = [k for n, (k, _) in enumerate(seq) if n not in keep]
            if moved and len(seq) >= 3:
                ex = "; ".join(f"“{rtext(A, rows_a[k], 6)}”" for k in moved)
                add(tcfg.get("row_order_severity", "error"),
                    f"Table rows in another order in stage: {len(moved)} row(s) of the prod table (p.{t.page + 1}) "
                    f"are not in the prod order (e.g. {ex})",
                    [Loc(rows_a[k].page, rows_a[k].box) for k in moved],
                    [Loc(rows_b[fwd[k][0][0]].page, rows_b[fwd[k][0][0]].box) for k in moved], "row order")
    # ---- a table running onto a new page must repeat its header there (stage)
    if tcfg.get("check_continuation_header", True):
        tb = tb_pages  # page by page: the header each continuation page shows
        for k in range(1, len(tb)):
            prev, t = tb[k - 1], tb[k]
            if not _continuation(prev, t, B) or len(t.rows) < 1:
                continue
            if any(w.page == t.page and w.norm and w.bbox[3] <= t.bbox[1] + 1 for w in B.words):
                continue  # text above it on the new page (a heading): a new table, not the old one continued
            first = prev
            for q in range(k - 1, 0, -1):  # the page the table starts on
                if _continuation(tb[q - 1], tb[q], B):
                    first = tb[q - 1]
                else:
                    break
            head = first.rows[0]
            if len(first.rows) < 2 or not sum(head.bag.values()):
                continue
            # only a real header row: mostly bold / heavier than the row under it, and short - a menu
            # table that starts straight with data ("Language  Sets the language ...") has none
            hw = [B.words[i] for i in head.idx if B.words[i].norm]
            nw = [B.words[i] for i in first.rows[1].idx if B.words[i].norm]
            heavy = lambda ws: sum(w.style.weight >= 500 for w in ws) / max(1, len(ws))
            if len(hw) > 14 or heavy(hw) < 0.9 or heavy(nw) >= 0.9:
                continue
            # side-by-side tables (“Menu Options” | “Menu Descriptions”) are detected as two: the new page
            # may start with either earlier header
            heads = [x.rows[0].bag for x in tb[:k] if x.rows and sum(x.rows[0].bag.values())]
            same = max((sum((h & t.rows[0].bag).values()) / sum(h.values()) for h in heads), default=0)
            same = max(same, sum((t.rows[0].bag & head.bag).values()) / max(1, sum(t.rows[0].bag.values())))
            if same < 0.8:
                add(tcfg.get("missing_header_severity", "error"),
                    f"Table header missing on continuation page: the table continues on stage p.{t.page + 1} "
                    f"without its header row “{rtext(B, head, 10)}” (first shown on p.{first.page + 1})",
                    [], [Loc(t.page, t.rows[0].box)], "missing header", a_at=None)
    if tcfg.get("check_icons", True):
        findings += _row_icons(u, rows_a, rows_b, fwd, target)
    findings += _overflow(u, al)
    if tcfg.get("check_borders", True):
        findings += _borders(u, al, ta_pages, tb_pages)
    # (a web page's PDF is a screenshot: no rules or fills to read - its borders / shading cannot be compared)
    web = B.raw_tables is not None
    if tcfg.get("check_cell_borders", True) and not web:
        findings += _cell_borders(u, al, tables(A, u.a_range, loose=True))
    if tcfg.get("check_row_background", True) and not web:
        findings += _row_background(u, tb_pages)
    if tcfg.get("check_header_align", True):
        findings += _header_align(u, tb_pages)
    if tcfg.get("check_single_column_center", True):
        findings += _single_column_center(u)
    return findings


def _icons_in(doc: Doc, row: TRow) -> list:
    """The small pictures (icons, buttons) inside a table row, left to right then top to bottom."""
    x0, y0, x1, y1 = row.box
    ims = [im for im in doc.images if im.page == row.page and im.bbox[2] - im.bbox[0] <= 60
           and x0 - 2 <= (im.bbox[0] + im.bbox[2]) / 2 <= x1 + 2 and y0 - 2 <= (im.bbox[1] + im.bbox[3]) / 2 <= y1 + 2]
    # a note's own icon (the pencil before “NOTE” in a note box inside the cell) belongs to the note, not the row
    labels = [doc.words[i] for i in row.idx if doc.words[i].norm.startswith("<label:")]
    ims = [im for im in ims if not any(0 <= w.bbox[0] - im.bbox[2] <= 30 and w.bbox[1] < im.bbox[3] + 4
                                       and im.bbox[1] < w.bbox[3] + 4 for w in labels)]
    return sorted(ims, key=lambda im: (round(im.bbox[1] / 8), im.bbox[0]))


def _row_icons(u: Unit, rows_a: list[TRow], rows_b: list[TRow], fwd: dict, target: dict) -> list[Finding]:
    """The icons in each table row, prod against stage (rows paired one to one): an icon missing or extra in
    stage, and an icon set above its text in stage where prod sets it beside the text in a column of its own.
    One finding per kind and table."""
    A, B = u.a, u.b
    sev = u.cfg["tables"].get("icon_severity", "warning")
    rtext = lambda d, r: snippet(d, r.label or r.idx, 6)
    groups: dict[tuple, list] = defaultdict(list)
    for k, r in enumerate(rows_a):
        if not fwd.get(k) or len(target.get(fwd[k][0][0], [])) != 1:
            continue
        s = rows_b[fwd[k][0][0]]
        ia, ib = _icons_in(A, r), _icons_in(B, s)
        if not ia and not ib:
            continue
        # a note's icon in one document, its label ("NOTE") in the other: the same house-style difference
        # _icons_in already excludes within one document - here the icon has no label there to exclude it
        # by (the prod note is icon-only), so the row's own label words settle it instead
        has_label = lambda d, row: any(d.words[i].norm.startswith("<label:") for i in row.idx)
        if len(ia) != len(ib) and (has_label(A, r) or has_label(B, s)):
            continue
        if len(ia) != len(ib):
            groups[(r.table, "icon missing" if len(ia) > len(ib) else "icon extra")].append((r, s, ia, ib))
            continue
        # where the icon sits against the row's first text: beside it (prod) vs on a line above it (stage)
        word = lambda d, i: d.words[i].norm and any(c.isalnum() for c in d.words[i].text)
        la = [A.words[i] for i in r.idx if word(A, i)]  # the row's first real word (“/” between two icons is not)
        lb = [B.words[i] for i in s.idx if word(B, i)]
        if la and lb:
            beside = lambda im, w: im.bbox[2] <= w.bbox[0] + 2 and im.bbox[1] < w.bbox[3] and w.bbox[1] < im.bbox[3]
            above = lambda im, w: im.bbox[3] <= w.bbox[1] + 2 and abs(im.bbox[0] - w.bbox[0]) <= 40
            if beside(ia[-1], la[0]) and above(ib[-1], lb[0]):
                groups[(r.table, "icon above text")].append((r, s, ia, ib))
    out = []
    for (_, kind), items in groups.items():
        eg = "; ".join(f"“{rtext(A, r)}”" for r, *_ in items[:4])
        msg = {"icon missing": f"Icon missing in a table row in stage: {len(items)} row(s) show fewer icons than in prod (e.g. {eg})",
               "icon extra": f"Extra icon in a table row in stage: {len(items)} row(s) show more icons than in prod (e.g. {eg})",
               "icon above text": f"Icon not beside its text in stage: in {len(items)} table row(s) the icon sits above the "
                                  f"text in the same cell, where prod sets it beside the text in a column of its own (e.g. {eg})"}[kind]
        out.append(Finding("tables", sev, msg,
                           [Loc(im.page, im.bbox) for _, _, ia, _ in items for im in ia] or [Loc(r.page, r.box) for r, *_ in items],
                           [Loc(im.page, im.bbox) for _, _, _, ib in items for im in ib] or [Loc(s.page, s.box) for _, s, *_ in items],
                           {"kind": kind, "rows": len(items)}, types=[kind]))
    return out


def _is_header_bar(doc: Doc, row: TRow) -> bool:
    """The row sits on a dark header bar (the spec's #333333, white text)."""
    bg = _background(doc, row.page, row.box)
    if not bg:
        return False
    r, g, b = (int(bg[k:k + 2], 16) for k in (1, 3, 5))
    return (r + g + b) / 3 < 110


def _header_align(u: Unit, tb: list) -> list[Finding]:
    """Table header cells: each line of header text is left-aligned in its column (design spec: table
    headers are always left-aligned, never centred or right-aligned - an absolute rule, not judged
    against what prod happens to do). A header off the left edge by more than header_align_tolerance pt
    is reported, named by how it is actually aligned instead (centred / right-aligned / indented)."""
    tol = u.cfg["tables"].get("header_align_tolerance", 4.0)
    sev = u.cfg["tables"].get("header_align_severity", "error")
    A, B = u.a, u.b
    b2a = {j: i for i, j in u.pairs}
    rcfg = u.cfg["report"]
    out = []
    for t in tb:
        raw = next((r for r in _raw(B, t.page) if r[0] == t.key[1]), None)
        if not raw or not is_data_table(B, t):
            continue
        grid = raw[3]
        head = t.rows[0]
        if not _is_header_bar(B, head):
            continue
        bad = []
        for x0, x1 in grid:
            words = [i for i in head.idx if x0 - 1 <= (B.words[i].bbox[0] + B.words[i].bbox[2]) / 2 <= x1 + 1]
            if not words:
                continue
            by_line: dict[int, list[int]] = defaultdict(list)
            for i in words:
                by_line[B.words[i].line].append(i)
            hows = []
            for ln, ws in by_line.items():
                left = min(B.words[i].bbox[0] for i in ws) - x0
                right = x1 - max(B.words[i].bbox[2] for i in ws)
                if left < 0 or left <= tol:
                    continue  # at (or past) the column's left edge: left-aligned, as the spec requires
                hows.append("centred" if right >= 0 and abs(left - right) <= 2 * tol else "right-aligned" if 0 <= right <= tol
                            else f"indented {left:.0f}pt from the left edge")
            if hows:  # one entry per header cell, however many lines it wraps to
                bad.append((sorted(words, key=lambda i: (B.words[i].line, B.words[i].bbox[0])), hows[0]))
        if not bad:
            continue
        idx = [i for ws, _ in bad for i in ws]
        names = "; ".join(f"“{' '.join(B.words[i].text for i in ws)}” {how}" for ws, how in bad)
        a_idx = [b2a[j] for j in idx if j in b2a]
        out.append(Finding(
            "tables", sev,
            f"Table header not left-aligned in stage ({len(bad)} cell{'s' if len(bad) > 1 else ''}): {names}"
            + " — table headers must be left-aligned (design spec)",
            locs(A, a_idx, rcfg["max_locs"]) if a_idx else [], locs(B, idx, rcfg["max_locs"]),
            {"kind": "table header alignment", "cells": len(bad)}, types=["table header alignment"]))
    return out


def _single_column_center(u: Unit) -> list[Finding]:
    """A table with only one column (a key / single-field layout, not a multi-column data table): every
    cell's text, header and data alike, must be centred in that one column - the opposite rule from a
    normal multi-column table's header (left-aligned, see _header_align). `tables()` itself only ever
    builds a TTable for >= 2 grid columns (a 1-column grid would not be a "data table" by that measure),
    so this reads the raw per-page grid directly instead of the shared TTable list."""
    B = u.b
    if B.raw_tables is None:
        return []
    A = u.a
    tol = u.cfg["tables"].get("single_column_tolerance", 4.0)
    sev = u.cfg["tables"].get("single_column_severity", "error")
    b2a = {j: i for i, j in u.pairs}
    rcfg = u.cfg["report"]
    out = []
    for p in sorted({B.words[i].page for i in range(*u.b_range)}):
        figures = diagram_boxes(B, p)
        for _, tbox, rows, grid in _raw(B, p):
            if len(grid) != 1 or len(rows) < 2 or tuple(tbox) in figures:
                continue
            x0, x1 = grid[0]
            row_boxes = [(tbox[0], box[1], tbox[2], box[3]) for box, _ in rows]
            row_words = [[i for i in _inside(B, u.b_range, p, box) if B.words[i].norm] for box in row_boxes]
            # a note / tip / warning box the detector framed as a one-column grid: not a real table
            if any(B.words[i].norm.startswith("<label:") or _CALLOUT_LABEL.match(B.words[i].text.strip())
                  for words in row_words for i in words[:2]):
                continue
            bad = []
            for words in row_words:
                if not words:
                    continue
                by_line: dict[int, list[int]] = defaultdict(list)
                for i in words:
                    by_line[B.words[i].line].append(i)
                for ln, ws in by_line.items():
                    left = min(B.words[i].bbox[0] for i in ws) - x0
                    right = x1 - max(B.words[i].bbox[2] for i in ws)
                    if left < 0 or right < 0 or abs(left - right) <= tol:
                        continue  # already centred (within tolerance), or wider than the column: nothing to centre
                    how = ("left-aligned" if left <= tol else "right-aligned" if right <= tol
                          else f"off-centre by {abs(left - right) / 2:.0f}pt")
                    bad.append((sorted(ws, key=lambda i: B.words[i].bbox[0]), how))
            if not bad:
                continue
            idx = [i for ws, _ in bad for i in ws]
            names = "; ".join(f"“{' '.join(B.words[i].text for i in ws)}” {how}" for ws, how in bad[:6])
            a_idx = [b2a[j] for j in idx if j in b2a]
            out.append(Finding(
                "tables", sev,
                f"Single-column table cell not centred in stage ({len(bad)} cell{'s' if len(bad) > 1 else ''}): {names}"
                + " — a single-column table's text must be centred (design spec)",
                locs(A, a_idx, rcfg["max_locs"]) if a_idx else [], locs(B, idx, rcfg["max_locs"]),
                {"kind": "single column alignment", "cells": len(bad)}, types=["single column alignment"]))
    return out



_BG: "OrderedDict[tuple, tuple]" = None  # (path, page) -> (RGB samples, width, height) of the page at 1 px/pt


def _background(doc: Doc, page: int, box) -> str | None:
    """The colour behind a box on the rendered page: its most common pixel colour (text is the minority)."""
    global _BG
    from collections import OrderedDict
    if _BG is None:
        _BG = OrderedDict()
    key = (doc.path, page)
    if key not in _BG:
        pdf = (_DOCS.get(doc.path) or _DOCS.setdefault(doc.path, pymupdf.open(doc.path)))
        pix = pdf[page].get_pixmap(matrix=pymupdf.Matrix(1, 1), colorspace=pymupdf.csRGB, alpha=False)
        _BG[key] = (pix.samples, pix.width, pix.height)
        while len(_BG) > 8:
            _BG.popitem(last=False)
    px, W, H = _BG[key]
    x0, y0 = max(0, int(box[0])), max(0, int(box[1]))
    x1, y1 = min(W, int(box[2]) + 1), min(H, int(box[3]) + 1)
    seen = Counter(bytes(px[(y * W + x) * 3:(y * W + x) * 3 + 3]) for y in range(y0, y1, 2) for x in range(x0, x1, 2))
    if not seen:
        return None
    return "#%02x%02x%02x" % tuple(seen.most_common(1)[0][0])


def _on_picture(doc: Doc, page: int, box) -> bool:
    """The middle of the box lies on an embedded picture."""
    pdf = (_DOCS.get(doc.path) or _DOCS.setdefault(doc.path, pymupdf.open(doc.path)))
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in (i["bbox"] for i in pdf[page].get_image_info()))


def _row_background(u: Unit, tb: list) -> list[Finding]:
    """Table rows shaded on one side only: prod sets the group rows between the data rows ("Power mode",
    "Video Output") on a grey band, stage prints them plain - or the other way round. Each stage row
    (the header row aside: its bar is the design spec's business) against the same words in prod: the
    colour behind the row's first cell on both sides."""
    from .style import color_distance
    tcfg = u.cfg["tables"]
    tol = u.cfg["style"].get("color_tolerance", 24)
    A, B = u.a, u.b
    prod_of = {j: i for i, j in u.pairs + u.style_pairs}  # text the diff matched, and text read in another order
    a_norms = [A.words[i].norm for i in range(*u.a_range)]

    def by_text(cell: list[int]) -> list[int]:
        """The cell's words in prod, looked up by their text when the diff did not pair them (a table read
        column by column in one PDF, row by row in the other)."""
        seq = [B.words[j].norm for j in cell]
        hits = [u.a_range[0] + k for k in range(len(a_norms) - len(seq) + 1) if a_norms[k:k + len(seq)] == seq]
        return list(range(hits[0], hits[0] + len(seq))) if len(hits) == 1 else []
    shaded = lambda c: c is not None and color_distance(c, "#ffffff") > tol
    light = lambda w: len(w.style.color) == 7 and sum(int(w.style.color[k:k + 2], 16) for k in (1, 3, 5)) > 600
    box_of = lambda d, idx, pad=3: (min(d.words[i].bbox[0] for i in idx) - pad, min(d.words[i].bbox[1] for i in idx) - pad,
                                    max(d.words[i].bbox[2] for i in idx) + pad, max(d.words[i].bbox[3] for i in idx) + pad)
    def prod_words(cell: list[int]) -> tuple[list[int], list[int]]:
        """(stage words, the same words in prod) of a cell: paired by the diff, else looked up by text."""
        mine = [j for j in cell if j in prod_of]
        theirs = [prod_of[j] for j in mine]
        if len(mine) * 2 < len(cell):
            theirs = by_text(cell)  # (a unique run of the same words: "Networking" once in the section)
            mine = cell if theirs else []
        if theirs and len({A.words[i].page for i in theirs}) > 1:
            return [], []
        return mine, theirs

    # (shaded on which side, "row" | "first cell") -> [(stage row, prod words, stage words, prod colour, stage colour)]
    diff: dict[tuple, list] = defaultdict(list)
    for t in tb:
        if not is_data_table(B, t):
            continue
        for k, row in enumerate(t.rows):
            cell = [j for j in (row.label or row.idx) if B.words[j].norm]
            if k == 0 or not cell or all(light(B.words[j]) for j in cell):
                continue  # the header row / a header bar repeated on the next page
            if not any(ch.isalnum() for j in cell for ch in B.words[j].text):
                continue  # a "/" between two icons: the icons, not a shading, are what is behind it
            mine, theirs = prod_words(cell)
            if not theirs:
                continue  # not the same text in prod: nothing to compare the background with
            if _on_picture(A, A.words[theirs[0]].page, box_of(A, theirs)) or _on_picture(B, row.page, box_of(B, mine)):
                continue  # a picture behind the cell (an icon, a screenshot) is not shading
            ca = _background(A, A.words[theirs[0]].page, box_of(A, theirs))
            cb = _background(B, row.page, box_of(B, mine))
            if shaded(ca) == shaded(cb):
                continue
            # the whole row, or only its first cell (a shaded label column): the other cells tell
            rest = [j for j in row.idx if j not in set(row.label) and B.words[j].norm]
            rm, rt = prod_words(rest) if rest else ([], [])
            other = (_background(A, A.words[rt[0]].page, box_of(A, rt)) if shaded(ca) else
                     _background(B, row.page, box_of(B, rm))) if rt else None
            kind = "first cell" if other is not None and not shaded(other) else "row"
            diff[("prod" if shaded(ca) else "stage", kind)].append((row, theirs, mine, ca, cb))
    out = []
    for (side, kind), rows in diff.items():
        n = len(rows)
        eg = ", ".join(f"“{snippet(B, m, 5)}”" for _, _, m, _, _ in rows[:4]) + (" …" if n > 4 else "")
        cols = lambda k: ", ".join(dict.fromkeys(r[k].upper() for r in rows if r[k]))
        what = "rows" if kind == "row" else "first cells"
        other = "stage" if side == "prod" else "prod"
        if kind == "row":
            head = f"Table row background — {n} row(s) shaded in {side}, plain in {other} (e.g. {eg})"
        else:
            head = f"Table first-column background — {n} row(s) with the first cell shaded in {side}, plain in {other} (e.g. {eg})"
        prod_line = f"{what} shaded {cols(3)}" if side == "prod" else f"{what} plain {cols(3)}"
        stage_line = f"the same {what} plain {cols(4)}" if side == "prod" else f"the same {what} shaded {cols(4)}"
        a_locs = [Loc(A.words[th[0]].page, box_of(A, th, 0)) for _, th, _, _, _ in rows]
        b_locs = [Loc(r.page, r.box if kind == "row" else box_of(B, m, 0)) for r, _, m, _, _ in rows]
        out.append(Finding("tables", tcfg.get("row_background_severity", "warning"),
                           f"{head}\nProd: {prod_line}\nStage: {stage_line}", a_locs, b_locs,
                           {"kind": "row background", "prod": prod_line, "stage": stage_line, "rows": n, "cells": kind},
                           types=["row background"], links=list(zip(a_locs, b_locs))))
    return out


def _rules(doc: Doc, page: int) -> tuple[list, list]:
    """Border segments drawn on the page: vertical (x, y0, y1) and horizontal (y, x0, x1).
    Stroked lines and rectangle edges, and hairline filled rectangles (rules drawn as fills)."""
    key = (doc.path, page)
    if key not in _RULES:
        pdf = (_DOCS.get(doc.path) or _DOCS.setdefault(doc.path, pymupdf.open(doc.path)))
        ver, hor = [], []
        try:
            drawings = pdf[page].get_drawings()
        except Exception:
            drawings = []
        for d in drawings:
            stroked = d.get("color") is not None
            for it in d["items"]:
                if it[0] == "l":
                    p0, p1 = it[1], it[2]
                    if abs(p0.x - p1.x) < 1 and abs(p0.y - p1.y) > 4:
                        ver.append((p0.x, min(p0.y, p1.y), max(p0.y, p1.y)))
                    elif abs(p0.y - p1.y) < 1 and abs(p0.x - p1.x) > 4:
                        hor.append((p0.y, min(p0.x, p1.x), max(p0.x, p1.x)))
                elif it[0] == "re":
                    r = it[1]
                    if r.width < 2.5 and r.height > 4:
                        ver.append(((r.x0 + r.x1) / 2, r.y0, r.y1))
                    elif r.height < 2.5 and r.width > 4:
                        hor.append(((r.y0 + r.y1) / 2, r.x0, r.x1))
                    elif stroked:
                        ver += [(r.x0, r.y0, r.y1), (r.x1, r.y0, r.y1)]
                        hor += [(r.y0, r.x0, r.x1), (r.y1, r.x0, r.x1)]
        _RULES[key] = (ver, hor)
    return _RULES[key]


_EDGES = ("top", "bottom", "left", "right")
def _border_color(doc: Doc, page: int, box, edges: tuple = _EDGES) -> str | None:
    """The colour a table's outline is drawn in, read from the rendered page: along each drawn edge,
    the pixel that stands out most from the paper within 3 pt of the line, then the most common of
    those. Works however the border is made (stroked line, thin fill, or cell backgrounds leaving a
    gap over a darker panel)."""
    pdf = (_DOCS.get(doc.path) or _DOCS.setdefault(doc.path, pymupdf.open(doc.path)))
    x0, y0, x1, y1 = box
    Z = 3.0
    pad = 4
    clip = pymupdf.Rect(x0 - pad, y0 - pad, x1 + pad, y1 + pad) & pdf[page].rect
    if clip.is_empty:
        return None
    pix = pdf[page].get_pixmap(clip=clip, matrix=pymupdf.Matrix(Z, Z), colorspace=pymupdf.csRGB, alpha=False)
    W, H, n = pix.width, pix.height, pix.n
    px = pix.samples
    at = lambda x, y: tuple(px[(y * W + x) * n:(y * W + x) * n + 3])
    ink = lambda c: 765 - sum(c)  # how far from white
    found = Counter()
    samples = {"top": [(k, y0) for k in range(8)], "bottom": [(k, y1) for k in range(8)],
               "left": [(x0, k) for k in range(8)], "right": [(x1, k) for k in range(8)]}
    for e in edges:
        for k, fixed in samples[e]:
            if e in ("top", "bottom"):
                x = int(((x0 + (x1 - x0) * (k + 0.5) / 8) - clip.x0) * Z)
                ys = range(max(0, int((fixed - 3 - clip.y0) * Z)), min(H, int((fixed + 3 - clip.y0) * Z) + 1))
                cand = [at(min(max(x, 0), W - 1), y) for y in ys]
            else:
                y = int(((y0 + (y1 - y0) * (k + 0.5) / 8) - clip.y0) * Z)
                xs = range(max(0, int((fixed - 3 - clip.x0) * Z)), min(W, int((fixed + 3 - clip.x0) * Z) + 1))
                cand = [at(x, min(max(y, 0), H - 1)) for x in xs]
            if cand:
                c = max(cand, key=ink)
                if ink(c) > 30:
                    found["#%02x%02x%02x" % tuple(round(v / 17) * 17 for v in c)] += 1
    return found.most_common(1)[0][0] if found else None


def _border_profile(doc: Doc, t: "TTable", tol: float = 3.0, bounds_y: list | None = None, cols_x: list | None = None) -> dict:
    """Which borders of a table are drawn: each outer edge, and the share of row and column
    boundaries that have a rule. An edge counts when rules cover >= 60 % of its length.
    bounds_y / cols_x: row / column boundaries to test (default: the table's own rows and grid)."""
    ver, hor = _rules(doc, t.page)
    x0, y0, x1, y1 = t.bbox
    W, H = max(x1 - x0, 1), max(y1 - y0, 1)

    def h_cov(y, a, b):  # share of [a, b] covered by horizontal rules at height y
        segs = sorted((max(s0, a), min(s1, b)) for yy, s0, s1 in hor if abs(yy - y) <= tol and s1 > a and s0 < b)
        return _covered(segs) / max(b - a, 1)

    def v_cov(x, a, b):
        segs = sorted((max(s0, a), min(s1, b)) for xx, s0, s1 in ver if abs(xx - x) <= tol and s1 > a and s0 < b)
        return _covered(segs) / max(b - a, 1)
    prof = {"top": h_cov(y0, x0, x1) >= 0.6, "bottom": h_cov(y1, x0, x1) >= 0.6,
            "left": v_cov(x0, y0, y1) >= 0.6, "right": v_cov(x1, y0, y1) >= 0.6}
    if bounds_y is None:
        rows = sorted(t.rows, key=lambda r: r.box[1])
        bounds_y = [(a.box[3] + b.box[1]) / 2 for a, b in zip(rows, rows[1:]) if b.box[1] - a.box[3] > -2]
    # a row line sits anywhere in the gap between two rows' text: test the whole gap
    prof["rows"] = (sum(max(h_cov(y + d, x0, x1) for d in (-6, -3, 0, 3, 6)) >= 0.6 for y in bounds_y) / len(bounds_y)) if bounds_y else None
    if cols_x is None:
        grid = next((g for k, tb_, _, g in _raw(doc, t.page) if (t.page, k) == t.key), []) or []
        cols_x = [(a[1] + b[0]) / 2 for a, b in zip(grid, grid[1:])]
    prof["columns"] = (sum(max(v_cov(x + d, y0, y1) for d in (-6, -3, 0, 3, 6)) >= 0.6 for x in cols_x) / len(cols_x)) if cols_x else None
    prof["color"] = _border_color(doc, t.page, t.bbox, tuple(e for e in _EDGES if prof[e])) if any(prof[e] for e in _EDGES) else None
    return prof


def _covered(segs: list) -> float:
    total, end = 0.0, None
    for a, b in segs:
        if end is None or a > end:
            total += b - a
            end = b
        elif b > end:
            total += b - end
            end = b
    return total


_CALLOUT_LABEL = re.compile(r"^(notes?|tips?|hints?|warnings?|important|cautions?|danger|notice|attention)\s*:?$", re.I)


def is_data_table(d: Doc, t: "TTable") -> bool:
    """A table of data, not a figure or callout the detector framed: at least two rows with text in
    two or more cells, not mostly covered by a picture, and not a note / tip box."""
    if sum(1 for r in t.rows if r.cells >= 2) < 2 or is_callout(d, t):
        return False
    tb_ = pymupdf.Rect(t.bbox)
    pic = sum((pymupdf.Rect(im.bbox) & tb_).get_area() for im in d.images if im.page == t.page)
    drawn = [r for r in (pymupdf.Rect(c) for c in _figure_rects(d, t.page)) if (r & tb_).get_area() > 0]
    return pic < 0.3 * tb_.get_area() and sum((r & tb_).get_area() for r in drawn) < 0.3 * tb_.get_area()


def is_callout(d: Doc, t: "TTable") -> bool:
    """A note / tip / warning box the detector framed as a table (icon | text): a row starts with a
    callout label ("NOTE:", "TIP", "IMPORTANT")."""
    for r in t.rows:
        first = [d.words[i] for i in r.idx[:2]]
        if any(w.norm.startswith("<label:") or _CALLOUT_LABEL.match(w.text.strip()) for w in first):
            return True
    return False


def is_curve(it) -> bool:
    """A drawn curve that really bends: a "c" (bezier) item with some length whose control points leave
    the straight line between its ends, or a "qu" (quad). Renderers draw square box corners as
    zero-length curves (a border radius of 0) and straight edges as flat curves: those are table and
    callout outlines, not the strokes of an illustration."""
    if it[0] == "qu":
        return True
    if it[0] != "c":
        return False
    p0, c1, c2, p3 = it[1:5]
    dx, dy = p3.x - p0.x, p3.y - p0.y
    L = (dx * dx + dy * dy) ** 0.5
    if L < 1.0:
        return False
    return max(abs(dy * (c.x - p0.x) - dx * (c.y - p0.y)) / L for c in (c1, c2)) > 0.6


_FIG: dict[tuple, list] = {}


def _figure_rects(doc: Doc, page: int) -> list:
    """Areas of vector illustrations on the page (clusters of many drawn shapes with curves)."""
    key = (doc.path, page)
    if key not in _FIG:
        pdf = (_DOCS.get(doc.path) or _DOCS.setdefault(doc.path, pymupdf.open(doc.path)))
        out = []
        try:
            pg = pdf[page]
            drawings = pg.get_drawings()
            for r in pg.cluster_drawings(drawings=drawings):
                inside = [d for d in drawings if pymupdf.Rect(d["rect"]).intersects(r)]
                curves = sum(1 for d in inside for it in d["items"] if is_curve(it))
                if curves >= 8:
                    out.append(tuple(r))
        except Exception:
            pass
        _FIG[key] = out
    return _FIG[key]


def _borders(u: Unit, al, ta: list, tb: list) -> list[Finding]:
    """Borders of each prod table that its stage table does not draw (outer edges, lines between
    rows, lines between columns) - and borders stage adds. The stage table is the one holding most
    of the prod table's words."""
    A, B = u.a, u.b
    out = []
    from .style import color_distance
    where_b = {i: t for t in tb for r in t.rows for i in r.idx}

    data_table = is_data_table
    tb = [t for t in tb if data_table(B, t)]
    where_b = {i: t for t in tb for r in t.rows for i in r.idx}
    for t in ta:
        if not data_table(A, t):
            continue
        total = sum(len(r.idx) for r in t.rows)
        hits = Counter(id(where_b[al.a2b[i]]) for r in t.rows for i in r.idx if i in al.a2b and al.a2b[i] in where_b)
        sid, n = hits.most_common(1)[0] if hits else (None, 0)
        if n >= 0.4 * total:
            s = next(x for x in tb if id(x) == sid)
            pa, pb = _border_profile(A, t), _border_profile(B, s)
        else:
            # stage has no table the detector recognises here: judge the rules around the words
            mapped = [al.a2b[i] for r in t.rows for i in r.idx if i in al.a2b]
            if len(mapped) < 0.4 * total:
                continue
            page = Counter(B.words[j].page for j in mapped).most_common(1)[0][0]
            ws = [B.words[j] for j in mapped if B.words[j].page == page]
            pad = 6
            box = (min(w.bbox[0] for w in ws) - pad, min(w.bbox[1] for w in ws) - pad,
                   max(w.bbox[2] for w in ws) + pad, max(w.bbox[3] for w in ws) + pad)
            # widen to the rules that frame the words (a table's outline sits a cell padding away)
            ver, hor = _rules(B, page)
            lefts = [x for x, a, b in ver if box[0] - 40 <= x <= box[0] + pad and b > box[1] and a < box[3]]
            rights = [x for x, a, b in ver if box[2] - pad <= x <= box[2] + 40 and b > box[1] and a < box[3]]
            tops = [y for y, a, b in hor if box[1] - 30 <= y <= box[1] + pad and b > box[0] and a < box[2]]
            bots = [y for y, a, b in hor if box[3] - pad <= y <= box[3] + 30 and b > box[0] and a < box[2]]
            box = (min(lefts, default=box[0]), min(tops, default=box[1]), max(rights, default=box[2]), max(bots, default=box[3]))
            row_y = []
            for r in sorted(t.rows, key=lambda r: r.box[1]):
                ys = [B.words[al.a2b[i]].bbox for i in r.idx if i in al.a2b and B.words[al.a2b[i]].page == page]
                if ys:
                    row_y.append((min(b[1] for b in ys), max(b[3] for b in ys)))
            bounds = [(a[1] + b[0]) / 2 for a, b in zip(row_y, row_y[1:]) if b[0] > a[1]]
            s = TTable((page, -1), page, box, [])
            pa, pb = _border_profile(A, t), _border_profile(B, s, tol=4, bounds_y=bounds, cols_x=[])
            pa["columns"] = pb["columns"] = None  # no stage grid to test columns against
        # clearly another colour (black -> light grey), not the same line anti-aliased a shade lighter
        if pa.get("color") and pb.get("color") and color_distance(pa["color"], pb["color"]) > u.cfg["tables"].get("border_color_tolerance", 150) \
                and any(pa[e] and pb[e] for e in _EDGES):
            out.append(Finding(
                "tables", u.cfg["tables"].get("border_color_severity", "warning"),
                f"Table border colour differs: {pa['color'].upper()} in prod → {pb['color'].upper()} in stage "
                f"(prod p.{t.page + 1} ↔ stage p.{s.page + 1}, table “{' '.join(A.words[i].text for i in t.rows[0].idx)}”)",
                [Loc(t.page, t.bbox)], [Loc(s.page, s.bbox)],
                {"kind": "table-border-color", "baseline": pa["color"], "candidate": pb["color"]}, types=["table border"]))
        missing, added = [], []
        for e in _EDGES:
            if pa[e] and not pb[e]:
                missing.append(f"{e} border")
            elif pb[e] and not pa[e]:
                added.append(f"{e} border")
        for k, name in (("rows", "lines between rows"), ("columns", "lines between columns")):
            if pa[k] is not None and pb[k] is not None:
                if pa[k] >= 0.8 and pb[k] < 0.5:
                    missing.append(name)
                elif pb[k] >= 0.8 and pa[k] < 0.5:
                    added.append(name)
        label = " ".join(A.words[i].text for i in t.rows[0].idx)
        box_a, box_b = [Loc(t.page, t.bbox)], [Loc(s.page, s.bbox)]
        if missing:
            out.append(Finding(
                "tables", u.cfg["tables"].get("border_severity", "error"),
                f"Table border missing in stage: {', '.join(missing)} (prod p.{t.page + 1} ↔ stage p.{s.page + 1}, "
                f"table “{label}”)", box_a, box_b,
                {"kind": "table-border", "missing": missing, "baseline_borders": pa, "candidate_borders": pb},
                types=["table border"]))
        if added:
            out.append(Finding(
                "tables", "info",
                f"Table border added in stage: {', '.join(added)} (prod p.{t.page + 1} ↔ stage p.{s.page + 1}, "
                f"table “{label}”)", box_a, box_b,
                {"kind": "table-border-added", "added": added, "baseline_borders": pa, "candidate_borders": pb},
                types=["table border added"]))
    return out


_EDGES_Y: dict[tuple, list] = {}  # (path, page) -> horizontal edges drawn on the page (y, x0, x1)


def _edges_y(doc: Doc, page: int) -> list:
    """Everything on the page that reads as a horizontal line: rules, and the top / bottom edge of every
    drawn box - a row set apart by its background band has its line there."""
    key = (doc.path, page)
    if key not in _EDGES_Y:
        out = list(_rules(doc, page)[1])
        pdf = (_DOCS.get(doc.path) or _DOCS.setdefault(doc.path, pymupdf.open(doc.path)))
        try:
            for d in pdf[page].get_drawings():
                for it in d["items"]:
                    if it[0] == "re" and it[1].width > 4:
                        out += [(it[1].y0, it[1].x0, it[1].x1), (it[1].y1, it[1].x0, it[1].x1)]
        except Exception:
            pass
        _EDGES_Y[key] = out
    return _EDGES_Y[key]


def _cell_borders(u: Unit, al, ta: list) -> list[Finding]:
    """Every line prod draws between two cells, one by one: the text right above it and right below it is
    found in stage, and stage must draw a line between the two as well. (The table-wide border check looks at
    shares of rows; a table that keeps its row lines but loses the lines between the parts of one tall cell -
    “Advanced Color Temperature Tuning” | “Color Management” | “Wide Color Gamut” beside one label - passes it.)
    Only a real cell border counts: a rule inside the table whose two ends meet a column line or the table's
    edge (not a link underline, not a box drawn inside a cell). Nothing is said when the two texts are not
    both found in stage, or stage breaks the page between them."""
    A, B = u.a, u.b
    out = []
    for t in ta:
        # (a table continued over pages has a part with one tall row - a label beside several bordered parts:
        # still a table, though not one with two full rows)
        if is_callout(A, t) or _on_image(A, t):
            continue
        x0, y0, x1, y1 = t.bbox
        ver, hor = _rules(A, t.page)
        # column lines: the long vertical rules (the frame of a small picture inside a cell is not one)
        stops = [x0, x1] + [x for x, a, b in ver if x0 - 3 <= x <= x1 + 3 and b > y0 and a < y1
                            and b - a >= max(60, 0.3 * (y1 - y0))]
        # segments on one height that touch are one line
        by_y: dict = {}
        for y, a, b in hor:
            if y0 + 3 < y < y1 - 3 and b > x0 and a < x1:
                by_y.setdefault(round(y), []).append((a, b))
        lines = []
        for y, segs in by_y.items():
            segs.sort()
            lo, hi = segs[0]
            for a, b in segs[1:]:
                if a <= hi + 2:
                    hi = max(hi, b)
                else:
                    lines.append((y, lo, hi))
                    lo, hi = a, b
            lines.append((y, lo, hi))
        words = [i for r in t.rows for i in r.idx]
        missing = []
        for y, a, b in sorted(lines):
            if b - a < 30 or not (any(abs(a - x) <= 4 for x in stops) and any(abs(b - x) <= 4 for x in stops)):
                continue
            mid = lambda i: (A.words[i].bbox[0] + A.words[i].bbox[2]) / 2
            col = [i for i in words if a - 1 <= mid(i) <= b + 1 and i in al.a2b]
            above = [i for i in col if A.words[i].bbox[3] <= y + 1.5]
            below = [i for i in col if A.words[i].bbox[1] >= y - 1.5]
            if not above or not below:
                continue
            i_up = max(above, key=lambda i: (A.words[i].bbox[3], A.words[i].bbox[0]))
            i_dn = min(below, key=lambda i: (A.words[i].bbox[1], A.words[i].bbox[0]))
            if y - A.words[i_up].bbox[3] > 40 or A.words[i_dn].bbox[1] - y > 40:
                continue  # the nearest text found in stage is far from the line: not the two cells it parts
            wu, wd = B.words[al.a2b[i_up]], B.words[al.a2b[i_dn]]
            if wu.page != wd.page or wd.bbox[1] < wu.bbox[3] - 1:
                continue  # a page break between them, or not one below the other in stage
            left, right = min(wu.bbox[0], wd.bbox[0]) - 5, max(wu.bbox[2], wd.bbox[2]) + 5
            if any(wu.bbox[3] - 2 <= yy <= wd.bbox[1] + 2 and s1 - s0 >= 20 and s1 > left and s0 < right
                   for yy, s0, s1 in _edges_y(B, wu.page)):
                continue
            line_of = lambda d, w: " ".join(d.lines[w.line].text.split()[:6])
            missing.append(((y, a, b), wu, wd, line_of(A, A.words[i_up]), line_of(A, A.words[i_dn])))
        if not missing:
            continue
        eg = "; ".join(f"between “{up}” and “{dn}”" for _, _, _, up, dn in missing[:3])
        pages = sorted({wu.page + 1 for _, wu, _, _, _ in missing})
        out.append(Finding(
            "tables", u.cfg["tables"].get("cell_border_severity", "warning"),
            f"Table cell border missing in stage: {len(missing)} line(s) between cells of the prod table are not drawn "
            f"in stage - {eg} (prod p.{t.page + 1} ↔ stage p.{', '.join(map(str, pages))})",
            [Loc(t.page, (a, y - 2, b, y + 2)) for (y, a, b), *_ in missing],
            # where the line belongs in stage: the gap between the two texts, as wide as the prod line
            [Loc(wu.page, (min(wu.bbox[0], wd.bbox[0]) - 2, wu.bbox[3] - 1,
                           min(min(wu.bbox[0], wd.bbox[0]) - 2 + (b - a), B.pages[wu.page].width - 12),
                           max(wd.bbox[1] + 1, wu.bbox[3] + 3))) for (_, a, b), wu, wd, _, _ in missing],
            {"kind": "cell-border", "lines": len(missing)}, types=["cell border"]))
    return out


def _crossing(doc: Doc, li: int, tbox) -> bool:
    """The text line runs across a border of the table it sits in."""
    ln = doc.lines[li]
    x0, y0, x1, y1 = ln.bbox
    cy = (y0 + y1) / 2
    # glyph boxes include the font's full ascent/descent (tall in Arabic, Thai, ...): for row lines
    # use the core of the text instead, so letters that sit inside the cell never "cross" it
    y0, y1 = cy - 0.35 * ln.size, cy + 0.35 * ln.size
    ver, hor = _rules(doc, ln.page)
    inside = lambda x, y: tbox[0] - 2 <= x <= tbox[2] + 2 and tbox[1] - 2 <= y <= tbox[3] + 2
    m = 1.5  # glyph boxes touch a rule by a hair when text sits tight against it
    for x, ya, yb in ver:
        if inside(x, cy) and ya <= cy <= yb and x0 < x - m and x1 > x + m:
            return True
    for y, xa, xb in hor:
        overlap = min(x1, xb) - max(x0, xa)
        if inside((max(x0, xa) + min(x1, xb)) / 2, y) and overlap > 4 and y0 < y - m and y1 > y + m:
            return True
    return False


def _overflow(u: Unit, al: Aligner) -> list[Finding]:
    """Text in a stage table that runs outside its cell: across a column border, past the table
    edge or down over a row border. Reported only where the same text sits inside its cell in prod."""
    A, B = u.a, u.b
    if B.raw_tables is not None:  # web page: browser tables grow with their content
        return []
    out = []
    lines_a = {}
    for i in range(*u.a_range):
        lines_a.setdefault(A.words[i].line, []).append(i)
    tables_a = {(p, t): tb for p in {A.words[i].page for i in range(*u.a_range)} for t, tb, _, _ in _raw(A, p)}
    seen = set()
    for p in sorted({B.words[j].page for j in range(*u.b_range)}):
        for _, tbox, _, _ in _raw(B, p):
            for j in range(*u.b_range):
                w = B.words[j]
                if w.page != p or w.line in seen or not w.norm:
                    continue
                cx, cy = (w.bbox[0] + w.bbox[2]) / 2, (w.bbox[1] + w.bbox[3]) / 2
                if not (tbox[0] <= cx <= tbox[2] and tbox[1] <= cy <= tbox[3]):
                    continue
                seen.add(w.line)
                if not _crossing(B, w.line, tbox):
                    continue
                idx_b = [k for k in range(*u.b_range) if B.words[k].line == w.line]
                idx_a = [al.b2a[k] for k in idx_b if k in al.b2a]
                # the same text in prod: inside its own table cell there?
                prod_over = False
                for i in idx_a:  # the prod table holding that word, and does its line cross a border too
                    pw = A.words[i]
                    px, py = (pw.bbox[0] + pw.bbox[2]) / 2, (pw.bbox[1] + pw.bbox[3]) / 2
                    for (pp, _), tb in tables_a.items():
                        if pp == pw.page and tb[0] <= px <= tb[2] and tb[1] <= py <= tb[3] and _crossing(A, pw.line, tb):
                            prod_over = True
                if prod_over:
                    continue
                text = snippet(B, idx_b, 14)
                out.append(Finding(
                    "tables", u.cfg["tables"].get("overflow_severity", "error"),
                    f"Text outside the table border in stage: “{text}” runs across a cell border (stage p.{p + 1})",
                    locs(A, idx_a) if idx_a else [], [Loc(p, B.lines[w.line].bbox)],
                    {"kind": "overflow", "text": text},
                    baseline_at=None if idx_a else al.loc_in_a(idx_b[0]), types=["text outside table border"]))
    return out


def _one_cell_box(doc: Doc, idx: list[int]) -> tuple | None:
    """(page, box) of one drawn cell holding all the words idx: a stroked rectangle (or four rules) around
    them, with no column rule through them - the prod side of “one cell in prod, columns in stage”."""
    ws = [doc.words[i] for i in idx]
    if not ws or len({w.page for w in ws}) != 1:
        return None
    page = ws[0].page
    x0, y0 = min(w.bbox[0] for w in ws), min(w.bbox[1] for w in ws)
    x1, y1 = max(w.bbox[2] for w in ws), max(w.bbox[3] for w in ws)
    pdf = (_DOCS.get(doc.path) or _DOCS.setdefault(doc.path, pymupdf.open(doc.path)))
    best = None
    rules = []
    for d in pdf[page].get_drawings():
        r = d["rect"]
        if d.get("color") is not None and r.width > 2 and r.height > 2 and \
                r.x0 <= x0 + 1 and r.y0 <= y0 + 1 and r.x1 >= x1 - 1 and r.y1 >= y1 - 1 and r.width < 0.95 * pdf[page].rect.width:
            if best is None or r.get_area() < best.get_area():
                best = pymupdf.Rect(r)
        for it in d["items"]:
            if it[0] == "l" and abs(it[1].x - it[2].x) < 0.5:  # a vertical rule
                rules.append((it[1].x, min(it[1].y, it[2].y), max(it[1].y, it[2].y)))
    if best is None:
        return None
    # a column rule inside the box crossing the words: prod has columns too, it is not one cell
    if any(x0 + 2 < x < x1 - 2 and ya < y1 and yb > y0 for x, ya, yb in rules):
        return None
    return page, tuple(best)
