"""Design-spec check: the stage PDF against the manual's typography and page layout
(config/typography.toml, taken from Figma), not against prod.

Every stage word gets the spec role of what it is: its section heading level (h1-h3),
the first row of a table (table header) or another row (table default), small text
right below a table (table note), a link (body hyperlink), bold text (body strong) or
body text (body default), a callout's title (callout title). Its font family, weight and size,
and the line height of its paragraph, are compared to that role's style in the format the stage
page size matches (A4 = PDF View, A5 = PDF Print); colours marked "accent" are the theme's, read
from the cover page (components.theme). Links are underlined. Findings are grouped per section by
role and mismatch. The components (cover, page numbers, callouts, tables, lists, headings,
alignment) are checked in components.py.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from functools import lru_cache

import pymupdf

from ..model import Doc, Finding, Loc
from . import Unit, components, locs, snippet
from . import tables as tables_mod
from .style import color_distance


def themed(styles: dict, th: dict) -> dict:
    """The styles with "accent" colours resolved to the theme's."""
    return {k: {**v, "color": components.color_of(v.get("color"), th)} for k, v in styles.items()}


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
    """Per page, the areas of the links. A link that wraps onto the next line has one rectangle around both
    lines (the whole sentence) but a quad per line around its own words: the quads, when the PDF has them."""
    out: dict[int, list[tuple]] = defaultdict(list)
    with pymupdf.open(path) as d:
        for p in d:
            for l in p.get_links():
                if l.get("kind") not in (pymupdf.LINK_URI, pymupdf.LINK_GOTO, pymupdf.LINK_NAMED, pymupdf.LINK_GOTOR):
                    continue
                quads = []
                try:
                    typ, val = d.xref_get_key(l["xref"], "QuadPoints") if l.get("xref") else ("null", "")
                    nums = [float(v) for v in val.strip("[]").split()] if typ == "array" else []
                    for k in range(0, len(nums) - 7, 8):
                        xs, ys = nums[k:k + 8:2], nums[k + 1:k + 8:2]
                        r = pymupdf.Rect(min(xs), min(ys), max(xs), max(ys)) * p.transformation_matrix
                        r.normalize()
                        if r.intersects(l["from"]):
                            quads.append(tuple(r))
                except Exception:
                    quads = []
                out[p.number] += quads or [tuple(l["from"])]
    return dict(out)


def _center_in(b: tuple, r: tuple) -> bool:
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    return r[0] - 1 <= cx <= r[2] + 1 and r[1] - 1 <= cy <= r[3] + 1


def _link_word(b: tuple, r: tuple) -> bool:
    """The word is (mostly) link text: its middle in the link area and its start too. “(See“Wired” or
    “seeCustomizing” - running text glued to the link - starts before the link and takes its colour."""
    return _center_in(b, r) and b[0] >= r[0] - 2


_NON_LATIN = re.compile(r"[\u0590-\u08ff\u0e00-\u0fff\u1100-\u11ff\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff\uff00-\uffef]")


def _latin(text: str) -> bool:
    """Latin letters or digits and no CJK / Thai / Arabic ...: text the spec's fonts are meant for.
    Arrows, symbols and other scripts come from fallback fonts (NotoSansTC, GillSans) by design."""
    return bool(re.search(r"[A-Za-z0-9]", text)) and not _NON_LATIN.search(text)


def _callout_label(w) -> bool:
    return w.norm.startswith("<label:") or bool(tables_mod._CALLOUT_LABEL.match(w.text.strip()))


def _light(color: str) -> bool:
    return len(color) == 7 and sum(int(color[k:k + 2], 16) for k in (1, 3, 5)) > 600


def _in_pictures(doc: Doc, page: int, cache: dict) -> list[pymupdf.Rect]:
    """Areas of pictures and vector illustrations: text there is a figure label or screenshot
    text, which the spec's text styles do not cover."""
    if page not in cache:
        cache[page] = [pymupdf.Rect(im.bbox) for im in doc.images
                       if im.page == page and im.bbox[2] - im.bbox[0] > 40 and im.bbox[3] - im.bbox[1] > 30]
        cache[page] += [pymupdf.Rect(r) for r in tables_mod._figure_rects(doc, page)]
    return cache[page]


