"""Design-spec components of the stage PDF (Figma "Online User Manual": the Online UM stylesheet and the
PDF (View) Output & Pagination Specification), beyond the type scale typography.py checks word by word:

* theme        - the cover page's colour names the theme (BenQ, BenQ Education, ZOWIE, INFTY); the theme sets
                 the accent colour of Headline 1 and links, the cover's text colour and the cover's logos
* cover        - its colour matches a theme; title / optional text / subtitle / version in their styles; the
                 version is there; the theme's logos are there
* page numbers - on every page after the cover, counting up from 1, centred, in their style
* headings     - H1 -> H2 -> H3 without skipping a level
* callouts     - Important / Note / Tip / Warning: the fixed title, the type's background colour, an icon (the
                 same icon as the other callouts of the type), no picture and no second callout inside
* tables       - the header bar colour, the border colour
* lists        - ordered: 1, 2, 3 -> a, b, c -> I, II, III by level; unordered: a black circle
* alignment    - body text left-aligned (right-to-left scripts excepted)

Everything is measured on stage only; findings carry detail.kind = "spec" like the type-scale findings.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

import pymupdf

from ..extract import parse_font
from ..model import Doc, Finding, Loc
from . import Unit, locs, snippet
from . import layout as layout_mod
from . import tables as tables_mod
from .style import color_distance


def _pdf(doc: Doc) -> pymupdf.Document:
    return tables_mod._DOCS.get(doc.path) or tables_mod._DOCS.setdefault(doc.path, pymupdf.open(doc.path))


def _hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(round(v * 255) if isinstance(v, float) else int(v) for v in rgb[:3])


def _near(a: str | None, b: str | None, tol: float) -> bool:
    return bool(a and b) and color_distance(a.lower(), b.lower()) <= tol


def _font_ok(family: str, want: str) -> bool:
    return family.lower().replace(" ", "").startswith(want.lower().replace(" ", ""))


def spec_message(head: str, figma: str | None, stage: str | None) -> str:
    """What is wrong, then what Figma sets and what stage has, one line each:
         Table default — font size: Figma 12pt, stage 11pt (27 words, e.g. “Off: Always …”)
         Figma: Roboto Regular, 12pt / 16pt line height, colour #000000
         Stage: Roboto Regular, 11pt, colour #000000"""
    return head + (f"\nFigma: {figma}" if figma else "") + (f"\nStage: {stage}" if stage else "")


def _spec(label: str, cat: str, sev: str, msg: str, where: list[Loc], spec: str, typ: str,
          figma: str | None = None, stage: str | None = None, **detail) -> Finding:
    return Finding(cat, sev, spec_message(msg, figma, stage), [], where,
                   {"kind": "spec", "spec": f"Figma {label} · {spec}", "figma": figma, "stage": stage, **detail}, types=[typ])


def _style_of(span: dict, weights: dict) -> str:
    """How a span is set: "Poppins Medium, 36pt, colour #FFFFFF"."""
    family, weight, _ = parse_font(span["font"], span["flags"])
    name = {v: k for k, v in weights.items()}.get(weight, str(weight))
    return f"{family} {name}, {span['size']:g}pt, colour {('#%06x' % span['color']).upper()}"


# ------------------------------------------------------------------------------------------------ theme
_THEME: dict[tuple, dict] = {}
_VERSION = re.compile(r"^v\s?\d+(\.\d+)*$", re.I)


def theme(doc: Doc, cfg: dict) -> dict:
    """The stage PDF's theme, from its cover page (the first page, when it carries the document title in
    large type or is mostly one colour): the theme whose cover colour is closest to the page's
    dominant colour. No cover: the default theme. Returns the theme's settings plus key, cover (bool),
    color (the cover colour measured) and matched (the colour is within theme_tolerance)."""
    t = cfg.get("typography") or {}
    themes = t.get("themes") or {}
    key = (doc.path, repr(sorted(themes.items())), t.get("default_theme"))
    if key in _THEME:
        return _THEME[key]
    out = {"key": None, "label": "", "accent": None, "cover": False, "color": None, "matched": False}
    if themes:
        dflt = t.get("default_theme") if t.get("default_theme") in themes else next(iter(themes))
        out.update(themes[dflt], key=dflt)
        try:
            pg = _pdf(doc)[0]
            pix = pg.get_pixmap(matrix=pymupdf.Matrix(0.12, 0.12), colorspace=pymupdf.csRGB, alpha=False)
            px = pix.samples
            counts = Counter(bytes(px[k:k + 3]) for k in range(0, len(px), 3))
            top, n = counts.most_common(1)[0]
            share = n / max(1, pix.width * pix.height)
            big = max((s["size"] for b in pg.get_text("dict")["blocks"] for l in b.get("lines", [])
                       for s in l["spans"] if s["text"].strip()), default=0)
            color = _hex(tuple(top))
            white = sum(top) > 740
            out["color"] = color
            out["cover"] = big >= t.get("cover_min_text", 28) or (share >= 0.6 and not white)
            if out["cover"]:
                k = min(themes, key=lambda k: color_distance(color, themes[k]["cover_background"].lower()))
                out.update(themes[k], key=k, cover=True, color=color,
                           matched=color_distance(color, themes[k]["cover_background"].lower()) <= t.get("theme_tolerance", 40))
        except Exception:
            pass
    _THEME[key] = out
    return out


def color_of(value: str | None, th: dict, role: str | None = None) -> str | None:
    """A spec colour: "accent" / "cover_text" resolve to the theme's."""
    if value == "accent":
        return th.get("accent") or None
    if value == "cover_text":
        return (th.get("cover_colors") or {}).get(role) or th.get("cover_text") or None
    return value


# ------------------------------------------------------------------------------------------------ cover
_PROP = {"font-size": "font size", "font-weight": "font weight", "font-family": "font", "color": "colour",
         "line-height": "line height", "text-decoration": "underline"}

_REGION = {"top-left": (0, 0, 0.5, 0.2), "top-right": (0.5, 0, 1, 0.2), "bottom-left": (0, 0.85, 0.5, 1),
           "bottom-right": (0.5, 0.85, 1, 1), "bottom-center": (0.25, 0.85, 0.75, 1)}


