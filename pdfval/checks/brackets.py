"""What sits inside round brackets: a step number drawn as a circled digit "(❶)", a key icon "(⏻)".

The number may be live text, a drawn circle with a digit on it, or a small picture, and a picture
has no text for the content check to compare. So the brackets are compared by geometry: on each
side, the gap between "(" and the number and between the number and ")", in em of the text size.
Reported (stage against prod):
  - the number is not centred between the brackets in stage (prod is)
  - the space inside the brackets differs from prod
  - the brackets are empty in stage (prod has a number or icon in them)
"""
from __future__ import annotations

from functools import lru_cache

import pymupdf

from ..model import Doc, Finding, Loc
from . import Unit, locs


@lru_cache(maxsize=512)
def _marks(path: str, page: int) -> tuple:
    """Small drawn shapes and pictures on the page (candidates for a number drawn between brackets)."""
    try:
        pg = pymupdf.open(path)[page]
        out = [tuple(pymupdf.Rect(d["rect"])) for d in pg.get_drawings()
               if 2 <= pymupdf.Rect(d["rect"]).width <= 30 and 2 <= pymupdf.Rect(d["rect"]).height <= 30]
        out += [tuple(i["bbox"]) for i in pg.get_image_info()
                if 2 <= i["bbox"][2] - i["bbox"][0] <= 30 and 2 <= i["bbox"][3] - i["bbox"][1] <= 30]
        return tuple(out)
    except Exception:
        return ()


def _inside(doc: Doc, i: int) -> dict | None:
    """For an opening bracket word i: its closing bracket on the same row and what is between them
    ({"open", "close", "inner" (x0, x1) or None, "size"}); None when there is no closing bracket close by."""
    wo = doc.words[i]
    if not wo.text.endswith("("):
        return None
    size = wo.style.size
    y0, y1 = wo.bbox[1], wo.bbox[3]
    close = None
    for k in range(i + 1, min(i + 8, len(doc.words))):
        wk = doc.words[k]
        if wk.page != wo.page:
            break
        if wk.text.startswith(")") and wk.bbox[1] < y1 and wk.bbox[3] > y0 and 0 < wk.bbox[0] - wo.bbox[2] <= 3 * size:
            close = wk
            break
    if close is None:
        return None
    gx0, gx1 = wo.bbox[2], close.bbox[0]
    band = (gx0 - 0.5, y0 - 0.4 * size, gx1 + 0.5, y1 + 0.4 * size)
    boxes = [w.bbox for w in doc.words if w.page == wo.page and w is not wo and w is not close
             and w.bbox[0] >= band[0] and w.bbox[2] <= band[2] and w.bbox[1] < band[3] and w.bbox[3] > band[1]]
    boxes += [b for b in _marks(doc.path, wo.page)
              if b[0] >= band[0] and b[2] <= band[2] and b[1] < band[3] and b[3] > band[1]]
    inner = (min(b[0] for b in boxes), max(b[2] for b in boxes)) if boxes else None
    return {"open": wo, "close": close, "inner": inner, "size": size}


def _ink_gaps(doc: Doc, s: dict) -> tuple[float, float] | None:
    """The white space between the ink of "(", the number and ")" on the rendered page, in em
    (glyph boxes include their side bearings and a picture its padding, so boxes alone mislead)."""
    import numpy as np
    wo, wc = s["open"], s["close"]
    z = 12.0
    y0 = min(wo.bbox[1], wc.bbox[1]) - 0.2 * s["size"]
    y1 = max(wo.bbox[3], wc.bbox[3]) + 0.2 * s["size"]
    clip = pymupdf.Rect(wo.bbox[0], y0, wc.bbox[0] + 0.45 * s["size"], y1)
    try:
        pg = pymupdf.open(doc.path)[wo.page]
        pix = pg.get_pixmap(clip=clip, matrix=pymupdf.Matrix(z, z), colorspace=pymupdf.csGRAY, alpha=False)
    except Exception:
        return None
    g = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.stride)[:, :pix.width]
    ink = (g < 170).any(axis=0)
    runs, start = [], None
    for x, v in enumerate(list(ink) + [False]):
        if v and start is None:
            start = x
        elif not v and start is not None:
            runs.append((start, x - 1))
            start = None
    if len(runs) < 3:
        return None
    # the first run is "(", the last ")" (the clip ends just after its stroke); the number is in between
    lg = (runs[1][0] - runs[0][1] - 1) / z / s["size"]
    rg = (runs[-1][0] - runs[-2][1] - 1) / z / s["size"]
    return lg, rg


