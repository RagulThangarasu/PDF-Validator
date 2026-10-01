"""Genuine issues: what is really wrong in stage, as opposed to presentation noise.

Most findings (CSS, wrapping, case/punctuation, spacing) are differences a reviewer
can live with. The *genuine* ones are listed in `[genuine] types`: a section or
table header missing, a section duplicated, an image missing or broken, an image
or content block that landed in the wrong section, tables merged, data missing,
links that don't work or point to the wrong section.

This module
* finds the issues that need the whole document rather than one section:
  images and content blocks placed in the wrong section, and links whose target
  is a different section in stage than in prod;
* tags every finding with `genuine` and a plain-language `description`
  (what is wrong, where, why it matters) for the genuine-issues report.
"""
from __future__ import annotations

import re
from collections import Counter

import pymupdf

from . import ocr
from .checks import Aligner, Unit, locs, snippet
from .checks.assets import icon_max, pixel_compare, section_images, visual_distance
from .checks.tables import is_curve
from .checks.integrity import links as page_links
from .model import Doc, Finding, Image, Loc

_TOKEN = re.compile(r"[\w%]+")
_GOTO = (pymupdf.LINK_GOTO, pymupdf.LINK_NAMED)


def cross_section(results: list[tuple[Unit, list[Finding]]], A: Doc, B: Doc, cfg: dict, mode: str = "pdf",
                  progress=None) -> None:
    """Relate one-sided findings across sections (mutates the finding lists). progress(message): what it is doing."""
    say = progress or (lambda m: None)
    gcfg = cfg.get("genuine", {})
    units = [u for u, _ in results]
    _images(results, A, B, cfg)
    _duplicate_images(results, A, B, cfg)
    _content(results, gcfg.get("moved_min_words", 5), gcfg.get("moved_overlap", 0.8))
    _duplicate_content(results, A, gcfg.get("moved_min_words", 5), gcfg.get("moved_overlap", 0.8))
    say("Looking for missing text drawn as graphics")
    _text_as_graphics(results, A, B, cfg)  # first: it finds the text at its own spot, OCR anywhere
    _text_in_images(results, A, B, cfg, say)
    say("Checking links and image labels")
    _image_labels(results, A, B, cfg)
    _label_wording(results, A, B, cfg)
    _text_in_matched_artwork(results)
    if cfg["integrity"].get("links", True):
        for u, findings in results:
            findings.extend(_links(u, units, mode))


def _unit_of(units: list[Unit], side: str, idx: int | None) -> Unit | None:
    """Section containing word idx; a link target just above a heading (same page, within
    ~4 lines) belongs to that heading's section, not the end of the one before."""
    if idx is None:
        return None
    doc = units[0].a if side == "a" else units[0].b
    w = doc.words[idx]
    start = lambda u: u.a_range[0] if side == "a" else u.b_range[0]
    near = [u for u in units if idx <= start(u) < len(doc.words) and doc.words[start(u)].page == w.page
            and doc.words[start(u)].bbox[1] - w.bbox[1] < 60]
    if near:
        return min(near, key=start)
    for u in units:
        r = u.a_range if side == "a" else u.b_range
        if r[0] <= idx < r[1]:
            return u
    return None


def _images(results, A: Doc, B: Doc, cfg: dict) -> None:
    """A prod image missing from its section that shows up as an extra image in another section."""
    thr = cfg["assets"].get("visual_match_threshold", 0.25)
    extras = [(u, f) for u, fs in results for f in fs
              if f.check == "assets" and f.detail.get("kind") == "extra" and not f.detail.get("icon") and f.candidate]
    for u, fs in results:
        for f in [f for f in fs if f.check == "assets" and f.detail.get("kind") == "missing" and f.baseline]:
            x = Image(f.baseline[0].page, f.baseline[0].bbox)
            best = None
            for eu, ef in extras:
                if eu is u:
                    continue
                y = Image(ef.candidate[0].page, ef.candidate[0].bbox)
                d = visual_distance(A, x, B, y)
                if d <= thr and (best is None or d < best[0]):
                    best = (d, eu, ef)
            if not best:
                continue
            _, eu, ef = best
            extras.remove((eu, ef))
            _of(results, eu).remove(ef)
            f.message = (f"Image placed in the wrong section: in prod it is in “{u.title}” (p.{x.page + 1}), "
                         f"in stage it is in “{eu.title}” (p.{ef.candidate[0].page + 1})")
            f.candidate, f.candidate_at = ef.candidate, None
            f.severity, f.critical, f.types = "error", True, ["image in wrong section"]
            f.detail = {**f.detail, "kind": "wrong-section", "candidate_section": eu.title}


def _duplicate_images(results, A: Doc, B: Doc, cfg: dict) -> None:
    """An extra stage image that is the same picture as a prod image of another section, which is
    still in place there: the picture was duplicated into the wrong section."""
    acfg = cfg["assets"]
    thr, same = acfg.get("visual_match_threshold", 0.25), acfg.get("same_picture_similarity", 0.8)
    homes = [(u, x) for u, _ in results for x in section_images(A, u.a_range) if not icon_max(A, x, acfg)]
    for u, fs in results:
        for f in [f for f in fs if f.check == "assets" and f.detail.get("kind") == "extra"
                  and not f.detail.get("icon") and f.candidate]:
            y = Image(f.candidate[0].page, f.candidate[0].bbox)
            best = None
            for hu, x in homes:
                if hu is u:
                    continue  # same section: pictures in another order, not a duplicate
                d = visual_distance(A, x, B, y)
                if d <= thr and (best is None or d < best[0]) and pixel_compare(A, x, B, y)[0] >= same:
                    best = (d, hu, x)
            if not best:
                continue
            _, hu, x = best
            f.message = (f"Image duplicated: the picture from “{hu.title}” (prod p.{x.page + 1}) appears again in "
                         f"“{u.title}” in stage (p.{y.page + 1}), where prod has no such picture")
            f.baseline, f.baseline_at = [Loc(x.page, x.bbox)], None
            f.severity, f.critical, f.types = "error", True, ["duplicate image"]
            f.detail = {**f.detail, "kind": "duplicate", "baseline_section": hu.title}


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text or "")]


def _duplicate_content(results, A: Doc, min_words: int, overlap: float) -> None:
    """Extra stage text that repeats a passage of prod (usually from another section): content
    duplicated into the wrong place, not new content."""
    from difflib import SequenceMatcher
    prod = [(u, [t for w in A.words[u.a_range[0]:u.a_range[1]] for t in _tokens(w.text)]) for u, _ in results]
    bags = [(u, Counter(toks)) for u, toks in prod]
    for u, fs in results:
        for f in [f for f in fs if f.check == "content" and f.detail.get("op") in ("insert", "replace")
                  and "extra text" in (f.types or []) and not f.detail.get("moved_in")]:
            extra = _tokens(f.detail.get("candidate_text"))
            if len(extra) < min_words:
                continue
            need = Counter(extra)
            best = None
            for (pu, toks), (_, bag) in zip(prod, bags):
                if pu is u:
                    continue  # repeated within its own section is table reading order, not a duplicate
                if sum((need & bag).values()) < overlap * len(extra):
                    continue  # cheap pre-filter before the sequence match
                m = SequenceMatcher(None, toks, extra, autojunk=False).find_longest_match(0, len(toks), 0, len(extra))
                if m.size >= overlap * len(extra) and (best is None or m.size > best[1]):
                    best = (pu, m.size)
            if not best:
                continue
            src = best[0]
            f.message = (f"Content duplicated in stage: “{snippet_text(f.detail.get('candidate_text'))}” is prod text "
                         f"from “{src.title}”, repeated in “{u.title}”")
            f.severity, f.types = "error", ["duplicate content"]
            f.detail = {**f.detail, "op": "duplicate", "baseline_section": src.title}


def _figures(A: Doc, cfg: dict) -> dict[int, list[pymupdf.Rect]]:
    """Where prod has figures, per page: its pictures (not icons) and its line drawings - vector
    paths with curves (a lamp, a person, a hand), merged into one box per drawing. Table rules
    and callout backgrounds are straight lines and fills, not figures."""
    out: dict[int, list[pymupdf.Rect]] = {}
    for im in A.images:
        if not icon_max(A, im, cfg["assets"]):
            out.setdefault(im.page, []).append(pymupdf.Rect(im.bbox))
    try:
        doc = pymupdf.open(A.path)
    except Exception:
        return out
    with doc:
        for pno, page in enumerate(doc):
            boxes = [pymupdf.Rect(d["rect"]) for d in page.get_drawings()
                     if d.get("color") is not None and any(is_curve(it) for it in d["items"])]
            merged: list[pymupdf.Rect] = []
            for r in boxes:  # merge strokes that touch (within 12 pt) into one figure box
                r = pymupdf.Rect(r)
                for m in [m for m in merged if (m + (-12, -12, 12, 12)).intersects(r)]:
                    r |= m
                    merged.remove(m)
                merged.append(r)
            out.setdefault(pno, []).extend(m for m in merged if m.width >= 30 and m.height >= 30)
    return out


