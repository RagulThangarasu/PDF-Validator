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
from collections import defaultdict
import pymupdf
from PIL import Image as PILImage
from PIL import ImageChops, ImageDraw, ImageFilter

from ..model import Doc, Finding, Image, Loc
from . import Aligner, Unit, snippet
from .tables import is_curve

_DOCS: dict[str, pymupdf.Document] = {}
_VIS: dict[tuple, tuple[int, float, float]] = {}


def visual(doc: Doc, im: Image) -> tuple[int, float, float]:
    """(256-bit difference hash, visible width pt, visible height pt).

    Uniform borders/padding (placement box vs. actual picture) are trimmed first,
    so the same picture placed in a larger or differently padded box still
    matches, and geometry is measured on what is actually visible."""
    key = (doc.path, im.page, im.bbox)
    if key not in _VIS:
        pdf = (_DOCS.get(doc.path) or _DOCS.setdefault(doc.path, pymupdf.open(doc.path)))
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
    pdf = (_DOCS.get(doc.path) or _DOCS.setdefault(doc.path, pymupdf.open(doc.path)))
    pix = pdf[page].get_pixmap(clip=pymupdf.Rect(rect), matrix=pymupdf.Matrix(px_per_pt, px_per_pt),
                               colorspace=pymupdf.csGRAY, alpha=False)
    return np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.stride)[:, :pix.width].astype(np.float32)


def _has_artwork(doc: Doc, page: int, rect) -> bool:
    """The spot holds drawn artwork: an embedded picture over part of it, or vector shapes that are
    not just table / box rules (curves, slanted strokes, filled shapes) - at least a few of them."""
    r = pymupdf.Rect(rect)
    if r.is_empty:
        return False
    if any((pymupdf.Rect(im.bbox) & r).get_area() >= 0.3 * r.get_area() for im in doc.images if im.page == page):
        return True
    pdf = (_DOCS.get(doc.path) or _DOCS.setdefault(doc.path, pymupdf.open(doc.path)))
    n = 0
    try:
        drawings = pdf[page].get_drawings()
    except Exception:
        return True  # cannot tell: do not reject
    for d in drawings:
        if not pymupdf.Rect(d["rect"]).intersects(r):
            continue
        for it in d["items"]:
            if is_curve(it):
                n += 1
            elif it[0] == "l":
                p0, p1 = it[1], it[2]
                if abs(p0.x - p1.x) > 1 and abs(p0.y - p1.y) > 1 and r.contains(p0):
                    n += 1  # a slanted stroke
            elif it[0] == "re" and d.get("fill") and min(it[1].width, it[1].height) > 3 and r.contains(it[1].tl):
                n += 1
    return n >= 4


def size_reported(rel_x: float, rel_y: float, acfg: dict) -> bool:
    """A size difference worth reporting: the stage picture grew or shrank by more than [assets]
    size_ratio_tolerance (relative) or width_tolerance (as a fraction of the content box) - either
    direction, not only a big shrink, so every real size change is captured with its exact numbers."""
    if rel_x <= 0:
        return False
    return abs(rel_x - rel_y) > acfg.get("width_tolerance", 0.10) or \
        abs(rel_y / rel_x - 1) > acfg.get("size_ratio_tolerance", 0.2)


def find_artwork(img_doc: Doc, im: Image, other: Doc, at, min_score: float, max_dist: float,
                 claimed: list | None = None, reach: float = 220, mask: list | None = None) -> tuple[float, tuple] | None:
    """Look for the picture `im` drawn on the other side near `at`, whatever it is
    made of there (embedded bitmap, vector paths, live text). Renders the other
    page around the aligned spot and runs normalised cross-correlation of the
    picture at several scales; the best hit must also pass the visual-hash test
    (two independent signals, so text strips don't pass as logos). `claimed`
    holds (page, rect) already matched, so two identical icons don't both claim
    the same spot. `reach`: how far (pt) above and below the spot to search. `mask`: (page, rect) areas
    left out of the search, not claimed (another section's part of the page).
    Returns (score, rect in pt)."""
    if at is None:
        return None
    pw = other.pages[at.page].width
    ph = other.pages[at.page].height
    y0, y1 = max(0.0, at.bbox[1] - reach), min(ph, at.bbox[1] + reach)
    tpl_full = PILImage.fromarray(_gray(img_doc, im.page, im.bbox, 1.0).astype(np.uint8))
    base_w = (other.right(at.page) - other.left(at.page)) * _rel_width(img_doc, im)
    # render once at 1 px/pt and area-average down: rendering straight at a low zoom drops
    # thin lines, so line-art drawn as vectors would not correlate with its bitmap
    hay_full = _gray(other, at.page, (0, y0, pw, y1), 1.0)
    for pg, r in (claimed or []) + (mask or []):
        if pg == at.page:
            hay_full[max(0, int(r[1] - y0)):max(0, int(r[3] - y0)), int(r[0]):int(r[2])] = 255
    hay_img = PILImage.fromarray(hay_full.astype(np.uint8))
    hay_cache: dict[float, "np.ndarray"] = {}

    blur = 0

    def at_scale(s: float) -> tuple[float, tuple] | None:
        # correlate at a resolution where the picture is ~48 px wide (and, for a wide banner,
        # ~20 px tall): enough to recognise a label, logo or strip of illustrations, and cheap
        # (the cost grows with template area x search area)
        w_pt = max(base_w * s, 1)
        k = round(min(0.6, max(96 / w_pt, 20 / max(w_pt * im_aspect(im), 1))), 3)
        if (k, blur) not in hay_cache:
            size = (max(1, round(hay_img.width * k)), max(1, round(hay_img.height * k)))
            h = hay_img.resize(size, PILImage.BOX)
            hay_cache[(k, blur)] = np.asarray(h.filter(ImageFilter.GaussianBlur(blur)) if blur else h, np.float32)
        hay = hay_cache[(k, blur)]
        tw = round(base_w * s * k)
        th = round(base_w * s * k * im_aspect(im))
        if tw < 12 or th < 8 or tw >= hay.shape[1] or th >= hay.shape[0]:
            return None
        t = tpl_full.resize((tw, th), PILImage.BOX)
        ncc = _ncc(hay, np.asarray(t.filter(ImageFilter.GaussianBlur(blur)) if blur else t, np.float32))
        if ncc is None:
            return None
        iy, ix = np.unravel_index(int(ncc.argmax()), ncc.shape)
        x0, yy0 = ix / k, y0 + iy / k
        return float(ncc[iy, ix]), (x0, yy0, x0 + tw / k, yy0 + th / k)

    def search() -> tuple | None:
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
        return best

    best = search()
    if not best or best[0] < min_score:
        # fine detail (a QR code's modules) shrunk to the search size aliases, and the match then depends on
        # where the window starts: compare the softened pictures (the shape, not the grid) - the visual
        # hash and pixel tests below still decide whether it is the same picture
        blur = 1.2
        best = search()
    if not best or best[0] < min_score:
        return None
    hit = Image(at.page, best[1])
    # second signal: the visual hash, or - for a strong correlation, where the hash is too strict
    # for line art re-drawn with other stroke widths - the pixel similarity of the two pictures
    if visual_distance(img_doc, im, other, hit) > max_dist and \
            (best[0] < 0.75 or pixel_compare(other, hit, img_doc, im)[0] < 0.8):
        return None
    if not _has_artwork(other, at.page, best[1]):
        return None  # empty paper, table rules or plain text: not a drawing of the picture
    if claimed is not None:
        claimed.append((at.page, best[1]))
    return best


_FAST: dict[int, int] = {}


def _fast_len(n: int) -> int:
    """Smallest m >= n whose only prime factors are 2, 3 and 5."""
    if n not in _FAST:
        m = n
        while True:
            k = m
            for p in (2, 3, 5):
                while k % p == 0:
                    k //= p
            if k == 1:
                break
            m += 1
        _FAST[n] = m
    return _FAST[n]


_FIG: dict[tuple, list] = {}


def _in_figure(doc, im, pad: float = 6) -> bool:
    """A small picture on or against a larger picture / drawing (a mouse cursor or a hand on a diagram): part of
    that figure, which is compared as a whole - not an icon of its own."""
    key = (doc.path, im.page)
    if key not in _FIG:
        try:
            pg = (_DOCS.get(doc.path) or _DOCS.setdefault(doc.path, pymupdf.open(doc.path)))[im.page]
            boxes = [tuple(i["bbox"]) for i in pg.get_image_info()] + [tuple(r) for r in pg.cluster_drawings()]
        except Exception:
            boxes = []
        _FIG[key] = boxes
    a = (im.bbox[2] - im.bbox[0]) * (im.bbox[3] - im.bbox[1])
    for b in _FIG[key]:
        if (b[2] - b[0]) * (b[3] - b[1]) < 4 * a:
            continue
        if b[0] - pad < im.bbox[2] and b[2] + pad > im.bbox[0] and b[1] - pad < im.bbox[3] and b[3] + pad > im.bbox[1]:
            return True
    return False


def _margin_icon(doc, im) -> bool:
    """An icon set in the margin beside a paragraph (a note / tip / safety icon heading its text): the callout
    style of the template, which the other PDF draws its own way - not an icon of the content."""
    h = im.bbox[3] - im.bbox[1]
    cy = (im.bbox[1] + im.bbox[3]) / 2
    for ln in doc.lines:
        if ln.page != im.page:
            continue
        if abs((ln.bbox[1] + ln.bbox[3]) / 2 - cy) <= h / 2 + 8 and 0 <= ln.bbox[0] - im.bbox[2] <= 60 \
                and ln.bbox[2] - ln.bbox[0] >= 100:
            return True
    return False


