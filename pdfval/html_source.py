"""Web page (URL) -> Doc, so a PDF can be validated against its HTML version.

The page is rendered in headless Chromium and read from the DOM (not OCR):
every word with its rendered box and computed style, the heading outline
(h1-h6), real tables (table/tr/td), images and links. The full-page screenshot
is cut into "pages" (slices) and saved as an image-only PDF with the links
added as annotations, so the rest of the pipeline (screenshots, viewer, sync
scrolling, report) treats the web page like any other document. 1 CSS px = 1 pt.
"""
from __future__ import annotations

import io
import math
import re
from pathlib import Path

import pymupdf
from PIL import Image as PILImage

from . import normalize
from .extract import _measure
from .model import Doc, Image, Line, PageInfo, Style, Word

DEFAULT_EXCLUDE = "nav, header, footer, aside, script, style, noscript, template, [aria-hidden='true'], [role='navigation']"

# Runs in the page. Returns words (DOM order) with boxes and styles, headings, tables, images, links.
_EXTRACT_JS = r"""
([rootSel, exclude]) => {
  const root = (rootSel && document.querySelector(rootSel)) || document.querySelector('main') ||
               document.querySelector('article') || document.body;
  const sx = window.scrollX, sy = window.scrollY;
  const excluded = el => !!(el && exclude && el.closest(exclude));
  const visible = el => el && (el.checkVisibility ? el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true }) : el.offsetParent !== null);
  const BLOCK = new Set(['block', 'list-item', 'table-cell', 'table-caption', 'flex', 'grid', 'flow-root']);
  const blockIds = new Map();
  const blockOf = el => {
    let b = el;
    while (b && b !== root && !(/^H[1-6]$/.test(b.tagName) || BLOCK.has(getComputedStyle(b).display))) b = b.parentElement;
    b = b || root;
    if (!blockIds.has(b)) blockIds.set(b, blockIds.size);
    return blockIds.get(b);
  };
  const hex = c => { const m = c.match(/\d+(\.\d+)?/g) || [0, 0, 0]; return '#' + m.slice(0, 3).map(v => (+v | 0).toString(16).padStart(2, '0')).join(''); };
  const styleCache = new Map();
  const styleOf = el => {
    if (styleCache.has(el)) return styleCache.get(el);
    const cs = getComputedStyle(el);
    const s = { family: cs.fontFamily.split(',')[0].replace(/["']/g, '').trim(), weight: +cs.fontWeight || 400,
                italic: cs.fontStyle !== 'normal', size: parseFloat(cs.fontSize), color: hex(cs.color),
                pre: /pre/.test(cs.whiteSpace) };
    styleCache.set(el, s); return s;
  };
  const words = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const range = document.createRange();
  let node;
  while ((node = walker.nextNode())) {
    const el = node.parentElement;
    if (!el || excluded(el) || !visible(el) || !node.data.trim()) continue;
    const st = styleOf(el), block = blockOf(el);
    const h = el.closest('h1,h2,h3,h4,h5,h6');
    const a = el.closest('a[href]');
    const re = /\S+/g; let m;
    while ((m = re.exec(node.data))) {
      range.setStart(node, m.index); range.setEnd(node, m.index + m[0].length);
      const rs = range.getClientRects(); if (!rs.length) continue;
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (const r of rs) { x0 = Math.min(x0, r.left); y0 = Math.min(y0, r.top); x1 = Math.max(x1, r.right); y1 = Math.max(y1, r.bottom); }
      if (x1 - x0 < 0.5) continue;
      const after = node.data.slice(m.index + m[0].length).match(/^\s*/)[0].length;
      words.push([m[0], x0 + sx, y0 + sy, x1 + sx, y1 + sy, st.family, st.weight, st.italic, st.size, st.color, block,
                  h ? +h.tagName[1] : 0, a ? a.href : '', st.pre ? after : Math.min(after, 1)]);
    }
  }
  const rect = el => { const r = el.getBoundingClientRect(); return [r.left + sx, r.top + sy, r.right + sx, r.bottom + sy]; };
  const headings = [...root.querySelectorAll('h1,h2,h3,h4,h5,h6')].filter(h => visible(h) && !excluded(h) && h.innerText.trim())
    .map(h => [+h.tagName[1], h.innerText.replace(/\s+/g, ' ').trim(), ...rect(h)]);
  const images = [...root.querySelectorAll('img, svg, [role="img"]')].filter(i => visible(i) && !excluded(i) && !i.closest('svg svg'))
    .map(rect).filter(r => r[2] - r[0] >= 4 && r[3] - r[1] >= 4);
  const tables = [...root.querySelectorAll('table')].filter(t => visible(t) && !excluded(t)).map(t => {
    const rows = [...t.rows].filter(visible);
    const gridRow = rows.reduce((b, r) => (r.cells.length > (b ? b.cells.length : 0) ? r : b), null);
    return { box: rect(t), rows: rows.map(r => [rect(r), r.cells.length ? rect(r.cells[0]) : null]),
             grid: gridRow ? [...gridRow.cells].map(c => { const q = rect(c); return [q[0], q[2]]; }) : [] };
  });
  const links = [...root.querySelectorAll('a[href]')].filter(a => visible(a) && !excluded(a) && a.innerText.trim())
    .map(a => [a.href, ...rect(a)]);
  const de = document.documentElement;
  return { title: document.title, width: Math.max(de.clientWidth, 320), height: Math.max(de.scrollHeight, document.body.scrollHeight),
           words, headings, images, tables, links };
}
"""


