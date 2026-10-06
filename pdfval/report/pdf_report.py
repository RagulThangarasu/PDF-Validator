"""Printable PDF report: summary, sections table, style map, and every issue
with its prod and stage screenshots side by side (A4 landscape).

The summary pages are an HTML flow (pymupdf.Story); the issue pages are drawn
directly with one shared font, and screenshots are down-sampled to JPEG.
"""
from __future__ import annotations

import io
from collections import Counter
import re
from html import escape
from pathlib import Path
from typing import Callable

import pymupdf
from PIL import Image

from ..genuine import concise

SEV_COLOR = {"error": "#d92d20", "warning": "#b45309", "info": "#64748b"}
# the severity as the reports print it: the word "error" is not used anywhere in a report
SEV_LABEL = {"error": "HIGH", "warning": "WARNING", "info": "INFO"}
STATUS_COLOR = {"fail": "#d92d20", "warn": "#b45309", "pass": "#16a34a"}
CHECK_COLOR = {"toc": "#9333ea", "structure": "#2563eb", "content": "#dc2626", "tables": "#0284c7", "assets": "#0d9488",
               "integrity": "#b91c1c", "style": "#7c3aed", "layout": "#ea580c"}
PCT_COLOR = {"pass": "#16a34a", "warn": "#b45309", "fail": "#d92d20"}
CATS = [("content", "Content"), ("images", "Images"), ("tables", "Tables"), ("toc", "TOC"), ("structure", "Structure"),
        ("links", "Links & rendering"), ("css", "CSS / layout")]
CAT_LABEL = dict(CATS)
CAT_COLOR = {"toc": "#9333ea", "content": "#dc2626", "images": "#0d9488", "tables": "#0284c7", "structure": "#2563eb",
             "links": "#b91c1c", "css": "#7c3aed"}
CAT_ORDER = {c: k for k, (c, _) in enumerate(CATS)}
# the report lists the issues in this order, each group on its own pages
GROUPS = [("content", "Content", "text: missing, extra, changed words, case, punctuation, spacing"),
          ("links", "Links", "links, attachments, broken characters, text off the page"),
          ("formatting", "Formatting", "sections and headings, tables, images, TOC, layout, bullets and lists"),
          ("css", "CSS", "fonts, sizes, weights and colours")]
GROUP_ORDER = {g: k for k, (g, _, _) in enumerate(GROUPS)}


def group_of(f: dict) -> str:
    """Report group of a finding: content, links, formatting or css (style only; layout is formatting)."""
    if f.get("check") == "style":
        return "css"
    cat = f.get("category")
    return cat if cat in ("content", "links") else "formatting"
CSS = """
* { font-family: sans-serif; font-size: 9px; color: #1d2330; }
h1 { font-size: 20px; margin: 0 0 2px 0; } h2 { font-size: 13px; margin: 14px 0 2px 0; }
table.grid { border-collapse: collapse; width: 100%; }
table.grid th, table.grid td { border-bottom: 0.5px solid #d0d4da; padding: 3px 4px; text-align: left; font-size: 8.5px; }
table.grid th { background-color: #f1f3f6; }
.n { text-align: right; }
.muted { color: #6a7282; }
.cap { font-size: 7.5px; color: #6a7282; font-weight: bold; }
"""


def _pct_status(c: dict) -> str:
    return "pass" if c["match_pct"] >= c["pass_pct"] else "warn" if c["match_pct"] >= c["warn_pct"] else "fail"


INCLUDE_ALL = {"summary": True, "genuine": True, "critical": True, "coverage": False, "truncated": True,
               "sections": True, "toc": True, "toc_compare": True, "stylemap": True, "issues": True, "screenshots": True}

# picture issues the PDF report itself shows (with prod / stage screenshots): size and resolution
PDF_IMAGE_TYPES = {"image smaller", "image bigger", "size / aspect", "image pixelated", "image vertical alignment",
                   # every other issue of the picture itself (the image report shows a picture's own text and marks:
                   # numbers, labels, leader lines, red overlay, artwork missing): reported here, or it is in no report
                   "image changed", "duplicate image", "image distorted", "image alignment", "image in wrong section",
                   "image mirrored", "extra image", "image order", "placement", "image outside box", "image blurred",
                   "icon differs", "icon pixelated", "icon missing inline"}  # an icon in a sentence: another one, coarse, or gone

# the genuine-issues report: the metrics at the top, then every genuine issue with its screenshots -
# no overview tables (the issue pages say what each issue is)
GENUINE = {"include": {"summary": True, "categories": False, "genuine": "counts", "css": False, "toc_compare": True,
                       "critical": False, "coverage": False, "truncated": True, "sections": False,
                       "toc": False, "stylemap": False, "issues": True, "screenshots": True},
           # image issues are in image-issues.pdf only - except a picture's size (smaller / bigger in stage) and a
           # pixelated picture: the image report shows picture text and overlays, so these are reported here
           "filter": {"genuine_only": True, "no_images": True, "keep_types": sorted(PDF_IMAGE_TYPES)}}

# the CSS report: every CSS / typography / layout issue that is not in the PDF report (fonts, sizes, colours,
# line heights, spec styles), with its screenshots
CSS_REPORT = {"include": {**GENUINE["include"], "genuine": False, "css": True, "coverage": False, "truncated": False},
              "filter": {"categories": ["css"], "non_genuine": True}}

# the image report: every picture issue (missing / changed / size / pixelated / blurred / alignment / order,
# and picture labels missing in stage or drawn into the stage picture), whether in the PDF report or not
IMAGE_TYPES = {"missing image label", "label in picture", "text in image"}
IMAGE_REPORT = {"include": {**GENUINE["include"], "genuine": False, "coverage": False, "truncated": False},
                "filter": {"image_issues": True}}

# what image-issues.pdf shows, per publication: every picture issue the reports keep ([assets] report_types) and
# a label of the picture missing in stage - with or without the label settings of the other reports
IMAGE_ISSUE_TYPES = {"missing image", "broken image", "image changed", "image blacked out", "image pixelated",
                     "image distorted", "duplicate image", "image alignment", "image in wrong section", "image combined",
                     "missing image label", "image smaller", "image bigger", "image mirrored",
                     # a picture's own text and marks, compared picture by picture (picture_labels.py, assets.py):
                     # callout numbers, labels, leader lines and the red highlight overlay missing / added in stage
                     "callout numbers missing", "image label missing", "leader lines missing", "image marks"}


# what the image report shows: picture size (incl. much smaller in stage), alignment, a picture over its box
# (overlay), missing / different artwork (icons included) and a picture under another section - not text
# (missing text, blurred text inside a picture), picture labels / captions or pictures combined into one
IMAGE_REPORT_TYPES = {"size / aspect", "image smaller", "image bigger", "image mirrored", "image alignment", "placement", "image outside box",
                      "missing image", "image changed", "broken image", "image blacked out", "image distorted",
                      "image in wrong section"}


# everything about a picture: kept out of the PDF report (it goes to image-issues.pdf)
IMAGE_ANY_TYPES = IMAGE_REPORT_TYPES | IMAGE_ISSUE_TYPES | {
    "image combined", "image pixelated", "image blurred", "duplicate image", "raster vs vector", "extra image",
    "image order", "missing image label", "label in picture", "text in image", "text as graphic", "image outside box"}
_PICS: dict[tuple, list] = {}