def _plain_twin(u, y: Image, at: Loc, claimed: list, acfg: dict) -> tuple | None:
    """A prod vector drawing near `at` that looks like the stage picture `y` (visual hash) at up to 2x
    another size - simple artwork the correlation search rejects as "no artwork" (a plain grey card)."""
    try:
        pg = u.a.pdf_page(at.page) if hasattr(u.a, "pdf_page") else pymupdf.open(u.a.path)[at.page]
        clusters = [tuple(r) for r in pg.cluster_drawings() if r.width >= 12 and r.height >= 12]
    except Exception:
        return None
    thr = acfg.get("visual_match_threshold", 0.25)
    wy = y.bbox[2] - y.bbox[0]
    span = acfg.get("twin_reach", 160)
    best = None
    for r in clusters:
        if any(p == at.page and abs(c[0] - r[0]) < 2 and abs(c[1] - r[1]) < 2 for p, c in claimed):
            continue
        if abs((r[1] + r[3]) / 2 - at.bbox[1]) > span:
            continue
        ratio = _rel_width(u.a, Image(at.page, r)) / max(_rel_width(u.b, y), 1e-6)
        if not 0.5 <= ratio <= 2.0 or (r[2] - r[0]) > 3 * wy + 50:
            continue
        d = visual_distance(u.b, y, u.a, Image(at.page, r))
        if d <= thr and (best is None or d < best[0]):
            best = (d, r)
    return best[1] if best else None


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
    # zero-padded to sizes made of 2s, 3s and 5s (any padding >= H+th-1 gives the same linear correlation;
    # FFTs of such sizes are many times faster than of a large prime)
    shape = (_fast_len(H + th - 1), _fast_len(W + tw - 1))
    corr = np.fft.irfft2(np.fft.rfft2(hay, shape) * np.fft.rfft2(t[::-1, ::-1], shape), shape)[th - 1:H, tw - 1:W]
    ii = np.pad(hay.cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    ii2 = np.pad((hay * hay).cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    box = lambda a: a[th:, tw:] - a[:-th, tw:] - a[th:, :-tw] + a[:-th, :-tw]
    s1, s2 = box(ii), box(ii2)
    var = np.maximum(s2 - s1 * s1 / (th * tw), 0)
    return corr / (np.sqrt(var) * tn + 1e-6)


def _ppi(im: Image) -> float | None:
    """Pixels per inch the picture is printed at (the lower of both axes); None: not a bitmap."""
    w, h = im.px if im.px else (0, 0)
    bw, bh = im.bbox[2] - im.bbox[0], im.bbox[3] - im.bbox[1]
    if not w or not h or bw < 20 or bh < 20:  # unknown, or an icon too small to judge
        return None
    return min(w / (bw / 72), h / (bh / 72))


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
    pdf = (_DOCS.get(doc.path) or _DOCS.setdefault(doc.path, pymupdf.open(doc.path)))
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


def _mirror_scores(a: Doc, x: Image, b: Doc, y: Image) -> tuple[float, float, float]:
    """(direct correlation, correlation flipped left-right, correlation flipped top-bottom). A simple
    line diagram (a screen box, an icon, a silhouette) can hash and correlate almost identically to its
    own mirror image - the ink mass barely changes - so a flipped diagram can pass as "the same picture"
    even though the reader sees it the wrong way round. Comparing against the flipped candidate too
    catches that a notably better match."""
    ta, tb = _thumb(a, x), _thumb(b, y)
    ca = ta - ta.mean()
    na = np.sqrt((ca * ca).sum()) + 1e-9

    def corr(t: np.ndarray) -> float:
        c = t - t.mean()
        return float((ca * c).sum() / (na * np.sqrt((c * c).sum()) + 1e-9))

    return corr(tb), corr(np.fliplr(tb)), corr(np.flipud(tb))


def visual_distance(a: Doc, x: Image, b: Doc, y: Image) -> float:
    """0 = identical look, ~0.5 = unrelated."""
    return bin(visual(a, x)[0] ^ visual(b, y)[0]).count("1") / 256


def _pos(doc: Doc, idx: int) -> tuple[int, float]:
    if idx >= len(doc.words):
        return (len(doc.pages), 0.0)
    w = doc.words[idx]
    return (w.page, w.bbox[1])


def _outside(doc: Doc, rng: tuple[int, int], page: int) -> list[tuple[int, tuple]]:
    """The parts of the page that belong to other sections: above the section's heading on its first page,
    from the next section's heading down on its last page, the whole page before / after it. A search for
    the section's artwork there would find the same picture of another section (a product drawn in
    "Package contents" and again in "Product overview")."""
    (sp, sy), (ep, ey) = _pos(doc, rng[0]), _pos(doc, rng[1])
    if not 0 <= page < len(doc.pages):
        return []
    W, H = doc.pages[page].width, doc.pages[page].height
    if page < sp or page > ep:
        return [(page, (0, 0, W, H))]
    out = []
    if page == sp:
        out.append((page, (0, 0, W, max(0.0, sy - 2))))
    if page == ep:
        out.append((page, (0, ey - 1, W, H)))
    return out


def _drawing_near(doc: Doc, rng: tuple[int, int], at, claimed: list, pic_doc: Doc, pic: Image,
                  reach: float = 200) -> tuple | None:
    """An illustration drawn as vectors (a cluster of shapes with curves) in this section of the document,
    within `reach` pt of the spot `at` on its page, not claimed yet, of about the size and shape of the
    other side's picture `pic` (half to twice its share of the text width, its width : height within
    1.8x - not a whole page of drawings for a small picture): the nearest one."""
    from .tables import _figure_rects
    want_w = _rel_width(pic_doc, pic) * (doc.right(at.page) - doc.left(at.page))
    shape = (pic.bbox[2] - pic.bbox[0]) / max(pic.bbox[3] - pic.bbox[1], 1)
    out = [(p, o) for p, o in _outside(doc, rng, at.page)]
    best = None
    for r in _figure_rects(doc, at.page):
        c = pymupdf.Point((r[0] + r[2]) / 2, (r[1] + r[3]) / 2)
        if r[2] - r[0] < 40 or r[3] - r[1] < 30 or any(pymupdf.Rect(o).contains(c) for _, o in out):
            continue
        if not 0.5 <= (r[2] - r[0]) / max(want_w, 1) <= 2.0 or \
                not 1 / 1.8 <= ((r[2] - r[0]) / max(r[3] - r[1], 1)) / shape <= 1.8:
            continue  # another size or shape than the picture
        if any(pg == at.page and pymupdf.Rect(cr).intersects(pymupdf.Rect(r)) for pg, cr in claimed):
            continue
        if any(im.page == at.page and (pymupdf.Rect(im.bbox) & pymupdf.Rect(r)).get_area() > 0.5 * pymupdf.Rect(r).get_area()
               for im in doc.images):
            continue  # an embedded picture: paired (or not) by the picture pairing
        d = 0 if r[1] <= at.bbox[1] <= r[3] else min(abs(r[1] - at.bbox[1]), abs(r[3] - at.bbox[1]))
        if d <= reach and (best is None or d < best[0]):
            best = (d, tuple(r))
    return best[1] if best else None


def _whole_figure(doc: Doc, rng: tuple[int, int], page: int, rect: tuple, pic_doc: Doc, pic: Image,
                  gap: float = 40) -> tuple | None:
    """The figure a drawn part belongs to: a figure drawn as vectors in pieces (a screen above, the
    devices below, labels between them as live text) is one picture in the other PDF. Grows `rect` by
    the drawings and text lines right above / below it (within `gap` pt), as long as the result keeps
    nearer the other picture's shape. The grown area, or None when nothing was added."""
    from .tables import _figure_rects
    want = (pic.bbox[3] - pic.bbox[1]) / max(pic.bbox[2] - pic.bbox[0], 1)  # height : width of the picture
    out = [o for _, o in _outside(doc, rng, page)]
    parts = [tuple(r) for r in _figure_rects(doc, page)] + \
        [tuple(l.bbox) for l in doc.lines if l.page == page and len(l.text.split()) <= 4]  # short labels only
    parts = [r for r in parts if not any(pymupdf.Rect(o).contains(pymupdf.Point((r[0] + r[2]) / 2, (r[1] + r[3]) / 2)) for o in out)]
    box, grown = pymupdf.Rect(rect), False
    off = lambda b: abs((b.height / max(b.width, 1)) - want)
    while True:
        cand = [pymupdf.Rect(r) for r in parts if not box.contains(pymupdf.Rect(r))
                and pymupdf.Rect(r).x1 > box.x0 - 60 and pymupdf.Rect(r).x0 < box.x1 + 60
                and (0 <= pymupdf.Rect(r).y0 - box.y1 <= gap or 0 <= box.y0 - pymupdf.Rect(r).y1 <= gap
                     or pymupdf.Rect(r).intersects(box))]
        nxt = min(cand, key=lambda r: min(abs(r.y0 - box.y1), abs(box.y0 - r.y1)), default=None)
        if nxt is None or off(box | nxt) > off(box):
            break
        box |= nxt
        grown = True
    return tuple(box) if grown else None


def _layer_of(x: Image, placed: list) -> bool:
    """x lies on top of other pictures of the same page that have their stage picture: a screenshot built
    from tiles (the menu, a highlight bar, a cursor layered on it) is one picture in stage. True when at
    least 80 % of x is covered by those pictures."""
    r = pymupdf.Rect(x.bbox)
    if r.is_empty:
        return False
    under = [pymupdf.Rect(p.bbox) & r for p in placed if p is not x and p.page == x.page]
    if not under:
        return False
    # area covered (the tiles may overlap each other: sampled on a grid)
    n, hit = 0, 0
    for i in range(12):
        for j in range(6):
            pt = pymupdf.Point(r.x0 + (i + 0.5) * r.width / 12, r.y0 + (j + 0.5) * r.height / 6)
            n += 1
            hit += any(c.contains(pt) for c in under if not c.is_empty)
    return hit >= 0.8 * n


def section_images(doc: Doc, rng: tuple[int, int]) -> list[Image]:
    start, end = _pos(doc, rng[0]), _pos(doc, rng[1])
    return stack_slices([im for im in doc.images if start <= (im.page, im.bbox[1] + 1) < end])


def stack_slices(images: list[Image], tol: float = 2.5) -> list[Image]:
    """Pictures stacked edge to edge - the same left and right edges, each one starting where the one above
    ends - are one picture cut into strips (an OSD menu exported as header / body / footer bitmaps): joined
    into one, so they are compared as the picture the reader sees, not as strips no other picture matches."""
    out: list[Image] = []
    for im in sorted(images, key=lambda i: (i.page, i.bbox[0], i.bbox[1])):
        last = out[-1] if out else None
        if last is not None and last.page == im.page and not last.broken and not im.broken \
                and abs(last.bbox[0] - im.bbox[0]) <= tol and abs(last.bbox[2] - im.bbox[2]) <= tol \
                and -tol <= im.bbox[1] - last.bbox[3] <= tol:
            out[-1] = Image(im.page, (min(last.bbox[0], im.bbox[0]), last.bbox[1], max(last.bbox[2], im.bbox[2]),
                                      max(last.bbox[3], im.bbox[3])))
        else:
            out.append(im)
    # back in reading order (page, top, left), as the callers expect
    return sorted(out, key=lambda i: (i.page, i.bbox[1], i.bbox[0]))


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
            # margins, 0.99 pixel-alike pictures can hash 0.28 apart); an icon: its look - but also by
            # its pixels when the look-alike hash misses it (reflowed onto another page, re-rendered
            # at another size: the same instructional icon, found by its pixels even though small)
            vis = visual_distance(u.a, x, u.b, y)
            if big(x) and sim(x, n) >= sim_same:
                cands.append((1 - sim(x, n), k, n))
            elif not big(x) and (vis <= same_look or sim(x, n) >= sim_same):
                cands.append((min(vis, 1 - sim(x, n)), k, n))
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


def _sequence(u: Unit, pairs: list, acfg: dict) -> list[Finding]:
    """Pictures in another order: paired by look, so a swap is not a missing picture - but the reader
    sees them in a different sequence. The longest run that keeps prod's order stays; every picture
    outside it is reported where it sits on both sides."""
    if len(pairs) < 2:
        return []
    pos = lambda im: (im.page, im.bbox[1], im.bbox[0])
    ps = sorted(pairs, key=lambda p: pos(p[0]))
    seq = [pos(y) for _, y in ps]
    # longest increasing subsequence of the stage positions (O(n^2), a section has few pictures)
    best = [1] * len(seq); prev = [-1] * len(seq)
    for i in range(len(seq)):
        for k in range(i):
            if seq[k] < seq[i] and best[k] + 1 > best[i]:
                best[i], prev[i] = best[k] + 1, k
    i = max(range(len(seq)), key=best.__getitem__); keep = set()
    while i >= 0:
        keep.add(i); i = prev[i]
    out = []
    for n, (x, y) in enumerate(ps):
        if n in keep:
            continue
        out.append(Finding(
            "assets", acfg.get("order_severity", "error"),
            f"Image sequence differs: this picture is #{n + 1} of {len(ps)} in prod but comes in another place "
            f"in stage (prod p.{x.page + 1} ↔ stage p.{y.page + 1})",
            [Loc(x.page, x.bbox)], [Loc(y.page, y.bbox)], {"kind": "image order"}, types=["image order"]))
    return out


def _combined(u: Unit, ib, used: set, missing: list, changed: list, art_score: float, same_look: float,
              min_pic: float, same_screen: set | None = None) -> list[tuple[Image, Image, tuple]]:
    """Missing / "different" prod pictures found inside a stage picture of the section that is at
    least 1.5x their area (stage combined several prod pictures into one). Returns
    [(prod picture, stage picture, (score, rect))] and takes them out of `missing` / `changed`;
    the stage picture counts as placed. Mutates the lists."""
    area = lambda im: (im.bbox[2] - im.bbox[0]) * (im.bbox[3] - im.bbox[1])
    idx = {id(y): n for n, y in enumerate(ib)}
    # stage pictures no prod picture claimed yet come first: a prod screen that stage shows on its own
    # (cropped wider, without prod's red marks) is that picture - not the small copy of it inside a
    # browser screenshot that already pairs with prod's own screenshot
    bigs = sorted((y for y in ib if _rel_width(u.b, y) >= min_pic), key=lambda y: idx[id(y)] in used)
    out = []
    merged: set[int] = set()  # stage pictures already holding other prod pictures (slices of one screen)
    merged_score = u.cfg["assets"].get("combined_slice_score", 0.72)

    def inside(x):
        for y in bigs:
            # a picture that already holds slices of the same prod screen is that screen: the next slice only
            # needs the strong correlation too (prod's red highlight frames fail the look-alike test)
            free = idx[id(y)] not in used or idx[id(y)] in merged
            if area(y) < (1.0 if free else 1.5) * area(x):
                continue
            # one search over the whole stage picture: a tall part (a remote control beside the
            # product) fits in no fixed-height window, so it would never be found
            mid, half = (y.bbox[1] + y.bbox[3]) / 2, (y.bbox[3] - y.bbox[1]) / 2
            # an unclaimed stage picture needs only a strong correlation: the look-alike test would reject the
            # same screen with prod's red marks on it; an already paired one needs both signals, as before
            # a known combination of prod slices: the slice must lie inside it (checked below) and correlate well;
            # prod's red highlight frames on a slice cost it some correlation
            need = merged_score if idx[id(y)] in merged else max(art_score, 0.85) if free else art_score
            hit = find_artwork(u.a, x, u.b, Loc(y.page, (y.bbox[0], mid, y.bbox[2], mid + 1)),
                               need, 1.0 if free else same_look, reach=max(220, half + 30))
            r = hit[1] if hit else None
            # inside the stage picture - mostly: the prod picture's own white margin (beside its
            # callout numbers) may stick out past the stage picture's edge
            if r:
                ix = max(0.0, min(r[2], y.bbox[2]) - max(r[0], y.bbox[0]))
                iy = max(0.0, min(r[3], y.bbox[3]) - max(r[1], y.bbox[1]))
                if ix * iy >= 0.85 * (r[2] - r[0]) * (r[3] - r[1]):
                    return y, hit
        return None

    def own_partner(x, y, strict: bool = True) -> bool:
        """A "different" picture found in the stage picture it was paired with (the same spot): the same
        screen, cropped wider or without prod's marks - it stays that pair, whatever else contains it.
        strict=False: the ordinary two-signal test of a combined picture (correlation + look-alike)."""
        mid, half = (y.bbox[1] + y.bbox[3]) / 2, (y.bbox[3] - y.bbox[1]) / 2
        hit = find_artwork(u.a, x, u.b, Loc(y.page, (y.bbox[0], mid, y.bbox[2], mid + 1)),
                           max(art_score, 0.85) if strict else art_score, 1.0 if strict else same_look,
                           reach=max(220, half + 30))
        if not hit:
            return False
        r = hit[1]
        ix = max(0.0, min(r[2], y.bbox[2]) - max(r[0], y.bbox[0]))
        iy = max(0.0, min(r[3], y.bbox[3]) - max(r[1], y.bbox[1]))
        return ix * iy >= 0.85 * (r[2] - r[0]) * (r[3] - r[1])

    candidates = [m for m in missing if not m[2]] + list(changed)  # pictures, not icons
    held: dict[tuple, bool] = {}

    def holds_other(y, x) -> bool:
        """Another prod picture of the section is also inside stage picture y: stage merged several prod
        pictures into one image (the projector and its remote), so y is a combination, not x re-cropped."""
        key = (id(y), id(x))
        if key not in held:
            held[key] = any(own_partner(z[0], y) or own_partner(z[0], y, strict=False) for z in candidates if z[0] is not x)
        return held[key]

    recropped = []
    # a second pass: slices checked before the first one was found in the stage picture
    for item in candidates + [None] + candidates:
        if item is None:
            if not merged:
                break
            continue
        if item not in missing and item not in changed:
            continue  # placed in the first pass
        if item in changed and own_partner(item[0], item[1]):
            if area(item[1]) >= 2.5 * area(item[0]):
                # a small prod picture (the music notes) found inside a much larger stage picture of the same
                # spot (phone + speaker + notes): stage combined the illustration into one image - not a re-crop
                changed.remove(item)
                used.add(idx[id(item[1])])
                merged.add(idx[id(item[1])])
                mid, half = (item[1].bbox[1] + item[1].bbox[3]) / 2, (item[1].bbox[3] - item[1].bbox[1]) / 2
                hit = find_artwork(u.a, item[0], u.b, Loc(item[1].page, (item[1].bbox[0], mid, item[1].bbox[2], mid + 1)),
                                   max(art_score, 0.85), 1.0, reach=max(220, half + 30))
                out.append((item[0], item[1], hit))
                continue
            if not holds_other(item[1], item[0]):
                if same_screen is not None:
                    same_screen.add((id(item[0]), id(item[1])))
                continue
        found = inside(item[0])
        if found:
            y = found[0]
            free = idx[id(y)] not in used
            (missing if item in missing else changed).remove(item)
            used.add(idx[id(y)])
            if free and area(y) < 2.5 * area(item[0]) and not holds_other(y, item[0]):
                # about the same size: the same picture, cropped or marked differently - a changed picture
                recropped.append((item[0], y, visual_distance(u.a, item[0], u.b, y)))
                if same_screen is not None:
                    same_screen.add((id(item[0]), id(y)))
            else:
                merged.add(idx[id(y)])
                out.append((item[0], *found))
    changed.extend(recropped)
    return out


# ---------------------------------------------------------------- how a picture sits on the page
_PX: dict[tuple, tuple[int, int] | None] = {}


def _px_size(doc: Doc, im: Image) -> tuple[int, int] | None:
    """Pixel size of the embedded bitmap placed at this spot (None: vector art or not found)."""
    key = (doc.path, im.page, tuple(round(v) for v in im.bbox))
    if key not in _PX:
        _PX[key] = None
        try:
            pdf = (_DOCS.get(doc.path) or _DOCS.setdefault(doc.path, pymupdf.open(doc.path)))
            r = pymupdf.Rect(im.bbox)
            for info in pdf[im.page].get_image_info():
                b = pymupdf.Rect(info["bbox"])
                if abs(b.x0 - r.x0) < 2 and abs(b.y0 - r.y0) < 2 and abs(b.x1 - r.x1) < 2 and abs(b.y1 - r.y1) < 2:
                    _PX[key] = (info.get("width", 0), info.get("height", 0))
                    break
        except Exception:
            pass
    return _PX[key]


def alignment(doc: Doc, page: int, box) -> str:
    """Where a picture sits in the text column: full width, left, right, centred, or indented by N pt."""
    left, right = doc.left(page), doc.right(page)
    if right <= left:
        left, right = 0.0, doc.pages[page].width
    W = right - left
    x0, x1 = box[0], box[2]
    tol = max(4.0, 0.03 * W)
    mid, page_mid = (x0 + x1) / 2, doc.pages[page].width / 2
    if x0 <= left + tol and x1 >= right - tol and x1 - x0 >= 0.9 * W:
        return "full width"
    if abs(x0 - left) <= tol:
        return "left"
    if abs(x1 - right) <= tol:
        return "right"
    # centred in the text column or on the page (the measured column can be narrower than the real one)
    if abs(mid - (left + right) / 2) <= tol or abs(mid - page_mid) <= tol:
        return "centred"
    return f"indented {x0 - left:.0f} pt"


def describe(doc: Doc, loc: Loc) -> dict:
    """Size (pt, mm, pixels), share of the text width and alignment of the picture at `loc`."""
    im = next((m for m in doc.images if m.page == loc.page and all(abs(a - b) < 1.5 for a, b in zip(m.bbox, loc.bbox))), None)
    if im is not None:
        _, w, h = visual(doc, im)
    else:
        w, h = loc.bbox[2] - loc.bbox[0], loc.bbox[3] - loc.bbox[1]
    left, right = doc.left(loc.page), doc.right(loc.page)
    px = _px_size(doc, im) if im is not None else None
    return {"width_pt": round(w, 1), "height_pt": round(h, 1), "width_mm": round(w * 25.4 / 72, 1),
            "height_mm": round(h * 25.4 / 72, 1), "pixels": list(px) if px else None,
            "text_width_share": round(w / max(right - left, 1), 3), "align": alignment(doc, loc.page, loc.bbox)}


def _dims(d: dict) -> str:
    px = f" ({d['pixels'][0]} × {d['pixels'][1]} px)" if d.get("pixels") else " (vector)"
    return (f"{d['width_mm']:g} × {d['height_mm']:g} mm{px}, {d['text_width_share']:.0%} of text width, "
            f"{d['align']}")


def _sharpness(doc: Doc, im: Image, width_px: int = 480) -> float | None:
    """Edge contrast of the picture rendered {width_px} wide (variance of the Laplacian): a blurred
    picture has soft edges, so a much lower value than the same picture on the other side."""
    try:
        pdf = (_DOCS.get(doc.path) or _DOCS.setdefault(doc.path, pymupdf.open(doc.path)))
        r = pymupdf.Rect(im.bbox)
        z = width_px / max(r.width, 1)
        pix = pdf[im.page].get_pixmap(clip=r, matrix=pymupdf.Matrix(z, z), colorspace=pymupdf.csGRAY, alpha=False)
        g = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.stride)[:, :pix.width].astype(np.float32)
        if g.shape[0] < 8 or g.shape[1] < 8 or float(g.std()) < 4:
            return None
        lap = g[1:-1, :-2] + g[1:-1, 2:] + g[:-2, 1:-1] + g[2:, 1:-1] - 4 * g[1:-1, 1:-1]
        return float(lap.var())
    except Exception:
        return None


def _block(doc: Doc, im: Image) -> int:
    """Pixel-block size of a bitmap enlarged by repeating pixels (nearest-neighbour upscale of a small
    screenshot): rendered at its own pixel grid, every k-th row / column changes and the ones between
    repeat. 1: not blocky."""
    try:
        w = im.px[0] if im.px else 0
        r = pymupdf.Rect(im.bbox)
        if not w or r.width < 20 or r.height < 20:
            return 1
        z = min(w, 1200) / r.width
        pdf = (_DOCS.get(doc.path) or _DOCS.setdefault(doc.path, pymupdf.open(doc.path)))
        pix = pdf[im.page].get_pixmap(clip=r, matrix=pymupdf.Matrix(z, z), colorspace=pymupdf.csGRAY, alpha=False)
        g = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.stride)[:, :pix.width].astype(np.float32)
        if min(g.shape) < 32 or float(g.std()) < 4:
            return 1

        def axis(d: np.ndarray) -> int:
            for k in range(2, 9):
                for ph in range(k):
                    edge = (np.arange(len(d)) + 1 - ph) % k == 0
                    e = d[edge]
                    # the rows / columns between block edges repeat, and (nearly) every block edge changes
                    if len(e) >= 4 and e.mean() > 2 and d[~edge].mean() < 0.08 * e.mean() \
                            and (e > 0.3 * e.mean()).mean() >= 0.8:
                        return k
            return 1
        kx = axis(np.abs(np.diff(g, axis=1)).mean(axis=0))
        ky = axis(np.abs(np.diff(g, axis=0)).mean(axis=1))
        return min(kx, ky)
    except Exception:
        return 1


