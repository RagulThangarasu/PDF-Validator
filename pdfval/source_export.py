"""Source export: everything a PDF contains, as raw data and as AEM Guides (DITA) source.

Stage PDFs are made from AEM Guides DITA, which was itself converted from the prod PDF; content
and structure get lost on the way. This writes, for one PDF, a zip with

  raw/   every fact read straight from the PDF, nothing filtered: each text line with its spans
         (font, size, colour, bold/italic, superscript, position), the outline, named destinations,
         tables cell by cell (also as CSV), links, the images as their original files, embedded
         attachments, fonts, metadata / XMP, and the plain text of every page.
  dita/  the document rebuilt as DITA 1.3 source that AEM Guides can import: a .ditamap with one
         topic per heading, nested like the outline; paragraphs, bulleted / numbered lists,
         note / tip / warning callouts, CALS tables, figures with the extracted images, and inline
         bold, italic, superscript / subscript and links (<xref>).
  aem/   stage only, with an AEM login: the real topic files downloaded from AEM for every topic
         GUID in the PDF (and manifest.json: GUID -> DAM path).

The running headers / footers and the printed table of contents are left out of dita/ (the map
generates them) but are in raw/.
"""
from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable
from xml.sax.saxutils import escape, quoteattr

import pymupdf

from . import extract, normalize, sections, toc as tocmod
from .checks import tables as tmod

NOTE_TYPES = {"note": "note", "notes": "note", "tip": "tip", "tips": "tip", "hint": "tip", "warning": "warning",
              "important": "important", "caution": "caution", "danger": "danger", "notice": "notice",
              "attention": "attention"}
_BULLET = re.compile(r"^[•●■▪◦○◆◇►▶➢✓✔\-–—*·]$")
_ENUM = re.compile(r"^\(?(?:\d{1,3}|[A-Za-z]|[ivxlcdmIVXLCDM]{1,4})[.)]$")


def export(pdf_path: str, out_zip: str | Path, cfg: dict, *, label: str = "document", aem_cfg: dict | None = None,
           progress: Callable[[float, str], None] | None = None) -> Path:
    """Write <out_zip> with raw/, dita/ and (stage, logged in) aem/. Returns its path."""
    report = progress or (lambda f, m: None)
    files: dict[str, bytes] = {}
    pdf = pymupdf.open(pdf_path)
    report(0.05, "Reading raw data")
    images = _raw(pdf, files)
    report(0.5, "Rebuilding DITA source")
    _dita(pdf_path, pdf, cfg, files, images, label)
    if aem_cfg:
        report(0.85, "Downloading the topic files from AEM")
        _aem(pdf_path, aem_cfg, files)
    files["README.txt"] = _readme(pdf_path, label, "aem/manifest.json" in files).encode()
    out = Path(out_zip)
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(files):
            z.writestr(name, files[name])
    report(1.0, "Done")
    return out


# ------------------------------------------------------------------ raw

