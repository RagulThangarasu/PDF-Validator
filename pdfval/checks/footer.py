"""Running headers and footers, prod vs stage.

The text comparison leaves them out (they repeat on every page, their page numbers shift with every
layout change), so extract keeps them as Doc.furniture and they are compared here, page by page:
each prod page against the stage page most of its text went to. Per band (footer, header):
  - page number present on one side only
  - page number in another place on the page (left / centred / right)
  - page number printed another way ("12" vs "Page 12" / "12 / 60")
  - header / footer text (a chapter name, the product name, the version) missing, added or changed
  - the page number's font, size or colour
Each kind of difference is one finding listing its pages, attached to the section of its first page.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from ..model import Doc, Finding, Loc
from .style import color_distance

_NUM = re.compile(r"^\W*(?:page\s*)?(\d{1,4}|[ivxlc]{1,7})(?:\s*(?:/|of)\s*\d{1,4})?\W*$", re.I)
_NAME = {400: "Regular", 500: "Medium", 700: "Bold"}


def _items(doc: Doc, page: int, band: str) -> tuple[dict | None, list[dict]]:
    """The page's page number {text, box, pos, pattern, style} and its other header/footer texts [{text, box}]."""
    number, texts = None, []
    W = doc.pages[page].width
    for f in doc.furniture:
        if f["page"] != page or f["band"] != band:
            continue
        whole = f["text"]
        if _NUM.match(whole):  # the line is the page number
            cx = (f["bbox"][0] + f["bbox"][2]) / 2
            st = f["words"][0][2]
            number = number or {"text": whole, "box": f["bbox"], "pos": _pos(cx, W),
                                "pattern": re.sub(r"\d+|\b[ivxlc]+\b", "#", whole.lower()), "style": st}
            continue
        # "5  Important safety instructions": a number glued to the chapter name
        words = f["words"]
        num_w = [w for w in words if re.fullmatch(r"\d{1,4}", w[0])]
        if num_w and number is None and (words[0] is num_w[0] or words[-1] is num_w[-1]):
            w = num_w[0] if words[0] is num_w[0] else num_w[-1]
            cx = (w[1][0] + w[1][2]) / 2
            number = {"text": w[0], "box": w[1], "pos": _pos(cx, W), "pattern": "#", "style": w[2]}
            rest = [x for x in words if x is not w]
            if rest:
                texts.append({"text": " ".join(x[0] for x in rest),
                              "box": (min(x[1][0] for x in rest), min(x[1][1] for x in rest),
                                      max(x[1][2] for x in rest), max(x[1][3] for x in rest))})
            continue
        texts.append({"text": whole, "box": f["bbox"]})
    return number, texts


def _pos(cx: float, W: float) -> str:
    if abs(cx - W / 2) <= 0.08 * W:
        return "centred"
    return "left" if cx < W / 2 else "right"


def _key(text: str) -> str:
    """Header/footer text compared without its numbers (a chapter number, a date) and case."""
    return re.sub(r"\s+", " ", re.sub(r"\d+", "#", text.lower())).strip(" #-–—|·")


def _style(st) -> str:
    return f"{st.family} {_NAME.get(st.weight, st.weight)} {st.size:g}pt {st.color.upper()}"


def _page_map(units) -> dict[int, int]:
    """prod page -> the stage page most of its paired words sit on (pages with at least 5 paired words)."""
    votes: dict[int, Counter] = defaultdict(Counter)
    for u in units:
        for i, j in u.pairs:
            votes[u.a.words[i].page][u.b.words[j].page] += 1
    return {pa: c.most_common(1)[0][0] for pa, c in votes.items() if sum(c.values()) >= 5}


def _pages(ps: list[int]) -> str:
    ps = sorted(set(p + 1 for p in ps))
    runs, start, prev = [], None, None
    for p in ps + [None]:
        if start is None:
            start = prev = p
        elif p is not None and p == prev + 1:
            prev = p
        else:
            runs.append(f"{start}" if start == prev else f"{start}-{prev}")  # "-": the screenshot caption font has no "–"
            start = prev = p
    return ", ".join(runs[:12]) + (" …" if len(runs) > 12 else "")