def _pictures_on(path: str, page: int) -> list:
    """Embedded images and drawn illustrations (not tables) of a page, as boxes."""
    key = (path, page)
    if key not in _PICS:
        out = []
        try:
            pg = pymupdf.open(path)[page]
            out = [tuple(i["bbox"]) for i in pg.get_image_info() if i["bbox"][2] - i["bbox"][0] >= 30]
            try:
                tabs = [tuple(t.bbox) for t in pg.find_tables().tables]
            except Exception:
                tabs = []
            paths = pg.get_drawings()
            words = pg.get_text("words")
            for r in pg.cluster_drawings(drawings=paths):
                if r.width < 40 or r.height < 40 or r.height >= 0.8 * pg.rect.height or \
                        any(abs(r.x0 - t[0]) < 6 and abs(r.y0 - t[1]) < 6 for t in tabs):
                    continue
                # an illustration: curves in it and little text (a note box or table frame has neither)
                curves = sum(1 for d in paths if r.contains(d["rect"]) for it in d["items"] if it[0] in ("c", "qu"))
                inside = sum(1 for w in words if r.x0 <= (w[0] + w[2]) / 2 <= r.x1 and r.y0 <= (w[1] + w[3]) / 2 <= r.y1)
                if curves >= 8 and inside <= 25:
                    out.append(tuple(r))
        except Exception:
            pass
        _PICS[key] = out
    return _PICS[key]


def text_on_picture(result: dict, f: dict) -> bool:
    """A content issue ("Data missing", changed text) whose prod words all sit on a picture: the picture's
    labels / callout numbers, an image matter for image-issues.pdf, not a content issue of the PDF report."""
    if f.get("check") != "content" or not set(f.get("types") or []) & {"missing text", "changed text", "extra text"}:
        return False
    side, locs = ("baseline", f.get("baseline")) if f.get("baseline") else ("candidate", f.get("candidate"))
    path = (result.get("meta", {}).get(side) or {}).get("path")
    if not locs or not path:
        return False
    def inside(l):
        b = l["bbox"]
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        return any(p[0] - 4 <= cx <= p[2] + 4 and p[1] - 4 <= cy <= p[3] + 4 for p in _pictures_on(path, l["page"]))
    return all(inside(l) for l in locs)


def image_related(result: dict, f: dict) -> bool:
    return f.get("category") == "images" or f.get("check") == "assets" \
        or bool(set(f.get("types") or []) & IMAGE_ANY_TYPES) or text_on_picture(result, f)


def is_image_issue(f: dict) -> bool:
    return bool(set(f.get("types") or []) & IMAGE_REPORT_TYPES)


def _actual_cell(st: dict) -> str:
    """Stage's most used style for a Figma style; differing values in red."""
    a = st.get("actual")
    if not a:
        return "<span class='muted'>not used in stage</span>"
    red = lambda ok, v: v if ok else f"<b style='color:#dc2626'>{v}</b>"
    parts = [red(a["font"].lower().startswith(st["font"].lower()), escape(a["font"])),
             red(a["weight"] == st["weight"], escape(a["weight"])),
             red(abs(a["size"] - st["size"]) <= 0.25, f"{a['size']:g} pt"),
             (red(abs(a["line_height"] - st["line_height"]) <= 1.0, f"{a['line_height']:g} pt line height")
              if a.get("line_height") is not None else "<span class='muted'>line height —</span>"),
             f"<span style='background-color:{a['color']};border:0.5px solid #9aa1ad'>&#160;&#160;&#160;</span> "
             + red(a["color"] == (st["color"] or a["color"]), escape(a["color"]))]
    return " · ".join(parts) + f" <span class='muted'>({a['share']:.0%} of {a['words']} words)</span>"