def _roles(u: Unit, t: dict, styles: dict, fonts: dict) -> dict[int, str]:
    """The spec role of every stage word of the section ("" = not covered by the spec: figure labels,
    symbols). Tables are data tables only (not note boxes or picture grids); white text sits on a
    header bar; a heading in the branding font that the TOC does not list still is a heading."""
    doc, rng = u.b, u.b_range
    role: dict[int, str] = {}
    note_size = (styles.get("table_note") or {}).get("size")
    links = _link_rects(doc.path)
    for tb in tables_mod.tables(doc, rng):
        if not tables_mod.is_data_table(doc, tb):
            continue
        # the first row is a header only when it looks like one: white on a header bar, or bolder than
        # the rows below (a specification table often has no header row)
        weight = lambda rows: Counter(doc.words[i].style.weight for r in rows for i in r.idx).most_common(1)[0][0] \
            if any(r.idx for r in rows) else 400
        first = tb.rows[0]
        header = bool(first.idx) and (sum(_light(doc.words[i].style.color) for i in first.idx) * 2 >= len(first.idx)
                                      or (len(tb.rows) > 1 and weight([first]) > weight(tb.rows[1:])))
        for k, r in enumerate(tb.rows):
            for i in r.idx:
                w = doc.words[i]
                if _callout_label(w):
                    role[i] = ""
                elif any(_link_word(w.bbox, lr) for lr in links.get(w.page, ())):
                    role[i] = "body_hyperlink"  # a link in a table cell is a link
                elif (k == 0 and header) or _light(w.style.color):
                    role[i] = "table_header"
                else:
                    role[i] = "table_default"
        if note_size:  # small text right below the table is its note
            bottom = tb.bbox[3]
            for i in range(*rng):
                w = doc.words[i]
                if (w.page == tb.page and i not in role and 0 <= w.bbox[1] - bottom <= t.get("table_note_gap", 40)
                        and w.style.size <= note_size + 0.5):
                    role[i] = "table_note"
    titles = components.callout_title_words(doc, u.cfg) if "callout_title" in styles else set()
    caption_lines: set[int] = set()
    if "caption" in styles:
        from . import caption_rows
        lines_by_li: dict[int, list[int]] = defaultdict(list)
        for i in range(*rng):
            lines_by_li[doc.words[i].line].append(i)
        for page in sorted({doc.words[i].page for i in range(*rng)}):
            pics = caption_rows._things(doc, page)
            caption_lines.update(caption_rows._captions(doc, page, lines_by_li, pics, 12, 24))
    # a link area often spans the whole sentence ("See Notes on HDMI for details."): when its words are in two
    # colours, only the ones not in the running text's colour are the link
    plain_in_link: set[int] = set()
    by_rect: dict[tuple, list[int]] = defaultdict(list)
    for i in range(*rng):
        w = doc.words[i]
        for lr in links.get(w.page, ()):
            if _center_in(w.bbox, lr):
                by_rect[(w.page, lr)].append(i)
                break
    for idx in by_rect.values():
        cols = Counter(doc.words[i].style.color for i in idx)
        if len(cols) > 1:
            body = Counter(doc.words[i].style.color for i in range(*rng) if doc.words[i].role == "body").most_common(1)
            if body and body[0][0] in cols:
                plain_in_link.update(i for i in idx if doc.words[i].style.color == body[0][0])
    pics: dict[int, list] = {}
    brand = fonts.get("branding", "").lower().replace(" ", "")
    heads = sorted(((k, styles[k]["size"]) for k in ("h1", "h2", "h3") if k in styles), key=lambda kv: -kv[1])
    for i in range(*rng):
        if i in role:
            continue
        w = doc.words[i]
        cx, cy = (w.bbox[0] + w.bbox[2]) / 2, (w.bbox[1] + w.bbox[3]) / 2
        if w.role.startswith("h") and w.role[1:].isdigit():
            role[i] = f"h{min(int(w.role[1:]), 3)}"
        elif i in titles:
            role[i] = "callout_title"  # "NOTE" / "TIP": the Callout Component's title
        elif _callout_label(w):
            role[i] = ""  # "Note:" outside a callout box: checked as a callout (components.py), not as text
        elif w.line in caption_lines:
            role[i] = "caption"  # a short line right under a picture: the Picture Component's caption
        elif any(r.contains(pymupdf.Point(cx, cy)) for r in _in_pictures(doc, w.page, pics)):
            role[i] = ""  # a label on a picture / screenshot text
        elif i not in plain_in_link and any(_link_word(w.bbox, r) for r in links.get(w.page, ())):
            role[i] = "body_hyperlink"
        elif _light(w.style.color):
            role[i] = "table_header"  # white text on a dark header bar
        elif brand and w.style.family.lower().replace(" ", "").startswith(brand) and heads \
                and w.style.size >= heads[-1][1] - 0.5:
            role[i] = min(heads, key=lambda kv: abs(kv[1] - w.style.size))[0]  # a heading the TOC does not list
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
    doc = u.b
    th = components.theme(doc, u.cfg)
    styles, fonts, weights = themed(f["styles"], th), t.get("fonts", {}), t.get("weights", {})
    size_tol, lh_tol = t.get("size_tolerance", 0.25), t.get("line_height_tolerance", 1.0)
    color_tol = t.get("color_tolerance", 24)
    role = _roles(u, t, styles, fonts)
    # what stage actually uses per style, over the whole document (for the report's Figma table)
    seen = u.cfg.setdefault("_typo_actual", {})
    for i, r in role.items():
        w = doc.words[i]
        if r and w.norm and _latin(w.text):
            st = w.style
            seen.setdefault(r, Counter())[(st.family, st.weight, st.size, st.color.upper())] += 1
    groups: dict[tuple, list[int]] = defaultdict(list)
    for i, r in role.items():
        spec, w = styles.get(r), doc.words[i]
        if not spec or not w.norm:
            continue
        fam = fonts.get(spec["font"], spec["font"])
        latin = _latin(w.text)
        if latin and not w.style.family.lower().replace(" ", "").startswith(fam.lower().replace(" ", "")):
            groups[(r, "font-family", fam, w.style.family)].append(i)
        if spec.get("color") and color_distance(w.style.color, spec["color"].lower()) > color_tol:
            groups[(r, "color", spec["color"].upper(), w.style.color.upper())].append(i)
        if not latin:
            continue  # symbols / other scripts: their weight and size follow the fallback font
        want_w = weights.get(spec["weight"], 400)
        inline_bold = w.style.weight >= 600 and r in ("body_default", "table_default", "table_note", "body_hyperlink")
        if w.style.weight != want_w and not inline_bold:  # bold is an inline style of any text (spec: Inline Elements)
            got_w = {v: k for k, v in weights.items()}.get(w.style.weight)
            groups[(r, "font-weight", f"{spec['weight']} ({want_w})",
                    f"{got_w} ({w.style.weight})" if got_w else str(w.style.weight))].append(i)
        if abs(w.style.size - spec["size"]) > size_tol:
            groups[(r, "font-size", f"{spec['size']:g}pt", f"{w.style.size:g}pt")].append(i)
        if spec.get("underline") and not _underlined(doc, i):
            groups[(r, "text-decoration", "underline", "none")].append(i)
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
        u.cfg.setdefault("_typo_lh", {}).setdefault(r, Counter())[round(got * 2) / 2] += 1  # for the report's table
        if abs(got - spec["line_height"]) > lh_tol:
            groups[(r, "line-height", f"{spec['line_height']:g}pt", f"{round(got * 2) / 2:g}pt")].append(ib)

    findings, max_locs = [], u.cfg["report"]["max_locs"]
    theme_note = f" ({th['label']} theme)" if th.get("label") else ""
    for (r, prop, want, got), idx in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        name = _role_name(r)
        accent = f["styles"][r].get("color") == "accent" and prop == "color"
        expected = _describe(f["label"], name + (theme_note if accent else ""), styles[r], fonts, weights)
        actual = _actual(doc, idx, weights)
        w_, g_ = ("underlined", "not underlined") if prop == "text-decoration" else (want, got)
        figma = _style_line(styles[r], fonts) + (f" ({th['label']} theme accent)" if accent else "")
        stage = actual + (f", line height {got}" if prop == "line-height" else "") \
            + (", not underlined" if prop == "text-decoration" else "")
        findings.append(Finding(
            "style", t.get("severity", "warning"),
            components.spec_message(f"{name} — {components._PROP.get(prop, prop)}: Figma {w_}, stage {g_} "
                                    f"({len(idx)} word{'s' if len(idx) != 1 else ''}, e.g. “{snippet(doc, idx, 8)}”)", figma, stage),
            [], locs(doc, idx, max_locs),
            {"kind": "spec", "role": r, "format": fkey, "words": len(idx), "figma": figma, "stage": stage,
             "spec": f"Figma {expected}", "stage_style": actual,
             "props": [{"property": prop, "baseline": f"Figma {want}", "candidate": got}]},
            types=[f"spec {prop}"]))
    return findings + components.check(u, f, role)