def capture(url: str, out_dir: str | Path, *, root: str = "", exclude: str = DEFAULT_EXCLUDE,
            width: int = 1280, wait_ms: int = 1500, slice_h: int = 1800, progress=None) -> tuple[Doc, dict]:
    """Render `url`, extract its structure and write <out_dir>/candidate_source.pdf. Returns (Doc, info)."""
    from playwright.sync_api import sync_playwright

    report = progress or (lambda f, m: None)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 1000}, device_scale_factor=1)
        report(0.05, f"Opening {url}")
        page.goto(url, wait_until="networkidle", timeout=90_000)
        # scroll through once so lazy-loaded images/sections render, then back to the top
        report(0.3, "Loading lazy content")
        total = page.evaluate("document.documentElement.scrollHeight")
        for y in range(0, int(total) + 1000, 800):
            page.evaluate(f"window.scrollTo(0, {y})")
            page.wait_for_timeout(60)
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(wait_ms)
        report(0.5, "Reading the page structure")
        data = page.evaluate(_EXTRACT_JS, [root, exclude])
        W, H = int(data["width"]), int(math.ceil(data["height"]))
        cuts = _cuts(data, H, slice_h)
        report(0.7, "Capturing the page")
        slices = []
        for y0, y1 in zip(cuts, cuts[1:]):
            png = page.screenshot(clip={"x": 0, "y": y0, "width": W, "height": y1 - y0}, full_page=True)
            slices.append((y0, y1, png))
        browser.close()

    report(0.9, "Building the document")
    pdf = pymupdf.open()
    for y0, y1, png in slices:
        pg = pdf.new_page(width=W, height=y1 - y0)
        pg.insert_image(pg.rect, stream=png)
    for href, x0, y0, x1, y1 in data["links"]:
        k = _slice_of(cuts, y0)
        if k is not None:
            pdf[k].insert_link({"kind": pymupdf.LINK_URI, "uri": href,
                                "from": pymupdf.Rect(x0, y0 - cuts[k], x1, y1 - cuts[k])})
    path = out / "candidate_source.pdf"
    pdf.save(path, garbage=3, deflate=True)
    doc = _to_doc(data, cuts, str(path))
    info = {"url": url, "title": data["title"], "width": W, "height": H, "pages": len(slices),
            "words": len(doc.words), "headings": len(data["headings"]), "tables": len(data["tables"]),
            "images": len(data["images"]), "links": len(data["links"]), "root": root or "auto (main / article / body)"}
    return doc, info


