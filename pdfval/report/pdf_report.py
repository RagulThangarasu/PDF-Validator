"""Printable PDF report: summary, sections table, style map, and every issue
with its prod and stage screenshots side by side (A4 landscape).

The summary pages are an HTML flow (pymupdf.Story); the issue pages are drawn
directly with one shared font, and screenshots are down-sampled to JPEG.
"""
from __future__ import annotations

import io
import re
from html import escape
from pathlib import Path
from typing import Callable

import pymupdf
from PIL import Image

SEV_COLOR = {"error": "#d92d20", "warning": "#b45309", "info": "#64748b"}
STATUS_COLOR = {"fail": "#d92d20", "warn": "#b45309", "pass": "#16a34a"}
CHECK_COLOR = {"toc": "#9333ea", "structure": "#2563eb", "content": "#e11d48", "tables": "#0284c7", "assets": "#0d9488",
               "integrity": "#b91c1c", "style": "#7c3aed", "layout": "#ea580c"}
PCT_COLOR = {"pass": "#16a34a", "warn": "#b45309", "fail": "#d92d20"}
CATS = [("content", "Content"), ("images", "Images"), ("tables", "Tables"), ("toc", "TOC"), ("structure", "Structure"),
        ("links", "Links & rendering"), ("css", "CSS / layout")]
CAT_LABEL = dict(CATS)
CAT_COLOR = {"toc": "#9333ea", "content": "#e11d48", "images": "#0d9488", "tables": "#0284c7", "structure": "#2563eb",
             "links": "#b91c1c", "css": "#7c3aed"}
CAT_ORDER = {c: k for k, (c, _) in enumerate(CATS)}
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