def _raw(pdf: pymupdf.Document, files: dict) -> dict:
    """raw/: everything, unfiltered. Returns {(page, rounded bbox): image path} for the DITA figures."""
    doc_json = {"file": Path(pdf.name).name, "page_count": pdf.page_count, "metadata": pdf.metadata,
                "language": _lang(pdf), "outline": [], "named_destinations": {}, "pages": [], "fonts": [],
                "attachments": []}
    try:
        xmp = pdf.get_xml_metadata()
        if xmp:
            files["raw/metadata.xmp"] = xmp.encode()
    except Exception:
        pass
    for item in pdf.get_toc(simple=False):
        lvl, title, page = item[:3]
        dest = item[3] if len(item) > 3 else {}
        doc_json["outline"].append({"level": lvl, "title": title, "page": page,
                                    "named": dest.get("nameddest") if isinstance(dest, dict) else None})
    try:
        for name, d in pdf.resolve_names().items():
            doc_json["named_destinations"][name] = {"page": d.get("page", -1) + 1,
                                                    "to": list(d["to"]) if d.get("to") else None}
    except Exception:
        pass

    fonts: dict[str, dict] = {}
    images: dict = {}
    seen_xref: dict[int, str] = {}
    text_pages, link_rows = [], []
    for pno, page in enumerate(pdf):
        pj = {"number": pno + 1, "width": round(page.rect.width, 2), "height": round(page.rect.height, 2),
              "rotation": page.rotation, "lines": [], "images": [], "links": [], "tables": [],
              "vector_drawings": 0}
        for b in page.get_text("dict", sort=True)["blocks"]:
            for ln in b.get("lines", []):
                spans = []
                for s in ln["spans"]:
                    spans.append({"text": s["text"], "font": s["font"], "size": round(s["size"], 2),
                                  "color": f"#{s['color']:06x}", "bold": bool(s["flags"] & 16),
                                  "italic": bool(s["flags"] & 2), "superscript_flag": bool(s["flags"] & 1),
                                  "origin": [round(v, 2) for v in s["origin"]],
                                  "bbox": [round(v, 2) for v in s["bbox"]]})
                    f = fonts.setdefault(s["font"], {"font": s["font"], "sizes": set(), "pages": set(), "chars": 0})
                    f["sizes"].add(round(s["size"], 1))
                    f["pages"].add(pno + 1)
                    f["chars"] += len(s["text"])
                pj["lines"].append({"text": "".join(s["text"] for s in ln["spans"]),
                                    "bbox": [round(v, 2) for v in ln["bbox"]], "block": b["number"],
                                    "dir": [round(v, 3) for v in ln["dir"]], "spans": spans})
        for k, info in enumerate(page.get_image_info(xrefs=True)):
            xref = info.get("xref") or 0
            path = seen_xref.get(xref) if xref else None
            if xref and path is None:
                try:
                    img = pdf.extract_image(xref)
                    path = f"raw/images/p{pno + 1}_img{k + 1}_x{xref}.{img['ext']}"
                    files[path] = img["image"]
                    seen_xref[xref] = path
                except Exception:
                    path = None
            if path is None:  # inline image or unreadable: render its area
                r = pymupdf.Rect(info["bbox"]) & page.rect
                if r.width > 2 and r.height > 2:
                    path = f"raw/images/p{pno + 1}_img{k + 1}_render.png"
                    files[path] = page.get_pixmap(clip=r, dpi=200).tobytes("png")
            box = [round(v, 2) for v in info["bbox"]]
            pj["images"].append({"bbox": box, "xref": xref, "pixels": [info.get("width"), info.get("height")],
                                 "file": path})
            if path:
                images[(pno, tuple(round(v) for v in info["bbox"]))] = path
        for ln in page.get_links():
            kind = {pymupdf.LINK_URI: "uri", pymupdf.LINK_GOTO: "goto", pymupdf.LINK_NAMED: "named",
                    pymupdf.LINK_GOTOR: "file", pymupdf.LINK_LAUNCH: "launch"}.get(ln["kind"], str(ln["kind"]))
            text = page.get_textbox(ln["from"]).strip()
            entry = {"bbox": [round(v, 2) for v in ln["from"]], "kind": kind, "text": text,
                     "uri": ln.get("uri"), "to_page": ln["page"] + 1 if ln.get("page", -1) >= 0 else None,
                     "name": ln.get("nameddest") or ln.get("name")}
            pj["links"].append(entry)
            link_rows.append([pno + 1, kind, text, entry["uri"] or "", entry["to_page"] or "", entry["name"] or ""])
        for t, tb, rows, grid in tmod.detect(page):
            cells = _table_cells(page, tb, rows, grid)
            pj["tables"].append({"bbox": [round(v, 2) for v in tb], "rows": cells})
            buf = io.StringIO()
            csv.writer(buf).writerows(cells)
            files[f"raw/tables/p{pno + 1}_table{t + 1}.csv"] = ("﻿" + buf.getvalue()).encode()
        try:
            pj["vector_drawings"] = len(page.get_drawings())
        except Exception:
            pass
        doc_json["pages"].append(pj)
        text_pages.append(f"===== page {pno + 1} =====\n{page.get_text('text', sort=True)}")

    for i in range(pdf.embfile_count()):
        info = pdf.embfile_info(i)
        name = info.get("filename") or info.get("name") or f"attachment{i + 1}"
        files[f"raw/attachments/{name}"] = pdf.embfile_get(i)
        doc_json["attachments"].append(name)
    for f in fonts.values():
        f["sizes"], f["pages"] = sorted(f["sizes"]), sorted(f["pages"])
    doc_json["fonts"] = sorted(fonts.values(), key=lambda f: -f["chars"])
    try:
        embedded = {f[3]: f[2] for p in pdf for f in p.get_fonts(full=True)}  # basefont -> type
    except Exception:
        embedded = {}

    files["raw/document.json"] = json.dumps(doc_json, ensure_ascii=False, indent=1).encode()
    files["raw/text.txt"] = "\n".join(text_pages).encode()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["font", "type", "sizes (pt)", "pages", "characters"])
    for f in doc_json["fonts"]:
        w.writerow([f["font"], embedded.get(f["font"], ""), " ".join(map(str, f["sizes"])),
                    _ranges(f["pages"]), f["chars"]])
    files["raw/fonts.csv"] = ("﻿" + buf.getvalue()).encode()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["page", "kind", "text", "uri", "to page", "named destination"])
    w.writerows(link_rows)
    files["raw/links.csv"] = ("﻿" + buf.getvalue()).encode()
    return images


