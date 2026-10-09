"""Per-issue screenshots: a prod crop and a stage crop of the region a finding
points at, with the finding's boxes drawn on top.

Each page is rendered once (LRU cache) and every crop is cut from that bitmap,
so ~1-2k issues take seconds rather than minutes.
"""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Callable

import pymupdf
from ..genuine import concise
from PIL import Image, ImageDraw, ImageFont

COLORS = {"content": (220, 38, 38), "style": (124, 58, 237), "layout": (234, 88, 12),
          "assets": (13, 148, 136), "structure": (37, 99, 235),
          "tables": (2, 132, 199), "integrity": (185, 28, 28)}
try:
    _FONT = ImageFont.load_default(size=17)
except TypeError:  # Pillow < 10.1
    _FONT = ImageFont.load_default()
CONTEXT, MIN_LINES, MAX_LINES = 5, 16, 40  # crop: text lines above/below the issue, min/max lines shown
FOOTER_BAND = 0.12  # share of the page height at the top / bottom that is the running header / footer
ANCHOR_WORDS = 4  # words of text just above / below a picture, looked up on the other side to find the same picture
SAME_SHAPE = 0.2  # two pictures whose width:height differs by at most this share are the same picture, redrawn
TEXT_BOX = 0.1  # a drawn box whose area is at least this share words is a note box / table, not an illustration
SEVERITIES = {"errors": {"error"}, "warnings": {"error", "warning"}, "all": {"error", "warning", "info"}, "none": set(),
              "reports": set()}  # reports: only what the genuine-issues and image reports show (batch runs)