_INK: dict[tuple, str | None] = {}


def _cdist(a: str, b: str) -> float:
    return sum((int(a[k:k + 2], 16) - int(b[k:k + 2], 16)) ** 2 for k in (1, 3, 5)) ** 0.5


def _ink_color(doc: Doc, im: Image) -> str | None:
    """The colour of a picture's coloured pixels (white, black and greys left out), as rendered on the page:
    None when it has hardly any (a black / grey icon)."""
    key = (doc.path, im.page, tuple(round(v, 1) for v in im.bbox))
    if key not in _INK:
        _INK[key] = None
        try:
            pdf = (_DOCS.get(doc.path) or _DOCS.setdefault(doc.path, pymupdf.open(doc.path)))
            z = 64 / max(im.bbox[2] - im.bbox[0], 1)
            pix = pdf[im.page].get_pixmap(clip=pymupdf.Rect(im.bbox), matrix=pymupdf.Matrix(z, z),
                                          colorspace=pymupdf.csRGB, alpha=False)
            a = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, 3).astype(np.int16)
            sat = a.max(axis=2) - a.min(axis=2) > 50
            if sat.mean() >= 0.08:
                m = a[sat].mean(axis=0)
                _INK[key] = "#%02x%02x%02x" % tuple(int(v) for v in m)
        except Exception:
            pass
    return _INK[key]