def _summary_html(result: dict, include: dict | None = None, note: str = "", n_issues: int | None = None) -> list[str]:
    """HTML blocks for the front pages; each block starts on a new page (MuPDF's Story
    can loop forever when a long table starts part-way down a page after another table).
    include: which parts to render (INCLUDE_ALL keys); note: the filters applied."""
    inc = {**INCLUDE_ALL, **(include or {})}
    sm, meta = result["summary"], result["meta"]
    doc_row = lambda label, m: (f"<tr><th>{label}</th><td>{escape(m['url'])} (web page, rendered and read from its DOM)</td></tr>"
                                if m.get("url") else
                                f"<tr><th>{label}</th><td>{escape(Path(m['path']).name)} · {m['pages']} pages · "
                                f"{m['page_size'][0]}×{m['page_size'][1]} pt · body {m['body_size']}pt</td></tr>")
    html = [
        f"<h1>PDF Parity Report — <span style='color:{STATUS_COLOR.get(sm['result'])}'>{sm['result'].upper()}</span></h1>",
        f"<p class='muted'>Generated {escape(meta['generated'])} · prod is the reference baseline</p>",
        "<table class='grid'>",
        (f"<tr><th>Product</th><td><b>{escape(meta['name'])}</b></td></tr>" if meta.get("name") else ""),
        doc_row("Prod (baseline)", meta["baseline"]), doc_row("Web page (candidate)" if meta.get("mode") == "html" else "Stage (candidate)", meta["candidate"]),
        f"<tr><th>Result</th><td><b style='color:{STATUS_COLOR.get(sm['result'])}'>{sm['result'].upper()}</b> — "
        f"sections: <b style='color:#d92d20'>{sm['fail']} fail</b>, <b style='color:#b45309'>{sm['warn']} warn</b>, "
        f"<b style='color:#16a34a'>{sm['pass']} pass</b></td></tr>",
        f"<tr><th>Score</th><td><b style='color:{PCT_COLOR[_pct_status(sm['content'])]};font-size:11px'>"
        f"{sm['content']['match_pct']:.2f}%</b> = 100% − {sm['content'].get('issue_weight_pct', 0.1):g}% × "
        f"{sm['content'].get('counted_issues', sm['content'].get('issues', 0))} issues in sections that do not pass "
        f"({sm['content'].get('issues', 0)} issues in this report; a passing section counts as 100%) · "
        f"{sm['content']['missing_words']} missing · {sm['content']['extra_words']} extra · "
        f"{sm['content']['spacing_issues']} spacing · pass ≥ {sm['content']['pass_pct']}%, warn ≥ {sm['content']['warn_pct']}% · "
        f"sections {sm['content']['pass']} pass / {sm['content']['warn']} warn / {sm['content']['fail']} fail</td></tr>",
        f"<tr><th>Critical / breaking</th><td><b style='color:{'#b42318' if sm['critical']['total'] else '#16a34a'}'>"
        f"{sm['critical']['total']}</b> " + (" · ".join(f"{v} × {escape(k)}" for k, v in sm["critical"]["by_kind"].items())
                                             or "— no missing sections, rows, images, files, links or glyphs") + "</td></tr>",
        _missing_sections_row(result),
        (f"<tr><th>CSS / layout</th><td>{sm['css']['issues']} issues (style {sm['css']['style']}, layout {sm['css']['layout']}) "
         "— reported separately, not part of the content %</td></tr>" if inc.get("css", True) else ""),
        "<tr><th>All findings</th><td>" + " · ".join(f"<b style='color:{CHECK_COLOR.get(k, '#475569')}'>{k}</b> {v}"
                                                    for k, v in sm["by_check"].items()
                                                    if inc.get("css", True) or k not in ("style", "layout")) + "</td></tr>"
        + (f"<tr><th>This report</th><td>{escape(note)}</td></tr>" if note else "") + "</table>",
    ]
    if not inc["summary"]:  # keep only the title, the documents and the filter note
        html = html[:3] + [doc_row("Prod (baseline)", meta["baseline"]),
                           doc_row("Web page (candidate)" if meta.get("mode") == "html" else "Stage (candidate)", meta["candidate"]),
                           (f"<tr><th>This report</th><td>{escape(note)}</td></tr>" if note else "") + "</table>"]
    elif inc.get("categories", True):
        html.append("<h2>Issues by category and type</h2><table class='grid'><tr><th>Category</th><th>Total</th><th>Types</th></tr>")
        # CATS first (known order/colour), then any category the run produced that CATS doesn't know about yet,
        # so a new check's category is still shown instead of being silently left out of this table
        extra = [(c, c.title()) for c in sm["by_category"] if c not in CAT_LABEL]
        for c, label in [*CATS, *extra]:
            bc = sm["by_category"][c]
            html.append(f"<tr><td><b style='color:{CAT_COLOR.get(c, '#475569')}'>{label}</b></td><td class='n'>{bc['total']}</td><td>"
                        + (" · ".join(f"{escape(t)} {n}" for t, n in bc["types"].items()) or "—") + "</td></tr>")
        html.append("</table>")
    typo = meta.get("typography")
    if inc["summary"] and typo and typo.get("styles"):  # the Figma type scale the typography issues refer to
        m = typo.get("margins") or {}
        th = typo.get("theme") or {}
        theme = (f" Theme <b>{escape(th['name'])}</b> (accent {escape(th.get('accent') or '')}), "
                 + (f"read from the cover ({escape(th.get('cover_color') or '')})." if th.get("cover") else "no cover page: the default theme.")
                 if th.get("name") else "")
        html.append(f"<h2>Design spec (Figma) — {escape(typo['format'])}: expected headings and text</h2>"
                    f"<p class='muted'>Stage text is checked against these styles; each typography issue names the style it "
                    f"expected.{theme} Page {typo['page'][0]:g} × {typo['page'][1]:g} pt, margins top {m.get('top', 0):g} · bottom "
                    f"{m.get('bottom', 0):g} · left {m.get('left', 0):g} · right {m.get('right', 0):g} pt.</p>"
                    "<table class='grid'><tr><th>Style</th><th>Font</th><th>Weight</th><th class='n'>Size</th>"
                    "<th class='n'>Line height</th><th>Colour</th><th>Stage (actual): font · weight · size · line height · colour</th></tr>"
                    + "".join(f"<tr><td><b>{escape(st['style'])}</b></td><td>{escape(st['font'])}</td><td>{escape(st['weight'])}"
                              f"{', underlined' if st.get('underline') else ''}</td><td class='n'>{st['size']:g} pt</td>"
                              f"<td class='n'>{st['line_height']:g} pt</td><td><span style='background-color:{st['color'] or '#000'};border:0.5px solid #9aa1ad'>&#160;&#160;&#160;</span> "
                              f"{escape(st['color'])}</td><td>{_actual_cell(st)}</td></tr>" for st in typo["styles"]) + "</table>")
    gen = [(s, f) for s in result["sections"] for f in s["findings"] if f.get("genuine")]
    if inc["genuine"] == "counts":
        pass  # the issues follow with screenshots: no count overview on the first page
    elif inc["genuine"]:
        html.append(genuine_html(gen))
    crit = [(s, f) for s in result["sections"] for f in s["findings"] if f.get("critical")]
    if crit and inc["critical"]:
        html.append("<h2 style='color:#b42318'>Critical issues</h2><table class='grid'><tr><th>#</th><th>Section</th>"
                    "<th>Check</th><th>Issue</th><th>Prod p.</th><th>Stage p.</th></tr>")
        for s, f in crit:
            html.append(f"<tr><td>{f['id']}</td><td>{escape(s['title'])}</td><td>{CAT_LABEL.get(f['category'], f['category'].title())}</td><td>{escape(f['message']).replace(chr(10), '<br/>')}</td>"
                        f"<td>{f['baseline'][0]['page'] + 1 if f['baseline'] else '—'}</td>"
                        f"<td>{f['candidate'][0]['page'] + 1 if f['candidate'] else '—'}</td></tr>")
        html.append("</table>")
    if inc.get("coverage", True):
        html.append(coverage_html(result))
    if inc.get("truncated", True):
        html.append(truncated_html(result))
    if inc["sections"]:
        html.append("\f")  # block break
    # known category columns first, then any category these sections actually use that CATS doesn't name yet
    sec_cats = [*CATS, *[(c, c.title()) for c in
                         dict.fromkeys(c for s in result["sections"] for c in s.get("categories", {})) if c not in CAT_LABEL]]
    html += [] if not inc["sections"] else [
        "<h2>Sections</h2><table class='grid'><tr><th>#</th><th>Section</th><th>Prod p.</th><th>Stage p.</th>"
        "<th>Status</th><th>Content %</th><th>Missing / extra</th><th>Critical</th>"
        + "".join(f"<th>{label}</th>" for _, label in sec_cats) + "</tr>",
    ]
    for n, s in enumerate(result["sections"] if inc["sections"] else [], 1):
        status_cell = "" if s["status"] == "pass" else f"<b style='color:{STATUS_COLOR[s['status']]}'>{s['status']}</b>"
        html.append(
            f"<tr><td>{n}</td><td>{escape(s['title'])}</td><td>{s['baseline']['start']['page'] + 1}</td>"
            f"<td>{s['candidate']['start']['page'] + 1}</td>"
            f"<td>{status_cell}</td>"
            f"<td class='n'><b style='color:{PCT_COLOR[s['content']['status']]}'>{s['content']['match_pct']:.2f}%</b></td>"
            f"<td class='n'>{s['content']['missing_words']} / {s['content']['extra_words']}</td>"
            f"<td class='n'>{'<b style=\'color:#b42318\'>' + str(s['critical']) + '</b>' if s['critical'] else ''}</td>"
            + "".join(f"<td class='n'>{s.get('categories', {}).get(c, 0) or ''}</td>" for c, _ in sec_cats) + "</tr>")
    if inc["sections"]:
        html.append("</table>")
    t = result.get("toc")
    if t and t["rows"] and inc["toc"]:
        html.append("\f")
        ts = t["summary"]
        tone = {"match": "#16a34a", "level differs": "#b45309", "title differs": "#b45309",
                "missing in stage": "#d92d20", "extra in stage": "#64748b", "order differs": "#d92d20"}
        html.append(f"<h2>Table of contents: prod vs stage — <span style='color:{STATUS_COLOR.get(ts['status'], '#333')}'>"
                    f"{ts['status'].upper()}</span></h2><p><b>Sequence:</b> "
                    + ("<b style='color:#16a34a'>same order</b>" if ts["sequence_ok"]
                       else f"<b style='color:#d92d20'>{ts['order differs']} entries out of order</b>")
                    + f" · <b>levels matching:</b> {ts['levels_match_pct']}%</p>")
        html.append(f"<p class='muted'>Prod {ts['baseline_entries']} entries "
                    f"({t['levels']['baseline']} levels, pages {', '.join(map(str, t['pages']['baseline']))}) · Stage "
                    f"{ts['candidate_entries']} entries ({t['levels']['candidate']} levels, pages {', '.join(map(str, t['pages']['candidate']))}) · "
                    f"match {ts['match']} · level differs {ts['level differs']} · order differs {ts['order differs']} · "
                    f"title differs {ts['title differs']} · missing {ts['missing in stage']} · extra {ts['extra in stage']} · "
                    f"wrong page no. {ts['wrong page']}</p><table class='grid'><tr><th>Prod #</th><th>Prod</th><th>L</th><th>p.</th>"
                    "<th>Status</th><th>Stage #</th><th>Stage</th><th>L</th><th>p.</th></tr>")
        for k, r in enumerate(t["rows"], 1):
            a, b = r["baseline"], r["candidate"]
            cell = lambda e: (f"<td style='padding-left:{4 + 12 * (e['level'] - 1)}px'>{escape(e['title'])}</td><td>{e['level']}</td>"
                              f"<td>{e['page'] if e['page'] is not None else ''}{' !' if e['page_ok'] is False else ''}</td>") if e else "<td>—</td><td></td><td></td>"
            html.append(f"<tr><td>{r['pos']['baseline'] or '—'}</td>{cell(a)}<td><b style='color:{tone[r['status']]}'>{TOC_LABEL.get(r['status'], r['status'])}</b></td>"
                        f"<td>{r['pos']['candidate'] or '—'}</td>{cell(b)}</tr>")
        html.append("</table>")
    if result["style_map"] and inc["stylemap"]:
        html.append("\f")
        html.append("<h2>Global style map (prod → stage)</h2><p class='muted'>Every font / size / colour mismatch "
                    "across the document, grouped by role — each row is one CSS rule to fix in stage.</p>"
                    "<table class='grid'><tr><th>Role</th><th>Prod</th><th>Stage</th><th>Words</th><th>Sections</th></tr>")
        for m in result["style_map"]:
            html.append(f"<tr><td>{escape(m['role'])}</td><td>{escape(m['baseline'])}</td><td><b>{escape(m['candidate'])}</b>"
                        f"</td><td class='n'>{m['words']}</td><td class='n'>{m['sections']}</td></tr>")
        html.append("</table>")
    return [b for b in "".join(html).split("\f") if b.strip()]