def _summary_html(result: dict) -> list[str]:
    """HTML blocks for the front pages; each block starts on a new page (MuPDF's Story
    can loop forever when a long table starts part-way down a page after another table)."""
    sm, meta = result["summary"], result["meta"]
    doc_row = lambda label, m: (f"<tr><th>{label}</th><td>{escape(m['url'])} (web page, rendered and read from its DOM)</td></tr>"
                                if m.get("url") else
                                f"<tr><th>{label}</th><td>{escape(Path(m['path']).name)} · {m['pages']} pages · "
                                f"{m['page_size'][0]}×{m['page_size'][1]} pt · body {m['body_size']}pt</td></tr>")
    html = [
        f"<h1>PDF Parity Report — <span style='color:{STATUS_COLOR.get(sm['result'])}'>{sm['result'].upper()}</span></h1>",
        f"<p class='muted'>Generated {escape(meta['generated'])} · prod is the reference baseline</p>",
        "<table class='grid'>", doc_row("Prod (baseline)", meta["baseline"]), doc_row("Web page (candidate)" if meta.get("mode") == "html" else "Stage (candidate)", meta["candidate"]),
        f"<tr><th>Result</th><td><b style='color:{STATUS_COLOR.get(sm['result'])}'>{sm['result'].upper()}</b> — "
        f"sections: <b style='color:#d92d20'>{sm['fail']} fail</b>, <b style='color:#b45309'>{sm['warn']} warn</b>, "
        f"<b style='color:#16a34a'>{sm['pass']} pass</b></td></tr>",
        f"<tr><th>Content match</th><td><b style='color:{PCT_COLOR[_pct_status(sm['content'])]};font-size:11px'>"
        f"{sm['content']['match_pct']:.2f}%</b> of prod words present in stage (text, punctuation, spacing; not CSS) · "
        f"{sm['content']['missing_words']} missing · {sm['content']['extra_words']} extra · "
        f"{sm['content']['spacing_issues']} spacing · pass ≥ {sm['content']['pass_pct']}%, warn ≥ {sm['content']['warn_pct']}% · "
        f"sections {sm['content']['pass']} pass / {sm['content']['warn']} warn / {sm['content']['fail']} fail</td></tr>",
        f"<tr><th>Critical / breaking</th><td><b style='color:{'#b42318' if sm['critical']['total'] else '#16a34a'}'>"
        f"{sm['critical']['total']}</b> " + (" · ".join(f"{v} × {escape(k)}" for k, v in sm["critical"]["by_kind"].items())
                                             or "— no missing sections, rows, images, files, links or glyphs") + "</td></tr>",
        f"<tr><th>CSS / layout</th><td>{sm['css']['issues']} issues (style {sm['css']['style']}, layout {sm['css']['layout']}) "
        "— reported separately, not part of the content %</td></tr>",
        "<tr><th>All findings</th><td>" + " · ".join(f"<b style='color:{CHECK_COLOR[k]}'>{k}</b> {v}"
                                                    for k, v in sm["by_check"].items()) + "</td></tr></table>",
    ]
    html.append("<h2>Issues by category and type</h2><table class='grid'><tr><th>Category</th><th>Total</th><th>Types</th></tr>")
    for c, label in CATS:
        bc = sm["by_category"][c]
        html.append(f"<tr><td><b style='color:{CAT_COLOR[c]}'>{label}</b></td><td class='n'>{bc['total']}</td><td>"
                    + (" · ".join(f"{escape(t)} {n}" for t, n in bc["types"].items()) or "—") + "</td></tr>")
    html.append("</table>")
    crit = [(s, f) for s in result["sections"] for f in s["findings"] if f.get("critical")]
    if crit:
        html.append("<h2 style='color:#b42318'>Critical issues</h2><table class='grid'><tr><th>#</th><th>Section</th>"
                    "<th>Check</th><th>Issue</th><th>Prod p.</th><th>Stage p.</th></tr>")
        for s, f in crit:
            html.append(f"<tr><td>{f['id']}</td><td>{escape(s['title'])}</td><td>{CAT_LABEL[f['category']]}</td><td>{escape(f['message'])}</td>"
                        f"<td>{f['baseline'][0]['page'] + 1 if f['baseline'] else '—'}</td>"
                        f"<td>{f['candidate'][0]['page'] + 1 if f['candidate'] else '—'}</td></tr>")
        html.append("</table>")
    html.append("\f")  # block break
    html += [
        "<h2>Sections</h2><table class='grid'><tr><th>#</th><th>Section</th><th>Prod p.</th><th>Stage p.</th>"
        "<th>Status</th><th>Content %</th><th>Missing / extra</th><th>Critical</th>"
        + "".join(f"<th>{label}</th>" for _, label in CATS) + "</tr>",
    ]
    for n, s in enumerate(result["sections"], 1):
        html.append(
            f"<tr><td>{n}</td><td>{escape(s['title'])}</td><td>{s['baseline']['start']['page'] + 1}</td>"
            f"<td>{s['candidate']['start']['page'] + 1}</td>"
            f"<td><b style='color:{STATUS_COLOR[s['status']]}'>{s['status']}</b></td>"
            f"<td class='n'><b style='color:{PCT_COLOR[s['content']['status']]}'>{s['content']['match_pct']:.2f}%</b></td>"
            f"<td class='n'>{s['content']['missing_words']} / {s['content']['extra_words']}</td>"
            f"<td class='n'>{'<b style=\'color:#b42318\'>' + str(s['critical']) + '</b>' if s['critical'] else ''}</td>"
            + "".join(f"<td class='n'>{s.get('categories', {}).get(c, 0) or ''}</td>" for c, _ in CATS) + "</tr>")
    html.append("</table>")
    t = result.get("toc")
    if t and t["rows"]:
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
            html.append(f"<tr><td>{r['pos']['baseline'] or '—'}</td>{cell(a)}<td><b style='color:{tone[r['status']]}'>{r['status']}</b></td>"
                        f"<td>{r['pos']['candidate'] or '—'}</td>{cell(b)}</tr>")
        html.append("</table>")
    if result["style_map"]:
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
        self.regular, self.bold = pymupdf.Font("notos"), pymupdf.Font("notosbo")
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

    def runs(self, runs: list[tuple[str, str, bool]], size: float, x: float | None = None):
        """One line of differently coloured runs: [(text, '#hex', bold)]."""
        x = self.M if x is None else x
        for text, color, bold in runs:
            font = self.bold if bold else self.regular
            tw = pymupdf.TextWriter(self.PAGE)
            tw.append((x, self.y + size), text, font=font, fontsize=size)
            tw.write_text(self.page, color=_rgb(color))
            x += font.text_length(text, size)
        self.y += size * 1.35