_MARKS: dict[tuple, tuple] = {}


def _prod_marks_only(u: Unit, x: Image, y: Image, acfg: dict) -> bool:
    """Prod's picture carries red highlight marks (boxes, arrows, red text) that stage's copy does not have."""
    (ra, _, sa), (rb, _, _) = _red_marks(u.a, x), _red_marks(u.b, y)
    return ra >= acfg.get("marks_min_pixels", 20) and rb < acfg.get("marks_ratio", 0.3) * ra \
        and sa <= acfg.get("marks_max_share", 0.1)


def _labels_only(u: Unit, x: Image, y: Image, acfg: dict) -> dict | None:
    """The two pictures are the same artwork once the text on them is taken out: prod's picture with
    its labels (“A. Tabletop  B. Pole mount  C. Wall/ceiling mount” drawn into the image) and stage's
    without them, or labelled differently. The text on each picture (live text over it and OCR'd
    words in it) is blanked, the rest trimmed to what is drawn and correlated; at least
    same_picture_similarity: only the labels differ - returns each side's label words, else None."""
    (ta, la), (tb, lb) = _without_text(u.a, x), _without_text(u.b, y)
    if ta is None or tb is None or not (la or lb):
        return None  # nothing drawn, or no text on either picture: the plain comparison stands
    ca, cb = ta - ta.mean(), tb - tb.mean()
    sim = float((ca * cb).sum() / (np.sqrt((ca * ca).sum() * (cb * cb).sum()) + 1e-9))
    return {"prod": la, "stage": lb} if sim >= acfg.get("same_picture_similarity", 0.8) else None


def _without_text(doc: Doc, im: Image, n: int = 64) -> tuple["np.ndarray | None", list[str]]:
    """(n x n grayscale of the picture with its text blanked (live words over it, OCR words in it), trimmed
    to what is left - None when nothing is left; the words blanked)."""
    from .. import ocr
    pdf = (_DOCS.get(doc.path) or _DOCS.setdefault(doc.path, pymupdf.open(doc.path)))
    r = pymupdf.Rect(im.bbox)
    z = 200 / max(r.width, 1)
    pix = pdf[im.page].get_pixmap(clip=r, matrix=pymupdf.Matrix(z, z), colorspace=pymupdf.csGRAY, alpha=False)
    g = PILImage.frombytes("L", (pix.width, pix.height), pix.samples)
    bg = g.getpixel((1, 1))
    live = [w for w in doc.words if w.page == im.page and r.contains(pymupdf.Point((w.bbox[0] + w.bbox[2]) / 2,
                                                                                    (w.bbox[1] + w.bbox[3]) / 2))]
    boxes, texts = [w.bbox for w in live], [w.text for w in live]
    if ocr.available():
        # words, not specks: OCR reads strokes of a line drawing as stray characters. A word of 2+ letters /
        # digits, text-sized; a one-letter word (“A.” of “A. Tabletop”) only beside such a word on its line
        read = [(t, b) for t, b in ocr.word_boxes(doc.path, im.page, tuple(im.bbox), dpi=300)
                if any(c.isalnum() for c in t) and (b[3] - b[1]) <= 0.25 * r.height + 4]
        words = [b for t, b in read if sum(c.isalnum() for c in t) >= 2]
        beside = lambda b: any(min(b[3], w[3]) - max(b[1], w[1]) > 0.5 * (b[3] - b[1])
                               and min(abs(w[0] - b[2]), abs(b[0] - w[2])) <= 2 * (b[3] - b[1]) for w in words)
        got = [(t, b) for t, b in read if sum(c.isalnum() for c in t) >= 2 or beside(b)]
        seen = set()
        for t, b in sorted(got, key=lambda tb: (round(tb[1][1] / 4), tb[1][0])):  # reading order, each word once
            k = (t, round(b[0]), round(b[1]))
            if k not in seen:
                seen.add(k)
                boxes.append(b)
                texts.append(t)
    draw = ImageDraw.Draw(g)
    for b in boxes:
        draw.rectangle([(b[0] - r.x0 - 1.5) * z, (b[1] - r.y0 - 1.5) * z, (b[2] - r.x0 + 1.5) * z, (b[3] - r.y0 + 1.5) * z],
                       fill=bg)
    box = ImageChops.difference(g, PILImage.new("L", g.size, bg)).point(lambda v: 255 if v > 28 else 0).getbbox()
    if not box:
        return None, texts
    g = g.crop(box)
    return np.asarray(g.resize((n, n), PILImage.BILINEAR).filter(ImageFilter.GaussianBlur(1.2)), np.float64), texts


def _red_marks(doc: Doc, im: Image, width: int = 400) -> tuple[int, tuple | None, float]:
    """Strong red pixels drawn on a picture, as rendered on the page (an outline drawn over a screenshot
    is part of the page, not of the embedded bitmap): their number at `width` px across and their
    bounding box in pt, and their share of the picture. Photo reds (skin, brick: red with much green / blue)
    do not count."""
    key = (doc.path, im.page, tuple(round(v, 1) for v in im.bbox))
    if key not in _MARKS:
        _MARKS[key] = (0, None, 0.0)
        try:
            pdf = (_DOCS.get(doc.path) or _DOCS.setdefault(doc.path, pymupdf.open(doc.path)))
            x0, y0, x1, y1 = im.bbox
            z = width / max(x1 - x0, 1)
            pix = pdf[im.page].get_pixmap(clip=pymupdf.Rect(im.bbox), matrix=pymupdf.Matrix(z, z),
                                          colorspace=pymupdf.csRGB, alpha=False)
            a = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, 3).astype(np.int16)
            r, g, b = a[..., 0], a[..., 1], a[..., 2]
            m = (r >= 160) & (g <= 80) & (b <= 80) & (r - np.maximum(g, b) >= 100)
            n = int(m.sum())
            if n:
                ys, xs = np.nonzero(m)
                _MARKS[key] = (n, (x0 + xs.min() / z, y0 + ys.min() / z, x0 + (xs.max() + 1) / z, y0 + (ys.max() + 1) / z),
                               n / m.size)
        except Exception:
            pass
    return _MARKS[key]


