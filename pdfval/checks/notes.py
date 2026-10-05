"""Note / callout look, prod against stage: the same kind of note should be set the same way.

A note is a line holding only its title ("Note", "NOTE:", "Tip" … the [content] label_words). Its look is
read from the drawings around that title: a filled box (its colour), a bar on its left (a thin rule or
rectangle), or a border. Per note type the usual look of each side is compared; when they differ there is one
finding for the type, with the first notes of each side as its places:
    Note style differs: prod — bar on the left #AC95C0 (13 notes) · stage — filled box #DAE8F2 (13 notes)
Prod notes drawn without a title (only an icon) are not read; a type is compared only when both sides have
at least `min_notes` titled notes of it (a single "Note" in an icon legend is no note style)."""
from __future__ import annotations

import re
from collections import Counter, defaultdict

import pymupdf

from ..model import Doc, Finding, Loc
from . import tables as tables_mod


def _hex(c) -> str:
    return "#%02X%02X%02X" % tuple(round(v * 255) for v in c[:3])


def _white(c) -> bool:
    return c is None or all(v > 0.96 for v in c[:3])


def look_of(page: pymupdf.Page, title: pymupdf.Rect, drawings: list) -> str:
    """How the note whose title sits at `title` is drawn: filled box / bar on the left / border / none."""
    cy = (title.y0 + title.y1) / 2
    fill = bar = border = None
    for p in drawings:
        q = p["rect"]
        if q.width >= 100 and q.x0 - 2 <= title.x0 and q.x1 + 2 >= title.x1 and q.y0 - 2 <= cy <= q.y1 + 2:
            if p.get("fill") is not None and not all(v > 0.99 for v in p["fill"][:3]):
                fill = _hex(p["fill"])
            elif p.get("fill") is None and p.get("color") is not None and not _white(p["color"]):
                border = _hex(p["color"])
        if q.width <= 5 and q.height >= title.height and 0 <= title.x0 - q.x1 <= 30 and q.y0 - 4 <= cy <= q.y1 + 4:
            c = p.get("fill") if p.get("fill") is not None else p.get("color")
            if c is not None and not _white(c):
                bar = _hex(c)
    parts = [f"filled box {fill}" if fill else "", f"bar on the left {bar}" if bar else "",
             f"border {border}" if border and not fill else ""]
    return " + ".join(p for p in parts if p) or "no box or bar"


def notes(doc: Doc, labels: list[str]) -> list[dict]:
    """Every titled note of the document: type, page, title box, look."""
    if not labels:
        return []
    title_re = re.compile(r"^(%s)\s*:?\s*$" % "|".join(map(re.escape, labels)), re.I)
    pdf = pymupdf.open(doc.path)
    # a legend row ("Symbol | Item | Meaning", "Warning" named in its own cell) names the label, it is not
    # a styled callout, even when the row itself carries a bar / fill of its own (the table's own style) -
    # unlike a real note box nested in a cell, which makes that row noticeably taller than the table's
    # others to fit the note's own explanatory lines; a plain label row stays the table's usual row height
    cells = defaultdict(list)  # page -> [(table bbox, [row bbox, ...])]
    try:
        for tb in tables_mod.tables(doc, (0, len(doc.words))):
            if tables_mod.is_data_table(doc, tb) or len(tb.rows) >= 3 or max(r.cells for r in tb.rows) >= 3:
                cells[tb.page].append((pymupdf.Rect(tb.bbox), [pymupdf.Rect(r.box) for r in tb.rows]))
    except Exception:
        pass  # a Doc without extracted words (a test's own stand-in): no table exclusion, nothing to lose
    out = []
    for pno, pg in enumerate(pdf):
        drawings = None
        for b in pg.get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                t = "".join(s["text"] for s in ln["spans"]).strip()
                m = title_re.match(t)
                if not m:
                    continue
                r = pymupdf.Rect(ln["bbox"])
                center = r.tl + (r.width / 2, r.height / 2)
                hosting = next(((tb_box, rows) for tb_box, rows in cells[pno] if tb_box.contains(center)), None)
                if hosting is not None:
                    row_boxes = hosting[1]
                    row = next((rb for rb in row_boxes if rb.contains(center)), None)
                    # the table's plain row height (its smallest row, not a median - half the rows can
                    # be note-tall and skew a median upward): a row much taller than that holds a real
                    # note box, not just this table's own ordinary row height
                    plain_h = min((rb.height for rb in row_boxes), default=None)
                    if row is None or plain_h is None or row.height <= 1.5 * plain_h:
                        continue  # an ordinary row of the table (its own style, or a plain label): not a note
                if drawings is None:
                    drawings = pg.get_drawings()
                out.append({"type": m.group(1).lower(), "page": pno, "bbox": tuple(r), "look": look_of(pg, r, drawings),
                            "key": _key_below(pg, r)})
    return out