def _table_cells(page: pymupdf.Page, tb, rows, grid) -> list[list[str]]:
    """Text of every cell: rows from the detector, columns from the table's column grid."""
    words = [w for w in page.get_text("words", sort=True)
             if tb[0] - 1 <= (w[0] + w[2]) / 2 <= tb[2] + 1 and tb[1] - 1 <= (w[1] + w[3]) / 2 <= tb[3] + 1]
    ncol = max(len(grid), 1)
    out = []
    for rb, _ in rows:
        cells = [[] for _ in range(ncol)]
        for w in words:
            cy, cx = (w[1] + w[3]) / 2, (w[0] + w[2]) / 2
            if not rb[1] - 1 <= cy <= rb[3] + 1:
                continue
            c = _col(grid, cx)
            cells[c].append(w)
        out.append([" ".join(x[4] for x in sorted(c, key=lambda x: (round(x[1]), x[0]))) for c in cells])
    return out


def _col(grid, cx: float) -> int:
    if not grid:
        return 0
    return min(range(len(grid)), key=lambda g: 0 if grid[g][0] - 1 <= cx <= grid[g][1] + 1
               else min(abs(cx - grid[g][0]), abs(cx - grid[g][1])))


def _ranges(pages: list[int]) -> str:
    out, start = [], None
    for k, p in enumerate(pages):
        if start is None:
            start = p
        if k + 1 == len(pages) or pages[k + 1] != p + 1:
            out.append(f"{start}" if start == p else f"{start}-{p}")
            start = None
    return ", ".join(out)


def _lang(pdf: pymupdf.Document) -> str:
    try:
        v = pdf.xref_get_key(pdf.pdf_catalog(), "Lang")
        if v[0] == "string":
            return v[1]
    except Exception:
        pass
    return ""


# ------------------------------------------------------------------ DITA

