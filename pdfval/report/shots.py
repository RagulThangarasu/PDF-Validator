"""Per-issue screenshots: a prod crop and a stage crop of the region a finding
points at, with the finding's boxes drawn on top.

Each page is rendered once (LRU cache) and every crop is cut from that bitmap,
so ~1-2k issues take seconds rather than minutes.
"""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Callable

import pymupdf
from PIL import Image, ImageDraw, ImageFont

COLORS = {"content": (225, 29, 72), "style": (124, 58, 237), "layout": (234, 88, 12),
          "assets": (13, 148, 136), "structure": (37, 99, 235),
          "tables": (2, 132, 199), "integrity": (185, 28, 28)}
try:
    _FONT = ImageFont.load_default(size=17)
except TypeError:  # Pillow < 10.1
    _FONT = ImageFont.load_default()
PAD, MIN_H, MAX_H = 45, 150, 420  # crop: padding around the boxes, min/max crop height (pt)
SEVERITIES = {"errors": {"error"}, "warnings": {"error", "warning"}, "all": {"error", "warning", "info"}, "none": set()}


class _PageCache:
    def __init__(self, path: str, zoom: float, size: int = 8):
        self.doc, self.zoom, self.size = pymupdf.open(path), zoom, size
        self.cache: OrderedDict[int, Image.Image] = OrderedDict()

    def get(self, pno: int) -> Image.Image:
        if pno in self.cache:
            self.cache.move_to_end(pno)
            return self.cache[pno]
        pix = self.doc[pno].get_pixmap(matrix=pymupdf.Matrix(self.zoom, self.zoom), alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        self.cache[pno] = img
        if len(self.cache) > self.size:
            self.cache.popitem(last=False)
        return img


def _crop(pc: _PageCache, page: int, boxes: list, color, note: str | None,
          pad: float = PAD, min_h: float = MIN_H, max_h: float = MAX_H) -> Image.Image:
    src = pc.get(page)
    z = pc.zoom
    pw, ph = src.width / z, src.height / z
    y0 = min(b[1] for b in boxes) - pad
    y1 = max(b[3] for b in boxes) + pad
    if y1 - y0 < min_h:
        mid = (y0 + y1) / 2
        y0, y1 = mid - min_h / 2, mid + min_h / 2
    y1 = min(y1, y0 + max_h)
    y0, y1 = max(0, y0), min(ph, y1)
    img = src.crop((0, int(y0 * z), int(pw * z), int(y1 * z))).convert("RGBA")
    over = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    for b in boxes:
        r = [(b[0] - 2) * z, (b[1] - 2 - y0) * z, (b[2] + 2) * z, (b[3] + 2 - y0) * z]
        if r[3] < 0 or r[1] > img.height:
            continue
        d.rectangle(r, fill=color + (38,), outline=color + (255,), width=3)
    img = Image.alpha_composite(img, over)
    if note:
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, img.width, 30], fill=(30, 35, 48, 230))
        d.text((10, 6), note, fill=(255, 255, 255, 255), font=_FONT)
    return img.convert("RGB")