class _Canvas:
    """Direct drawing for the issue pages: one shared Noto font (embedded once,
    subset at the end), manual wrapping and page breaks."""
    PAGE = pymupdf.paper_rect("a4-l")
    M = 36

    def __init__(self, doc: pymupdf.Document):
        self.doc = doc
        # the bundled Noto fonts occasionally fail to load under concurrent/parallel runs (a race in
        # mupdf's own font-resource extraction) - "cannot find builtin font ..." - a core PDF font
        # (always available, no CJK glyphs) lets the report still build rather than crash the run
        try:
            self.regular, self.bold = pymupdf.Font("notos"), pymupdf.Font("notosbo")
        except Exception:
            self.regular = self.bold = pymupdf.Font("helv")
        self.page, self.y = None, 0.0
        self.width = self.PAGE.width - 2 * self.M

    def new_page(self):
        self.page = self.doc.new_page(width=self.PAGE.width, height=self.PAGE.height)
        self.y = self.M

    def room(self, h: float) -> bool:
        return self.page is not None and self.y + h <= self.PAGE.height - self.M - 8

    def wrap(self, text: str, size: float, font=None, width: float | None = None) -> list[str]:
        font, width = font or self.regular, width or self.width
        lines, cur = [], ""
        for word in text.split(" "):
            trial = f"{cur} {word}" if cur else word
            if font.text_length(trial, size) <= width or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        return lines + [cur] if cur else lines

    def runs(self, runs: list[tuple], size: float, x: float | None = None):
        """One line of differently coloured runs: [(text, '#hex', bold)] or (text, '#hex', bold, url) for a link."""
        x = self.M if x is None else x
        for text, color, bold, *url in runs:
            font = self.bold if bold else self.regular
            tw = pymupdf.TextWriter(self.PAGE)
            tw.append((x, self.y + size), text, font=font, fontsize=size)
            tw.write_text(self.page, color=_rgb(color))
            w = font.text_length(text, size)
            if url and url[0]:
                r = pymupdf.Rect(x, self.y, x + w, self.y + size * 1.3)
                self.page.draw_line(r.bl + (0, -1), r.br + (0, -1), color=_rgb(color), width=0.5)
                self.page.insert_link({"kind": pymupdf.LINK_URI, "from": r, "uri": url[0]})
            x += w
        self.y += size * 1.35


def _rgb(hex_: str) -> tuple[float, float, float]:
    return tuple(int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5))


def _jpeg_bytes(src: Path, width: int = 1400) -> tuple[bytes, float] | None:
    if not src.exists():
        return None
    img = Image.open(src).convert("RGB")
    if img.width > width:
        img = img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88, optimize=True)
    return buf.getvalue(), img.height / img.width


TOC_COLOR = {"match": "#16a34a", "level differs": "#b45309", "title differs": "#b45309", "order differs": "#d92d20",
             "missing in stage": "#d92d20", "extra in stage": "#2563eb"}
WRONG_PAGE = "#9333ea"
# the TOC status as the reader sees it: an entry in another place in the sequence is 'sequence wrong'
TOC_LABEL = {"order differs": "sequence wrong"}