def cover(doc: Doc, cfg: dict, fmt: dict, th: dict) -> list[Finding]:
    t = cfg.get("typography") or {}
    label, sev, lsev = fmt["label"], t.get("severity", "warning"), t.get("layout_severity", "warning")
    names = ", ".join(f"{v.get('label', k)} {v['cover_background'].upper()}" for k, v in (t.get("themes") or {}).items())
    if not th.get("cover"):
        return [_spec(label, "layout", "info", "No cover page", [Loc(0, (0, 0, 60, 20))],
                      f"PDF (View) - Cover: themes {names}", "spec cover",
                      figma=f"a cover page in a theme colour ({names})",
                      stage=f"starts with a topic page (checked against the {th.get('label')} theme, "
                            f"accent {str(th.get('accent')).upper()})")]
    pg = _pdf(doc)[0]
    W, H = pg.rect.width, pg.rect.height
    page_box = (0, 0, W, H)
    out = []
    if not th.get("matched"):
        out.append(_spec(label, "style", sev, "Cover colour is not a theme colour", [Loc(0, page_box)],
                         f"PDF (View) - Cover: themes {names}", "spec theme",
                         figma=f"a theme colour: {names}",
                         stage=f"{th['color'].upper()} (closest: {th['label']} {th['cover_background'].upper()})",
                         props=[{"property": "cover background", "baseline": f"Figma {th['cover_background'].upper()}",
                                 "candidate": th["color"].upper()}]))
    lines = []
    for b in pg.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            spans = [s for s in ln["spans"] if s["text"].strip()]
            if spans:
                lines.append({"bbox": tuple(ln["bbox"]), "text": " ".join(s["text"].strip() for s in spans),
                              "spans": spans, "size": max(s["size"] for s in spans)})
    specs = fmt.get("cover") or {}
    fonts, weights = t.get("fonts", {}), t.get("weights", {})
    if specs and lines:
        tsize = max(l["size"] for l in lines)
        title = [l for l in lines if l["size"] >= tsize - 1]
        ty0, ty1 = min(l["bbox"][1] for l in title), max(l["bbox"][3] for l in title)
        version = [l for l in lines if l not in title and _VERSION.match(l["text"]) and l["bbox"][1] > 0.8 * H]
        rest = [l for l in lines if l not in title and l not in version]
        # the optional text right above the title, the subtitle right below it (within one title line)
        roles = {"title": title, "version": version,
                 "optional": [l for l in rest if l["bbox"][3] <= ty0 + 1 and ty0 - l["bbox"][3] < 1.5 * tsize],
                 "subtitle": [l for l in rest if l["bbox"][1] >= ty1 - 1 and l["bbox"][1] - ty1 < 1.5 * tsize]}
        role_name = {"title": "document title", "optional": "optional text", "subtitle": "subtitle", "version": "version"}
        for role, spec in specs.items():
            ls = roles.get(role) or []
            want_c = color_of(spec.get("color"), th, role)
            fam = fonts.get(spec["font"], spec["font"])
            want = f"{fam} {spec['weight']} {spec['size']:g}pt" + (f", colour {want_c.upper()}" if want_c else "")
            if not ls:
                if role == "version":
                    out.append(_spec(label, "layout", lsev, "Cover: version missing",
                                     [Loc(0, (0.7 * W, 0.9 * H, W, H))], f"PDF (View) - Cover: version {want}",
                                     "spec cover", figma=f"the version at the bottom right, e.g. “V1.0” ({want})",
                                     stage="no version on the cover"))
                continue
            bad = defaultdict(list)
            for l in ls:
                for s in l["spans"]:
                    family, weight, _ = parse_font(s["font"], s["flags"])
                    got_c = "#%06x" % s["color"]
                    if not _font_ok(family, fam):
                        bad[("font-family", fam, family)].append(l)
                    if weight != weights.get(spec["weight"], 400):
                        bad[("font-weight", f"{spec['weight']} ({weights.get(spec['weight'], 400)})", str(weight))].append(l)
                    if abs(s["size"] - spec["size"]) > t.get("size_tolerance", 0.25):
                        bad[("font-size", f"{spec['size']:g}pt", f"{s['size']:g}pt")].append(l)
                    if want_c and color_distance(got_c, want_c.lower()) > t.get("color_tolerance", 24):
                        bad[("color", want_c.upper(), got_c.upper())].append(l)
            for (prop, w_, g_), hit in bad.items():
                hit = list({id(l): l for l in hit}.values())
                out.append(_spec(label, "style", sev, f"Cover {role_name[role]} — {_PROP.get(prop, prop)}: Figma {w_}, "
                                 f"stage {g_} (“{hit[0]['text'][:50]}”)", [Loc(0, l["bbox"]) for l in hit],
                                 f"PDF (View) - Cover ({th['label']}): {role_name[role]} {want}", f"spec {prop}",
                                 figma=f"{want} ({th['label']} cover)", stage=_style_of(hit[0]["spans"][0], weights),
                                 role=f"cover {role}", props=[{"property": prop, "baseline": f"Figma {w_}", "candidate": g_}]))
    # the theme's logos: a picture (embedded or drawn) in each place
    pics = [tuple(i["bbox"]) for i in pg.get_image_info()]
    try:
        pics += [tuple(r) for r in pg.cluster_drawings() if r.width * r.height < 0.5 * W * H]
    except Exception:
        pass
    for region in th.get("logos") or []:
        x0, y0, x1, y1 = _REGION.get(region, (0, 0, 0, 0))
        r = (x0 * W, y0 * H, x1 * W, y1 * H)
        if not any(r[0] <= (p[0] + p[2]) / 2 <= r[2] and r[1] <= (p[1] + p[3]) / 2 <= r[3] for p in pics):
            out.append(_spec(label, "layout", lsev, f"Cover: {th['label']} logo missing",
                             [Loc(0, r)], f"PDF (View) - Cover ({th['label']}): logos {', '.join(th.get('logos') or [])}",
                             "spec cover logo", figma=f"the {th['label']} logo at the {region.replace('-', ' ')}",
                             stage=f"no logo at the {region.replace('-', ' ')}"))
    return out


# ------------------------------------------------------------------------------------------------ page numbers
def page_numbers(doc: Doc, cfg: dict, fmt: dict, th: dict) -> list[Finding]:
    t = cfg.get("typography") or {}
    label, sev, lsev = fmt["label"], t.get("severity", "warning"), t.get("layout_severity", "warning")
    spec = fmt.get("page_number") or {}
    fonts, weights = t.get("fonts", {}), t.get("weights", {})
    pdf = _pdf(doc)
    start = 1 if th.get("cover") else 0
    found: dict[int, dict] = {}
    for p in range(start, len(pdf)):
        pg = pdf[p]
        H = pg.rect.height
        cands = []
        for b in pg.get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                spans = [s for s in ln["spans"] if s["text"].strip()]
                text = "".join(s["text"] for s in spans).strip()
                if spans and re.fullmatch(r"\d{1,4}", text) and ln["bbox"][1] > 0.9 * H:
                    cands.append({"n": int(text), "bbox": tuple(ln["bbox"]), "span": spans[0], "w": pg.rect.width})
        if cands:
            found[p] = max(cands, key=lambda c: c["bbox"][3])
    out = []
    missing = [p for p in range(start, len(pdf)) if p not in found]
    want_spec = ""
    if spec:
        col = color_of(spec.get("color"), th)
        want_spec = f"{fonts.get(spec['font'], spec['font'])} {spec['weight']} {spec['size']:g}pt" + (f", colour {col.upper()}" if col else "")
    where = f"PDF (View) - Topic Pages: page number centred below the content{', ' + want_spec if want_spec else ''}"
    if missing:
        out.append(_spec(label, "layout", lsev, f"Page number missing — {len(missing)} page(s)",
                         [Loc(p, (0, pdf[p].rect.height - 40, pdf[p].rect.width, pdf[p].rect.height)) for p in missing[:10]],
                         where, "spec page number",
                         figma="a page number centred below the content on every page" + (" after the cover" if start else ""),
                         stage=f"no page number on p.{', '.join(str(p + 1) for p in missing[:30])}" + (" …" if len(missing) > 30 else "")))
    if not found:
        return out
    # counting: the page after the cover is page 1. The whole document one off (the cover counted) is one
    # finding; pages that break the document's own sequence are listed
    offs = Counter(c["n"] - (p + 1 - start) for p, c in found.items())
    off0 = offs.most_common(1)[0][0]
    first = min(found)
    if off0:
        out.append(_spec(label, "layout", lsev, f"Page numbering — Figma starts at 1, stage at {1 + off0} ({offs[off0]} page(s))",
                         [Loc(first, found[first]["bbox"])],
                         where + ("; the first page after the cover is 1" if start else "; the first page is 1"), "spec page number",
                         figma=("the page after the cover is page 1" if start else "the first page is page 1"),
                         stage=(f"the page after the cover is page {1 + off0}" if start else f"the first page is page {1 + off0}")
                               + (" (the cover is counted)" if start and off0 == 1 else ""),
                         props=[{"property": "page number", "baseline": "Figma 1", "candidate": str(1 + off0)}]))
    wrong = [(p, c) for p, c in sorted(found.items()) if c["n"] - (p + 1 - start) != off0]
    if wrong:
        out.append(_spec(label, "layout", lsev, f"Page numbers out of sequence — {len(wrong)} page(s)",
                         [Loc(p, c["bbox"]) for p, c in wrong[:10]], where, "spec page number",
                         figma="page numbers count up by one",
                         stage=", ".join(f"p.{p + 1} shows {c['n']} (should be {p + 1 - start + off0})" for p, c in wrong[:8])
                               + (" …" if len(wrong) > 8 else "")))
    off = [(p, c) for p, c in sorted(found.items())
           if abs((c["bbox"][0] + c["bbox"][2]) / 2 - c["w"] / 2) > t.get("align_tolerance", 6)]
    if off:
        out.append(_spec(label, "layout", lsev, f"Page number not centred — {len(off)} page(s)",
                         [Loc(p, c["bbox"]) for p, c in off[:10]], where, "spec page number",
                         figma="centred below the content", stage=f"off-centre on p.{', '.join(str(p + 1) for p, _ in off[:20])}"))
    if spec:
        bad = defaultdict(list)
        for p, c in sorted(found.items()):
            s = c["span"]
            family, weight, _ = parse_font(s["font"], s["flags"])
            fam = fonts.get(spec["font"], spec["font"])
            col = color_of(spec.get("color"), th)
            if not _font_ok(family, fam):
                bad[("font-family", fam, family)].append(p)
            if weight != weights.get(spec["weight"], 400):
                bad[("font-weight", spec["weight"], str(weight))].append(p)
            if abs(s["size"] - spec["size"]) > t.get("size_tolerance", 0.25):
                bad[("font-size", f"{spec['size']:g}pt", f"{s['size']:g}pt")].append(p)
            if col and color_distance("#%06x" % s["color"], col.lower()) > t.get("color_tolerance", 24):
                bad[("color", col.upper(), ("#%06x" % s["color"]).upper())].append(p)
        for (prop, w_, g_), ps in bad.items():
            out.append(_spec(label, "style", sev, f"Page number — {_PROP.get(prop, prop)}: Figma {w_}, stage {g_} ({len(ps)} page(s))",
                             [Loc(p, found[p]["bbox"]) for p in ps[:10]], where, f"spec {prop}",
                             figma=want_spec, stage=_style_of(found[ps[0]]["span"], weights),
                             role="page number", props=[{"property": prop, "baseline": f"Figma {w_}", "candidate": g_}]))
    return out


