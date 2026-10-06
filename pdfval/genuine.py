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
from collections import Counter, defaultdict

import pymupdf

from . import ocr
from .checks import Aligner, Unit, locs, snippet
from .checks.assets import icon_max, pixel_compare, section_images, visual, visual_distance
from .checks.tables import is_curve
from .checks.integrity import links as page_links
from .model import Doc, Finding, Image, Loc

_TOKEN = re.compile(r"[\w%]+")
_GOTO = (pymupdf.LINK_GOTO, pymupdf.LINK_NAMED)


# text a reader sees in stage, only not as live text: dropped unless [genuine] report_visually_present = true
VISUALLY_PRESENT = {"label in picture", "text in image", "text as graphic"}
# text that belongs to a picture (its labels, callout numbers, dimension lines): dropped unless
# [genuine] report_image_labels = true
IMAGE_TEXT = {"missing image label", "label in picture", "text in image"}


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
    _repeated_content(results, A, B, gcfg.get("duplicate_min_words", 3))
    say("Looking for missing text drawn as graphics")
    _text_as_graphics(results, A, B, cfg)  # first: it finds the text at its own spot, OCR anywhere
    _graphics_in_prod(results, A, B, cfg)  # the other way: extra stage text that prod has drawn as a graphic
    _text_in_images(results, A, B, cfg, say)
    say("Checking links and image labels")
    _image_labels(results, A, B, cfg)
    _label_wording(results, A, B, cfg)
    _text_in_matched_artwork(results)
    if not gcfg.get("report_visually_present", False):
        # the text is there for the reader, only drawn into the stage picture instead of live text: not an issue
        # (the steps above still used these to know the words are not missing)
        for u, findings in results:
            for f in findings:
                if set(f.types or []) and set(f.types) <= VISUALLY_PRESENT:
                    credit_dropped(u, f)
            findings[:] = [f for f in findings if not (set(f.types or []) and set(f.types) <= VISUALLY_PRESENT)]
    if not gcfg.get("report_image_labels", False):
        # labels, numbers and callout text in / on a picture (prod live text, stage drawn into the picture or
        # without it): content of the image, not reported. A finding that is only that is dropped; one with
        # another difference keeps that difference.
        for u, findings in results:
            keep = []
            for f in findings:
                types = set(f.types or [])
                if types & IMAGE_TEXT:
                    rest = [t for t in (f.types or []) if t not in IMAGE_TEXT]
                    if not rest:
                        credit_dropped(u, f)
                        if "missing image label" in types:
                            # a label of the picture missing in stage: kept for the image report only
                            # (image-issues.pdf), not counted or shown anywhere else
                            f.detail = {**f.detail, "image_report_only": True}
                            keep.append(f)
                        continue
                    f.types = rest
                keep.append(f)
            findings[:] = keep
        _drop_picture_text(results, A, B, cfg)
    _xref_format(results, A, B)
    # suppress false "missing text" findings when the text appears in stage links
    _suppress_link_text_missing(results, B, cfg)
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


def _link_text_pool(doc: Doc) -> set[str]:
    """Extract all text that appears inside links (clickable regions). Returns normalized words."""
    pool = set()
    try:
        for page_num in range(len(doc.pages)):
            for link in page_links(doc, page_num):
                if link.get("from"):
                    link_rect = pymupdf.Rect(link["from"])
                    # find all words whose bbox overlaps the link region
                    for word in doc.words:
                        if word.page == page_num and word.norm:
                            w_rect = pymupdf.Rect(word.bbox)
                            if w_rect.intersects(link_rect):
                                pool.add(word.norm.lower())
    except Exception:
        pass
    return pool


def _suppress_link_text_missing(results, B: Doc, cfg: dict) -> None:
    """Suppress false "missing text" findings when text appears in stage links.
    When missing prod text is found in stage link regions, downgrade/remove the finding."""
    icfg = cfg.get("integrity", {})
    if not icfg.get("check_link_text_in_content", True):
        return
    
    ignore_missing = icfg.get("ignore_link_text_missing", True)
    link_pool = _link_text_pool(B)
    
    if not link_pool:
        return
    
    for u, findings in results:
        # check all "missing text" findings in this unit
        to_remove = []
        for f in findings:
            if f.check != "content" or "missing text" not in (f.types or []) or not f.baseline:
                continue
            
            # extract words from the missing text
            missing_words = set()
            if f.detail and "baseline_text" in f.detail:
                missing_words = {w.lower() for w in _TOKEN.findall(f.detail["baseline_text"])}
            
            # check if all (or most) missing words appear in links
            if missing_words:
                found_in_links = sum(1 for w in missing_words if w in link_pool)
                pct_found = found_in_links / len(missing_words) if missing_words else 0.0
                
                # if >= 50% of missing words are in links, it's likely a false positive
                if pct_found >= 0.5:
                    if ignore_missing:
                        # suppress the finding entirely
                        to_remove.append(f)
                    else:
                        # downgrade severity for visibility (but don't fail validation)
                        f.severity = "info"
                        f.message = f"{f.message} [found in link text]"
        
        # remove suppressed findings
        for f in to_remove:
            findings.remove(f)


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


def _repeated_content(results, A: Doc, B: Doc, min_words: int) -> None:
    """Extra stage text that repeats content: judged by the content, not by headings. Points repeated
    under one heading (the heading once, its bullets twice), a paragraph repeated in its own section, a
    section's text repeated under another (or an unbookmarked) heading. Every sentence / bullet / line
    block of stage that stage has more often than prod is a duplicate; an extra-text finding made of
    such sentences is that duplicate (with other new words in it: both). Text read in another order
    (a table) has the same count on both sides: not a duplicate."""
    def sentences(doc: Doc) -> list[tuple[tuple, list[int]]]:
        out, cur, toks = [], [], []

        def close():
            if len(toks) >= min_words:
                out.append((tuple(toks), list(cur)))
            cur.clear()
            toks.clear()
        prev_block = None
        for k, w in enumerate(doc.words):
            block = doc.lines[w.line].block if 0 <= w.line < len(doc.lines) else None
            bullet = not any(c.isalnum() for c in w.text) and w.text.strip() in ("•", "·", "–", "-", "▪", "■", "○", "◦")
            if (block != prev_block or bullet or w.page != (doc.words[k - 1].page if k else w.page)) and cur:
                close()
            prev_block = block
            cur.append(k)
            toks.extend(_tokens(w.text))
            if re.search(r"[.!?:;]$", w.text):
                close()
        close()
        return out

    from .checks.table_cells import _page_tables
    tbox: dict = {}

    def in_table(doc: Doc, ws: list[int]) -> bool:
        # table rows repeat by design (“HDMI v v v” in every colour-space table): the cell check's job
        w = doc.words[ws[0]]
        key = (id(doc), w.page)
        if key not in tbox:
            tbox[key] = [t["bbox"] for t in _page_tables(doc, w.page)]
        cx, cy = (w.bbox[0] + w.bbox[2]) / 2, (w.bbox[1] + w.bbox[3]) / 2
        return any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in tbox[key])

    sa = [(t, ws) for t, ws in sentences(A) if not in_table(A, ws)]
    sb = [(t, ws) for t, ws in sentences(B) if not in_table(B, ws)]
    count_a = Counter(t for t, _ in sa)
    count_b = Counter(t for t, _ in sb)
    # prod content that stage has more often: a copy of what prod has (text new to stage is extra text)
    dup = [(t, ws) for t, ws in sb if count_b[t] > count_a[t] >= 1]
    if not dup:
        return
    section_of = lambda k: next((u for u, _ in results if u.b_range[0] <= k < u.b_range[1]), None)

    def inside(k: int, f: Finding) -> bool:
        w = B.words[k]
        cx, cy = (w.bbox[0] + w.bbox[2]) / 2, (w.bbox[1] + w.bbox[3]) / 2
        return any(l.page == w.page and l.bbox[0] - 1 <= cx <= l.bbox[2] + 1 and l.bbox[1] - 1 <= cy <= l.bbox[3] + 1
                   for l in f.candidate)

    times = lambda n: "once" if n == 1 else f"{n} times"
    for u, fs in results:
        for f in [f for f in fs if f.check == "content" and "extra text" in (f.types or [])
                  and f.detail.get("op") in ("insert", "replace") and not f.detail.get("moved_in") and f.candidate]:
            hits = [(t, ws) for t, ws in dup if sum(inside(k, f) for k in ws) >= 0.6 * len(ws)]
            if not hits:
                continue
            n_dup = sum(len(t) for t, _ in hits)
            extra = len(_tokens(f.detail.get("candidate_text")))
            t0, ws0 = hits[0]
            # the other copy of the first duplicated sentence: an occurrence outside this finding
            other = next((ws for t, ws in sb if t == t0 and not any(inside(k, f) for k in ws)), None)
            where = ""
            if other:
                w, ou = B.words[other[0]], section_of(other[0])
                where = f" (the other copy: stage p.{w.page + 1}" + \
                    (f", “{ou.title}”)" if ou is not None and ou is not u else ", same section)")
            text = " ".join(" ".join(B.words[k].text for k in ws) for _, ws in hits)
            f.message = (f"Content duplicated in stage: “{snippet_text(text)}” is in stage {times(count_b[t0])} "
                         f"but {times(count_a[t0])} in prod{where}"
                         + (f" · {f.message}" if n_dup < 0.8 * extra else ""))
            f.severity = "error"
            f.types = ["duplicate content"] + ([t for t in f.types if t != "duplicate content"] if n_dup < 0.8 * extra else [])
            f.detail = {**f.detail, "op": "duplicate", "duplicated_text": text, "stage_copies": count_b[t0],
                        "prod_copies": count_a[t0],
                        **({"other_copy": {"page": B.words[other[0]].page, "bbox": list(B.words[other[0]].bbox)}} if other else {})}
    # an extra section (a heading prod does not have) whose text is all duplicated: the section is a duplicate
    for u, fs in results:
        for sf in [f for f in fs if f.check == "structure" and f.detail.get("anchor_side") == "candidate"
                   and f.detail.get("anchor_word") is not None and "Extra section" in f.message]:
            k = sf.detail["anchor_word"]
            body = next((f for f in fs if "duplicate content" in (f.types or []) and len(f.types) == 1
                         and (inside(k, f) or any(l.page == B.words[k].page and 0 <= l.bbox[1] - B.words[k].bbox[3] <= 40
                                                  for l in f.candidate[:1]))), None)
            if body is None:
                continue
            title = sf.detail.get("heading", "")
            sf.message = (f"Duplicate section in stage: “{title}” (p.{B.words[k].page + 1}) repeats content stage already "
                          f"has: {body.message.removeprefix('Content duplicated in stage: ')}")
            sf.severity, sf.critical, sf.types = "error", True, ["duplicate section"]
            sf.candidate = [*sf.candidate, *body.candidate]
            sf.detail = {**sf.detail, "kind": "duplicate-section", "folded": sf.detail.get("folded", 0) + 1}
            fs.remove(body)


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
            # artwork drawn as many small filled shapes, no strokes (a QR code, a pixel icon): a dense
            # cluster of them is a figure too (a table's cell fills are few and large)
            fills = [pymupdf.Rect(d["rect"]) for d in page.get_drawings()
                     if d.get("fill") is not None and d["rect"].width < 20 and d["rect"].height < 20]
            groups: list[list] = []  # [box, count]
            for r in fills:
                hit = [g for g in groups if (g[0] + (-3, -3, 3, 3)).intersects(r)]
                box, n = pymupdf.Rect(r), 1
                for g in hit:
                    box |= g[0]
                    n += g[1]
                    groups.remove(g)
                groups.append([box, n])
            out[pno].extend(g[0] for g in groups if g[1] >= 30 and g[0].width >= 30 and g[0].height >= 30)
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
            f.detail = {**f.detail, "ocr_page": hit.page, "ocr_box": list(hit.bbox), "credited": True}
            _count_present(u, n)


