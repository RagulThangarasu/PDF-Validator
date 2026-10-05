"""Table of contents: detection, parsing and prod <-> stage comparison.

The printed TOC is not body content: its page numbers shift with every layout
change and its entries repeat the headings. So it is taken out of the content
check and compared on its own terms:

* detection  – pages near the front with ≥ `min_entries` lines ending in dot
               leaders + a page number ("Mounting ........ 20")
* entries    – title, printed page number, level. Wrapped titles are joined.
               The level comes from the indentation: the distinct left edges of
               entry lines (within 4 pt) are ranked, leftmost = level 1.
               Documents without a printed TOC fall back to their PDF bookmarks.
* comparison – order-preserving fuzzy title alignment, then per row:
               match · level differs · title differs · order differs · missing in stage · extra in stage
               ("order differs": the entry exists on both sides but at a different
               position in the sequence, e.g. two entries swapped)
               plus a page check per side: does the printed number point at the
               page where that heading really is (using the document's own page
               numbering offset)?
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher

from . import normalize
from .model import Anchor, Doc, Finding, Loc

_ENTRY = re.compile(r"^(?P<title>.*?)[\s.·]*\.{3,}[\s.]*(?P<page>\d+|[ivxlcdmIVXLCDM]{1,6})\s*$")


@dataclass
class TocEntry:
    title: str
    norm: str
    level: int
    page: int | None  # printed page number
    toc_page: int  # 0-based PDF page the entry is printed on
    bbox: tuple
    x0: float
    actual_page: int | None = None  # 1-based PDF page of the heading (from bookmarks/text)
    page_ok: bool | None = None


@dataclass
class Toc:
    source: str  # "printed" | "bookmarks" | "none"
    pages: list[int] = field(default_factory=list)
    entries: list[TocEntry] = field(default_factory=list)
    lines: list[int] = field(default_factory=list)  # Doc.lines indices that belong to the TOC pages
    heading: str = ""
    page_sizes: dict = field(default_factory=dict)  # 0-based page -> (width, height) pt


_TOC_HEAD = re.compile(r"^\s*(table\s+of\s+contents?|contents?)\s*$", re.I)


def detect(doc: Doc, cfg: dict) -> Toc:
    tcfg = cfg.get("toc", {})
    min_entries = tcfg.get("min_entries", 4)
    front = max(3, int(len(doc.pages) * tcfg.get("max_front_fraction", 0.25)))
    per_page = Counter(ln.page for ln in doc.lines if ln.page < front and _ENTRY.match(normalize.clean(ln.text)))
    pages = sorted(p for p, n in per_page.items() if n >= min_entries)
    if not pages:
        # a TOC further in (after the cover, notices, a quick start): pages of "Title ..... 12" lines anywhere,
        # the first of them headed "Contents" / "Table of Contents" (a dotted Q&A index or list of figures is not)
        every = Counter(ln.page for ln in doc.lines if _ENTRY.match(normalize.clean(ln.text)))
        rich = sorted(p for p, n in every.items() if n >= min_entries)
        heads = {ln.page for ln in doc.lines if _TOC_HEAD.match(normalize.clean(ln.text))}
        start = next((p for p in rich if p in heads), None)
        if start is not None:
            pages = [start]
            while pages[-1] + 1 in rich:
                pages.append(pages[-1] + 1)
    if pages:
        pages = list(range(pages[0], pages[-1] + 1))  # contiguous run
        return _printed(doc, pages)
    if doc.outline:
        return Toc("bookmarks", entries=[TocEntry(t.strip(), normalize.title(t), lvl, p, -1, (0, 0, 0, 0), 0.0, p)
                                         for lvl, t, p in doc.outline])
    return Toc("none")


def _printed(doc: Doc, pages: list[int]) -> Toc:
    toc = Toc("printed", pages=pages, page_sizes={p: (doc.pages[p].width, doc.pages[p].height) for p in pages})
    lines = [i for i, ln in enumerate(doc.lines) if ln.page in pages]
    toc.lines = lines
    raw: list[tuple[str, int | None, int, tuple, float]] = []
    pending = None  # a wrapped title line waiting for its leader line
    for i in lines:
        ln = doc.lines[i]
        text = normalize.clean(ln.text)
        m = _ENTRY.match(text)
        if m:
            title = m["title"].strip(" .")
            box, x0 = ln.bbox, ln.bbox[0]
            if pending and pending[0].page == ln.page and ln.bbox[1] - pending[0].bbox[3] < ln.size * 0.8:
                title = f"{normalize.clean(pending[0].text)} {title}".strip()
                box = (min(pending[0].bbox[0], box[0]), pending[0].bbox[1], max(pending[0].bbox[2], box[2]), box[3])
                x0 = pending[0].bbox[0]
            # a roman number (front matter: i, ii, iv) is kept as the title's end marker only - front-matter
            # pages are numbered separately, so it is not checked against the page it points to
            raw.append((title, int(m["page"]) if m["page"].isdigit() else None, ln.page, box, x0))
            pending = None
        elif text and not toc.heading and not raw:
            toc.heading = text  # "Table of contents"
        elif text:
            pending = (ln,)
    # levels from indentation: cluster left edges within 4 pt, leftmost = level 1
    edges: list[float] = []
    for x in sorted({round(r[4], 1) for r in raw}):
        if not edges or x - edges[-1] > 4:
            edges.append(x)
    # a level is an indent many entries share. A few entries set a little off it (a title starting with another
    # glyph, a wrapped title's first line: 9 pt left of the 31 entries at 85 pt) are not a level of their own -
    # they would shift every level after them ("chapter = level 2, sub-entry = level 4"): a thinly used edge
    # closer to a well used one than half the indent step belongs to it
    if len(edges) > 2:
        count = lambda e: sum(1 for r in raw if abs(round(r[4], 1) - e) <= 4)
        major = [e for e in edges if count(e) >= max(2, 0.1 * len(raw))]
        if len(major) >= 2:
            step = min(b - a for a, b in zip(major, major[1:]))
            merged = {e: min(major, key=lambda m: abs(m - e)) for e in edges}
            snap = {e: (m if abs(m - e) < 0.5 * step else e) for e, m in merged.items()}
            edge_of = lambda x: snap[max((e for e in edges if x >= e - 4), default=edges[0])]
            raw = [(t, pg, tp, b, edge_of(x0)) for t, pg, tp, b, x0 in raw]
            edges = sorted(set(snap.values()))
    level = lambda x: 1 + max(k for k, e in enumerate(edges) if x >= e - 4) if edges else 1
    toc.entries = [TocEntry(t, normalize.title(t), level(x0), pg, tp, tuple(b), x0) for t, pg, tp, b, x0 in raw]
    return toc


def resolve_pages(toc: Toc, anchors: list[Anchor]) -> None:
    """Where does each entry's heading really sit, and does its printed number point there?
    Printed numbers and PDF page indices differ by a document-wide offset (cover,
    unnumbered front matter), estimated as the most common difference."""
    by_title: dict[str, list[int]] = {}
    for a in anchors:
        by_title.setdefault(a.norm, []).append(a.page + 1)
    for e in toc.entries:
        cands = by_title.get(e.norm) or []
        if e.page is not None and cands:
            e.actual_page = min(cands, key=lambda p: abs(p - e.page))
    diffs = Counter(e.actual_page - e.page for e in toc.entries if e.actual_page and e.page is not None)
    if not diffs:
        return
    offset = diffs.most_common(1)[0][0]
    for e in toc.entries:
        if e.actual_page and e.page is not None:
            e.page_ok = abs(e.actual_page - (e.page + offset)) <= 1  # a heading at the page top may sit on the prior index


def _align(a: list[TocEntry], b: list[TocEntry], thr: float) -> list[tuple[int | None, int | None, float]]:
    """Order-preserving fuzzy alignment; returns rows (i, j, similarity) incl. unmatched."""
    n, m = len(a), len(b)
    sim = lambda i, j: 1.0 if a[i].norm == b[j].norm else (lambda s: s if s >= thr else 0.0)(
        SequenceMatcher(None, a[i].norm, b[j].norm).ratio())
    S = [[sim(i, j) for j in range(m)] for i in range(n)]
    # exact titles outweigh fuzzy ones, so two swapped entries with similar titles
    # ("Updating an app" / "Updating all apps") are not paired with each other in order
    W = lambda x: 1.0 if x == 1.0 else 0.45 * x
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            best = max(dp[i + 1][j], dp[i][j + 1])
            if S[i][j]:
                best = max(best, dp[i + 1][j + 1] + W(S[i][j]))
            dp[i][j] = best
    rows, i, j = [], 0, 0
    while i < n or j < m:
        if i < n and j < m and S[i][j] and dp[i][j] == dp[i + 1][j + 1] + W(S[i][j]):
            rows.append((i, j, S[i][j])); i += 1; j += 1
        elif j >= m or (i < n and dp[i + 1][j] >= dp[i][j + 1]):
            rows.append((i, None, 0.0)); i += 1
        else:
            rows.append((None, j, 0.0)); j += 1
    return rows


def _sequence(rows: list[tuple], a: list[TocEntry], b: list[TocEntry], thr: float) -> list[tuple]:
    """Entries the in-order alignment left unpaired on both sides but that carry the same
    title are the same entry at a different position: pair them as 'order differs'
    (kept at the prod position; the stage-only row is dropped)."""
    free_b = {j: b[j] for i, j, _ in rows if i is None}
    out, used = [], set()
    for i, j, s in rows:
        if i is not None and j is None:
            best = max(((jj, 1.0 if e.norm == a[i].norm else SequenceMatcher(None, a[i].norm, e.norm).ratio())
                        for jj, e in free_b.items() if jj not in used), key=lambda h: h[1], default=None)
            if best and best[1] >= max(thr, 0.9):
                used.add(best[0])
                out.append((i, best[0], best[1], True))
                continue
        out.append((i, j, s, False))
    return [r for r in out if not (r[0] is None and r[1] in used)]


def compare(ta: Toc, tb: Toc, cfg: dict) -> tuple[dict, list[Finding]]:
    tcfg = cfg.get("toc", {})
    sev = tcfg.get("severity", {})
    thr = tcfg.get("title_match_threshold", 0.8)
    rows_out, findings = [], []
    for i, j, s, moved in _sequence(_align(ta.entries, tb.entries, thr), ta.entries, tb.entries, thr):
        ea = ta.entries[i] if i is not None else None
        eb = tb.entries[j] if j is not None else None
        if ea and eb and moved:
            status = "order differs"
        elif ea and eb:
            status = "match" if s == 1.0 and ea.level == eb.level else \
                     "level differs" if s == 1.0 else "title differs"
        else:
            status = "missing in stage" if ea else "extra in stage"
        flags = []
        if ea and ea.page_ok is False:
            flags.append("prod page wrong")
        if eb and eb.page_ok is False:
            flags.append("stage page wrong")
        row = {"status": status, "similarity": round(s, 3), "flags": flags,
               "pos": {"baseline": i + 1 if i is not None else None, "candidate": j + 1 if j is not None else None},
               "baseline": _entry_json(ea), "candidate": _entry_json(eb)}
        if ea and eb and ea.level != eb.level and status in ("title differs", "order differs"):
            row["flags"].append("level differs")
        rows_out.append(row)
        # findings for everything that is not a clean match
        msg, types = None, []
        if status == "order differs":
            msg = (f"TOC entry out of order: “{ea.title}” is entry #{i + 1} in prod but #{j + 1} in stage "
                   f"(the sequence around it differs)")
            types = ["order differs"] + (["level differs"] if ea.level != eb.level else [])
        elif status == "level differs":
            msg, types = f"TOC level differs: “{ea.title}” is level {ea.level} in prod, level {eb.level} in stage", ["level differs"]
        elif status == "title differs":
            msg = f"TOC title differs: “{ea.title}” → “{eb.title}”" + (
                f" (and level {ea.level} → {eb.level})" if ea.level != eb.level else "")
            types = ["title differs"] + (["level differs"] if ea.level != eb.level else [])
        elif status == "missing in stage":
            msg, types = f"TOC entry missing in stage: “{ea.title}” (level {ea.level})", ["missing entry"]
        elif status == "extra in stage":
            msg, types = f"Extra TOC entry in stage: “{eb.title}” (level {eb.level})", ["extra entry"]
        if msg:
            findings.append(Finding("toc", sev.get(types[0], "warning"), msg,
                                    [Loc(ea.toc_page, ea.bbox)] if ea and ea.toc_page >= 0 else [],
                                    [Loc(eb.toc_page, eb.bbox)] if eb and eb.toc_page >= 0 else [],
                                    {"kind": types[0], "row": len(rows_out) - 1}, types=types))
        for side, e in (("prod", ea), ("stage", eb)):
            if e and e.page_ok is False:
                findings.append(Finding("toc", sev.get("wrong page", "warning"),
                                        f"TOC page number wrong in {side}: “{e.title}” says p.{e.page}, heading is on PDF page {e.actual_page}",
                                        [Loc(e.toc_page, e.bbox)] if side == "prod" and e.toc_page >= 0 else [],
                                        [Loc(e.toc_page, e.bbox)] if side == "stage" and e.toc_page >= 0 else [],
                                        {"kind": "wrong page", "row": len(rows_out) - 1}, types=["wrong page"]))
    st = Counter(r["status"] for r in rows_out)
    wrong_pages = sum(1 for r in rows_out for f in r["flags"] if f.endswith("page wrong"))
    verdict = ("fail" if st["missing in stage"] or st["order differs"] or wrong_pages
               else "warn" if st["level differs"] or st["title differs"] or st["extra in stage"] else "pass")
    summary = {
        "status": verdict,
        "sequence_ok": not st["order differs"],
        "order differs": st["order differs"],
        "baseline_entries": len(ta.entries), "candidate_entries": len(tb.entries),
        "match": st["match"], "level differs": st["level differs"], "title differs": st["title differs"],
        "missing in stage": st["missing in stage"], "extra in stage": st["extra in stage"],
        "wrong page": wrong_pages,
        "levels_match_pct": round(100.0 * sum(1 for r in rows_out if r["baseline"] and r["candidate"]
                                              and r["baseline"]["level"] == r["candidate"]["level"])
                                  / max(1, sum(1 for r in rows_out if r["baseline"] and r["candidate"])), 1),
    }
    info = {"source": {"baseline": ta.source, "candidate": tb.source},
            "pages": {"baseline": [p + 1 for p in ta.pages], "candidate": [p + 1 for p in tb.pages]},
            "heading": {"baseline": ta.heading, "candidate": tb.heading},
            "levels": {"baseline": max((e.level for e in ta.entries), default=0),
                       "candidate": max((e.level for e in tb.entries), default=0)},
            "summary": summary, "rows": rows_out}
    info["page_size"] = {"baseline": {p + 1: list(sz) for p, sz in ta.page_sizes.items()},
                         "candidate": {p + 1: list(sz) for p, sz in tb.page_sizes.items()}}
    return info, findings


def _entry_json(e: TocEntry | None) -> dict | None:
    if not e:
        return None
    return {"title": e.title, "level": e.level, "page": e.page, "toc_page": e.toc_page + 1 if e.toc_page >= 0 else None,
            "actual_page": e.actual_page, "page_ok": e.page_ok, "bbox": [round(v, 1) for v in e.bbox]}
