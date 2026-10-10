"""Icons set inside a line of text (“press [button] / [button] for confirmation”): each prod icon is looked up
in stage at the same place in the sentence - after / before the same word - and compared there:

* icon missing  - stage has no picture at that place;
* icon differs  - stage shows another picture;
* icon pixelated - stage's icon is a low-resolution bitmap where prod's is drawn sharp (vector, or a bitmap
                   with clearly more pixels): it blurs as soon as the page is zoomed or printed.

An icon is a small picture (bitmap or drawing) level with a line of text and right beside one of its words -
in running text and in table cells alike. The big pictures are the image check's; whether a table row has all its
icons, and the icon of a note box, have their own checks."""
from __future__ import annotations

from collections import defaultdict

import pymupdf

from ..model import Finding, Loc
from . import Unit

_ICONS: dict = {}
_LOOK: dict = {}
_DOCS: dict = {}


def _pdf(path: str):
    return _DOCS.get(path) or _DOCS.setdefault(path, pymupdf.open(path))


def reset() -> None:
    for d in _DOCS.values():
        d.close()
    for c in (_ICONS, _LOOK, _DOCS):
        c.clear()


def _icons(doc, page: int, lo: float, hi: float) -> list[tuple]:
    """(box, dpi): dpi is None for a drawn (vector) icon."""
    key = (doc.path, page)
    if key not in _ICONS:
        out = []
        size_ok = lambda b: lo <= b[2] - b[0] <= hi and lo <= b[3] - b[1] <= hi and 0.4 <= (b[2] - b[0]) / max(b[3] - b[1], 1e-6) <= 2.5
        try:
            pg = _pdf(doc.path)[page]
            for i in pg.get_image_info():
                b = tuple(i["bbox"])
                if size_ok(b):
                    out.append((b, i["width"] / max((b[2] - b[0]) / 72, 1e-6)))
            ds = [d for d in pg.get_drawings() if d["rect"].width <= hi and d["rect"].height <= hi
                  and (d["rect"].width >= 1.5 or d["rect"].height >= 1.5)]
            for r in (pg.cluster_drawings(drawings=ds, x_tolerance=1.5, y_tolerance=1.5) if ds else []):
                b = tuple(r)
                if size_ok(b) and not any(min(b[2], o[2]) > max(b[0], o[0]) and min(b[3], o[3]) > max(b[1], o[1]) for o, _ in out):
                    out.append((b, None))
        except Exception:
            pass
        _ICONS[key] = out
    return _ICONS[key]


def _look(doc, page: int, box: tuple):
    """The icon as a small grey picture, stretched to full contrast (the same icon drawn lighter is the same)."""
    key = (doc.path, page, tuple(round(v, 1) for v in box))
    if key not in _LOOK:
        from PIL import Image as PILImage, ImageOps
        pix = _pdf(doc.path)[page].get_pixmap(clip=pymupdf.Rect(box), dpi=300, colorspace=pymupdf.csGRAY, alpha=False)
        im = PILImage.frombytes("L", (pix.width, pix.height), pix.samples).resize((24, 24), PILImage.LANCZOS)
        _LOOK[key] = list(ImageOps.autocontrast(im).getdata())
    return _LOOK[key]


def _highlight(doc, page: int, box: tuple, n: int = 24) -> set:
    """The lit part of an icon: its brightest pixels (the highlighted arrow / centre of a dark button), without the
    frame around the icon. Empty when nothing stands out (a greyed-out icon)."""
    key = (doc.path, page, tuple(round(v, 1) for v in box), "hl")
    if key not in _LOOK:
        from PIL import Image as PILImage, ImageOps
        pix = _pdf(doc.path)[page].get_pixmap(clip=pymupdf.Rect(box), dpi=300, colorspace=pymupdf.csGRAY, alpha=False)
        im = ImageOps.autocontrast(PILImage.frombytes("L", (pix.width, pix.height), pix.samples).resize((n, n), PILImage.LANCZOS))
        px = im.tobytes()
        inner = [(y, x) for y in range(3, n - 3) for x in range(3, n - 3)]
        top = max(px[y * n + x] for y, x in inner)
        _LOOK[key] = {(y, x) for y, x in inner if px[y * n + x] >= 0.75 * top and px[y * n + x] >= 150}
    return _LOOK[key]