def credit_dropped(u: Unit, f: Finding) -> None:
    """A text finding left out of the report (picture labels, text drawn into a picture, an ignored type): its
    words are not a content difference either - prod's missing words count as matched, stage's extra words
    as nothing extra - so the content % says the same as the issues do. Once per finding."""
    if not u.content or f.detail.get("credited") or f.check not in ("content", "layout"):
        return
    f.detail["credited"] = True
    base, cand = _tokens(f.detail.get("baseline_text")), _tokens(f.detail.get("candidate_text"))
    if base and (f.candidate == [] or "missing" in " ".join(f.types or []) or "label" in " ".join(f.types or [])
                 or "graphic" in " ".join(f.types or []) or "image" in " ".join(f.types or [])):
        _count_present(u, len(base))
    elif cand and not base:
        c = u.content
        gone = min(len(cand), c.get("extra_words", 0))
        c["extra_words"] -= gone
        denom = c["baseline_words"] + c["extra_words"]
        c["match_pct"] = round(100.0 * max(c["matched_words"] - c.get("spacing_issues", 0)
                                           - c.get("script_issues", 0), 0) / denom, 2) if denom else 100.0


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
                f.detail = {**f.detail, "kind": "text-as-graphic", "credited": True}
                _count_present(u, len(_tokens(text)))


def _graphics_in_prod(results, A: Doc, B: Doc, cfg: dict) -> None:
    """Extra stage text that prod shows as a graphic, not live text: a word converted to outlines in
    prod (“العربية” in the OSD language list - vector paths, no text to extract) is live text in
    stage. Each extra stage line is rendered and searched for among prod's pictures / drawings
    around the aligned spot (prod's live text blanked). Found: the text is in prod too - not extra
    content (“text as graphic”: reported only with [genuine] report_visually_present)."""
    ccfg = cfg["content"]
    if not ccfg.get("graphic_text", True):
        return
    max_words, thr = ccfg.get("graphic_text_max_words", 12), ccfg.get("graphic_text_score", 0.72)
    edge_thr = ccfg.get("graphic_text_edge_score", 0.65)
    hays: dict[tuple, "np.ndarray"] = {}
    claimed: set = set()
    for u, fs in results:
        for f in [f for f in fs if f.check == "content" and "extra text" in (f.types or [])
                  and f.detail.get("op") == "insert" and f.baseline_at and f.candidate
                  and 0 < f.detail.get("words", 0) <= max_words]:
            if not any(ch.isalnum() for ch in f.detail.get("candidate_text") or ""):
                continue  # a lone bullet / dash matches any stroke
            found = []
            for loc in f.candidate:
                hit = _drawn(B, loc.bbox, loc.page, A, f.baseline_at, thr, edge_thr, hays)
                if hit is None and len(f.candidate) == 1 and f.detail.get("words", 0) <= 3:
                    # drawn in another font (prod's outlines are not stage's font: the pixels differ)
                    hit = _outlined_word(B, loc, A, f.baseline_at, claimed)
                if hit is None:
                    break
                found.append(hit)
            else:
                text = f.detail.get("candidate_text", "")
                f.check, f.severity, f.critical = "layout", ccfg.get("graphic_text_severity", "warning"), False
                f.types = ["text as graphic"]
                f.message = (f"Text shown as a graphic in prod, as live text in stage: “{snippet_text(text)}” "
                             f"(prod p.{f.baseline_at.page + 1} ↔ stage p.{f.candidate[0].page + 1})")
                f.baseline, f.baseline_at = [Loc(f.baseline_at.page, r) for r in found], None
                f.detail = {**f.detail, "kind": "text-as-graphic", "credited": True}
                c = u.content
                if c:  # its words are in prod: not extra
                    c["extra_words"] = max(0, c.get("extra_words", 0) - len(_tokens(text)))
                    denom = c["baseline_words"] + c["extra_words"]
                    c["match_pct"] = round(100.0 * max(c["matched_words"] - c.get("spacing_issues", 0)
                                                       - c.get("script_issues", 0), 0) / denom, 2) if denom else 100.0


def _outlined_word(B: Doc, loc: Loc, A: Doc, at: Loc, claimed: set):
    """Prod rect of a word converted to outlines at the aligned spot: one filled dark vector path of
    many curve / line segments, shaped like the stage word's ink (same aspect ratio, a text-sized
    height), in the same column a line or two from the spot, with no live prod text on it. For a
    word in another font, where the pixels of the two do not match."""
    import numpy as np
    from .checks.assets import _gray
    g = _gray(B, loc.page, loc.bbox, 4.0)
    if g.size == 0:
        return None
    ys, xs = np.where(g < 128)
    if not len(xs):
        return None
    iw, ih = (xs.max() - xs.min() + 1) / 4, (ys.max() - ys.min() + 1) / 4
    if ih < 3:
        return None
    pdf = pymupdf.open(A.path)
    best = None
    for d in pdf[at.page].get_drawings():
        r = d["rect"]
        fill = d.get("fill")
        if (at.page, round(r.x0), round(r.y0)) in claimed or not fill or sum(fill) / 3 > 0.5:
            continue
        if len(d["items"]) < 12 or not any(it[0] == "c" for it in d["items"]):
            continue  # a glyph outline is curves; a rule or box is not
        if not (0.6 * ih <= r.height <= 2.0 * ih) or not 0.65 <= (r.width / r.height) / (iw / ih) <= 1.5:
            continue
        if r.x1 < at.bbox[0] - 40 or r.x0 > at.bbox[2] + 40:
            continue  # another column
        gap = max(at.bbox[1] - r.y1, r.y0 - at.bbox[3], 0)
        if gap > 3 * max(at.bbox[3] - at.bbox[1], r.height):
            continue
        if any(w.page == at.page and _in(w, r) for w in A.words):
            continue  # live text there: not an outlined word
        if best is None or gap < best[0]:
            best = (gap, r)
    if best is None:
        return None
    r = best[1]
    # the shape only says “a word is drawn here”: what it says is read (OCR, in the stage word's script)
    # and must be the stage word - else the stage text is extra after all
    text = " ".join(w.text for w in B.words if w.page == loc.page and _in(w, pymupdf.Rect(loc.bbox)))
    if not _reads_as(A.path, at.page, (r.x0, r.y0, r.x1, r.y1), text):
        return None
    claimed.add((at.page, round(r.x0), round(r.y0)))
    return (r.x0, r.y0, r.x1, r.y1)


# Unicode script of a word -> tesseract language
_SCRIPTS = [((0x0600, 0x06FF), "ara"), ((0x0750, 0x077F), "ara"), ((0xFB50, 0xFDFF), "ara"), ((0xFE70, 0xFEFF), "ara"),
            ((0x0590, 0x05FF), "heb"), ((0x0E00, 0x0E7F), "tha"), ((0x0900, 0x097F), "hin"), ((0x0400, 0x04FF), "rus"),
            ((0x0370, 0x03FF), "ell"), ((0xAC00, 0xD7AF), "kor"), ((0x3040, 0x30FF), "jpn"), ((0x4E00, 0x9FFF), "chi_sim+chi_tra")]


