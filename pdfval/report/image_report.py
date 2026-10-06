"""The image report (image-issues.pdf): only what is wrong with a picture's own content, shown as the pictures.

Captured (nothing else):
  * callout numbers missing   - “1” “2” “a” of a prod picture that stage's picture does not have
  * image labels missing      - short text set on / right above, below, left or right of the picture
  * leader lines missing      - the lines from a label to the picture
  * red overlay missing/added - red highlight marks drawn over a picture on one side only
  * image pixelated           - stage prints the picture from far fewer pixels
  * artwork missing           - a prod picture that stage does not show (or that does not load / is blacked out)
Per issue: the section, its AEM topic, the issue name, what prod has and what stage has in one line each, and the
prod and the stage picture side by side, rendered at their own resolution (an embedded bitmap at its native
pixels, a vector drawing sharp). A missing picture's stage side is the stage page around the place it belongs."""
from __future__ import annotations

from pathlib import Path

import pymupdf

KINDS = {  # issue name per finding (by type / kind)
    "callout numbers missing": "Callout numbers missing",
    "image label missing": "Image labels missing",
    "missing image label": "Image labels missing",
    "leader lines missing": "Leader lines missing",
    "image pixelated": "Image pixelated",
    # the picture itself: not there in stage, not loading, or blacked out
    "missing image": "Artwork missing",
    "broken image": "Artwork not loading",
    "image blacked out": "Artwork blacked out",
}
_NO_ARTWORK = ("Artwork missing", "Artwork not loading")  # no stage picture to pair: the stage spot is shown


def _kind(f: dict) -> str | None:
    d = f.get("detail") or {}
    if d.get("kind") == "marks":  # the red highlight overlay (boxes / arrows drawn over the picture) on one side only
        return "Red overlay missing" if d.get("marks") == "missing" else "Red overlay added"
    names = [KINDS[t] for t in (f.get("types") or []) if t in KINDS]
    return " · ".join(dict.fromkeys(names)) or None


def issues(result: dict) -> list[tuple[dict, dict, str]]:
    """(section, finding, issue name) for the image report, one per prod picture and kind."""
    out, seen = [], set()
    shown: dict[int, list] = {}  # prod page -> boxes of the figures already in the report
    try:
        A = pymupdf.open(result["meta"]["baseline"]["path"])
    except Exception:
        A = None
    for s in result["sections"]:
        for f in (s.get("image_findings") or []) + s["findings"]:
            name = _kind(f)
            if name and name.split(" · ")[0] in _NO_ARTWORK and f.get("baseline") and not f.get("candidate"):
                # a picture stage does not show: its stage side is the place it belongs (where the text around it
                # is in stage) - a band of the stage page around that spot
                at = f.get("candidate_at")
                if not at:
                    continue
                f = {**f, "candidate": [{"page": at["page"], "bbox": [0, max(at["bbox"][1] - 150, 0), 1e5, at["bbox"][3] + 150]}],
                     "_spot_only": True}
            if not name or not f.get("baseline") or not f.get("candidate"):
                continue
            b = f["baseline"][0]
            box = tuple(round(v) for v in b["bbox"])
            if A is not None:
                # only what sits on / right at a real picture; and one entry per prod picture
                pic = _whole_picture(A[b["page"]], [l for l in f["baseline"] if l["page"] == b["page"]])
                if pic is None and f.get("_spot_only"):
                    pic = pymupdf.Rect(b["bbox"])  # the missing picture itself, however small (an icon)
                if pic is None:
                    continue
                # the section's own heading above a picture is not the picture's label
                norm = lambda t: "".join(c for c in t.lower() if c.isalnum())
                gone = (f.get("detail") or {}).get("labels_missing") or (f.get("detail") or {}).get("labels") or []
                if gone and norm(" ".join(gone)) == norm(s["title"]):
                    continue
                box = tuple(round(v / 6) for v in pic)
                # the same figure found from two findings (two of its labels, two checks): once - also when the
                # two boxes differ a little (most of the smaller one lies in the other)
                same = False
                for q in shown.setdefault(b["page"], []):
                    inter = (pymupdf.Rect(q) & pic).get_area() if pymupdf.Rect(q).intersects(pic) else 0.0
                    if inter >= 0.6 * min(pymupdf.Rect(q).get_area(), pic.get_area()):
                        same = True
                        break
                if same:
                    continue
                shown[b["page"]].append(tuple(pic))
            key = (b["page"], box)
            if key in seen:
                continue  # the same picture from two checks: once
            seen.add(key)
            out.append((s, f, name))
    return out