def document(doc: Doc, cfg: dict, fmt: dict) -> list[Finding]:
    th = theme(doc, cfg)
    if fmt.get("print"):  # PDF (Print) has no cover: the document starts with the content, numbered from 1
        th = {**th, "cover": False}
        out = print_structure(doc, cfg, fmt)
    else:
        out = cover(doc, cfg, fmt, th)
    return out + page_numbers(doc, cfg, fmt, th) + callout_colons(doc, cfg, fmt) + pagination(doc, cfg, fmt)


_FRONT = re.compile(r"^(table of contents?|contents|q\s*&\s*a index)$", re.I)


def print_structure(doc: Doc, cfg: dict, fmt: dict) -> list[Finding]:
    """PDF (Print) Page Structure: no cover, Q&A index or table of contents; the version and title at the
    top of the first page only."""
    t = cfg.get("typography") or {}
    label, lsev = fmt["label"], t.get("layout_severity", "warning")
    pdf, out = _pdf(doc), []
    big = max((s["size"] for b in pdf[0].get_text("dict")["blocks"] for l in b.get("lines", []) for s in l["spans"]
               if s["text"].strip()), default=0)
    if big >= t.get("cover_min_text", 28):
        out.append(_spec(label, "layout", lsev, "Cover page in the Print version", [Loc(0, tuple(pdf[0].rect))],
                         "Page Structure: no cover, Q&A index or table of contents", "spec page structure",
                         figma="no cover: the document starts with the content page", stage="a cover page on p.1"))
    for p in range(min(len(pdf), 6)):
        for ln in (l for b in pdf[p].get_text("dict")["blocks"] for l in b.get("lines", [])):
            text = " ".join(s["text"].strip() for s in ln["spans"]).strip()
            if _FRONT.match(text):
                out.append(_spec(label, "layout", lsev, f"“{text}” page in the Print version", [Loc(p, tuple(ln["bbox"]))],
                                 "Page Structure: no cover, Q&A index or table of contents", "spec page structure",
                                 figma="no Q&A index or table of contents", stage=f"“{text}” on p.{p + 1}"))
    first = lambda p: [l for b in pdf[p].get_text("dict")["blocks"] for l in b.get("lines", [])
                       if l["bbox"][1] < 0.12 * pdf[p].rect.height and any(s["text"].strip() for s in l["spans"])]
    ver = lambda ls: [l for l in ls if _VERSION.match(" ".join(s["text"].strip() for s in l["spans"]).strip().split()[-1] if
                      " ".join(s["text"].strip() for s in l["spans"]).strip() else "")]
    if pdf.page_count and not ver(first(0)):
        out.append(_spec(label, "layout", lsev, "First page header: version missing", [Loc(0, (0, 0, pdf[0].rect.width, 60))],
                         "First page header: version and document title at the top of the first page only", "spec page structure",
                         figma="the document version and title at the top of page 1", stage="no version at the top of page 1"))
    rep = [p for p in range(1, pdf.page_count) if ver(first(p))]
    if rep:
        out.append(_spec(label, "layout", lsev, f"Document header repeated — {len(rep)} page(s)",
                         [Loc(p, (0, 0, pdf[p].rect.width, 60)) for p in rep[:10]],
                         "First page header: not repeated on later pages", "spec page structure",
                         figma="version and title on page 1 only", stage=f"repeated on p.{', '.join(str(p + 1) for p in rep[:20])}"))
    return out