def _text_in_images(results, A: Doc, B: Doc, cfg: dict, progress=None) -> None:
    """Missing prod text that stage draws inside a picture (figure labels, dimension callouts baked
    into the image): read the stage pictures with OCR - the section's own pages and the pages next
    to it first, then the rest of the document - and when the words are there, the text is not
    missing: an info finding "Text in image", counted as present in the content %.
    Only text that sits on or next to a prod figure is a label; missing body text is not looked for."""
    ccfg = cfg["content"]
    if not ccfg.get("ocr_images", True) or not ocr.available():
        return
    max_words, dpi = ccfg.get("ocr_max_words", 60), ccfg.get("ocr_dpi", 300)
    min_pt = ccfg.get("ocr_min_image_pt", 40)
    pics = [im for im in B.images if im.bbox[2] - im.bbox[0] >= min_pt and im.bbox[3] - im.bbox[1] >= min_pt * 0.5]
    pad, figs = ccfg.get("ocr_label_distance_pt", 40), None  # a label sits on its figure or this close to it
    if not pics:
        return
    work = []  # (unit, findings to look for, pictures on / next to the section's pages first)
    for u, fs in results:
        todo = [f for f in fs if f.check == "content" and "missing text" in (f.types or [])
                and 0 < len(_tokens(f.detail.get("baseline_text"))) <= max_words and f.baseline]
        if todo and figs is None:
            figs = _figures(A, cfg)
        # most of its lines on / next to a figure (dimension labels also sit by straight arrows)
        # on the figure, or level with it and beside it (callouts to the right / left of a drawing:
        # "Ceiling/Wall mount screw: M4", "Unit: mm") within half the figure's width
        beside = lambda b, r: b.y1 > r.y0 - pad and b.y0 < r.y1 + pad and max(r.x0 - b.x1, b.x0 - r.x1) <= 0.5 * r.width
        on_fig = lambda l: any((r + (-pad, -pad, pad, pad)).contains(pymupdf.Rect(l.bbox)) or beside(pymupdf.Rect(l.bbox), r)
                               for r in figs.get(l.page, []))
        todo = [f for f in todo if 2 * sum(map(on_fig, f.baseline)) >= len(f.baseline)]
        if not todo:
            continue
        pages = {B.words[k].page for k in range(*u.b_range)} if u.b_range[1] > u.b_range[0] else set()
        near = {p + d for p in pages for d in (-1, 0, 1)}
        work.append((u, todo, [im for im in pics if im.page in near]))
    if not work:
        return
    # read every picture once, in parallel: normal and 2x readings of all of them; the sharper 3x
    # reading (tiny labels) only of the pictures next to a section that is missing text
    say = progress or (lambda m: None)
    close = {(im.page, tuple(im.bbox)) for _, _, ims in work for im in ims}
    ocr.prefetch(B.path, [(im.page, im.bbox) for im in pics], dpi, (1, 2),
                 lambda k, n: say(f"Reading text in pictures (OCR) {k}/{n}"))
    ocr.prefetch(B.path, sorted(close), dpi, (3,), lambda k, n: say(f"Reading small labels in pictures (OCR) {k}/{n}"))
    for u, todo, near_pics in work:
        order = near_pics + [im for im in pics if im not in near_pics]
        for f in todo:
            text = f.detail.get("baseline_text", "")
            hit = next((im for im in order if ocr.found(text, ocr.image_text(B.path, im.page, im.bbox, dpi, (1, 2)))), None) \
                or next((im for im in near_pics if ocr.found(text, ocr.image_text(B.path, im.page, im.bbox, dpi, (1, 2, 3)))), None)
            if hit is None:
                continue
            n = len(_tokens(text))
            f.severity, f.critical, f.types = "info", False, ["text in image"]
            f.message = f"Text drawn in a picture in stage (read by OCR, stage p.{hit.page + 1}): “{snippet_text(text)}”"
            f.candidate, f.candidate_at = [Loc(hit.page, hit.bbox)], None
            f.detail = {**f.detail, "ocr_page": hit.page, "ocr_box": list(hit.bbox)}
            _count_present(u, n)


def _count_present(u: Unit, n: int) -> None:
    """n words reported missing are there after all: count them as matched in the content %."""
    c = u.content
    if not c:
        return
    moved = min(n, c.get("missing_words", 0))
    c["missing_words"] -= moved
    c["matched_words"] += moved
    denom = c["baseline_words"] + c["extra_words"]
    c["match_pct"] = round(100.0 * max(c["matched_words"] - c.get("spacing_issues", 0)
                                       - c.get("script_issues", 0), 0) / denom, 2) if denom else 100.0


def _text_as_graphics(results, A: Doc, B: Doc, cfg: dict) -> None:
    """Missing prod text that stage shows as a graphic instead of live text: figure badges
    ("[Figure (A)]" with a white letter on a drawn circle in prod, an icon picture in stage) or a
    label drawn into a diagram. Each missing prod line is rendered and searched for on the stage
    page around the gap, with stage's own live text blanked out so only pictures and drawings can
    match. When every line is found, the text is there - a presentation (CSS / layout) difference,
    not missing data."""
    ccfg = cfg["content"]
    if not ccfg.get("graphic_text", True):
        return
    max_words, thr = ccfg.get("graphic_text_max_words", 12), ccfg.get("graphic_text_score", 0.72)
    edge_thr = ccfg.get("graphic_text_edge_score", 0.65)
    hays: dict[tuple, "np.ndarray"] = {}
    for u, fs in results:
        for f in [f for f in fs if f.check == "content" and "missing text" in (f.types or [])
                  and f.detail.get("op") == "delete" and f.candidate_at and f.baseline
                  and 0 < f.detail.get("words", 0) <= max_words]:
            at = f.candidate_at
            found = []
            if not any(ch.isalnum() for ch in f.detail.get("baseline_text") or ""):
                continue  # a dash, bullet or ">" alone matches any stroke of a drawing: stays missing text
            for loc in f.baseline:
                ws = [w for w in A.words if w.page == loc.page and _in(w, pymupdf.Rect(loc.bbox))]
                hit = _drawn(A, loc.bbox, loc.page, B, at, thr, edge_thr, hays)
                if hit is None and len(ws) > 1:  # stage may wrap the line: look for each word on its own
                    # a lone glyph ("~", "-") matches anywhere: it is not looked for, only the words around it
                    words = [w for w in ws if (w.bbox[2] - w.bbox[0]) * (w.bbox[3] - w.bbox[1]) >= 20]
                    hit = []
                    for w in words:
                        h = _drawn(A, w.bbox, loc.page, B, at, thr, edge_thr, hays)
                        if h is None:
                            hit = None
                            break
                        hit.append(h)
                    hit = hit or None
                if hit is None:
                    break
                found.extend(hit if isinstance(hit, list) else [hit])
            else:
                text = f.detail.get("baseline_text", "")
                f.check, f.severity, f.critical = "layout", ccfg.get("graphic_text_severity", "warning"), False
                f.types = ["text as graphic"]
                f.message = (f"Text shown as a graphic in stage, not as live text: “{snippet_text(text)}” "
                             f"(prod p.{f.baseline[0].page + 1} ↔ stage p.{at.page + 1})")
                f.candidate, f.candidate_at = [Loc(at.page, r) for r in found], None
                f.detail = {**f.detail, "kind": "text-as-graphic"}
                _count_present(u, len(_tokens(text)))


def _drawn(A: Doc, box, page: int, B: Doc, at: Loc, thr: float, edge_thr: float, hays: dict, lo: float = 0.8):
    """Stage rect where the prod text in `box` appears as a picture / drawing near `at`, or None.
    Normalised cross-correlation over a range of scales (stage pages and fonts are often larger)."""
    import numpy as np
    from PIL import Image as PILImage
    from .checks.assets import _gray, _ncc

    R = 4.0  # px per pt of the renders; each scale is area-averaged down from them
    box = (box[0] - 1, box[1] - 1, box[2] + 1, box[3] + 1)
    tpl = _gray(A, page, box, R)
    if tpl.size == 0 or float(tpl.std()) < 8:
        return None
    tpl = PILImage.fromarray(tpl.astype(np.uint8))
    pg = B.pages[at.page]
    h, w = box[3] - box[1], box[2] - box[0]
    # the gap's own lines first (an inline badge), then the figures around it: the same badge
    # letter often appears in the figure too, and the nearer one is the one that replaced the text
    for reach in (h * 3, 180):
        y0, y1 = max(0.0, at.bbox[1] - reach), min(pg.height, at.bbox[3] + reach)
        key = (at.page, round(y0), round(y1))
        if key not in hays:
            hays[key] = _graphics_only(B, at.page, (0, y0, pg.width, y1), R)
        hay = hays[key]
        best = None
        for s in np.geomspace(lo, 2.0, 12 if lo < 0.8 else 9):  # picture labels (lo=0.5) are often drawn smaller
            k = min(2.5, 16 / (h * s))  # the text ~16 px tall: enough to find it; _glyphs_match looks closer
            tw, th = round(w * s * k), round(h * s * k)
            H = np.asarray(hay.resize((max(1, round(hay.width * k / R)), max(1, round(hay.height * k / R))),
                                      PILImage.BOX), np.float32)
            if tw < 4 or th < 4 or tw >= H.shape[1] or th >= H.shape[0]:
                continue
            ncc = _ncc(H, np.asarray(tpl.resize((tw, th), PILImage.BOX), np.float32))
            if ncc is None:
                continue
            iy, ix = np.unravel_index(int(ncc.argmax()), ncc.shape)
            if best is None or ncc[iy, ix] > best[0]:
                best = (float(ncc[iy, ix]), (ix / k, y0 + iy / k, (ix + tw) / k, y0 + (iy + th) / k))
        if best and best[0] >= thr and (hit := _glyphs_match(A, page, box, B, at.page, best[1], edge_thr)):
            return hit
    return None


