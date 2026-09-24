"""Integrity check: things that are broken in stage regardless of prod.

* broken glyphs   – U+FFFD / private-use / NUL characters (missing font or bad encoding)
* text off page   – text drawn outside the page box (overflow / clipping)
* broken links    – internal links to non-existent pages, empty URIs
* missing links   – text that is a link in prod but plain text in stage
All but missing links are critical.
"""
from __future__ import annotations

import pymupdf

from ..model import Doc, Finding, Loc
from . import Aligner, Unit, locs, snippet

_DOCS: dict[str, pymupdf.Document] = {}
_LINKS: dict[tuple, list[dict]] = {}
_OFFPAGE: dict[tuple, list[tuple]] = {}


def _pdf(doc: Doc) -> pymupdf.Document:
    return _DOCS.setdefault(doc.path, pymupdf.open(doc.path))


def links(doc: Doc, page: int) -> list[dict]:
    key = (doc.path, page)
    if key not in _LINKS:
        _LINKS[key] = _pdf(doc)[page].get_links()
    return _LINKS[key]


def offpage_words(doc: Doc, page: int) -> list[tuple]:
    key = (doc.path, page)
    if key not in _OFFPAGE:
        pg = _pdf(doc)[page]
        r = pg.rect
        _OFFPAGE[key] = [w for w in pg.get_text("words", clip=pymupdf.INFINITE_RECT())
                         if w[4].strip() and (w[0] < r.x0 - 1 or w[1] < r.y0 - 1 or w[2] > r.x1 + 1 or w[3] > r.y1 + 1)]
    return _OFFPAGE[key]


def _bad_char(c: str) -> bool:
    return c in ("�", "\x00") or 0xE000 <= ord(c) <= 0xF8FF


def _in_any(box, rects) -> bool:
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return any(r.x0 - 1 <= cx <= r.x1 + 1 and r.y0 - 1 <= cy <= r.y1 + 1 for r in rects)


def check(u: Unit) -> list[Finding]:
    icfg = u.cfg["integrity"]
    B, A = u.b, u.a
    findings = []
    b_pages = sorted({B.words[i].page for i in range(*u.b_range)})

    if icfg.get("glyphs", True):
        bad = [i for i in range(*u.b_range) if any(_bad_char(c) for c in B.words[i].text)]
        if bad:
            findings.append(Finding(
                "integrity", "error", f"Broken glyphs in stage ({len(bad)} words), e.g. “{snippet(B, bad, 6)}”",
                [], locs(B, bad), {"kind": "glyph", "words": len(bad)}, critical=True))

    if icfg.get("offpage", True):
        for p in b_pages:
            ws = offpage_words(B, p)
            if ws:
                findings.append(Finding(
                    "integrity", "error",
                    f"Text outside the page on stage p.{p + 1}: “{' '.join(w[4] for w in ws[:8])}”",
                    [], [Loc(p, (max(w[0], 0), max(w[1], 0), min(w[2], B.pages[p].width), min(w[3], B.pages[p].height)))
                         for w in ws[:20]], {"kind": "offpage", "words": len(ws)}, critical=True))

    if icfg.get("links", True):
        n_pages = _pdf(B).page_count
        for p in b_pages:
            for ln in links(B, p):
                broken = ((ln["kind"] == pymupdf.LINK_GOTO and not 0 <= ln.get("page", -1) < n_pages)
                          or (ln["kind"] == pymupdf.LINK_URI and not ln.get("uri")))
                if broken:
                    findings.append(Finding(
                        "integrity", "error", f"Broken link on stage p.{p + 1} (target: {ln.get('uri') or ln.get('page')})",
                        [], [Loc(p, tuple(ln["from"]))], {"kind": "broken-link"}, critical=True))
        # text that is a link in prod but not in stage (compared on matched words)
        al = Aligner(u)
        missing: list[int] = []
        for i in range(*u.a_range):
            j = al.a2b.get(i)
            if j is None:
                continue
            wa, wb = A.words[i], B.words[j]
            a_links = [l["from"] for l in links(A, wa.page)]
            if a_links and _in_any(wa.bbox, a_links) and not _in_any(wb.bbox, [l["from"] for l in links(B, wb.page)]):
                missing.append(i)
        if missing:
            b_idx = [al.a2b[i] for i in missing]
            findings.append(Finding(
                "integrity", icfg.get("missing_link_severity", "error"),
                f"Link missing in stage: “{snippet(A, missing, 10)}” is a link in prod but plain text in stage "
                f"({len(missing)} words)",
                locs(A, missing), locs(B, b_idx), {"kind": "missing-link", "words": len(missing)}))
    return findings
