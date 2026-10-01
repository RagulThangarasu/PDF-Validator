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
from dataclasses import dataclass, field

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
    return out


def _loose(t: str) -> str:
    """Presence tests ignore case and punctuation ('Note' -> 'NOTE:' is a content change)."""
    return re.sub(r"[\W_]+", "", t).lower()


def _bag(doc: Doc, idx) -> Counter:
    # split at hyphens/slashes: where a cell wraps decides whether "non-condensing" is one word or two
    return Counter(k for i in idx for part in re.split(r"[-/–—]", doc.words[i].norm) if (k := _loose(part)))


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


def tables(doc: Doc, rng: tuple[int, int]) -> list[TTable]:
    pages = sorted({doc.words[i].page for i in range(*rng)})
    out = []
    for p in pages:
        for t, tbox, rows, grid in _raw(doc, p):
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
            hits.sort(key=lambda h: (h[1] < best - 0.15, near(h[0]), -h[1]))
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


def check(u: Unit) -> list[Finding]:
    tcfg = u.cfg["tables"]
    if not tcfg.get("enabled", True):
        return []
    thr = tcfg.get("row_match_ratio", 0.5)
    al = Aligner(u)
    A, B = u.a, u.b
    ta, tb = tables(A, u.a_range), tables(B, u.b_range)
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

    def add(sev, msg, a_locs, b_locs, kind, critical=False, a_at=None, b_at=None):
        findings.append(Finding("tables", sev, msg, a_locs, b_locs, {"kind": kind}, baseline_at=a_at,
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
            [Loc(r.page, r.box)], [], "missing row", critical=True, b_at=al.loc_in_b(r.idx[0]))
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
        dest = {row_table_b[fwd[k][0][0]] for k in ks if fwd[k] and id(rows_b[fwd[k][0][0]]) not in rep_b}
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
                    [Loc(t.page, t.bbox)], [], "missing table", critical=True, b_at=al.loc_in_b(t.rows[0].idx[0]))
                # its rows are part of this one finding, not each a "row missing" of their own
                for k in [k for k, r in enumerate(rows_a) if r.table == t.key and k in missing_row]:
                    if missing_row[k] in findings:
                        findings.remove(missing_row.pop(k))
        else:
            _missing_header(t, ta, tb, dest, rows_a, missing_row, findings, add, tcfg, thr, A, B, al, rtext)
        if len(dest) >= 2:
            bt = [x for x in tb if x.key in dest]
            if not all(_continuation(p, q, B) for p, q in zip(bt, bt[1:])):
                add(tcfg.get("table_split_severity", "warning"),
                    f"Table split in stage: prod table (p.{t.page + 1}, “{rtext(A, t.rows[0], 6)}”) is {len(dest)} tables in stage",
                    [Loc(t.page, t.bbox)], [Loc(x.page, x.bbox) for x in bt], "table split")
    src_of: dict[tuple, set] = defaultdict(set)
    for k, r in enumerate(rows_a):
        if fwd[k] and id(r) not in rep_a and id(rows_b[fwd[k][0][0]]) not in rep_b:
            src_of[row_table_b[fwd[k][0][0]]].add(r.table)
    for t in tb:
        srcs = src_of.get(t.key, set())
        if len(srcs) >= 2:
            at = [x for x in ta if x.key in srcs]
            if not all(_continuation(p, q, A) for p, q in zip(at, at[1:])):
                add(tcfg.get("table_merged_severity", "warning"),
                    f"Tables merged in stage: {len(srcs)} prod tables are one table in stage (stage p.{t.page + 1}, "
                    f"“{rtext(B, t.rows[0], 6)}”)", [Loc(x.page, x.bbox) for x in at], [Loc(t.page, t.bbox)], "tables merged")
        elif not srcs:
            mirror = [al.b2a[i] for r in t.rows for i in r.idx if i in al.b2a]
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
    findings += _overflow(u, al)
    if tcfg.get("check_borders", True):
        findings += _borders(u, al, ta, tb)
    if tcfg.get("check_row_background", True):
        findings += _row_background(u, tb)
    return findings


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