def _graphics_only(B: Doc, page: int, rect, R: float):
    """Stage page region at R px/pt with its live text blanked: the content check compares live
    text, so only pictures and drawings may match here."""
    import numpy as np
    from PIL import Image as PILImage
    from .checks.assets import _gray

    x0, y0 = rect[0], rect[1]
    hay = _gray(B, page, rect, R)
    for w in B.words:
        if w.page == page and w.bbox[3] > y0 and w.bbox[1] < rect[3] and w.bbox[2] > x0 and w.bbox[0] < rect[2]:
            hay[max(0, int((w.bbox[1] - y0) * R)):max(0, int((w.bbox[3] - y0) * R) + 1),
                max(0, int((w.bbox[0] - x0) * R)):max(0, int((w.bbox[2] - x0) * R) + 1)] = 255
    return PILImage.fromarray(hay.astype(np.uint8))


def _glyphs_match(A: Doc, a_page: int, box, B: Doc, page: int, near, edge_thr: float, lo: float = 0.8):
    """Second look at a hit, sharper and on the edges only: a badge (A) and a badge (B) correlate
    well as dark circles, but the strokes of the letters differ. Re-matches the text at ~48 px tall
    around the hit over fine scale steps and compares the high-passed pictures at the best spot.
    Returns the stage rect, or None when the glyphs are not the same."""
    import numpy as np
    from PIL import Image as PILImage
    from PIL import ImageFilter
    from .checks.assets import _gray, _ncc

    R = 12.0
    tpl = PILImage.fromarray(_gray(A, a_page, box, R).astype(np.uint8))
    h, w = box[3] - box[1], box[2] - box[0]
    m = max(near[2] - near[0], near[3] - near[1]) * 0.6
    pg = B.pages[page]
    rect = (max(0.0, near[0] - m), max(0.0, near[1] - m), min(pg.width, near[2] + m), min(pg.height, near[3] + m))
    hay = _graphics_only(B, page, rect, R)
    edges = lambda im, r: np.asarray(im, np.float32) - np.asarray(im.filter(ImageFilter.GaussianBlur(r)), np.float32)
    best = None
    for s in np.geomspace(lo, 2.0, 32 if lo < 0.8 else 25):
        k = min(R, 48 / (h * s))
        tw, th = round(w * s * k), round(h * s * k)
        H = hay.resize((max(1, round(hay.width * k / R)), max(1, round(hay.height * k / R))), PILImage.BOX)
        if tw < 4 or th < 4 or tw >= H.width or th >= H.height:
            continue
        T = tpl.resize((tw, th), PILImage.BOX)
        ncc = _ncc(np.asarray(H, np.float32), np.asarray(T, np.float32))
        if ncc is None:
            continue
        iy, ix = np.unravel_index(int(ncc.argmax()), ncc.shape)
        if best is None or ncc[iy, ix] > best[0]:
            r = max(1.0, th / 10)
            pe, te = edges(H.crop((ix, iy, ix + tw, iy + th)), r).ravel(), edges(T, r).ravel()
            pe, te = pe - pe.mean(), te - te.mean()
            e = float((pe * te).sum() / (np.sqrt((pe * pe).sum() * (te * te).sum()) + 1e-9))
            best = (float(ncc[iy, ix]), e, (rect[0] + ix / k, rect[1] + iy / k, rect[0] + (ix + tw) / k, rect[1] + (iy + th) / k))
    return best[2] if best and best[1] >= edge_thr else None


def _vector_pictures(doc: Doc, page: int, min_w: float = 60, min_h: float = 40) -> list:
    """Illustrations drawn as vectors on a page: clusters of drawings with curves (a projector, a
    mountain), not tables, frames or note boxes (straight lines / rectangles, or full of text)."""
    key = (doc.path, page)
    if key not in _VECTOR_PICS:
        pdf = pymupdf.open(doc.path)
        out = []
        try:
            paths = pdf[page].get_drawings()
            for r in pdf[page].cluster_drawings(drawings=paths):
                if r.width < min_w or r.height < min_h:
                    continue
                inside = [d for d in paths if r.contains(d["rect"])]
                curves = sum(1 for d in inside for it in d["items"] if is_curve(it))
                words = sum(1 for w in doc.words if w.page == page and _in(w, r))
                if curves >= 8 and words <= 25:
                    out.append(Image(page, tuple(r)))
        except Exception:
            pass
        _VECTOR_PICS[key] = out
    return _VECTOR_PICS[key]


_VECTOR_PICS: dict[tuple, list] = {}


def _image_labels(results, A: Doc, B: Doc, cfg: dict) -> None:
    """Missing prod text that is a picture's label: drawn on the picture or right beside it (callouts,
    dimension labels - the picture may be an embedded image or drawn as vectors), or a short caption of
    2-12 words directly above or below it. A lone missing word next to a picture is ordinary missing text.
    The stage copy of the picture (the most similar stage picture near the same place) becomes the stage
    side of the finding, so the screenshots show the picture with and without its labels."""
    acfg = cfg["assets"]
    thr, same = acfg.get("visual_match_threshold", 0.25), acfg.get("same_picture_similarity", 0.8)
    rasters = [x for x in A.images if not icon_max(A, x, acfg)]
    stage_pics = [y for y in B.images if not icon_max(B, y, acfg)]
    ccfg = cfg["content"]
    gthr, gedge = ccfg.get("graphic_text_score", 0.72), ccfg.get("graphic_text_edge_score", 0.65)
    hays: dict = {}

    def on(l, im, pad_x, pad_y):
        x0, y0, x1, y1 = l.bbox
        return im.page == l.page and x0 >= im.bbox[0] - pad_x and x1 <= im.bbox[2] + pad_x \
            and y0 >= im.bbox[1] - pad_y and y1 <= im.bbox[3] + pad_y

    def caption(l, im):
        x0, y0, x1, y1 = l.bbox
        return im.page == l.page and x0 < im.bbox[2] and x1 > im.bbox[0] \
            and (0 <= im.bbox[1] - y1 <= 32 or 0 <= y0 - im.bbox[3] <= 32)

    def stage_copy(x: Image, near: Loc | None, u: Unit):
        pages = {near.page + d for d in (-1, 0, 1)} if near else \
            {B.words[k].page for k in range(*u.b_range)} if u.b_range[1] > u.b_range[0] else set()
        best = None
        for y in (y for y in stage_pics if y.page in pages):
            d = visual_distance(A, x, B, y)
            if best is None or d < best[0]:
                best = (d, y)
        if best and (best[0] <= thr or pixel_compare(A, x, B, best[1])[0] >= same):
            return best[1]
        return None

    for u, fs in results:
        for f in fs:
            if f.check != "content" or "missing text" not in (f.types or []) or not f.baseline:
                continue
            n = len(_tokens(f.detail.get("baseline_text")))
            if n < 1:
                continue
            page = f.baseline[0].page
            raster_here, drawn_here = [x for x in rasters if x.page == page], _vector_pictures(A, page)
            if n == 1 and not any(all(on(l, im, 4, 4) for l in f.baseline) for im in raster_here + drawn_here):
                continue  # a lone missing word is a label only when it sits on the picture itself
            short = all(len(t.split()) <= 6 for t in _label_lines(A, f.baseline))
            # on the picture; a drawn illustration's short labels may also sit right beside it (a dimension)
            pic = next((im for im in raster_here + drawn_here if all(on(l, im, 4, 4) for l in f.baseline)), None) or \
                (next((im for im in drawn_here if all(on(l, im, 70, 20) for l in f.baseline)), None) if short else None)
            is_caption = pic is None and n <= 12 and any(caption(l, im) for l in f.baseline for im in raster_here)
            if pic is None and not is_caption:
                continue
            f.types = ["missing image label"] + [t for t in f.types if t != "missing image label"]
            y = stage_copy(pic, f.candidate_at, u) if pic is not None else None
            if y is None:
                f.message = "Image label / caption missing in stage: " + f.message
                # show stage's picture at that spot, even when it is not the same picture: the screenshots
                # then put the two pictures side by side (prod with its labels, stage without)
                near = f.candidate_at
                if near is not None and pic is not None:
                    cands = [y2 for y2 in stage_pics if abs(y2.page - near.page) <= 1]
                    if cands:
                        y2 = min(cands, key=lambda y2: (y2.page != near.page, abs((y2.bbox[1] + y2.bbox[3]) / 2 - near.bbox[1])))
                        f.candidate, f.candidate_at = [Loc(y2.page, y2.bbox)], None
                        f.message += f" (stage picture p.{y2.page + 1} shown; it is not the same picture)"
                        f.detail = {**f.detail, "stage_picture": list(y2.bbox), "stage_picture_page": y2.page}
                continue
            labels = ", ".join(f"“{snippet_text(l)}”" for l in _label_lines(A, f.baseline))
            at = Loc(y.page, y.bbox)
            drawn = [_drawn(A, l.bbox, l.page, B, at, gthr, gedge, hays, lo=0.5) for l in f.baseline]
            f.detail = {**f.detail, "stage_picture": list(y.bbox), "stage_picture_page": y.page}
            if drawn and all(drawn):  # the labels are drawn into the stage picture: present, not live text
                f.check, f.critical, f.types = "layout", False, ["text as graphic"]
                f.message = (f"Picture labels drawn into the stage picture, not live text: {labels} "
                             f"(prod p.{page + 1} ↔ stage p.{y.page + 1})")
                f.candidate, f.candidate_at = [Loc(y.page, r) for r in drawn], None
                _count_present(u, n)
                continue
            # some labels drawn into the stage picture, some not: name only the missing ones
            names = lambda ok: ", ".join(dict.fromkeys(f"“{snippet_text(t)}”" for t in
                                                       _label_lines(A, [l for l, d in zip(f.baseline, drawn) if bool(d) == ok])))
            missing, present = names(False), names(True)
            f.message = (f"Image labels missing in stage: {missing or labels} on the prod picture (p.{page + 1}) "
                         f"are not on the stage picture (p.{y.page + 1})"
                         + (f"; drawn into the stage picture: {present}" if present else ""))
            f.candidate, f.candidate_at = [at], None
            f.detail["labels_missing"], f.detail["labels_in_picture"] = missing, present