def _toc_section(c: "_Canvas", t: dict, web: bool, out: Path) -> None:
    """The TOC compared like the UI's TOC tab: the banner, both TOC pages side by side with every entry
    boxed in its status colour, then the entries table (prod entry | status | stage entry)."""
    sm, other = t["summary"], "web page" if web else "stage"
    c.new_page()
    c.runs([("TOC comparison", "#1d2330", True), (f"   prod vs {other}, entry by entry", "#6a7282", False)], 14)
    c.y += 4
    head_b, head_c = t["heading"].get("baseline") or "—", t["heading"].get("candidate") or "—"
    same_head = head_b.strip().lower() == head_c.strip().lower()
    c.runs([("TOC status  ", "#6a7282", True), (sm["status"].upper(), TOC_COLOR.get("match" if sm["status"] == "pass" else
                                                                   "level differs" if sm["status"] == "warn" else "order differs"), True),
            ("     Sequence  ", "#6a7282", True),
            ("✓ same order" if sm["sequence_ok"] else f"✗ {sm['order differs']} out of order", "#16a34a" if sm["sequence_ok"] else "#d92d20", True),
            ("     Levels matching  ", "#6a7282", True), (f"{sm['levels_match_pct']}%", "#1d2330", True),
            ("     Entries  ", "#6a7282", True), (f"{sm['baseline_entries']} prod · {sm['candidate_entries']} {other}", "#1d2330", True)], 9)
    c.runs([("TOC heading  ", "#6a7282", True), (f"“{head_b}” {'=' if same_head else '≠'} “{head_c}”", "#1d2330" if same_head else "#d92d20", True),
            ("     Levels  ", "#6a7282", True), (f"{t['levels']['baseline']} prod · {t['levels']['candidate']} {other}", "#1d2330", True)], 9)
    counts = [(k, sm.get(k, 0)) for k in ("match", "level differs", "title differs", "order differs", "missing in stage", "extra in stage")]
    c.runs([x for k, n in counts for x in ((f"■ ", TOC_COLOR[k], True), (f"{TOC_LABEL.get(k, k).replace('stage', other)} {n}    ", "#1d2330", False))]
           + [("■ ", WRONG_PAGE, True), (f"wrong page no. {sm.get('wrong page', 0)}", "#1d2330", False)], 8)
    c.y += 6

    # --- the TOC pages side by side, every entry boxed in its status colour
    col_w = (c.width - 16) / 2
    pages_b, pages_c = t.get("images", {}).get("baseline", []), t.get("images", {}).get("candidate", [])
    for k in range(max(len(pages_b), len(pages_c))):
        pb = pages_b[k] if k < len(pages_b) else None
        pc = pages_c[k] if k < len(pages_c) else None
        tiles = []
        for side, pg in (("baseline", pb), ("candidate", pc)):
            im = _jpeg_bytes(out / pg["src"], width=1400) if pg else None
            tiles.append((side, pg, im))
        h = max((min(col_w * im[1], c.PAGE.height - 2 * c.M - 40) for _, _, im in tiles if im), default=0)
        if not h:
            continue
        room = c.PAGE.height - c.M - 8 - c.y - 20
        if room < 300:  # too little left on this page: the pair starts a new one
            c.new_page()
            room = c.PAGE.height - c.M - 8 - c.y - 20
        h = min(h, room)  # else shrink the pair to fit under what is already on the page
        for col, (side, pg, im) in enumerate(tiles):
            x0 = c.M + col * (col_w + 16)
            c.page.insert_text((x0, c.y + 8), f"{'PROD' if side == 'baseline' else other.upper()} TOC p.{pg['page'] if pg else '—'}",
                               fontsize=7, color=_rgb("#6a7282"))
            if not im:
                continue
            w = h / im[1]
            r = pymupdf.Rect(x0, c.y + 12, x0 + w, c.y + 12 + h)
            c.page.insert_image(r, stream=im[0])
            c.page.draw_rect(r, color=(0.8, 0.82, 0.85), width=0.5)
            size = t.get("page_size", {}).get(side, {}).get(str(pg["page"]))
            if not size:
                continue
            sx, sy = r.width / size[0], r.height / size[1]
            for row in t["rows"]:
                e = row.get(side)
                if not e or e.get("toc_page") != pg["page"] or not e.get("bbox"):
                    continue
                b = e["bbox"]
                box = pymupdf.Rect(r.x0 + b[0] * sx, r.y0 + b[1] * sy, r.x0 + b[2] * sx, r.y0 + b[3] * sy)
                wrong = any(fl.endswith("page wrong") for fl in row.get("flags", [])) and side == "candidate"
                col_ = _rgb(WRONG_PAGE if wrong else TOC_COLOR.get(row["status"], "#6a7282"))
                c.page.draw_rect(box, color=col_, fill=col_, fill_opacity=0.12, width=0.8)
        c.y += h + 20

    # --- the entries: prod | status | stage
    if not c.room(60):
        c.new_page()
    c.runs([("TOC validation", "#1d2330", True), ("   prod TOC  ·  stage TOC  ·  status", "#6a7282", False)], 10)
    # no table: three plain columns - the prod TOC, the stage TOC (each entry indented by its level) and the status
    side_w = c.width * 0.40
    xs = [c.M, c.M + side_w + 8, c.M + 2 * side_w + 16]
    st_w = c.M + c.width - xs[2]
    for x, label, col in ((xs[0], "Prod TOC", "#2563eb"), (xs[1], "Stage TOC", "#0f766e"), (xs[2], "Status", "#475569")):
        _text(c, x, c.y, label, col, True)
    c.y += 14
    for row in t["rows"]:
        st = TOC_LABEL.get(row["status"], row["status"]) + ("  ·  " + ", ".join(row["flags"]) if row.get("flags") else "")
        colour = TOC_COLOR.get(row["status"], "#6a7282")
        cols = []
        for e in (row.get("baseline"), row.get("candidate")):
            if e:
                ind = 12 * (min(max(int(e["level"]), 1), 3) - 1)
                cols.append((ind, c.wrap(f"{e['title']} (p.{e.get('page') if e.get('page') is not None else '—'})", 8, c.regular, side_w - ind)))
            else:
                cols.append((0, ["—"]))
        status = c.wrap(st, 8, c.bold, st_w)
        h = 11 * max(len(cols[0][1]), len(cols[1][1]), len(status)) + 1
        if not c.room(h + 2):
            c.new_page()
        y = c.y
        for x, (ind, lines) in zip(xs, cols):
            for j, line in enumerate(lines):
                _text(c, x + ind, y + 11 * j, line, "#1d2330" if line != "—" else "#9aa3af", False)
        for j, line in enumerate(status):
            _text(c, xs[2], y + 11 * j, line, colour, True)
        c.y = y + h
    c.y += 6


def _missing_sections_row(result: dict) -> str:
    """The first page names every section of prod that stage does not have: its title and prod page."""
    gone = [f for s in result.get("sections", []) for f in s.get("findings", []) if "missing section" in (f.get("types") or [])]
    if not gone:
        return "<tr><th>Sections missing</th><td><b style='color:#16a34a'>0</b> — every prod section is in stage</td></tr>"
    name = lambda f: (f.get("detail") or {}).get("heading") or f.get("message", "")
    page = lambda f: f"prod p.{f['baseline'][0]['page'] + 1}" if f.get("baseline") else "prod"
    items = " · ".join(f"“{escape(name(f))}” ({page(f)})" for f in gone)
    return (f"<tr><th>Sections missing</th><td><b style='color:#b42318'>{len(gone)}</b> "
            f"<span style='color:#b42318'>missing in stage:</span> {items}</td></tr>")


def _text(c: "_Canvas", x: float, y: float, text: str, color: str, bold: bool) -> None:
    tw = pymupdf.TextWriter(c.PAGE)
    tw.append((x, y + 9), text, font=c.bold if bold else c.regular, fontsize=8)
    tw.write_text(c.page, color=_rgb(color))


def _section_header(c: _Canvas, s: dict, cont: bool = False):
    # a passing section needs no badge here: only WARN / FAIL are worth flagging
    status = [(s["status"].upper(), STATUS_COLOR[s["status"]], True)] if s["status"] != "pass" else []
    c.runs([(s["title"] + (" (cont.)" if cont else "") + "   ", "#1d2330", True), *status], 13)
    c.runs([(f"Prod p.{s['baseline']['start']['page'] + 1}–{s['baseline']['end']['page'] + 1}  ·  "
             f"Stage p.{s['candidate']['start']['page'] + 1}–{s['candidate']['end']['page'] + 1}  ·  "
             f"score {s['content']['match_pct']:.2f}% ({s['content'].get('issues', 0)} issues)  ·  "
             f"CSS issues {s['css']['issues']}" + (f"  ·  {s['critical']} CRITICAL" if s['critical'] else ""),
             "#6a7282", False)], 8.5)
    c.y += 4


