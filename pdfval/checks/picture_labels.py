"""A picture's own text, prod against stage: its callout numbers (“1” “2” “a”), its labels (short text set right
on, above, below, left or right of it - not the paragraphs around it) and the leader lines that tie a label to
the picture. That text is left out of the content comparison (genuine.skip_picture_text); here it is compared
picture by picture, for the image report only.

A stage picture may carry the labels as text or drawn into the image: they count as there when stage's text
around the picture or the text read from the picture (OCR) has them. Leader lines are compared only when stage
sets its labels as text too (lines drawn into a bitmap cannot be counted)."""
from __future__ import annotations

import re
from collections import defaultdict

import pymupdf

from ..model import Doc, Finding, Image, Loc

# a callout: 1-2 digits or one letter, maybe with “.” or “)” - not a unit (“cm”) or a word
_NUMBER = re.compile(r"^\(?(\d{1,2}|[A-Za-z])[.)]?$")
_PLAIN: dict = {}
# a unit after a number makes it a dimension (“3 cm”, “45 °”, “100 mm”, “1.5 m”), not a callout number
_UNIT = re.compile(r"^(mm|cm|m|km|in|inch|inches|ft|\"|'|°|°c|°f|%|kg|g|lb|lbs|v|w|a|hz|khz|mhz|ghz|ms|s|px|pt|dpi)[.,)]?$", re.I)


def _ocr_plain(path: str, page: int, bbox: tuple) -> str:
    """The picture read as it is (no contrast stretch): a thin line drawing whose labels the shared OCR
    reading (contrast-stretched) loses - “3cm 3cm” on a pole-mount drawing."""
    import subprocess
    key = (path, page, tuple(round(v, 1) for v in bbox))
    if key not in _PLAIN:
        _PLAIN[key] = ""
        try:
            png = pymupdf.open(path)[page].get_pixmap(clip=pymupdf.Rect(bbox), dpi=300).tobytes("png")
            r = subprocess.run(["tesseract", "stdin", "stdout", "--psm", "11"], input=png, capture_output=True, timeout=120)
            _PLAIN[key] = r.stdout.decode("utf-8", "replace")
        except Exception:
            pass
    return _PLAIN[key]


def _key(t: str) -> str:
    return re.sub(r"[^0-9a-z]", "", t.lower())


def _pictures(doc: Doc, page: int, cfg: dict) -> list[Image]:
    from ..genuine import _vector_pictures
    from .assets import icon_max
    acfg = cfg.get("assets", {})
    from ..genuine import _line_drawings
    return [x for x in doc.images if x.page == page and not icon_max(doc, x, acfg)] + _vector_pictures(doc, page) + \
        _line_drawings(doc, page)


def _leader_lines(doc: Doc, pic: tuple, labels: list) -> int:
    """Thin straight lines with one end on the picture and the other end at one of its labels."""
    if not labels:
        return 0
    pdf = pymupdf.open(doc.path)
    page = pdf[labels[0].page]
    near = lambda pt: any(w.bbox[0] - 25 <= pt.x <= w.bbox[2] + 25 and w.bbox[1] - 25 <= pt.y <= w.bbox[3] + 25 for w in labels)
    inside = lambda pt: pic[0] - 3 <= pt.x <= pic[2] + 3 and pic[1] - 3 <= pt.y <= pic[3] + 3
    n = 0
    for d in page.get_drawings():
        for it in d["items"]:
            if it[0] != "l":
                continue
            a, b = it[1], it[2]
            if abs(a - b) < 8:
                continue
            if (near(a) and inside(b)) or (near(b) and inside(a)) or (near(a) and near(b) and (inside(a) or inside(b))):
                n += 1
    return n


