"""Side-by-side page report: the run page by page, the way the viewer scrolls with sync on.

Each report page shows one prod page next to the stage page that holds the same text (paired through
the viewer's sync points), every issue on that page pair boxed and numbered on both screenshots, and
below them the numbered list of those issues with their full description. A page pair without issues
gets one line in a "no issues" list at the end, so the report stays short.

A separate report: it reads a finished run (results.json + baseline.pdf / candidate.pdf) and changes
nothing in it.
"""
from __future__ import annotations

import io
from collections import Counter
from pathlib import Path

import pymupdf
from PIL import Image as PILImage, ImageDraw, ImageFont

PAGE = pymupdf.paper_rect("a4-l")
M = 24                  # page margin (pt)
SHOT_H = 360            # height available for the two page screenshots (pt)
SEV_RGB = {"error": (220, 38, 38), "warning": (180, 83, 9), "info": (100, 116, 139)}


def _pairs(result: dict, n_a: int, n_b: int) -> list[tuple[int | None, int | None]]:
    """(prod page, stage page) in reading order: each stage page with the prod page most of its synced
    lines come from; prod pages no stage page claims (a prod-only page) get a pair of their own."""
    votes: dict[int, Counter] = {}
    for pa, _, pb, _ in result.get("sync") or []:
        votes.setdefault(pb, Counter())[pa] += 1
    pairs, last_a = [], -1
    for pb in range(n_b):
        pa = votes[pb].most_common(1)[0][0] if pb in votes else None
        if pa is None and last_a + 1 < n_a and (pb + 1 not in votes or votes[pb + 1].most_common(1)[0][0] > last_a + 1):
            pa = last_a + 1  # no matched text (a picture-only page): the prod page after the last one
        if pa is not None:
            for skipped in range(last_a + 1, pa):  # prod pages with no stage partner
                pairs.append((skipped, None))
            last_a = max(last_a, pa)
        pairs.append((pa, pb))
    for skipped in range(last_a + 1, n_a):
        pairs.append((skipped, None))
    return pairs


def _issues(result: dict, genuine_only: bool) -> list[tuple[dict, dict]]:
    out = []
    for s in result["sections"]:
        for f in s["findings"]:
            if genuine_only and not f.get("genuine"):
                continue
            out.append((s, f))
    return out


def _locs(f: dict, side: str) -> list[dict]:
    locs = list(f.get(side) or [])
    at = f.get(side + "_at")
    if not locs and at:
        locs = [{**at, "at": True}]  # not on this side: where it would be
    return locs


def _pictures(pdf: pymupdf.Document, page: int) -> list:
    """Embedded images and drawn illustrations of a page (as the screenshots see them)."""
    pg, out = pdf[page], []
    try:
        out += [tuple(i["bbox"]) for i in pg.get_image_info() if i["bbox"][3] - i["bbox"][1] >= 30]
        out += [tuple(r) for r in pg.cluster_drawings() if r.height >= 40 and r.width >= 40 and r.height < 0.8 * pg.rect.height]
    except Exception:
        pass
    return out


def _picture_boxes(pdf: pymupdf.Document, page: int, marks: list) -> list:
    """An image issue is marked by one box around the picture, not by boxes on the text inside it."""
    from .shots import IMAGE_KINDS
    pics, out, seen = None, [], set()
    for num, loc, sev, types in marks:
        if set(types) & IMAGE_KINDS and not loc.get("at"):
            pics = pics if pics is not None else _pictures(pdf, page)
            b = loc["bbox"]
            cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
            hit = next((r for r in pics if r[0] - 2 <= cx <= r[2] + 2 and r[1] - 2 <= cy <= r[3] + 2), None)
            if hit:
                if (num, hit) in seen:
                    continue
                seen.add((num, hit))
                loc = {**loc, "bbox": list(hit)}
        out.append((num, loc, sev))
    return out