def _toc_places(result: dict, rows: list[tuple], entry) -> dict:
    """{finding id: (section title, AEM topic)} for each TOC issue: the entry's heading found in the body of
    the PDF that has it (stage for an extra / changed entry, prod for a missing one - never on the TOC pages
    themselves), the section covering that spot, and on the stage side the AEM topic at that spot."""
    from .. import aem as _aem
    meta = result["meta"]
    docs, anchors = {}, None
    norm = lambda t: re.sub(r"\W+", " ", t or "").strip().lower()
    out = {}
    for s, f in rows:
        t = entry(f)
        side = "baseline" if not f.get("candidate") and f.get("baseline") else "candidate"
        path = meta[side].get("path")
        if not t or not path:
            continue
        try:
            doc = docs.get(path) or docs.setdefault(path, pymupdf.open(path))
        except Exception:
            continue
        toc_pages = {l["page"] for l in (f.get(side) or [])}
        hit = None
        for pno in range(len(doc)):
            if pno in toc_pages:
                continue
            for b in doc[pno].get_text("blocks"):
                if norm(b[4]).startswith(norm(t)) and len(norm(b[4])) <= len(norm(t)) + 8:
                    hit = (pno, b[1])
                    break
            if hit:
                break
        if not hit:
            continue
        key = lambda pos: (pos["page"], pos["y"])
        sec = next((x for x in result["sections"] if key(x[side]["start"]) <= hit < key(x[side]["end"])), None) or \
            max((x for x in result["sections"] if key(x[side]["start"]) <= hit), key=lambda x: key(x[side]["start"]), default=None)
        topic = ""
        if side == "candidate":
            if anchors is None:
                try:
                    items, _ = _aem.anchors(path)
                    anchors = _aem.Locator(items) if items else False
                except Exception:
                    anchors = False
            if anchors:
                got = anchors.at(hit[0], hit[1] + 2)
                topic = got[0].title if got else ""
        out[f["id"]] = ((sec["candidate_title"] if side == "candidate" else sec["title"]) if sec else "", topic)
    return out


def _toc_table_html(rows: list[tuple], result: dict | None = None) -> str:
    """One row per TOC issue: the entry, the section and AEM topic it belongs to, what is wrong, and where."""
    ent = re.compile(r"[“\"]([^”\"]+)[”\"]")
    def entry(f):
        d = f.get("detail") or {}
        t = d.get("baseline_title") or d.get("candidate_title") or d.get("title") or d.get("heading")
        if not t:
            m = ent.search(f.get("message", ""))
            t = m.group(1) if m else ""
        return t
    def page(locs):
        return ", ".join(dict.fromkeys(f"p.{l['page'] + 1}" for l in (locs or []))) or "—"
    places = _toc_places(result, rows, entry) if result else {}
    body = "".join(
        f"<tr><td class='n'>{k}</td><td><b>{escape(entry(f))}</b></td>"
        f"<td>{escape(places.get(f['id'], ('', ''))[0] or '—')}</td>"
        f"<td>{escape(places.get(f['id'], ('', ''))[1] or (f.get('aem') or {}).get('topic') or '—')}</td>"
        f"<td><b style='color:{SEV_COLOR.get(f['severity'], '#1d2330')}'>{escape(f.get('issue') or ', '.join(f.get('types') or []))}</b><br/>"
        f"{escape(concise(f.get('description') or f['message']))}</td>"
        f"<td>{page(f.get('baseline'))}</td><td>{page(f.get('candidate'))}</td></tr>"
        for k, (s, f) in enumerate(rows, 1))
    return (f"<h2>Table of contents issues ({len(rows)})</h2>"
            "<table class='grid'><tr><th class='n'>#</th><th>TOC entry</th><th>Section</th><th>AEM topic</th>"
            "<th>Issue</th><th>Prod</th><th>Stage</th></tr>" + body + "</table>")


def truncated_html(result: dict) -> str:
    """Findings a per-check cap (report.max_findings_per_check) left out of the report entirely: every
    section and check that dropped some, so a capped run never hides this silently. Critical and genuine
    issues are never capped (engine.py); image/asset omissions are not listed here - pictures have their
    own separate filtering (assets.report_types) and are reported in image-issues.pdf, not this note."""
    from ..engine import CATEGORY
    rows = [(s, c, n) for s in result["sections"] for c, info in (s.get("checks") or {}).items()
            for n in [info.get("truncated", 0)] if n and CATEGORY.get(c, c) != "images"]
    if not rows:
        return ""
    html = [f"<h2 style='color:#b45309'>Issues omitted by the per-check cap ({sum(n for _, _, n in rows)})</h2>",
            "<p class='muted'>report.max_findings_per_check thinned these checks below their real count in this "
            "section; raise or remove the cap (0 = no cap) in config to see every one of them.</p>",
            "<table class='grid'><tr><th>Section</th><th>Check</th><th>Omitted</th></tr>"]
    for s, c, n in rows:
        html.append(f"<tr><td>{escape(s['title'])}</td><td><b style='color:{CHECK_COLOR.get(c, '#475569')}'>{escape(c)}</b></td>"
                    f"<td class='n'>{n}</td></tr>")
    return "".join(html) + "</table>"


def coverage_html(result: dict) -> str:
    """Every finding with prod-side content and nothing at all on the stage side: a definitive list of
    "this exists in prod but stage has no counterpart" that is not limited by type, category or the
    genuine/noise classification - a prod element either has a stage match somewhere, or it is here."""
    gaps = [(s, f) for s in result["sections"] for f in s["findings"] if f.get("baseline") and not f.get("candidate")]
    if not gaps:
        return "<h2 style='color:#16a34a'>Content coverage: nothing missing</h2><p>Every prod element has a match in stage.</p>"
    html = [f"<h2 style='color:#b42318'>Content coverage: prod material not found in stage ({len(gaps)})</h2>",
            "<p class='muted'>Every issue whose prod location has no stage counterpart at all, regardless of its "
            "type or severity - nothing in this list is filtered out.</p>",
            "<table class='grid'><tr><th>#</th><th>Section</th><th>Category</th><th>Issue</th><th>Prod p.</th></tr>"]
    for s, f in gaps:
        html.append(f"<tr><td>{f['id']}</td><td>{escape(s['title'])}</td>"
                    f"<td>{CAT_LABEL.get(f['category'], f['category'].title())}</td>"
                    f"<td><b style='color:{f.get('color') or SEV_COLOR.get(f['severity'], '#1d2330')}'>"
                    f"{escape(f.get('issue') or ', '.join(f.get('types') or []))}</b><br/>"
                    f"<span class='muted'>{escape(f['message']).replace(chr(10), ' ')[:200]}</span></td>"
                    f"<td>{f['baseline'][0]['page'] + 1}</td></tr>")
    return "".join(html) + "</table>"


def _prod_stage_html(f: dict) -> str:
    """The issue for a table cell: what prod has / what stage has; else its one-line description."""
    from ..genuine import prod_stage
    ps = prod_stage(f)
    if not ps:
        return escape(f["description"]).replace(chr(10), "<br/>")
    return (f"<b style='color:#2563eb'>Prod:</b> {escape(ps[0])}<br/><b style='color:#0f766e'>Stage:</b> {escape(ps[1])}")