def _dita(pdf_path: str, pdf: pymupdf.Document, cfg: dict, files: dict, images: dict, label: str) -> None:
    doc = extract.load(pdf_path, label, cfg)
    anchors = [a for a in sections.build_anchors(doc, cfg) if a.located]
    toc_lines = set(tocmod.detect(doc, cfg).lines)
    lang = _doc_lang(_lang(pdf), "".join(w.text for w in doc.words[:20000]))
    links = defaultdict(list)
    for pno, page in enumerate(pdf):
        for ln in page.get_links():
            links[pno].append(ln)
    tables = {p: tmod._RAW.get((doc.path, p)) or tmod.detect(pdf[p]) for p in range(len(doc.pages))}

    starts = [a.word for a in anchors]
    bounds = [(None, 0, starts[0] if starts else len(doc.words))] if not starts or starts[0] > 0 else []
    bounds += [(a, a.word, starts[k + 1] if k + 1 < len(starts) else len(doc.words)) for k, a in enumerate(anchors)]
    used_ids: Counter = Counter()
    topics = []  # (level, file, title)
    for a, w0, w1 in bounds:
        title = a.title if a else "Front matter"
        if not a and not any(doc.words[i].norm for i in range(w0, w1)):
            continue
        tid = _id(title, used_ids)
        body = _body(doc, pdf, w0, w1, a, toc_lines, tables, images, links, files)
        xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
               '<!DOCTYPE topic PUBLIC "-//OASIS//DTD DITA Topic//EN" "topic.dtd">\n'
               f'<topic id="{tid}" xml:lang="{lang}">\n  <title>{escape(title)}</title>\n'
               f'  <body>\n{body}  </body>\n</topic>\n')
        name = f"topics/{tid}.dita"
        files[f"dita/{name}"] = _xml_safe(xml).encode()
        topics.append((a.level if a else 1, name, title))

    map_title = (pdf.metadata or {}).get("title") or Path(pdf_path).stem
    map_title = re.sub(r"\.ditamap$", "", map_title)
    files[f"dita/{_id(map_title, Counter())}.ditamap"] = _xml_safe(_map(map_title, topics, lang)).encode()


_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def _xml_safe(xml: str) -> str:
    """Control characters (a glyph with a broken text mapping) are not allowed in XML: shown as �."""
    return _XML_BAD.sub("\ufffd", xml)


_SCRIPTS = [("ja", "\u3040-\u30ff"), ("ko", "\uac00-\ud7af"), ("zh", "\u4e00-\u9fff"), ("ar", "\u0600-\u06ff"),
            ("he", "\u0590-\u05ff"), ("ru", "\u0400-\u04ff"), ("th", "\u0e00-\u0e7f"), ("el", "\u0370-\u03ff")]


def _doc_lang(tag: str, text: str) -> str:
    """The PDF's language tag, unless the text is written in another script (InDesign often
    leaves a default tag): then the language of the script most of the letters are in."""
    tag = (tag or "").split("-")[0].lower()
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return tag or "en"
    counts = {code: sum(1 for c in letters if re.match(f"[{rng}]", c)) for code, rng in _SCRIPTS}
    code, n = max(counts.items(), key=lambda kv: kv[1])
    if code == "zh" and counts["ja"] > 0.05 * len(letters):
        code, n = "ja", n + counts["ja"]
    if n > 0.5 * len(letters):
        return code
    return tag if tag and tag not in dict(_SCRIPTS) else "en"  # Latin script: keep a Latin-language tag


def _map(title: str, topics: list, lang: str) -> str:
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<!DOCTYPE map PUBLIC "-//OASIS//DTD DITA Map//EN" "map.dtd">',
           f'<map xml:lang="{lang}">', f"  <title>{escape(title)}</title>"]
    stack: list[int] = []
    for level, href, t in topics:
        while stack and stack[-1] >= level:
            stack.pop()
            out.append("  " * (len(stack) + 1) + "</topicref>")
        out.append("  " * (len(stack) + 1) + f'<topicref href="{href}" navtitle={quoteattr(t)}>')
        stack.append(level)
    while stack:
        stack.pop()
        out.append("  " * (len(stack) + 1) + "</topicref>")
    out.append("</map>")
    return re.sub(r"(<topicref [^>]*)>\n\s*</topicref>", r"\1/>", "\n".join(out)) + "\n"


def _id(title: str, used: Counter) -> str:
    base = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")[:50] or "topic"
    if base[0].isdigit():
        base = "t-" + base
    used[base] += 1
    return base if used[base] == 1 else f"{base}-{used[base]}"