def _words(t: str) -> list[str]:
    return re.findall(r"\w+", t.lower())


def _key_below(pg: pymupdf.Page, title: pymupdf.Rect) -> str:
    """The first words of the note's text: the line just under (or beside) its title."""
    best = None
    for b in pg.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            r = pymupdf.Rect(ln["bbox"])
            t = "".join(sp["text"] for sp in ln["spans"]).strip()
            if not t or r == title or r.y0 < title.y0 - 1 or r.y0 > title.y1 + 30 or r.x1 < title.x0:
                continue
            if best is None or (r.y0, r.x0) < (best[0].y0, best[0].x0):
                best = (r, t)
    return " ".join(_words(best[1])[:6]) if best else ""


def icon_notes(doc: Doc) -> list[dict]:
    """Notes without a title, only an icon (prod's “!” drawn as a circle with a bar and a dot, a pencil):
    a small square picture or drawing with a line of text starting just right of it. Per note: its first words
    (the key to the same note on the other side, which names its type), page, box of its first line, look."""
    pdf = pymupdf.open(doc.path)
    out = []
    for pno, pg in enumerate(pdf):
        drawings = pg.get_drawings()
        icons = [pymupdf.Rect(i["bbox"]) for i in pg.get_image_info()]
        icons += [p["rect"] for p in drawings if p.get("fill") is not None and not _white(p["fill"])]
        icons = [r for r in icons if 9 <= r.width <= 36 and 9 <= r.height <= 36 and 0.7 <= r.width / r.height <= 1.4]
        if not icons:
            continue
        lines = [(pymupdf.Rect(ln["bbox"]), "".join(sp["text"] for sp in ln["spans"]).strip())
                 for b in pg.get_text("dict")["blocks"] for ln in b.get("lines", [])]
        seen = set()
        for ic in icons:
            near = [(r, t) for r, t in lines if t and 0 <= r.x0 - ic.x1 <= 30 and r.y0 - 4 <= ic.y0 <= r.y1 + 4]
            if not near:
                continue
            r, t = min(near, key=lambda rt: (rt[0].y0, rt[0].x0))
            if len(_words(t)) < 3 or (round(r.x0), round(r.y0)) in seen:
                continue
            seen.add((round(r.x0), round(r.y0)))
            out.append({"page": pno, "bbox": tuple(r), "key": " ".join(_words(t)[:6]), "look": look_of(pg, r, drawings)})
    return out


_WORD = re.compile(r"\w+")


def _rgb(h: str) -> tuple[int, int, int]:
    return tuple(int(h[k:k + 2], 16) for k in (1, 3, 5))


def _parts(look: str) -> tuple[str | None, str | None]:
    """(fill colour, bar colour) of a look string."""
    fill = re.search(r"filled box (#\w{6})", look)
    bar = re.search(r"bar on the left (#\w{6})", look)
    return (fill.group(1) if fill else None), (bar.group(1) if bar else None)


def _same_look(a: str, b: str, tol: float) -> bool:
    """Two looks alike: the same parts (fill, bar), their colours within `tol` (RGB distance)."""
    (fa, ba), (fb, bb) = _parts(a), _parts(b)
    if bool(fa) != bool(fb) or bool(ba) != bool(bb):
        return False
    near = lambda x, y: x is None or sum((p - q) ** 2 for p, q in zip(_rgb(x), _rgb(y))) ** 0.5 <= tol
    return near(fa, fb) and near(ba, bb)