def _zoom(page: pymupdf.Page, clip: pymupdf.Rect) -> float:
    """The scale that renders the clip at the native pixels of the biggest bitmap in it (vector art: 4x)."""
    best = 0.0
    for info in page.get_image_info():
        r = pymupdf.Rect(info["bbox"])
        if r.intersects(clip) and r.width > 0 and info.get("width"):
            best = max(best, info["width"] / r.width)
    return min(max(best, 2.0), 6.0) if best else 4.0


_PIECES: dict[tuple, list] = {}
_TABLES: dict[tuple, list] = {}


def _tables(pg: pymupdf.Page) -> list[pymupdf.Rect]:
    """The page's tables (ruled grids, or at least a header band of several cells)."""
    key = (pg.parent.name, pg.number)
    if key not in _TABLES:
        if len(_TABLES) > 400:
            _TABLES.clear()
        try:
            _TABLES[key] = [pymupdf.Rect(t.bbox) for t in pg.find_tables().tables if t.col_count >= 2]
        except Exception:
            _TABLES[key] = []
    return _TABLES[key]


def _pieces(pg: pymupdf.Page, max_share: float) -> list[pymupdf.Rect]:
    """The picture pieces of a page: its embedded images and its vector drawings - not its tables. A table is drawn
    too (rules, a header band), and one right under a figure touches it: a drawing made only of straight horizontal /
    vertical rules and filled boxes with text in it is a table (or a note box), never part of the artwork."""
    key = (pg.parent.name, pg.number)
    if key not in _PIECES:
        if len(_PIECES) > 400:
            _PIECES.clear()
        pics = [(pymupdf.Rect(i["bbox"]), False) for i in pg.get_image_info() if pymupdf.Rect(i["bbox"]).width >= 40]
        try:
            drawings = pg.get_drawings()
            words = [pymupdf.Rect(w[:4]) for w in pg.get_text("words")]
            rasters = [pymupdf.Rect(i["bbox"]) for i in pg.get_image_info()]
            for r in pg.cluster_drawings(drawings=drawings):
                if r.width < 60 or r.height < 40:
                    continue
                shaped = 0  # curves and slanted strokes: what artwork is made of
                art = pymupdf.Rect()  # the area those shapes cover
                for d in drawings:
                    dr = pymupdf.Rect(d["rect"])
                    if not r.contains(dr):
                        continue
                    n = sum(1 for it in d["items"]
                            if it[0] == "c" or (it[0] == "l" and abs(it[1].x - it[2].x) > 1 and abs(it[1].y - it[2].y) > 1))
                    shaped += n
                    # the artwork's own area: shapes of a picture's size, not a frame / rule across the cluster
                    if n and dr.width < 0.9 * r.width:
                        art |= dr
                held = [w for w in words if r.contains(pymupdf.Point((w.x0 + w.x1) / 2, (w.y0 + w.y1) / 2))]
                inside = len(held)
                text_share = sum(w.get_area() for w in held) / max(r.get_area(), 1)
                has_raster = any((q & r).get_area() >= 0.2 * r.get_area() for q in rasters if q.intersects(r))
                table = inside >= 6 and shaped < 4
                # a box of running text (a NOTE / IMPORTANT callout: rounded corners and an icon are its only
                # curves) is not a picture: its words fill it, a figure's labels only dot it
                if inside >= 8 and text_share >= 0.15 and shaped < 60 and not has_raster:
                    table = True
                for t in _tables(pg):  # the drawing is a table's grid (most of each lies in the other)
                    inter = (t & r).get_area() if t.intersects(r) else 0.0
                    if inter >= 0.5 * min(t.get_area(), r.get_area()) and inter >= 0.3 * max(t.get_area(), r.get_area()):
                        table = True
                    # a part of the table's ruling (one column's borders, a header band): mostly inside the
                    # table and no artwork of its own - a drawing in a table cell has curves and stays
                    if inter >= 0.6 * r.get_area() and shaped < 80 and not has_raster:  # (rounded cell corners are curves too)
                        table = True
                    # the table's whole grid, however its cells are drawn (rounded corners on every cell)
                    if inter >= 0.8 * r.get_area() and inter >= 0.8 * t.get_area() and not has_raster \
                            and shaped < 12 * max(inside, 1):
                        table = True
                # a cluster that swallowed body text (a drawing joined to paragraphs by a rule or a frame): the
                # picture is the part its curves cover, not the text beside / under it
                if not table and inside >= 12 and not art.is_empty and art.width >= 40 and art.height >= 30 \
                        and art.get_area() < 0.6 * r.get_area():
                    r = art + (-4, -4, 4, 4)
                pics.append((r, table))
        except Exception:
            pass
        _PIECES[key] = pics
    area = pg.rect.get_area()
    return [pymupdf.Rect(r) for r, table in _PIECES[key] if not table and r.get_area() < max_share * area]