def _render(pdf: pymupdf.Document, page: int, marks: list[tuple[int, dict, str]], zoom: float = 1.6) -> bytes:
    """The page as PNG with each issue's boxes and its number drawn on it."""
    pix = pdf[page].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    im = PILImage.frombytes("RGB", (pix.width, pix.height), pix.samples)
    d = ImageDraw.Draw(im, "RGBA")
    try:
        font = ImageFont.truetype("Arial.ttf", int(11 * zoom))
    except OSError:
        font = ImageFont.load_default()
    labelled = set()
    for num, loc, sev in marks:
        x0, y0, x1, y1 = (v * zoom for v in loc["bbox"])
        rgb = SEV_RGB.get(sev, SEV_RGB["error"])
        if loc.get("at"):  # missing on this side: a dashed line where it would be
            for x in range(int(x0), int(max(x1, x0 + 40)), 8):
                d.line([(x, y0), (x + 4, y0)], fill=rgb + (255,), width=3)
        else:
            d.rectangle([x0 - 2, y0 - 2, x1 + 2, y1 + 2], outline=rgb + (255,), width=3, fill=rgb + (28,))
        if num not in labelled:  # the number once per issue and page, at its first box
            labelled.add(num)
            t = str(num)
            w = d.textlength(t, font=font) + 8
            bx, by = max(0, x0 - w - 2), max(0, y0 - 2)
            d.rectangle([bx, by, bx + w, by + 15 * zoom], fill=rgb + (255,))
            d.text((bx + 4, by + 1), t, fill=(255, 255, 255), font=font)
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


class _Out:
    def __init__(self, doc: pymupdf.Document):
        self.doc = doc
        # falls back to a core PDF font (no CJK glyphs) if the bundled Noto font fails to load - a
        # race in mupdf's own font-resource extraction under concurrent/parallel runs, not fatal
        try:
            self.regular, self.bold = pymupdf.Font("notos"), pymupdf.Font("notosbo")
        except Exception:
            self.regular = self.bold = pymupdf.Font("helv")
        self.page = None
        self.y = 0.0

    def new(self):
        self.page = self.doc.new_page(width=PAGE.width, height=PAGE.height)
        self.page.insert_font(fontname="nr", fontbuffer=self.regular.buffer)
        self.page.insert_font(fontname="nb", fontbuffer=self.bold.buffer)
        self.y = M

    def text(self, x: float, s: str, size: float, bold: bool = False, color=(0.11, 0.14, 0.19), width: float | None = None):
        """Wrapped text from the current y; returns the height used."""
        font, width = (self.bold if bold else self.regular), width or (PAGE.width - M - x)
        s = s.replace("↔", "<->").replace("⇄", "<->").replace("⇆", "<->").replace("→", "->")  # not in the Noto font
        lines, cur = [], ""
        for para in s.split("\n"):
            cur = ""
            for word in para.split(" "):
                trial = (cur + " " + word).strip()
                if font.text_length(trial, size) <= width or not cur:
                    cur = trial
                else:
                    lines.append(cur)
                    cur = word
            lines.append(cur)
        h = 0.0
        for ln in lines:
            if self.y + size * 1.3 > PAGE.height - M:
                self.new()
            self.page.insert_text((x, self.y + size), ln, fontname="nb" if bold else "nr", fontsize=size, color=color)
            self.y += size * 1.3
            h += size * 1.3
        return h