def panels(doc: Doc, labels: list[str]) -> list[dict]:
    """Shaded boxes without a note title (“Available when input source is set to Google TV.”): a filled,
    non-white rectangle holding at least 3 words of text. Table cells (a filled box beside another on the same
    row, or one crossed by grid lines) and titled notes (compared above) are left out. Per box: its text (the
    key to pair it with the other side), page, the box of its first line and its look."""
    title_re = re.compile(r"^(%s)\s*:?\s*$" % "|".join(map(re.escape, labels)), re.I) if labels else None
    pdf = pymupdf.open(doc.path)
    out = []
    for pno, pg in enumerate(pdf):
        drawings = pg.get_drawings()
        # a very pale panel (#F8F8F8) is still a panel: only (near) pure white is no fill here
        filled = [p["rect"] for p in drawings if p.get("fill") is not None and not all(v > 0.99 for v in p["fill"][:3])
                  and p["rect"].width >= 100 and 12 <= p["rect"].height <= 400]
        if not filled:
            continue
        try:
            tables = [pymupdf.Rect(t.bbox) for t in pg.find_tables().tables]
        except Exception:
            tables = []
        lines = [(pymupdf.Rect(ln["bbox"]), "".join(sp["text"] for sp in ln["spans"]).strip())
                 for b in pg.get_text("dict")["blocks"] for ln in b.get("lines", [])]
        for r in filled:
            if any((r & t).get_area() > 0.5 * r.get_area() for t in tables):
                continue  # a table's header row or shaded cells (table colours are their own check)
            if any(o is not r and abs(o.y0 - r.y0) < 2 and abs(o.y1 - r.y1) < 2 and (abs(o.x0 - r.x1) < 3 or abs(r.x0 - o.x1) < 3)
                   for o in filled):
                continue  # a table row: cells side by side
            if sum(1 for p in drawings if p["rect"].width <= 1.5 and p["rect"].height >= 0.5 * r.height
                   and r.x0 + 5 < p["rect"].x0 < r.x1 - 5 and r.y0 - 2 <= p["rect"].y0 <= r.y1) >= 1:
                continue  # crossed by a grid line: a table
            inside = [(lr, t) for lr, t in lines if t and r.contains(pymupdf.Point((lr.x0 + lr.x1) / 2, (lr.y0 + lr.y1) / 2))]
            text = " ".join(t for _, t in inside)
            if len(_WORD.findall(text)) < 3 or (title_re and any(title_re.match(t) for _, t in inside)):
                continue
            first = min((lr for lr, _ in inside), key=lambda q: (q.y0, q.x0))
            out.append({"page": pno, "bbox": tuple(first), "box": tuple(r), "text": text,
                        "key": " ".join(_WORD.findall(text.lower())[:5]), "look": look_of(pg, first, drawings)})
    return out


def compare_panels(A: Doc, B: Doc, cfg: dict) -> list[Finding]:
    """The same shaded box, prod against stage: its fill colour and its bar. One finding per pair of looks."""
    ncfg = cfg.get("notes") or {}
    labels = cfg["content"].get("label_words") or []
    tol = float(ncfg.get("panel_color_tolerance", 8))
    pb = defaultdict(list)
    for p in panels(B, labels):
        pb[p["key"]].append(p)
    groups: dict[tuple, list] = defaultdict(list)
    for a in panels(A, labels):
        if not pb.get(a["key"]):
            continue  # not shaded in stage, or not there: a missing box is reported by the content checks
        b = pb[a["key"]].pop(0)
        if not _same_look(a["look"], b["look"], tol):
            groups[(a["look"], b["look"])].append((a, b))
    out = []
    for (la, lb), pairs in groups.items():
        eg = pairs[0][0]["text"]
        eg = eg if len(eg) <= 70 else eg[:67].rsplit(" ", 1)[0] + " …"
        out.append(Finding(
            "style", ncfg.get("severity", "warning"),
            f"Shaded box style differs: prod — {la} · stage — {lb} ({len(pairs)} box(es), e.g. “{eg}”)",
            [Loc(a["page"], a["box"]) for a, _ in pairs][:3], [Loc(b["page"], b["box"]) for _, b in pairs][:3],
            {"kind": "panel-style", "baseline_look": la, "candidate_look": lb, "boxes": len(pairs),
             "pairs": [[a["page"], a["box"], b["page"], b["box"]] for a, b in pairs],
             "texts": [a["text"][:120] for a, _ in pairs]},
            types=["callout style"]))
    return out


