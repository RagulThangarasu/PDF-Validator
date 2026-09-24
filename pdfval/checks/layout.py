"""Alignment / layout check on lines whose first word matched textually.

* indent      – line x-offset from the page's content-box left edge
* text-align  – left / center / right relative to the content box
* line-height – baseline-to-baseline distance of consecutive matched lines (em)
* space-above – gap above the section heading (em)
* line-wrap   – the text is the same but wraps to the next line at a different
                word (or a word is hyphenated/split over two lines on one side
                only). Content treats these as a match; they are layout issues.

Offsets are measured relative to each document's own margins, so the check
works even when page size and margins differ between the two PDFs.
"""
from __future__ import annotations

from collections import defaultdict

from ..model import Doc, Finding, Line
from . import Unit, locs
from .content import same_row, visible_break


def text_align(doc: Doc, ln: Line, tol: float) -> str:
    left, right = doc.left(ln.page), doc.right(ln.page)
    x0, x1 = ln.bbox[0], ln.bbox[2]
    if abs(x0 - left) <= tol or x0 < left or (x1 - x0) > 0.7 * (right - left):
        return "left"  # flush-left, or a (near) full-width line: left/justified
    if abs((x0 + x1) / 2 - (left + right) / 2) <= tol:
        return "center"
    if abs(x1 - right) <= tol:
        return "right"
    return "left"  # indented


def _gap_above(doc: Doc, li: int) -> float | None:
    ln = doc.lines[li]
    if li == 0 or doc.lines[li - 1].page != ln.page:
        return None
    return (ln.bbox[1] - doc.lines[li - 1].bbox[3]) / max(ln.size, 1)


def check(u: Unit) -> list[Finding]:
    lcfg, rcfg = u.cfg["layout"], u.cfg["report"]
    if not lcfg.get("enabled", True):
        return []
    tol_i, tol_a, tol_lh = lcfg["indent_tolerance"], lcfg["align_tolerance"], lcfg["line_height_tolerance_em"]
    A, B = u.a, u.b
    starts = [(i, j) for i, j in u.pairs if A.words[i].line_start and B.words[j].line_start]
    line_map = {A.words[i].line: B.words[j].line for i, j in starts}
    groups: dict[tuple, list[tuple[int, int]]] = defaultdict(list)

    for i, j in starts:
        wa, wb = A.words[i], B.words[j]
        la, lb = A.lines[wa.line], B.lines[wb.line]
        ia, ib = la.bbox[0] - A.left(la.page), lb.bbox[0] - B.left(lb.page)
        ca, cb = text_align(A, la, tol_a), text_align(B, lb, tol_a)
        if ca != cb:
            groups[(wa.role, "text-align", ca, cb)].append((i, j))
        elif ca == "left" and abs(ia - ib) > tol_i:
            groups[(wa.role, "indent", f"{2 * round(ia / 2):g}pt", f"{2 * round(ib / 2):g}pt")].append((i, j))
        # line-height: previous line also matched on both sides, same page
        pa, pb = wa.line - 1, wb.line - 1
        if pa >= 0 and line_map.get(pa) == pb and A.lines[pa].page == la.page and B.lines[pb].page == lb.page:
            lha = (la.bbox[3] - A.lines[pa].bbox[3]) / max(la.size, 1)
            lhb = (lb.bbox[3] - B.lines[pb].bbox[3]) / max(lb.size, 1)
            if 0 < lha < 3 and 0 < lhb < 3 and abs(lha - lhb) > tol_lh:
                groups[(wa.role, "line-height", f"{lha:.1f}em", f"{lhb:.1f}em")].append((i, j))

    findings = _wraps(u)
    for (role, prop, x, y), pairs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if len(pairs) < lcfg.get("min_lines", 1):
            continue
        findings.append(Finding(
            "layout", lcfg.get("severity", {}).get(prop, "warning"),
            f"[{role}] {prop}: {x} → {y} ({len(pairs)} lines)",
            locs(A, [p[0] for p in pairs], rcfg["max_locs"]), locs(B, [p[1] for p in pairs], rcfg["max_locs"]),
            {"role": role, "property": prop, "baseline": x, "candidate": y, "lines": len(pairs)},
            types=[prop],
        ))

    # spacing above the section heading
    if u.a_anchor and u.b_anchor and u.a_anchor.located and u.b_anchor.located:
        wa, wb = A.words[u.a_anchor.word], B.words[u.b_anchor.word]
        ga, gb = _gap_above(A, wa.line), _gap_above(B, wb.line)
        if ga is not None and gb is not None and abs(ga - gb) > lcfg["heading_space_tolerance_em"]:
            findings.append(Finding(
                "layout", lcfg.get("severity", {}).get("space-above", "warning"),
                f"[heading] space-above: {ga:.1f}em → {gb:.1f}em",
                [locs(A, [u.a_anchor.word])[0]], [locs(B, [u.b_anchor.word])[0]],
                {"property": "space-above", "baseline": round(ga, 2), "candidate": round(gb, 2)},
            ))
    return findings


def _wraps(u: Unit) -> list[Finding]:
    """Same text, wrapped to the next line at a different place on one side.
    Two consecutive matched words with a line break between them on one side only
    (not a paragraph break - that is content), plus the words the content check
    found split over lines differently ("config-/uration", "SL6504/ / SL7504/").
    One finding per role; each place is marked by the words either side of the break."""
    lcfg, rcfg = u.cfg["layout"], u.cfg["report"]
    if not lcfg.get("check_wrap", True):
        return []
    A, B = u.a, u.b
    places: dict[str, list[tuple[list[int], list[int], str]]] = defaultdict(list)
    seen = set()
    pairs = sorted(u.pairs)
    for (i, j), (i2, j2) in zip(pairs, pairs[1:]):
        if i2 != i + 1 or j2 != j + 1:
            continue
        a_wrap, b_wrap = not same_row(A, i, i2), not same_row(B, j, j2)  # visual rows, not extracted lines
        if a_wrap == b_wrap or visible_break(A, i, i2) or visible_break(B, j, j2):
            continue
        side = "prod" if a_wrap else "stage"
        places[A.words[i].role].append(([i, i2], [j, j2], f"“{A.words[i].text} / {A.words[i2].text}” wraps in {side} only"))
        seen.update((i, i2))
    for a_idx, b_idx in u.wraps:
        if a_idx and b_idx and not seen.intersection(a_idx):
            text = " ".join(A.words[k].text for k in a_idx)
            places[A.words[a_idx[0]].role].append((a_idx, b_idx, f"“{text}” is split over lines differently"))

    findings = []
    for role, ps in sorted(places.items(), key=lambda kv: -len(kv[1])):
        if len(ps) < lcfg.get("min_lines", 1):
            continue
        a_locs = [l for a, _, _ in ps for l in locs(A, a)][: rcfg["max_locs"]]
        b_locs = [l for _, b, _ in ps for l in locs(B, b)][: rcfg["max_locs"]]
        examples = [m for _, _, m in ps]
        findings.append(Finding(
            "layout", lcfg.get("severity", {}).get("line-wrap", "info"),
            f"[{role}] line-wrap: same text wraps to the next line at a different place ({len(ps)}): "
            + "; ".join(examples[:3]) + (" …" if len(ps) > 3 else ""),
            a_locs, b_locs,
            {"role": role, "property": "line-wrap", "places": len(ps), "examples": examples[:50]},
            types=["line-wrap"],
        ))
    return findings