def check(u: Unit) -> list[Finding]:
    cfg = u.cfg.get("brackets", {})
    if not cfg.get("enabled", True):
        return []
    tol_em = cfg.get("tolerance_em", 0.12)
    sev = cfg.get("severity", "warning")
    rcfg = u.cfg["report"]
    fwd = dict(u.pairs + u.style_pairs)
    findings = []
    for i in range(*u.a_range):
        if not u.a.words[i].text.endswith("(") or i not in fwd:
            continue
        a, b = _inside(u.a, i), _inside(u.b, fwd[i])
        if a is None or b is None or a["inner"] is None:
            continue  # prod has nothing measurable between the brackets
        j = fwd[i]
        idx_a = [i] + [k for k in range(i + 1, i + 8) if k < len(u.a.words) and u.a.words[k] is a["close"]]
        idx_b = [j] + [k for k in range(j + 1, j + 8) if k < len(u.b.words) and u.b.words[k] is b["close"]]
        gaps = lambda s, d: _ink_gaps(d, s) or ((s["inner"][0] - s["open"].bbox[2]) / s["size"],
                                                 (s["close"].bbox[0] - s["inner"][1]) / s["size"])
        la, ra = gaps(a, u.a)
        where = f"(prod p.{u.a.words[i].page + 1} ↔ stage p.{u.b.words[j].page + 1}), in “{_line(u.a, i)}”"
        if b["inner"] is None:
            findings.append(Finding(
                "content", "error", f"Nothing inside ( ) in stage: prod has a number / icon between the brackets {where}",
                locs(u.a, idx_a, rcfg["max_locs"]), locs(u.b, idx_b, rcfg["max_locs"]),
                {"kind": "bracket-empty", "baseline_gaps_em": [round(la, 2), round(ra, 2)],
                 "expected": f"A number / icon between the brackets, space {la:.2f} em left and {ra:.2f} em right (prod)",
                 "actual": "Nothing between the brackets (stage)"}, types=["bracket spacing"]))
            continue
        lb, rb = gaps(b, u.b)
        issues = []
        if abs(lb - rb) > tol_em and abs(lb - rb) > abs(la - ra) + tol_em:
            issues.append(f"not centred in stage: space left {lb:.2f} em, right {rb:.2f} em (prod {la:.2f} / {ra:.2f} em)")
        elif abs(lb - la) > tol_em or abs(rb - ra) > tol_em:
            issues.append(f"space inside the brackets differs: stage {lb:.2f} / {rb:.2f} em, prod {la:.2f} / {ra:.2f} em "
                          f"(left / right of the number)")
        if issues:
            box_a = (a["open"].bbox[0], a["open"].bbox[1], a["close"].bbox[2], a["close"].bbox[3])
            box_b = (b["open"].bbox[0], b["open"].bbox[1], b["close"].bbox[2], b["close"].bbox[3])
            findings.append(Finding(
                "content", sev, f"Number inside ( ) {issues[0]} {where}",
                [Loc(a["open"].page, box_a)], [Loc(b["open"].page, box_b)],
                {"kind": "bracket-spacing", "baseline_gaps_em": [round(la, 2), round(ra, 2)],
                 "candidate_gaps_em": [round(lb, 2), round(rb, 2)],
                 "expected": f"Space inside ( ): {la:.2f} em left, {ra:.2f} em right of the number (prod)",
                 "actual": f"Space inside ( ): {lb:.2f} em left, {rb:.2f} em right of the number (stage)"},
                types=["bracket spacing"], links=[(Loc(a["open"].page, box_a), Loc(b["open"].page, box_b))]))
    return findings


def _line(doc: Doc, i: int) -> str:
    w = doc.words[i]
    ws = [x.text for x in doc.words[max(0, i - 6):i + 4] if x.page == w.page and abs(x.bbox[1] - w.bbox[1]) < 2 * w.style.size]
    return " ".join(ws)