def _highlight_differs(a, pa: int, ba: tuple, b, pb: int, bb: tuple) -> bool:
    """Both icons have a lit part, and neither one's lies (within a pixel) where the other's does."""
    ma, mb = _highlight(a, pa, ba), _highlight(b, pb, bb)
    if len(ma) < 4 or len(mb) < 4 or max(len(ma), len(mb)) > 150:  # nothing lit, or a light icon (not a lit part)
        return False
    grow = lambda m: {(y + dy, x + dx) for y, x in m for dy in (-1, 0, 1) for dx in (-1, 0, 1)}
    cover = lambda m, o: len(m & grow(o)) / len(m)
    return max(cover(ma, mb), cover(mb, ma)) < 0.3


def _beside(doc, words: list[int], box: tuple, reach: float):
    """(word before, word after) the icon on its line - a word level with the icon, at most `reach` away."""
    cy = (box[1] + box[3]) / 2
    before = after = None
    for i in words:
        b = doc.words[i].bbox
        if not (b[1] - 3 <= cy <= b[3] + 3):
            continue
        if b[2] <= box[0] + 2 and box[0] - b[2] <= reach and (before is None or b[2] > doc.words[before].bbox[2]):
            before = i
        if b[0] >= box[2] - 2 and b[0] - box[2] <= reach and (after is None or b[0] < doc.words[after].bbox[0]):
            after = i
    return before, after