def _whole_picture(pg: pymupdf.Page, locs: list) -> pymupdf.Rect | None:
    """The full picture a finding is about: its marked spots (a label's own box, a part of the picture) joined
    with the whole picture they sit on or next to - the embedded image or the vector drawing that holds / is
    nearest to them. A finding marking only the “2” badge of a drawing still shows the drawing."""
    spot = pymupdf.Rect(locs[0]["bbox"])
    for l in locs[1:]:
        spot |= pymupdf.Rect(l["bbox"])
    pics = _pieces(pg, 0.9)
    if not pics:
        return None

    def gap(r):  # 0 when the spot is on the picture, else the distance to it
        dx = max(r.x0 - spot.x1, spot.x0 - r.x1, 0)
        dy = max(r.y0 - spot.y1, spot.y0 - r.y1, 0)
        return (dx * dx + dy * dy) ** 0.5
    near = [r for r in pics if gap(r) <= 60]
    if not near:
        return None  # no picture at this spot (a step-number badge in the text column): not a picture issue
    # the picture holding the spot; of several (a drawing inside a frame), the smallest that still holds it all
    holding = [r for r in near if gap(r) == 0]
    best = min(holding, key=lambda r: r.get_area()) if holding else min(near, key=gap)
    if best.get_area() < 4 * spot.get_area() and holding:  # the spot is most of a small piece: take the largest around
        best = max(holding, key=lambda r: r.get_area())
    # the whole figure: a figure is often several pieces (a bitmap of the device, cables and arrows drawn as
    # vectors below it, a second bitmap for the plug) - every piece touching what is already taken joins it, so the
    # artwork is never shown cut off. A piece as large as the page (a frame, a background) is not a figure part.
    page_area = pg.rect.get_area()
    best, rest = pymupdf.Rect(best), [r for r in pics if r.get_area() < 0.6 * page_area]
    for _ in range(8):
        # (touching: 4 pt - figures stacked in a column a line apart stay separate figures)
        touch = [r for r in rest if (best + (-4, -4, 4, 4)).intersects(r) and not best.contains(r)]
        if not touch:
            break
        joined = pymupdf.Rect(best)
        for r in touch:
            joined |= r
        if joined.get_area() > 0.85 * page_area:
            break
        best = joined
    # the picture, plus the marked spots that touch it (a callout beside it) - not a heading or a paragraph line
    # further off that a check marked as its label
    out = pymupdf.Rect(best)
    grown = best + (-12, -12, 12, 12)
    for l in locs:
        r = pymupdf.Rect(l["bbox"])
        # (a marked area that is the figure itself - it holds the picture's centre - is the figure: its labels
        # and badges below the drawing belong to it)
        centre = pymupdf.Point((best.x0 + best.x1) / 2, (best.y0 + best.y1) / 2)
        if r.intersects(grown) and (r.height <= 0.6 * best.height + 20 or
                                    (r.contains(centre) and r.get_area() < 0.6 * page_area)):
            out |= r
    # the artwork around the picture: every leader line running into it, and the callout number / short label
    # at the line's outer end (“1” left of a screenshot, 28 pt away) - the picture is shown with them
    words = pg.get_text("words")
    try:
        segs = [(it[1], it[2]) for d in pg.get_drawings() for it in d["items"]
                if it[0] == "l" and 6 <= abs(it[1] - it[2]) <= 250]
    except Exception:
        segs = []
    reached = [best]  # the picture, then the ends of the lines already followed (a leader line may bend)
    on = lambda p: any(r.x0 - 3 <= p.x <= r.x1 + 3 and r.y0 - 3 <= p.y <= r.y1 + 3 for r in reached)
    for _ in range(3):
        added = []
        for a_, b_ in segs:
            if on(a_) == on(b_):
                continue  # not a line leading out from what is already included
            end = b_ if on(a_) else a_
            out |= pymupdf.Rect(min(a_.x, b_.x), min(a_.y, b_.y), max(a_.x, b_.x), max(a_.y, b_.y))
            added.append(pymupdf.Rect(end.x - 1, end.y - 1, end.x + 1, end.y + 1))
            for w in words:  # the label at the line's end
                wr = pymupdf.Rect(w[:4])
                if wr.x0 - 14 <= end.x <= wr.x1 + 14 and wr.y0 - 14 <= end.y <= wr.y1 + 14 and wr.width <= 160:
                    out |= wr
        if not added:
            break
        reached += added
    # a short label cut by the figure's edge is shown whole ("RJ45 cable" left of the laptop, "LAN switch or hub"
    # under the switch): words overlapping the box widen it - a little, never by a paragraph beside the figure
    # a table above / below / beside the picture is not part of it, whatever marked it (a finding's area that runs on
    # over the table under the figure): the picture ends where the table starts. A picture inside a table cell stays.
    for t in _tables(pg):
        if not t.intersects(out) or t.contains(best):
            continue
        if t.y0 >= best.y1 - 6:
            out.y1 = min(out.y1, t.y0)
        elif t.y1 <= best.y0 + 6:
            out.y0 = max(out.y0, t.y1)
        elif t.x0 >= best.x1 - 6:
            out.x1 = min(out.x1, t.x0)
        elif t.x1 <= best.x0 + 6:
            out.x0 = max(out.x0, t.x1)
    blocks: dict[int, list] = {}
    for w in words:
        blocks.setdefault(w[5], []).append(pymupdf.Rect(w[:4]))
    for rs in blocks.values():  # the label as a whole ("RJ45" + "cable", "LAN switch" + "or hub"), when it is short
        if len(rs) > 8 or not any(wr.intersects(out) for wr in rs):
            continue
        box = pymupdf.Rect(rs[0])
        for wr in rs[1:]:
            box |= wr
        # cut by the edge - a good part of it is inside already - not a caption or a line that only touches it
        if not out.contains(box) and box.width <= 170 and box.height <= 60 \
                and (box & out).get_area() >= 0.3 * box.get_area() \
                and not any(t.contains(pymupdf.Point((box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2))
                            and not t.contains(best) for t in _tables(pg)):
            out |= box
    out = (out + (-4, -4, 4, 4)) & pg.rect
    for t in _tables(pg):  # the margin does not reach into a table either (its top rule under the figure)
        if t.intersects(out) and not t.contains(best):
            if t.y0 >= best.y1 - 6:
                out.y1 = min(out.y1, t.y0 - 1)
            elif t.y1 <= best.y0 + 6:
                out.y0 = max(out.y0, t.y1 + 1)
    # never a picture with the page's running text: grown far beyond the picture itself (a marked spot down the
    # page - a page number taken for a callout number -, a rule followed into the text) and holding paragraphs,
    # it is shown as the picture alone
    if out.get_area() > 2.2 * best.get_area():
        extra = [w for w in words if out.contains(pymupdf.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2))
                 and not best.contains(pymupdf.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2))]
        if len(extra) >= 20:
            out = (best + (-4, -4, 4, 4)) & pg.rect
    return out