def _annotate(u: Unit, findings: list[Finding]) -> None:
    """Every picture finding states the picture's size and alignment on each side it has."""
    for f in findings:
        if f.check != "assets" or f.detail.get("kind") == "marks":
            continue  # (red marks: the finding points at the marks, not at a whole picture to measure)
        parts = []
        for side, doc, ls in (("prod", u.a, f.baseline), ("stage", u.b, f.candidate)):
            if ls:
                d = describe(doc, ls[0])
                f.detail = {**f.detail, f"{'baseline' if side == 'prod' else 'candidate'}_image": d}
                parts.append(f"{side}: {_dims(d)}")
        if parts:
            head, nl, rest = f.message.partition("\n")  # "…\nProd: …\nStage: …": the sizes go with the headline
            f.message = head + " — " + " · ".join(parts) + nl + rest


def _drawn_counterpart(img_doc: Doc, im: Image, other: Doc, page: int, claimed: list, max_dist: float,
                       min_sim: float = 0.55, rng: tuple[int, int] | None = None) -> tuple[float, tuple, int] | None:
    """The other side's vector drawing that is the same artwork as the picture `im`: prod draws an
    illustration (a QR code, assembly steps, an OSD menu screenshot) as vectors where stage embeds it
    as an image, or the reverse. Every separate drawing (a cluster of shapes) on the page and the
    pages next to it, of a similar shape, is scaled to the picture's size and compared: the same
    pixels (>= min_sim) and the same look (visual hash <= max_dist + 0.05). The search at fixed scales
    (find_artwork) misses drawings of another size. Each drawing is claimed once. Returns
    (similarity, rect, page) or None."""
    pdf = (_DOCS.get(other.path) or _DOCS.setdefault(other.path, pymupdf.open(other.path)))
    w, h = im.bbox[2] - im.bbox[0], im.bbox[3] - im.bbox[1]
    if w < 20 or h < 15:
        return None
    ar = w / h
    best = None
    for pg in (page, page - 1, page + 1):
        if not 0 <= pg < pdf.page_count:
            continue
        try:
            rects = [pymupdf.Rect(c) for c in pdf[pg].cluster_drawings(x_tolerance=6, y_tolerance=6)]
        except Exception:
            continue
        pics = [pymupdf.Rect(i.bbox) for i in other.images if i.page == pg]
        for r in rects:
            if r.width < 20 or r.height < 15 or not 0.6 < (r.width / r.height) / ar < 1.6:
                continue
            if any((p & r).get_area() > 0.5 * r.get_area() for p in pics):
                continue  # an embedded picture: paired (or not) by the picture pairing
            if any(cp == pg and pymupdf.Rect(cr).intersects(r) for cp, cr in claimed):
                continue
            if rng and any(pymupdf.Rect(o).contains(pymupdf.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2))
                           for _, o in _outside(other, rng, pg)):
                continue  # another section's drawing (rng: the section's words on the other side)
            cand = Image(pg, tuple(r))
            sim = pixel_compare(img_doc, im, other, cand)[0]
            if sim >= min_sim and (best is None or sim > best[0]) \
                    and visual_distance(img_doc, im, other, cand) <= max_dist + 0.05:
                best = (sim, tuple(r), pg)
    if best:
        claimed.append((best[2], best[1]))
    return best


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
    confirmed_diff: set = set()  # pairs moved to "changed" by the pixel check: truly a different
                                 # picture, even when the perceptual hash still reads them as close
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
            confirmed_diff.add((id(x), id(y)))
        elif home is changed and sim >= sim_same:
            changed.remove(item)
            pairs.append(item)
        elif home is changed and sim < sim_diff:
            confirmed_diff.add((id(x), id(y)))  # already "changed": the pixels confirm it, not just the hash
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
    same_screen: set = set()  # changed pairs that are the same picture, cropped or marked differently
    combined_with = []
    for x, y, hit in _combined(u, ib, used, missing, changed, art_score, same_look, min_pic, same_screen):
        combined_with.append((x, y))
        findings.append(Finding(
            "assets", vec_sev, f"Picture combined in stage: the prod picture is part of a larger stage picture "
                               f"(prod p.{x.page + 1} ↔ stage p.{y.page + 1}, match {hit[0]:.0%})",
            [Loc(x.page, x.bbox)], [Loc(y.page, hit[1])], {"kind": "combined"}, types=["image combined"]))
    # prod pictures that have their stage picture (paired or found inside a bigger one)
    placed = [x2 for x2, _, _ in pairs] + [x2 for x2, _ in combined_with] + [c[0] for c in changed]
    for x, ax, icon in missing:
        if icon and (icons == "ignore" or (acfg.get("margin_icons", "ignore") == "ignore" and _margin_icon(u.a, x)) or _in_figure(u.a, x)):
            continue
        if _layer_of(x, placed):
            continue  # a layer of a screenshot prod builds from several image tiles: stage's picture holds it
        at = al.loc_in_b(ax)
        vec = find_artwork(u.a, x, u.b, at, art_score, same_look, claimed_b,
                           mask=_outside(u.b, u.b_range, at.page) if at else None)
        vpage = at.page if vec else None
        if not vec and not icon and at is not None:
            dc = _drawn_counterpart(u.a, x, u.b, at.page, claimed_b, same_look, rng=u.b_range)
            if dc:
                vec, vpage = (dc[0], dc[1]), dc[2]
        if vec:
            findings.append(Finding(
                "assets", vec_sev, f"Same artwork, but an embedded image in prod and drawn as vector/text in stage "
                                   f"(prod p.{x.page + 1} ↔ stage p.{vpage + 1}, match {vec[0]:.0%})",
                [Loc(x.page, x.bbox)], [Loc(vpage, vec[1])], {"kind": "raster-vs-vector"}))
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
        same = (id(x), id(y)) in same_screen
        # the same picture re-captured, re-cropped or with / without highlight marks: an image difference to
        # look at (a warning), not an error - only a picture that is not prod's picture at all is an error
        # (the pixel check, when it ran, overrides the hash guess: it already proved the two apart)
        similar = same or (vis <= acfg.get("similar_picture_distance", 0.5) and (id(x), id(y)) not in confirmed_diff)
        if similar and _prod_marks_only(u, x, y, acfg):
            continue  # the same picture, prod with red highlight marks / red text on it, stage without: no "image
            #           differs" - the marks are the image report's "Image overlay" (or nothing, ignore_prod_red_marks)
        lab = _labels_only(u, x, y, acfg) if not u.cfg.get("genuine", {}).get("report_image_labels", False) else None
        if lab is not None:
            # the same artwork, only its labels set on one side / differently: not an image difference. Labels
            # prod's picture has and stage's lacks are reported in the image report only
            gone = [t for t in lab["prod"] if t.lower() not in {s.lower() for s in lab["stage"]}]
            if gone:
                findings.append(Finding(
                    "assets", acfg.get("similar_image_severity", "warning"),
                    f"Image label missing in stage: “{' '.join(gone)}” on the prod picture is not on the stage picture "
                    f"(the artwork is the same; prod p.{x.page + 1} ↔ stage p.{y.page + 1})",
                    [Loc(x.page, x.bbox)], [Loc(y.page, y.bbox)],
                    {"kind": "labels", "labels_missing": gone, "image_report_only": True}, types=["missing image label"]))
            continue
        # the same picture at another size: that is what the reader notices - reported as bigger / smaller in
        # stage, not as "differs" (rendering it at another size is also what lowers the visual similarity)
        rel_x, rel_y = _rel_width(u.a, x), _rel_width(u.b, y)
        grow = rel_y / max(rel_x, 1e-6) - 1
        if similar and max(rel_x, rel_y) >= icon_w and (abs(rel_x - rel_y) > acfg["width_tolerance"]
                                                        or abs(grow) > acfg.get("size_ratio_tolerance", 0.2)):
            if not size_reported(rel_x, rel_y, acfg):
                continue  # not a real size difference after all (within tolerance)
            wx, hx, wy, hy = x.bbox[2] - x.bbox[0], x.bbox[3] - x.bbox[1], y.bbox[2] - y.bbox[0], y.bbox[3] - y.bbox[1]
            findings.append(Finding(
                "assets", acfg.get("size_severity", "warning"),
                f"Image {'bigger' if grow > 0 else 'smaller'} in stage by {abs(grow):.0%}: the same picture "
                f"(prod p.{x.page + 1} ↔ stage p.{y.page + 1}): {wx:.0f}×{hx:.0f} pt → {wy:.0f}×{hy:.0f} pt, "
                f"width {rel_x:.0%} → {rel_y:.0%} of content box",
                [Loc(x.page, x.bbox)], [Loc(y.page, y.bbox)],
                {"kind": "size", "baseline_width": round(rel_x, 3), "candidate_width": round(rel_y, 3),
                 "visual_distance": round(vis, 3)}, types=["image smaller" if grow < 0 else "image bigger"]))
            continue
        findings.append(Finding(
            "assets", acfg.get("similar_image_severity", "warning") if similar else acfg.get("changed_severity", "error"),
            (f"Image differs in stage: the same picture, but not identical - cropped differently or with other "
             f"marks (e.g. highlight boxes, labels) (prod p.{x.page + 1} ↔ stage p.{y.page + 1}, visual similarity "
             f"{1 - vis:.0%})" if same else
             f"Image differs in stage: another version of the same picture (re-captured or edited, e.g. without "
             f"prod's highlight marks) (prod p.{x.page + 1} ↔ stage p.{y.page + 1}, visual similarity {1 - vis:.0%})"
             if similar else
             f"Different image in stage: the picture at this spot is not the prod picture "
             f"(prod p.{x.page + 1} ↔ stage p.{y.page + 1}, visual similarity {1 - vis:.0%})"),
            [Loc(x.page, x.bbox)], [Loc(y.page, y.bbox)],
            {"kind": "changed", "visual_distance": round(vis, 3), "same_picture": same},
            # visually the same picture (re-captured, cropped, edited): its own type, apart from another picture
            types=["image version"] if similar else ["image changed"]))
        if same:
            # the same picture, only cropped differently or with other marks (a label drawn into it, prod's
            # highlight boxes): an info note, not an issue - kept out of the PDF and image reports
            findings[-1].severity, findings[-1].types = "info", ["image recropped"]
            findings[-1].detail["kind"] = "recropped"
    pending = []  # stage pictures with no artwork found in prod: a drawing of prod's own at the spot, or extra
    for n, y in enumerate(ib):
        if n in used:
            continue
        icon = _rel_width(u.b, y) < icon_w
        if icon and (icons == "ignore" or (acfg.get("margin_icons", "ignore") == "ignore" and _margin_icon(u.b, y)) or _in_figure(u.b, y)):
            continue
        at = al.loc_in_a(anc_b[n])
        vec = find_artwork(u.b, y, u.a, at, art_score, same_look, claimed_a,
                           mask=_outside(u.a, u.a_range, at.page) if at else None)
        if not vec and not icon and at is not None:
            dc = _drawn_counterpart(u.b, y, u.a, at.page, claimed_a, same_look, rng=u.a_range)
            if dc:  # the drawing sits on its own page: report it there
                vec, at = (dc[0], dc[1]), Loc(dc[2], dc[1])
        if vec and (pb := _ppi(y)) and pb < acfg.get("pixelated_ppi", 110):
            # prod draws it as vectors (sharp at any size), stage embeds a low-resolution bitmap of it
            findings.append(Finding(
                "assets", acfg.get("pixelated_severity", "error"),
                f"Image pixelated in stage: a {pb:.0f} pixels-per-inch bitmap where prod draws the artwork as "
                f"vectors (prod p.{at.page + 1} ↔ stage p.{y.page + 1})",
                [Loc(at.page, vec[1])], [Loc(y.page, y.bbox)],
                {"kind": "pixelated", "baseline_ppi": None, "candidate_ppi": round(pb)}, types=["image pixelated"]))
        if vec:
            findings.append(Finding(
                "assets", vec_sev, f"Same artwork, but drawn as vector/text in prod and an embedded image in stage "
                                   f"(prod p.{at.page + 1} ↔ stage p.{y.page + 1}, match {vec[0]:.0%})",
                [Loc(at.page, vec[1])], [Loc(y.page, y.bbox)], {"kind": "raster-vs-vector"}))
            continue
        pending.append((y, at, icon))  # after every stage picture had its artwork search (see below)
    for y, at, icon in pending:
        near = _drawing_near(u.a, u.a_range, at, claimed_a, u.b, y) if not icon and at is not None else None
        if near:
            # the drawing may be one part of a figure drawn in pieces: the whole figure, when it is the picture
            whole = _whole_figure(u.a, u.a_range, at.page, near, u.b, y)
            if whole is not None:
                xw = Image(at.page, whole)
                if visual_distance(u.b, y, u.a, xw) <= same_look or pixel_compare(u.a, xw, u.b, y)[0] >= sim_same:
                    claimed_a.append((at.page, whole))
                    findings.append(Finding(
                        "assets", vec_sev, f"Same artwork, but drawn as vector/text in prod and an embedded image in stage "
                                           f"(prod p.{at.page + 1} ↔ stage p.{y.page + 1}, drawn in parts in prod)",
                        [Loc(at.page, whole)], [Loc(y.page, y.bbox)], {"kind": "raster-vs-vector"}))
                    continue
            # prod has a drawing of its own at this spot (vector artwork, e.g. with callout numbers stage's
            # picture lacks), of about the picture's size, not the same artwork: another picture at the same
            # place, from this section only
            claimed_a.append((at.page, near))
            vis = visual_distance(u.b, y, u.a, Image(at.page, near))
            # (a note, not an error: the drawing is often the same artwork drawn otherwise, or one part of a
            # combined stage picture - nothing says the picture changed)
            findings.append(Finding(
                "assets", "info",
                f"Picture at this spot: drawn as vectors in prod, an embedded image in stage "
                f"(prod p.{at.page + 1} ↔ stage p.{y.page + 1}, visual similarity {1 - vis:.0%})",
                [Loc(at.page, near)], [Loc(y.page, y.bbox)], {"kind": "raster-vs-vector", "visual_distance": round(vis, 3)},
                types=["raster vs vector"]))
            continue
        twin = _plain_twin(u, y, at, claimed_a, acfg) if at is not None else None
        if twin:
            # prod draws the same simple artwork (a grey booklet, a plain card) as vectors near this spot - too
            # plain for the artwork search - at another size: the same picture, scaled differently
            claimed_a.append((at.page, twin))
            x = Image(at.page, twin)
            rel_x, rel_y = _rel_width(u.a, x), _rel_width(u.b, y)
            grow = rel_y / max(rel_x, 1e-6) - 1
            if size_reported(rel_x, rel_y, acfg):
                wx, hx, wy, hy = twin[2] - twin[0], twin[3] - twin[1], y.bbox[2] - y.bbox[0], y.bbox[3] - y.bbox[1]
                findings.append(Finding(
                    "assets", acfg.get("size_severity", "warning"),
                    f"Image size differs: the same picture drawn {abs(grow):.0%} {'larger' if grow > 0 else 'smaller'} in stage "
                    f"(prod p.{at.page + 1} ↔ stage p.{y.page + 1}): {wx:.0f}×{hx:.0f} pt → {wy:.0f}×{hy:.0f} pt, "
                    f"width {rel_x:.0%} → {rel_y:.0%} of content box",
                    [Loc(at.page, twin)], [Loc(y.page, y.bbox)], {"kind": "size"},
                    types=["image smaller" if grow < 0 else "image bigger"]))
            continue
        findings.append(Finding(
            "assets", acfg.get("icons", "warning") if icon else acfg.get("count_severity", "error"),
            f"Extra {kind(icon).lower()} in candidate (stage p.{y.page + 1}, {_rel_width(u.b, y):.0%} of content width)",
            [], [Loc(y.page, y.bbox)], {"kind": "extra", "icon": icon},
            baseline_at=at))
    u.image_pairs = [(x, y) for x, y, _ in pairs]
    # a matched picture flipped in stage: the hash/correlation above tolerates it (same shapes, same ink
    # mass), so a real left-right or top-bottom flip would otherwise pass as "the same picture" unreported
    mirror_gap = acfg.get("mirrored_min_gap", 0.15)
    for x, y, vis in pairs:
        if _rel_width(u.a, x) < icon_w or vis > acfg.get("mirrored_max_distance", 0.15):
            continue  # an icon (too simple to tell mirrored from not), or already different enough elsewhere
        direct, flip_h, flip_v = _mirror_scores(u.a, x, u.b, y)
        if max(flip_h, flip_v) - direct >= mirror_gap:
            axis = "left-right" if flip_h >= flip_v else "top-bottom"
            findings.append(Finding(
                "assets", acfg.get("mirrored_severity", "error"),
                f"Image mirrored in stage: the picture is flipped {axis} (prod p.{x.page + 1} ↔ stage p.{y.page + 1})",
                [Loc(x.page, x.bbox)], [Loc(y.page, y.bbox)],
                {"kind": "mirrored", "axis": axis, "direct_similarity": round(direct, 3)}, types=["image mirrored"]))
    findings += _sequence(u, [(x, y) for x, y, _ in pairs], acfg)
    # pixelated: stage prints the picture from far fewer pixels per inch than prod does (a low-resolution
    # export or a screenshot of the picture), so it looks blocky / blurred on screen and on paper
    low, ratio = acfg.get("pixelated_ppi", 110), acfg.get("pixelated_ratio", 1.5)
    for x, y, _ in pairs:
        pa, pb = _ppi(x), _ppi(y)
        # an enlarged copy of a small bitmap (pixels repeated in k x k blocks) has only 1/k of its pixels' detail
        ka = _block(u.a, x) if pa else 1
        kb = _block(u.b, y) if pb and _rel_width(u.b, y) >= icon_w else 1
        blocky = f", enlarged from a smaller bitmap ({kb}×{kb} pixel blocks)" if kb > ka else ""
        pa, pb = pa and pa / ka, pb and pb / kb
        if pa and pb and pb < low and pa >= ratio * pb:
            findings.append(Finding(
                "assets", acfg.get("pixelated_severity", "error"),
                f"Image pixelated in stage: {pb:.0f} pixels per inch as printed{blocky}, prod {pa:.0f} "
                f"(prod p.{x.page + 1} ↔ stage p.{y.page + 1})",
                [Loc(x.page, x.bbox)], [Loc(y.page, y.bbox)],
                {"kind": "pixelated", "baseline_ppi": round(pa), "candidate_ppi": round(pb)}, types=["image pixelated"]))
    # red highlight marks (boxes / outlines drawn over a screenshot to point at a field): on one side only, the
    # pictures are "the same" by their look, but the reader does not see the same thing
    # icon colour: the same icon in another colour (a blue settings gear in prod, purple in stage)
    col_tol = acfg.get("icon_color_tolerance", 60)
    by_col: dict[tuple, list] = defaultdict(list)
    for x, y, _ in pairs:
        if min(_rel_width(u.a, x), _rel_width(u.b, y)) >= icon_w:
            continue
        ca, cb = _ink_color(u.a, x), _ink_color(u.b, y)
        if ca and cb and _cdist(ca, cb) > col_tol:
            by_col[(ca, cb)].append((x, y))
    # icons left unpaired (an icon in another colour looks like another icon): the most alike unused stage icon
    free_b = [y for n, y in enumerate(ib) if n not in used and _rel_width(u.b, y) < icon_w and _ink_color(u.b, y)]
    for x, _, is_icon in missing:
        if not is_icon or not (ca := _ink_color(u.a, x)) or not free_b:
            continue
        # the shape decides (the look hash changes with the colour): the best pixel correlation
        y = max(free_b, key=lambda y: pixel_compare(u.a, x, u.b, y)[0])
        if pixel_compare(u.a, x, u.b, y)[0] >= 0.4 and visual_distance(u.a, x, u.b, y) <= 0.45 \
                and (cb := _ink_color(u.b, y)) and _cdist(ca, cb) > col_tol:
            free_b.remove(y)
            by_col[(ca, cb)].append((x, y))
    # a recoloured icon set changes consistently (blue gear -> purple gear everywhere); icons that come in several
    # colours by design (red / green / orange LED states) mix: one prod colour to several stage colours, or many
    # prod colours at once - icons told apart by their colour, not a colour change
    shade = lambda c: tuple(round(int(c[k:k + 2], 16) / 48) for k in (1, 3, 5))
    # ... and a colour some icon keeps (a purple note icon purple in stage too) was not recoloured: another
    # icon of that colour "changed" is a mismatched pair (a note icon against a red LED dot)
    kept = {shade(ca) for x, y, _ in pairs if min(_rel_width(u.a, x), _rel_width(u.b, y)) < icon_w
            and (ca := _ink_color(u.a, x)) and (cb := _ink_color(u.b, y)) and _cdist(ca, cb) <= col_tol}
    by_col = {k: v for k, v in by_col.items() if shade(k[0]) not in kept}
    to = defaultdict(set)
    for ca, cb in by_col:
        to[shade(ca)].add(shade(cb))
    if len(to) >= 3 or any(len(v) > 1 for v in to.values()):
        by_col = {}
    for (ca, cb), xs in by_col.items():
        la, lb = [Loc(x.page, x.bbox) for x, _ in xs], [Loc(y.page, y.bbox) for _, y in xs]
        findings.append(Finding(
            "assets", acfg.get("icon_color_severity", "error"),
            f"Icon colour differs — {len(xs)} icon(s) (prod p.{xs[0][0].page + 1} ↔ stage p.{xs[0][1].page + 1})"
            f"\nProd: icon in {ca.upper()}\nStage: the same icon in {cb.upper()}",
            la, lb, {"kind": "icon color", "baseline_color": ca, "candidate_color": cb},
            types=["image changed"], links=list(zip(la, lb))))
    min_px, keep = acfg.get("marks_min_pixels", 20), acfg.get("marks_ratio", 0.3)
    max_share = acfg.get("marks_max_share", 0.1)
    for x, y, _ in pairs:
        if min(_rel_width(u.a, x), _rel_width(u.b, y)) < icon_w:
            continue  # an icon: a red LED / power icon is red all over, its colour is not a mark on it
        (ra, box_a, sa), (rb, box_b, sb) = _red_marks(u.a, x), _red_marks(u.b, y)
        for n_on, n_off, on_side, box, share in ((ra, rb, "prod", box_a, sa), (rb, ra, "stage", box_b, sb)):
            if n_on < min_px or n_off >= keep * n_on or share > max_share:
                continue  # (a mark is a thin outline: a picture that is largely red is red by itself)
            if on_side == "prod" and acfg.get("ignore_prod_red_marks", True):
                continue  # prod's red highlight boxes / red text on a picture, left out in stage: not reported
            # the marked area on the side that has the marks, and the same area of the other side's picture
            src, dst = (x, y) if on_side == "prod" else (y, x)
            fx0, fy0 = (box[0] - src.bbox[0]) / (src.bbox[2] - src.bbox[0]), (box[1] - src.bbox[1]) / (src.bbox[3] - src.bbox[1])
            fx1, fy1 = (box[2] - src.bbox[0]) / (src.bbox[2] - src.bbox[0]), (box[3] - src.bbox[1]) / (src.bbox[3] - src.bbox[1])
            w, h = dst.bbox[2] - dst.bbox[0], dst.bbox[3] - dst.bbox[1]
            other = (dst.bbox[0] + fx0 * w, dst.bbox[1] + fy0 * h, dst.bbox[0] + fx1 * w, dst.bbox[1] + fy1 * h)
            pad = lambda r: (r[0] - 5, r[1] - 5, r[2] + 5, r[3] + 5)  # the highlight around the marks, not on them
            la, lb = (Loc(x.page, pad(box)), Loc(y.page, pad(other))) if on_side == "prod" else \
                (Loc(x.page, pad(other)), Loc(y.page, pad(box)))
            what = "missing" if on_side == "prod" else "added"
            findings.append(Finding(
                "assets", acfg.get("marks_severity", "error"),
                f"Image different in stage: red highlight marks {what} (prod p.{x.page + 1} ↔ stage p.{y.page + 1})"
                + (f"\nProd: red marks drawn on the picture\nStage: the same picture without the red marks" if what == "missing"
                   else f"\nProd: the picture without red marks\nStage: red marks drawn on the same picture"),
                [la], [lb], {"kind": "marks", "marks": what, "baseline_red_px": ra, "candidate_red_px": rb,
                             "image_report_only": True},
                types=["image marks"], links=[(la, lb)]))  # the same picture with / without a red overlay
    blur_ratio = acfg.get("blurred_ratio", 0.35)
    for x, y, _ in pairs:
        ar_x, ar_y = _aspect(u.a, x), _aspect(u.b, y)
        rel_x, rel_y = _rel_width(u.a, x), _rel_width(u.b, y)
        # blurred: the same picture with much softer edges in stage (a scaled-up or re-compressed copy)
        if rel_x >= icon_w:
            sa, sb = _sharpness(u.a, x), _sharpness(u.b, y)
            if sa and sb and sb < blur_ratio * sa:
                findings.append(Finding(
                    "assets", acfg.get("blurred_severity", "error"),
                    f"Image blurred in stage: edge sharpness {sb:.0f} vs {sa:.0f} in prod ({sb / sa:.0%}) "
                    f"(prod p.{x.page + 1} ↔ stage p.{y.page + 1})",
                    [Loc(x.page, x.bbox)], [Loc(y.page, y.bbox)],
                    {"kind": "blurred", "baseline_sharpness": round(sa), "candidate_sharpness": round(sb)},
                    types=["image blurred"]))
        problems = []
        al_x, al_y = alignment(u.a, x.page, x.bbox), alignment(u.b, y.page, y.bbox)
        if acfg.get("check_alignment", True):
            # skip center alignment changes if configured; only report left/right differences
            skip_center = (acfg.get("ignore_center_alignment", True) and 
                          ("centred" in al_x or "centred" in al_y))
            # only a real move counts: the picture's centre shifts by >= align_min_shift of the text width
            # (fully left vs fully right); a wide picture labelled "full width" in one PDF and "right" in the
            # other, or a few points of difference, is not an alignment issue
            def centre(doc, im):
                l, r = doc.left(im.page), doc.right(im.page)
                return ((im.bbox[0] + im.bbox[2]) / 2 - l) / max(r - l, 1)
            shift = abs(centre(u.a, x) - centre(u.b, y))
            # [assets] alignment_sides_only: only a complete move from one side to the other (left <-> right)
            side = lambda a: a.split()[0] if a.split() and a.split()[0] in ("left", "right") else None
            sides_ok = not acfg.get("alignment_sides_only", True) or (side(al_x) and side(al_y) and side(al_x) != side(al_y))
            if (max(rel_x, rel_y) >= icon_w and al_x != al_y and sides_ok and
                not (al_x.startswith("indented") and al_y.startswith("indented")) and
                not skip_center and shift >= acfg.get("align_min_shift", 0.15)):
                problems.append(f"alignment {al_x} → {al_y}")
        if abs(ar_x - ar_y) / ar_x > acfg["aspect_tolerance"]:
            problems.append(f"aspect {ar_x:.2f} → {ar_y:.2f}")
        grow = rel_y / max(rel_x, 1e-6) - 1
        if max(rel_x, rel_y) >= icon_w and size_reported(rel_x, rel_y, acfg):
            wx, hx = x.bbox[2] - x.bbox[0], x.bbox[3] - x.bbox[1]
            wy, hy = y.bbox[2] - y.bbox[0], y.bbox[3] - y.bbox[1]
            problems.insert(0, f"{wx:.0f}×{hx:.0f} pt → {wy:.0f}×{hy:.0f} pt, "
                               f"width {rel_x:.0%} → {rel_y:.0%} of content box")
        if problems:
            # stretched = stage draws the picture out of its own pixel proportions and prod does not
            # (a different measured aspect alone is usually another crop or frame of the same picture)
            tol = 1 + acfg.get("distorted_tolerance", 0.25)
            off = lambda im: max(im.stretch, 1 / max(im.stretch, 1e-6))
            distorted = off(y) >= tol and off(x) < 1.1 and rel_x >= icon_w
            findings.append(Finding(
                "assets", "error" if distorted else acfg.get("geometry_severity", "warning"),
                ("Image distorted in stage (stretched or squashed)" if distorted else kind(rel_x < icon_w)
                 + (f" {'smaller' if grow < 0 else 'bigger'} in stage by {abs(grow):.0%}" if " pt → " in problems[0] else ""))
                + f" (prod p.{x.page + 1} ↔ stage p.{y.page + 1}): " + ", ".join(problems),
                [Loc(x.page, x.bbox)], [Loc(y.page, y.bbox)],
                {"baseline_aspect": round(ar_x, 3), "candidate_aspect": round(ar_y, 3),
                 "baseline_width": round(rel_x, 3), "candidate_width": round(rel_y, 3)},
                types=["image distorted"] if distorted else (["image alignment"] if all(p.startswith("alignment") for p in problems)
                                                             # a real size change, either direction
                                                             else (["image smaller" if grow < 0 else "image bigger"]
                                                                   if " pt → " in problems[0] else [])),
            ))
    if acfg.get("grid_repair", True):
        findings = _grid_repair(u, findings, acfg)
    if acfg.get("check_vertical_alignment", True):
        findings += _vertical_alignment(u, acfg)
    _annotate(u, findings)
    return findings


