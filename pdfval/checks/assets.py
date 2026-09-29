"""Assets check: images inside the section.

Images are paired by *where they sit in the text* and *what they look like*,
not by their order:
  1. position – each image is anchored to the first word at/below its top edge;
     that word is mapped into the other PDF through the content alignment
     (Aligner), so only images within `match_window_words` of the expected
     spot are candidates;
  2. appearance – a 16×16 difference hash of the rendered image picks the
     visually closest candidate. A candidate at the same spot that looks
     different is reported as "content differs" (e.g. a replaced screenshot).
One extra/missing image therefore never shifts every later comparison.

A stage image or icon that failed to load (web page) or renders blank where
prod has a picture is reported as broken.

Paired images are compared for aspect ratio and width relative to the content
box (independent of page size). Unpaired ones are reported as missing/extra,
with the aligned position on the other side for the screenshot.
"""
from __future__ import annotations

import numpy as np
import pymupdf
from PIL import Image as PILImage
from PIL import ImageChops, ImageFilter

from ..model import Doc, Finding, Image, Loc
from . import Aligner, Unit

_DOCS: dict[str, pymupdf.Document] = {}
_VIS: dict[tuple, tuple[int, float, float]] = {}


def visual(doc: Doc, im: Image) -> tuple[int, float, float]:
    """(256-bit difference hash, visible width pt, visible height pt).

    Uniform borders/padding (placement box vs. actual picture) are trimmed first,
    so the same picture placed in a larger or differently padded box still
    matches, and geometry is measured on what is actually visible."""
    key = (doc.path, im.page, im.bbox)
    if key not in _VIS:
        pdf = _DOCS.setdefault(doc.path, pymupdf.open(doc.path))
        r = pymupdf.Rect(im.bbox)
        z = 160 / max(r.width, 1)
        pix = pdf[im.page].get_pixmap(clip=r, matrix=pymupdf.Matrix(z, z), colorspace=pymupdf.csGRAY, alpha=False)
        g = PILImage.frombytes("L", (pix.width, pix.height), pix.samples)
        bg = PILImage.new("L", g.size, g.getpixel((1, 1)))
        box = ImageChops.difference(g, bg).point(lambda v: 255 if v > 28 else 0).getbbox()
        if box:
            g = g.crop(box)
        w_pt, h_pt = g.width / z, g.height / z
        px = g.resize((17, 16), PILImage.LANCZOS).tobytes()
        bits = 0
        for row in range(16):
            for col in range(16):
                bits = (bits << 1) | (px[row * 17 + col] > px[row * 17 + col + 1])
        _VIS[key] = (bits, w_pt, h_pt)
    return _VIS[key]


def _gray(doc: Doc, page: int, rect, px_per_pt: float) -> "np.ndarray":
    pdf = _DOCS.setdefault(doc.path, pymupdf.open(doc.path))
    pix = pdf[page].get_pixmap(clip=pymupdf.Rect(rect), matrix=pymupdf.Matrix(px_per_pt, px_per_pt),
                               colorspace=pymupdf.csGRAY, alpha=False)
    return np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.stride)[:, :pix.width].astype(np.float32)


