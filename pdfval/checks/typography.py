"""Design-spec check: the stage PDF against the manual's typography and page layout
(config/typography.toml, taken from Figma), not against prod.

Every stage word gets the spec role of what it is: its section heading level (h1-h3),
the first row of a table (table header) or another row (table default), small text
right below a table (table note), a link (body hyperlink), bold text (body strong) or
body text (body default). Its font family, weight and size, and the line height of its
paragraph, are compared to that role's style in the format the stage page size matches
(A4 = PDF View, A5 = PDF Print). Findings are grouped per section by role and mismatch.
"""
from __future__ import annotations

from collections import defaultdict
from functools import lru_cache

import pymupdf

from ..model import Doc, Finding, Loc
from . import Unit, locs, snippet
from . import tables as tables_mod
from .style import color_distance


def spec_format(doc: Doc, cfg: dict) -> tuple[str, dict] | None:
    """The spec format for this document: configured, or the one its first page size matches."""
    t = cfg.get("typography") or {}
    fmts = {k: v for k, v in (t.get("formats") or {}).items() if v.get("page")}
    if t.get("format", "auto") != "auto":
        k = t["format"]
        return (k, fmts[k]) if k in fmts else None
    if not doc.pages:
        return None
    w, h = doc.pages[0].width, doc.pages[0].height
    tol = t.get("page_size_tolerance", 0.02)
    for k, f in fmts.items():
        fw, fh = f["page"]
        if abs(w - fw) <= fw * tol and abs(h - fh) <= fh * tol:
            return k, f
    return None


@lru_cache(maxsize=4)
def _link_rects(path: str) -> dict[int, list[tuple]]:
    out: dict[int, list[tuple]] = defaultdict(list)
    with pymupdf.open(path) as d:
        for p in d:
            out[p.number] = [tuple(l["from"]) for l in p.get_links() if l.get("kind") in (pymupdf.LINK_URI, pymupdf.LINK_GOTO,
                                                                                          pymupdf.LINK_NAMED, pymupdf.LINK_GOTOR)]
    return dict(out)


def _center_in(b: tuple, r: tuple) -> bool:
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    return r[0] - 1 <= cx <= r[2] + 1 and r[1] - 1 <= cy <= r[3] + 1


def _roles(u: Unit, t: dict, styles: dict) -> dict[int, str]:
    doc, rng = u.b, u.b_range
    role: dict[int, str] = {}
    note_size = (styles.get("table_note") or {}).get("size")
    for tb in tables_mod.tables(doc, rng):
        for k, r in enumerate(tb.rows):
            for i in r.idx:
                role[i] = "table_header" if k == 0 else "table_default"
        if note_size:  # small text right below the table is its note
            bottom = tb.bbox[3]
            for i in range(*rng):
                w = doc.words[i]
                if (w.page == tb.page and i not in role and 0 <= w.bbox[1] - bottom <= t.get("table_note_gap", 40)
                        and abs(w.style.size - note_size) <= abs(w.style.size - styles["table_default"]["size"])):
                    role[i] = "table_note"
    links = _link_rects(doc.path)
    for i in range(*rng):
        if i in role:
            continue
        w = doc.words[i]
        if w.role.startswith("h") and w.role[1:].isdigit():
            role[i] = f"h{min(int(w.role[1:]), 3)}"
        elif any(_center_in(w.bbox, r) for r in links.get(w.page, ())):
            role[i] = "body_hyperlink"
        elif w.style.weight >= 600:
            role[i] = "body_strong"
        else:
            role[i] = "body_default"
    return role