_ICONS: dict[tuple, list] = {}


def _icons(doc: Doc, page: int, max_pt: float) -> list[tuple]:
    """Small pictures of a page - embedded images and vector drawings up to max_pt wide and high (a warning
    triangle, a note icon beside its paragraph)."""
    key = (doc.path, len(doc.words), page, max_pt)
    if key not in _ICONS:
        if len(_ICONS) > 2000:
            _ICONS.clear()
        out = [tuple(im.bbox) for im in doc.images if im.page == page
               and 8 <= im.bbox[2] - im.bbox[0] <= max_pt and 8 <= im.bbox[3] - im.bbox[1] <= max_pt]
        try:
            pdf = _DOCS.get(doc.path) or _DOCS.setdefault(doc.path, pymupdf.open(doc.path))
            over = lambda r: any(min(r.x1, o[2]) > max(r.x0, o[0]) and min(r.y1, o[3]) > max(r.y0, o[1]) for o in out)
            drawings = pdf[page].get_drawings()
            for r in pdf[page].cluster_drawings(drawings=drawings):
                if 8 <= r.width <= max_pt and 8 <= r.height <= max_pt and not over(r):
                    out.append(tuple(r))
            # a page whose drawings all hang together (crop marks, a frame around everything) is one cluster: the
            # icons are then found as what they are drawn with - a small shape filled in a colour (not white)
            for d in drawings:
                r, fill = pymupdf.Rect(d["rect"]), d.get("fill")
                if fill is not None and sum(fill[:3]) < 2.4 and 12 <= r.width <= max_pt and 12 <= r.height <= max_pt \
                        and 0.5 <= r.width / max(r.height, 1) <= 2.0 and not over(r):
                    out.append(tuple(r))
        except Exception:
            pass
        _ICONS[key] = out
    return _ICONS[key]