def _reads_as(path: str, page: int, box: tuple, text: str) -> bool:
    """Does the drawing in `box` read as `text`? OCR as one line in the word's own script; letters
    and digits compared after Unicode normalisation (Arabic presentation forms “ﺍﻟﻌﺮﺑﻴﺔ” are
    “العربية”), about one wrong character in five allowed. False when OCR is not available."""
    import subprocess
    import unicodedata
    from . import ocr as _ocr
    from .glyphs import _langs
    if not _ocr.available():
        return False
    key = lambda t: "".join(c for c in unicodedata.normalize("NFKC", t or "") if c.isalnum()).lower()
    want = key(text)
    if not want:
        return False
    code = next((ord(c) for c in want if not c.isascii()), None)
    lang = next((l for (lo, hi), l in _SCRIPTS if code is not None and lo <= code <= hi), "eng")
    have = set(_langs())
    lang = "+".join(x for x in lang.split("+") if x in have) or "eng"
    pdf = pymupdf.open(path)
    for pad, psm in ((2, "7"), (2, "8"), (6, "7")):
        rect = pymupdf.Rect(box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad)
        png = pdf[page].get_pixmap(clip=rect, dpi=300, colorspace=pymupdf.csGRAY).tobytes("png")
        try:
            got = key(subprocess.run(["tesseract", "stdin", "stdout", "-l", lang, "--psm", psm], input=png,
                                     capture_output=True, timeout=60).stdout.decode("utf-8", "replace"))
        except Exception:
            continue
        if got and _edits(want, got) <= len(want) // 5:
            return True
    return False


