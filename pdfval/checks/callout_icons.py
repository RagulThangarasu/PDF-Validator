"""The kind of a callout's icon, for a note that has no label of its own.

Prod (InDesign) often sets a note with only its icon - a pencil for a Note, an exclamation mark for a
Warning - where stage (AEM Guides) prints the label (“NOTE”, “WARNING”). The icon says what the label
would be: [content] callout_icons maps each icon kind to the labels it stands for, so a stage label is
compared with prod's icon instead of being reported as a label on one side only.

The icon is read from its drawing: rendered, its marks (light on a dark disc, or dark on a light one)
split into connected parts -
  exclamation  two parts on the icon's vertical axis: a tall bar with a small dot below it
  pencil       its marks (body, point, eraser) along one line slanted 25-65 degrees
  lightbulb    a large round bulb on the vertical axis with flat base stripes below it (a Tip)
anything else is not recognised (None): the finding stays as it was.
"""
from __future__ import annotations

import math

import numpy as np
import pymupdf

from ..model import Doc, Loc

_DOCS: dict[str, pymupdf.Document] = {}


def _pdf(doc: Doc) -> pymupdf.Document:
    return _DOCS.get(doc.path) or _DOCS.setdefault(doc.path, pymupdf.open(doc.path))


def icon_left_of(doc: Doc, at: Loc) -> tuple | None:
    """The box of the icon set left of a note's text at `at` (its first words): a square-ish picture or
    filled drawing, 10-32 pt, ending at most 50 pt before the text, level with its first lines."""
    page = _pdf(doc)[at.page]
    x0, y0 = at.bbox[0], at.bbox[1]
    h = max(at.bbox[3] - at.bbox[1], 6)
    boxes = [pymupdf.Rect(i["bbox"]) for i in page.get_image_info()]
    boxes += [d["rect"] for d in page.get_drawings() if d.get("fill") is not None]
    cands = [r for r in boxes if 10 <= r.width <= 32 and 10 <= r.height <= 32 and 0.7 <= r.width / r.height <= 1.4
             and r.x1 <= x0 + 2 and x0 - r.x1 <= 50 and r.y0 <= y0 + 3 * h and r.y1 >= y0 - 2 * h]
    if not cands:
        return None
    # the icon level with the note's first line (the next note's icon may sit a line or two lower), then its
    # outline - the largest box over it - not a mark inside it
    near = min(cands, key=lambda r: abs(r.y0 - y0))
    same = [r for r in cands if r.intersects(near) and (r & near).get_area() >= 0.5 * min(r.get_area(), near.get_area())]
    return tuple(max(same, key=lambda r: r.get_area()))


def _parts(mask: np.ndarray, min_px: int) -> list[np.ndarray]:
    """Connected parts (4-neighbour) of a boolean mask, as arrays of (y, x); parts under min_px dropped."""
    seen = np.zeros_like(mask, bool)
    out = []
    H, W = mask.shape
    for y, x in zip(*np.nonzero(mask)):
        if seen[y, x]:
            continue
        stack, pts = [(y, x)], []
        seen[y, x] = True
        while stack:
            cy, cx = stack.pop()
            pts.append((cy, cx))
            for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                if 0 <= ny < H and 0 <= nx < W and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))
        if len(pts) >= min_px:
            out.append(np.array(pts))
    return out


def kind(doc: Doc, page: int, box: tuple) -> str | None:
    """'exclamation', 'pencil', 'lightbulb' or None for the icon drawn in `box`."""
    r = pymupdf.Rect(box)
    z = 120 / max(r.width, 1)
    pix = _pdf(doc)[page].get_pixmap(clip=r, matrix=pymupdf.Matrix(z, z), colorspace=pymupdf.csGRAY, alpha=False)
    g = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.stride)[:, :pix.width].astype(np.float32)
    H, W = g.shape
    yy, xx = np.mgrid[0:H, 0:W]
    inner = ((yy - H / 2) / (H / 2)) ** 2 + ((xx - W / 2) / (W / 2)) ** 2 <= 0.8 ** 2  # inside the disc's rim
    dark = g < 128
    # a dark disc carries light marks; an outlined icon dark marks on light
    marks = (g > 170) & inner if dark[inner].mean() > 0.4 else dark & inner
    parts = sorted(_parts(marks, max(6, int(0.004 * H * W))), key=len, reverse=True)
    if not parts:
        return None
    geo = []
    for p in parts:
        ys, xs = p[:, 0], p[:, 1]
        geo.append({"cx": xs.mean(), "cy": ys.mean(), "w": np.ptp(xs) + 1, "h": np.ptp(ys) + 1, "n": len(p), "pts": p})
    # exclamation: a tall bar and a dot below it, both on the vertical axis
    if len(geo) == 2:
        bar, dot = sorted(geo, key=lambda q: q["cy"])
        if all(abs(q["cx"] - W / 2) <= 0.15 * W for q in (bar, dot)) and bar["h"] >= 2.2 * bar["w"] \
                and dot["h"] <= 0.6 * bar["h"] and dot["cy"] > bar["cy"] + bar["h"] / 2:
            return "exclamation"
    # lightbulb: a centred bulb (its body and base stripes may be one mark) that is taller than it is wide,
    # with light rays above and beside it
    big = geo[0]
    if abs(big["cx"] - W / 2) <= 0.12 * W and 0.3 * big["h"] <= big["w"] <= 0.9 * big["h"] \
            and big["h"] >= 0.3 * H and any(q["cy"] < big["cy"] for q in geo[1:]):
        return "lightbulb"
    # pencil: its marks (body, point, eraser - separate parts or one) lie along one slanted line
    pts = np.concatenate([q["pts"] for q in geo]).astype(np.float64)
    vals, vecs = np.linalg.eigh(np.cov((pts - pts.mean(0)).T))
    if vals[0] > 0 and vals[1] / vals[0] >= 2.5:
        vy, vx = vecs[:, 1]
        ang = abs(math.degrees(math.atan2(vy, vx))) % 180
        ang = min(ang, 180 - ang)  # 0 = horizontal, 90 = vertical
        if 25 <= ang <= 65:
            return "pencil"
    return None