def _body(doc, pdf, w0: int, w1: int, anchor, toc_lines: set, tables: dict, images: dict, links: dict,
          files: dict) -> str:
    """Topic body: blocks (paragraphs, list items, notes), tables and figures in reading order."""
    # lines of this section, minus the heading itself and the printed TOC
    line_ids = []
    for i in range(w0, w1):
        li = doc.words[i].line
        if (not line_ids or line_ids[-1] != li) and li not in toc_lines:
            line_ids.append(li)
    if anchor:  # skip the heading line(s): lines whose text is the start of the title
        title = normalize.title(anchor.title)
        acc = ""
        while line_ids:
            acc = (acc + " " + normalize.title(doc.lines[line_ids[0]].text)).strip()
            if acc and title.startswith(acc[: len(title)]) and len(acc) <= len(title) + 2:
                line_ids.pop(0)
                if len(acc) >= len(title) - 2:
                    break
            else:
                break

    items = []  # (page, y, kind, payload)
    table_lines: dict[tuple, list[int]] = defaultdict(list)
    blocks: dict[tuple, list[int]] = defaultdict(list)
    for li in line_ids:
        ln = doc.lines[li]
        t = _in_table(ln, tables.get(ln.page) or [])
        if t is not None:
            table_lines[(ln.page, t)].append(li)
        else:
            blocks[ln.block].append(li)
    for key, lis in blocks.items():
        ln = doc.lines[lis[0]]
        items.append((ln.page, ln.bbox[1], "block", lis))
    for (p, t), lis in table_lines.items():
        tb = tables[p][t]
        items.append((p, tb[1][1], "table", (p, tb, lis)))
    if doc.words[w0:w1]:
        # a figure belongs to the section up to where the next one starts (it may sit below the last text)
        first = doc.words[w0]
        lo = (first.page, first.bbox[1] - 2)
        hi = (doc.words[w1].page, doc.words[w1].bbox[1]) if w1 < len(doc.words) else (len(doc.pages), 0.0)
        for im in doc.images:
            if lo <= (im.page, im.bbox[1]) < hi and not any(
                    _inside(im.bbox, tables[im.page][k][1]) for k in range(len(tables.get(im.page) or []))):
                items.append((im.page, im.bbox[1], "image", im))
    items.sort(key=lambda x: (x[0], x[1]))

    out, lst = [], None  # lst = ("ul"|"ol", [li xml])
    def flush():
        nonlocal lst
        if lst:
            out.append(f"    <{lst[0]}>\n" + "".join(f"      <li>{x}</li>\n" for x in lst[1]) + f"    </{lst[0]}>\n")
            lst = None
    for p, y, kind, data in items:
        if kind == "image":
            flush()
            href = _figure(doc, pdf, data, images, files)
            if href:
                out.append(f'    <fig><image href="{escape(href)}" placement="break"/></fig>\n')
            continue
        if kind == "table":
            flush()
            out.append(_table(doc, *data, links))
            continue
        words = [i for li in data for i in _line_words(doc, li)]
        if not words:
            continue
        first = doc.words[words[0]].text.strip()
        label = re.sub(r"[^\w]+$", "", first).lower()
        if _BULLET.match(first) or _ENUM.match(first):
            kind_l = "ol" if _ENUM.match(first) else "ul"
            if lst and lst[0] != kind_l:
                flush()
            lst = lst or (kind_l, [])
            lst[1].append(_inline(doc, words[1:], links))
            continue
        flush()
        if label in NOTE_TYPES and len(words) > 1:
            out.append(f'    <note type="{NOTE_TYPES[label]}">{_inline(doc, words[1:], links)}</note>\n')
        else:
            out.append(f"    <p>{_inline(doc, words, links)}</p>\n")
    flush()
    return "".join(out)


def _line_words(doc, li: int) -> list[int]:
    k = doc.lines[li].first_word
    out = []
    while 0 <= k < len(doc.words) and doc.words[k].line == li:
        out.append(k)
        k += 1
    return out


def _in_table(ln, found) -> int | None:
    cx, cy = (ln.bbox[0] + ln.bbox[2]) / 2, (ln.bbox[1] + ln.bbox[3]) / 2
    for k, (_, tb, _, _) in enumerate(found):
        if tb[0] - 1 <= cx <= tb[2] + 1 and tb[1] - 1 <= cy <= tb[3] + 1:
            return k
    return None


def _inside(box, tb) -> bool:
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return tb[0] <= cx <= tb[2] and tb[1] <= cy <= tb[3]