def find_artwork(img_doc: Doc, im: Image, other: Doc, at, min_score: float, max_dist: float,
                 claimed: list | None = None) -> tuple[float, tuple] | None:
    """Look for the picture `im` drawn on the other side near `at`, whatever it is
    made of there (embedded bitmap, vector paths, live text). Renders the other
    page around the aligned spot and runs normalised cross-correlation of the
    picture at several scales; the best hit must also pass the visual-hash test
    (two independent signals, so text strips don't pass as logos). `claimed`
    holds (page, rect) already matched, so two identical icons don't both claim
    the same spot. Returns (score, rect in pt)."""
    if at is None:
        return None
    pw = other.pages[at.page].width
    ph = other.pages[at.page].height
    y0, y1 = max(0.0, at.bbox[1] - 220), min(ph, at.bbox[1] + 220)
    tpl_full = PILImage.fromarray(_gray(img_doc, im.page, im.bbox, 1.0).astype(np.uint8))
    base_w = (other.right(at.page) - other.left(at.page)) * _rel_width(img_doc, im)
    # render once at 1 px/pt and area-average down: rendering straight at a low zoom drops
    # thin lines, so line-art drawn as vectors would not correlate with its bitmap
    hay_full = _gray(other, at.page, (0, y0, pw, y1), 1.0)
    for pg, r in claimed or []:
        if pg == at.page:
            hay_full[max(0, int(r[1] - y0)):max(0, int(r[3] - y0)), int(r[0]):int(r[2])] = 255
    hay_img = PILImage.fromarray(hay_full.astype(np.uint8))
    hay_cache: dict[float, "np.ndarray"] = {}

    def at_scale(s: float) -> tuple[float, tuple] | None:
        # correlate at a resolution where the picture is ~48 px wide (and, for a wide banner,
        # ~20 px tall): enough to recognise a label, logo or strip of illustrations, and cheap
        # (the cost grows with template area x search area)
        w_pt = max(base_w * s, 1)
        k = round(min(0.6, max(96 / w_pt, 20 / max(w_pt * im_aspect(im), 1))), 3)
        if k not in hay_cache:
            size = (max(1, round(hay_img.width * k)), max(1, round(hay_img.height * k)))
            hay_cache[k] = np.asarray(hay_img.resize(size, PILImage.BOX), np.float32)
        hay = hay_cache[k]
        tw = round(base_w * s * k)
        th = round(base_w * s * k * im_aspect(im))
        if tw < 12 or th < 8 or tw >= hay.shape[1] or th >= hay.shape[0]:
            return None
        ncc = _ncc(hay, np.asarray(tpl_full.resize((tw, th), PILImage.BOX), np.float32))
        if ncc is None:
            return None
        iy, ix = np.unravel_index(int(ncc.argmax()), ncc.shape)
        x0, yy0 = ix / k, y0 + iy / k
        return float(ncc[iy, ix]), (x0, yy0, x0 + tw / k, yy0 + th / k)

    best, best_s = None, 1.0
    for s in (0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.15, 1.3, 1.5):
        hit = at_scale(s)
        if hit and (best is None or hit[0] > best[0]):
            best, best_s = hit, s
    # the correlation peak of a large picture is narrow in scale: refine between the grid steps
    for f in (0.92, 0.96, 1.04, 1.08):
        hit = at_scale(best_s * f)
        if hit and (best is None or hit[0] > best[0]):
            best = hit
    if not best or best[0] < min_score:
        return None
    hit = Image(at.page, best[1])
    # second signal: the visual hash, or - for a strong correlation, where the hash is too strict
    # for line art re-drawn with other stroke widths - the pixel similarity of the two pictures
    if visual_distance(img_doc, im, other, hit) > max_dist and \
            (best[0] < 0.75 or pixel_compare(other, hit, img_doc, im)[0] < 0.8):
        return None
    if claimed is not None:
        claimed.append((at.page, best[1]))
    return best