def _label_wording(results, A: Doc, B: Doc, cfg: dict) -> None:
    """One plain wording for every finding about a prod picture's labels, whichever step found it
    (drawn into the stage picture, read there by OCR, missing): the prod labels highlighted on the
    prod side; on the stage side the same labels where stage has them (the label itself, located
    in the stage picture by OCR when it is drawn into the image), else the stage picture without
    them. Expected / Actual say exactly that. A label found in stage is never also called missing."""
    figs = None
    for _, fs in results:
        for f in fs:
            types = set(f.types or [])
            if not types & {"text as graphic", "text in image", "missing image label"} or not f.baseline:
                continue
            if types == {"text as graphic"} and "stage_picture" not in f.detail:
                # text drawn as a graphic: a picture's label only when it sits on / beside a prod picture
                # (a badge "(A)" drawn as an icon inside a sentence stays "text as graphic")
                figs = figs if figs is not None else _figures(A, cfg)
                pad = 40
                if not all(any((r + (-pad, -pad, pad, pad)).contains(pymupdf.Rect(l.bbox)) for r in figs.get(l.page, []))
                           for l in f.baseline):
                    continue
            labels = list(dict.fromkeys(_label_lines(A, f.baseline))) or [snippet_text(f.detail.get("baseline_text", ""))]
            pa = f.baseline[0].page + 1
            other_pic = "it is not the same picture" in f.message  # the nearest stage picture, shown for comparison
            note = " (the stage picture shown is the nearest one; it is not the same picture)" if other_pic else ""
            pic_page = f.detail.get("stage_picture_page", f.detail.get("ocr_page"))
            pic_box = f.detail.get("stage_picture") or f.detail.get("ocr_box")
            labels = [t for t in (re.sub(r"[\ufffc\ufffd]", "", t).strip() for t in labels) if t]  # object placeholders
            q = lambda ls: ", ".join(f"“{t}”" for t in ls)
            if "missing image label" in types and pic_box is not None:
                # re-check each label in the stage picture: by OCR, where it is drawn into the image
                found, boxes = [], []
                # the stage picture chosen first, then every other stage picture on the pages around it
                # (the labels may be on another picture: one booklet picture per label, "Quick Start Guide")
                pics = [(pic_page, tuple(pic_box))] + [(y.page, tuple(y.bbox)) for y in B.images
                                                        if abs(y.page - pic_page) <= 1 and tuple(y.bbox) != tuple(pic_box)
                                                        and y.bbox[2] - y.bbox[0] >= 30 and y.bbox[3] - y.bbox[1] >= 20]
                for t in labels:
                    for pg, box in (pics if ocr.available() else []):
                        hit = ocr.locate(t, B.path, pg, box)
                        if hit:
                            found.append(t)
                            boxes += [(pg, h) for h in hit]
                            break
                missing = [t for t in labels if t not in found]
                if not missing:  # every label is on a stage picture after all
                    other_pic = False
                    f.types = ["label in picture"]
                    types = {"label in picture"}
                    f.candidate = [Loc(pg, h) for pg, h in boxes]
                else:
                    pb = pic_page + 1
                    f.message = (f"Image label missing in stage: {q(missing)} on the prod picture (p.{pa}) "
                                 f"is not on the stage picture (p.{pb})"
                                 + (f"; {q(found)} {'is' if len(found) == 1 else 'are'} there, drawn into the picture" if found else "") + note)
                    f.candidate = [Loc(pic_page, tuple(pic_box))] + [Loc(pg, h) for pg, h in boxes]
                    f.detail = {**f.detail, "labels_missing": q(missing), "labels_in_picture": q(found),
                                "expected": f"{q(labels)} on the picture (prod p.{pa})",
                                "actual": (f"The stage picture (p.{pb}) without {q(missing)}"
                                           + (f"; {q(found)} drawn into the picture" if found else ""))}
                    continue
            if "missing image label" in types:  # no stage copy of the picture known
                f.message = f"Image label missing in stage: {q(labels)} on the prod picture (p.{pa}); no stage picture shows it"
                f.detail = {**f.detail, "expected": f"{q(labels)} on the picture (prod p.{pa})",
                            "actual": f"Not in stage: no stage picture shows {q(labels)}"}
                continue
            # present in stage, drawn into the picture: one type, however it was found (drawn at its
            # spot, read by OCR) - the label is there, so an info note, not an issue
            f.severity, f.critical, f.types = "info", False, ["label in picture"]
            if "text in image" in types and f.candidate and pic_box is not None and len(f.candidate) == 1 \
                    and list(f.candidate[0].bbox) == list(pic_box):
                boxes = [b for t in labels for b in ocr.locate(t, B.path, pic_page, tuple(pic_box))]
                if boxes:
                    f.candidate = [Loc(pic_page, b) for b in boxes]
            pb = f.candidate[0].page + 1 if f.candidate else (pic_page + 1 if pic_page is not None else None)
            where_b = f" (stage p.{pb})" if pb else ""
            f.message = (f"Image label on the stage picture: {q(labels)} - there, drawn into the image "
                         f"(prod p.{pa} ↔ stage p.{pb})")
            f.detail = {**f.detail, "expected": f"{q(labels)} on the picture, as live text (prod p.{pa})",
                        "actual": f"{q(labels)} on the stage picture, drawn into the image{where_b}"}


def _text_in_matched_artwork(results) -> None:
    """Prod text that is part of a prod drawing which stage shows as a picture of the same artwork
    (an OSD menu screenshot drawn as vectors with live text in prod, an embedded image in stage): the
    words are in the stage picture, not missing. The picture check pairs the two (raster-vs-vector);
    missing text inside that prod drawing becomes "label in picture" on the stage picture."""
    art = []  # (prod page, prod rect, stage loc) of every drawing <-> picture pair
    for _, fs in results:
        for f in fs:
            if f.check == "assets" and (f.detail or {}).get("kind") == "raster-vs-vector" and f.baseline and f.candidate:
                art.append((f.baseline[0].page, pymupdf.Rect(f.baseline[0].bbox), f.candidate[0]))
    if not art:
        return
    for u, fs in results:
        for f in fs:
            if f.check != "content" or "missing text" not in (f.types or []) or not f.baseline:
                continue
            home = next((st for pg, r, st in art if all(l.page == pg and (r + (-4, -4, 4, 4)).contains(pymupdf.Rect(l.bbox))
                                                          for l in f.baseline)), None)
            if home is None:
                continue
            text = snippet_text(f.detail.get("baseline_text", ""))
            f.severity, f.critical, f.types = "info", False, ["label in picture"]
            f.message = (f"Text of the prod drawing is in the stage picture: “{text}” (prod draws it as vector art "
                         f"with live text, stage shows the same artwork as an image, p.{home.page + 1})")
            f.candidate, f.candidate_at = [home], None
            _count_present(u, len(_tokens(f.detail.get("baseline_text"))))


def _label_lines(A: Doc, locs_: list) -> list[str]:
    """The text of each boxed label line: “3000 m (10000 feet)”, “0 m (0 feet)”."""
    out = []
    for l in locs_:
        ws = [w.text for w in A.words if w.page == l.page and _in(w, pymupdf.Rect(l.bbox))]
        if ws:
            out.append(" ".join(ws))
    # a label wrapped over two lines ("3000 m" / "(10000 feet)"): join a line that starts with "("
    joined = []
    for t in out:
        if joined and t.startswith("("):
            joined[-1] += " " + t
        else:
            joined.append(t)
    return joined


def _of(results, u: Unit) -> list[Finding]:
    return next(fs for x, fs in results if x is u)


def _bag(text: str) -> Counter:
    return Counter(t.lower() for t in _TOKEN.findall(text or ""))