def _figures(pg: pymupdf.Page, y0: float = 0.0, y1: float = 1e9) -> list[pymupdf.Rect]:
    """The whole figures of a page between y0 and y1, each once."""
    pics = _pieces(pg, 0.6)
    out: list[pymupdf.Rect] = []
    for r in sorted(pics, key=lambda r: -r.get_area()):
        if not (y0 - 4 <= (r.y0 + r.y1) / 2 <= y1 + 4):
            continue
        whole = _whole_picture(pg, [{"bbox": tuple(r)}])
        if whole is None or any((q & whole).get_area() >= 0.6 * min(q.get_area(), whole.get_area())
                                for q in out if q.intersects(whole)):
            continue
        out.append(whole)
    return out


def _look(pg: pymupdf.Page, clip: pymupdf.Rect, n: int = 32):
    """n x n grey thumbnail of the clip, mean removed (for comparing two figures by their look)."""
    import numpy as np
    z = 96 / max(clip.width, clip.height, 1)
    pix = pg.get_pixmap(matrix=pymupdf.Matrix(z, z), clip=clip, colorspace=pymupdf.csGRAY, alpha=False)
    from PIL import Image as _I
    a = np.asarray(_I.frombytes("L", (pix.width, pix.height), pix.samples).resize((n, n)), dtype=np.float64)
    return a - a.mean()