def genuine_html(gen: list[tuple]) -> str:
    """Overview of the genuine issues: count per issue, then one row per issue with its description."""
    from ..genuine import where
    if not gen:
        return ("<h2 style='color:#16a34a'>Issues: none</h2><p>No missing or duplicated sections, missing or "
                "broken images, misplaced images or content, missing data, table or link problems.</p>")
    by = Counter(f.get("issue") or f.get("check", "") for _, f in gen)
    out = [f"<h2 style='color:#b42318'>Issues</h2>",
           _topics_html(gen),
           "<table class='grid'><tr><th>#</th><th>Section</th><th>Issue</th><th>Prod</th><th>Stage</th><th>AEM topic (GUID)</th>"
           "<th>Description</th></tr>"]
    for s, f in gen:
        pa, pc = where(f)
        out.append(f"<tr><td>{f['id']}</td><td>{escape(s['title'])}</td>"
                   f"<td><b style='color:{f.get('color') or '#1d2330'}'>{escape(f['issue'])}</b>"
                   f"<br/><span class='muted'>{escape(f.get('why', ''))}</span></td>"
                   f"<td>{pa or '—'}</td><td>{pc or '—'}</td><td>{_guid_cell(f.get('aem'))}</td>"
                   f"<td>{_prod_stage_html(f)}</td></tr>")
    return "".join(out) + "</table>"


def _topics_html(gen: list[tuple]) -> str:
    """The AEM topics to open and fix, most genuine issues first."""
    topics: dict[str, list] = {}
    for _, f in gen:
        if f.get("aem"):
            topics.setdefault(f["aem"]["guid"], [f["aem"], 0])[1] += 1
    if not topics:
        return ""
    rows = "".join(f"<tr><td>{_guid_cell(a)}</td><td>{n}</td></tr>"
                   for a, n in sorted(topics.values(), key=lambda x: -x[1]))
    return ("<h3>AEM topics to fix</h3><table class='grid'><tr><th>Topic · GUID (click to open in AEM)</th>"
            f"<th>Genuine issues</th></tr>{rows}</table><h3>All genuine issues</h3>")


def _guid_cell(a: dict | None) -> str:
    if not a:
        return "—"
    g = escape(a["guid"])
    link = f"<a href='{escape(a['url'])}'>{g}</a>" if a.get("url") else g
    return f"{escape(a.get('topic') or '')}<br/><span class='muted'>{link}</span>"


def select_issues(result: dict, flt: dict | None = None, severities: set[str] | None = None) -> list[tuple]:
    """(section, finding) pairs matching the report filter:
    categories / types / severities (lists; empty = all), critical_only, sections (section ids)."""
    flt = flt or {}
    cats, types = set(flt.get("categories") or []), set(flt.get("types") or [])
    sev = set(flt.get("severities") or []) or severities or {"error", "warning", "info"}
    secs = set(flt.get("sections") or [])
    q = (flt.get("q") or "").lower()
    ids = set(flt["ids"]) if flt.get("ids") is not None else None  # exactly the issues picked in the UI
    keep = set(flt.get("keep_types") or [])  # types shown whatever genuine_only / no_images say
    out = []
    for s in result["sections"]:
        if secs and s["id"] not in secs:
            continue
        for f in s["findings"] + (s.get("image_findings", []) if flt.get("image_issues") else []):
            if ids is not None:
                if f["id"] in ids:
                    out.append((s, f))
                continue
            if f["severity"] not in sev or (cats and f.get("category") not in cats) \
                    or (types and not set(f.get("types") or []) & types) or (flt.get("critical_only") and not f.get("critical")) \
                    or (q and q not in f["message"].lower() and q not in s["title"].lower()) \
                    or (flt.get("genuine_only") and not f.get("genuine") and not (keep & set(f.get("types") or []))
                        and not (flt.get("plus_images") and is_image_issue(f))) \
                    or (flt.get("non_genuine") and f.get("genuine")) \
                    or (flt.get("images") and not is_image_issue(f)) \
                    or (flt.get("image_issues") and not (set(f.get("types") or []) & IMAGE_ISSUE_TYPES
                                                         or (f.get("genuine") and text_on_picture(result, f)))) \
                    or (flt.get("no_images") and image_related(result, f) and not (keep & set(f.get("types") or []))):
                continue
            out.append((s, f))
    # content first, then links, formatting and CSS last; in each group the document's section
    # order, critical issues first within a section
    pos = {id(s): k for k, s in enumerate(result["sections"])}
    out.sort(key=lambda sf: (GROUP_ORDER[group_of(sf[1])], pos[id(sf[0])], not sf[1].get("critical"),
                             CAT_ORDER.get(sf[1].get("category"), 9)))
    return out


def describe(flt: dict | None, n: int) -> str:
    """Human-readable filter note for the report's first page."""
    flt = flt or {}
    if flt.get("note"):  # the UI describes its own selection
        return f"{n} issue(s) · {flt['note']}"
    parts = []
    if flt.get("categories"):
        parts.append("categories: " + ", ".join(CAT_LABEL.get(c, c) for c in flt["categories"]))
    if flt.get("types"):
        parts.append("types: " + ", ".join(flt["types"]))
    if flt.get("severities"):
        parts.append("severity: " + ", ".join(flt["severities"]))
    if flt.get("critical_only"):
        parts.append("critical only")
    if flt.get("genuine_only"):
        parts.append("genuine issues only")
    if flt.get("image_issues"):
        parts.append("image issues: artwork missing in stage, image label missing in stage")
    if flt.get("sections"):
        parts.append(f"{len(flt['sections'])} section(s)")
    if flt.get("q"):
        parts.append(f"search “{flt['q']}”")
    return f"{n} issue(s) · " + ("; ".join(parts) if parts else "all issues")