def _content(results, min_words: int, overlap: float) -> None:
    """A block of prod text missing from its section that appears as extra text in another section."""
    extras = [(u, f, _bag(f.detail.get("candidate_text"))) for u, fs in results for f in fs
              if f.check == "content" and f.detail.get("op") in ("insert", "replace")]
    for u, fs in results:
        for f in [f for f in fs if f.check == "content" and f.detail.get("op") in ("delete", "replace")]:
            bag = _bag(f.detail.get("baseline_text"))
            n = sum(bag.values())
            if n < min_words:
                continue
            best = None
            for eu, ef, ebag in extras:
                if eu is u:
                    continue
                score = sum((bag & ebag).values()) / n
                if score >= overlap and (best is None or score > best[0]):
                    best = (score, eu, ef, ebag)
            if not best:
                continue
            _, eu, ef, ebag = best
            extras.remove((eu, ef, ebag))
            if sum((ebag - bag).values()) < min_words:  # the extra block is just this text
                _of(results, eu).remove(ef)
            else:
                ef.detail["moved_in"] = True  # the rest of it is extra; this part is not a duplicate
            f.message = (f"Content placed in the wrong section: “{snippet_text(f.detail.get('baseline_text'))}” "
                         f"is in “{u.title}” in prod but in “{eu.title}” in stage")
            f.candidate, f.candidate_at = ef.candidate, None
            f.severity, f.critical, f.types = "error", True, ["content in wrong section"]
            f.detail = {**f.detail, "op": "wrong-section", "candidate_section": eu.title}


def snippet_text(t: str | None, n: int = 0) -> str:
    """The text in full (n is ignored: an issue is never shortened with "…")."""
    return " ".join((t or "").split())


def _target_word(doc: Doc, ln: dict) -> int | None:
    page = ln.get("page", -1)
    if ln.get("kind") not in _GOTO or not 0 <= page < len(doc.pages):
        return None
    y = ln["to"].y if ln.get("to") is not None else 0.0
    return Aligner.word_at(doc, (0, len(doc.words)), page, y - 2)


def _link_at(doc: Doc, w) -> dict | None:
    cx, cy = (w.bbox[0] + w.bbox[2]) / 2, (w.bbox[1] + w.bbox[3]) / 2
    for ln in page_links(doc, w.page):
        r = ln["from"]
        if r.x0 - 1 <= cx <= r.x1 + 1 and r.y0 - 1 <= cy <= r.y1 + 1:
            return ln
    return None


def _links(u: Unit, units: list[Unit], mode: str) -> list[Finding]:
    """Links present on both sides whose target differs: an internal link that jumps to a
    different section in stage than in prod, or (PDF vs PDF) a web link to another address."""
    al = Aligner(u)
    A, B = u.a, u.b
    out, seen = [], set()
    for i in range(*u.a_range):
        j = al.a2b.get(i)
        if j is None and _link_at(A, A.words[i]) is not None:
            # the link's words differ (quotes, "page 12" vs "page 14"): the stage link between the paired
            # words around it
            prv = next((al.a2b[k] for k in range(i - 1, max(u.a_range[0], i - 12) - 1, -1) if k in al.a2b), None)
            nxt = next((al.a2b[k] for k in range(i + 1, min(u.a_range[1], i + 25)) if k in al.a2b), None)
            if prv is not None and nxt is not None and 0 < nxt - prv <= 30:
                j = next((k for k in range(prv + 1, nxt) if _link_at(B, B.words[k]) is not None), None)
        if j is None:
            continue
        la = _link_at(A, A.words[i])
        if la is None or (A.words[i].page, tuple(la["from"])) in seen:
            continue
        lb = _link_at(B, B.words[j])
        if lb is None:
            continue  # plain text in stage: reported as a missing link
        seen.add((A.words[i].page, tuple(la["from"])))
        idx_a = [k for k in range(*u.a_range) if _in(A.words[k], la["from"]) and A.words[k].page == A.words[i].page]
        idx_b = [al.a2b[k] for k in idx_a if k in al.a2b]
        text = snippet(A, idx_a, 10) if idx_a else A.words[i].text
        # the link's title in quotes (see “Connecting the display”): quotes missing / added in stage
        qa, qb = _quoted(A, idx_a or [i]), _quoted(B, sorted(idx_b) or [j])
        if qa != qb:
            ta_, tb_ = _link_text(A, idx_a or [i]), _link_text(B, sorted(idx_b) or [j])
            out.append(Finding(
                "integrity", "error",
                (f"Link quotation marks missing in stage: “{ta_}” in prod → “{tb_}” in stage" if qa else
                 f"Link quotation marks added in stage: “{ta_}” in prod → “{tb_}” in stage"),
                locs(A, idx_a or [i]), locs(B, sorted(idx_b) or [j]),
                {"kind": "link-quotes", "baseline": ta_, "candidate": tb_},
                types=["link quotes"], links=[(locs(A, idx_a or [i])[0], locs(B, sorted(idx_b) or [j])[0])]))
        ta, tb = _target_word(A, la), _target_word(B, lb)
        if ta is not None and tb is not None:
            ua, ub = _unit_of(units, "a", ta), _unit_of(units, "b", tb)
            if ua is not None and ub is not None and ua is not ub and not _names_target(text, ub.title, ua.title):
                out.append(Finding(
                    "integrity", "error",
                    f"Link points to the wrong section: “{text}” goes to “{ua.title}” in prod "
                    f"but to “{ub.title}” in stage (stage p.{lb['page'] + 1})",
                    locs(A, idx_a or [i]), locs(B, idx_b or [j]),
                    {"kind": "wrong-link-target", "baseline_target": ua.title, "candidate_target": ub.title},
                    critical=True, types=["link to wrong section"]))
            # same section (else reported above), but does it land on the same place? the heading each lands on
            da, db = _landing(A, la), _landing(B, lb)
            if da and db and _title_ratio(da, db) < 0.8 and not _names_target(text, db, da) \
                    and not (ua is not None and ub is not None and ua is not ub):
                out.append(Finding(
                    "integrity", "error",
                    f"Link lands on a different place: “{text}” goes to “{da}” in prod (p.{la['page'] + 1}) "
                    f"but to “{db}” in stage (p.{lb['page'] + 1})",
                    locs(A, idx_a or [i]), locs(B, idx_b or [j]),
                    {"kind": "wrong-link-spot", "baseline_target": da, "candidate_target": db},
                    critical=True, types=["link lands elsewhere"]))
        elif mode == "pdf" and la.get("kind") == pymupdf.LINK_URI and lb.get("kind") == pymupdf.LINK_URI:
            ua_, ub_ = (la.get("uri") or "").rstrip("/"), (lb.get("uri") or "").rstrip("/")
            if ua_ and ub_ and ua_ != ub_:
                out.append(Finding(
                    "integrity", "error",
                    f"Link goes to a different address: “{text}” → {ua_} in prod but {ub_} in stage",
                    locs(A, idx_a or [i]), locs(B, idx_b or [j]),
                    {"kind": "link-target-differs", "baseline_uri": ua_, "candidate_uri": ub_},
                    types=["link target differs"]))
    out += _page_numbers(u, B)
    return out


_QUOTES = "\"“”„«»‘’'"


def _link_text(doc: Doc, idx: list[int]) -> str:
    """The link's words with the words right before / after it (a quote mark may be its own word)."""
    lo, hi = max(0, min(idx) - 1), min(len(doc.words) - 1, max(idx) + 1)
    ks = [k for k in range(lo, hi + 1) if doc.words[k].page == doc.words[idx[0]].page]
    return " ".join(doc.words[k].text for k in ks)


def _quoted(doc: Doc, idx: list[int]) -> bool:
    """The link text holds a quoted title (“Connecting the display” on page 12): a word opening with a
    quotation mark and a later (or the same) word closing with one, in the link or right beside it."""
    lo, hi = max(0, min(idx) - 1), min(len(doc.words) - 1, max(idx) + 1)
    ws = [doc.words[k].text for k in range(lo, hi + 1)]
    opens = [n for n, t in enumerate(ws) if t[:1] in _QUOTES]
    return any(t.rstrip(".,;:)")[-1:] in _QUOTES and (n > o or len(t) > 1) for o in opens
               for n, t in enumerate(ws) if n >= o)


def _landing(doc: Doc, ln: dict) -> str:
    """The text a page link lands on: the first line at / below its destination point."""
    from .checks.integrity import _destination
    try:
        return _destination(doc, ln)
    except Exception:
        return ""


def _title_ratio(a: str, b: str) -> float:
    """How alike two landing headings are; 1.0 when one starts the other (a heading wrapped over two
    lines is read as its first line: “Playing multimedia files from a” / “… from a USB flash drive”)."""
    from difflib import SequenceMatcher
    from .normalize import title
    ta, tb = title(a), title(b)
    short, long_ = sorted((ta, tb), key=len)
    if len(short) >= 8 and long_.startswith(short):
        return 1.0
    return SequenceMatcher(None, ta, tb).ratio()


_PRINTED: dict[tuple, int | None] = {}


def _printed_page(doc: Doc, page: int) -> int | None:
    """The page number printed in the page's footer / header (not its index in the file)."""
    key = (doc.path, page)
    if key not in _PRINTED:
        pg = pymupdf.open(doc.path)[page]
        h = pg.rect.height
        nums = [w[4] for w in pg.get_text("words") if (w[1] > 0.9 * h or w[3] < 0.1 * h) and w[4].isdigit()]
        _PRINTED[key] = int(nums[0]) if len(set(nums)) == 1 else None
    return _PRINTED[key]