def pagination(doc: Doc, cfg: dict, fmt: dict) -> list[Finding]:
    """Content / Component Pagination Rules: a heading never last on a page; a callout or picture never split
    over two pages; a table continued on the next page repeats its header, and a table header never stands
    alone at the bottom of a page."""
    t = cfg.get("typography") or {}
    label, lsev = fmt["label"], t.get("layout_severity", "warning")
    m = fmt.get("margins") or {}
    out = []
    # headings at the bottom of a page: nothing of the section follows on that page
    last_line = {}
    for li, ln in enumerate(doc.lines):
        if ln.first_word >= 0 and doc.words[ln.first_word].norm:
            last_line[ln.page] = li
    orphan = []
    for page, li in last_line.items():
        ln = doc.lines[li]
        w = doc.words[ln.first_word]
        if w.role[:1] == "h" and w.role[1:].isdigit() and page + 1 < len(doc.pages):
            H = doc.pages[page].height
            pics = [im.bbox for im in doc.images if im.page == page] + list(tables_mod._figure_rects(doc, page))
            try:
                pics += [tuple(r) for r in _pdf(doc)[page].cluster_drawings() if r.height > 20]
            except Exception:
                pass
            below = any(b[1] > ln.bbox[3] for b in pics)
            if not below and ln.bbox[3] > H * 5 / 6:  # the lowest thing on the page, in its bottom sixth
                orphan.append((li, w.role))
    if orphan:
        out.append(_spec(label, "layout", lsev, f"Heading at the bottom of a page — {len(orphan)} heading(s) "
                         f"(e.g. “{doc.lines[orphan[0][0]].text.strip()[:50]}”)",
                         [Loc(doc.lines[li].page, doc.lines[li].bbox) for li, _ in orphan[:10]],
                         "Heading Rules: H1-H3 never at the bottom of a page; the heading moves to the next page",
                         "spec pagination", figma="the heading starts the next page, with its content",
                         stage="last line of p." + ", p.".join(str(doc.lines[li].page + 1) for li, _ in orphan[:20])))
    # callouts split over two pages: a box reaching the bottom margin, the same colour at the top of the next page
    cos = [c for c in callouts(doc, cfg) if c.get("box")]
    split = []
    for c in cos:
        H = doc.pages[c["page"]].height
        if c["box"][3] >= H - m.get("bottom", 24) - 30:
            nxt = [d for d in cos if d["page"] == c["page"] + 1 and d["box"][1] <= m.get("top", 24) + 30 and d["fill"] == c["fill"]]
            if nxt:
                split.append(c)
    pdf = _pdf(doc)
    # any shaded box (a note / callout panel, with or without a title) cut by the page break: it reaches the
    # last line of text on its page, and a box of the same colour and width starts the next page
    def boxes(p):
        out = []
        for d in pdf[p].get_drawings():
            r, f = d["rect"], d.get("fill")
            if f and r.width > 100 and r.height > 12 and sum(f[:3]) < 2.9 and r.width * r.height < 0.8 * pdf[p].rect.get_area():
                out.append((pymupdf.Rect(r), _hex(f)))
        return out
    seen = {(c["page"], tuple(round(v) for v in c["box"])) for c in split}
    tabs = defaultdict(list)  # a table's grid (its border colour behind the cells) may break over pages
    for tb in tables_mod.tables(doc, (0, len(doc.words))):
        tabs[tb.page].append(pymupdf.Rect(tb.bbox))
    in_table = lambda p, r: any((t & r).get_area() > 0.3 * r.get_area() for t in tabs[p])
    for p in range(len(pdf) - 1):
        ys = [ln.bbox[3] for ln in doc.lines if ln.page == p and ln.bbox[1] < pdf[p].rect.height * 0.92]
        ys2 = [ln.bbox[1] for ln in doc.lines if ln.page == p + 1 and ln.bbox[1] > pdf[p + 1].rect.height * 0.06]
        if not ys or not ys2:
            continue
        low, high = max(ys), min(ys2)
        nxt = boxes(p + 1)
        H = pdf[p].rect.height
        imgs = [pymupdf.Rect(i["bbox"]) for i in pdf[p].get_image_info()]
        titled = {tuple(round(v) for v in c["box"]) for c in cos if c["page"] == p and c.get("title") is not None}
        for r, col in boxes(p):
            if r.y1 < low - 4 or r.y1 < H - m.get("bottom", 24) - 40 or in_table(p, r):
                continue  # the box ends before the page does / a table
            # a callout: its title, or its icon in the left part of the box (a row of a list or table has neither)
            icon = sum(1 for i in imgs if r.contains(i) and i.x1 <= r.x0 + 0.3 * r.width and i.width <= 70) == 1  # (a list: one per row)
            if not icon and tuple(round(v) for v in r) not in titled:
                continue
            twin = [q for q, c2 in nxt if not in_table(p + 1, q) and c2 == col and abs(q.x0 - r.x0) < 6
                    and abs(q.width - r.width) < 0.15 * r.width and q.y0 <= min(high + 4, m.get("top", 24) + 40)]
            if twin and (p, tuple(round(v) for v in r)) not in seen:
                seen.add((p, tuple(round(v) for v in r)))
                split.append({"page": p, "box": tuple(r)})
    if split:
        out.append(_spec(label, "layout", lsev, f"Callout split over two pages — {len(split)} callout(s)",
                         [Loc(c["page"], c["box"]) for c in split[:10]],
                         "Callout / Image: one unit; the whole component moves to the next page", "spec pagination",
                         figma="the whole callout on one page", stage="split at the end of p." + ", p.".join(str(c["page"] + 1) for c in split)))
    # tables: a header alone at the bottom of a page, a continuation without the repeated header
    alone, bare = [], []
    tbs = [tb for tb in tables_mod.tables(doc, (0, len(doc.words))) if tables_mod.is_data_table(doc, tb) and tb.rows[0].idx]
    light = lambda i: (lambda c: len(c) == 7 and sum(int(c[k:k + 2], 16) for k in (1, 3, 5)) > 600)(doc.words[i].style.color)
    head = lambda tb: sum(light(i) for i in tb.rows[0].idx) * 2 >= len(tb.rows[0].idx)
    for tb in tbs:
        H = doc.pages[tb.page].height
        if head(tb) and len(tb.rows) <= 1 and tb.bbox[3] >= H - m.get("bottom", 24) - 60:
            alone.append(tb)
    by_page = defaultdict(list)
    for tb in tbs:
        by_page[tb.page].append(tb)
    for tb in tbs:
        H = doc.pages[tb.page].height
        if not head(tb) or tb.bbox[3] < H - m.get("bottom", 24) - 40:
            continue
        nxt = [x for x in by_page.get(tb.page + 1, []) if x.bbox[1] <= m.get("top", 24) + 40]
        if nxt and not head(nxt[0]) and abs((nxt[0].bbox[2] - nxt[0].bbox[0]) - (tb.bbox[2] - tb.bbox[0])) < 10:
            bare.append(nxt[0])
    if alone:
        out.append(_spec(label, "layout", lsev, f"Table header alone at the bottom of a page — {len(alone)} table(s)",
                         [Loc(x.page, x.bbox) for x in alone[:10]], "Table: the header never alone at the bottom of a page",
                         "spec pagination", figma="the table moves to the next page", stage="only the header row on p." + ", p.".join(str(x.page + 1) for x in alone)))
    if bare:
        out.append(_spec(label, "layout", lsev, f"Continued table without its header — {len(bare)} table(s)",
                         [Loc(x.page, x.rows[0].box) for x in bare[:10]], "Table: the header is repeated at the top of the continued table",
                         "spec pagination", figma="the header row repeated on the next page", stage="no header row on p." + ", p.".join(str(x.page + 1) for x in bare)))
    return out


# ------------------------------------------------------------------------------------------------ callouts
_CALLOUTS: dict[tuple, list[dict]] = {}
_TYPE = {"note": "note", "notes": "note", "tip": "tip", "tips": "tip", "warning": "warning", "warnings": "warning",
         "important": "important"}


def _icon_hash(pdf: pymupdf.Document, page: int, box) -> tuple | None:
    """12 x 12 average hash of the icon, to tell icons apart."""
    try:
        pix = pdf[page].get_pixmap(clip=pymupdf.Rect(box), matrix=pymupdf.Matrix(3, 3), colorspace=pymupdf.csGRAY, alpha=False)
        from PIL import Image as PILImage
        im = PILImage.frombytes("L", (pix.width, pix.height), pix.samples).resize((12, 12), PILImage.BOX)
        v = list(im.getdata()) if not hasattr(im, "get_flattened_data") else list(im.get_flattened_data())
        m = sum(v) / len(v)
        return tuple(int(x < m) for x in v)
    except Exception:
        return None