def _in_order(na: list[dict], nb: list[dict]) -> list[tuple[dict, dict]]:
    """The n-th note of prod with the n-th of stage when both have as many (page numbers differ between the
    two layouts: stage p.22 is prod p.27); else by position in the document (page / page count)."""
    if len(na) == len(nb):
        return list(zip(na, nb))
    la, lb = (max((n["page"] for n in x), default=0) + 1 for x in (na, nb))
    used: set[int] = set()
    out = []
    for a in na:
        free = [k for k in range(len(nb)) if k not in used]
        if not free:
            break
        k = min(free, key=lambda k: abs(nb[k]["page"] / lb - a["page"] / la))
        used.add(k)
        out.append((a, nb[k]))
    return out


def _pair_by_position(na: list[dict], nb: list[dict]) -> list[tuple[dict, dict]]:
    """Pair each prod note with its nearest stage note of the same type (by page, then vertical
    position), not by raw list position: an extra/missing note on one side shifts every zip() pair
    after it, so outlier checks on a sequential pairing miss real mismatches past that point."""
    used_b: set[int] = set()
    pairs = []
    for a_note in na:
        best_k, best_d = None, None
        for k, b_note in enumerate(nb):
            if k in used_b:
                continue
            d = abs(b_note["page"] - a_note["page"]) * 10_000 + abs(b_note["bbox"][1] - a_note["bbox"][1])
            if best_d is None or d < best_d:
                best_k, best_d = k, d
        if best_k is not None:
            used_b.add(best_k)
            pairs.append((a_note, nb[best_k]))
    return pairs