def _ncc(hay: "np.ndarray", tpl: "np.ndarray") -> "np.ndarray | None":
    """Normalised cross-correlation of tpl over every position of hay (valid region).
    Correlation via FFT, window mean/variance via integral images – same result as a
    sliding window, orders of magnitude faster."""
    hay, tpl = hay.astype(np.float64), tpl.astype(np.float64)  # float32 integral images lose precision
    th, tw = tpl.shape
    H, W = hay.shape
    t = tpl - tpl.mean()
    tn = float(np.sqrt((t * t).sum()))
    if tn < 1e-3 or th > H or tw > W:
        return None
    shape = (H + th - 1, W + tw - 1)
    corr = np.fft.irfft2(np.fft.rfft2(hay, shape) * np.fft.rfft2(t[::-1, ::-1], shape), shape)[th - 1:H, tw - 1:W]
    ii = np.pad(hay.cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    ii2 = np.pad((hay * hay).cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    box = lambda a: a[th:, tw:] - a[:-th, tw:] - a[th:, :-tw] + a[:-th, :-tw]
    s1, s2 = box(ii), box(ii2)
    var = np.maximum(s2 - s1 * s1 / (th * tw), 0)
    return corr / (np.sqrt(var) * tn + 1e-6)


def im_aspect(im: Image) -> float:
    """height / width of the placement box (the template is the full box)."""
    return (im.bbox[3] - im.bbox[1]) / max(im.bbox[2] - im.bbox[0], 1e-6)


_BLANK: dict[tuple, bool] = {}


def blank(doc: Doc, im: Image) -> bool:
    """Nothing visible: the placement box renders as one flat colour."""
    key = (doc.path, im.page, im.bbox)
    if key not in _BLANK:
        g = _gray(doc, im.page, im.bbox, min(2.0, 64 / max(im.bbox[2] - im.bbox[0], 1)))
        _BLANK[key] = g.size == 0 or float(g.std()) < 3.0
    return _BLANK[key]


def broken(a: Doc, x: Image | None, b: Doc, y: Image) -> bool:
    """Stage image y failed to load, or is blank where prod image x shows a picture."""
    return y.broken or (x is not None and blank(b, y) and not blank(a, x))


def _thumb(doc: Doc, im: Image, n: int = 64, trim: bool = True) -> "np.ndarray":
    """n x n grayscale of the visible picture (borders trimmed, as in visual()), slightly blurred
    so a one-pixel shift between two renderings of the same picture does not count."""
    pdf = _DOCS.setdefault(doc.path, pymupdf.open(doc.path))
    r = pymupdf.Rect(im.bbox)
    z = 200 / max(r.width, 1)
    pix = pdf[im.page].get_pixmap(clip=r, matrix=pymupdf.Matrix(z, z), colorspace=pymupdf.csGRAY, alpha=False)
    g = PILImage.frombytes("L", (pix.width, pix.height), pix.samples)
    bg = PILImage.new("L", g.size, g.getpixel((1, 1)))
    box = ImageChops.difference(g, bg).point(lambda v: 255 if v > 28 else 0).getbbox() if trim else None
    if box:
        g = g.crop(box)
    return np.asarray(g.resize((n, n), PILImage.BILINEAR).filter(ImageFilter.GaussianBlur(1.2)), np.float64)


def pixel_compare(a: Doc, x: Image, b: Doc, y: Image) -> tuple[float, float]:
    """(structural similarity -1..1, share of the picture near-black in stage but not in prod).
    The difference hash only sees brightness *steps*: a blacked-out area, or another picture with
    a similar layout, can hash alike. This correlates the pixels themselves (brightness-independent)
    and measures how much of the picture turned black."""
    ta, tb = _thumb(a, x), _thumb(b, y)
    ca, cb = ta - ta.mean(), tb - tb.mean()
    sim = float((ca * cb).sum() / (np.sqrt((ca * ca).sum() * (cb * cb).sum()) + 1e-9))
    # blackout on the whole placement box: trimming takes the corner colour as background, so a
    # black block in a corner would be cut away as "padding"
    fa, fb = _thumb(a, x, trim=False), _thumb(b, y, trim=False)
    dark = float(((fb < 40) & (fa >= 80)).mean())
    return sim, dark


def visual_distance(a: Doc, x: Image, b: Doc, y: Image) -> float:
    """0 = identical look, ~0.5 = unrelated."""
    return bin(visual(a, x)[0] ^ visual(b, y)[0]).count("1") / 256


def _pos(doc: Doc, idx: int) -> tuple[int, float]:
    if idx >= len(doc.words):
        return (len(doc.pages), 0.0)
    w = doc.words[idx]
    return (w.page, w.bbox[1])


def section_images(doc: Doc, rng: tuple[int, int]) -> list[Image]:
    start, end = _pos(doc, rng[0]), _pos(doc, rng[1])
    return [im for im in doc.images if start <= (im.page, im.bbox[1] + 1) < end]


def _rel_width(doc: Doc, im: Image) -> float:
    """Visible width as a share of the content box."""
    return visual(doc, im)[1] / max(doc.right(im.page) - doc.left(im.page), 1)


def icon_max(doc: Doc, im: Image, acfg: dict) -> bool:
    """True for a small icon (narrower than assets.icon_max_width of the content box)."""
    return _rel_width(doc, im) < acfg.get("icon_max_width", 0.08)


def _aspect(doc: Doc, im: Image) -> float:
    _, w, h = visual(doc, im)
    return w / max(h, 1e-6)


def _repair_by_look(u: Unit, ia, ib, used: set, pairs: list, changed: list, missing: list, anchor,
                    icon_w: float, same_look: float, sim_same: float, min_pic: float) -> None:
    """Pictures the text could not place: when the pages lay the pictures out in another order (a
    two-column page read the other way, items flowing onto the next page), each prod picture
    looks for its partner at the wrong spot, takes whatever picture is there ("different image",
    or a look-alike line drawing) and leaves its real partner over ("extra image"). Here the
    missing, "different" and weakly paired prod pictures (outline alike, pixels not the same
    picture) are matched again with the leftover, "different" and weakly paired stage pictures
    of the section, wherever they are: a pair needs the same look and the same pixels. What is
    left stays missing / extra / different. Mutates the lists."""
    idx_b = {id(y): n for n, y in enumerate(ib)}
    big = lambda x: _rel_width(u.a, x) >= min_pic
    same = {}  # (id x, n) -> pixel similarity, computed once

    def sim(x, n):
        if (id(x), n) not in same:
            same[(id(x), n)] = pixel_compare(u.a, x, u.b, ib[n])[0]
        return same[(id(x), n)]

    weak = [p for p in pairs if big(p[0]) and sim(p[0], idx_b[id(p[1])]) < sim_same]
    held = list(changed) + weak  # prod pictures that hold a stage picture they may not own
    pool_a = [m for m in missing if not m[2]] + held
    pool_b = {n for n in range(len(ib)) if n not in used} | {idx_b[id(h[1])] for h in held}
    cands = []
    for k, item in enumerate(pool_a):
        x = item[0]
        for n in pool_b:
            y = ib[n]
            if (item in held and y is item[1]) or _rel_width(u.b, y) < icon_w:
                continue  # its own picture was judged already; icons are not pictures
            # a picture: its pixels decide (the hash of a line drawing is fooled by crops and
            # margins, 0.99 pixel-alike pictures can hash 0.28 apart); an icon: its look
            vis = visual_distance(u.a, x, u.b, y)
            if big(x) and sim(x, n) >= sim_same:
                cands.append((1 - sim(x, n), k, n))
            elif not big(x) and vis <= same_look:
                cands.append((vis, k, n))
    taken_a: dict[int, tuple[int, float]] = {}
    taken_b: set[int] = set()
    for vis, k, n in sorted(cands):
        if k not in taken_a and n not in taken_b:
            taken_a[k] = (n, vis)
            taken_b.add(n)
    if not taken_a:
        return
    for k, item in enumerate(pool_a):
        own = idx_b[id(item[1])] if item in held else None
        home = changed if item in changed else pairs if item in held else missing
        if k in taken_a:
            n, vis = taken_a[k]
            home.remove(item)
            if own is not None and own not in taken_b:
                used.discard(own)  # not the picture of this spot after all: an extra one
            pairs.append((item[0], ib[n], visual_distance(u.a, item[0], u.b, ib[n])))
            used.add(n)
        elif own is not None and own in taken_b:
            home.remove(item)  # its stage picture belongs to another prod picture
            missing.append((item[0], anchor(u.a, u.a_range, item[0]), False))


def _combined(u: Unit, ib, used: set, missing: list, changed: list, art_score: float, same_look: float,
              min_pic: float) -> list[tuple[Image, Image, tuple]]:
    """Missing / "different" prod pictures found inside a stage picture of the section that is at
    least 1.5x their area (stage combined several prod pictures into one). Returns
    [(prod picture, stage picture, (score, rect))] and takes them out of `missing` / `changed`;
    the stage picture counts as placed. Mutates the lists."""
    area = lambda im: (im.bbox[2] - im.bbox[0]) * (im.bbox[3] - im.bbox[1])
    bigs = [y for y in ib if _rel_width(u.b, y) >= min_pic]
    out = []

    def inside(x):
        for y in bigs:
            if area(y) < 1.5 * area(x):
                continue
            top = y.bbox[1] + 200  # find_artwork searches 220 pt above and below its spot
            while top - 220 < y.bbox[3]:
                hit = find_artwork(u.a, x, u.b, Loc(y.page, (y.bbox[0], top, y.bbox[2], top + 1)), art_score, same_look)
                r = hit[1] if hit else None
                if r and r[0] >= y.bbox[0] - 4 and r[2] <= y.bbox[2] + 4 and r[1] >= y.bbox[1] - 4 and r[3] <= y.bbox[3] + 4:
                    return y, hit
                top += 400
        return None

    for item in [m for m in missing if not m[2]] + list(changed):  # pictures, not icons
        found = inside(item[0])
        if found:
            (missing if item in missing else changed).remove(item)
            used.add(next(n for n, y in enumerate(ib) if y is found[0]))
            out.append((item[0], *found))
    return out


def check(u: Unit) -> list[Finding]:
    acfg = u.cfg["assets"]
    al = Aligner(u)
    icon_w = acfg.get("icon_max_width", 0.08)
    window = acfg.get("match_window_words", 80)
    ia, ib = section_images(u.a, u.a_range), section_images(u.b, u.b_range)

    def anchor(doc: Doc, rng, im: Image) -> int:
        w = Aligner.word_at(doc, rng, im.page, im.bbox[1])
        return w if w is not None else max(rng[1] - 1, rng[0])

    anc_b = [anchor(u.b, u.b_range, y) for y in ib]
    same_look = acfg.get("visual_match_threshold", 0.25)
    same_spot = acfg.get("same_spot_words", 12)
    used: set[int] = set()
    pairs, changed, missing, broken_ = [], [], [], []
    for x in ia:
        ax = anchor(u.a, u.a_range, x)
        exp = al.to_b(ax)
        x_icon = _rel_width(u.a, x) < icon_w
        best = None
        for n, y in enumerate(ib):
            if n in used or exp is None:
                continue
            dist = abs(anc_b[n] - exp)
            if dist > window:
                continue
            vis = visual_distance(u.a, x, u.b, y)
            score = vis + 0.15 * dist / window  # appearance first, position breaks ties
            if best is None or score < best[0]:
                best = (score, n, vis, dist)
        if best and best[3] <= same_spot and broken(u.a, x, u.b, ib[best[1]]):
            used.add(best[1])  # the picture is there in prod; stage has an empty/failed box at that spot
            broken_.append((x, ib[best[1]], x_icon))
        elif best and best[2] <= same_look:
            used.add(best[1])
            pairs.append((x, ib[best[1]], best[2]))
        elif best and best[3] <= same_spot and not x_icon:
            used.add(best[1])  # same position, different picture: replaced image
            changed.append((x, ib[best[1]], best[2]))
        else:
            missing.append((x, ax, x_icon))

    # Second opinion from the pixels (pictures only; icons are often redrawn): a paired picture that
    # is really another picture, or has a blacked-out area, and a "changed" one that is the same
    # picture rendered differently (the hash is fooled by crops / flat areas both ways).
    sim_same = acfg.get("same_picture_similarity", 0.8)
    sim_diff = acfg.get("different_picture_similarity", 0.3)
    black_share = acfg.get("blackout_fraction", 0.15)
    min_pic = acfg.get("picture_min_width", 0.15)  # small logos lose detail when scaled: no "different" verdict
    blacked = []
    for item in list(pairs) + list(changed):
        x, y, vis = item
        if _rel_width(u.a, x) < icon_w:
            continue
        sim, dark = pixel_compare(u.a, x, u.b, y)
        home = pairs if item in pairs else changed
        if dark >= black_share:
            home.remove(item)
            blacked.append((x, y, dark))
        elif home is pairs and sim < sim_diff and _rel_width(u.a, x) >= min_pic:
            pairs.remove(item)
            changed.append((x, y, vis))
        elif home is changed and sim >= sim_same:
            changed.remove(item)
            pairs.append(item)
    _repair_by_look(u, ia, ib, used, pairs, changed, missing, anchor, icon_w, same_look, sim_same, min_pic)
    # Icons are paired like any image (so a resized icon is a size finding, not
    # missing+extra); only *unpaired* icons are subject to the `icons` setting,
    # because note/tip/LED icons are often raster in one PDF and vector in the other.
    icons = acfg.get("icons", "ignore")
    findings = []
    kind = lambda icon: "Icon" if icon else "Image"
    vec_sev = acfg.get("raster_vs_vector_severity", "info")
    art_score = acfg.get("artwork_match_score", 0.6)
    claimed_a: list = []
    claimed_b: list = []
    # prod pictures that stage shows as part of one bigger picture (several views of the product
    # combined into one image): found inside it, so neither missing nor a different picture
    for x, y, hit in _combined(u, ib, used, missing, changed, art_score, same_look, min_pic):
        findings.append(Finding(
            "assets", vec_sev, f"Picture combined in stage: the prod picture is part of a larger stage picture "
                               f"(prod p.{x.page + 1} ↔ stage p.{y.page + 1}, match {hit[0]:.0%})",
            [Loc(x.page, x.bbox)], [Loc(y.page, hit[1])], {"kind": "combined"}, types=["image combined"]))
    for x, ax, icon in missing:
        if icon and icons == "ignore":
            continue
        at = al.loc_in_b(ax)
        vec = find_artwork(u.a, x, u.b, at, art_score, same_look, claimed_b)
        if vec:
            findings.append(Finding(
                "assets", vec_sev, f"Same artwork, but an embedded image in prod and drawn as vector/text in stage "
                                   f"(prod p.{x.page + 1} ↔ stage p.{at.page + 1}, match {vec[0]:.0%})",
                [Loc(x.page, x.bbox)], [Loc(at.page, vec[1])], {"kind": "raster-vs-vector"}))
            continue
        findings.append(Finding(
            "assets", acfg.get("icons", "warning") if icon else acfg.get("count_severity", "error"),
            f"{kind(icon)} missing in candidate (prod p.{x.page + 1}, {_rel_width(u.a, x):.0%} of content width)",
            [Loc(x.page, x.bbox)], [], {"kind": "missing", "icon": icon},
            candidate_at=at, critical=not icon))
    for x, y, icon in broken_:
        findings.append(Finding(
            "assets", acfg.get("broken_severity", "error"),
            f"{kind(icon)} broken in stage: it does not display (prod p.{x.page + 1} ↔ stage p.{y.page + 1})",
            [Loc(x.page, x.bbox)], [Loc(y.page, y.bbox)], {"kind": "broken", "icon": icon}, critical=True))
    for n, y in enumerate(ib):  # failed to load, with no counterpart found in prod
        if n not in used and y.broken:
            used.add(n)
            icon = _rel_width(u.b, y) < icon_w
            findings.append(Finding(
                "assets", acfg.get("broken_severity", "error"),
                f"{kind(icon)} broken in stage: it failed to load (stage p.{y.page + 1})",
                [], [Loc(y.page, y.bbox)], {"kind": "broken", "icon": icon}, baseline_at=al.loc_in_a(anc_b[n]),
                critical=True))
    for x, y, dark in blacked:
        findings.append(Finding(
            "assets", acfg.get("broken_severity", "error"),
            f"Image blacked out in stage: {dark:.0%} of the picture is black where prod shows content "
            f"(prod p.{x.page + 1} ↔ stage p.{y.page + 1})",
            [Loc(x.page, x.bbox)], [Loc(y.page, y.bbox)], {"kind": "blackout", "dark_share": round(dark, 3)}, critical=True))
    for x, y, vis in changed:
        findings.append(Finding(
            "assets", acfg.get("changed_severity", "error"),
            f"Different image in stage: the picture at this spot is not the prod picture "
            f"(prod p.{x.page + 1} ↔ stage p.{y.page + 1}, visual similarity {1 - vis:.0%})",
            [Loc(x.page, x.bbox)], [Loc(y.page, y.bbox)], {"kind": "changed", "visual_distance": round(vis, 3)}))
    for n, y in enumerate(ib):
        if n in used:
            continue
        icon = _rel_width(u.b, y) < icon_w
        if icon and icons == "ignore":
            continue
        at = al.loc_in_a(anc_b[n])
        vec = find_artwork(u.b, y, u.a, at, art_score, same_look, claimed_a)
        if vec:
            findings.append(Finding(
                "assets", vec_sev, f"Same artwork, but drawn as vector/text in prod and an embedded image in stage "
                                   f"(prod p.{at.page + 1} ↔ stage p.{y.page + 1}, match {vec[0]:.0%})",
                [Loc(at.page, vec[1])], [Loc(y.page, y.bbox)], {"kind": "raster-vs-vector"}))
            continue
        findings.append(Finding(
            "assets", acfg.get("icons", "warning") if icon else acfg.get("count_severity", "error"),
            f"Extra {kind(icon).lower()} in candidate (stage p.{y.page + 1}, {_rel_width(u.b, y):.0%} of content width)",
            [], [Loc(y.page, y.bbox)], {"kind": "extra", "icon": icon},
            baseline_at=at))
    u.image_pairs = [(x, y) for x, y, _ in pairs]
    for x, y, _ in pairs:
        ar_x, ar_y = _aspect(u.a, x), _aspect(u.b, y)
        rel_x, rel_y = _rel_width(u.a, x), _rel_width(u.b, y)
        problems = []
        if abs(ar_x - ar_y) / ar_x > acfg["aspect_tolerance"]:
            problems.append(f"aspect {ar_x:.2f} → {ar_y:.2f}")
        if max(rel_x, rel_y) >= icon_w and abs(rel_x - rel_y) > acfg["width_tolerance"]:
            problems.append(f"width {rel_x:.0%} → {rel_y:.0%} of content box")
        if problems:
            # stretched = stage draws the picture out of its own pixel proportions and prod does not
            # (a different measured aspect alone is usually another crop or frame of the same picture)
            tol = 1 + acfg.get("distorted_tolerance", 0.25)
            off = lambda im: max(im.stretch, 1 / max(im.stretch, 1e-6))
            distorted = off(y) >= tol and off(x) < 1.1 and rel_x >= icon_w
            findings.append(Finding(
                "assets", "error" if distorted else acfg.get("geometry_severity", "warning"),
                ("Image distorted in stage (stretched or squashed)" if distorted else kind(rel_x < icon_w))
                + f" (prod p.{x.page + 1} ↔ stage p.{y.page + 1}): " + ", ".join(problems),
                [Loc(x.page, x.bbox)], [Loc(y.page, y.bbox)],
                {"baseline_aspect": round(ar_x, 3), "candidate_aspect": round(ar_y, 3),
                 "baseline_width": round(rel_x, 3), "candidate_width": round(rel_y, 3)},
                types=["image distorted"] if distorted else [],
            ))
    return findings
