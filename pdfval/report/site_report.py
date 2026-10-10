"""Site validation report (site.pdf): every left-nav, link, pager, image, table and CSS-vs-spec check
the site run captured (pdfval/site_nav.py). Fail/warn rows are cards (status label, one-line expectation,
page, and a screenshot crop of the issue when it points at one page element) in a muted palette - pale
tint backgrounds and thin borders, not solid colour fills; pass rows are compact single lines, so every
pass is still in the report without burying the issues that matter under them.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Callable
from xml.sax.saxutils import escape

import pymupdf
from PIL import Image as PILImage

from .. import site_nav

# text colour, pale tint background, pale border - never a solid colour fill
STATUS = {
    "fail": {"text": "#b42318", "bg": "#fef2f2", "border": "#fecdca"},
    "warn": {"text": "#b54708", "bg": "#fffaeb", "border": "#fde68a"},
    "pass": {"text": "#067647", "bg": "#f0fdf4", "border": "#bbf7d0"},
    "info": {"text": "#475467", "bg": "#f9fafb", "border": "#e4e7ec"},
}
STATUS_LABEL = {"pass": "Pass", "warn": "Warn", "fail": "Fail", "info": "Info"}
CROP_PAD = 16          # px of context kept around the issue's own box
CROP_MAX = (420, 260)  # the crop is shrunk to fit this, never enlarged

CSS = """
* { font-family: sans-serif; color: #1d2330; }
body { font-size: 10px; }
h1 { font-size: 22px; margin: 0 0 4px 0; font-weight: bold; }
.subtitle { font-size: 9px; color: #667085; margin: 0 0 14px 0; }
table.dash { border-collapse: collapse; width: 100%; margin: 0 0 18px 0; }
table.dash th { background: #f9fafb; border: 1px solid #e4e7ec; padding: 7px 10px; font-size: 8.5px;
                color: #667085; text-align: left; font-weight: bold; }
table.dash td { border: 1px solid #e4e7ec; padding: 8px 10px; font-size: 13px; font-weight: bold; }
h2 { font-size: 12.5px; margin: 18px 0 9px 0; padding: 7px 11px; background: #f9fafb;
     border: 1px solid #e4e7ec; border-radius: 4px; color: #344054; }
h2 .n { font-weight: normal; color: #667085; }
.muted { color: #667085; }
.card { border: 1px solid; border-left-width: 3px; border-radius: 4px; padding: 8px 11px; margin: 0 0 7px 0; }
.label { display: inline; padding: 1px 7px; border-radius: 3px; font-size: 8px; font-weight: bold;
         margin-right: 7px; border: 1px solid; }
.title { font-size: 10.5px; font-weight: bold; }
.meta { font-size: 8.5px; color: #667085; margin-top: 3px; }
.shot { margin-top: 7px; }
.shot img { border: 1px solid #d0d5dd; border-radius: 4px; }
.passline { font-size: 9px; line-height: 14px; padding: 6px 8px; margin: 0 0 4px 0;
            background: #fbfbfc; border-radius: 3px; }
.passline .pg { display: block; font-size: 8px; color: #98a2b3; margin-top: 2px; }
"""


def _line(r: dict) -> str:
    """Status, item and its one-line expectation: what the spec / the navigation / the PDF expects,
    and what the page actually has - "Headline 1 — bold missing: expected 700, found 400", etc."""
    item, exp, act, note = r.get("item") or "", r.get("expected") or "", r.get("actual") or "", r.get("note") or ""
    if r["status"] == "pass":
        body = act or "as expected"
    elif exp and act:
        body = f"expected {exp}, found {act}"
    elif exp:
        body = f"expected {exp}, not found"
    elif act:
        body = act
    else:
        body = "—"
    line = f"{item}: {body}" if item else body
    return line + (f" — {note}" if note else "")


def _crop(out_dir: Path, shot: dict, idx: int, cache: dict) -> str | None:
    """A PNG of the issue's own box, cut from its page's full screenshot, +CROP_PAD px of context - the
    relative path to embed (pymupdf.Story's archive is rooted at out_dir), or None when the page
    screenshot or the box is missing (never fails the report over a missing crop)."""
    page_shot, box = shot.get("page_shot"), shot.get("box")
    if not page_shot or not box or len(box) != 4:
        return None
    img = cache.get(page_shot)
    if img is None:
        try:
            img = cache[page_shot] = PILImage.open(out_dir / page_shot)
        except Exception:
            return cache.setdefault(page_shot, None)
    if img is None:
        return None
    w, h = img.size
    x0, y0, x1, y1 = box
    cx0, cy0 = max(0, int(x0 - CROP_PAD)), max(0, int(y0 - CROP_PAD))
    cx1, cy1 = min(w, int(x1 + CROP_PAD)), min(h, int(y1 + CROP_PAD))
    if cx1 <= cx0 or cy1 <= cy0:
        return None
    crop = img.crop((cx0, cy0, cx1, cy1))
    crop.thumbnail(CROP_MAX)
    crops = out_dir / "site-shots" / "crops"
    crops.mkdir(parents=True, exist_ok=True)
    name = f"{idx:04d}.png"
    crop.save(crops / name)
    return f"site-shots/crops/{name}"


def _card(r: dict, out_dir: Path, idx: int, cache: dict) -> tuple[str, int]:
    """One fail/warn row as a card: status label, one-line expectation, page, and its screenshot crop
    (when the row points at one page element). Returns (html, next idx)."""
    s = STATUS.get(r["status"], STATUS["info"])
    crop = None
    if r.get("shot"):
        idx += 1
        crop = _crop(out_dir, r["shot"], idx, cache)
    shot_html = f"<div class='shot'><img src='{crop}' style='max-width:380px;max-height:240px'></div>" if crop else ""
    html = (f"<div class='card' style='background:{s['bg']};border-color:{s['border']}'>"
            f"<span class='label' style='color:{s['text']};border-color:{s['border']};background:#ffffff'>"
            f"{STATUS_LABEL.get(r['status'], r['status'])}</span>&#160;&#160;"
            f"<span class='title'>{escape(_line(r))}</span>"
            f"<div class='meta'>{escape(r.get('page', '') or '')}</div>{shot_html}</div>")
    return html, idx


def build(result: dict, out_dir: str | Path, *, progress: Callable[[float, str], None] | None = None,
          filename: str = "site.pdf", title: str = "Site validation report",
          groups: list[tuple[str, str]] | None = None) -> Path | None:
    """One PDF for the whole site run: a summary dashboard, then every group (left navigation, links,
    pager, on this page, page layout and pictures, breadcrumb, CSS vs the design spec, ...) with its
    fail/warn rows as cards (with a screenshot crop when the row points at one page element) and its
    pass rows as compact lines. Removed when the run has no site rows (a plain PDF-vs-PDF compare)."""
    report = progress or (lambda f, m: None)
    out_dir = Path(out_dir)
    out = out_dir / filename
    site = result.get("site") or {}
    rows = site.get("rows") or []
    if site.get("error") or not rows:
        out.unlink(missing_ok=True)
        return None
    sm = site.get("summary") or {}
    counts = sm.get("groups") or {}
    meta = result.get("meta") or {}
    candidate = ((meta.get("candidate") or {}).get("url") or (meta.get("candidate") or {}).get("path") or "")
    overall = STATUS.get(sm.get("status"), STATUS["info"])
    html = [
        f"<h1>{escape(title)}</h1>"
        f"<p class='subtitle'>{escape(candidate)} · generated {escape(meta.get('generated', ''))}</p>"
        "<table class='dash'><tr><th>Result</th><th>Pages</th><th>Fail</th><th>Warn</th><th>Pass</th></tr>"
        f"<tr><td style='color:{overall['text']}'>{str(sm.get('status', '')).upper()}</td>"
        f"<td>{sm.get('pages', 0)}</td>"
        f"<td style='color:{STATUS['fail']['text']}'>{sm.get('fail', 0)}</td>"
        f"<td style='color:{STATUS['warn']['text']}'>{sm.get('warn', 0)}</td>"
        f"<td style='color:{STATUS['pass']['text']}'>{sm.get('pass', 0)}</td></tr></table>",
    ]
    cache: dict = {}
    idx = 0
    for gid, label in (groups or site_nav.GROUPS):
        grows = [r for r in rows if r["group"] == gid]
        if not grows:
            continue
        gc = counts.get(gid) or {}
        html.append(f"<h2>{escape(label)} <span class='n'>— "
                    f"<span style='color:{STATUS['fail']['text']}'>{gc.get('fail', 0)} fail</span> · "
                    f"<span style='color:{STATUS['warn']['text']}'>{gc.get('warn', 0)} warn</span> · "
                    f"<span style='color:{STATUS['pass']['text']}'>{gc.get('pass', 0)} pass</span></span></h2>")
        for r in grows:
            if r["status"] in ("fail", "warn"):
                card_html, idx = _card(r, out_dir, idx, cache)
                html.append(card_html)
        passing = [r for r in grows if r["status"] not in ("fail", "warn")]
        for r in passing:
            colour = STATUS.get(r["status"], STATUS["info"])["text"]
            html.append(f"<div class='passline'><span style='color:{colour};font-weight:bold'>"
                        f"{STATUS_LABEL.get(r['status'], r['status'])}</span>&#160;&#160;"
                        f"{escape(_line(r))}<span class='pg'>{escape(r.get('page', '') or '')}</span></div>")

    buf = io.BytesIO()
    writer = pymupdf.DocumentWriter(buf)
    page_rect = pymupdf.paper_rect("a4-l")
    report(0.2, "Building the site report")
    archive = pymupdf.Archive(str(out_dir))
    for block in "".join(html).split("\f"):
        if not block.strip():
            continue
        story = pymupdf.Story(block, user_css=CSS, archive=archive)
        more, pages = 1, 0
        while more and pages < 20 + block.count("<div class='card'") // 4 + block.count("passline") // 10:
            dev = writer.begin_page(page_rect)
            more, _ = story.place(page_rect + (36, 32, -36, -40))
            story.draw(dev)
            writer.end_page()
            pages += 1
    writer.close()
    report(0.9, "Saving the site report")
    doc = pymupdf.open("pdf", buf.getvalue())
    doc.save(out, garbage=4, deflate=True, clean=True)
    doc.close()
    report(1.0, "Site report done")
    return out