def _page_numbers(u: Unit, B: Doc) -> list[Finding]:
    """A stage link whose text names a page ("… on page 19") must land on the page printed with that
    number. The number may differ from prod's (layouts differ) - it must be right in stage itself."""
    out, seen = [], set()
    for j in range(*u.b_range):
        w = B.words[j]
        ln = _link_at(B, w)
        if ln is None or (w.page, tuple(ln["from"])) in seen or ln.get("kind") not in (pymupdf.LINK_GOTO, pymupdf.LINK_NAMED):
            continue
        seen.add((w.page, tuple(ln["from"])))
        idx = [k for k in range(max(u.b_range[0], j - 3), min(u.b_range[1], j + 40))
               if B.words[k].page == w.page and _in(B.words[k], ln["from"])]
        text = snippet(B, idx) if idx else w.text
        m = re.search(r"\bpage\s+(\d+)\b", text, re.I)
        if not m or not 0 <= ln.get("page", -1) < len(B.pages):
            continue
        said, printed = int(m.group(1)), _printed_page(B, ln["page"])
        if printed is not None and printed != said:
            out.append(Finding(
                "integrity", "error",
                f"Page number in the link text is wrong: “{text}” goes to the page numbered {printed} "
                f"(stage p.{ln['page'] + 1}), not page {said}",
                [], locs(B, idx or [j]), {"kind": "link-page-number", "says": said, "lands_on": printed},
                critical=False, types=["link page number"]))
    return out


def _names_target(text: str, stage_title: str, prod_title: str) -> bool:
    """The link text names stage's target and not prod's ("Front panel." -> section "Front panel" in
    stage, its parent "Components" in prod): stage is right, not a wrong link."""
    from difflib import SequenceMatcher
    from .normalize import title
    t = title(re.sub(r"\s*on page \d+\s*$", "", text or "", flags=re.I)).strip("“”\"' .")
    score = lambda h: max(SequenceMatcher(None, t, title(h)).ratio(), 1.0 if title(h) and title(h) in t else 0.0)
    return bool(t) and score(stage_title) >= 0.85 and score(prod_title) < 0.85


def _in(w, r) -> bool:
    cx, cy = (w.bbox[0] + w.bbox[2]) / 2, (w.bbox[1] + w.bbox[3]) / 2
    return r.x0 - 1 <= cx <= r.x1 + 1 and r.y0 - 1 <= cy <= r.y1 + 1


# ---------------------------------------------------------------- tagging

# type -> (short name, why it matters)
_WHY = {
    "missing section": ("Section missing", "A whole section of the prod document is not in stage."),
    "duplicate section": ("Section duplicated", "The same section appears more often in stage than in prod."),
    "order differs": ("Section in the wrong place", "The section sits in a different position in stage."),
    "missing image": ("Image missing", "A picture from prod is not shown in stage."),
    "broken image": ("Image / icon broken", "The picture is in stage but does not display."),
    "image in wrong section": ("Image in wrong section", "The picture is in stage, but under a different section."),
    "content in wrong section": ("Content in wrong section", "The text is in stage, but under a different section."),
    "duplicate content": ("Content duplicated", "Stage repeats prod text in a place where prod does not have it."),
    "duplicate image": ("Image duplicated", "A prod picture appears a second time in stage, in a section that should not have it."),
    "image blacked out": ("Image blacked out", "Part of the picture is black in stage where prod shows content."),
    "image changed": ("Different image", "Stage shows another picture than prod at this spot."),
    "missing image label": ("Image label / caption missing", "The text that labels or captions a picture in prod is not in stage."),
    "missing text": ("Data missing", "Text from prod is not in stage."),
    "extra text": ("Extra content", "Stage has text that is not in prod."),
    "changed text": ("Text changed", "The words differ between prod and stage."),
    "case": ("Uppercase / lowercase differs", "The same words are written with different capital letters."),
    "punctuation": ("Punctuation differs", "A punctuation mark (. , : ; quotes, dashes, apostrophes) differs."),
    "case + punctuation": ("Case and punctuation differ", "Capital letters and punctuation marks differ."),
    "spacing": ("Space after a word differs", "A space is missing, extra or doubled between two words."),
    "superscript": ("Superscript / subscript differs", "Text is raised or lowered (e.g. “Cr⁺⁶”, “H₂O”, a footnote mark) on one side only."),
    "paragraph break": ("Paragraph break differs", "Text is split into paragraphs differently."),
    "marker glued": ("No space after the list number / bullet",
                     "The list number or bullet touches its text in stage (“16.RS-232”), where prod has a space."),
    "bullet marker": ("Bullet / list number missing or different",
                      "A bullet point or list number (1. 2. 3., a. b. c.) is missing, added or different in stage."),
    "numbering style": ("List numbering differs", "The list is numbered differently (for example a, b, c in prod but i, ii, iii or 1, 2, 3 in stage)."),
    "numbering format": ("List number format differs", "The punctuation around the list numbers differs (for example a. in prod, a) in stage)."),
    "numbering sequence": ("List numbering out of sequence", "A list number repeats or skips (for example i, ii, ii) in stage but not in prod."),
    "image outside box": ("Image outside its note / box", "In prod the picture is inside a note or box together with its text; in stage it is not (or the reverse)."),
    "bullet": ("Bullet alignment differs", "The bullet or its text sits at a different position relative to the text around it."),
    "missing row": ("Table row missing", "A row of a prod table is not in stage."),
    "extra section": ("Extra section", "Stage has a section that prod does not have."),
    "outline level": ("Heading level differs", "The heading sits at a different level of the outline in stage (e.g. H3 → H2)."),
    "missing entry": ("TOC entry missing", "A chapter listed in the prod table of contents is not listed in stage."),
    "extra entry": ("Extra TOC entry", "The stage table of contents lists a chapter that prod does not."),
    "title differs": ("TOC title differs", "A chapter is listed with different words in the stage table of contents."),
    "wrong page": ("TOC page number wrong", "The stage table of contents points to a page where the chapter does not start."),
    "level differs": ("TOC level differs", "A chapter is listed at a different level in the stage table of contents."),
    "heading differs": ("TOC heading differs", "The heading of the table of contents differs."),
    "extra image": ("Extra image", "Stage shows a picture that prod does not have here."),
    "image distorted": ("Image distorted", "The picture is stretched or squashed in stage."),
    "extra table": ("Extra table", "Stage has a table that prod does not have."),
    "table border": ("Table border differs", "A border of the prod table (outline, row or column lines) is missing in stage, or drawn in another colour."),
    "text outside table border": ("Text outside table border", "Text in a stage table runs across its cell border (into the next cell, past the table edge or over a row line); in prod it fits inside the cell."),
    "extra row": ("Extra table row", "A stage table has a row that the prod table does not have."),
    "row split": ("Table row split", "One prod table row is broken into several rows in stage."),
    "cells split": ("Table cells split", "Rows have more cells in stage than in prod."),
    "table to text": ("Table turned into text", "A prod table is plain text in stage."),
    "text to table": ("Text turned into a table", "Plain prod text is a table in stage."),
    "missing table": ("Table missing", "A whole prod table is not in stage."),
    "missing header": ("Table header missing", "The table in stage has no header row, so its columns are unlabelled."),
    "tables merged": ("Tables merged", "Separate prod tables are one table in stage."),
    "rows merged": ("Table rows merged", "Separate prod rows are one row in stage."),
    "table split": ("Table split", "One prod table is broken into several tables in stage."),
    "cells merged": ("Table cells merged", "Rows have fewer cells in stage than in prod."),
    "broken link": ("Link broken", "The link in stage goes nowhere."),
    "missing link": ("Link not working", "The text is a link in prod but plain text in stage."),
    "link to wrong section": ("Link to wrong section", "The link jumps to a different section in stage."),
    "link target differs": ("Link to a different address", "The web link goes to a different address in stage."),
    "missing file": ("Attachment missing", "A file embedded in prod is not in stage."),
    "broken glyph": ("Broken characters", "Characters in stage do not render (missing font or bad encoding)."),
    "text off page": ("Text cut off", "Text in stage is drawn outside the page."),
    "moved text": ("Text on another line / in another order", "The same words are in stage, on another line or in another order."),
    "reordered": ("Text in another order", "The same words are in stage, in another order."),
    "label only": ("Callout label on one side only", "A note / caution label (e.g. “NOTE:”) is printed on one side only."),
    "repeated header": ("Repeated table header", "A table header is repeated at a page break on one side only."),
    "continued header": ("“(continued)” header", "A “(continued)” heading is repeated at a page break on one side only."),
    "raster vs vector": ("Artwork as picture vs drawing", "The same artwork is an embedded picture on one side and drawn as vectors on the other."),
    "image combined": ("Pictures combined", "A prod picture is shown as part of one larger picture in stage."),
    "text in image": ("Text inside a picture", "The prod text is drawn inside a stage picture (read by OCR), not as live text."),
    "table border added": ("Table border added", "A stage table has a border or rule that the prod table does not."),
    "image blurred": ("Image blurred", "The picture is noticeably softer / less sharp in stage than in prod."),
    "image order": ("Image sequence differs", "The pictures appear in a different order in stage than in prod."),
    "footer": ("Footer differs", "The page footer (page number, its place and style, or the text beside it such as a chapter "
                                 "name) is not the same in stage as in prod."),
    "header": ("Header differs", "The running header at the top of the pages is not the same in stage as in prod."),
    "bracket spacing": ("Number inside ( ) spaced differently",
                        "The number or icon between round brackets - a step number such as (❶) - is not centred, sits with "
                        "a different space to the brackets, or is missing in stage."),
    "image alignment": ("Image alignment differs", "The picture sits differently in the text column (left / centred / right / full width) in stage than in prod."),
    "link quotes": ("Link quotation marks", "The link title's quotation marks (“…”) are missing or added in stage."),
    "link lands elsewhere": ("Link lands on a different place", "The link jumps to another heading in stage than in prod."),
    "link page number": ("Page number in link text is wrong", "The link says “on page N” but jumps to a page with another number."),
    "extra link": ("Extra link in stage", "The text is a link in stage but plain text in prod."),
    "image pixelated": ("Image pixelated", "Stage shows the picture at a much lower resolution than prod: it looks blocky or blurred."),
    "row order": ("Table rows in another order", "The rows of the table come in a different order in stage."),
    "placement": ("Image alignment differs", "The picture sits elsewhere in stage: inline in the sentence vs on its own line, or after other text."),
    "spec font-size": ("Font size off the design spec", "Stage text is not in the font size the design spec sets for it."),
    "spec color": ("Text colour off the design spec", "Stage text is not in the colour the design spec sets for it."),
    "spec line-height": ("Line height off the design spec", "The lines of a stage paragraph are not spaced as the design spec sets."),
    "spec font-family": ("Font off the design spec", "Stage text is not in the font the design spec sets for it."),
    "spec font-weight": ("Font weight off the design spec", "Stage text is bolder or lighter than the design spec sets."),
    "spec page margin": ("Text outside the page margins", "Stage text runs into the page margin the design spec reserves."),
    "spec page size": ("Page size off the design spec", "The stage page size matches no format of the design spec."),
    "spec text-decoration": ("Link not underlined", "A stage link is not underlined as the design spec sets for links."),
    "spec theme": ("Cover colour matches no theme", "The cover's colour is none of the design spec's themes (BenQ, EDU, ZOWIE, INFTY)."),
    "spec cover": ("Cover off the design spec", "The cover page lacks an element the design spec's cover has (version, title)."),
    "spec cover logo": ("Cover logo missing", "The cover lacks a logo the theme's cover carries."),
    "spec page number": ("Page number off the design spec", "A page number is missing, out of sequence or not centred."),
    "spec heading level": ("Heading level skipped", "A heading skips a level (H1 → H3): the design spec keeps H1 → H2 → H3."),
    "spec callout title": ("Callout title off the design spec", "A callout's title is not the fixed title of its type (IMPORTANT, NOTE, TIP, WARNING)."),
    "spec callout background": ("Callout colour off the design spec", "A callout is not in its type's background colour, or is plain text instead of a callout."),
    "spec callout icon": ("Callout icon off the design spec", "A callout has no icon, another type's icon, or an icon of another size."),
    "spec callout content": ("Callout content not allowed", "A callout holds a picture or a second callout: it takes paragraphs only."),
    "spec table header": ("Table header bar colour off the design spec", "The table's header bar is not in the design spec's colour."),
    "spec table border": ("Table border colour off the design spec", "The table's border is not in the design spec's colour."),
    "spec list numbering": ("List numbering off the design spec", "An ordered list level is not numbered 1, 2, 3 → a, b, c → I, II, III."),
    "spec bullet": ("Bullet off the design spec", "An unordered list does not use the black circle bullet."),
    "spec pagination": ("Page break off the design spec", "A heading at a page bottom, a callout split over pages, or a table header alone / not repeated."),
    "spec page structure": ("Print page structure off the design spec", "The Print version has a cover, Q&A index or TOC, or the first page header is missing or repeated."),
    "row background": ("Table row background differs", "Rows that one side shades (group rows between the data rows) are plain on the other."),
    "spec text-align": ("Text not left-aligned", "Body text is centred or right-aligned; the design spec left-aligns all content."),
    "size / aspect": ("Image size differs", "The picture is shown at another width or aspect ratio in stage."),
    "emphasis": ("Bold / italic differs", "The same words are bold (or italic) on one side and plain on the other: the emphasis the reader relies on changed."),
    "list level": ("List level differs", "A paragraph that sits under a list item's text in prod (part of that bullet or numbered item) starts under another item's text, or as body text, in stage: the list's marker / text columns are not kept."),
    "row alignment": ("Items not aligned in a row", "Text that sits side by side on one row in prod (list items in two columns, captions under a row of pictures) is at different heights in stage."),
    "label in picture": ("Image label on the stage picture", "The picture's label is there in stage, drawn into the image instead of as live text."),
    "text as graphic": ("Text shown as a graphic", "Stage shows this text as a picture or drawing, not as live text (not searchable, not translatable)."),
    "bookmark only": ("Heading only in the bookmarks", "The heading is in the PDF bookmarks but not found as text on the page."),
}


