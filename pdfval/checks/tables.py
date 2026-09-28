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
        pdf = _DOCS.setdefault(doc.path, pymupdf.open(doc.path))
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
    """Grid columns that hold text of this row (by word centre)."""
    cols = set()
    for i in idx:
        cx = (doc.words[i].bbox[0] + doc.words[i].bbox[2]) / 2
        for k, (x0, x1) in enumerate(grid):
            if x0 - 1 <= cx <= x1 + 1:
                cols.add(k)
                break
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
                f"Rows merged in stage: {len(ks)} prod rows ({' | '.join('“' + rtext(A, rows_a[k], 6) + '”' for k in ks[:4])}) "
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
            ex = ", ".join(f"“{rtext(A, r, 4)}” {r.cells}→{s.cells}" for r, s in ds[:3])
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
    findings += _overflow(u, al)
    return findings


def _rules(doc: Doc, page: int) -> tuple[list, list]:
    """Border segments drawn on the page: vertical (x, y0, y1) and horizontal (y, x0, x1).
    Stroked lines and rectangle edges, and hairline filled rectangles (rules drawn as fills)."""
    key = (doc.path, page)
    if key not in _RULES:
        pdf = _DOCS.setdefault(doc.path, pymupdf.open(doc.path))
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