def compare(A: Doc, B: Doc, cfg: dict) -> list[Finding]:
    ncfg = cfg.get("notes") or {}
    if not ncfg.get("compare_style", True):
        return []
    labels = cfg["content"].get("label_words") or []
    min_n = int(ncfg.get("min_notes", 2))
    by_a, by_b = defaultdict(list), defaultdict(list)
    na_all, nb_all = notes(A, labels), notes(B, labels)
    for n in na_all:
        by_a[n["type"]].append(n)
    for n in nb_all:
        by_b[n["type"]].append(n)
    # a prod note with only an icon (“!”, a pencil) is the type its stage twin is titled (“IMPORTANT”): the same
    # note text names it, so it is compared like a titled one
    if ncfg.get("icon_notes", True):
        types_b = {n["key"]: n["type"] for n in nb_all if n.get("key")}
        titled_a = {n["key"] for n in na_all if n.get("key")}
        for n in icon_notes(A):
            typ = types_b.get(n["key"])
            if typ and n["key"] not in titled_a:
                from . import callout_icons
                box = callout_icons.icon_left_of(A, Loc(n["page"], n["bbox"]))
                kind_ = callout_icons.kind(A, n["page"], box) if box else None
                by_a[typ].append({**n, "type": typ, "icon": True, "icon_kind": kind_})
        for typ in by_a:
            by_a[typ].sort(key=lambda n: (n["page"], n["bbox"][1]))
    out = []
    for typ in sorted(set(by_a) & set(by_b)):
        na, nb = by_a[typ], by_b[typ]
        # a prod note drawn with only its icon has no box or bar by design: its look is the icon, which the
        # callout-icon check compares with the label (pencil = Note, lightbulb = Tip). Not a box/bar style
        na = [n for n in na if not (n.get("icon") and n.get("icon_kind"))]
        # a lone note is compared when each side has the same number (one Warning in each): a single title
        # on one side only is more likely an icon legend than a note
        if (len(na) < min_n or len(nb) < min_n) and len(na) != len(nb):
            continue
        la, ca = Counter(n["look"] for n in na).most_common(1)[0]
        lb, cb = Counter(n["look"] for n in nb).most_common(1)[0]
        name = typ.capitalize()
        kind = "notes" if typ == "note" else f"{name} notes"  # “13 of 13 notes”, “14 of 14 Important notes”
        if la != lb:
            out.append(Finding(
                "style", ncfg.get("severity", "warning"),
                f"{name} style differs: prod — {la} ({ca} of {len(na)} {kind}) · stage — {lb} ({cb} of {len(nb)} {kind})",
                [Loc(n["page"], n["bbox"]) for n in na if n["look"] == la][:3],
                [Loc(n["page"], n["bbox"]) for n in nb if n["look"] == lb][:3],
                {"kind": "note-style", "note": typ, "baseline_look": la, "candidate_look": lb,
                 "baseline_notes": len(na), "candidate_notes": len(nb),
                 "pairs": [[a["page"], a["bbox"], b["page"], b["bbox"]] for a, b in
                           _in_order([n for n in na if n["look"] == la], [n for n in nb if n["look"] == lb])]},
                types=["callout style"]))
        # one-off outliers: a note whose own look does not match its paired note's look, even though
        # most notes of the type agree (so the finding above does not fire, or fires for a different
        # pair) - matched by page proximity (an extra/missing note on one side would otherwise shift
        # every zip(na, nb) pair after it out of alignment, so real outliers past that point go uncaught)
        if ncfg.get("compare_outliers", True):
            for a_note, b_note in _pair_by_position(na, nb):
                if a_note["look"] == b_note["look"] or (a_note["look"], b_note["look"]) == (la, lb):
                    continue
                out.append(Finding(
                    "style", ncfg.get("severity", "warning"),
                    f"{name} style differs here only: prod — {a_note['look']} · stage — {b_note['look']} "
                    f"(other {kind} match)",
                    [Loc(a_note["page"], a_note["bbox"])], [Loc(b_note["page"], b_note["bbox"])],
                    {"kind": "note-style-outlier", "note": typ, "baseline_look": a_note["look"],
                     "candidate_look": b_note["look"]},
                    types=["callout style"]))
    if ncfg.get("compare_panels", True):
        out += compare_panels(A, B, cfg)
    return out


def place(findings: list[Finding], A: Doc, units: list) -> dict:
    """Each document-wide note finding shown where its notes are: one finding per section holding some of
    them (that section's notes highlighted), not one finding in the first section only. Returns
    {unit id: [findings]}; a finding without pairs goes to the first section."""
    starts = []  # (page, y) where each section starts in prod
    for u in units:
        k = next((i for i in range(*u.a_range) if A.words[i].norm), None)
        starts.append((A.words[k].page, A.words[k].bbox[1]) if k is not None else (10 ** 6, 0))

    def unit_at(page: int, y: float):
        best = None
        for u, st in zip(units, starts):
            if st <= (page, y + 2) and (best is None or st >= best[1]):
                best = (u, st)
        return best[0] if best else (units[0] if units else None)
    out: dict = defaultdict(list)
    for f in findings:
        pairs = f.detail.get("pairs") or []
        if not pairs or not units:
            if units:
                out[units[0].id].append(f)
            continue
        groups: dict = defaultdict(list)
        for pa, ba, pb, bb in pairs:
            u = unit_at(pa, ba[1])
            if u is not None:
                groups[u.id].append((Loc(pa, tuple(ba)), Loc(pb, tuple(bb))))
        total = len(pairs)
        for uid, ls in groups.items():
            here = f"{len(ls)} here, {total} in the document"
            out[uid].append(Finding(
                f.check, f.severity, f"{f.message} — {here}",
                [a for a, _ in ls], [b for _, b in ls], {**{k: v for k, v in f.detail.items() if k != "pairs"}, "here": len(ls)},
                types=list(f.types), links=ls))
    return out