def build(result: dict, out_dir: str | Path, *, severities: set[str] | None = None,
          progress: Callable[[float, str], None] | None = None, options: dict | None = None,
          filename: str = "report.pdf") -> Path:
    """options = {"include": {summary, critical, sections, toc, stylemap, issues, screenshots: bool},
                  "filter": {categories, types, severities, critical_only, sections, q}}"""
    out = Path(out_dir)
    opts = options or {}
    inc = {**INCLUDE_ALL, **(opts.get("include") or {})}
    issues = select_issues(result, opts.get("filter"), severities) if inc["issues"] else []
    note = describe(opts.get("filter"), len(issues)) if options else ""
    toc = result.get("toc") if inc.get("toc_compare") else None
    toc_issues = [x for x in issues if x[1]["check"] == "toc"] if toc and toc.get("rows") else []
    if toc_issues:  # the TOC is compared entry by entry in its own section, like the UI's TOC tab
        issues = [x for x in issues if x[1]["check"] != "toc"]

    # --- summary, sections table, style map: HTML flow (Story)
    buf = io.BytesIO()
    writer = pymupdf.DocumentWriter(buf)
    page_rect = pymupdf.paper_rect("a4-l")
    blocks = _summary_html(result, inc, note)
    toc_rows = [x for x in (select_issues(result, opts.get("filter"), severities) if inc["issues"] else [])
                if x[1]["check"] == "toc"]
    # (no TOC issue table on the first page: the TOC comparison follows on its own pages)
    if (opts.get("filter") or {}).get("genuine_only"):
        blocks[0] = blocks[0].replace("PDF Parity Report", "PDF Report", 1)
    elif (opts.get("filter") or {}).get("non_genuine"):
        blocks[0] = blocks[0].replace("PDF Parity Report", "CSS Report", 1)
    elif (opts.get("filter") or {}).get("images") or (opts.get("filter") or {}).get("image_issues"):
        blocks[0] = blocks[0].replace("PDF Parity Report", "Image Report", 1)
    # (no "Issues (n) follow on the next pages" overview: the issues follow, grouped, each group headed)
    for block in blocks:  # each block on fresh pages; a page cap guards against a layout loop
        story = pymupdf.Story(block, user_css=CSS)
        more, pages = 1, 0
        while more and pages < 20 + block.count("<tr>") // 8:
            dev = writer.begin_page(page_rect)
            more, _ = story.place(page_rect + (36, 32, -36, -40))
            story.draw(dev)
            writer.end_page()
            pages += 1
    writer.close()
    doc = pymupdf.open("pdf", buf.getvalue())

    # --- issues: direct drawing, prod | stage screenshots side by side
    c = _Canvas(doc)
    if toc_issues:
        _toc_section(c, toc, result["meta"].get("mode") == "html", out)
    col_w, gap, max_h = (c.width - 16) / 2, 16, 320
    current = group = None
    for k, (s, f) in enumerate(issues):
        if group_of(f) != group:  # each group starts on a new page with its heading
            group = group_of(f)
            gk = GROUP_ORDER[group]
            c.new_page()
            n_g = sum(1 for _, x in issues if group_of(x) == group)
            c.runs([(f"{gk + 1}. {GROUPS[gk][1]}", "#1d2330", True), (f"   {n_g} issue(s) · {GROUPS[gk][2]}", "#6a7282", False)], 14)
            c.y += 6
            current = None
        if progress and k % 50 == 0:
            progress(k / max(len(issues), 1), f"PDF report: issue {k}/{len(issues)}")
        shots = f.get("shots", {}) if inc["screenshots"] else {}
        imgs = {side: _jpeg_bytes(out / shots[side]) if shots.get(side) else None for side in ("baseline", "candidate")}
        img_h = max((min(col_w * im[1], max_h) for im in imgs.values() if im), default=0)
        text = f["description"] if f.get("genuine") and (opts.get("filter") or {}).get("genuine_only") else concise(f["message"])
        # the issue in full, never shortened; several differences at one spot: one line each
        parts = [p.strip() for block in text.split("\n\n") for p in block.split("  ·  ") if p.strip()]
        rows_txt = []
        for k, part in enumerate(parts, 1):
            head, *rest = part.split("\n")  # a design-spec issue: headline, "Figma: …", "Stage: …"
            rows_txt.append(("Issue", head, "#1d2330") if len(parts) == 1 else (f"Difference {k}", head, "#b42318"))
            rows_txt += [(ln.split(": ", 1)[0], ln.split(": ", 1)[1], "#7c3aed" if ln.startswith("Figma") else "#2563eb" if ln.startswith("Prod") else "#0f766e")
                         for ln in rest if ln.startswith(("Figma: ", "Prod: ", "Stage: "))]
        # the issue as what prod has and what stage has - nothing else (its name and pages are in the line above);
        # an issue that cannot be put that way keeps its one-line description
        from ..genuine import prod_stage
        ps = prod_stage(f)
        if ps:
            rows_txt = [("Prod", ps[0], "#2563eb"), ("Stage", ps[1], "#0f766e")]
        msg_lines = []  # (label or None, text, label colour)
        for label, value, colour in rows_txt:
            wrapped = c.wrap(re.sub(r"\.{4,}", " ", f"{label}: {value}"), 9)
            msg_lines.append((label, wrapped[0][len(label) + 2:], colour))
            msg_lines += [(None, ln, colour) for ln in wrapped[1:]]
        a = f.get("aem")
        need = 12 + len(msg_lines) * 12.2 + (11 if a else 0) + (img_h + 16 if img_h else 0) + 8
        need = min(need, c.PAGE.height - 2 * c.M - 60)  # longer than a page: it continues on the next one
        if s is not current:
            if not c.room(need + 40):
                c.new_page()
            elif c.y > c.M:
                c.y += 10
            _section_header(c, s)
            current = s
        elif not c.room(need):
            c.new_page()
            _section_header(c, s, cont=True)
        pa = f"p.{f['baseline'][0]['page'] + 1}" if f["baseline"] else "—"
        pc = f"p.{f['candidate'][0]['page'] + 1}" if f["candidate"] else "—"
        c.runs(([("CRITICAL  ", "#b42318", True)] if f.get("critical") else [])
               + [(SEV_LABEL.get(f["severity"], f["severity"].upper()), SEV_COLOR[f["severity"]], True), ("  ·  ", "#6a7282", False),
                (f.get("issue") if f.get("genuine") else f"{CAT_LABEL.get(f.get('category'), f['check'])} · {', '.join(f.get('types', []))}",
                 f.get("color") or CAT_COLOR.get(f.get("category"), "#333333"), True),
                (f"  ·  #{f['id']}  ·  prod {pa} ↔ stage {pc}", "#6a7282", False)], 8)
        for label, line, colour in msg_lines:
            if not c.room(12.2):
                c.new_page()
                _section_header(c, s, cont=True)
            c.runs(([(f"{label}: ", colour, True)] if label else []) + [(line, "#1d2330", False)], 9)
        if a:
            near = f"  ·  near {a['element']}" + (f" “{a['element_title']}”" if a.get("element_title") else "") if a.get("element") else ""
            c.runs([("AEM topic  ", "#6a7282", True), (a.get("topic") or "", "#1d2330", False), ("  ·  ", "#6a7282", False),
                    (a["guid"] + ("  ↗" if a.get("url") else ""), "#0f766e", True, a.get("url")), (near, "#6a7282", False)], 8)
        if img_h and not c.room(img_h + 16 + (11 if a else 0)):
            c.new_page()
            _section_header(c, s, cont=True)
        if img_h:
            c.y += 2
            for col, (side, label) in enumerate((("baseline", "PROD"), ("candidate", "STAGE"))):
                x0 = c.M + col * (col_w + gap)
                y_save = c.y
                c.runs([(f"{label} p.{shots.get(side + '_page', '?')}", "#6a7282", True)], 7, x=x0)
                c.y = y_save
                im = imgs[side]
                if im:
                    ih = min(col_w * im[1], max_h)
                    r = pymupdf.Rect(x0, c.y + 11, x0 + ih / im[1], c.y + 11 + ih)
                    c.page.insert_image(r, stream=im[0])
                    c.page.draw_rect(r, color=(0.8, 0.82, 0.85), width=0.5)
            c.y += img_h + 16
        c.page.draw_line((c.M, c.y), (c.PAGE.width - c.M, c.y), color=(0.88, 0.9, 0.92), width=0.5)
        c.y += 8

    if progress:
        progress(0.95, "PDF report: saving")
    try:
        footer = pymupdf.Font("notos")
    except Exception:
        footer = pymupdf.Font("helv")
    product = result["meta"].get("name") or ""
    for i, page in enumerate(doc):
        if not page.is_wrapped:  # Story pages leave the CTM unbalanced
            page.wrap_contents()
        tw = pymupdf.TextWriter(page.rect)
        tw.append((page.rect.width - 170, page.rect.height - 18), f"PDF Parity Report  ·  page {i + 1} of {doc.page_count}",
                  font=footer, fontsize=7)
        if product:  # every page says which publication it is about (a batch delivers one report per product)
            tw.append((36, page.rect.height - 18), product, font=footer, fontsize=7)
        tw.write_text(page, color=(0.55, 0.58, 0.63))
    doc.subset_fonts()
    path = out / filename
    doc.save(path, garbage=4, deflate=True)
    return path