def check(u: Unit) -> list[Finding]:
    cfg = u.cfg.get("inline_icons") or {}
    if not cfg.get("enabled", True):
        return []
    A, B = u.a, u.b
    lo, hi, reach = cfg.get("min_pt", 6), cfg.get("max_pt", 30), cfg.get("reach_pt", 14)
    a2b = dict(u.pairs)
    by_page: dict = defaultdict(list)
    for i in range(*u.a_range):
        if A.words[i].norm:
            by_page[A.words[i].page].append(i)
    b_by_page: dict = defaultdict(list)
    for j in range(*u.b_range):
        if B.words[j].norm:
            b_by_page[B.words[j].page].append(j)
    from .tables import _raw
    in_table = lambda doc, page, box: any(t[1][0] <= (box[0] + box[2]) / 2 <= t[1][2] and t[1][1] <= (box[1] + box[3]) / 2 <= t[1][3]
                                           for t in _raw(doc, page))
    found: dict = defaultdict(list)  # kind -> [(prod box, page, stage loc, text)]
    for page, words in sorted(by_page.items()):
        for box, dpi in _icons(A, page, lo, hi):
            # an icon in a table cell is compared too (the same icon? sharp?); whether a row has all its icons is
            # the table check's, so a missing one is not reported from here a second time
            tabled = in_table(A, page, box)
            before, after = _beside(A, words, box, reach)
            jb, ja = a2b.get(before) if before is not None else None, a2b.get(after) if after is not None else None
            if jb is None and ja is None:
                continue
            where = (f"after “{A.words[before].text}”" if before is not None and jb is not None else f"before “{A.words[after].text}”")
            hit = None
            for j, side in ((jb, "after"), (ja, "before")):
                if j is None:
                    continue
                w = B.words[j]
                # the stage icon may be a shrunk version of prod's (a real size regression): searched for down
                # to a quarter of the usual floor, or it falls below `lo` and reads as "missing" instead of
                # "smaller" - the one case this check exists to catch
                for sbox, sdpi in _icons(B, w.page, lo * 0.25, hi):
                    cy = (sbox[1] + sbox[3]) / 2
                    gap = sbox[0] - w.bbox[2] if side == "after" else w.bbox[0] - sbox[2]
                    if w.bbox[1] - 4 <= cy <= w.bbox[3] + 4 and -3 <= gap <= reach + 4:
                        hit = (w.page, sbox, sdpi)
                        break
                if hit:
                    break
            if hit is None:
                # nothing at that place: only when the two words around the icon follow each other in stage too
                # (no stage text between them that could be the icon set as a symbol of a font)
                if not tabled and jb is not None and ja is not None and ja == jb + 1 and B.words[jb].page == B.words[ja].page:
                    wb, wa = B.words[jb], B.words[ja]
                    gapbox = (wb.bbox[2], min(wb.bbox[1], wa.bbox[1]), max(wa.bbox[0], wb.bbox[2] + 4), max(wb.bbox[3], wa.bbox[3])) \
                        if abs(wb.bbox[1] - wa.bbox[1]) < 4 and wa.bbox[0] > wb.bbox[2] else tuple(wb.bbox)
                    found["missing"].append((box, page, Loc(wb.page, gapbox), where))
                continue
            spage, sbox, sdpi = hit
            la, lb = _look(A, page, box), _look(B, spage, sbox)
            diff = sum(abs(x - y) for x, y in zip(la, lb)) / (255 * len(la))
            if diff <= cfg.get("differs_over", 0.30) and cfg.get("check_highlight", True) \
                    and _highlight_differs(A, page, box, B, spage, sbox):
                # the same button drawn with another part lit (the 5-way controller: its right arrow in prod, its
                # centre or its up arrow in stage) - too small a part of the icon to move the overall difference
                found["highlight"].append((box, page, Loc(spage, sbox), where))
            if diff > cfg.get("differs_over", 0.30):
                found["differs"].append((box, page, Loc(spage, sbox), where))
            else:
                # the same icon (by look), so a size difference is real, not a side-effect of it being another
                # icon entirely - compared by area (both dimensions can shrink without the aspect ratio moving)
                area_a = max((box[2] - box[0]) * (box[3] - box[1]), 1e-6)
                area_b = (sbox[2] - sbox[0]) * (sbox[3] - sbox[1])
                ratio = (area_b / area_a) ** 0.5
                if ratio < cfg.get("smaller_than", 0.75):
                    found["smaller"].append((box, page, Loc(spage, sbox), f"{where} ({ratio:.0%} of prod's size)"))
                elif ratio > cfg.get("bigger_than", 1.34):
                    found["bigger"].append((box, page, Loc(spage, sbox), f"{where} ({ratio:.0%} of prod's size)"))
                elif sdpi is not None and sdpi < cfg.get("min_dpi", 200) and (dpi is None or dpi >= 1.4 * sdpi):
                    found["pixelated"].append((box, page, Loc(spage, sbox),
                                               f"{where} ({sdpi:.0f} dpi; prod: {'vector' if dpi is None else f'{dpi:.0f} dpi'})"))
    out = []
    text = {"missing": ("icon missing inline", "Icon missing in stage", "the icon in the sentence in prod is not shown in stage"),
            "highlight": ("icon differs", "Icon differs in stage",
                          "the same button with another part highlighted than in prod (e.g. another arrow of the 5-way controller)"),
            "differs": ("icon differs", "Icon differs in stage", "stage shows another icon than prod at the same place in the sentence"),
            "smaller": ("icon smaller", "Icon smaller in stage", "the icon in the sentence is smaller in stage than in prod"),
            "bigger": ("icon bigger", "Icon bigger in stage", "the icon in the sentence is bigger in stage than in prod"),
            "pixelated": ("icon pixelated", "Icon pixelated in stage",
                          "stage's icon is a low-resolution bitmap (blurs when zoomed or printed) where prod's is sharp")}
    for kind, items in found.items():
        typ, head, why = text[kind]
        if kind == "highlight":
            # one finding per icon: each is shown and marked on its own prod and stage screenshot
            for b, pg, loc, where in items:
                out.append(Finding(
                    "assets", cfg.get("severity", "warning"),
                    f"{head}: {why} - {where} (prod p.{pg + 1} ↔ stage p.{loc.page + 1})",
                    [Loc(pg, tuple(b))], [loc], {"kind": "inline icon", "what": kind, "icons": 1}, types=[typ]))
            continue
        out.append(Finding(
            "assets", cfg.get("severity", "warning"),
            f"{head}: {len(items)} icon(s) - {why}: " + "; ".join(dict.fromkeys(t for *_, t in items)),
            [Loc(p, tuple(b)) for b, p, _, _ in items][:u.cfg["report"]["max_locs"]],
            [l for _, _, l, _ in items][:u.cfg["report"]["max_locs"]],
            {"kind": "inline icon", "what": kind, "icons": len(items)}, types=[typ]))
    return out