def check(u: Unit) -> list[Finding]:
    t = u.cfg.get("typography") or {}
    if not t.get("enabled", True) or u.b.raw_tables is not None:  # a web page: its CSS, not the PDF spec
        return []
    fmt = spec_format(u.b, u.cfg)
    if not fmt:
        return []
    fkey, f = fmt
    styles, fonts, weights = f["styles"], t.get("fonts", {}), t.get("weights", {})
    size_tol, lh_tol = t.get("size_tolerance", 0.25), t.get("line_height_tolerance", 1.0)
    color_tol = t.get("color_tolerance", 24)
    doc = u.b
    role = _roles(u, t, styles)
    groups: dict[tuple, list[int]] = defaultdict(list)
    for i, r in role.items():
        spec, w = styles.get(r), doc.words[i]
        if not spec or not w.norm:
            continue
        fam = fonts.get(spec["font"], spec["font"])
        if not w.style.family.lower().replace(" ", "").startswith(fam.lower().replace(" ", "")):
            groups[(r, "font-family", fam, w.style.family)].append(i)
        want_w = weights.get(spec["weight"], 400)
        inline_bold = w.style.weight >= 600 and r in ("body_default", "table_default", "table_note", "body_hyperlink")
        if w.style.weight != want_w and not inline_bold:  # bold is an inline style of any text (spec: Inline Elements)
            groups[(r, "font-weight", f"{spec['weight']} ({want_w})", str(w.style.weight))].append(i)
        if abs(w.style.size - spec["size"]) > size_tol:
            groups[(r, "font-size", f"{spec['size']:g}pt", f"{w.style.size:g}pt")].append(i)
        if spec.get("color") and color_distance(w.style.color, spec["color"].lower()) > color_tol:
            groups[(r, "color", spec["color"].upper(), w.style.color.upper())].append(i)
    # line height: top-to-top distance of consecutive lines in one paragraph (text block) of one role
    seen_line: dict[int, int] = {}
    for i in sorted(role):
        seen_line.setdefault(doc.words[i].line, i)
    lines = sorted(seen_line)
    for a, b in zip(lines, lines[1:]):
        la, lb = doc.lines[a], doc.lines[b]
        ia, ib = seen_line[a], seen_line[b]
        r = role[ia]
        if r != role[ib] or la.page != lb.page or la.block != lb.block or b != a + 1 or r.startswith("table"):
            continue
        spec = styles.get(r)
        if not spec:
            continue
        # a wrapped line of the same paragraph: below the previous one, same column, not a lone list marker,
        # and closer than a paragraph gap
        got = round(lb.bbox[1] - la.bbox[1], 1)
        if (abs(la.bbox[0] - lb.bbox[0]) > 2 * spec["size"] or len(la.text.strip()) < 3 or len(lb.text.strip()) < 3
                or not 0.5 * spec["line_height"] <= got <= 1.6 * spec["line_height"]):
            continue
        if abs(got - spec["line_height"]) > lh_tol:
            groups[(r, "line-height", f"{spec['line_height']:g}pt", f"{round(got * 2) / 2:g}pt")].append(ib)

    findings, max_locs = [], u.cfg["report"]["max_locs"]
    for (r, prop, want, got), idx in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        name = r.replace("_", " ").replace("h1", "Headline 1").replace("h2", "Headline 2").replace("h3", "Headline 3")
        findings.append(Finding(
            "style", t.get("severity", "warning"),
            f"[spec {f['label']} · {name}] {prop}: spec {want}, stage {got} ({len(idx)} words, e.g. “{snippet(doc, idx, 8)}”)",
            [], locs(doc, idx, max_locs),
            {"kind": "spec", "role": r, "format": fkey, "words": len(idx),
             "spec": _describe(f["label"], name, styles[r], fonts),
             "props": [{"property": prop, "baseline": f"spec {want}", "candidate": got}]},
            types=[f"spec {prop}"]))
    return findings


def _describe(label: str, name: str, st: dict, fonts: dict) -> str:
    return (f"{label} · {name}: {fonts.get(st['font'], st['font'])} {st['weight']}, "
            f"{st['size']:g}pt / {st['line_height']:g}pt line height" + (f", colour {st['color'].upper()}" if st.get("color") else "")
            + (", underlined" if st.get("underline") else ""))


def document(B: Doc, cfg: dict) -> list[Finding]:
    """Whole stage document against the spec's page format: page size and text inside the margins."""
    t = cfg.get("typography") or {}
    if not t.get("enabled", True) or B.raw_tables is not None or not B.pages:
        return []
    fmt = spec_format(B, cfg)
    sev = t.get("layout_severity", "warning")
    if not fmt:
        sizes = ", ".join(f"{v['label']} {v['page'][0]:g}×{v['page'][1]:g}pt"
                          for v in (t.get("formats") or {}).values() if v.get("page"))
        return [Finding("layout", sev, f"[spec] Page size {B.pages[0].width:.0f}×{B.pages[0].height:.0f}pt matches no "
                                       f"spec format ({sizes}): typography not validated against the spec",
                        detail={"kind": "spec", "property": "spec page size"}, types=["spec page size"])]
    fkey, f = fmt
    m, tol = f.get("margins") or {}, t.get("margin_tolerance", 2.0)
    out, bad = [], defaultdict(list)
    for i, w in enumerate(B.words):
        if not w.norm:
            continue
        pg = B.pages[w.page]
        x0, y0, x1, y1 = w.bbox
        for side, off in (("left", m.get("left", 0) - x0), ("right", x1 - (pg.width - m.get("right", 0))),
                          ("top", m.get("top", 0) - y0), ("bottom", y1 - (pg.height - m.get("bottom", 0)))):
            if off > tol:
                bad[side].append(i)
    for side, idx in bad.items():
        pages = sorted({B.words[i].page + 1 for i in idx})
        out.append(Finding(
            "layout", sev,
            f"[spec {f['label']}] Text inside the {side} page margin ({m.get(side, 0):g}pt) on {len(pages)} page(s): "
            f"p.{', '.join(map(str, pages[:12]))}{' …' if len(pages) > 12 else ''} (e.g. “{snippet(B, idx, 8)}”)",
            [], [Loc(B.words[i].page, B.words[i].bbox) for i in idx[:cfg['report']['max_locs']]],
            {"kind": "spec", "property": "spec page margin", "side": side, "pages": pages, "format": fkey,
             "spec": f"{f['label']}: page {f['page'][0]:g}×{f['page'][1]:g}pt, margins top {m.get('top', 0):g} · "
                     f"bottom {m.get('bottom', 0):g} · left {m.get('left', 0):g} · right {m.get('right', 0):g}pt"},
            types=["spec page margin"]))
    return out