def _alike(a, b) -> float:
    import numpy as np
    d = float(np.sqrt((a * a).sum() * (b * b).sum()))
    return float((a * b).sum() / d) if d > 1e-9 else 0.0


def stage_spot(A: pymupdf.Document, B: pymupdf.Document, s: dict, f: dict) -> tuple[int, list]:
    """(stage page, spots) of the stage figure shown beside the prod figure: the finding's own stage spot - unless a
    figure of the section's stage pages looks clearly more like the prod figure (the finding pointed at a neighbour
    figure, or at one of the section before): then that one. A section with several figures shows the right pair."""
    b0 = f["baseline"][0]
    own_page = f["candidate"][0]["page"]
    own = [l for l in f["candidate"] if l["page"] == own_page]
    try:
        pa = A[b0["page"]]
        clip_a = _whole_picture(pa, [l for l in f["baseline"] if l["page"] == b0["page"]])
        if clip_a is None:
            return own_page, own
        look_a, ar_a = _look(pa, clip_a), clip_a.width / max(clip_a.height, 1)
        st, en = s["candidate"]["start"], s["candidate"]["end"]
        cands = []
        for pn in range(st["page"], min(en["page"], len(B) - 1) + 1):
            pg = B[pn]
            for r in _figures(pg, st["y"] if pn == st["page"] else 0.0, en["y"] if pn == en["page"] else 1e9):
                ar = r.width / max(r.height, 1)
                if 0.5 <= ar / ar_a <= 2.0:  # a figure of quite another shape is another figure
                    cands.append((_alike(look_a, _look(pg, r)), pn, r))
        inside = (st["page"], st["y"] - 4) <= (own_page, own[0]["bbox"][1]) <= (en["page"], en["y"] + 4)
        if not cands and not inside:  # the finding's spot is in another section: any figure of this one is nearer
            for pn in range(st["page"], min(en["page"], len(B) - 1) + 1):
                pg = B[pn]
                cands += [(_alike(look_a, _look(pg, r)), pn, r)
                          for r in _figures(pg, st["y"] if pn == st["page"] else 0.0, en["y"] if pn == en["page"] else 1e9)]
        if not cands:
            return own_page, own
        best = max(cands, key=lambda c: c[0])
        clip_own = _whole_picture(B[own_page], own)
        score_own = _alike(look_a, _look(B[own_page], clip_own)) if clip_own is not None else -1.0
        same = clip_own is not None and best[1] == own_page and best[2].intersects(clip_own)
        if same or (inside and score_own >= best[0] - 0.1):
            return own_page, own
        return best[1], [{"page": best[1], "bbox": tuple(best[2])}]
    except Exception:
        return own_page, own