def callouts(doc: Doc, cfg: dict) -> list[dict]:
    """Every callout of the document: a coloured box holding a callout title ("NOTE:", "TIP" ...) at the start of
    a line, or a box in a callout colour with an icon at its top left and no title. Per callout: page, box, fill,
    title word(s), its text and type, the icon's box and hash, pictures inside and further titles inside."""
    comp = (cfg.get("typography") or {}).get("components") or {}
    key = (doc.path, repr(sorted(comp.items())))
    if key in _CALLOUTS:
        return _CALLOUTS[key]
    tol = (cfg.get("typography") or {}).get("color_tolerance", 24)
    backgrounds = {k: v.lower() for k, v in (comp.get("callout_backgrounds") or {}).items()}
    pdf = _pdf(doc)
    by_page: dict[int, list[int]] = defaultdict(list)
    for i, w in enumerate(doc.words):
        by_page[w.page].append(i)
    out = []
    # a table cell holding the word "Warning" (a legend of the note icons) is not a callout
    cells = defaultdict(list)
    for tb in tables_mod.tables(doc, (0, len(doc.words))):
        if tables_mod.is_data_table(doc, tb) or len(tb.rows) >= 3 or max(r.cells for r in tb.rows) >= 3:
            cells[tb.page].append(pymupdf.Rect(tb.bbox))  # (a callout box read as a table is icon | text: 2 cells)
    for p, idx in sorted(by_page.items()):
        pg = pdf[p]
        area = pg.rect.width * pg.rect.height
        fills = [(pymupdf.Rect(d["rect"]), _hex(d["fill"])) for d in pg.get_drawings()
                 if d.get("fill") and d["rect"].width >= 80 and d["rect"].height >= 14 and d["rect"].width * d["rect"].height < 0.8 * area]
        fills = [(r, c) for r, c in fills if sum(int(c[k:k + 2], 16) for k in (1, 3, 5)) < 750]  # coloured, not white
        images = [tuple(i["bbox"]) for i in pg.get_image_info()]
        # a title is a line of its own ("NOTE:"), never a heading ("Notes on HDMI port and cable")
        titles = [i for i in idx if doc.lines[doc.words[i].line].first_word == i and not doc.words[i].role.startswith("h")
                  and tables_mod._CALLOUT_LABEL.match(doc.lines[doc.words[i].line].text.strip())
                  and not any(r.contains(pymupdf.Point((doc.words[i].bbox[0] + doc.words[i].bbox[2]) / 2,
                                                       (doc.words[i].bbox[1] + doc.words[i].bbox[3]) / 2)) for r in cells[p])]
        boxes: dict[tuple, dict] = {}
        for i in titles:
            w = doc.words[i]
            c = pymupdf.Point((w.bbox[0] + w.bbox[2]) / 2, (w.bbox[1] + w.bbox[3]) / 2)
            holding = [(r, col) for r, col in fills if r.contains(c)]
            r, col = min(holding, key=lambda rc: rc[0].get_area()) if holding else (None, None)
            k = tuple(round(v) for v in r) if r is not None else ("plain", i)
            if k in boxes:
                boxes[k]["inner"].append(i)  # a second title inside the same box
                continue
            boxes[k] = {"page": p, "box": tuple(r) if r is not None else None, "fill": col, "title": i, "inner": []}
        isize = comp.get("callout_icon_size") or 14
        grey = backgrounds.get("tip")
        for r, col in fills:  # a box in a callout colour with a callout icon and no title (grey is any panel's colour)
            k = tuple(round(v) for v in r)
            if k in boxes or not any(_near(col, b, tol) for t_, b in backgrounds.items() if b != grey):
                continue
            icon = [im for im in images if r.contains(pymupdf.Rect(im)) and abs(im[3] - im[1] - isize) <= 3
                    and abs(im[2] - im[0] - isize) <= 3 and im[0] - r.x0 < 30 and im[1] - r.y0 < 30]
            if icon and any(r.contains(pymupdf.Point((doc.words[i].bbox[0] + doc.words[i].bbox[2]) / 2,
                                                     (doc.words[i].bbox[1] + doc.words[i].bbox[3]) / 2)) for i in idx):
                boxes[k] = {"page": p, "box": tuple(r), "fill": col, "title": None, "inner": [], "icon": icon[0]}
        for co in boxes.values():
            r = pymupdf.Rect(co["box"]) if co["box"] else None
            ti = co["title"]
            inside = [i for i in idx if r is not None and r.contains(pymupdf.Point(
                (doc.words[i].bbox[0] + doc.words[i].bbox[2]) / 2, (doc.words[i].bbox[1] + doc.words[i].bbox[3]) / 2))]
            co["words"] = inside or ([ti] if ti is not None else [])
            if ti is not None:
                w = doc.words[ti]
                co["text"] = w.text.strip()
                co["type"] = _TYPE.get(co["text"].rstrip(":").strip().lower())
                cy = (w.bbox[1] + w.bbox[3]) / 2
                if r is not None:
                    icon = [im for im in images if r.contains(pymupdf.Rect(im).tl + (0.5, 0.5)) and im[2] <= w.bbox[0] + 2
                            and im[2] - im[0] <= 24 and im[3] - im[1] <= 24 and abs((im[1] + im[3]) / 2 - cy) <= 10]
                    co["icon"] = max(icon, key=lambda im: im[2]) if icon else None
                else:
                    co["icon"] = None
            else:
                co["text"], co["type"] = None, None
            if co.get("icon"):
                co["hash"] = _icon_hash(pdf, p, co["icon"])
            co["pictures"] = [im for im in images if r is not None and r.contains(pymupdf.Rect(im).tl + (1, 1))
                              and im[3] - im[1] > 24 and im[2] - im[0] > 40]
            out.append(co)
    _CALLOUTS[key] = out
    return out


def _hamming(a, b) -> int:
    return sum(x != y for x, y in zip(a, b))


def _icon_types(cos: list[dict]) -> dict[str, tuple]:
    """The usual icon of each callout type: the icon closest to the others of its type."""
    by: dict[str, list[tuple]] = defaultdict(list)
    for co in cos:
        if co.get("type") and co.get("hash"):
            by[co["type"]].append(co["hash"])
    return {k: min(hs, key=lambda h: sum(_hamming(h, o) for o in hs)) for k, hs in by.items() if len(hs) >= 2}