def _vertical_alignment(u: Unit, acfg: dict) -> list[Finding]:
    """An icon beside a paragraph: where it sits against that text - centred on it in prod, at its first line in
    stage (or the other way round). The paragraph is the same on both sides (its matched words), so the icons are
    compared whatever they are made of (a vector drawing in prod, a bitmap in stage: never paired as pictures)."""
    max_pt, reach = acfg.get("icon_beside_text_max_pt", 60), acfg.get("icon_beside_text_gap_pt", 45)
    key = lambda t: "".join(c for c in (t or "").lower() if c.isalnum())
    a_keys = [(k, key(u.a.words[k].text)) for k in range(*u.a_range)]
    a_keys = [(k, t) for k, t in a_keys if t]

    def find(b_words: list[int], start: int) -> int | None:
        """Prod index of the first of these stage words, as the same consecutive words at or after `start`."""
        want = [key(u.b.words[j].text) for j in b_words]
        want = [t for t in want if t]
        if not want:
            return None
        for n in range(len(a_keys) - len(want) + 1):
            if a_keys[n][0] >= start and all(a_keys[n + m][1] == want[m] for m in range(len(want))):
                return a_keys[n][0]
        return None

    by_line: dict[int, list[int]] = defaultdict(list)
    for j in range(*u.b_range):
        by_line[u.b.words[j].line].append(j)
    lines = sorted(by_line)
    out, seen = [], set()
    for page in sorted({u.b.words[j].page for j in range(*u.b_range)}):
        for ic in _icons(u.b, page, max_pt):
            cy, h = (ic[1] + ic[3]) / 2, ic[3] - ic[1]
            # the paragraph right of the icon: the line level with it, then the lines running on below / above
            # at the same left edge
            near = [li for li in lines if u.b.lines[li].page == page and 0 <= u.b.lines[li].bbox[0] - ic[2] <= reach
                    and u.b.lines[li].bbox[1] - 4 <= cy <= u.b.lines[li].bbox[3] + max(h, 12)]
            if not near:
                continue
            start = min(near, key=lambda li: abs((u.b.lines[li].bbox[1] + u.b.lines[li].bbox[3]) / 2 - cy))
            x0 = u.b.lines[start].bbox[0]
            block, k = [start], lines.index(start)
            for step in (1, -1):
                n = k + step
                while 0 <= n < len(lines):
                    ln, prev = u.b.lines[lines[n]], u.b.lines[block[-1] if step == 1 else block[0]]
                    gap = ln.bbox[1] - prev.bbox[3] if step == 1 else prev.bbox[1] - ln.bbox[3]
                    if ln.page != page or abs(ln.bbox[0] - x0) > 4 or gap > 0.6 * (prev.bbox[3] - prev.bbox[1]):
                        break
                    block.append(lines[n]) if step == 1 else block.insert(0, lines[n])
                    n += step
            top_b, bot_b = u.b.lines[block[0]].bbox[1], u.b.lines[block[-1]].bbox[3]
            words_b = [j for li in block for j in by_line[li] if u.b.words[j].norm]
            if bot_b - top_b < 2 * h or len(words_b) < 6:
                continue  # one or two lines: centred and top are the same place
            # the same paragraph in prod: by its text (its first and its last words), since a block read in another
            # order on the two sides (columns) is not in the matched word pairs
            ia = find(words_b[:4], u.a_range[0])
            iz = find(words_b[-3:], ia) if ia is not None else None
            if ia is None or iz is None:
                continue
            iz += 2
            wa, wz = u.a.words[ia], u.a.words[iz]
            if wa.page != wz.page or iz - ia > 3 * len(words_b):
                continue
            top_a, bot_a = u.a.lines[wa.line].bbox[1], u.a.lines[wz.line].bbox[3]
            ax0 = min(u.a.lines[u.a.words[k].line].bbox[0] for k in range(ia, iz + 1))
            cands = [o for o in _icons(u.a, wa.page, max_pt) if 0 <= ax0 - o[2] <= reach
                     and top_a - (o[3] - o[1]) <= (o[1] + o[3]) / 2 <= bot_a + (o[3] - o[1])]
            if len(cands) != 1 or bot_a - top_a < 2 * (cands[0][3] - cands[0][1]):
                continue  # no icon beside the prod paragraph (a missing icon is another check's), or not clear which
            pa = cands[0]
            rel = lambda box, top, bot: ((box[1] + box[3]) / 2 - top) / max(bot - top, 1)
            ra, rb = rel(pa, top_a, bot_a), rel(ic, top_b, bot_b)
            name = lambda r: "at the top of" if r < 0.3 else "at the bottom of" if r > 0.7 else "centred on"
            if name(ra) == name(rb) or abs(ra - rb) < acfg.get("icon_vertical_tolerance", 0.25) or (page, ic) in seen:
                continue
            seen.add((page, ic))
            out.append(Finding(
                "assets", acfg.get("vertical_alignment_severity", "warning"),
                f"Image vertical alignment differs: {name(ra)} its text in prod, {name(rb)} its text in stage "
                f"(prod p.{wa.page + 1} ↔ stage p.{page + 1}, beside “{' '.join(u.b.words[j].text for j in words_b[:6])}”)",
                [Loc(wa.page, pa)], [Loc(page, ic)],
                {"kind": "vertical alignment", "baseline": round(ra, 2), "candidate": round(rb, 2)},
                types=["image vertical alignment"], links=[(Loc(wa.page, pa), Loc(page, ic))]))
    return out