def _rgb(hex_: str) -> tuple[float, float, float]:
    return tuple(int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5))


def _jpeg_bytes(src: Path, width: int = 600) -> tuple[bytes, float] | None:
    if not src.exists():
        return None
    img = Image.open(src).convert("RGB")
    if img.width > width:
        img = img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=60, optimize=True)
    return buf.getvalue(), img.height / img.width


def _section_header(c: _Canvas, s: dict, cont: bool = False):
    c.runs([(s["title"] + (" (cont.)" if cont else "") + "   ", "#1d2330", True),
            (s["status"].upper(), STATUS_COLOR[s["status"]], True)], 13)
    c.runs([(f"Prod p.{s['baseline']['start']['page'] + 1}–{s['baseline']['end']['page'] + 1}  ·  "
             f"Stage p.{s['candidate']['start']['page'] + 1}–{s['candidate']['end']['page'] + 1}  ·  "
             f"content match {s['content']['match_pct']:.2f}% ({s['content']['status']})  ·  "
             f"CSS issues {s['css']['issues']}" + (f"  ·  {s['critical']} CRITICAL" if s['critical'] else ""),
             "#6a7282", False)], 8.5)
    c.y += 4


def build(result: dict, out_dir: str | Path, *, severities: set[str] | None = None,
          progress: Callable[[float, str], None] | None = None) -> Path:
    out = Path(out_dir)
    sev = severities or {"error", "warning", "info"}
    issues = [(s, f) for s in result["sections"]
              for f in sorted(s["findings"], key=lambda f: (not f.get("critical"), CAT_ORDER.get(f.get("category"), 9)))
              if f["severity"] in sev]

    # --- summary, sections table, style map: HTML flow (Story)
    buf = io.BytesIO()
    writer = pymupdf.DocumentWriter(buf)
    page_rect = pymupdf.paper_rect("a4-l")
    blocks = _summary_html(result)
    blocks[-1] += f"<h2>Issues ({len(issues)}) follow on the next pages</h2>"
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
    col_w, gap, max_h = (c.width - 16) / 2, 16, 230
    current = None
    for k, (s, f) in enumerate(issues):
        if progress and k % 50 == 0:
            progress(k / max(len(issues), 1), f"PDF report: issue {k}/{len(issues)}")
        shots = f.get("shots", {})
        imgs = {side: _jpeg_bytes(out / shots[side]) if shots.get(side) else None for side in ("baseline", "candidate")}
        img_h = max((min(col_w * im[1], max_h) for im in imgs.values() if im), default=0)
        msg_lines = c.wrap(re.sub(r"\.{4,}", " … ", f["message"]), 9)
        if len(msg_lines) > 5:
            msg_lines = msg_lines[:5]
            msg_lines[-1] += " …"
        need = 12 + len(msg_lines) * 12.2 + (img_h + 16 if img_h else 0) + 8
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
               + [(f["severity"].upper(), SEV_COLOR[f["severity"]], True), ("  ·  ", "#6a7282", False),
                (f"{CAT_LABEL.get(f.get('category'), f['check'])} · {', '.join(f.get('types', []))}",
                 CAT_COLOR.get(f.get("category"), "#333333"), True),
                (f"  ·  #{f['id']}  ·  prod {pa} ↔ stage {pc}", "#6a7282", False)], 8)
        for line in msg_lines:
            c.runs([(line, "#1d2330", False)], 9)
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
    footer = pymupdf.Font("notos")
    for i, page in enumerate(doc):
        if not page.is_wrapped:  # Story pages leave the CTM unbalanced
            page.wrap_contents()
        tw = pymupdf.TextWriter(page.rect)
        tw.append((page.rect.width - 170, page.rect.height - 18), f"PDF Parity Report  ·  page {i + 1} of {doc.page_count}",
                  font=footer, fontsize=7)
        tw.write_text(page, color=(0.55, 0.58, 0.63))
    doc.subset_fonts()
    path = out / "report.pdf"
    doc.save(path, garbage=4, deflate=True)
    return path