def build(result: dict, run_dir: str | Path, filename: str = "side-by-side.pdf", genuine_only: bool = True) -> Path:
    run_dir = Path(run_dir)
    pa_doc = pymupdf.open(run_dir / "baseline.pdf" if (run_dir / "baseline.pdf").exists() else result["meta"]["baseline"]["path"])
    pb_doc = pymupdf.open(run_dir / "candidate.pdf" if (run_dir / "candidate.pdf").exists() else result["meta"]["candidate"]["path"])
    issues = _issues(result, genuine_only)
    pairs = _pairs(result, len(pa_doc), len(pb_doc))
    out_doc = pymupdf.open()
    o = _Out(out_doc)
    meta = result["meta"]

    # cover: what this report is, and the page pairs with their issue counts
    o.new()
    o.text(M, "Side-by-side page report", 20, bold=True)
    o.y += 4
    o.text(M, f"Prod: {Path(meta['baseline']['path']).name}   |   Stage: {Path(meta['candidate']['path']).name}", 9,
           color=(0.42, 0.45, 0.51))
    o.text(M, ("Each page: the prod page beside the stage page with the same text (as the viewer scrolls with sync on), "
               "every " + ("genuine " if genuine_only else "") + "issue on that pair boxed and numbered on both pages, "
               "and the issues listed below with their full description. Red = error, amber = warning; a dashed line "
               "marks where something missing on that side would be."), 9, color=(0.42, 0.45, 0.51))
    o.y += 8

    per_pair, empty = [], []
    for pa, pb in pairs:
        here = [(s, f) for s, f in issues
                if (pa is not None and any(l["page"] == pa for l in _locs(f, "baseline")))
                or (pb is not None and any(l["page"] == pb for l in _locs(f, "candidate")))]
        (per_pair if here else empty).append((pa, pb, here))
    o.text(M, f"{len(per_pair)} page pair(s) with issues · {len(empty)} without · {len(issues)} issue(s)", 11, bold=True)

    lbl = lambda p: f"p.{p + 1}" if p is not None else "—"
    for pa, pb, here in per_pair:
        o.new()
        o.text(M, f"Prod {lbl(pa)}  |  Stage {lbl(pb)}   ·   {len(here)} issue(s)", 13, bold=True)
        top = o.y + 4
        col_w = (PAGE.width - 2 * M - 16) / 2
        for k, (doc, p, side) in enumerate(((pa_doc, pa, "baseline"), (pb_doc, pb, "candidate"))):
            x = M + k * (col_w + 16)
            o.page.insert_text((x, top + 8), "PROD" if k == 0 else "STAGE", fontname="nb", fontsize=8, color=(0.4, 0.45, 0.5))
            if p is None:
                o.page.insert_text((x, top + 40), "No matching page on this side", fontname="nr", fontsize=10, color=(0.4, 0.45, 0.5))
                continue
            marks = [(n + 1, l, f["severity"], f.get("types") or []) for n, (s, f) in enumerate(here)
                     for l in _locs(f, side) if l["page"] == p]
            png = _render(doc, p, _picture_boxes(doc, p, marks))
            r = doc[p].rect
            scale = min(col_w / r.width, SHOT_H / r.height)
            rect = pymupdf.Rect(x, top + 12, x + r.width * scale, top + 12 + r.height * scale)
            o.page.insert_image(rect, stream=png)
            o.page.draw_rect(rect, color=(0.82, 0.84, 0.87), width=0.6)
        o.y = top + 12 + SHOT_H + 10
        for n, (s, f) in enumerate(here, 1):
            sev = f["severity"]
            rgb = tuple(v / 255 for v in SEV_RGB.get(sev, SEV_RGB["error"]))
            if o.y + 24 > PAGE.height - M:
                o.new()
                o.text(M, f"Prod {lbl(pa)}  |  Stage {lbl(pb)} (continued)", 10, bold=True)
            head = (f"{n}. {f.get('issue') or ', '.join(f.get('types') or [])}  ·  {sev}"
                    + ("  ·  CRITICAL" if f.get("critical") else "") + f"  ·  #{f['id']}  ·  {s['title']}")
            o.text(M, head, 8.5, bold=True, color=rgb)
            o.text(M + 12, f.get("description") or f["message"], 8)
            o.y += 3

    if empty:
        o.new()
        o.text(M, "Page pairs without issues", 13, bold=True)
        o.text(M, ", ".join(f"prod {lbl(pa)} / stage {lbl(pb)}" for pa, pb, _ in empty), 9)

    for k, pg in enumerate(out_doc):
        pg.insert_text((PAGE.width - M - 60, PAGE.height - 10), f"Page {k + 1} / {len(out_doc)}", fontsize=7,
                       color=(0.42, 0.45, 0.51))
    path = run_dir / filename
    out_doc.save(path, garbage=3, deflate=True)
    return path