_UNDER: dict[tuple, list] = {}


def _underlined(doc: Doc, i: int) -> bool:
    """A drawn line under the word (the link underline): thin, horizontal, at the word's baseline."""
    w = doc.words[i]
    key = (doc.path, w.page)
    if key not in _UNDER:
        try:
            pg = components._pdf(doc)[w.page]
            _UNDER[key] = [tuple(d["rect"]) for d in pg.get_drawings() if d["rect"].height <= 2.5 and d["rect"].width >= 2]
        except Exception:
            _UNDER[key] = []
    x0, _, x1, y1 = w.bbox
    return any(w.bbox[3] - 5 <= r[1] <= y1 + 3 and min(x1, r[2]) - max(x0, r[0]) >= 0.5 * (x1 - x0)
               for r in _UNDER[key])


def _role_name(r: str) -> str:
    return {"h1": "Headline 1", "h2": "Headline 2", "h3": "Headline 3"}.get(r, r.replace("_", " ").capitalize())


def _actual(doc: Doc, idx: list[int], weights: dict) -> str:
    """What stage uses for these words: the most common font, weight, size and colour."""
    names = {v: k for k, v in weights.items()}
    (fam, wt, size, col), _ = Counter((doc.words[i].style.family, doc.words[i].style.weight, doc.words[i].style.size,
                                       doc.words[i].style.color.upper()) for i in idx).most_common(1)[0]
    return f"{fam} {names.get(wt, wt)}, {size:g}pt, colour {col}"