_GRID_KINDS = ("missing", "extra", "changed", "raster-vs-vector")


def _grid_repair(u: Unit, findings: list[Finding], acfg: dict) -> list[Finding]:
    """A figure of several panels (a 2 x 2 grid of steps) has no text between its pictures, and the two PDFs
    may build each panel differently (prod: a drawn monitor plus a small OSD bitmap; stage: one bitmap per
    panel) - pairing along the text then crosses the panels: "missing" + "extra" + "different" pictures that
    are all there. When a page pair has such a mix, its pictures are paired again by their place in the figure:
    each side's pictures scaled to one frame, a prod picture with the stage picture at the same spot."""
    groups: dict[tuple, list[Finding]] = {}
    for f in findings:
        k = (f.detail or {}).get("kind")
        if f.check != "assets" or k not in _GRID_KINDS:
            continue
        pa = f.baseline[0].page if f.baseline else None
        pb = f.candidate[0].page if f.candidate else None
        if pa is None and f.baseline_at is not None:
            pa = f.baseline_at.page
        if pb is None and f.candidate_at is not None:
            pb = f.candidate_at.page
        if pa is None or pb is None:
            continue
        groups.setdefault((pa, pb), []).append(f)
    out = list(findings)
    for (pa, pb), fs in groups.items():
        kinds = {f.detail.get("kind") for f in fs}
        if len(fs) < 3 or not ({"missing", "extra"} & kinds):
            continue  # an ordinary missing / extra picture: nothing crossed
        P = list(dict.fromkeys(tuple(l.bbox) for f in fs for l in f.baseline if l.page == pa))
        S = list(dict.fromkeys(tuple(l.bbox) for f in fs for l in f.candidate if l.page == pb))
        if len(P) < 2 or len(S) < 2:
            continue
        frame = lambda bs: (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs))
        fa, fb = frame(P), frame(S)
        to_b = lambda x, y: (fb[0] + (x - fa[0]) / max(fa[2] - fa[0], 1) * (fb[2] - fb[0]),
                             fb[1] + (y - fa[1]) / max(fa[3] - fa[1], 1) * (fb[3] - fb[1]))
        to_a = lambda x, y: (fa[0] + (x - fb[0]) / max(fb[2] - fb[0], 1) * (fa[2] - fa[0]),
                             fa[1] + (y - fb[1]) / max(fb[3] - fb[1], 1) * (fa[3] - fa[1]))
        inside = lambda pt, b, pad=4: b[0] - pad <= pt[0] <= b[2] + pad and b[1] - pad <= pt[1] <= b[3] + pad
        mapping = {}
        for b in P:
            c = to_b((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
            hit = next((s for s in S if inside(c, s)), None)
            if hit is None:
                break
            mapping[b] = hit
        if len(mapping) < len(P):
            continue  # the figures are not laid out alike: leave the text pairing
        new = []
        for b, sb in mapping.items():
            x, y = Image(pa, b), Image(pb, sb)
            vis = visual_distance(u.a, x, u.b, y)
            part = (b[2] - b[0]) * (b[3] - b[1]) / max((sb[2] - sb[0]) * (sb[3] - sb[1]), 1)
            vector = any(f.detail.get("kind") == "raster-vs-vector" and f.baseline and tuple(f.baseline[0].bbox) == b for f in fs)
            if vector:
                msg = (f"Same panel, but drawn as vector/text in prod and an embedded image in stage "
                       f"(prod p.{pa + 1} ↔ stage p.{pb + 1})")
                new.append(Finding("assets", acfg.get("raster_vs_vector_severity", "info"), msg, [Loc(pa, b)], [Loc(pb, sb)],
                                   {"kind": "raster-vs-vector"}))
                continue
            what = ("the prod picture is one part of the stage picture of this panel (stage draws the panel as one "
                    "image)" if part < 0.6 else "another version of the picture of this panel")
            new.append(Finding(
                "assets", acfg.get("similar_image_severity", "warning"),
                f"Image differs in stage: {what} (prod p.{pa + 1} ↔ stage p.{pb + 1}, figure panel matched by "
                f"position, visual similarity {1 - vis:.0%})",
                [Loc(pa, b)], [Loc(pb, sb)], {"kind": "changed", "visual_distance": round(vis, 3), "grid": True}))
        # stage panels no prod picture maps to: a prod drawing at that spot is the same panel drawn as vectors
        used_s = set(mapping.values())
        try:
            clusters = [tuple(r) for r in pymupdf.open(u.a.path)[pa].cluster_drawings() if r.width >= 30 and r.height >= 30]
        except Exception:
            clusters = []
        for sb in S:
            if sb in used_s:
                continue
            c = to_a((sb[0] + sb[2]) / 2, (sb[1] + sb[3]) / 2)
            # the smallest drawing at that spot (a page-wide cluster of rules is not the panel)
            drawn = min((r for r in clusters if inside(c, r, 8)
                         and (r[2] - r[0]) * (r[3] - r[1]) <= 3 * (sb[2] - sb[0]) * (sb[3] - sb[1]) * (fa[2] - fa[0]) / max(fb[2] - fb[0], 1)),
                        key=lambda r: (r[2] - r[0]) * (r[3] - r[1]), default=None)
            if drawn is not None:
                new.append(Finding("assets", acfg.get("raster_vs_vector_severity", "info"),
                                   f"Same panel, but drawn as vector/text in prod and an embedded image in stage "
                                   f"(prod p.{pa + 1} ↔ stage p.{pb + 1})", [Loc(pa, drawn)], [Loc(pb, sb)],
                                   {"kind": "raster-vs-vector"}))
            else:
                old = next((f for f in fs if f.detail.get("kind") == "extra" and f.candidate and tuple(f.candidate[0].bbox) == sb), None)
                if old is not None:
                    new.append(old)
        drop = {id(f) for f in fs}
        out = [f for f in out if id(f) not in drop] + new
    return out