def tag(f: dict, genuine_types: set[str], data_min_words: int = 3, exclude_checks: set[str] = frozenset(),
        everything: bool = False) -> None:
    """Set f['genuine'] and, for genuine findings, f['issue'] (short name), f['description'] (what
    exactly is wrong and where) and f['why'] (why it matters). Findings of `exclude_checks`
    (e.g. "toc") are never genuine; they stay in the full report."""
    types = [] if f.get("check") in exclude_checks else (f.get("types") or [])
    gt = [t for t in types if t in genuine_types]
    if everything and f.get("check") not in exclude_checks:
        gt = gt or types or [f.get("check", "issue")]  # every finding, named by its first type
    # text is data missing when its words are really absent from stage, not one changed or moved word
    if not everything and gt == ["missing text"] and f["check"] == "content" and not f.get("critical") \
            and f["detail"].get("absent_words", 0) < data_min_words:
        gt = []
    f["genuine"] = bool(gt)
    if c := color_of(f):
        f["color"] = c
    if gt:
        # bullets: a missing / added / changed bullet point is named as such, the rest is alignment
        kind = f["detail"].get("kind")
        key = kind if gt[0] == "bullet" and kind in _WHY else gt[0]
        name, why = _WHY.get(key, (gt[0].capitalize(), ""))
        merged = f["detail"].get("merged_types")
        if merged:  # several differences at one spot: "Punctuation differs + Bullet / list number missing"
            names = list(dict.fromkeys(_WHY.get(t, (t.capitalize(), ""))[0] for t in merged))
            name, why = " + ".join(names), " ".join(dict.fromkeys(_WHY.get(t, ("", ""))[1] for t in merged)).strip()
        f["issue"], f["why"] = name, why
        f["description"] = f["message"]


# issue colour (UI, PDF reports, screenshot boxes): red = what the reader sees is wrong or missing,
# blue = how it is laid out. Everything else keeps its category colour.
RED, BLUE = "#dc2626", "#2563eb"
_RED_TYPES = {"missing image", "broken image", "image changed", "image blacked out", "missing image label",
              "size / aspect", "image distorted", "placement", "image outside box",   # image size / alignment
              "image pixelated", "row order", "extra link", "missing link", "link to wrong section",
              "image blurred", "image alignment", "link lands elsewhere", "link page number", "link quotes",
              "spec font-size", "font-size",                                            # text size
              "bullet marker", "numbering style", "numbering format", "numbering sequence",  # bullet, (-)
              "marker glued", "bracket spacing"}
_BLUE_KINDS = {"bullet indent", "bullet gap", "hanging indent"}


def color_of(f: dict) -> str | None:
    """RED for content, links, image size / alignment / label, a missing image or icon, text size and
    bullet or dash differences; BLUE for indent, CSS and layout; None: the category colour."""
    check, types, kind = f.get("check"), set(f.get("types") or []), (f.get("detail") or {}).get("kind")
    if kind == "marks":
        return BLUE  # red highlight marks on a picture: red boxes around them would hide what the issue is about
    if check in ("content", "integrity") or types & _RED_TYPES or kind in _RED_TYPES:
        return RED
    if check == "assets" and kind in ("missing", "broken", "changed", "blackout", "distorted"):
        return RED
    if check in ("style", "layout") or kind in _BLUE_KINDS or "indent" in types:
        return BLUE
    return None


def _q(t: str) -> str:
    return f"“{' '.join((t or '').split())}”"


def _picture(im: dict | None) -> str:
    """“102.5 × 70.8 mm (600 × 400 px), 94% of the text width, centred”."""
    if not im:
        return ""
    parts = []
    if im.get("width_mm") and im.get("height_mm"):
        parts.append(f"{im['width_mm']:g} × {im['height_mm']:g} mm")
    if im.get("pixels"):
        px = im["pixels"]
        parts[-1:] = [(parts[-1] + " " if parts else "") + (f"({px[0]} × {px[1]} px)" if isinstance(px, (list, tuple)) else f"({px})")]
    if im.get("text_width_share") is not None:
        parts.append(f"{im['text_width_share']:.0%} of the text width")
    if im.get("align"):
        parts.append(im["align"])
    return ", ".join(parts)


