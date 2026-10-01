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
            if ln["kind"] != pymupdf.LINK_NAMED or not name:
                continue
            if ln.get("page", -1) >= 0:
                # resolved by pymupdf, but its point is in PDF user space (y from the page bottom), unlike
                # a plain page link: flip it, or the jump lands at the page foot - in the next section
                to = ln.get("to")
                if to is not None and 0 <= ln["page"] < pdf.page_count:
                    ln["to"] = pymupdf.Point(to.x, pdf[ln["page"]].rect.height - to.y)
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
        _LINKS[key] = _by_quads(pdf, page, out)
    return _LINKS[key]


def _by_quads(pdf: pymupdf.Document, page: int, out: list[dict]) -> list[dict]:
    """A link that wraps over two lines is often one rectangle around both whole lines, which also
    covers the words before and after it ("Work with the test pattern ... See" before the linked
    "Displaying test pattern for fine- / tuning the image"). Its QuadPoints hold the exact areas:
    such a link becomes one entry per area (same target, same "xref"), so a word is in a link only
    when it sits in the clickable text."""
    H = pdf[page].rect.height
    res = []
    for ln in out:
        quads = None
        if ln.get("xref"):
            try:
                kind, val = pdf.xref_get_key(ln["xref"], "QuadPoints")
                if kind == "array":
                    v = [float(t) for t in val.strip("[]").split()]
                    quads = [v[k:k + 8] for k in range(0, len(v) - 7, 8)]
            except Exception:
                quads = None
        if not quads or len(quads) < 2:
            res.append(ln)
            continue
        for q in quads:
            xs, ys = q[0::2], q[1::2]
            r = pymupdf.Rect(min(xs), H - max(ys), max(xs), H - min(ys))  # PDF user space: y from the bottom
            if r.is_empty or r.width < 1:
                continue
            res.append({**ln, "from": r})
    return res


def _address(ln: dict) -> str:
    """A web / file link's address, compared loosely: no scheme, no "www.", no trailing slash, any case."""
    a = (ln.get("uri") or ln.get("file") or "").strip().lower()
    for pre in ("https://", "http://", "mailto:", "file://"):
        if a.startswith(pre):
            a = a[len(pre):]
    return a.removeprefix("www.").rstrip("/")


def _target(ln: dict) -> str:
    """Where a link goes, in words; "" when it goes nowhere."""
    if ln.get("unresolved"):
        return ""
    if ln["kind"] in (pymupdf.LINK_URI, pymupdf.LINK_LAUNCH, pymupdf.LINK_GOTOR):
        return ln.get("uri") or ln.get("file") or ""
    if ln["kind"] in (pymupdf.LINK_GOTO, pymupdf.LINK_NAMED):
        return f"p.{ln['page'] + 1}" if ln.get("page", -1) >= 0 else ""
    return ""


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


def _linked_lines(doc: Doc, idx: list[int]) -> bool:
    """Does any link on the words' pages sit on the lines those words are on? The same link can
    cover more or fewer words on the other side ("Shipping contents on page 8" vs "see Shipping
    contents", or a rectangle around two whole lines): the sentence is linked on both sides."""
    for page in {doc.words[k].page for k in idx}:
        rects = [pymupdf.Rect(l["from"]) for l in links(doc, page) if _target(l)]
        if not rects:
            continue
        for k in idx:
            w = doc.words[k]
            if w.page != page:
                continue
            ln = doc.lines[w.line].bbox
            row = pymupdf.Rect(ln[0], w.bbox[1], ln[2], w.bbox[3])  # the word's line, full width
            if any(r.intersects(row) and min(r.y1, row.y1) - max(r.y0, row.y0) > 0.4 * row.height for r in rects):
                return True
    return False


def _destination(doc: Doc, ln: dict) -> str:
    """The text a page link lands on (the heading of the target section), "" if not known."""
    page = ln.get("page", -1)
    if ln.get("kind") not in (pymupdf.LINK_GOTO, pymupdf.LINK_NAMED) or not 0 <= page < len(doc.pages):
        return ""
    y = ln["to"].y if ln.get("to") is not None else 0
    lines = [l for l in doc.lines if l.page == page and l.bbox[3] >= y - 2]
    return (min(lines, key=lambda l: l.bbox[1]).text.strip()[:60]) if lines else ""