def check(A: Doc, B: Doc, units: list, cfg: dict) -> dict:
    """{unit id: [Finding]} - one finding per prod picture whose numbers / labels / leader lines stage lacks."""
    from .. import ocr
    from .assets import visual_distance
    pcfg = cfg.get("picture_labels") or {}
    if not pcfg.get("enabled", True) or not getattr(A, "picture_text", None):
        return {}
    use_ocr = ocr.available() and pcfg.get("ocr", True)
    out: dict = defaultdict(list)
    used: set = set()
    for u in units:
        if u.a_range[0] >= u.a_range[1] or u.b_range[0] >= u.b_range[1]:
            continue
        a_pages = {A.words[i].page for i in range(*u.a_range)}
        b_pages = sorted({B.words[i].page for i in range(*u.b_range)})
        stage_pics = [y for p in b_pages for y in _pictures(B, p, cfg)]
        if not stage_pics:
            continue
        for (page, box), idx in A.picture_text.items():
            # each picture once: in the section that holds its labels (a page can end one section, start another)
            if page not in a_pages or not (u.a_range[0] <= idx[0] < u.a_range[1]):
                continue
            x = Image(page, box)
            cands = [(visual_distance(A, x, B, y), n, y) for n, y in enumerate(stage_pics) if (y.page, y.bbox) not in used]
            if not cands:
                continue
            dist, _, y = min(cands, key=lambda c: c[0])
            if dist > pcfg.get("max_distance", 0.42):
                continue  # no stage copy of this picture in the section (a missing picture is the image check's)
            used.add((y.page, y.bbox))
            # (only when prod's labels sit on the picture: then the look compared includes them. Labels outside it -
            # callouts with leader lines left of a screenshot - are not in that look: an identical stage screenshot
            # is one WITHOUT them)
            inside = all(box[0] - 2 <= A.words[k].bbox[0] and A.words[k].bbox[2] <= box[2] + 2
                         and box[1] - 2 <= A.words[k].bbox[1] and A.words[k].bbox[3] <= box[3] + 2 for k in idx)
            if inside and dist <= pcfg.get("same_picture_distance", 0.12) and not getattr(B, "picture_text", {}).get((y.page, y.bbox)):
                # the stage picture is the same artwork (near-identical look) with its text drawn in: the same
                # callouts and labels are there - OCR missing a thin "3" on a line drawing is not a missing callout
                continue
            # stage's labels of that picture: its own picture text, and what OCR reads in it
            s_idx = [k for (p, b), ks in getattr(B, "picture_text", {}).items() if p == y.page
                     and min(b[2], y.bbox[2]) > max(b[0], y.bbox[0]) and min(b[3], y.bbox[3]) > max(b[1], y.bbox[1]) for k in ks]
            s_text = " ".join(B.words[k].text for k in s_idx)
            seen = (ocr.image_text(B.path, y.page, y.bbox) + "\n" + _ocr_plain(B.path, y.page, y.bbox)) if use_ocr else ""
            # stage may set a label as ordinary text right around its picture (“(Represented as H for horizontal
            # position.)” beside the grid): there for the reader, so not missing
            around = " ".join(w.text for w in B.words if w.page == y.page
                              and y.bbox[0] - 150 <= (w.bbox[0] + w.bbox[2]) / 2 <= y.bbox[2] + 150
                              and y.bbox[1] - 40 <= (w.bbox[1] + w.bbox[3]) / 2 <= y.bbox[3] + 40)
            hay = s_text + " " + seen + " " + around
            # numbers: every digit run / letter run (OCR joins “3cm”); labels: the line without spaces in the text
            have = set(re.findall(r"\d+|[a-z]+", hay.lower()))
            flat = _key(hay)
            lines: dict = defaultdict(list)
            for k in idx:
                lines[A.words[k].line].append(k)
            numbers, labels = [], []
            for li, ks in lines.items():
                ws = [A.words[k] for k in ks]
                text = " ".join(w.text for w in ws)
                if all(_NUMBER.match(w.text) for w in ws):
                    # a list number beside the picture (“2.” with its item's text to its right) is the list's
                    row = [w for w in A.words if w.page == ws[0].page and w.line != li and w.norm
                           and min(w.bbox[3], ws[-1].bbox[3]) - max(w.bbox[1], ws[-1].bbox[1]) > 0.5 * (ws[-1].bbox[3] - ws[-1].bbox[1])
                           and 0 <= w.bbox[0] - ws[-1].bbox[2] <= 40]
                    if row:
                        continue
                    # a measurement, not a callout: the number with a unit right after it (“3” “cm”, “45” “°”)
                    unit = [w for w in A.words if w.page == ws[0].page and w.line != li
                            and _UNIT.match(w.text.strip())
                            and min(w.bbox[3], ws[-1].bbox[3]) - max(w.bbox[1], ws[-1].bbox[1]) > 0.4 * (ws[-1].bbox[3] - ws[-1].bbox[1])
                            and -1 <= w.bbox[0] - ws[-1].bbox[2] <= 12]
                    if unit:
                        continue
                    numbers += [w.text for w in ws if _key(w.text) not in have]
                elif not (_key(text) and (_key(text) in flat or ocr.found(text, hay))):
                    labels.append(text)
            lead_a = _leader_lines(A, box, [A.words[k] for k in idx])
            lead_b = _leader_lines(B, y.bbox, [B.words[k] for k in s_idx]) if s_idx else None
            lines_missing = lead_b is not None and lead_a >= 1 and lead_b < lead_a
            if not (numbers or labels or lines_missing):
                continue
            region = [min([box[0]] + [A.words[k].bbox[0] for k in idx]) - 6, min([box[1]] + [A.words[k].bbox[1] for k in idx]) - 6,
                      max([box[2]] + [A.words[k].bbox[2] for k in idx]) + 6, max([box[3]] + [A.words[k].bbox[3] for k in idx]) + 6]
            types = (["callout numbers missing"] if numbers else []) + (["image label missing"] if labels else []) + \
                (["leader lines missing"] if lines_missing else [])
            what = "; ".join(filter(None, [
                f"numbers {', '.join(dict.fromkeys(numbers))}" if numbers else "",
                f"labels {', '.join('“' + t + '”' for t in labels)}" if labels else "",
                f"leader lines {lead_a} → {lead_b}" if lines_missing else ""]))
            out[u.id].append(Finding(
                "assets", pcfg.get("severity", "warning"),
                f"Picture text missing in stage: {what} (prod p.{page + 1} ↔ stage p.{y.page + 1})",
                [Loc(page, tuple(region))], [Loc(y.page, y.bbox)],
                {"kind": "picture labels", "numbers": numbers, "labels": labels,
                 "leader_lines": [lead_a, lead_b], "ocr": bool(seen), "image_report_only": True}, types=types))
    return dict(out)