def compare(A: Doc, B: Doc, units: list, cfg: dict) -> list[tuple]:
    """[(unit, Finding)]: every header / footer difference, grouped by kind across pages."""
    fcfg = cfg.get("footer", {})
    if not fcfg.get("enabled", True) or not units or B.raw_tables is not None:
        return []  # a web page has no running header / footer
    pmap = _page_map(units)
    size_tol, col_tol = fcfg.get("size_tolerance", 0.5), fcfg.get("color_tolerance", 60)
    groups: dict[tuple, list[tuple]] = defaultdict(list)  # key -> [(prod page, stage page, prod loc, stage loc)]
    for band in ("footer", "header"):
        for pa in sorted(pmap):
            pb = pmap[pa]
            na, ta = _items(A, pa, band)
            nb, tb = _items(B, pb, band)
            # page numbers
            if na and not nb:
                groups[(band, "number missing", "", "")].append((pa, pb, Loc(pa, na["box"]), None))
            elif nb and not na:
                groups[(band, "number added", "", "")].append((pa, pb, None, Loc(pb, nb["box"])))
            elif na and nb:
                la, lb = Loc(pa, na["box"]), Loc(pb, nb["box"])
                if na["pos"] != nb["pos"]:
                    groups[(band, "number position", na["pos"], nb["pos"])].append((pa, pb, la, lb))
                if na["pattern"] != nb["pattern"]:
                    groups[(band, "number format", na["text"], nb["text"])].append((pa, pb, la, lb))
                sa, sb = na["style"], nb["style"]
                if (abs(sa.size - sb.size) > size_tol or sa.family.lower() != sb.family.lower() or sa.weight != sb.weight
                        or color_distance(sa.color, sb.color) > col_tol):
                    groups[(band, "number style", _style(sa), _style(sb))].append((pa, pb, la, lb))
            # texts beside / instead of the page number
            ka = {_key(t["text"]): t for t in ta if _key(t["text"])}
            kb = {_key(t["text"]): t for t in tb if _key(t["text"])}
            miss = [k for k in ka if k not in kb]
            extra = [k for k in kb if k not in ka]
            while miss and extra:  # one text in place of another on the same page: changed
                a, b = miss.pop(0), extra.pop(0)
                groups[(band, "text changed", ka[a]["text"], kb[b]["text"])].append(
                    (pa, pb, Loc(pa, ka[a]["box"]), Loc(pb, kb[b]["box"])))
            for k in miss:
                groups[(band, "text missing", ka[k]["text"], "")].append((pa, pb, Loc(pa, ka[k]["box"]), None))
            for k in extra:
                groups[(band, "text added", "", kb[k]["text"])].append((pa, pb, None, Loc(pb, kb[k]["box"])))

    # one finding per kind of difference: all header / footer texts missing in stage together (a chapter
    # name on each chapter's pages is one pattern, not one issue per chapter), and the page number's
    # places on the prod pages together (left on even, right on odd pages -> centred)
    merged: dict[tuple, list[tuple]] = defaultdict(list)
    for (band, kind, x, y), hits in groups.items():
        if kind in ("text missing", "text added"):
            key = (band, kind, "", "")
        elif kind == "number position":
            key = (band, kind, "", y)
        else:
            key = (band, kind, _key(x) if kind.startswith("text") else x, _key(y) if kind.startswith("text") else y)
        merged[key].extend((h, x, y) for h in hits)
    max_locs = cfg["report"]["max_locs"]
    out = []
    for (band, kind, _, _), rows in merged.items():
        hits = [h for h, _, _ in rows]
        x, y = rows[0][1], rows[0][2]
        if kind in ("text missing", "text added", "number position"):
            # every text (or every prod position) with its pages: “Introduction” p.8–15; “Operation” p.21–30
            by: dict[str, list[int]] = defaultdict(list)
            for h, xx, yy in rows:
                by[yy if kind == "text added" else xx].append(h[1] if kind == "text added" else h[0])
            order = sorted(by.items(), key=lambda kv: min(kv[1]))
            if kind == "number position":
                x = " / ".join(f"{t} ({len(ps)} pages)" for t, ps in order)
            else:
                x = y = "; ".join(f"“{t}” p.{_pages(ps)}" for t, ps in order)
        pa_s, pb_s = [h[0] for h in hits], [h[1] for h in hits]
        where = f"prod p.{_pages(pa_s)} ↔ stage p.{_pages(pb_s)}"
        Band = band.capitalize()
        msg, check, sev, types = {
            "number missing": (f"{Band} page number missing in stage ({len(hits)} pages: {where})", "content", "error", [band]),
            "number added": (f"{Band} page number only in stage, prod has none ({len(hits)} pages: {where})", "content", "warning", [band]),
            "number position": (f"{Band} page number position: {x} in prod → {y} in stage ({len(hits)} pages: {where})",
                                "layout", "warning", [band]),
            "number format": (f"{Band} page number printed differently: “{x}” in prod → “{y}” in stage ({len(hits)} pages: {where})",
                              "content", "warning", [band]),
            "number style": (f"{Band} page number style: {x} in prod → {y} in stage ({len(hits)} pages: {where})", "layout", "warning", [band]),
            "text missing": (f"{Band} text missing in stage ({len(hits)} pages): {x}", "content", "error", [band]),
            "text added": (f"{Band} text only in stage ({len(hits)} pages): {y}", "content", "warning", [band]),
            "text changed": (f"{Band} text differs: “{x}” in prod → “{y}” in stage ({len(hits)} pages: {where})", "content", "warning", [band]),
        }[kind]
        f = Finding(check, fcfg.get("severity", {}).get(kind, sev), msg,
                    [h[2] for h in hits if h[2]][:max_locs], [h[3] for h in hits if h[3]][:max_locs],
                    {"kind": f"{band} {kind}", "band": band, "pages": len(hits), "baseline": x, "candidate": y,
                     "baseline_pages": sorted({p + 1 for p in pa_s}), "candidate_pages": sorted({p + 1 for p in pb_s})},
                    candidate_at=None if any(h[3] for h in hits) else Loc(hits[0][1], _slot(B, hits[0][1], band)),
                    baseline_at=None if any(h[2] for h in hits) else Loc(hits[0][0], _slot(A, hits[0][0], band)),
                    types=types, links=[(h[2], h[3]) for h in hits if h[2] and h[3]][:max_locs])
        out.append((_unit_of_page(units, min(pa_s)), f))
    return out


def _slot(doc: Doc, page: int, band: str) -> tuple:
    """Where the missing header / footer would be: the band across the page."""
    W, H = doc.pages[page].width, doc.pages[page].height
    return (0.1 * W, 0.94 * H, 0.9 * W, 0.96 * H) if band == "footer" else (0.1 * W, 0.04 * H, 0.9 * W, 0.06 * H)


def _unit_of_page(units: list, page: int):
    """The section a prod page belongs to (the one holding most of its words)."""
    best = max(units, key=lambda u: sum(1 for k in range(*u.a_range) if u.a.words[k].page == page))
    return best
