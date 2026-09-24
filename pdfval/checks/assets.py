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

Paired images are compared for aspect ratio and width relative to the content
box (independent of page size). Unpaired ones are reported as missing/extra,
with the aligned position on the other side for the screenshot.
"""
from __future__ import annotations

import numpy as np
import pymupdf
from PIL import Image as PILImage
from PIL import ImageChops

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
    tpl_full = _gray(img_doc, im.page, im.bbox, 1.0)
    base_w = (other.right(at.page) - other.left(at.page)) * _rel_width(img_doc, im)
    best = None
    hay_cache: dict[float, "np.ndarray"] = {}
    for s in (0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.15, 1.3, 1.5):
        # correlate at a resolution where the picture is ~48 px wide: enough to recognise
        # a label or logo, and cheap (the cost grows with template area x search area)
        k = round(min(0.6, 48 / max(base_w * s, 1)), 3)
        if k not in hay_cache:
            hay = _gray(other, at.page, (0, y0, pw, y1), k)
            for pg, r in claimed or []:
                if pg == at.page:
                    hay[max(0, int((r[1] - y0) * k)):max(0, int((r[3] - y0) * k)), int(r[0] * k):int(r[2] * k)] = 255
            hay_cache[k] = hay
        hay = hay_cache[k]
        tw = int(base_w * s * k)
        th = int(tw * im_aspect(im))
        if tw < 12 or th < 8 or tw >= hay.shape[1] or th >= hay.shape[0]:
            continue
        tpl = np.asarray(PILImage.fromarray(tpl_full.astype(np.uint8)).resize((tw, th), PILImage.BILINEAR), np.float32)
        ncc = _ncc(hay, tpl)
        if ncc is None:
            continue
        iy, ix = np.unravel_index(int(ncc.argmax()), ncc.shape)
        score = float(ncc[iy, ix])
        if best is None or score > best[0]:
            x0, yy0 = ix / k, y0 + iy / k
            best = (score, (x0, yy0, x0 + tw / k, yy0 + th / k))
    if not best or best[0] < min_score:
        return None
    if visual_distance(img_doc, im, other, Image(at.page, best[1])) > max_dist:
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


def _aspect(doc: Doc, im: Image) -> float:
    _, w, h = visual(doc, im)
    return w / max(h, 1e-6)


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
    pairs, changed, missing = [], [], []
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
        if best and best[2] <= same_look:
            used.add(best[1])
            pairs.append((x, ib[best[1]], best[2]))
        elif best and best[3] <= same_spot and not x_icon:
            used.add(best[1])  # same position, different picture: replaced image
            changed.append((x, ib[best[1]], best[2]))
        else:
            missing.append((x, ax, x_icon))

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
    for x, y, vis in changed:
        findings.append(Finding(
            "assets", acfg.get("changed_severity", "warning"),
            f"Image content differs (prod p.{x.page + 1} ↔ stage p.{y.page + 1}, visual similarity {1 - vis:.0%})",
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
            findings.append(Finding(
                "assets", acfg.get("geometry_severity", "warning"),
                f"{kind(rel_x < icon_w)} (prod p.{x.page + 1} ↔ stage p.{y.page + 1}): " + ", ".join(problems),
                [Loc(x.page, x.bbox)], [Loc(y.page, y.bbox)],
                {"baseline_aspect": round(ar_x, 3), "candidate_aspect": round(ar_y, 3),
                 "baseline_width": round(rel_x, 3), "candidate_width": round(rel_y, 3)},
            ))
    return findings