def render(result: dict, out_dir: str | Path, mode: str = "all", zoom: float = 1.6,
           progress: Callable[[float, str], None] | None = None) -> int:
    """Adds f["shots"] = {"baseline": rel, "candidate": rel} to each rendered finding."""
    want = SEVERITIES.get(mode, SEVERITIES["all"])
    # genuine issues always get screenshots (unless none at all): they go into the genuine-issues report
    todo = [(s, f) for s in result["sections"] for f in s["findings"]
            if f["severity"] in want or (want and f.get("genuine"))]
    if not todo:
        return 0
    out = Path(out_dir)
    (out / "shots").mkdir(parents=True, exist_ok=True)
    caches = {"baseline": _PageCache(result["meta"]["baseline"]["path"], zoom),
              "candidate": _PageCache(result["meta"]["candidate"]["path"], zoom)}
    for k, (s, f) in enumerate(todo):
        f["shots"] = {}
        color = COLORS.get(f["check"], (220, 38, 38))
        linked = _linked_view(f.get("links") or [])
        for side in ("baseline", "candidate"):
            locs = f[side]
            if linked:  # the same words highlighted on both sides
                page, boxes, note = linked[side]
                img = _crop(caches[side], page, boxes, color, note)
                kind = "issue"
            elif locs:
                page = locs[0]["page"]
                boxes = [l["bbox"] for l in locs if l["page"] == page]
                others = len({l["page"] for l in locs}) - 1
                note = f"+ more on {others} other page(s)" if others else None
                img = _crop(caches[side], page, boxes, color, note)
                kind = "issue"
            elif f.get(side + "_at"):  # one-sided: show the aligned position on this side
                at = f[side + "_at"]
                page = at["page"]
                where = "stage" if side == "candidate" else "prod"
                missing_sec = f["detail"].get("in_missing_section") or (
                    f["check"] == "structure" and "not found in candidate" in f["message"] and f["detail"].get("heading"))
                # (no title in the banner: its font has no CJK/Arabic glyphs; the issue text names the section)
                how = "where the missing section belongs: before the next section" if missing_sec \
                    else "aligned by surrounding text"
                img = _crop_marker(caches[side], page, at["bbox"][1], color, f"Not in {where} - marker shows {how}")
                kind = "section-slot" if missing_sec else "aligned"
            else:  # no alignment available at all: fall back to the section start
                start = s[side]["start"]
                page = start["page"]
                label = "Not present in stage" if side == "candidate" else "Not present in prod"
                img = _crop_region(caches[side], page, start["y"], f"{label} - showing section start")
                kind = "section"
            rel = f"shots/{f['id']}_{'prod' if side == 'baseline' else 'stage'}.webp"
            img.save(out / rel, "WEBP", quality=80)
            f["shots"][side] = rel
            f["shots"][side + "_page"] = page + 1
            f["shots"][side + "_kind"] = kind
        if progress and k % 20 == 0:
            progress(k / len(todo), f"Screenshots {k}/{len(todo)}")
    return len(todo)


def _linked_view(links: list) -> dict | None:
    """From (prod box, stage box) links of the same text, pick the ones both crops can
    show: same page as the first link on each side and within one crop height of it
    on both sides. Both screenshots then highlight exactly the same words."""
    if not links:
        return None
    fa, fb = links[0]
    span = MAX_H - 2 * PAD
    fits = lambda l, f: l["page"] == f["page"] and l["bbox"][3] - f["bbox"][1] <= span and f["bbox"][1] - l["bbox"][1] <= span
    shown = [(a, b) for a, b in links if fits(a, fa) and fits(b, fb)]
    hidden = len(links) - len(shown)
    note = f"+ {hidden} more place(s) outside this view" if hidden else None
    return {"baseline": (fa["page"], [a["bbox"] for a, _ in shown], note),
            "candidate": (fb["page"], [b["bbox"] for _, b in shown], note)}


def _crop_marker(pc: _PageCache, page: int, y: float, color, note: str) -> Image.Image:
    """Crop around an insertion point and draw a dashed marker line at it."""
    src, z = pc.get(page), pc.zoom
    ph = src.height / z
    y0 = max(0.0, y - 110)
    y1 = min(ph, y0 + 240)
    img = src.crop((0, int(y0 * z), src.width, int(y1 * z)))
    d = ImageDraw.Draw(img)
    my = int((y - 3 - y0) * z)
    for x in range(0, img.width, 22):
        d.line([(x, my), (x + 12, my)], fill=color, width=4)
    d.polygon([(0, my - 10), (14, my), (0, my + 10)], fill=color)
    d.rectangle([0, 0, img.width, 30], fill=(30, 35, 48))
    d.text((10, 6), note, fill=(255, 255, 255), font=_FONT)
    return img


def _crop_region(pc: _PageCache, page: int, y: float, note: str) -> Image.Image:
    src, z = pc.get(page), pc.zoom
    y0 = max(0, y - 30)
    y1 = min(src.height / z, y0 + 220)
    img = src.crop((0, int(y0 * z), src.width, int(y1 * z)))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, img.width, 30], fill=(30, 35, 48))
    d.text((10, 6), note, fill=(255, 255, 255), font=_FONT)
    return img