def _link_look(doc: Doc, idx: list[int]) -> str:
    """How the words look when they are styled as a link - a colour of their own on the line, and/or
    an underline - e.g. "#7231c6, underlined"; "" for plain text."""
    idx = [k for k in idx if doc.words[k].norm]
    if not idx:
        return ""
    w0 = doc.words[idx[0]]
    others = [w for w in doc.words if w.page == w0.page and w.line in {doc.words[k].line for k in idx}
              and doc.words.index(w) not in idx] if len(doc.words) < 200000 else []
    colour = w0.style.color
    own_colour = bool(others) and all(w.style.color != colour for w in others) or \
        (not others and colour.lower() not in ("#000000", "#231f20", "#1d1d1b"))
    under = False
    try:
        pg = _pdf(doc)[w0.page]
        for d in pg.get_drawings():
            r = d["rect"]
            if r.height < 2.5 and any(abs(r.y0 - doc.words[k].bbox[3]) < 3 and r.x0 < doc.words[k].bbox[2]
                                      and r.x1 > doc.words[k].bbox[0] for k in idx if doc.words[k].page == w0.page):
                under = True
                break
    except Exception:
        pass
    return ", ".join(x for x in (colour if own_colour else "", "underlined" if under else "") if x)


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
        # text that is a link in prod but not in stage (compared on matched words), one finding per prod link.
        # Judged by where the link goes: a prod link with no target is not a link stage can lose, and a
        # stage link to the same address on the same or a neighbouring page means the link is there
        # (on other words: the heading, the picture), not missing.
        al = Aligner(u)
        per_link: dict[tuple, list[int]] = {}
        for i in range(*u.a_range):
            j = al.a2b.get(i)
            if j is None:
                continue
            wa, wb = A.words[i], B.words[j]
            la = next((l for l in links(A, wa.page) if _in_any(wa.bbox, [l["from"]])), None)
            if la is None or not _target(la) or _in_any(wb.bbox, [l["from"] for l in links(B, wb.page)]):
                continue
            per_link.setdefault((wa.page, _target(la) or la.get("xref") or tuple(la["from"])), []).append(i)
        for (pa, _), missing in per_link.items():
            la = next(l for l in links(A, pa) if _in_any(A.words[missing[0]].bbox, [l["from"]]))
            addr = _address(la)
            pb = B.words[al.a2b[missing[0]]].page
            if addr and any(_address(l) == addr for q in (pb - 1, pb, pb + 1) if 0 <= q < len(B.pages) for l in links(B, q)):
                continue
            b_idx = [al.a2b[i] for i in missing]
            if _linked_lines(B, b_idx):
                continue  # stage links the same sentence, on other words of it
            dest = _destination(A, la)
            to = f"{_target(la)}" + (f", “{dest}”" if dest else "")
            look = _link_look(B, b_idx)
            # the link's whole clickable text in prod (all its areas), not only the words matched in stage
            dest_key = lambda l: (l.get("nameddest") or "", _target(l), tuple(round(v) for v in l["to"]) if l.get("to") is not None else ())
            same = [l["from"] for l in links(A, pa) if dest_key(l) == dest_key(la)]  # one link, maybe two annotations
            clickable = [k for k in range(*u.a_range) if A.words[k].page == pa and A.words[k].norm
                         and _in_any(A.words[k].bbox, same)] or missing
            shown = snippet(A, clickable, 12)
            findings.append(Finding(
                "integrity", icfg.get("missing_link_severity", "error"),
                (f"Link not clickable in stage: “{shown}” looks like a link in stage ({look}) but has no "
                 f"hyperlink; in prod it goes to {to}") if look else
                (f"Link missing in stage: “{shown}” is a link in prod (to {to}) but plain text in stage"),
                locs(A, missing), locs(B, b_idx), {"kind": "missing-link", "words": len(missing), "target": _target(la),
                                                   "destination": dest, "styled_as_link": bool(look)}))
        # the reverse: text that is a link in stage but plain text in prod (an extra link), one finding
        # per stage link - unless prod links the same address on the same or a neighbouring page
        per_link = {}
        for j in range(*u.b_range):
            i = al.b2a.get(j)
            if i is None:
                continue
            wa, wb = A.words[i], B.words[j]
            lb = next((l for l in links(B, wb.page) if _in_any(wb.bbox, [l["from"]])), None)
            if lb is None or not _target(lb) or _in_any(wa.bbox, [l["from"] for l in links(A, wa.page)]):
                continue
            per_link.setdefault((wb.page, _target(lb) or lb.get("xref") or tuple(lb["from"])), []).append(j)
        for (pb, _), extra in per_link.items():
            lb = next(l for l in links(B, pb) if _in_any(B.words[extra[0]].bbox, [l["from"]]))
            addr = _address(lb)
            pa = A.words[al.b2a[extra[0]]].page
            if addr and any(_address(l) == addr for q in (pa - 1, pa, pa + 1) if 0 <= q < len(A.pages) for l in links(A, q)):
                continue
            a_idx = [al.b2a[j] for j in extra]
            if _linked_lines(A, a_idx):
                continue  # prod links the same sentence, on other words of it
            findings.append(Finding(
                "integrity", icfg.get("extra_link_severity", "warning"),
                f"Extra link in stage: “{snippet(B, extra, 10)}” is a link in stage (to {_target(lb)}) but plain "
                f"text in prod ({len(extra)} words)",
                locs(A, a_idx), locs(B, extra), {"kind": "extra-link", "words": len(extra), "target": _target(lb)}))
    return findings