class _PageCache:
    def __init__(self, path: str, zoom: float, size: int = 8):
        self.doc, self.zoom, self.size = pymupdf.open(path), zoom, size
        self.cache: OrderedDict[int, Image.Image] = OrderedDict()
        self.pics: dict[int, list] = {}  # page -> picture boxes (see _pictures)
        self.words: dict[int, list] = {}  # page -> words (see _words)

    def get(self, pno: int) -> Image.Image:
        if pno in self.cache:
            self.cache.move_to_end(pno)
            return self.cache[pno]
        pix = self.doc[pno].get_pixmap(matrix=pymupdf.Matrix(self.zoom, self.zoom), alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        self.cache[pno] = img
        if len(self.cache) > self.size:
            self.cache.popitem(last=False)
        return img


def _strip(pc: _PageCache, page: int, y0: float, y1: float) -> tuple[Image.Image, int | None]:
    """The page from y0 to y1 (pt), full width; a window that runs past the page end continues with the
    top of the next page (a section that starts at the foot of a page), below a thin page-break line.
    Returns (image, pixel row where the next page starts, or None)."""
    src, z = pc.get(page), pc.zoom
    ph = src.height / z
    top = src.crop((0, int(max(0.0, y0) * z), src.width, int(min(y1, ph) * z)))
    if y1 <= ph + 1 or page + 1 >= len(pc.doc):
        return top, None
    nxt = pc.get(page + 1)
    rest = nxt.crop((0, 0, min(src.width, nxt.width), int(min(y1 - ph, nxt.height / z) * z)))
    gap = 26
    img = Image.new("RGB", (src.width, top.height + gap + rest.height), (255, 255, 255))
    img.paste(top, (0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle([0, top.height, img.width, top.height + gap - 1], fill=(226, 232, 240))
    d.text((10, top.height + 3), f"page break - continues on p.{page + 2}", fill=(71, 85, 105), font=_FONT)
    img.paste(rest, (0, top.height + gap))
    return img, top.height + gap


# set per finding by render(): an image issue is marked by one box around each picture it concerns,
# never by boxes on the text inside the picture
_PIC_MODE = False
IMAGE_KINDS = {"missing image", "image changed", "image combined", "size / aspect", "image alignment", "placement",
               "image outside box", "broken image", "image blacked out", "image distorted", "image pixelated",
               "missing image label", "label in picture", "extra image", "image order", "raster vs vector"}


def _to_pictures(pc: "_PageCache", page: int, boxes: list) -> list:
    """Each box inside (or mostly over) a picture of the page becomes that picture's box, once."""
    out = []
    for b in boxes:
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        hit = next((r for r in _pictures(pc, page) if r[0] - 2 <= cx <= r[2] + 2 and r[1] - 2 <= cy <= r[3] + 2), None)
        r = tuple(hit) if hit else tuple(b)
        if r not in out:
            out.append(r)
    return out


# (words other languages do not share: “in”, “is”, “of”, “for”, “on” are Dutch, Italian, Norwegian, French words too)
_EN_WORDS = frozenset("the and with this that your you are from when will which have should".split())
_ENGLISH: dict = {}


def _english(pc: "_PageCache", page: int) -> bool:
    """Is the page's text English? By its own words, page by page - a manual can hold a section per language.
    English: Latin letters, and the everyday English words (the, and, of, to ...) make up a fair share of them.
    A page with hardly any text counts as English unless it is in another script."""
    key = (id(pc), page)
    if key not in _ENGLISH:
        try:
            words = [w[4].lower().strip(".,;:!?()\"'“”") for w in pc.doc[page].get_text("words")]
        except Exception:
            words = []
        words = [w for w in words if w.isalpha()]
        latin = sum(1 for w in words if all(ord(ch) < 0x250 for ch in w))
        if words and latin < 0.7 * len(words):
            _ENGLISH[key] = False
        elif len(words) < 40:
            # little text (the last lines of a section): not English when not one English word is among them
            _ENGLISH[key] = len(words) < 8 or any(w in _EN_WORDS for w in words)
        else:
            _ENGLISH[key] = sum(1 for w in words if w in _EN_WORDS) >= 0.02 * len(words)
    return _ENGLISH[key]


_UNDERLINE_OVER = 4  # more text lines than this marked in one view: underlined, not boxed


def _crop(pc: _PageCache, page: int, boxes: list, color, note: str | None, window: tuple[float, float],
          next_boxes: list | None = None) -> Image.Image:
    """The page from window[0] to window[1] (pt), full width, with the finding's boxes drawn on it
    (and next_boxes on the next page, when the window continues there)."""
    if _PIC_MODE:
        boxes = _to_pictures(pc, page, boxes)
        next_boxes = _to_pictures(pc, page + 1, next_boxes or [])
    z = pc.zoom
    y0, y1 = window
    img, cont = _strip(pc, page, y0, y1)
    img = img.convert("RGBA")
    over = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    hidden = 0
    marks = [(b, (b[1] - 2 - y0) * z) for b in boxes]
    if cont is not None:
        marks += [(b, cont + (b[1] - 2) * z) for b in (next_boxes or [])]
    # many lines of text marked in one view (a whole paragraph in another weight): boxes on every line bury the
    # text under their outlines - the lines are underlined instead, the text stays as printed. A few places
    # (a word, a line or two) keep their box: easier to spot on a full page.
    # ... and always on a page that is not English: accents and non-Latin letters fill the line's height, so a box
    # drawn round them cuts through the text
    dense = sum(1 for b, _ in marks if b[3] - b[1] <= 30) > _UNDERLINE_OVER or not _english(pc, page)
    for b, top in marks:
        r = [(b[0] - 2) * z, top, (b[2] + 2) * z, top + (b[3] - b[1] + 4) * z]
        if r[3] < 0 or r[1] > img.height or (cont is not None and b in boxes and r[1] >= cont - 26):
            hidden += 1
            continue
        # a text line is tinted; a picture or block (taller than a few lines) is only outlined, so its
        # colours and detail are shown exactly as in the PDF
        if dense and b[3] - b[1] <= 30:
            y = top + (b[3] - b[1] + 3) * z
            d.line([(b[0] * z, y), (b[2] * z, y)], fill=color + (255,), width=3)
            continue
        tint = color + (38,) if b[3] - b[1] <= 30 else None
        d.rectangle(r, fill=tint, outline=color + (255,), width=3)
    img = Image.alpha_composite(img, over)
    if hidden:  # never drop a highlighted place silently
        note = f"{note}  ·  " if note else ""
        note += f"+ {hidden} more place(s) on this page outside this view"
    if note:
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, img.width, 30], fill=(30, 35, 48, 230))
        d.text((10, 6), note, fill=(255, 255, 255, 255), font=_FONT)
    return img.convert("RGB")


def _pictures(pc: "_PageCache", page: int) -> list:
    """Picture boxes on a page: embedded images and drawn illustrations (clusters of vector shapes).
    A drawn box full of text - a note box, a table - is not an illustration (its callout labels
    are a small share of a drawing's area)."""
    if page not in pc.pics:
        pg, out = pc.doc[page], []
        try:
            out += [tuple(i["bbox"]) for i in pg.get_image_info() if i["bbox"][3] - i["bbox"][1] >= 30]
            ws = _words(pc, page)
            for r in pg.cluster_drawings():
                if r.height >= 40 and r.width >= 40 and r.height < 0.8 * pg.rect.height:
                    text = sum((w[2] - w[0]) * (w[3] - w[1]) for w in ws
                               if w[0] >= r.x0 - 1 and w[2] <= r.x1 + 1 and w[1] >= r.y0 - 1 and w[3] <= r.y1 + 1)
                    if text < TEXT_BOX * r.width * r.height:
                        out.append(tuple(r))
        except Exception:
            pass
        pc.pics[page] = out
    return pc.pics[page]


def _words(pc: "_PageCache", page: int) -> list:
    """The page's words in reading order: (x0, y0, x1, y1, text), text lower-cased letters and digits
    only, so a hyphen, a quote style or punctuation does not stop the same words matching."""
    if page not in pc.words:
        out = []
        for w in pc.doc[page].get_text("words", sort=True):
            t = "".join(ch for ch in w[4].lower() if ch.isalnum())
            if t:
                out.append((w[0], w[1], w[2], w[3], t))
        pc.words[page] = out
    return pc.words[page]


def _find_words(words: list, seq: list, near: float) -> tuple | None:
    """(top, bottom) of the run of words spelling seq that is closest to y = near, or None."""
    k, best = len(seq), None
    for i in range(len(words) - k + 1):
        if [w[4] for w in words[i:i + k]] == seq:
            hit = (min(w[1] for w in words[i:i + k]), max(w[3] for w in words[i:i + k]))
            if best is None or abs(hit[0] - near) < abs(best[0] - near):
                best = hit
    return best


def _union(boxes: list) -> tuple:
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def _same_place(src: "_PageCache", sp: int, rng: list, boxes: list,
                dst: "_PageCache", dp: int, y: float, line: float) -> tuple | None:
    """The other side of a finding on or next to a picture, when that side has nothing to highlight.
    src page sp shows the y range rng (grown to the whole picture) with the finding's boxes; dst page dp
    has the aligned position y. The same picture on dst is the one between the same text: the words
    just above rng and just below it, looked up on dst - on the next page too, when dst breaks the
    page after the text above (the picture then heads the next page, and a crop of the page end
    would show the previous section instead). Returns (dst page, y range to show there, the
    finding's spot in that picture - when the boxes sit inside a bigger picture of the same shape -
    or None), or None when dst has no picture there (a picture that is simply missing: the
    text-aligned marker already shows where it belongs)."""
    if not any(p[1] <= rng[1] and p[3] >= rng[0] for p in _pictures(src, sp)):
        return None
    ws, theirs = _words(src, sp), _words(dst, dp)
    above = [w for w in ws if w[3] <= rng[0] + 2]
    below = [w for w in ws if w[1] >= rng[1] - 2]
    top = bottom = None
    for k in range(ANCHOR_WORDS, 1, -1):  # the longest run of words that is found (lines wrap differently)
        if top is None and len(above) >= k:
            hit = _find_words(theirs, [w[4] for w in above[-k:]], y)
            top = hit[1] if hit else None
        if bottom is None and len(below) >= k:
            hit = _find_words(theirs, [w[4] for w in below[:k]], y)
            bottom = hit[0] if hit else None
    if top is None and bottom is None:
        return None
    below_here = bottom is not None
    reach = 1.5 * (rng[1] - rng[0]) + 2 * line  # only one side's text found: about the picture's height from it
    top = bottom - reach if top is None else top
    bottom = top + reach if bottom is None else bottom
    if not (top < bottom and top <= y + 2 * line and bottom >= y - 2 * line):
        return None  # the words matched somewhere else on the page, not around the aligned position
    pics = [p for p in _pictures(dst, dp) if top < (p[1] + p[3]) / 2 < bottom]
    moved = False
    if not pics and not below_here and dp + 1 < len(dst.doc):
        # the text below is not on this page: it, and the picture before it, head the next page
        nxt = _words(dst, dp + 1)
        for k in range(ANCHOR_WORDS, 1, -1):
            hit = _find_words(nxt, [w[4] for w in below[:k]], 0) if len(below) >= k else None
            if hit:
                pics = [p for p in _pictures(dst, dp + 1) if (p[1] + p[3]) / 2 < hit[0]]
                dp, moved = dp + 1, True
                break
    if not pics:
        return None
    q, u = _union(pics), _union(boxes)
    area = lambda r: max(r[2] - r[0], 0) * max(r[3] - r[1], 0)
    shape = lambda r: (r[2] - r[0]) / max(r[3] - r[1], 1e-6)
    around = [p for p in _pictures(src, sp) if p[0] - 2 <= u[0] and p[1] - 2 <= u[1] and p[2] + 2 >= u[2]
              and p[3] + 2 >= u[3] and area(p) >= 2 * area(u)]
    spot = None
    if around:  # e.g. a highlight drawn on a screenshot: the same spot on the other side's screenshot
        p = max(around, key=area)
        if abs(shape(p) / max(shape(q), 1e-6) - 1) <= SAME_SHAPE:
            sx, sy = (q[2] - q[0]) / (p[2] - p[0]), (q[3] - q[1]) / (p[3] - p[1])
            spot = (q[0] + (u[0] - p[0]) * sx, q[1] + (u[1] - p[1]) * sy,
                    q[0] + (u[2] - p[0]) * sx, q[1] + (u[3] - p[1]) * sy)
    y = q[1] if moved else y  # on the next page: the marker at the top of the picture there
    shown = [q[1], q[3]] if spot else [min(q[1], y), max(q[3], y)]  # the marker is in view too
    return dp, shown, spot


def _with_pictures(pc: "_PageCache", page: int, y: list, near: float = 12) -> list:
    """The y range of the issue, grown to every picture it overlaps (within `near` pt): the whole
    picture shows in the screenshot, not a slice of it."""
    y0, y1 = y
    for (_, b0, _, b1) in _pictures(pc, page):
        if b0 <= y1 + near and b1 >= y0 - near and (b1 - b0) < 480:
            y0, y1 = min(y0, b0), max(y1, b1)
    return [y0, y1]


def _content_bounds(meta: dict) -> tuple[float, float] | None:
    """The content column's own left/right (pt), so a marker line is drawn only across it - not the
    whole page width. On a web capture the page image is the full browser viewport (left nav, content,
    "on this page" panel side by side): a full-width line also crosses the nav, landing on whatever
    heading happens to sit at that height in its own unrelated list - confusing, not a wrong position.
    None when the document has no measured margins (too few lines) - callers fall back to full width."""
    lr = (meta.get("margins") or {}).get("odd")
    return (lr[0], lr[1]) if lr and lr[1] - lr[0] > 50 else None


def _plan(sides: dict, line: dict, page_h: dict) -> dict:
    """One crop window per side, so prod and stage show the same area: the same number of text
    lines (each document measured in its own line height - an A5 manual at 8 pt and an A4 one
    at 12 pt), the issue in the middle, CONTEXT lines above and below it.
    sides[side] = ("boxes", [y0, y1]) | ("point", y) | ("start", y)."""
    span = max(((v[1][1] - v[1][0]) / line[k] for k, v in sides.items() if v[0] == "boxes"), default=0.0)
    n = min(span + 2 * CONTEXT, MAX_LINES)  # window height in lines, the same on both sides
    n = max(n, MIN_LINES)
    out = {}
    for k, (kind, y) in sides.items():
        h = n * line[k]
        # both sides start the same way: CONTEXT lines above the first marked place (the other side's
        # marker for text it lacks), so the two screenshots show the same passage from the same point -
        # not each centred on its own spread of marks (10 marks in prod, 2 in stage)
        if kind == "boxes":
            y0 = y[0] - CONTEXT * line[k]
        elif kind == "point":  # where the missing text belongs
            y0 = y - CONTEXT * line[k]
        else:  # section start: from just above it
            y0 = y - CONTEXT * line[k]
        y0 = max(0.0, y0)  # past the page end the screenshot continues on the next page (_strip)
        out[k] = (y0, y0 + h)
    return out


def render(result: dict, out_dir: str | Path, mode: str = "all", zoom: float = 2.5,
           progress: Callable[[float, str], None] | None = None) -> int:
    """Adds f["shots"] = {"baseline": rel, "candidate": rel} to each rendered finding.
    [report] full_page_shots (default on): each screenshot is the whole page the issue is on, prod and stage
    alike, so the two never show different crops."""
    global _PIC_MODE
    try:
        from .. import engine as _engine
        full_page = _engine.load_config().get("report", {}).get("full_page_shots", True)
    except Exception:
        full_page = True
    want = SEVERITIES.get(mode, SEVERITIES["all"])
    # genuine issues always get screenshots (unless none at all): they go into the genuine-issues report
    from .pdf_report import PDF_IMAGE_TYPES, is_image_issue
    # ... and so do the picture issues that report shows whether genuine or not (size, pixelated)
    todo = [(s, f) for s in result["sections"] for f in s["findings"]
            if f["severity"] in want or (mode != "none" and f.get("genuine"))
            or (mode != "none" and PDF_IMAGE_TYPES & set(f.get("types") or []))
            or (mode == "reports" and is_image_issue(f))]
    # the image report's own issues (a picture's label missing): always pictured, unless no screenshots at all
    todo += [(s, f) for s in result["sections"] for f in s.get("image_findings", []) if mode != "none"]
    # a finding already pictured (a batch run's genuine / image issues) is not rendered again
    todo = [(s, f) for s, f in todo if "shots" not in f]
    if not todo:
        return 0
    out = Path(out_dir)
    (out / "shots").mkdir(parents=True, exist_ok=True)
    caches = {"baseline": _PageCache(result["meta"]["baseline"]["path"], zoom),
              "candidate": _PageCache(result["meta"]["candidate"]["path"], zoom)}
    # a text line in each document (pt): its body size x 1.4
    line = {side: 1.4 * float(result["meta"][side].get("body_size") or 10) for side in ("baseline", "candidate")}
    content_x = {side: _content_bounds(result["meta"][side]) for side in ("baseline", "candidate")}
    for k, (s, f) in enumerate(todo):
        f["shots"] = {}
        c = f.get("color")  # the issue's own colour (genuine.color_of): red / blue, else by check
        color = tuple(int(c[n:n + 2], 16) for n in (1, 3, 5)) if c else COLORS.get(f["check"], (220, 38, 38))
        title = f.get("issue") or ", ".join(f.get("types") or []) or f["check"]
        linked = _linked_view(f.get("links") or [], line)
        what: dict[str, tuple] = {}  # side -> (page, kind, draw) ; draw is called with the crop window
        drawn: dict[str, list] = {}  # side -> the finding's boxes on the page shown
        marked: dict[str, tuple] = {}  # side -> (page, y, "prod" / "stage") of a text-aligned marker
        where: dict[str, tuple] = {}  # side -> what the window is planned around (see _plan)
        for side in ("baseline", "candidate"):
            locs = f[side]
            if linked:  # the same words highlighted on both sides
                page, boxes, note = linked[side]
                # plus every other place of the issue on that page (an issue of several differences:
                # the colons as well as the dropped bullets)
                boxes = boxes + [tuple(l["bbox"]) for l in f[side] if l["page"] == page and tuple(l["bbox"]) not in map(tuple, boxes)]
                where[side] = ("boxes", [min(b[1] for b in boxes), max(b[3] for b in boxes)])
                drawn[side] = boxes
                nb = [tuple(l["bbox"]) for l in f[side] if l["page"] == page + 1]
                what[side] = (page, "issue", lambda w, c=caches[side], p=page, b=boxes, n=note, nb=nb: _crop(c, p, b, color, n, w, nb))
            elif locs:
                page = locs[0]["page"]
                boxes = [l["bbox"] for l in locs if l["page"] == page]
                if f["check"] == "structure" and f["detail"].get("heading"):
                    boxes = boxes[:1]  # a missing section: anchored at its heading, not every box of it
                others = len({l["page"] for l in locs}) - 1
                note = f"+ more on {others} other page(s)" if others else None
                where[side] = ("boxes", [min(b[1] for b in boxes), max(b[3] for b in boxes)])
                drawn[side] = boxes
                nb = [tuple(l["bbox"]) for l in locs if l["page"] == page + 1]
                what[side] = (page, "issue", lambda w, c=caches[side], p=page, b=boxes, n=note, nb=nb: _crop(c, p, b, color, n, w, nb))
            elif side == "baseline" and f["detail"].get("kind") == "spec":
                # a design-spec finding: the reference is the Figma spec, not prod
                what[side] = (0, "spec", lambda w, t=f["detail"].get("spec", ""): _spec_card(t, color))
            elif side == "candidate" and f["detail"].get("stage_page_error"):
                # the section's web page did not load: any crop of the pages that did would mislead
                err = f["detail"]["stage_page_error"]
                page = f["candidate_at"]["page"] if f.get("candidate_at") else 0
                what[side] = (page, "page-error", lambda w, e=err: _error_card(e["url"], e["reason"], color))
            elif f.get(side + "_at"):  # one-sided: show the aligned position on this side
                at = f[side + "_at"]
                page = at["page"]
                where_ = "stage" if side == "candidate" else "prod"
                missing_sec = f["detail"].get("in_missing_section") or (
                    f["check"] == "structure" and "not found in candidate" in f["message"] and f["detail"].get("heading"))
                # (no title in the banner: its font has no CJK/Arabic glyphs; the issue text names the section)
                how = "where the missing section belongs: before the next section" if missing_sec \
                    else f"the {where_} note, which has no label" if "label only" in (f.get("types") or []) \
                    else "aligned by surrounding text"
                where[side] = ("point", at["bbox"][1])
                if not missing_sec and "label only" not in (f.get("types") or []):
                    marked[side] = (page, at["bbox"][1], where_)
                what[side] = (page, "section-slot" if missing_sec else "aligned",
                              lambda w, c=caches[side], p=page, y=at["bbox"][1], t=f"Not in {where_} - marker shows {how}", xb=content_x[side]:
                              _crop_marker(c, p, y, color, t, w, xbounds=xb))
            else:  # no alignment available at all: fall back to the section start
                start = s[side]["start"]
                page = start["page"]
                label = "Not present in stage" if side == "candidate" else "Not present in prod"
                where[side] = ("start", start["y"])
                what[side] = (page, "section", lambda w, c=caches[side], p=page, t=f"{label} - showing section start":
                              _crop_region(c, p, t, w))
        page_h = {side: caches[side].get(what[side][0]).height / caches[side].zoom for side in where}
        # an issue in one side's page header / footer: the other side's crop shows its header / footer
        # too (same place on the page), not a marker in the body text
        for side, other in (("baseline", "candidate"), ("candidate", "baseline")):
            if where.get(side, ("",))[0] == "boxes" and where.get(other, ("",))[0] in ("point", "start"):
                y0, y1 = where[side][1]
                rel = (y0 + y1) / 2 / page_h[side]
                if rel <= FOOTER_BAND or rel >= 1 - FOOTER_BAND:
                    where[other] = ("point", rel * page_h[other])
                    marked.pop(other, None)
        # an issue on or next to a picture: show the whole picture, not a slice of it
        for side, (kind, y) in list(where.items()):
            if kind == "boxes":
                where[side] = ("boxes", _with_pictures(caches[side], what[side][0], y))
        # a picture on one side, nothing to highlight on the other: that side shows the same picture
        # (found between the same text above and below it), not only the text after the marker, and
        # a spot inside the picture (a highlight on a screenshot) is marked at the same spot of it
        for side, other in (("baseline", "candidate"), ("candidate", "baseline")):
            if side in marked and where.get(other, ("",))[0] == "boxes" and other in drawn:
                page, y, name = marked[side]
                own = [min(b[1] for b in drawn[other]), max(b[3] for b in drawn[other])]
                got = _same_place(caches[other], what[other][0], _with_pictures(caches[other], what[other][0], own, 0),
                                  drawn[other], caches[side], page, y, line[side])
                if got:
                    page, shown, spot = got
                    y = y if page == marked[side][0] else shown[0]
                    page_h[side] = caches[side].get(page).height / caches[side].zoom
                    where[side] = ("boxes", shown)
                    how = "the same spot in the same picture" if spot else "aligned by surrounding text"
                    what[side] = (page, "aligned", lambda w, c=caches[side], p=page, y=y, t=f"Not in {name} - marker shows {how}",
                                  b=spot, xb=content_x[side]: _crop_marker(c, p, y, color, t, w, b, xbounds=xb))
        windows = _plan(where, line, page_h)
        if full_page:
            for side in what:
                pg = what[side][0]
                if pg is not None and pg >= 0:
                    windows[side] = (0.0, caches[side].get(pg).height / caches[side].zoom)
        _PIC_MODE = bool(set(f.get("types") or []) & IMAGE_KINDS)
        for side in ("baseline", "candidate"):
            page, kind, draw = what[side]
            # the prod picture says what is expected, the stage picture what is actually there
            img = _caption(draw(windows.get(side)), "PROD" if side == "baseline" else "STAGE", title,
                           concise(f["message"]).replace("  ·  ", "\n"), color)
            rel = f"shots/{f['id']}_{'prod' if side == 'baseline' else 'stage'}.webp"
            img.save(out / rel, "WEBP", quality=92, method=0)  # fastest encoder: same quality, ~8 % larger, 2-3x faster
            f["shots"][side] = rel
            f["shots"][side + "_page"] = page + 1
            f["shots"][side + "_kind"] = kind
        if progress and k % 20 == 0:
            progress(k / len(todo), f"Screenshots {k}/{len(todo)}")
    return len(todo)


def _caption(img: Image.Image, side: str, title: str, text: str, color) -> Image.Image:
    """A strip above the screenshot that says what the issue is: the side, the issue name in the
    issue's colour, then the issue text (up to 3 lines). Above the picture, so it hides nothing."""
    width, pad = img.width, 10
    # arrows and every script but plain Latin: a font that has those letters
    font = _cjk_font(title + text) if any(ord(ch) >= 0x250 for ch in title + text) else _FONT
    if font is _FONT:
        text = text.replace("—", "-").replace("×", "x")  # the default font has no em dash / times sign
    cjk = font is not _FONT and any(ord(ch) >= 0x2E80 for ch in text)  # no spaces between CJK words: wrap anywhere
    sep = "" if cjk else " "
    lines = []
    for para in text.split("\n"):  # a design-spec issue: its headline, "Figma: …", "Stage: …", each on its own line
        words, cur = (list(para) if cjk else para.split()), ""
        for w in words:
            trial = f"{cur}{sep}{w}" if cur else w
            if font.getlength(trial) <= width - 2 * pad or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
    # every line: the caption is never cut short
    h = 34 + 24 * len(lines) + 6
    out = Image.new("RGB", (width, img.height + h), (255, 255, 255))
    d = ImageDraw.Draw(out)
    d.rectangle([0, 0, width, h - 1], fill=(248, 250, 252))
    d.rectangle([0, 0, 6, h - 1], fill=color)
    d.text((pad + 4, 8), f"{side}  ·  ", fill=(100, 116, 139), font=font)
    d.text((pad + 4 + font.getlength(f"{side}  ·  "), 8), title, fill=color, font=font)
    for n, ln in enumerate(lines):
        y = 34 + 24 * n
        lab = next((k for k in ("Figma: ", "Prod: ", "Stage: ") if ln.startswith(k)), None)
        if lab:  # a design-spec issue: the Figma / Stage label in colour
            d.text((pad + 4, y), lab, fill={"Figma: ": (124, 58, 237), "Prod: ": (37, 99, 235)}.get(lab, (15, 118, 110)), font=font)
            d.text((pad + 4 + font.getlength(lab), y), ln[len(lab):], fill=(29, 35, 48), font=font)
        else:
            d.text((pad + 4, y), ln, fill=(29, 35, 48), font=font)
    d.line([(0, h - 1), (width, h - 1)], fill=(226, 232, 240), width=1)
    out.paste(img, (0, h))
    return out


_WIDE: list = []
_WIDE_FILES = ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", "/Library/Fonts/Arial Unicode.ttf",
               "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
               "C:/Windows/Fonts/arial.ttf")