def _inline(doc, idx: list[int], links: dict) -> str:
    """Words -> DITA inline markup: <b>, <i>, <sup>/<sub>, <xref>; spacing as in the PDF."""
    if not idx:
        return ""
    weights = Counter(doc.words[i].style.weight >= 600 for i in idx)
    base_bold = weights[True] > weights[False]
    out, open_link = [], None
    for n, i in enumerate(idx):
        w = doc.words[i]
        link = _link_at(links.get(w.page, []), w.bbox)
        key = id(link) if link else None
        if key != (id(open_link) if open_link else None):
            if open_link:
                out.append("</xref>")
            open_link = link
            if link:
                out.append(_xref_open(link))
        text = _script(w)
        if w.style.italic:
            text = f"<i>{text}</i>"
        if w.style.weight >= 600 and not base_bold:
            text = f"<b>{text}</b>"
        out.append(text)
        nxt = idx[n + 1] if n + 1 < len(idx) else None
        if nxt is not None:
            glued = w.space_after == 0 or (w.space_after is None and normalize.nospace_char(w.text[-1:])
                                           and normalize.nospace_char(doc.words[nxt].text[:1]))
            if not glued:
                out.append(" ")
    if open_link:
        out.append("</xref>")
    return "".join(out).replace("</b> <b>", " ").replace("</i> <i>", " ")


def _script(w) -> str:
    if not w.script:
        return escape(w.text)
    out, k = [], 0
    while k < len(w.text):
        pos = w.script[k] if k < len(w.script) else "."
        n = k
        while n < len(w.text) and (w.script[n] if n < len(w.script) else ".") == pos:
            n += 1
        part = escape(w.text[k:n])
        out.append(f"<sup>{part}</sup>" if pos == "^" else f"<sub>{part}</sub>" if pos == "_" else part)
        k = n
    return "".join(out)


def _link_at(links: list, bbox):
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    for ln in links:
        r = ln["from"]
        if r.x0 - 1 <= cx <= r.x1 + 1 and r.y0 - 1 <= cy <= r.y1 + 1:
            return ln
    return None


def _xref_open(link) -> str:
    if link.get("kind") == pymupdf.LINK_URI and link.get("uri"):
        uri = link["uri"]
        fmt = "html" if uri.startswith(("http:", "https:")) else ""
        return f'<xref href={quoteattr(uri)} scope="external"' + (f' format="{fmt}"' if fmt else "") + ">"
    return f'<xref href="#" outputclass="page-{link.get("page", -1) + 1}">'  # internal jump (target page kept)


def _table(doc, page: int, tb_entry, lis: list[int], links: dict) -> str:
    _, tb, rows, grid = tb_entry
    ncol = max(len(grid), 1)
    cells = [[[] for _ in range(ncol)] for _ in rows]
    for li in lis:
        ln = doc.lines[li]
        cy = (ln.bbox[1] + ln.bbox[3]) / 2
        r = next((k for k, (rb, _) in enumerate(rows) if rb[1] - 1 <= cy <= rb[3] + 1), None)
        if r is None:
            continue
        for i in _line_words(doc, li):
            w = doc.words[i]
            cells[r][_col(grid, (w.bbox[0] + w.bbox[2]) / 2)].append(i)
    # a bordered note box ("[icon] | Important  ...") is a note, not a table
    filled = [c for row in cells for c in row if c]
    if len(filled) == 1:
        words = sorted(filled[0], key=lambda i: (doc.words[i].line, i))
        label = re.sub(r"[^\w]+$", "", doc.words[words[0]].text).lower()
        if label in NOTE_TYPES and len(words) > 1:
            return f'    <note type="{NOTE_TYPES[label]}">{_inline(doc, words[1:], links)}</note>\n'
        return f"    <p>{_inline(doc, words, links)}</p>\n"
    head = rows and all(doc.words[i].style.weight >= 600 for c in cells[0] for i in c) and any(cells[0])
    xml = [f'    <table>\n      <tgroup cols="{ncol}">\n']
    xml += [f'        <colspec colname="c{k + 1}"/>\n' for k in range(ncol)]
    def row_xml(r):
        return "          <row>" + "".join(f"<entry>{_inline(doc, sorted(c, key=lambda i: (doc.words[i].line, i)), links)}</entry>"
                                           for c in cells[r]) + "</row>\n"
    body_rows = range(1, len(rows)) if head else range(len(rows))
    if head:
        xml.append("        <thead>\n" + row_xml(0) + "        </thead>\n")
    xml.append("        <tbody>\n" + "".join(row_xml(r) for r in body_rows if any(cells[r])) + "        </tbody>\n")
    xml.append("      </tgroup>\n    </table>\n")
    return "".join(xml)