def stage_spots(A: pymupdf.Document, B: pymupdf.Document, rows: list) -> list[tuple[int, list]]:
    """stage_spot of every row, then the rows of one section set right together: two prod figures of a section
    (the back view of two models, drawn alike) must not show each other's stage figure. Among the stage figures
    those rows chose, each prod figure gets the one that looks most like it over all of them; where the looks
    are as good either way (near-identical drawings), the figures keep their reading order."""
    from itertools import permutations
    spots = [stage_spot(A, B, s, f) for s, f, _ in rows]
    groups: dict[int, list[int]] = {}
    for k, (s, _, _) in enumerate(rows):
        groups.setdefault(id(s), []).append(k)
    for ks in groups.values():
        if not 2 <= len(ks) <= 6:
            continue
        try:
            prod, stage = [], []
            for k in ks:
                f = rows[k][1]
                b0 = f["baseline"][0]
                ca = _whole_picture(A[b0["page"]], [l for l in f["baseline"] if l["page"] == b0["page"]])
                sp, sl = spots[k]
                cb = _whole_picture(B[sp], sl)
                if ca is None or cb is None:
                    raise ValueError
                prod.append((b0["page"], ca))
                stage.append((sp, cb & B[sp].rect))
            # the same stage figure chosen twice is left alone (one of the two has no figure of its own)
            if len({(p, tuple(round(v) for v in r)) for p, r in stage}) < len(ks):
                continue
            la = [_look(A[p], r, 48) for p, r in prod]
            lb = [_look(B[p], r, 48) for p, r in stage]
            sim = [[_alike(x, y) for y in lb] for x in la]
            order = lambda items: sorted(range(len(items)), key=lambda i: (items[i][0], round(items[i][1].y0 / 40), items[i][1].x0))
            rank_a, rank_b = {i: n for n, i in enumerate(order(prod))}, {j: n for n, j in enumerate(order(stage))}
            best, best_score = None, -1e9
            for perm in permutations(range(len(ks))):
                score = sum(sim[i][perm[i]] for i in range(len(ks))) \
                    + 0.04 * sum(rank_a[i] == rank_b[perm[i]] for i in range(len(ks)))
                if score > best_score:
                    best, best_score = perm, score
            if best is not None and list(best) != list(range(len(ks))):
                chosen = [spots[k] for k in ks]
                for i, k in enumerate(ks):
                    sp, r = stage[best[i]]
                    spots[k] = (sp, [{"page": sp, "bbox": tuple(r)}]) if best[i] != i else chosen[i]
        except Exception:
            continue
    return spots


def _picture(doc: pymupdf.Document, page: int, locs: list) -> pymupdf.Pixmap:
    pg = doc[page]
    clip = (_whole_picture(pg, locs) or pymupdf.Rect(locs[0]["bbox"])) & pg.rect
    return pg.get_pixmap(matrix=pymupdf.Matrix(_zoom(pg, clip), _zoom(pg, clip)), clip=clip, alpha=False)