def _cjk_font(text: str = ""):
    """A font that has the caption's letters: the default one is Latin only, so Russian, Greek, Arabic, Thai,
    Chinese ... came out as empty boxes. Tried in turn - a system font with every script, FiraGO (Cyrillic,
    Greek, Arabic, Hebrew, Thai) and Droid Sans Fallback (Chinese / Japanese / Korean), both bundled with
    PyMuPDF - and the one covering most of the text is used."""
    if not _WIDE:
        import io
        import os
        for f in _WIDE_FILES:
            if os.path.exists(f):
                try:
                    _WIDE.append((pymupdf.Font(fontfile=f), ImageFont.truetype(f, 17)))
                except Exception:
                    pass
        for name in ("figo", "cjk"):
            try:
                pf = pymupdf.Font(name)
                _WIDE.append((pf, ImageFont.truetype(io.BytesIO(pf.buffer), 17)))
            except Exception:
                pass
        _WIDE.append((None, _FONT))
    chars = {ch for ch in text if not ch.isspace() and ord(ch) > 0x7F}
    best = max(_WIDE, key=lambda pf: sum(1 for ch in chars if pf[0] is not None and pf[0].has_glyph(ord(ch))))
    return best[1]


def _linked_view(links: list, line: dict) -> dict | None:
    """From (prod box, stage box) links of the same text, pick the ones both crops can
    show: same page as the first link on each side and within one crop height of it
    on both sides (the crop height is MAX_LINES text lines, see _plan). Both screenshots
    then highlight exactly the same words."""
    if not links:
        return None
    fa, fb = links[0]
    room = {k: (MAX_LINES - 2 * CONTEXT) * line[k] for k in line}
    fits = lambda l, f, span: l["page"] == f["page"] and l["bbox"][3] - f["bbox"][1] <= span and f["bbox"][1] - l["bbox"][1] <= span
    shown = [(a, b) for a, b in links if fits(a, fa, room["baseline"]) and fits(b, fb, room["candidate"])]
    hidden = len(links) - len(shown)
    note = f"+ {hidden} more place(s) outside this view" if hidden else None
    return {"baseline": (fa["page"], [a["bbox"] for a, _ in shown], note),
            "candidate": (fb["page"], [b["bbox"] for _, b in shown], note)}


