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
_NAMES: dict[str, dict] = {}  # path -> named destinations


def _pdf(doc: Doc) -> pymupdf.Document:
    return (_DOCS.get(doc.path) or _DOCS.setdefault(doc.path, pymupdf.open(doc.path)))


def links(doc: Doc, page: int) -> list[dict]:
    """The page's links. A jump to a named destination (how InDesign and AEM Guides link inside a
    document) gets the page and position of that destination, like a plain page link; one whose
    destination does not exist is marked `unresolved` (a broken link)."""
    key = (doc.path, page)
    if key not in _LINKS:
        pdf = _pdf(doc)
        out = pdf[page].get_links()
        for ln in out:
            name = ln.get("nameddest")
            if ln["kind"] != pymupdf.LINK_NAMED or not name or ln.get("page", -1) >= 0:
                continue
            if doc.path not in _NAMES:
                try:
                    _NAMES[doc.path] = pdf.resolve_names()
                except Exception:
                    _NAMES[doc.path] = {}
            dest = next((_NAMES[doc.path][n] for n in _name_forms(pdf, ln, name) if n in _NAMES[doc.path]), None)
            if dest and 0 <= dest.get("page", -1) < pdf.page_count:
                ln["page"] = dest["page"]
                to = dest.get("to")  # PDF user space: y counts from the bottom of the page
                ln["to"] = pymupdf.Point(to[0], pdf[dest["page"]].rect.height - to[1]) if to else pymupdf.Point(0, 0)
            else:
                ln["unresolved"] = name
        _LINKS[key] = out
    return _LINKS[key]


def _name_forms(pdf: pymupdf.Document, ln: dict, name: str) -> list[str]:
    """The destination name as given, re-decoded (non-ASCII names such as "錨點" come through as
    UTF-8 bytes read as Latin-1), and as stored in the link's action in the PDF."""
    forms = [name]
    try:
        forms.append(name.encode("latin-1").decode("utf-8"))
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    if ln.get("xref"):
        for key in ("A/D", "Dest"):
            try:
                kind, val = pdf.xref_get_key(ln["xref"], key)
                if kind in ("string", "name"):
                    forms.append(val.lstrip("/"))
            except Exception:
                pass
    return forms


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
                          or (ln["kind"] == pymupdf.LINK_URI and not ln.get("uri"))
                          or ln.get("unresolved"))
                if broken:
                    target = ln.get("uri") or (f"destination “{ln['unresolved']}” does not exist" if ln.get("unresolved")
                                               else f"page {ln.get('page')}")
                    findings.append(Finding(
                        "integrity", "error", f"Broken link on stage p.{p + 1} (target: {target})",
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