def _style_line(st: dict, fonts: dict) -> str:
    """A spec style in a line: "Roboto Regular, 12pt / 16pt line height, colour #000000"."""
    return (f"{fonts.get(st['font'], st['font'])} {st['weight']}, {st['size']:g}pt / {st['line_height']:g}pt line height"
            + (f", colour {st['color'].upper()}" if st.get("color") else "") + (", underlined" if st.get("underline") else ""))


def _describe(label: str, name: str, st: dict, fonts: dict, weights: dict | None = None) -> str:
    return (f"{label} · {name}: {fonts.get(st['font'], st['font'])} {st['weight']}, "
            f"{st['size']:g}pt / {st['line_height']:g}pt line height" + (f", colour {st['color'].upper()}" if st.get("color") else "")
            + (", underlined" if st.get("underline") else ""))


def reference(B: Doc, cfg: dict) -> dict | None:
    """The Figma type scale the stage PDF is checked against (for the report): format, page and every style."""
    t = cfg.get("typography") or {}
    if not t.get("enabled", True) or B.raw_tables is not None or not B.pages:
        return None
    fmt = spec_format(B, cfg)
    if not fmt:
        return None
    _, f = fmt
    fonts = t.get("fonts", {})
    m = f.get("margins") or {}
    th = components.theme(B, cfg)
    return {"format": f["label"], "page": f.get("page"), "margins": m,
            "theme": {"name": th.get("label"), "accent": (th.get("accent") or "").upper(), "cover": th.get("cover"),
                      "cover_color": (th.get("color") or "").upper(), "matched": th.get("matched")},
            "styles": [{"style": _role_name(k), "font": fonts.get(st["font"], st["font"]), "weight": st["weight"],
                        "size": st["size"], "line_height": st["line_height"], "color": (st.get("color") or "").upper(),
                        "underline": bool(st.get("underline")), "actual": _stage_actual(cfg, k, t.get("weights", {}))}
                       for k, st in themed(f["styles"], th).items()]}


def _stage_actual(cfg: dict, role: str, weights: dict) -> dict | None:
    """The font, weight, size, line height and colour stage uses most for this style, and how many words use it
    (line height: None when the style has no wrapped paragraph lines in stage to measure)."""
    c = (cfg.get("_typo_actual") or {}).get(role)
    if not c:
        return None
    (fam, wt, size, col), n = c.most_common(1)[0]
    names = {v: k for k, v in weights.items()}
    lh = (cfg.get("_typo_lh") or {}).get(role)  # line height: the most common between wrapped lines of the style
    return {"font": fam, "weight": names.get(wt, str(wt)), "size": size, "color": col,
            "line_height": lh.most_common(1)[0][0] if lh else None,
            "share": round(n / sum(c.values()), 2), "words": sum(c.values())}


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
        return [Finding("layout", sev, components.spec_message(
                            "Page size — no spec format matches (typography not checked against the spec)",
                            f"a spec page size: {sizes}", f"{B.pages[0].width:.0f}×{B.pages[0].height:.0f}pt"),
                        detail={"kind": "spec", "property": "spec page size", "figma": sizes,
                                "stage": f"{B.pages[0].width:.0f}×{B.pages[0].height:.0f}pt"}, types=["spec page size"])]
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
            components.spec_message(f"Text in the {side} page margin — {len(pages)} page(s) (e.g. “{snippet(B, idx[:8])}”)",
                                    f"{side} margin {m.get(side, 0):g}pt kept free of text",
                                    f"text in the {side} margin on p.{', '.join(map(str, pages[:30]))}" + (" …" if len(pages) > 30 else "")),
            [], [Loc(B.words[i].page, B.words[i].bbox) for i in idx[:cfg['report']['max_locs']]],
            {"kind": "spec", "property": "spec page margin", "side": side, "pages": pages, "format": fkey,
             "spec": f"{f['label']}: page {f['page'][0]:g}×{f['page'][1]:g}pt, margins top {m.get('top', 0):g} · "
                     f"bottom {m.get('bottom', 0):g} · left {m.get('left', 0):g} · right {m.get('right', 0):g}pt"},
            types=["spec page margin"]))
    return out + components.document(B, cfg, f)