def _cuts(data: dict, H: int, slice_h: int) -> list[int]:
    """Slice boundaries every ~slice_h px, moved up so they don't cut through a table,
    an image or a line of text (a cut there would split one element over two pages)."""
    spans = [(t["box"][1], t["box"][3]) for t in data["tables"] if t["box"][3] - t["box"][1] < slice_h * 0.9]
    spans += [(r[1], r[3]) for r in data["images"] if r[3] - r[1] < slice_h * 0.9]
    spans += [(w[2], w[4]) for w in data["words"]]
    cuts, y = [0], 0
    while y + slice_h < H:
        c = y + slice_h
        for a, b in spans:
            if a < c < b and a > y + slice_h * 0.3:
                c = min(c, int(a) - 2)
        cuts.append(int(c))
        y = int(c)
    cuts.append(H)
    return cuts


def _slice_of(cuts: list[int], y: float) -> int | None:
    for k, (a, b) in enumerate(zip(cuts, cuts[1:])):
        if a <= y < b:
            return k
    return None


def _to_doc(data: dict, cuts: list[int], path: str) -> Doc:
    W = int(data["width"])
    pages = [PageInfo(W, b - a) for a, b in zip(cuts, cuts[1:])]
    words: list[Word] = []
    lines: list[Line] = []
    prev = None
    for k, (t, x0, y0, x1, y1, fam, wt, it, px, col, block, hlevel, href, after) in enumerate(data["words"]):
        p = _slice_of(cuts, y0)
        if p is None:
            continue
        top = y0 - cuts[p]
        size = round(px * 0.75, 1)  # CSS px -> pt, comparable with the PDF's font sizes
        new_line = (prev is None or prev[0] != p or prev[1] != block
                    or abs(top - prev[2]) > 0.5 * max(y1 - y0, 1))
        if new_line:
            if words:
                words[-1].space_after = None  # line end
            lines.append(Line(p, (x0, top, x1, y1 - cuts[p]), t, size, len(words), (p, block)))
        else:
            ln = lines[-1]
            ln.bbox = (min(ln.bbox[0], x0), min(ln.bbox[1], top), max(ln.bbox[2], x1), max(ln.bbox[3], y1 - cuts[p]))
            ln.text += " " + t
            ln.size = max(ln.size, size)
        words.append(Word(t, normalize.token(t, case_sensitive=True, ignore={"•", "●", "■", "▪", "◦"}),
                          p, (x0, top, x1, y1 - cuts[p]), Style(fam, int(wt), bool(it), size, col),
                          len(lines) - 1, line_start=new_line, space_after=int(after)))
        prev = (p, block, top)
    if words:
        words[-1].space_after = None
    images = []
    for x0, y0, x1, y1 in data["images"]:
        p = _slice_of(cuts, y0)
        if p is not None:
            images.append(Image(p, (x0, y0 - cuts[p], x1, min(y1, cuts[p + 1]) - cuts[p])))
    # outline from the heading hierarchy: levels ranked (h2/h3/h4 used -> 1/2/3)
    used = sorted({h[0] for h in data["headings"]})
    outline = []
    for lvl, text, x0, y0, x1, y1 in data["headings"]:
        p = _slice_of(cuts, y0)
        if p is not None:
            outline.append((used.index(lvl) + 1, text, p + 1))
    doc = Doc(path, "candidate", pages, words, lines, images, outline)
    # real table structure from the DOM, used by the tables check instead of detection
    raw: dict[int, list] = {}
    for t, tb in enumerate(data["tables"]):
        p = _slice_of(cuts, tb["box"][1])
        if p is None:
            continue
        off = cuts[p]
        sh = lambda r: (r[0], r[1] - off, r[2], r[3] - off)
        raw.setdefault(p, []).append((t, sh(tb["box"]), [(sh(r), sh(c) if c else None) for r, c in tb["rows"]],
                                      [tuple(g) for g in tb["grid"]]))
    doc.raw_tables = raw
    _measure(doc)
    return doc