def _callout_findings(u: Unit, fmt: dict) -> list[Finding]:
    t = u.cfg.get("typography") or {}
    comp = t.get("components") or {}
    label, sev, lsev = fmt["label"], t.get("severity", "warning"), t.get("layout_severity", "warning")
    tol = t.get("color_tolerance", 24)
    titles = comp.get("callout_titles") or {}
    backgrounds = comp.get("callout_backgrounds") or {}
    doc = u.b
    allc = callouts(doc, u.cfg)
    usual = _icon_types(allc)
    size = comp.get("callout_icon_size")
    mine = [co for co in allc if u.b_range[0] <= (co["title"] if co["title"] is not None else co["words"][0]) < u.b_range[1]]
    kinds = ", ".join(f"{titles[k]} {backgrounds.get(k, '').upper()}" for k in titles)
    where = f"Callout Component: {kinds}; fixed title, the type's icon, paragraph content only (no image, no callout inside)"
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for co in mine:
        typ = co["type"]
        if co["title"] is None:
            groups[("title missing", "", "")].append(co)
        elif typ is None:
            groups[("not a callout type", co["text"], "")].append(co)
        elif co["text"] != titles.get(typ, co["text"]) and co["text"].rstrip(":").strip() != titles[typ]:
            groups[("title", titles[typ], co["text"])].append(co)  # ("NOTE:" for NOTE: one finding, see callout_colons)
        want = backgrounds.get(typ) if typ else None
        if co["title"] is not None and co["box"] is None:
            groups[("no box", titles.get(typ) or co["text"], "")].append(co)
        elif want and not _near(co["fill"], want, tol):
            groups[("background", f"{titles[typ]} {want.upper()}", (co["fill"] or "").upper())].append(co)
        if co["box"] is not None and typ:
            if not co.get("icon"):
                groups[("icon missing", titles[typ], "")].append(co)
            else:
                h = co.get("hash")
                if h and typ in usual and _hamming(h, usual[typ]) > 30:
                    other = min((k for k in usual if k != typ), key=lambda k: _hamming(h, usual[k]), default=None)
                    alike = other if other and _hamming(h, usual[other]) <= 30 else None
                    groups[("icon differs", titles[typ], titles.get(alike, "") if alike else "")].append(co)
                ih = co["icon"][3] - co["icon"][1]
                if size and abs(ih - size) > 3:
                    groups[("icon size", f"{size:g}pt", f"{ih:.0f}pt")].append(co)
        if co["pictures"] and not _prod_note_has_picture(u, co):
            groups[("picture inside", titles.get(typ) or co["text"] or "", "")].append(co)
        if co["inner"]:
            groups[("callout inside", titles.get(typ) or co["text"] or "", doc.words[co["inner"][0]].text)].append(co)
    out = []
    for (what, a, b), cos in groups.items():
        n = len(cos)
        at = lambda co: Loc(co["page"], co["box"] or doc.words[co["title"]].bbox)
        title_at = lambda co: Loc(co["page"], doc.words[co["title"]].bbox)
        cnt = f"{n} callout(s)"
        if what == "title":
            msg, fig, stg, typ, where_, cat = (f"Callout title — {cnt}", f"“{a}” (fixed title of the type)", f"“{b}”",
                                               "spec callout title", [title_at(c) for c in cos], "style")
        elif what == "title missing":
            msg, fig, stg, typ, where_, cat = (f"Callout title missing — {cnt}", "a title: IMPORTANT, NOTE, TIP or WARNING",
                                               "a callout box with no title", "spec callout title", [at(c) for c in cos], "style")
        elif what == "not a callout type":
            msg, fig, stg, typ, where_, cat = (f"Callout type — {cnt}", "one of IMPORTANT, NOTE, TIP, WARNING", f"“{a}”",
                                               "spec callout title", [title_at(c) for c in cos], "style")
        elif what == "no box":
            msg, fig, stg, typ, where_, cat = (f"“{a}” is not a Callout component — {n} place(s)",
                                               f"a {a} Callout: coloured box, icon and title", f"“{a}” as plain text, no box",
                                               "spec callout background", [title_at(c) for c in cos], "style")
        elif what == "background":
            msg, fig, stg, typ, where_, cat = (f"Callout background — {cnt}", a, b or "none",
                                               "spec callout background", [at(c) for c in cos], "style")
        elif what == "icon missing":
            msg, fig, stg, typ, where_, cat = (f"{a} callout icon missing — {cnt}", f"the {a} icon left of the title",
                                               "no icon", "spec callout icon", [at(c) for c in cos], "style")
        elif what == "icon differs":
            msg, fig, typ, where_, cat = (f"{a} callout icon — {cnt}", f"the {a} icon", "spec callout icon",
                                          [Loc(c["page"], c["icon"]) for c in cos], "style")
            stg = f"the {b} icon" if b else f"an icon unlike the other {a} callouts'"
        elif what == "icon size":
            msg, fig, stg, typ, where_, cat = (f"Callout icon size — {cnt}", a, b, "spec callout icon",
                                               [Loc(c["page"], c["icon"]) for c in cos], "style")
        elif what == "picture inside":
            msg, fig, stg, typ, where_, cat = (f"Picture inside a {a} callout — {cnt}", "a callout holds paragraphs only, no picture",
                                               "a picture inside the callout", "spec callout content",
                                               [Loc(c["page"], c["pictures"][0]) for c in cos], "layout")
        else:
            msg, fig, stg, typ, where_, cat = (f"Callout inside a {a} callout — {cnt}", "a callout holds paragraphs only, no callout inside",
                                               f"a “{b}” callout inside it", "spec callout content",
                                               [Loc(c["page"], doc.words[c["inner"][0]].bbox) for c in cos], "layout")
        props = [{"property": typ.replace("spec ", ""), "baseline": f"Figma {fig}", "candidate": stg}]
        out.append(_spec(label, cat, lsev if cat == "layout" else sev, msg, where_[:u.cfg["report"]["max_locs"]], where, typ,
                         figma=fig, stage=stg, role="callout", props=props))
    return out


def _prod_note_has_picture(u: Unit, co: dict, gap: float = 40) -> bool:
    """Prod's note holds the picture too: a picture right below the prod text of the callout (within `gap`
    pt), under the note's text, not further left. Stage keeps prod's note as it was - not a callout issue."""
    prod_of = dict(u.pairs)
    back = {j: i for i, j in u.pairs}
    idx = [back[j] for j in co["words"] if j in back]
    if not idx:
        return False
    A = u.a
    page = A.words[idx[-1]].page
    idx = [i for i in idx if A.words[i].page == page]
    bottom, left = max(A.words[i].bbox[3] for i in idx), min(A.words[i].bbox[0] for i in idx)
    pics = [im.bbox for im in A.images if im.page == page] + list(tables_mod._figure_rects(A, page))
    try:
        pics += [tuple(r) for r in _pdf(A)[page].cluster_drawings() if r.width > 40 and r.height > 30]
    except Exception:
        pass
    if any(0 <= b[1] - bottom <= gap and b[0] >= left - 20 for b in pics):
        return True
    # a drawing (curves, not rules) below the note's text inside the drawing that surrounds the note
    try:
        drs = _pdf(A)[page].get_drawings()
        top = min(A.words[i].bbox[1] for i in idx)
        for box in (pymupdf.Rect(r) for r in _pdf(A)[page].cluster_drawings(drawings=drs)):
            if not (box.y0 <= top and box.y1 > bottom + gap):
                continue
            curves = sum(1 for d in drs if box.contains(pymupdf.Rect(d["rect"])) and d["rect"].y0 >= bottom - 2
                         and d["rect"].x0 >= left - 20 and d["rect"].y0 <= bottom + 150
                         for it in d["items"] if tables_mod.is_curve(it))
            if curves >= 8:
                return True
    except Exception:
        pass
    return False


def callout_colons(doc: Doc, cfg: dict, fmt: dict) -> list[Finding]:
    """Callout titles that differ from Figma's fixed title only by a colon ("NOTE:" for NOTE): the template's
    way of writing every title, one finding for the document rather than one per section."""
    t = cfg.get("typography") or {}
    titles = (t.get("components") or {}).get("callout_titles") or {}
    cos = [co for co in callouts(doc, cfg) if co.get("type") in titles and co["text"] != titles[co["type"]]
           and co["text"].rstrip(":").strip() == titles[co["type"]]]
    if not cos:
        return []
    kinds = Counter(co["text"] for co in cos)
    return [_spec(fmt["label"], "style", t.get("severity", "warning"),
                  f"Callout titles end with a colon — {len(cos)} callout(s)",
                  [Loc(co["page"], doc.words[co["title"]].bbox) for co in cos[:cfg["report"]["max_locs"]]],
                  "Callout Component: each type has a fixed title that cannot be modified", "spec callout title",
                  figma=", ".join(titles.values()) + " (no colon)",
                  stage=", ".join(f"“{k}” ×{n}" for k, n in kinds.most_common()),
                  role="callout", props=[{"property": "callout title", "baseline": "Figma NOTE", "candidate": "NOTE:"}])]


def callout_title_words(doc: Doc, cfg: dict) -> set[int]:
    return {co["title"] for co in callouts(doc, cfg) if co["title"] is not None and co["box"] is not None}