def _crop_marker(pc: _PageCache, page: int, y: float, color, note: str, window: tuple[float, float],
                 box: tuple | None = None, xbounds: tuple[float, float] | None = None) -> Image.Image:
    """Crop the window and draw a dashed marker line at the insertion point y - or, given the box
    where the missing thing belongs, a dashed box there with the arrow pointing at it. xbounds (pt):
    the content column's own left/right, so the line spans only that - not a web page's left nav or
    "on this page" panel, which share the same y-coordinates as the content next to them."""
    z = pc.zoom
    y0, y1 = window
    img, _ = _strip(pc, page, y0, y1)
    d = ImageDraw.Draw(img)
    if box:
        l, t, r, b = [(box[0] - 3) * z, (box[1] - 3 - y0) * z, (box[2] + 3) * z, (box[3] + 3 - y0) * z]
        for a in range(int(l), int(r), 16):  # dashed outline
            d.line([(a, t), (min(a + 9, r), t)], fill=color, width=3)
            d.line([(a, b), (min(a + 9, r), b)], fill=color, width=3)
        for a in range(int(t), int(b), 16):
            d.line([(l, a), (l, min(a + 9, b))], fill=color, width=3)
            d.line([(r, a), (r, min(a + 9, b))], fill=color, width=3)
        my = int((t + b) / 2)
    else:
        my = int((y - 3 - y0) * z)
        x0 = int(xbounds[0] * z) if xbounds else 0
        x1 = int(xbounds[1] * z) if xbounds else img.width
        for x in range(x0, x1, 22):
            d.line([(x, my), (x + 12, my)], fill=color, width=4)
    d.polygon([(0, my - 10), (14, my), (0, my + 10)], fill=color)
    d.rectangle([0, 0, img.width, 30], fill=(30, 35, 48))
    d.text((10, 6), note, fill=(255, 255, 255), font=_FONT)
    return img