def _figure(doc, pdf, im, images: dict, files: dict) -> str | None:
    """Copy the image into dita/images/ and return its href relative to the topic."""
    key = (im.page, tuple(round(v) for v in im.bbox))
    src = images.get(key) or next((v for (p, b), v in images.items()
                                   if p == im.page and abs(b[0] - key[1][0]) <= 2 and abs(b[1] - key[1][1]) <= 2), None)
    if not src:
        return None
    name = src.split("/")[-1]
    files.setdefault(f"dita/images/{name}", files[src])
    return f"../images/{name}"


# ------------------------------------------------------------------ AEM

def _aem(pdf_path: str, acfg: dict, files: dict) -> None:
    """aem/: the real topic files from AEM for every topic GUID in the (stage) PDF."""
    from base64 import b64encode
    from urllib import request as _rq
    from urllib.parse import quote

    from . import aem
    anchors, info = aem.anchors(pdf_path)
    topics = [(a.guid, a.lang, a.title) for a in anchors if not a.element]
    if not topics:
        return
    manifest = {"map": info.get("map"), "author": acfg.get("author"), "topics": []}
    if not acfg.get("password"):
        manifest["note"] = "No AEM login: log in (New comparison → AEM login) to download the real topic files."
        files["aem/manifest.json"] = json.dumps({**manifest, "topics": [
            {"guid": g, "lang": l, "title": t} for g, l, t in topics]}, ensure_ascii=False, indent=1).encode()
        return
    paths, err = aem.resolve([(g, l) for g, l, _ in topics], acfg)
    auth = "Basic " + b64encode(f"{acfg['user']}:{acfg['password']}".encode()).decode()
    author = acfg["author"].rstrip("/")
    for g, l, t in topics:
        entry = {"guid": g, "lang": l, "title": t, "path": paths.get(g)}
        if entry["path"]:
            try:
                req = _rq.Request(author + quote(entry["path"]), headers={"Authorization": auth})
                with _rq.urlopen(req, timeout=30) as r:
                    files["aem/" + entry["path"].lstrip("/")] = r.read()
                entry["file"] = "aem/" + entry["path"].lstrip("/")
            except Exception as e:
                entry["error"] = str(e)
        manifest["topics"].append(entry)
    if err:
        manifest["error"] = err
    files["aem/manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=1).encode()


def _readme(pdf_path: str, label: str, has_aem: bool) -> str:
    return f"""Source export of {Path(pdf_path).name} ({label})

raw/    Everything read straight from the PDF, unfiltered:
          document.json   every text line with its spans (font, size, colour, bold, italic,
                          superscript flag, position), outline, named destinations, images,
                          links, tables (cell by cell), metadata
          text.txt        plain text of every page
          tables/*.csv    every detected table, cell by cell
          images/         the embedded images as their original files
          attachments/    embedded files
          fonts.csv       fonts, sizes and pages they are used on
          links.csv       every link with its target
          metadata.xmp    XMP metadata
dita/   The document rebuilt as DITA 1.3 source (AEM Guides can import the folder):
        one topic per heading, nested in the .ditamap like the outline; paragraphs, lists,
        notes, CALS tables, figures and inline bold / italic / superscript / links.
        Running headers / footers and the printed table of contents are left out (the map
        generates them); they are in raw/.
{"aem/    The real topic files downloaded from AEM for every topic GUID in this PDF (manifest.json)." if has_aem else ""}
"""