# ------------------------------------------------------------------------------------------------ tables
def _table_findings(u: Unit, fmt: dict) -> list[Finding]:
    t = u.cfg.get("typography") or {}
    comp = t.get("components") or {}
    head_bg, border = comp.get("table_header_background"), comp.get("table_border")
    if not head_bg and not border:
        return []
    label, sev = fmt["label"], t.get("severity", "warning")
    tol = t.get("color_tolerance", 24)
    doc = u.b
    pdf = _pdf(doc)
    bad = defaultdict(list)
    light = lambda c: len(c) == 7 and sum(int(c[k:k + 2], 16) for k in (1, 3, 5)) > 600
    for tb in tables_mod.tables(doc, u.b_range):
        if not tables_mod.is_data_table(doc, tb) or not tb.rows[0].idx:
            continue
        first = tb.rows[0]
        if head_bg and sum(light(doc.words[i].style.color) for i in first.idx) * 2 >= len(first.idx):
            pts = [pymupdf.Point((doc.words[i].bbox[0] + doc.words[i].bbox[2]) / 2, (doc.words[i].bbox[1] + doc.words[i].bbox[3]) / 2)
                   for i in first.idx]
            fills = Counter()
            for d in pdf[tb.page].get_drawings():
                if d.get("fill"):
                    r = pymupdf.Rect(d["rect"])
                    hit = sum(r.contains(p) for p in pts)
                    if hit:
                        fills[_hex(d["fill"])] += hit
            got = next((c for c, _ in fills.most_common() if not light(c)), None)
            if got is None or not _near(got, head_bg, tol):
                bad[("header", head_bg.upper(), (got or "none").upper())].append(tb)
        if border:
            got = tables_mod._border_color(doc, tb.page, tb.bbox, ("bottom", "left", "right"))  # the top is the header bar
            if got and not _near(got, border, tol):
                bad[("border", border.upper(), got.upper())].append(tb)
    out = []
    for (what, w_, g_), tbs in bad.items():
        name = "header bar colour" if what == "header" else "border colour"
        out.append(_spec(label, "style", sev, f"Table {name} — Figma {w_}, stage {g_} ({len(tbs)} table(s))",
                         [Loc(x.page, x.rows[0].box if what == "header" else x.bbox) for x in tbs],
                         f"Table Component: header bar {str(head_bg).upper()} with white Table Header text, border {str(border).upper()}",
                         f"spec table {'header' if what == 'header' else 'border'}", role="table",
                         figma=f"{name} {w_}", stage=f"{name} {g_}",
                         props=[{"property": name, "baseline": f"Figma {w_}", "candidate": g_}]))
    return out


# ------------------------------------------------------------------------------------------------ lists
def _list_findings(u: Unit, fmt: dict) -> list[Finding]:
    t = u.cfg.get("typography") or {}
    comp = t.get("components") or {}
    cycle, bullets = comp.get("numbering") or [], comp.get("bullets") or ""
    if not cycle and not bullets:
        return []
    lcfg = u.cfg["layout"]
    doc = u.b
    items = layout_mod._list_items(doc, u.b_range, layout_mod._marker_test(lcfg),
                                   lcfg.get("bullet_tolerance_em", lcfg["line_height_tolerance_em"]),
                                   lcfg["bullet_intro_max_gap_em"])
    if not items:
        return []
    layout_mod._numbering(doc, items)
    boxes = [(tb.page, pymupdf.Rect(tb.bbox)) for tb in tables_mod.tables(doc, u.b_range) if tables_mod.is_data_table(doc, tb)]
    in_table = lambda w: any(p == w.page and r.contains(pymupdf.Point((w.bbox[0] + w.bbox[2]) / 2, (w.bbox[1] + w.bbox[3]) / 2))
                             for p, r in boxes)
    lists: dict[tuple, list[dict]] = defaultdict(list)
    for it in items.values():
        if not in_table(doc.words[it["marker"]]):
            lists[it["list"]].append(it)
    pdf = _pdf(doc)
    pics: dict[int, list] = {}

    def under_picture(w) -> bool:  # "(a) Display  (b) Wall": the key of the drawing above it
        if w.page not in pics:
            pics[w.page] = [tuple(i["bbox"]) for i in pdf[w.page].get_image_info() if i["bbox"][3] - i["bbox"][1] > 30] \
                + list(map(tuple, tables_mod._figure_rects(doc, w.page))) \
                + [tuple(r) for r in pdf[w.page].cluster_drawings() if r.height > 40 and r.width > 40]
        return any(0 <= w.bbox[1] - p[3] <= 60 and w.bbox[0] <= p[2] and p[2] - p[0] > 60 for p in pics[w.page])

    order = sorted(items.values(), key=lambda it: it["marker"])

    def parent(it):
        """The item this one's list sits in: the list's parent, or - when the lists are apart (a paragraph between
        a step and its sub-steps) - the nearest item before it whose text starts where this marker is."""
        m = doc.words[it["marker"]]
        under = lambda o: doc.words[o["text"]].bbox[0] - 6 <= m.bbox[0] <= doc.words[o["text"]].bbox[0] + 30
        if it.get("parent") is not None:
            return it["parent"] if under(it["parent"]) else None  # a list in the next column is not a sub-list
        for o in reversed([o for o in order if o["marker"] < it["marker"]]):
            om, ot = doc.words[o["marker"]], doc.words[o["text"]]
            if om.page != m.page or m.bbox[1] - om.bbox[1] > 300:
                break  # the item a sub-list belongs to is close above it on its page
            if om.bbox[0] < m.bbox[0] - 4 and under(o):
                # under the item's text, and nothing in between back at the outer margin (a heading, a paragraph:
                # the item has ended); a list in the next column starts much further right
                x = doc.words[o["text"]].bbox[0] - 4
                between = range(doc.words[o["text"]].line + 1, m.line)
                return None if any(doc.lines[li].page == m.page and doc.lines[li].bbox[0] < x for li in between) else o
            if om.bbox[0] <= m.bbox[0] - 4 or abs(om.bbox[0] - m.bbox[0]) <= 4:
                return None  # an item further left that this one does not sit under, or a sibling: top level
        return None

    body = doc.body_size or 12

    def level(it) -> int:  # ordered lists this one sits inside ("2. Connect the PC video cable" in heading-size type
        n, p, seen = 0, parent(it), 0  # is a numbered step heading, not a list level)
        while p is not None and seen < 12:
            n += "enum" in p and doc.words[p["text"]].style.size <= body + 0.6
            p, seen = parent(p), seen + 1
        return n

    bad = defaultdict(list)
    for members in lists.values():
        first = members[0]
        if "enum" in first:
            style = first.get("style")
            if not cycle or style is None:
                continue
            if len(members) < 2 and first.get("value") != 1:
                continue  # one "H." in running text is not a list
            if under_picture(doc.words[first["marker"]]):
                continue
            want = cycle[level(first) % len(cycle)]
            if style != want:
                bad[("numbering", level(first) + 1, want, style)].extend(members)
        elif bullets:
            for it in members:
                glyph = doc.words[it["marker"]].text.strip()
                if "enum" in it or not glyph or set(glyph) <= set("*†‡"):
                    continue  # a numbered item; "*" / "**" mark a footnote, not a list
                if glyph[0] not in bullets:
                    bad[("bullet", 0, bullets[0], glyph)].append(it)
    out = []
    label, sev = fmt["label"], t.get("severity", "warning")
    for (what, lvl, want, got), its in bad.items():
        idx = [it["marker"] for it in its]
        eg = f"{len(its)} item(s), e.g. “{snippet(doc, [its[0]['marker'], its[0]['text']], 6)}”"
        if what == "numbering":
            msg, typ = f"Ordered list level {lvl} numbering ({eg})", "spec list numbering"
            fig, stg = f"level {lvl} numbered {want}", f"numbered {got}"
        else:
            msg, typ = f"Bullet style ({eg})", "spec bullet"
            fig, stg = f"a black circle “{want}”", f"“{got}”"
        out.append(_spec(label, "style", sev, msg, locs(doc, idx, u.cfg["report"]["max_locs"]),
                         f"Lists: ordered {' → '.join(cycle)} by level (level 4 starts again), unordered a black circle",
                         typ, figma=fig, stage=stg, role="list", props=[{"property": typ.replace("spec ", ""), "baseline": f"Figma {want}", "candidate": got}]))
    return out