def build(result: dict, out_dir: str | Path, filename: str = "image-issues.pdf") -> Path:
    out = Path(out_dir) / filename
    meta, sm = result["meta"], result["summary"]
    A, B = pymupdf.open(meta["baseline"]["path"]), pymupdf.open(meta["candidate"]["path"])
    doc = pymupdf.open()
    W, H, M = 842, 595, 28  # A4 landscape
    font = pymupdf.Font("helv")
    rows = issues(result)
    # cover: the same consolidated content match % every other report leads with, so a reader never has
    # to open report.pdf just to see the overall score behind these picture issues
    c = sm["content"]
    pct_color = (0.09, 0.64, 0.23) if c["match_pct"] >= c["pass_pct"] else \
        (0.71, 0.33, 0.04) if c["match_pct"] >= c["warn_pct"] else (0.85, 0.18, 0.13)
    cover = doc.new_page(width=W, height=H)
    cover.insert_text((M, M + 16), "Image issues", fontsize=18, fontname="hebo")
    cover.insert_text((M, M + 34), f"{Path(meta['baseline']['path']).name}  vs  {Path(meta['candidate']['path']).name}",
                      fontsize=9, fontname="helv", color=(0.35, 0.38, 0.45))
    cover.insert_text((M, M + 62), f"{c['match_pct']:.2f}%", fontsize=28, fontname="hebo", color=pct_color)
    cover.insert_text((M, M + 80), "content match (every report; image issues are reported separately below)",
                      fontsize=9, fontname="helv", color=(0.35, 0.38, 0.45))
    cover.insert_text((M, M + 104), f"{len(rows)} image issue(s)" + (f"  ·  {sm['critical']['total']} critical" if sm["critical"]["total"] else ""),
                      fontsize=12, fontname="hebo", color=(0.2, 0.25, 0.35) if rows else pct_color)
    spots = stage_spots(A, B, rows)
    # (a missing picture has no stage figure to look for: its own spot stands)
    spots = [(f["candidate"][0]["page"], f["candidate"]) if f.get("_spot_only") else sp for (_, f, _), sp in zip(rows, spots)]
    from ..genuine import prod_stage
    try:
        uni = pymupdf.Font("notos")
    except Exception:
        uni = pymupdf.Font("helv")
    for (s, f, name), (spage, slocs) in zip(rows, spots):
        p = doc.new_page(width=W, height=H)
        topic = (f.get("aem") or {}).get("topic") or ""
        guid = (f.get("aem") or {}).get("guid") or ""
        head = f"{s['title']}  ·  {name}"
        p.insert_text((M, M + 12), head, fontsize=13, fontname="hebo")
        sub = "  ·  ".join(x for x in (f"AEM topic: {topic}" if topic else "", guid,
                                        f"prod p.{f['baseline'][0]['page'] + 1}  ↔  stage p.{spage + 1}") if x)
        p.insert_text((M, M + 28), sub, fontsize=9, fontname="helv", color=(0.35, 0.38, 0.45))
        if (f.get("aem") or {}).get("url"):
            p.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(M, M + 18, M + font.text_length(sub, 9), M + 31),
                           "uri": f["aem"]["url"]})
        # what differs, in two short lines: what prod's picture has, what stage's has (or lacks)
        ps = prod_stage(f)
        top = M + 44
        if ps:
            for label, text, colour in (("Prod:", ps[0], (0.15, 0.39, 0.92)), ("Stage:", ps[1], (0.06, 0.46, 0.43))):
                text = text if len(text) <= 150 else text[:149].rstrip() + "…"
                p.insert_text((M, top), label, fontsize=9, fontname="hebo", color=colour)
                tw = pymupdf.TextWriter(p.rect)  # a Unicode font: the labels' own quotes and scripts
                tw.append((M + 34, top), text, font=uni, fontsize=9)
                tw.write_text(p, color=(0.11, 0.14, 0.19))
                top += 13
            top += 4
        half = (W - 3 * M) / 2
        for k, (src, locs, label) in enumerate(((A, f["baseline"], "Prod"), (B, slocs, "Stage"))):
            loc = locs[0]
            x0 = M + k * (half + M)
            p.insert_text((x0, top + 10), label, fontsize=10, fontname="hebo", color=(0.2, 0.25, 0.35))
            box = pymupdf.Rect(x0, top + 18, x0 + half, H - M)
            if f.get("_spot_only") and (k == 1 or _whole_picture(src[loc["page"]], [loc]) is None):
                # the stage page around the spot, as it is; a small prod picture (an icon) with the text around it
                pg = src[loc["page"]]
                clip = (pymupdf.Rect(loc["bbox"]) if k == 1 else pymupdf.Rect(loc["bbox"]) + (-140, -70, 140, 70)) & pg.rect
                pix = pg.get_pixmap(matrix=pymupdf.Matrix(1.6, 1.6), clip=clip, alpha=False)
                if k == 0:  # the picture that is missing, marked
                    mark = pymupdf.IRect(*[int((v - o) * 1.6) for v, o in zip(loc["bbox"], (clip.x0, clip.y0, clip.x0, clip.y0))])
                    for t in range(3):
                        for x in range(max(mark.x0 - t, 0), min(mark.x1 + t, pix.width - 1)):
                            for yy in (mark.y0 - t, mark.y1 + t):
                                if 0 <= yy < pix.height:
                                    pix.set_pixel(x, yy, (220, 38, 38))
                        for yy in range(max(mark.y0 - t, 0), min(mark.y1 + t, pix.height - 1)):
                            for x in (mark.x0 - t, mark.x1 + t):
                                if 0 <= x < pix.width:
                                    pix.set_pixel(x, yy, (220, 38, 38))
            else:
                pix = _picture(src, loc["page"], [l for l in locs if l["page"] == loc["page"]])
            # fitted into its half page; the bitmap keeps all its pixels (zoom in the viewer to see them)
            sc = min(box.width / pix.width, box.height / pix.height)
            r = pymupdf.Rect(box.x0, box.y0, box.x0 + pix.width * sc, box.y0 + pix.height * sc)
            p.insert_image(r, pixmap=pix)
            p.draw_rect(r, color=(0.8, 0.82, 0.86), width=0.5)
    doc.save(out, deflate=True, garbage=3)
    return out
