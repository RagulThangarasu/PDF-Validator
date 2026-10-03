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
            if p.get("fill") is not None and not _white(p["fill"]):
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
    out = []
    for pno, pg in enumerate(pdf):
        drawings = None
        for b in pg.get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                t = "".join(s["text"] for s in ln["spans"]).strip()
                m = title_re.match(t)
                if not m:
                    continue
                if drawings is None:
                    drawings = pg.get_drawings()
                r = pymupdf.Rect(ln["bbox"])
                out.append({"type": m.group(1).lower(), "page": pno, "bbox": tuple(r), "look": look_of(pg, r, drawings)})
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
    for n in notes(A, labels):
        by_a[n["type"]].append(n)
    for n in notes(B, labels):
        by_b[n["type"]].append(n)
    out = []
    for typ in sorted(set(by_a) & set(by_b)):
        na, nb = by_a[typ], by_b[typ]
        if len(na) < min_n or len(nb) < min_n:
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
                 "baseline_notes": len(na), "candidate_notes": len(nb)},
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
    return out