def _spec_card(text: str, color) -> Image.Image:
    """Stand-in for the prod side of a design-spec finding: the style the spec asks for."""
    img = Image.new("RGB", (1280, 220), (248, 250, 252))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, img.width, 30], fill=(30, 35, 48))
    d.text((10, 6), "Design spec (Figma: Online User Manual) - checked against stage, not prod", fill=(255, 255, 255), font=_FONT)
    d.rectangle([0, 30, 8, img.height], fill=color)
    for k in range(0, len(text), 100):
        d.text((30, 70 + 28 * (k // 100)), text[k:k + 100], fill=(51, 65, 85), font=_FONT)
    return img


def _error_card(url: str, reason: str, color) -> Image.Image:
    """Stand-in for a stage page that failed to load: what was opened and what came back."""
    img = Image.new("RGB", (1280, 220), (248, 250, 252))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, img.width, 30], fill=(30, 35, 48))
    d.text((10, 6), "Not in stage - the stage page for this section was not captured", fill=(255, 255, 255), font=_FONT)
    d.rectangle([0, 30, 8, img.height], fill=color)
    d.text((30, 70), f"Stage page not captured: {reason}", fill=color, font=_FONT)
    url = url or "(no page to open: the left navigation has no entry for this section's chapter)"
    for k in range(0, len(url), 110):  # long URLs wrap
        d.text((30, 110 + 26 * (k // 110)), url[k:k + 110], fill=(51, 65, 85), font=_FONT)
    return img


def _crop_region(pc: _PageCache, page: int, note: str, window: tuple[float, float]) -> Image.Image:
    y0, y1 = window
    img, _ = _strip(pc, page, y0, y1)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, img.width, 30], fill=(30, 35, 48))
    d.text((10, 6), note, fill=(255, 255, 255), font=_FONT)
    return img