def _edits(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


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


def skip_picture_text(doc: Doc, cfg: dict) -> int:
    """Text that belongs to a picture is artwork, not content: the words on a picture (an embedded image or a
    vector illustration - callout letters “a b c d”, “Computer / gaming console”, port names) and short labels
    set right at its edge (“Inside the VESA cover” at the end of a leader line) are taken out of every
    comparison (content, fonts, labels) - prod's diagram font “c” is never compared with stage's table “c.”.
    Returns the number of words taken out."""
    from .checks.assets import icon_max
    acfg = cfg.get("assets", {})
    n = 0
    by_line: dict[int, list] = defaultdict(list)
    for k, w in enumerate(doc.words):
        by_line[w.line].append(k)
    block_words = Counter(doc.lines[w.line].block for w in doc.words if w.norm)
    doc.picture_text = {}  # (page, picture box) -> word indices of its labels / callout numbers
    pics: dict[int, list] = {}
    for w in doc.words:
        if w.page in pics:
            continue
        rs = [x for x in doc.images if x.page == w.page and not icon_max(doc, x, acfg)]
        pics[w.page] = [x.bbox for x in rs] + [x.bbox for x in _vector_pictures(doc, w.page)] + \
            [x.bbox for x in _line_drawings(doc, w.page)]
        try:  # layout diagrams drawn as plain boxes with a code in each (“(1.1)” “(2.1)”): figures too
            from .checks import tables as _t
            pics[w.page] += _t.diagram_boxes(doc, w.page)
        except Exception:
            pass
    # text in a table is content, even where the table's drawn number badges (❾ ❿) make its region look like
    # a vector picture: a table row's short cells ("Your Apps", "Browse your installed apps.") are never artwork
    from .checks import tables as _tables
    tbls: dict[int, list] = {}

    def in_table(w) -> bool:
        if w.page not in tbls:
            try:
                figs = set(_tables.diagram_boxes(doc, w.page))  # a diagram drawn as a grid is a figure, not a table
                tbls[w.page] = [t[1] for t in _tables._raw(doc, w.page) if tuple(t[1]) not in figs]
            except Exception:
                tbls[w.page] = []
        cx, cy = (w.bbox[0] + w.bbox[2]) / 2, (w.bbox[1] + w.bbox[3]) / 2
        return any(b[0] - 1 <= cx <= b[2] + 1 and b[1] - 1 <= cy <= b[3] + 1 for b in tbls[w.page])
    for line, ks in by_line.items():
        ws = [doc.words[k] for k in ks]
        boxes = pics.get(ws[0].page) or []
        if not boxes:
            continue
        x0, y0 = min(w.bbox[0] for w in ws), min(w.bbox[1] for w in ws)
        x1, y1 = max(w.bbox[2] for w in ws), max(w.bbox[3] for w in ws)
        cy = (y0 + y1) / 2
        on = lambda b, w: b[0] - 2 <= (w.bbox[0] + w.bbox[2]) / 2 <= b[2] + 2 and b[1] - 2 <= (w.bbox[1] + w.bbox[3]) / 2 <= b[3] + 2
        # a short label starting (or ending) right at a picture's edge, level with it - or tied to the picture by
        # a leader line (a callout “1” in a box 28 pt left of a screenshot, its line running into the picture)
        beside = len(ws) <= 6 and (any(b[1] - 4 <= cy <= b[3] + 4 and (-4 <= x0 - b[2] <= 8 or -4 <= b[0] - x1 <= 8)
                                       for b in boxes) or _leader_to_picture(doc, ws[0].page, (x0, y0, x1, y1), boxes))
        # a callout number set just above / below a picture, within its width (“1” over a monitor drawing, at the
        # end of a short leader stub) - numbers only: a caption there (“(PD2720U)”) is text both sides have
        if not beside and all(re.fullmatch(r"\(?(\d{1,2}|[A-Za-z])[.)]?", w.text) for w in ws):
            cx = (x0 + x1) / 2
            beside = any(b[0] - 4 <= cx <= b[2] + 4 and (0 <= b[1] - y1 <= 10 or 0 <= y0 - b[3] <= 10) for b in boxes)
        if len(ws) > 6 or block_words.get(doc.lines[line].block, 0) > 15:
            continue  # a sentence / paragraph (its last short line too): content, even where a drawing's box overlaps it
        if any(w.role and w.role.startswith("h") for w in ws):
            continue  # a heading beside a picture is the document's structure
        if in_table(ws[0]):
            continue
        if _list_marker(doc, ws):
            continue  # "1." beginning a list item whose text follows on its right: content, however close the picture
        if _in_text_column(doc, line, by_line, block_words):
            continue  # a bullet item, or a short line of the text column ("Remark:" under the list): content
        for k, w in zip(ks, ws):
            if w.norm and (beside or any(on(b, w) for b in boxes) or _floating_marker(doc, ws)):
                # kept for the image report: the picture the label belongs to (the nearest one)
                c = ((w.bbox[0] + w.bbox[2]) / 2, (w.bbox[1] + w.bbox[3]) / 2)
                pic = min(boxes, key=lambda b: max(b[0] - c[0], 0, c[0] - b[2]) + max(b[1] - c[1], 0, c[1] - b[3]))
                doc.picture_text.setdefault((w.page, tuple(round(v, 1) for v in pic)), []).append(k)
                w.norm = ""
                n += 1
    return n


_BULLETS = set("•●▪■◦‣·-–—*")
_COLUMN: dict[tuple, list] = {}


def _in_text_column(doc: Doc, line: int, by_line: dict, block_words) -> bool:
    """A short line that is running text, not a picture's label, although a picture is level with it: a bullet item
    (its bullet right before it on the row: "• M6 screw: for PL552/..."), or a line set in a column of body text -
    directly under / above a paragraph line or a bullet item, at the same left edge ("Remark:" under the list it
    belongs to). A label of a picture stands on its own beside the artwork."""
    ks = by_line[line]
    ws = [doc.words[k] for k in ks]
    page = ws[0].page
    key = (doc.path, len(doc.words), len(doc.lines), page)  # (not id(doc): an id is reused by a later document)
    if key not in _COLUMN:
        if len(_COLUMN) > 2000:
            _COLUMN.clear()
        rows = []
        for li, idx in by_line.items():
            lw = [doc.words[k] for k in idx]
            if lw[0].page != page:
                continue
            x0, y0 = min(w.bbox[0] for w in lw), min(w.bbox[1] for w in lw)
            x1, y1 = max(w.bbox[2] for w in lw), max(w.bbox[3] for w in lw)
            glyph = len(lw) == 1 and lw[0].text.strip() in _BULLETS
            starts = lw[0].text.strip() in _BULLETS and len(lw) > 1  # "• Inner loop: ..." read as one line
            rows.append([li, x0, y0, x1, y1, len(lw), glyph, starts])
        bullets = [r for r in rows if r[6]]
        for r in rows:  # a bullet item: its bullet glyph on the row, just left of it - or leading its own line
            r.append(r[7] or any(abs((b[2] + b[4]) / 2 - (r[2] + r[4]) / 2) <= 0.6 * (r[4] - r[2]) and 0 <= r[1] - b[3] <= 18
                                 for b in bullets))
        # running text: a long line, a line of a paragraph block, a bullet item - and, step by step, every short line
        # directly under / above one of those at the same left edge ("Remark:" under the list, then the line under it)
        text = {r[0] for r in rows if not r[6] and (r[5] > 6 or r[8] or block_words.get(doc.lines[r[0]].block, 0) > 15)}
        for _ in range(6):
            more = set()
            for me in rows:
                if me[0] in text or me[6]:
                    continue
                h = max(me[4] - me[2], 1)
                if any(r[0] in text and -1 <= max(r[2] - me[4], me[2] - r[4]) <= 1.5 * h and abs(r[1] - me[1]) <= 14
                       for r in rows):
                    more.add(me[0])
            if not more:
                break
            text |= more
        _COLUMN[key] = (rows, text)
    rows, text = _COLUMN[key]
    me = next((r for r in rows if r[0] == line), None)
    # (a long line needs no rescue here: only short lines are ever taken for labels)
    return me is not None and not me[6] and len(ws) <= 6 and line in text


_ENUM = re.compile(r"\(?(\d{1,3}|[a-zA-Z]|[ivxIVX]{1,4})[.)]")
_BARE_MARKER = re.compile(r"\d{1,2}")


def _floating_marker(doc: Doc, ws: list) -> bool:
    """A bare callout number on a diagram ("1", "2" - no trailing punctuation, nothing else on its line, no
    list item text picks it up to its right): the mirror image of a list marker, which always has item text
    following it. Catches a callout sitting on a diagram drawn with straight/orthogonal lines only (a monitor
    outline, a box): _vector_pictures only recognises curved or slanted-line illustrations as a picture, so a
    plain rectangular line drawing is not in `boxes` and its callout numbers would otherwise read as content."""
    if len(ws) != 1 or not _BARE_MARKER.fullmatch(ws[0].text.strip()):
        return False
    m = ws[0]
    cy, h = (m.bbox[1] + m.bbox[3]) / 2, m.bbox[3] - m.bbox[1]
    if any(w.page == m.page and w.line != m.line and any(c.isalpha() for c in w.text)
           and 0 <= w.bbox[0] - m.bbox[2] <= 40 and abs((w.bbox[1] + w.bbox[3]) / 2 - cy) <= max(3.0, 0.6 * h)
           for w in doc.words[max(0, doc.lines[m.line].first_word - 3):doc.lines[m.line].first_word + 8]):
        return False
    # the number cell of a table row ("2" | "LAN port" | "White" | "Flashing" | ...): its row's cells run on to its
    # right, further than a list item's text (a merged cell sets the number between two lines of the row). A callout
    # on a diagram has no worded text along its row.
    pg = _page_words(doc, m.page)
    return sum(1 for w in pg if w.line != m.line and w.bbox[0] > m.bbox[2] and sum(c.isalpha() for c in w.text) >= 2
               and abs((w.bbox[1] + w.bbox[3]) / 2 - cy) <= 1.2 * max(h, 1)) < 2


_PAGE_WORDS: dict[tuple, list] = {}


def _page_words(doc: Doc, page: int) -> list:
    key = (doc.path, len(doc.words), len(doc.lines), page)
    if key not in _PAGE_WORDS:
        if len(_PAGE_WORDS) > 2000:
            _PAGE_WORDS.clear()
        _PAGE_WORDS[key] = [w for w in doc.words if w.page == page]
    return _PAGE_WORDS[key]


def _list_marker(doc: Doc, ws: list) -> bool:
    """The line is a list's enumerator ("1.", "2)", "a.", "(iv)") with its item's text starting just right of it at the
    same height (a separate line in the PDF: "1." | "Present key with LED indicator"). A picture's callout number has
    no such punctuation ("1", "2" at the end of a leader line) and no item text running on beside it."""
    if len(ws) != 1 or not _ENUM.fullmatch(ws[0].text.strip()):
        return False
    m = ws[0]
    cy, h = (m.bbox[1] + m.bbox[3]) / 2, m.bbox[3] - m.bbox[1]
    return any(w.page == m.page and w.line != m.line and any(c.isalpha() for c in w.text)
               and 0 <= w.bbox[0] - m.bbox[2] <= 40 and abs((w.bbox[1] + w.bbox[3]) / 2 - cy) <= max(3.0, 0.6 * h)
               for w in doc.words[max(0, doc.lines[m.line].first_word - 3):doc.lines[m.line].first_word + 8])


_LEADERS: dict[tuple, list] = {}


def _leader_to_picture(doc: Doc, page: int, box: tuple, pics: list) -> bool:
    """A straight thin line with one end at this text (within 12 pt) and the other end on a picture."""
    key = (doc.path, page)
    if key not in _LEADERS:
        segs = []
        try:
            for d in pymupdf.open(doc.path)[page].get_drawings():
                for it in d["items"]:
                    if it[0] == "l" and abs(it[1] - it[2]) >= 6:
                        segs.append((it[1], it[2]))
        except Exception:
            pass
        _LEADERS[key] = segs
    near = lambda p: box[0] - 12 <= p.x <= box[2] + 12 and box[1] - 12 <= p.y <= box[3] + 12
    on = lambda p: any(b[0] - 2 <= p.x <= b[2] + 2 and b[1] - 2 <= p.y <= b[3] + 2 for b in pics)
    return any((near(a) and on(b)) or (near(b) and on(a)) for a, b in _LEADERS[key])


def _vector_pictures(doc: Doc, page: int, min_w: float = 60, min_h: float = 40) -> list:
    """Illustrations drawn as vectors on a page: clusters of drawings with curves (a projector, a
    mountain) or slanted lines (a booklet drawn in perspective), not tables, frames or note boxes
    (horizontal / vertical lines and rectangles, or full of text)."""
    key = (doc.path, page, min_w, min_h)
    if key not in _VECTOR_PICS:
        pdf = pymupdf.open(doc.path)
        out = []
        try:
            from .checks import tables as _tables
            paths = pdf[page].get_drawings()
            # data tables only (rows of text in 2+ cells): a diagram of lines and labels the detector reads as a
            # table (“Screen Diagonal | W | H” on a projection drawing) is still a drawing
            on_page = [k for k, w in enumerate(doc.words) if w.page == page]
            tabs = [pymupdf.Rect(t.bbox) for t in (_tables.tables(doc, (on_page[0], on_page[-1] + 1)) if on_page else [])
                    if _tables.is_data_table(doc, t)]
            for r in pdf[page].cluster_drawings(drawings=paths):
                if r.width < min_w or r.height < min_h:
                    continue
                if any((r & t).get_area() >= 0.5 * r.get_area() for t in tabs):
                    continue  # a table (its rules, numbered circles ❾ in its “No.” column): text, not a drawing
                inside = [d for d in paths if r.contains(d["rect"])]
                curves = sum(1 for d in inside for it in d["items"] if is_curve(it))
                # a line drawing in perspective (a stack of booklets) is slanted lines; table rules / frames are
                # horizontal and vertical only
                slanted = sum(1 for d in inside for it in d["items"] if it[0] == "l"
                              and abs(it[1].x - it[2].x) > 0.5 and abs(it[1].y - it[2].y) > 0.5)
                words = sum(1 for w in doc.words if w.page == page and _in(w, r))
                # a small cluster of many strokes (a booklet icon, 37 x 29 pt, 64 lines) is a drawing: a table or
                # frame that small has a handful of rules
                many = max(r.width, r.height) <= 60 and sum(len(d["items"]) for d in inside) >= 30
                if (curves >= 8 or slanted >= 12 or many) and words <= 25:
                    out.append(Image(page, tuple(r)))
        except Exception:
            pass
        _VECTOR_PICS[key] = out
    return _VECTOR_PICS[key]


_VECTOR_PICS: dict[tuple, list] = {}
_LINE_ART: dict[tuple, list] = {}


def _line_drawings(doc: Doc, page: int, min_w: float = 60, min_h: float = 40) -> list:
    """Drawings made of straight lines and boxes only (a monitor's front view: rectangles and a stand) - no
    curves, so _vector_pictures does not count them. A group of 6+ strokes with (almost) no text inside is a
    drawing, not a table or a note box (those hold text). Used for picture text only (callout numbers, labels),
    not for comparing pictures."""
    key = (doc.path, page)
    if key not in _LINE_ART:
        out = []
        try:
            pg = pymupdf.open(doc.path)[page]
            paths = pg.get_drawings()
            known = [pymupdf.Rect(x.bbox) for x in _vector_pictures(doc, page)]
            for r in pg.cluster_drawings(drawings=paths):
                if r.width < min_w or r.height < min_h or r.get_area() > 0.6 * pg.rect.get_area():
                    continue
                if any((r & k).get_area() >= 0.5 * r.get_area() for k in known):
                    continue
                strokes = sum(len(d["items"]) for d in paths if r.contains(d["rect"]))
                # text in its interior (its edge may carry the stubs of leader lines, with numbers at their ends)
                core = r + (6, 6, -6, -6)
                words = sum(1 for w in doc.words if w.page == page and core.x0 <= (w.bbox[0] + w.bbox[2]) / 2 <= core.x1
                            and core.y0 <= (w.bbox[1] + w.bbox[3]) / 2 <= core.y1)
                if strokes >= 6 and words <= 3:
                    out.append(Image(page, tuple(r)))
        except Exception:
            pass
        _LINE_ART[key] = out
    return _LINE_ART[key]


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

    def beside(l, im, gap=20, pad_y=20):
        # a label set just right / left of a drawing, inside its height (callouts at the end of leader
        # lines: “Inside the VESA cover”): it starts at the drawing's edge, however long it runs
        x0, y0, x1, y1 = l.bbox
        return im.page == l.page and y0 >= im.bbox[1] - pad_y and y1 <= im.bbox[3] + pad_y \
            and (-4 <= x0 - im.bbox[2] <= gap or -4 <= im.bbox[0] - x1 <= gap)

    def caption(l, im, gap=32):
        # just above / below the picture, overlapping it horizontally; a label touching the picture's frame
        # (its leader line runs into the drawing) may overlap the frame's edge a little
        x0, y0, x1, y1 = l.bbox
        return im.page == l.page and x0 < im.bbox[2] and x1 > im.bbox[0] \
            and (-8 <= im.bbox[1] - y1 <= gap or -8 <= y0 - im.bbox[3] <= gap)

    def stage_copy(x: Image, near: Loc | None, u: Unit):
        pages = {near.page + d for d in (-1, 0, 1)} if near else \
            {B.words[k].page for k in range(*u.b_range)} if u.b_range[1] > u.b_range[0] else set()
        best = None
        # embedded images and pictures drawn as vectors (an AEM illustration is often an SVG)
        for y in [y for y in stage_pics if y.page in pages] + [y for p in sorted(pages) if 0 <= p < len(B.pages)
                                                                for y in _vector_pictures(B, p)]:
            d = visual_distance(A, x, B, y)
            if best is None or d < best[0]:
                best = (d, y)
        if not best:
            return None
        # a sanity check before trusting the hash / pixel correlation: two unrelated line-art diagrams
        # (a tall, narrow remote control vs. a wide rear-panel view) can still hash or correlate alike -
        # both sparse line drawings on white, with scattered callout numbers - so a very different shape
        # rules a match out whatever the other two signals say
        _, xw, xh = visual(A, x)
        _, yw, yh = visual(B, best[1])
        shape_alike = min(xw, xh) > 0 and 0.5 <= (xh / xw) / max(yh / yw, 1e-6) <= 2.0
        if shape_alike and (best[0] <= thr or pixel_compare(A, x, B, best[1])[0] >= same):
            return best[1]
        return None

    # callout numbers of a picture (“1” “2” “3” “4” in boxes with leader lines over a screenshot) are the
    # picture's artwork, not text: a stage picture without them is not missing data - not reported at all
    callout = re.compile(r"^\(?[0-9A-Za-z]{1,2}[.)]?$")
    for u, fs in results:
        for f in list(fs):
            if f.check != "content" or "missing text" not in (f.types or []) or not f.baseline:
                continue
            toks = (f.detail.get("baseline_text") or "").split()
            if not toks or not all(callout.match(t) for t in toks):
                continue
            page = f.baseline[0].page
            pics = [x for x in rasters if x.page == page] + _vector_pictures(A, page)
            if pics and all(any(on(l, im, 60, 20) for im in pics) for l in f.baseline):
                fs.remove(f)
    for u, fs in results:
        for f in fs:
            # missing prod text - or prod text the diff paired with unrelated stage text ("Alignment arrow" ->
            # "NOTE:", the label of the note box under the stage picture): a picture label either way
            changed = "changed text" in (f.types or []) and "missing text" not in (f.types or [])
            if f.check != "content" or not ("missing text" in (f.types or []) or changed) or not f.baseline:
                continue
            n = len(_tokens(f.detail.get("baseline_text")))
            if n < 1:
                continue
            # a list bullet the diff glued onto the block ("… -20-60°C •"): not a label, and not on the picture
            def glyph_only(l):
                ws = [w for w in A.words if w.page == l.page and w.bbox[0] >= l.bbox[0] - 1 and w.bbox[2] <= l.bbox[2] + 1
                      and w.bbox[1] >= l.bbox[1] - 1 and w.bbox[3] <= l.bbox[3] + 1]
                return bool(ws) and all(not any(c.isalnum() for c in w.text) for w in ws)
            marks = [l for l in f.baseline if glyph_only(l)]
            if marks and len(marks) < len(f.baseline):
                f.baseline = [l for l in f.baseline if l not in marks]
            page = f.baseline[0].page
            # small drawings too: a package-contents remote control is 16 pt wide, a power adapter 39 pt tall
            raster_here, drawn_here = [x for x in rasters if x.page == page], _vector_pictures(A, page, 14, 14)
            # a figure's panel / callout number ("1", "2", "a)") beside one of its pictures is a label as well
            # (a panel drawn with straight lines only - a monitor corner - counts as a picture here)
            def panels(pg_no):
                try:
                    return [Image(pg_no, tuple(r)) for r in pymupdf.open(A.path)[pg_no].cluster_drawings()
                            if r.width >= 40 and r.height >= 40]
                except Exception:
                    return []
            marker = n == 1 and re.fullmatch(r"\(?[0-9]{1,2}[.)]?\)?|\(?[a-zA-Z][.)]\)?|\(?[a-z]\)?",
                                             (f.detail.get("baseline_text") or "").strip())
            if n == 1 and not any(all(on(l, im, 4, 4) for l in f.baseline) for im in raster_here + drawn_here) and \
                    not (marker and any(all(on(l, im, 30, 30) for l in f.baseline)
                                    for im in raster_here + drawn_here + panels(page))):
                continue  # a lone missing word is a label only when it sits on the picture itself

            # words, not separators: "Computer / gaming console / AV device" is a 5-word label
            short = all(sum(any(c.isalnum() for c in w) for w in t.split()) <= 6 for t in _label_lines(A, f.baseline))
            # on the picture; a drawn illustration's short labels may also sit right beside it (a dimension)
            pic = next((im for im in raster_here + drawn_here if all(on(l, im, 4, 4) for l in f.baseline)), None) or \
                (next((im for im in raster_here + drawn_here + panels(page) if all(on(l, im, 30, 30) for l in f.baseline)),
                      None) if marker else None) or \
                (next((im for im in drawn_here if all(on(l, im, 70, 20) for l in f.baseline)), None) if short else None)
            is_caption = pic is None and n <= 12 and (any(caption(l, im) for l in f.baseline for im in raster_here) or
                                                      (short and any(caption(l, im, 40) for l in f.baseline for im in drawn_here)))
            if pic is None and not is_caption and short:
                # last resort: short labels starting right beside a drawing, however long they run
                pic = next((im for im in drawn_here if all(beside(l, im) or on(l, im, 70, 20) for l in f.baseline)), None)
            if pic is None and not is_caption:
                # labels spread over several parts of one figure (a screen picture, a menu panel, a callout
                # bubble drawn beside them): each line on one of the page's pictures -> the figure they make up
                hosts = [next((im for im in raster_here + drawn_here if on(l, im, 4, 4)), None) or
                         (next((im for im in drawn_here if on(l, im, 70, 20) or beside(l, im)), None) if short else None) or
                         # a short caption right under / over its drawing (“Power adapter” under the adapter)
                         (next((im for im in raster_here + drawn_here if caption(l, im, 40)), None) if short else None)
                         for l in f.baseline]  # a callout number may sit just above / below its drawing
                got = [h for h in hosts if h]
                # nearly all on pictures: a stray list number read with the callouts ("… 10 3 2 1 7.") does
                # not turn a set of picture labels back into missing text
                if len(got) >= max(2, 0.8 * len(hosts)) and (len({id(h) for h in got}) > 1 or len(got) < len(hosts)):
                    pic = Image(page, (min(h.bbox[0] for h in got), min(h.bbox[1] for h in got),
                                       max(h.bbox[2] for h in got), max(h.bbox[3] for h in got)))
                else:
                    continue
            if pic is None and short:  # a short label right above / below a picture (often with a leader line): that picture
                pic = next((im for im in raster_here if any(caption(l, im) for l in f.baseline)), None) or \
                    next((im for im in drawn_here if any(caption(l, im, 40) for l in f.baseline)), None)
            if changed:
                # only a short label, and only when the stage words are not a rewording of it
                if n > 6 or set(_tokens(f.detail.get("baseline_text"))) & set(_tokens(f.detail.get("candidate_text"))):
                    continue
                f.types = [t for t in f.types if t != "changed text"] + ["missing text"]
            # a picture's label: an image issue, not "missing text / data missing"
            f.types = ["missing image label"] + [t for t in f.types if t not in ("missing image label", "missing text")]
            y = stage_copy(pic, f.candidate_at, u) if pic is not None else None
            if y is None:
                f.message = "Image label / caption missing in stage: " + f.message
                # show stage's picture at that spot, even when it is not the same picture: the screenshots
                # then put the two pictures side by side (prod with its labels, stage without)
                near = f.candidate_at
                if near is None and pic is not None and u.b_range[1] > u.b_range[0]:
                    # no insertion point found (the label's text moved elsewhere, or is simply gone): fall
                    # back to the section's own stage pages, so a shown picture still gets the disclaimer
                    # below, instead of a different stage page passing by without it
                    near = Loc(B.words[u.b_range[0]].page, (0, 0, 0, 0))
                if near is not None and pic is not None:
                    cands = [y2 for y2 in stage_pics if abs(y2.page - near.page) <= 1] + \
                        [y2 for p in (near.page - 1, near.page, near.page + 1) if 0 <= p < len(B.pages)
                         for y2 in _vector_pictures(B, p)]
                    if cands:
                        y2 = min(cands, key=lambda y2: (y2.page != near.page, abs((y2.bbox[1] + y2.bbox[3]) / 2 - near.bbox[1])))
                        f.candidate, f.candidate_at = [Loc(y2.page, y2.bbox)], None
                        f.message += f" (stage picture p.{y2.page + 1} shown; it is not the same picture)"
                        f.detail = {**f.detail, "stage_picture": list(y2.bbox), "stage_picture_page": y2.page}
                continue
            labels = ", ".join(f"“{snippet_text(l)}”" for l in _label_lines(A, f.baseline))
            at = Loc(y.page, y.bbox)
            # the stage picture is also meaningfully smaller: tiny labels may be there but too small to
            # read back reliably - the real issue is the size, not that the labels vanished
            rel_a = (pic.bbox[2] - pic.bbox[0]) / max(A.right(page) - A.left(page), 1e-6)
            rel_b = (y.bbox[2] - y.bbox[0]) / max(B.right(y.page) - B.left(y.page), 1e-6)
            smaller = rel_a > 0 and rel_b <= rel_a * cfg["assets"].get("smaller_report_ratio", 0.6)
            drawn = [_drawn(A, l.bbox, l.page, B, at, gthr, gedge, hays, lo=0.5) for l in f.baseline]
            f.detail = {**f.detail, "stage_picture": list(y.bbox), "stage_picture_page": y.page}
            # tiny print on a screenshot (an IP address, a device name at 2-4 pt) cannot be read back reliably:
            # when the picture's other labels are drawn into the stage picture, it is there too
            tiny = cfg["assets"].get("tiny_label_pt", 5.0)
            size_of = lambda l: max((ln.size for ln in A.lines if ln.page == l.page and ln.bbox[1] < l.bbox[3]
                                     and ln.bbox[3] > l.bbox[1] and ln.bbox[0] < l.bbox[2] and ln.bbox[2] > l.bbox[0]), default=99)
            # a one- or two-character callout ("7", "10") also matches strokes of the drawing: when most of the
            # labels are not in the stage picture, such short "finds" are chance, not the label
            short_txt = lambda l: len(re.sub(r"\W", "", " ".join(w.text for w in A.words if w.page == l.page
                                                                   and _in(w, pymupdf.Rect(l.bbox))))) <= 2
            if sum(map(bool, drawn)) < 0.5 * len(drawn):
                drawn = [None if short_txt(l) else d for l, d in zip(f.baseline, drawn)]
            if any(drawn) and not all(drawn):
                drawn = [d or (at.bbox if size_of(l) < tiny else None) for l, d in zip(f.baseline, drawn)]
            if drawn and all(drawn):  # the labels are drawn into the stage picture: present, not live text
                f.check, f.critical, f.types = "layout", False, ["text as graphic"]
                f.message = (f"Picture labels drawn into the stage picture, not live text: {labels} "
                             f"(prod p.{page + 1} ↔ stage p.{y.page + 1})")
                f.candidate, f.candidate_at = [Loc(y.page, r) for r in drawn], None
                f.detail["credited"] = True
                _count_present(u, n)
                continue
            # some labels drawn into the stage picture, some not: name only the missing ones
            names = lambda ok: ", ".join(dict.fromkeys(f"“{snippet_text(t)}”" for t in
                                                       _label_lines(A, [l for l, d in zip(f.baseline, drawn) if bool(d) == ok])))
            missing, present = names(False), names(True)
            f.message = (f"Image labels missing in stage: {missing or labels} on the prod picture (p.{page + 1}) "
                         f"are not on the stage picture (p.{y.page + 1})"
                         + (f"; drawn into the stage picture: {present}" if present else "")
                         + (f"; the stage picture is also smaller ({rel_b / max(rel_a, 1e-6):.0%} of the prod "
                            f"picture's width) - likely why its small print cannot be read back" if smaller else ""))
            f.candidate, f.candidate_at = [at], None
            f.detail["labels_missing"], f.detail["labels_in_picture"] = missing, present
            if smaller:
                f.types = list(dict.fromkeys((f.types or []) + ["image smaller"]))


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
            if f.detail.get("kind") == "labels":
                continue  # labels inside prod's picture itself (read by OCR): already worded, nothing live to place
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
                    # a one-character label (a callout number “7”) cannot be confirmed by reading the picture:
                    # the same digit is in any spec text or icon drawn there (“7.5W”) - it stays missing
                    if len(re.sub(r"\W", "", t)) < 2:
                        continue
                    for pg, box in (pics if ocr.available() else []):
                        hit = ocr.locate(t, B.path, pg, box)
                        if hit:
                            found.append(t)
                            boxes += [(pg, h) for h in hit]
                            break
                missing = [t for t in labels if t not in found]
                size = lambda t: max((ln.size for ln in A.lines if ln.page == pa - 1 and t in ln.text), default=99)
                if found and missing:
                    # tiny print on a screenshot (an IP address, a device name at 2-4 pt) is beyond OCR: when the
                    # picture's other labels are drawn into the stage picture, it is there too
                    tiny = cfg["assets"].get("tiny_label_pt", 5.0)
                    small = [t for t in missing if size(t) < tiny]
                    missing = [t for t in missing if t not in small]
                    found += small
                if missing and not found and not other_pic and \
                        all(size(t) < cfg["assets"].get("small_label_pt", 7.0) for t in missing):
                    # every label is small print (icon captions "0-40°C", "10-90%") and stage shows the same picture
                    # as a bitmap too coarse to read back: the labels are part of that picture, not missing text
                    found, missing = list(missing), []
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
            f.detail["credited"] = True
            _count_present(u, len(_tokens(f.detail.get("baseline_text"))))


def _drop_picture_text(results, A: Doc, B: Doc, cfg: dict) -> None:
    """[genuine] report_image_labels = false: a text difference in a picture's own labels - on the picture,
    right beside it or a short label just above / below it (“2 seconds” → “2 second” over the remote
    control) - is not reported, whatever kind of difference it is (changed, missing, extra, spacing)."""
    acfg = cfg["assets"]
    pics: dict = {}

    def pictures(doc: Doc, page: int) -> list:
        if (id(doc), page) not in pics:
            pics[(id(doc), page)] = [x for x in doc.images if x.page == page and not icon_max(doc, x, acfg)] \
                + _vector_pictures(doc, page, min_w=20)  # a tall, narrow drawing too: the remote control
        return pics[(id(doc), page)]

    def short(doc: Doc, l) -> bool:
        # the whole line the spot is on: “(!)” in “Press and hold the power button (!) on the projector …”
        # is body text, even with the power-key icon beside it
        cy = (l.bbox[1] + l.bbox[3]) / 2
        ln = next((x for x in doc.lines if x.page == l.page and x.bbox[1] <= cy <= x.bbox[3]
                   and x.bbox[0] <= l.bbox[2] and x.bbox[2] >= l.bbox[0]), None)
        text = ln.text if ln else " ".join(w.text for w in doc.words if w.page == l.page and _in(w, pymupdf.Rect(l.bbox)))
        return sum(any(c.isalnum() for c in w) for w in text.split()) <= 6

    def label(doc: Doc, l) -> bool:
        x0, y0, x1, y1 = l.bbox
        for im in pictures(doc, l.page):
            a0, b0, a1, b1 = im.bbox
            if x0 >= a0 - 4 and x1 <= a1 + 4 and y0 >= b0 - 4 and y1 <= b1 + 4:
                return True  # on the picture
            if y0 >= b0 - 20 and y1 <= b1 + 20 and (-4 <= x0 - a1 <= 20 or -4 <= a0 - x1 <= 20):
                return True  # right beside it (a callout at the end of a leader line)
            # just above / below it, about as wide as the picture: a label, not a line of body text
            # (“2 seconds” over the narrow remote-control drawing is a little wider than the drawing)
            if x0 < a1 and x1 > a0 and x1 - x0 <= max(1.5 * (a1 - a0), a1 - a0 + 40) \
                    and (-8 <= b0 - y1 <= 32 or -8 <= y0 - b1 <= 32):
                return True
        return False

    def on_picture(f: Finding) -> bool:
        sides = [(A, f.baseline), (B, f.candidate)]
        return any(ls for _, ls in sides) and all(short(d, l) and label(d, l) for d, ls in sides for l in ls)

    for u, findings in results:
        labels = [f for f in findings if f.check == "content" and on_picture(f)]
        if not labels:
            continue
        findings[:] = [f for f in findings if not any(f is x for x in labels)]
        c = u.content
        if not c:
            continue
        # the label's words do not count against the section's content match either
        for f in labels:
            na = len((f.detail.get("baseline_text") or "").split())
            nb = len((f.detail.get("candidate_text") or "").split())
            c["matched_words"] = min(c.get("matched_words", 0) + na, c.get("baseline_words", 0))
            c["missing_words"] = max(0, c.get("missing_words", 0) - na)
            c["extra_words"] = max(0, c.get("extra_words", 0) - nb)
        denom = c.get("baseline_words", 0) + c.get("extra_words", 0)
        good = c.get("matched_words", 0) - c.get("spacing_issues", 0) - c.get("script_issues", 0)
        c["match_pct"] = round(100.0 * max(good, 0) / denom, 2) if denom else 100.0


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
                # reported once, as "content in the wrong section" of prod's section: not extra text here too
                credit_dropped(eu, ef)
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


_XREF_SKELETON = re.compile(r"\(?\b(see|refer|to|on|page|pages|and)\b|\d+|[\W_]+", re.I)


def _xref_format(results, A: Doc, B: Doc) -> None:
    """How a cross-reference is printed is the template's: prod writes "(See page 15)", stage writes
    ("Audio-only mode" on page 13) - the target's title, in quotes or not. A text difference whose stage words
    are the text of an internal link, where prod's side holds only the reference's skeleton ("See page 15") or
    link text too, is that rendering - not a data mismatch (where the link leads is the links check's)."""
    def words_at(doc, locs_):
        out = []
        for l in locs_ or []:
            for w in doc.words:
                if w.page == l.page and l.bbox[0] - 1 <= (w.bbox[0] + w.bbox[2]) / 2 <= l.bbox[2] + 1 \
                        and l.bbox[1] - 1 <= (w.bbox[1] + w.bbox[3]) / 2 <= l.bbox[3] + 1:
                    out.append(w)
        return out
    internal = lambda doc, w: (ln := _link_at(doc, w)) is not None and ln.get("kind") in _GOTO
    for u, findings in results:
        keep = []
        for f in findings:
            types = set(f.types or [])
            if f.check != "content" or not types & {"changed text", "extra text"} or types - {"changed text", "extra text", "punctuation"}:
                keep.append(f)
                continue
            b_words = [w for w in words_at(B, f.candidate) if any(c.isalnum() for c in w.text)]
            a_text = f.detail.get("baseline_text") or ""
            a_words = [w for w in words_at(A, f.baseline) if any(c.isalnum() for c in w.text)]
            skeleton = not _XREF_SKELETON.sub("", a_text)
            if b_words and all(internal(B, w) for w in b_words) and \
                    (skeleton or (a_words and all(internal(A, w) for w in a_words))):
                credit_dropped(u, f)
                continue
            keep.append(f)
        findings[:] = keep


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
            if ua is not None and ub is not None and ua is not ub and not _names_target(text, ub.title, ua.title) \
                    and not _lands_at(B, lb, ua.title):
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
                    and not (ua is not None and ub is not None and ua is not ub) and not _lands_under(B, lb, da):
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
    out += _page_zero(u, al)
    return out


def _page_zero(u: Unit, al: Aligner) -> list[Finding]:
    """“… on page 0”: a cross-reference whose page number could not be resolved when stage was published
    (the target is missing from the output). The extra “on page N” itself is the template's format and is
    not an issue; page 0 is - it points nowhere. One finding per place, prod's same sentence beside it."""
    A, B = u.a, u.b
    out = []
    for j in range(u.b_range[0] + 1, u.b_range[1]):
        if B.words[j].text.rstrip(".,;:)") != "0" or B.words[j - 1].text.lower() != "page":
            continue
        k0 = j - 2 if j - 2 >= u.b_range[0] and B.words[j - 2].text.lower() == "on" else j - 1
        # the cross-reference itself: the linked words right before “on page 0” (its title)
        # (stage's broken xref is often plain text: a word belongs to it when it is linked in stage, or its
        # paired prod word is linked in prod)
        linked = lambda k: _link_at(B, B.words[k]) is not None or (k in al.b2a and _link_at(A, A.words[al.b2a[k]]) is not None)
        start = k0
        while start - 1 >= max(u.b_range[0], k0 - 20) and linked(start - 1):
            start -= 1
        idx_b = list(range(start, j + 1))
        # prod: the same title (its paired words), else the paired word before it
        ia_s = [al.b2a[k] for k in range(start, k0) if k in al.b2a]
        if not ia_s:
            prv = next((k for k in range(start - 1, max(u.b_range[0], start - 15) - 1, -1) if k in al.b2a), None)
            ia_s = [al.b2a[prv]] if prv is not None else []
        prod = locs(A, ia_s) if ia_s else []
        text = " ".join(B.words[k].text for k in idx_b)
        out.append(Finding(
            "integrity", "error",
            f"Page reference “on page 0” in stage: “{text}” - the cross-reference's page number was not "
            f"resolved (its target is missing from the published output) (stage p.{B.words[j].page + 1})",
            prod, locs(B, idx_b), {"kind": "page-zero", "candidate_text": text},
            types=["page zero"]))
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


def _lands_at(doc: Doc, ln: dict, heading: str, reach: float = 150) -> bool:
    """The link lands right at `heading` on its page - a little above it (AEM points at the top of the page /
    topic, the heading “Choosing a location” starts 44 pt lower: the landing point itself still belongs to the
    parent section) or a little below it. The reader arrives at that heading: the right section."""
    page, to = ln.get("page", -1), ln.get("to")
    if not heading or to is None or not 0 <= page < len(doc.pages):
        return False
    pg = pymupdf.open(doc.path)[page]
    needle = " ".join(heading.split()[:6])
    return any(abs(r.y0 - to.y) <= reach for r in pg.search_for(needle))


def _lands_under(doc: Doc, ln: dict, heading: str, reach: float = 150) -> bool:
    """The link lands a little below `heading` on its page (AEM points at the topic's first paragraph,
    InDesign at its heading): same place for the reader, the heading is at the top of what they see."""
    page, to = ln.get("page", -1), ln.get("to")
    if not heading or to is None or not 0 <= page < len(doc.pages):
        return False
    pg = pymupdf.open(doc.path)[page]
    needle = " ".join(heading.split()[:6])
    return any(r.y0 <= to.y + 4 and to.y - r.y0 <= reach for r in pg.search_for(needle))


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
    "image changed": ("Image differs", "The picture at this spot differs from prod's: another version of it (re-captured, "
                                       "re-cropped, without prod's highlight marks) or another picture."),
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
    "heading text": ("Section heading differs", "The section is in stage under a heading with other words or another number (e.g. Appendix 3 → Appendix 4)."),
    "outline level": ("Heading level differs", "The heading sits at a different level of the outline in stage (e.g. H3 → H2)."),
    "missing entry": ("TOC entry missing", "A chapter listed in the prod table of contents is not listed in stage."),
    "extra entry": ("Extra TOC entry", "The stage table of contents lists a chapter that prod does not."),
    "title differs": ("TOC title differs", "A chapter is listed with different words in the stage table of contents."),
    "wrong page": ("TOC page number wrong", "The stage table of contents points to a page where the chapter does not start."),
    "level differs": ("TOC level differs", "A chapter is listed at a different level in the stage table of contents."),
    "heading differs": ("TOC heading differs", "The heading of the table of contents differs."),
    "extra image": ("Extra image", "Stage shows a picture that prod does not have here."),
    "image distorted": ("Image distorted", "The picture is stretched or squashed in stage."),
    "image smaller": ("Image smaller in stage", "The stage picture is noticeably smaller than prod's, so fine print on it may not be legible."),
    "image bigger": ("Image bigger in stage", "The stage picture is noticeably bigger than prod's."),
    "image mirrored": ("Image mirrored in stage", "The picture is flipped left-right or top-bottom in stage: the same shapes, but the wrong way round."),
    "image vertical alignment": ("Image not aligned with its text", "The icon / picture beside a paragraph sits at another height against that text than in prod (centred on it vs at its first line)."),
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
    "icon missing inline": ("Icon missing in text", "An icon set inside a sentence in prod is not shown in stage."),
    "icon differs": ("Icon differs", "Stage shows another icon than prod at the same place in the sentence."),
    "icon pixelated": ("Icon pixelated", "Stage's icon is a low-resolution bitmap where prod's icon is sharp."),
    "caption row": ("Picture captions not aligned", "Captions of one row of pictures stand level in prod, not in stage."),
    "cell border": ("Table cell border missing", "A line between two cells of the prod table is not drawn in stage."),
    "table split": ("Table split", "One prod table is broken into several tables in stage."),
    "cells merged": ("Table cells merged", "Rows have fewer cells in stage than in prod."),
    "cell differs": ("Table cell differs", "A table cell holds other data in stage than in prod (a value or check mark missing, added or changed)."),
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
    "callout type differs": ("Callout type differs", "Stage labels the note as another type than prod's icon shows (e.g. IMPORTANT where prod has the warning icon)."),
    "repeated header": ("Repeated table header", "A table header is repeated at a page break on one side only."),
    "continued header": ("“(continued)” header", "A “(continued)” heading is repeated at a page break on one side only."),
    "raster vs vector": ("Artwork as picture vs drawing", "The same artwork is an embedded picture on one side and drawn as vectors on the other."),
    "image combined": ("Pictures combined", "A prod picture is shown as part of one larger picture in stage."),
    "text in image": ("Text inside a picture", "The prod text is drawn inside a stage picture (read by OCR), not as live text."),
    "table border added": ("Table border added", "A stage table has a border or rule that the prod table does not."),
    "label joined": ("Bold label joined with its text", "A bold label on its own line in prod runs into its plain text on one line in stage."),
    "image blurred": ("Image blurred", "The picture is noticeably softer / less sharp in stage than in prod."),
    "image order": ("Image sequence differs", "The pictures appear in a different order in stage than in prod."),
    "footer": ("Footer differs", "The page footer (page number, its place and style, or the text beside it such as a chapter "
                                 "name) is not the same in stage as in prod."),
    "header": ("Header differs", "The running header at the top of the pages is not the same in stage as in prod."),
    "bracket spacing": ("Number inside ( ) spaced differently",
                        "The number or icon between round brackets - a step number such as (❶) - is not centred, sits with "
                        "a different space to the brackets, or is missing in stage."),
    "image alignment": ("Image alignment differs", "The picture sits differently in the text column (left / centred / right / full width) in stage than in prod."),
    "page zero": ("Page reference “on page 0”", "A cross-reference in stage says “on page 0”: its target page was not resolved."),
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
    "icon missing": ("Icon missing in table", "A table row shows fewer icons in stage than in prod."),
    "icon extra": ("Extra icon in table", "A table row shows more icons in stage than in prod."),
    "icon order": ("Icons in another order", "The icons of a table row are in another order in stage."),
    "icon above text": ("Icon not beside its text", "Stage sets the row's icon above its text in one cell; prod sets it in a column of its own, beside the text."),
    "callout style": ("Note style differs", "The same kind of note is drawn differently in stage than in prod "
                                            "(e.g. a bar on the left in prod, a filled box in stage)."),
    "spec callout background": ("Callout colour off the design spec", "A callout is not in its type's background colour, or is plain text instead of a callout."),
    "spec callout icon": ("Callout icon off the design spec", "A callout has no icon, another type's icon, or an icon of another size."),
    "spec callout content": ("Callout content not allowed", "A callout holds a picture or a second callout: it takes paragraphs only."),
    "spec table header": ("Table header bar colour off the design spec", "The table's header bar is not in the design spec's colour."),
    "spec table border": ("Table border colour off the design spec", "The table's border is not in the design spec's colour."),
    "spec list numbering": ("List numbering off the design spec", "An ordered list level is not numbered 1, 2, 3 → a, b, c → I, II, III."),
    "spec bullet": ("Bullet off the design spec", "An unordered list does not use the black circle bullet."),
    "spec pagination": ("Page break off the design spec", "A heading at a page bottom, a callout split over pages, or a table header alone / not repeated."),
    "spec page structure": ("Print page structure off the design spec", "The Print version has a cover, Q&A index or TOC, or the first page header is missing or repeated."),
    "table header alignment": ("Table header not centred", "A table header cell's text is not centred in its column (design spec: header text centred)."),
    "row background": ("Table row background differs", "Rows that one side shades (group rows between the data rows) are plain on the other."),
    "spec text-align": ("Text not left-aligned", "Body text is centred or right-aligned; the design spec left-aligns all content."),
    "size / aspect": ("Image size differs", "The picture is shown at another width or aspect ratio in stage."),
    "emphasis": ("Font weight / italic differs", "The same words are set in another font weight (bold, medium, light ...) or italic on one side only: the emphasis the reader relies on changed."),
    "list level": ("List level differs", "A paragraph that sits under a list item's text in prod (part of that bullet or numbered item) starts under another item's text, or as body text, in stage: the list's marker / text columns are not kept."),
    "stacked to columns": ("Layout broken: stacked text in columns", "Text that prod stacks (a list item with its description underneath) is laid out side by side in columns in stage."),
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
    # bullet indent / gap / list level is layout (CSS), not a missing or changed marker
    if not everything and "indent" in types:
        gt = [t for t in gt if t != "bullet"]
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
        f["description"] = concise(f["message"])


# issue colour (UI, PDF reports, screenshot boxes): red = what the reader sees is wrong or missing,
# blue = how it is laid out. Everything else keeps its category colour.
RED, BLUE = "#dc2626", "#2563eb"
_RED_TYPES = {"missing image", "broken image", "image changed", "image blacked out", "missing image label",
              "size / aspect", "image distorted", "placement", "image outside box", "image mirrored",   # image size / alignment
              "image pixelated", "row order", "extra link", "missing link", "link to wrong section",
              "image blurred", "image alignment", "label joined", "link lands elsewhere", "link page number", "link quotes", "page zero",
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


# Issues whose message says in one sentence what prod has and what stage has: the two halves, for the report's
# Prod / Stage lines. (pattern on the message, prod text, stage text - \1.. are the pattern's groups)
_PROD_STAGE = [
    (r"Picture text missing in stage: (.+?) \(prod p\.\d+", r"On the picture: \1", r"Not on the stage picture: \1"),
    (r"red highlight marks missing", "Red highlight marks drawn on the picture", "The same picture without the red marks"),
    (r"red highlight marks added", "The picture without red marks", "Red highlight marks drawn on the same picture"),
    (r"Image labels? missing in stage: (.+?) on the prod picture", r"\1 on the picture", r"Not on the stage picture: \1"),
    (r"Not aligned in stage(?: \(\d+ places\))?: (.+?) are side by side on one row in prod \(p\.\d+\); in stage \(p\.\d+\) ([^;]+)",
     r"\1 side by side on one row", r"\2"),
    (r"Picture captions not level in stage: (.+?) stand in one row in prod; in stage (.+?)(?: of| \(|$)",
     r"\1 in one row", r"\2"),
    (r"Link lands on a different place(?: \(\d+ places\))?: (“.+?”) goes to (“.+?”) in prod(?: \(p\.\d+\))? but to (“.+?”) in stage",
     r"\1 goes to \2", r"\1 goes to \3"),
    (r"Link points to the wrong section: (“.+?”) goes to (“.+?”) in prod but to (“.+?”) in stage",
     r"\1 goes to \2", r"\1 goes to \3"),
    (r"Tables merged in stage: (\d+) prod tables are one table in stage", r"\1 separate tables", "One table"),
    (r"Table split in stage: prod table .*? is (\d+) tables in stage", "One table", r"\1 tables"),
    (r"Table header not centred in stage \(\d+ cells?\): (.+?)(?: —|$)", "Header text centred in its column", r"\1"),
    (r"Bold label joined with its text in stage: (“.+?”) is on its own line in prod with (“.+?”) on the next line",
     r"\1 on its own line, \2 on the next line", r"\1 and \2 run into one line"),
    (r"Page reference “on page 0” in stage", "A page number in the cross-reference", "“on page 0”"),
    (r"Extra icon in a table row in stage: .*?\(e\.g\. (“.+?”)\)", r"Row \1 without that icon", r"Row \1 with an extra icon"),
    (r"Icon missing in a table row in stage: .*?\(e\.g\. (“.+?”)\)", r"Row \1 with its icon", r"Row \1 without the icon"),
    (r"Table cell border missing in stage: \d+ line\(s\).*? - (between .+?)(?:;|$)", r"A border line \1", "No border line there"),
    (r"Image (bigger|smaller) in stage by (\d+%).*?: ([\d.]+×[\d.]+ pt) → ([\d.]+×[\d.]+ pt)", r"The picture at \3", r"The same picture at \4 (\2 \1)"),
    (r"Image size differs.*?the same picture drawn (\d+%) (smaller|larger) in stage.*?: ([\d.]+×[\d.]+ pt) → ([\d.]+×[\d.]+ pt)",
     r"The picture at \3", r"The same picture at \4 (\1 \2)"),
    (r"Callout type differs: stage labels the note (“.+?”), prod's note has the (\w+) icon \(a (.+?)\)", r"A \3 note (\2 icon)", r"Labelled \1"),
    (r"Content duplicated in stage: (“.+?”) is in stage (.+?) but (.+?) in prod", r"\1 \3", r"\1 \2"),
    (r"Table cell differs in stage \(\d+ cell\(s\)\): (row .+?): (.+?) → ([^;]+)", r"\1: \2", r"\1: \3"),
]


def prod_stage(f: dict) -> tuple[str, str] | None:
    """The issue as two statements - what prod has, what stage has - and nothing else (no explanation, no page
    numbers: the report's heading has them). None when the issue cannot be put that way: the report then shows
    its one-line description."""
    m = (f.get("message") or "").split("  ·  ")[0].split("\n")[0]
    for pat, prod, stage in _PROD_STAGE:
        hit = re.search(pat, m)
        if hit:
            return hit.expand(prod).strip(), hit.expand(stage).strip()
    try:
        e, a = expected_actual(f)
    except Exception:
        return None
    tail = lambda t: re.sub(r"\s*\((?:prod|stage) p\.\d+(?:[–-]\d+)?\)(?=:|\s*$)", "", (t or "").strip())
    e, a = tail(e), tail(a)
    if not e or not a or e.lower().startswith("as in prod") or len(a) > 400 and a.startswith(m[:40]):
        return None
    return e, a


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


_KEEP_PAREN = re.compile(r"\d|[“”\"‘’]|\bp\.|↔|→|%|pt\b|mm\b|px\b|places?\b|level\b|items?\b|cells?\b|rows?\b|words?\b")


def concise(msg: str) -> str:
    """The issue as the report states it: what is wrong and where, without the explanations around it.
      - the picture sizes (" — prod: … · stage: …") become their own Prod: / Stage: lines (of the first place)
      - explanatory brackets (“(space between marker and text)”, “(re-captured or edited)”) are dropped;
        brackets with pages, numbers or counts stay, and quoted document text is never touched
      - an explanatory tail (“ - the cross-reference's page number was not resolved …”,
        “ — header text must be centred in its column”) and the style role tag (“[text-10pt] ”) are dropped
      - a phrase repeated before every place (“prod has a space stage does not — ”) is said once
      - a long list of examples keeps its first three"""
    parts = []
    for part in msg.split("  ·  "):
        lines = part.split("\n")
        head, rest = lines[0], lines[1:]
        # picture sizes of every place: the first one as Prod / Stage lines, the rest dropped
        tails = list(re.finditer(r"\s+—\s+(?:prod: ([^;]*?)(?:\s+·\s+stage: ([^;]*?))?|stage: ([^;]*?))(?=;|$)", head))
        extra = []
        if tails:
            t = tails[0]
            extra = ([f"Prod: {t.group(1)}"] if t.group(1) else []) + \
                    ([f"Stage: {t.group(2) or t.group(3)}"] if (t.group(2) or t.group(3)) else [])
            for t in reversed(tails):
                head = head[:t.start()] + head[t.end():]
        head = re.sub(r"^\[[\w .-]+\]\s+", "", head)  # style role tag
        # quoted document text is kept exactly: work on the text between the quotes only
        chunks = re.split(r"(“[^”]*”)", head)
        for n in range(0, len(chunks), 2):
            c, prev = chunks[n], None
            while prev != c:  # nested brackets: innermost first
                prev = c
                c = re.sub(r"\s*\(([^()“”]*)\)", lambda b: b.group(0) if _KEEP_PAREN.search(b.group(1))
                           or len(b.group(1).split()) < 2 else "", c)
            chunks[n] = c
        head = "".join(chunks)
        # an explanation after a dash at the end (no quotes, no numbers): " - the reason why …"
        head = re.sub(r"\s+[-—]\s+(?=[a-z])[^“”\"\d]*$", "", head)
        # the same phrase before every place: said once, after the count
        items = head.split("; ")
        pre = re.match(r"^(.*?:\s+)(.+? — )", items[0]) if len(items) > 1 else None
        if pre and all(x.startswith(pre.group(2)) for x in items[1:]):
            p2 = pre.group(2)
            head = pre.group(1).rstrip(": ") + " — " + p2[:-3] + ": " + "; ".join(
                [items[0][len(pre.group(0)):]] + [x[len(p2):] for x in items[1:]])
        # “(13 items, e.g. “a”; “b”; “c”; “d” …)” -> the first three examples
        head = re.sub(r"(e\.g\. (?:“[^”]*”; ){2}“[^”]*”)(?:; “[^”]*”)+", r"\1 …", head)
        parts.append("\n".join([head.strip()] + rest + extra))
    return "  ·  ".join(parts)