def expected_actual(f: dict) -> tuple[str, str]:
    """What prod shows (expected) and what stage shows (actual), in plain words, for the report."""
    d, m = f.get("detail") or {}, f.get("message") or ""
    if d.get("expected") and d.get("actual"):  # set by the step that knows best (image labels)
        return d["expected"], d["actual"]
    kind, types, check = d.get("kind"), set(f.get("types") or []), f.get("check")
    pa, pb = where(f)
    at_a, at_b = f" (prod {pa})" if pa else "", f" (stage {pb})" if pb else ""
    img_a, img_b = _picture(d.get("baseline_image")), _picture(d.get("candidate_image"))
    with_a, with_b = (f": {img_a}" if img_a else ""), (f": {img_b}" if img_b else "")
    # specific kinds first
    if kind == "blurred" and d.get("baseline_sharpness"):
        b, c = d["baseline_sharpness"], d.get("candidate_sharpness") or 0
        return (f"Sharp picture, edge sharpness {b}{at_a}{with_a}",
                f"Blurred picture, edge sharpness {c} ({c / b:.0%} of prod){at_b}{with_b}")
    if "image alignment" in types:
        al = re.search(r"alignment (.+?) → (.+?)(?: —|$)", m)
        if al:
            return f"Picture {al.group(1)}{at_a}{with_a}", f"Picture {al.group(2)}{at_b}{with_b}"
    if kind == "outside-box":
        note = re.search(r"inside the note “(.+?)”", m)
        note = f" “{note.group(1)}”" if note else ""
        return f"Picture inside its note / box{note}{at_a}", f"Picture outside the note / box{at_b}"
    if kind == "text-as-graphic":
        t = _q(d.get("baseline_text"))
        return f"{t} as live text (selectable, searchable){at_a}", f"{t} drawn as a picture, not live text{at_b}"
    if kind in ("cells split", "cells merged"):
        ex = re.findall(r"“(.+?)” (\d+)→(\d+)", m)
        if ex:
            more = f" (and {len(ex) - 1} more row(s))" if len(ex) > 1 else ""
            return (f"Row “{ex[0][0]}” has {ex[0][1]} cells{at_a}",
                    f"Row “{ex[0][0]}” has {ex[0][2]} cells{more}{at_b}")
    # pictures
    if check == "assets" or "missing image label" in types:
        if "missing image label" in types:
            mm = re.search(r"Image labels missing in stage: (.*) on the prod picture \(p\.(\d+)\) are not on the stage picture \(p\.(\d+)\)(.*)", m)
            if mm:
                drawn = mm.group(4).strip("; ").replace("drawn into the stage picture:", "drawn into the picture:")
                return (f"The picture (prod p.{mm.group(2)}) with its labels {mm.group(1)}",
                        f"The picture (stage p.{mm.group(3)}) without the labels {mm.group(1)}" + (f"; {drawn}" if drawn else ""))
            return (f"Text {_q(d.get('baseline_text'))} with the picture{at_a}", f"The text is not in stage{at_b}")
        base = {"missing": ("Picture shown" + at_a, "No picture in stage"),
                "extra": ("No picture here in prod", "An extra picture" + at_b),
                "broken": ("Picture displays" + at_a, "Picture does not display: blank or failed to load" + at_b),
                "changed": ("The prod picture" + at_a, "A different picture" + at_b),
                "blackout": ("The picture shows its content" + at_a, f"{d.get('dark_share', 0):.0%} of the picture is black" + at_b),
                "pixelated": (f"Sharp picture: {d['baseline_ppi']} pixels per inch" if d.get("baseline_ppi") else "Sharp vector artwork",
                              f"Pixelated: {d.get('candidate_ppi')} pixels per inch"),
                "combined": ("A separate picture" + at_a, "Part of one larger combined picture" + at_b),
                "raster-vs-vector": ("Artwork drawn as vectors / embedded image" + at_a, "Same artwork, other format" + at_b)}.get(kind)
        exp, act = base or (("The prod picture" + at_a), m)
        if img_a:
            exp += f": {img_a}"
        if img_b:
            act += f": {img_b}"
        if kind is None and d.get("baseline_width") is not None:  # size / aspect of a paired picture
            exp = f"Picture{at_a}: " + (img_a or f"{d['baseline_width']:.0%} of the text width") + f", aspect {d.get('baseline_aspect')}"
            act = f"Picture{at_b}: " + (img_b or f"{d['candidate_width']:.0%} of the text width") + f", aspect {d.get('candidate_aspect')}"
        return exp, act
    # links
    if kind in ("missing-link", "extra-link") or "missing link" in types or "extra link" in types:
        tgt = d.get("target") or "its target"
        text = re.search(r"“(.*?)”", m)
        text = _q(text.group(1)) if text else "The text"
        if kind == "extra-link" or "extra link" in types:
            return f"{text} is plain text, no link{at_a}", f"{text} links to {tgt}{at_b}"
        if "looks like a link" in m:
            return f"{text} is a clickable link to {tgt}{at_a}", f"{text} looks like a link but is not clickable{at_b}"
        return f"{text} is a link to {tgt}{at_a}", f"{text} is plain text, no link{at_b}"
    if kind == "broken-link":
        return "The link goes to an existing page or address", m.split(": ", 1)[-1] if ": " in m else m
    # list numbers / bullets
    if kind == "marker glued":
        return (f"A space between the list number / bullet and its text (gap {d.get('baseline')})",
                f"No space: the number touches its text (gap {d.get('candidate')})")
    if kind == "bullet marker" and ("added in stage" in m or "missing in stage" in m):
        mark = re.search(r"“(.+?)”", m)
        item = re.search(r"before “(.+?)”", m)
        mk, it = (mark.group(1) if mark else "the marker"), (item.group(1) if item else "the item")
        if "added in stage" in m:
            return f"No “{mk}” before “{it}”{at_a}", f"“{mk}” before “{it}”{at_b}"
        return f"“{mk}” before “{it}”{at_a}", f"No “{mk}” before “{it}”{at_b}"
    # text: the full prod text vs the full stage text
    if check == "content" and d.get("op") in ("delete", "insert", "replace"):
        bt, ct = d.get("baseline_text") or "", d.get("candidate_text") or ""
        sa, sb = d.get("baseline_sentence") or "", d.get("candidate_sentence") or ""
        if sa and sb and d.get("words", 99) <= 6:  # a short difference: shown in its sentence on both sides
            op = d["op"]
            what_a = (f"no {_q(ct)} here" if op == "insert" else f"with {_q(bt)}")
            what_b = (f"extra {_q(ct)}" if op == "insert" else f"{_q(bt)} missing" if op == "delete"
                      else f"{_q(ct)} instead of {_q(bt)}")
            return f"{_q(sa)}  ({what_a}){at_a}", f"{_q(sb)}  ({what_b}){at_b}"
        arrow = re.search(r"“([^”]*)”\s*(\([^)]*\))?\s*→\s*“([^”]*)”\s*(\([^)]*\))?", m)
        if arrow and (arrow.group(2) or arrow.group(4)):  # "Word gap differs: “Power button” (1 space) → “Powerbutton” (no space)"
            return (f"{_q(arrow.group(1))} {arrow.group(2) or ''}".strip() + at_a,
                    f"{_q(arrow.group(3))} {arrow.group(4) or ''}".strip() + at_b)
        return (_q(bt) + at_a if bt else "Not in prod: nothing here" + at_a,
                _q(ct) + at_b if ct else "Not in stage" + at_b)
    # tables
    if check == "tables":
        row = re.search(r"“(.*)”", m)
        row = _q(row.group(1)) if row else ""
        if kind == "missing row":
            return f"Row {row}{at_a}", "The row is not in stage"
        if kind == "extra row":
            return "No such row in prod", f"Row {row}{at_b}"
        if kind == "row order":
            return f"The table rows in the prod order{at_a}", f"Rows in another order: {m.split(' (e.g. ', 1)[-1].rstrip(')')}{at_b}"
        if kind == "table-border":
            return f"Borders drawn: {', '.join(d.get('missing') or [])}{at_a}", f"Borders missing: {', '.join(d.get('missing') or [])}{at_b}"
        if kind == "table-border-added":
            return f"No border: {', '.join(d.get('added') or [])}{at_a}", f"Border added: {', '.join(d.get('added') or [])}{at_b}"
    # TOC
    if check == "toc":
        lv = re.search(r"is level (\d+) in prod, level (\d+) in stage", m)
        if lv:
            e = re.search(r"“(.+?)”", m)
            e = _q(e.group(1)) if e else "The entry"
            return f"{e} at level {lv.group(1)}", f"{e} at level {lv.group(2)}"
        e = re.search(r"“(.+?)”", m)
        e = _q(e.group(1)) if e else "The entry"
        if kind == "missing entry" or "missing entry" in types:
            return f"{e} listed in the TOC", f"{e} not listed in the TOC"
        if kind == "extra entry" or "extra entry" in types:
            return f"{e} not listed in the TOC", f"{e} listed in the TOC"
    # generic: “A” → “B”, "A → B", "X in prod, Y in stage"
    arrow = re.search(r"“([^”]*)”[^“→]*→\s*“([^”]*)”", m)
    if arrow:
        return _q(arrow.group(1)) + at_a, _q(arrow.group(2)) + at_b
    arrow = re.search(r":\s*([^:→]+?)\s*→\s*([^()]+?)(?:\s*\(|$)", m)
    if arrow:
        return arrow.group(1).strip() + at_a, arrow.group(2).strip() + at_b
    ps = re.search(r"(.+?) in prod,? (.+?) in stage", m)
    if ps:
        act = re.sub(r"^(but|and)\s+", "", ps.group(2).strip())
        return ps.group(1).split(": ", 1)[-1].strip() + " (prod)", act[:1].upper() + act[1:] + " (stage)"
    return f"As in prod{at_a}", m + at_b


def where(f: dict) -> tuple[str, str]:
    """('p.12', 'p.14') - pages of the finding in prod and stage ('' when one-sided without position)."""
    def pages(locs_, at):
        ps = sorted({l["page"] + 1 for l in locs_}) or ([at["page"] + 1] if at else [])
        return ", ".join(f"p.{p}" for p in ps)
    return pages(f.get("baseline") or [], f.get("baseline_at")), pages(f.get("candidate") or [], f.get("candidate_at"))