# ------------------------------------------------------------------------------------------------ headings
_HEADS: dict[str, list[tuple[int, int]]] = {}


def _headings(doc: Doc) -> list[tuple[int, int]]:
    """(first word, level) of every heading line of the document, in reading order."""
    if doc.path not in _HEADS:
        out, last = [], None
        for i, w in enumerate(doc.words):
            if w.role[:1] == "h" and w.role[1:].isdigit() and w.line != last:
                out.append((i, int(w.role[1:])))
            last = w.line
        _HEADS[doc.path] = out
    return _HEADS[doc.path]


def _heading_findings(u: Unit, fmt: dict) -> list[Finding]:
    t = u.cfg.get("typography") or {}
    doc = u.b
    heads = _headings(doc)
    out = []
    for k, (i, lvl) in enumerate(heads):
        if not u.b_range[0] <= i < u.b_range[1] or k == 0:
            continue
        j, prev = heads[k - 1]
        if lvl > prev + 1:
            line = [n for n in range(i, min(len(doc.words), i + 30)) if doc.words[n].line == doc.words[i].line]
            out.append(_spec(fmt["label"], "structure", t.get("layout_severity", "warning"),
                             f"Heading level skipped: “{snippet(doc, line, 8)}”",
                             locs(doc, line, 3), "Heading Hierarchy: H1 → H2 → H3, levels are not skipped",
                             "spec heading level", figma=f"H1 → H2 → H3, no level skipped (H{prev + 1} or higher here)",
                             stage=f"H{lvl} right after the H{prev} “{snippet(doc, [j], 1)}…”", role=f"h{lvl}",
                             props=[{"property": "heading level", "baseline": f"Figma H{prev + 1} or higher", "candidate": f"H{lvl}"}]))
    return out


# ------------------------------------------------------------------------------------------------ alignment
_RTL = re.compile(r"[֐-ࣿיִ-﷿ﹰ-﻿]")


def _align_findings(u: Unit, fmt: dict, role: dict[int, str]) -> list[Finding]:
    """Body text lines that are centred or right-aligned in the text column (content is left-aligned; right-to-left
    languages are the exception). Lines in tables, pictures and callout titles have other roles and are skipped."""
    t = u.cfg.get("typography") or {}
    doc = u.b
    tol = t.get("align_tolerance", 6)
    body = {"body_default", "body_strong", "body_hyperlink"}
    by_line: dict[int, list[int]] = defaultdict(list)
    for i, r in role.items():
        by_line[doc.words[i].line].append(i)
    tables = [(tb.page, pymupdf.Rect(tb.bbox)) for tb in tables_mod.tables(doc, u.b_range)]
    pic_cache: dict[int, list] = {}
    box_cache: dict[int, list] = {}

    def boxes_on(page: int) -> list:
        if page not in box_cache:
            try:
                box_cache[page] = [pymupdf.Rect(r) for r in _pdf(doc)[page].cluster_drawings() if r.width > 60 and r.height > 20]
            except Exception:
                box_cache[page] = []
        return box_cache[page]

    def pictures(page: int) -> list:
        if page not in pic_cache:
            pg = _pdf(doc)[page]
            pic_cache[page] = [tuple(i["bbox"]) for i in pg.get_image_info()] + list(map(tuple, tables_mod._figure_rects(doc, page)))
        return pic_cache[page]
    page_lines: dict[int, list] = defaultdict(list)
    for li in by_line:
        page_lines[doc.lines[li].page].append(doc.lines[li])
    bad = defaultdict(list)
    for li, idx in by_line.items():
        if not all(role.get(i) in body for i in idx):
            continue
        ln = doc.lines[li]
        if len(ln.text.strip()) < 4 or _RTL.search(ln.text) or ln.page == 0:
            continue
        left, right = doc.left(ln.page), doc.right(ln.page)
        if ln.bbox[2] - ln.bbox[0] > 0.7 * (right - left) or ln.bbox[0] - left <= tol:
            continue
        mid_pt = pymupdf.Point((ln.bbox[0] + ln.bbox[2]) / 2, (ln.bbox[1] + ln.bbox[3]) / 2)
        if any(p == ln.page and r.contains(mid_pt) for p, r in tables):
            continue  # a table cell
        # a line starting where a line near it starts is left-aligned in its column (a two-column list,
        # text beside a picture): centred / right-aligned lines of different lengths start at different x
        near = [o for o in page_lines[ln.page] if o is not ln and abs(o.bbox[1] - ln.bbox[1]) < 4 * ln.size]
        if any(abs(o.bbox[0] - ln.bbox[0]) <= 2 for o in near):
            continue
        if any(r.contains(mid_pt) for r in boxes_on(ln.page)):
            continue  # in a drawn box: a table the detector did not see, a panel
        pics = pictures(ln.page)
        if any(p[2] <= ln.bbox[0] + 2 and p[2] >= ln.bbox[0] - 40 and p[1] < ln.bbox[3] and p[3] > ln.bbox[1] for p in pics):
            continue  # the line goes on after an inline icon / a picture beside it
        if any(p[0] - 2 <= (ln.bbox[0] + ln.bbox[2]) / 2 <= p[2] + 2 and -30 <= ln.bbox[1] - p[3] <= 30 or
               p[0] - 2 <= (ln.bbox[0] + ln.bbox[2]) / 2 <= p[2] + 2 and -30 <= p[1] - ln.bbox[3] <= 30 for p in pics
               if p[3] - p[1] > 20):
            continue  # a caption / label of a picture
        mid = abs((ln.bbox[0] + ln.bbox[2]) / 2 - (left + right) / 2) <= tol
        # right-aligned: a paragraph whose lines end at the right edge and start at different x (one line
        # ending at the right margin is a full line of text beside something on the left)
        blk = [o for o in page_lines[ln.page] if o.block == ln.block]
        flush_right = len(blk) >= 2 and all(abs(o.bbox[2] - right) <= tol for o in blk) \
            and max(o.bbox[0] for o in blk) - min(o.bbox[0] for o in blk) > 2 * tol
        if mid or flush_right:
            bad["center" if mid else "right"].append(li)
    out = []
    for how, lis in bad.items():
        idx = [doc.lines[li].first_word for li in lis]
        out.append(_spec(fmt["label"], "layout", t.get("layout_severity", "warning"),
                         f"Text alignment ({len(lis)} line(s), e.g. “{doc.lines[lis[0]].text.strip()[:60]}”)",
                         [Loc(doc.lines[li].page, doc.lines[li].bbox) for li in lis[:u.cfg['report']['max_locs']]],
                         "Content Alignment: all content left-aligned (right-to-left languages excepted)", "spec text-align",
                         figma="left-aligned", stage="centred" if how == "center" else "right-aligned",
                         props=[{"property": "text-align", "baseline": "Figma left", "candidate": how}]))
    return out


def check(u: Unit, fmt: dict, role: dict[int, str]) -> list[Finding]:
    """The per-section component checks (the document-wide ones run in document())."""
    return (_callout_findings(u, fmt) + _table_findings(u, fmt) + _list_findings(u, fmt)
            + _heading_findings(u, fmt) + _align_findings(u, fmt, role))
