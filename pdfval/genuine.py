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
from .checks.integrity import links as page_links
from .model import Doc, Finding, Image, Loc

_TOKEN = re.compile(r"[\w%]+")
_GOTO = (pymupdf.LINK_GOTO, pymupdf.LINK_NAMED)


def cross_section(results: list[tuple[Unit, list[Finding]]], A: Doc, B: Doc, cfg: dict, mode: str = "pdf") -> None:
    """Relate one-sided findings across sections (mutates the finding lists)."""
    gcfg = cfg.get("genuine", {})
    units = [u for u, _ in results]
    _images(results, A, B, cfg)
    _duplicate_images(results, A, B, cfg)
    _content(results, gcfg.get("moved_min_words", 5), gcfg.get("moved_overlap", 0.8))
    _duplicate_content(results, A, gcfg.get("moved_min_words", 5), gcfg.get("moved_overlap", 0.8))
    _text_as_graphics(results, A, B, cfg)  # first: it finds the text at its own spot, OCR anywhere
    _text_in_images(results, A, B, cfg)
    _image_labels(results, A, cfg)
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
                     if d.get("color") is not None and any(it[0] in ("c", "qu") for it in d["items"])]
            merged: list[pymupdf.Rect] = []
            for r in boxes:  # merge strokes that touch (within 12 pt) into one figure box
                r = pymupdf.Rect(r)
                for m in [m for m in merged if (m + (-12, -12, 12, 12)).intersects(r)]:
                    r |= m
                    merged.remove(m)
                merged.append(r)
            out.setdefault(pno, []).extend(m for m in merged if m.width >= 30 and m.height >= 30)
    return out


def _text_in_images(results, A: Doc, B: Doc, cfg: dict) -> None:
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
    for u, fs in results:
        todo = [f for f in fs if f.check == "content" and "missing text" in (f.types or [])
                and 0 < len(_tokens(f.detail.get("baseline_text"))) <= max_words and f.baseline]
        if todo and figs is None:
            figs = _figures(A, cfg)
        # most of its lines on / next to a figure (dimension labels also sit by straight arrows)
        on_fig = lambda l: any((r + (-pad, -pad, pad, pad)).contains(pymupdf.Rect(l.bbox)) for r in figs.get(l.page, []))
        todo = [f for f in todo if 2 * sum(map(on_fig, f.baseline)) >= len(f.baseline)]
        if not todo or not pics:
            continue
        pages = {B.words[k].page for k in range(*u.b_range)} if u.b_range[1] > u.b_range[0] else set()
        near = {p + d for p in pages for d in (-1, 0, 1)}
        order = [im for im in pics if im.page in near] + [im for im in pics if im.page not in near]
        for f in todo:
            text = f.detail.get("baseline_text", "")
            # normal and 2x readings of every picture first; a sharper 3x reading only when needed
            hit = next((im for scales in ((1, 2), (1, 2, 3)) for im in order
                        if ocr.found(text, ocr.image_text(B.path, im.page, im.bbox, dpi, scales))), None)
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


def _drawn(A: Doc, box, page: int, B: Doc, at: Loc, thr: float, edge_thr: float, hays: dict):
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
        for s in np.geomspace(0.8, 2.0, 9):
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


def _glyphs_match(A: Doc, a_page: int, box, B: Doc, page: int, near, edge_thr: float):
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
    for s in np.geomspace(0.8, 2.0, 25):
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


def _image_labels(results, A: Doc, cfg: dict) -> None:
    """Missing prod text that is an image's label: drawn on the picture (callouts), or a short
    caption of 2-12 words directly above or below it. A lone missing word next to a picture is
    ordinary missing text."""
    acfg = cfg["assets"]
    pics = [x for x in A.images if not icon_max(A, x, acfg)]

    def on(l, im, pad):
        x0, y0, x1, y1 = l.bbox
        return im.page == l.page and x0 >= im.bbox[0] - pad and x1 <= im.bbox[2] + pad \
            and y0 >= im.bbox[1] - pad and y1 <= im.bbox[3] + pad

    def caption(l, im):
        x0, y0, x1, y1 = l.bbox
        return im.page == l.page and x0 < im.bbox[2] and x1 > im.bbox[0] \
            and (0 <= im.bbox[1] - y1 <= 32 or 0 <= y0 - im.bbox[3] <= 32)

    for _, fs in results:
        for f in fs:
            if f.check != "content" or "missing text" not in (f.types or []) or not f.baseline:
                continue
            n = len(_tokens(f.detail.get("baseline_text")))
            if n < 2:
                continue
            if any(all(on(l, im, 4) for l in f.baseline) for im in pics) or \
                    (n <= 12 and any(caption(l, im) for l in f.baseline for im in pics)):
                f.types = ["missing image label"] + f.types
                f.message = "Image label / caption missing in stage: " + f.message


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


def snippet_text(t: str | None, n: int = 12) -> str:
    w = (t or "").split()
    return " ".join(w[:n]) + (" …" if len(w) > n else "")


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
        elif mode == "pdf" and la.get("kind") == pymupdf.LINK_URI and lb.get("kind") == pymupdf.LINK_URI:
            ua_, ub_ = (la.get("uri") or "").rstrip("/"), (lb.get("uri") or "").rstrip("/")
            if ua_ and ub_ and ua_ != ub_:
                out.append(Finding(
                    "integrity", "error",
                    f"Link goes to a different address: “{text}” → {ua_} in prod but {ub_} in stage",
                    locs(A, idx_a or [i]), locs(B, idx_b or [j]),
                    {"kind": "link-target-differs", "baseline_uri": ua_, "candidate_uri": ub_},
                    types=["link target differs"]))
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
}


def tag(f: dict, genuine_types: set[str], data_min_words: int = 3, exclude_checks: set[str] = frozenset()) -> None:
    """Set f['genuine'] and, for genuine findings, f['issue'] (short name), f['description'] (what
    exactly is wrong and where) and f['why'] (why it matters). Findings of `exclude_checks`
    (e.g. "toc") are never genuine; they stay in the full report."""
    types = [] if f.get("check") in exclude_checks else (f.get("types") or [])
    gt = [t for t in types if t in genuine_types]
    # text is data missing when its words are really absent from stage, not one changed or moved word
    if gt == ["missing text"] and f["check"] == "content" and not f.get("critical") \
            and f["detail"].get("absent_words", 0) < data_min_words:
        gt = []
    f["genuine"] = bool(gt)
    if gt:
        # bullets: a missing / added / changed bullet point is named as such, the rest is alignment
        kind = f["detail"].get("kind")
        key = kind if gt[0] == "bullet" and kind in _WHY else gt[0]
        name, why = _WHY.get(key, (gt[0].capitalize(), ""))
        f["issue"], f["why"] = name, why
        f["description"] = f["message"]


def where(f: dict) -> tuple[str, str]:
    """('p.12', 'p.14') - pages of the finding in prod and stage ('' when one-sided without position)."""
    def pages(locs_, at):
        ps = sorted({l["page"] + 1 for l in locs_}) or ([at["page"] + 1] if at else [])
        return ", ".join(f"p.{p}" for p in ps[:4]) + (" …" if len(ps) > 4 else "")
    return pages(f.get("baseline") or [], f.get("baseline_at")), pages(f.get("candidate") or [], f.get("candidate_at"))
