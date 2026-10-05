"""Style ("CSS") check: font family, weight, italic, size and colour of the
same words on both sides, grouped by role and by the exact mismatch."""
from __future__ import annotations

from collections import OrderedDict, defaultdict

import numpy as np
import pymupdf

from ..model import SEVERITY_RANK, Finding, Style
from . import Unit, locs, paired_locs, snippet


def _rgb(hex_: str) -> tuple[int, int, int]:
    return int(hex_[1:3], 16), int(hex_[3:5], 16), int(hex_[5:7], 16)


def color_distance(a: str, b: str) -> float:
    return sum((x - y) ** 2 for x, y in zip(_rgb(a), _rgb(b))) ** 0.5


def diff(sa: Style, sb: Style, scfg: dict) -> tuple:
    props = set(scfg.get("properties", ["family", "weight", "italic", "size", "color"]))
    out = []
    if "family" in props and sa.family.lower() != sb.family.lower():
        out.append(("font-family", sa.family, sb.family))
    if "weight" in props and sa.weight != sb.weight:
        out.append(("font-weight", sa.weight, sb.weight))
    if "italic" in props and sa.italic != sb.italic:
        out.append(("font-style", "italic" if sa.italic else "normal", "italic" if sb.italic else "normal"))
    if "size" in props and abs(sa.size - sb.size) > scfg.get("size_tolerance", 0.5):
        out.append(("font-size", f"{sa.size:g}pt", f"{sb.size:g}pt"))
    if "color" in props and color_distance(sa.color, sb.color) > scfg.get("color_tolerance", 24):
        out.append(("color", sa.color, sb.color))
    return tuple(out)


def check(u: Unit) -> list[Finding]:
    scfg, rcfg = u.cfg["style"], u.cfg["report"]
    if not scfg.get("compare_with_prod", True):  # typography is checked on stage against the spec,
        if scfg.get("check_emphasis", True):     # but bold vs plain / italic vs upright is the text's own
            return _emphasis(u, scfg, rcfg)
        else:
            return []
    sev_map = scfg.get("severity", {})
    groups: dict[tuple, list[tuple[int, int]]] = defaultdict(list)
    for i, j in u.pairs + u.style_pairs:
        wa, wb = u.a.words[i], u.b.words[j]
        d = diff(wa.style, wb.style, scfg)
        if d:
            groups[(wa.role, d)].append((i, j))

    findings = []
    for (role, d), pairs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if len(pairs) < scfg.get("min_words", 1):
            continue
        sev = max((sev_map.get(p, "warning") for p, _, _ in d), key=SEVERITY_RANK.__getitem__)
        a_idx, b_idx = [p[0] for p in pairs], [p[1] for p in pairs]
        wa, wb = u.a.words[a_idx[0]], u.b.words[b_idx[0]]
        findings.append(Finding(
            "style", sev,
            f"[{role}] " + ", ".join(f"{p}: {x} → {y}" for p, x, y in d)
            + f" ({len(pairs)} words, e.g. “{snippet(u.a, a_idx, 8)}”)",
            locs(u.a, a_idx, rcfg["max_locs"]), locs(u.b, b_idx, rcfg["max_locs"]),
            {"role": role, "props": [{"property": p, "baseline": x, "candidate": y} for p, x, y in d],
             "words": len(pairs), "baseline_style": wa.style.css(), "candidate_style": wb.style.css()},
            links=paired_locs(u.a, u.b, pairs, rcfg["max_locs"]),
        ))
    return findings


_WEIGHT_NAME = {100: "Thin", 200: "ExtraLight", 300: "Light", 400: "Regular", 500: "Medium", 600: "SemiBold",
                700: "Bold", 800: "ExtraBold", 900: "Black"}
_GRAY: "OrderedDict[tuple, np.ndarray]" = OrderedDict()
_Z = 4.0


def _page_gray(path: str, page: int) -> "np.ndarray":
    key = (path, page)
    if key not in _GRAY:
        if len(_GRAY) >= 6:
            _GRAY.popitem(last=False)
        pix = pymupdf.open(path)[page].get_pixmap(matrix=pymupdf.Matrix(_Z, _Z), colorspace=pymupdf.csGRAY, alpha=False)
        _GRAY[key] = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.stride)[:, :pix.width]
    return _GRAY[key]


def _stroke(doc, w) -> float | None:
    """How thick the word's letter strokes are on the rendered page, in em: 2 x ink area / ink outline
    (a stroke of width s and length L has area s*L and an outline of about 2L). Ink is what differs from
    the word box's own background, so white text on a dark bar is measured too."""
    try:
        g = _page_gray(doc.path, w.page)
    except Exception:
        return None
    x0, y0, x1, y1 = (int(round(v * _Z)) for v in w.bbox)
    c = g[max(0, y0):max(0, y1), max(0, x0):max(0, x1)].astype(np.int16)
    if c.shape[0] < 4 or c.shape[1] < 4:
        return None
    bg = np.median(np.concatenate([c[0], c[-1], c[:, 0], c[:, -1]]))
    ink = np.abs(c - bg) > 90
    area = int(ink.sum())
    if area < 20:
        return None
    pad = np.pad(ink, 1)
    inner = pad[1:-1, 1:-1] & pad[:-2, 1:-1] & pad[2:, 1:-1] & pad[1:-1, :-2] & pad[1:-1, 2:]
    outline = int((ink & ~inner).sum())
    return (2 * area / max(outline, 1)) / (w.style.size * _Z)


def _stroke_ratio(A, wa, B, wb) -> float | None:
    """Stage stroke thickness / prod stroke thickness of the same word (None: not measurable)."""
    sa, sb = _stroke(A, wa), _stroke(B, wb)
    return sb / sa if sa and sb else None


def _emphasis(u: Unit, scfg: dict, rcfg: dict) -> list[Finding]:
    """Bold vs plain and italic vs upright of the same words, prod vs stage - also when fonts, sizes
    and colours are checked against the design spec only: emphasis marks what the reader must
    notice (a button name, a warning), so it is part of the text. Font-weight steps within
    plain (400 vs 500) or within bold (600 vs 800) are the spec's business."""
    bold = lambda st: st.weight >= 600
    # running text only - about the body size, not a heading: headings and titles are set by the
    # template (a display font regular in one PDF, bold in the other: the spec's business), tiny
    # figure numbers are part of the artwork
    body = max(u.a.body_size or 0, 1)
    # a word of the running text: letters (not a list number "3." or a figure number), not a callout label
    # ("Notes:", "Warning" - folded to <label:…>), at body size (sub-headings a size up are the spec's)
    text = lambda w: (not w.role.startswith("h") and 0.8 * body <= w.style.size <= 1.1 * body
                      and any(c.isalpha() for c in w.text) and not (w.norm or "").startswith("<label:"))
    # the font's declared weight is not always what the reader sees: "Medium" can look bold next to
    # "Regular", and a font named Bold can render as thin as a Medium. When the declared weights of a
    # word differ, the letter strokes are measured on the rendered pages and decide.
    up, down = scfg.get("emphasis_visual_ratio", 1.25), 1 / scfg.get("emphasis_visual_ratio", 1.25)
    same = scfg.get("emphasis_same_ratio", 1.14)
    groups: dict[tuple, list[tuple[int, int]]] = defaultdict(list)
    ratio: dict[tuple[int, int], float] = {}
    any_weight = scfg.get("emphasis_any_weight", True)
    for i, j in u.pairs + u.style_pairs:
        wa, wb = u.a.words[i], u.b.words[j]
        sa, sb = wa.style, wb.style
        if not text(wa):
            continue
        if any_weight:
            # every font-weight step of the running text counts (Regular -> Medium, Light -> Regular), not only
            # bold vs plain; the letter strokes are measured for the report, they do not hide a difference
            if sa.weight != sb.weight or sa.italic != sb.italic:
                if sa.weight != sb.weight and sum(c.isalpha() for c in wa.text) >= 3:
                    r = _stroke_ratio(u.a, wa, u.b, wb)
                    if r is not None:
                        ratio[(i, j)] = r
                groups[(wa.role, sa.weight, sb.weight, sa.italic, sb.italic)].append((i, j))
            continue
        ba, bb = bold(sa), bold(sb)
        if sa.weight != sb.weight and sum(c.isalpha() for c in wa.text) >= 3:
            r = _stroke_ratio(u.a, wa, u.b, wb)
            if r is not None:
                ratio[(i, j)] = r
                if r >= up:
                    ba, bb = False, True       # stage visibly bolder
                elif r <= down:
                    ba, bb = True, False       # stage visibly lighter
                elif 1 / same < r < same:
                    bb = ba                    # the two look the same weight: not a difference the reader sees
        if ba != bb or sa.italic != sb.italic:
            groups[(wa.role, 700 if ba else 400, 700 if bb else 400, sa.italic, sb.italic)].append((i, j))
    wname = lambda w: _WEIGHT_NAME.get(w, str(w))
    name = lambda w, it: f"{wname(w)} ({w})" + (" italic" if it else "")
    plain = lambda w, it: ("bold" if w >= 600 else "plain") + (" italic" if it else "")
    findings = []
    for (role, wa_, wb_, ia, ib), pairs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if len(pairs) < scfg.get("min_words", 1):
            continue
        pairs = list(dict.fromkeys(pairs))
        a_idx, b_idx = [p[0] for p in pairs], [p[1] for p in pairs]
        sa0, sb0 = u.a.words[a_idx[0]].style, u.b.words[b_idx[0]].style
        rs = sorted(ratio[p] for p in pairs if p in ratio)
        seen = (f"; letter strokes ×{rs[len(rs) // 2]:.2f} {'thicker' if rs[len(rs) // 2] > 1 else 'thinner'} in stage "
                f"(measured on the pages)") if rs else ""
        what = ("Font weight and style differ" if wa_ != wb_ and ia != ib else
                "Font weight differs" if wa_ != wb_ else "Italic differs")
        props = ([{"property": "font-weight", "baseline": name(wa_, False), "candidate": name(wb_, False)}]
                 if wa_ != wb_ else []) + \
                ([{"property": "font-style", "baseline": "italic" if ia else "normal", "candidate": "italic" if ib else "normal"}]
                 if ia != ib else [])
        findings.append(Finding(
            "style", scfg.get("emphasis_severity", "error"),
            # plain <-> bold said in words first (what the reader sees), then the exact weights
            (f"[{role}] {what}: {plain(wa_, ia)} in prod → {plain(wb_, ib)} in stage ({name(wa_, ia)} → {name(wb_, ib)}; "
             if (wa_ >= 600) != (wb_ >= 600) else f"[{role}] {what}: {name(wa_, ia)} in prod → {name(wb_, ib)} in stage (")
            + f"{sa0.family} → {sb0.family}{seen}) ({len(pairs)} words, e.g. “{snippet(u.a, a_idx, 8)}”)",
            locs(u.a, a_idx, rcfg["max_locs"]), locs(u.b, b_idx, rcfg["max_locs"]),
            {"role": role, "props": props, "words": len(pairs), "kind": "emphasis",
             "baseline_weight": wa_, "candidate_weight": wb_,
             "stroke_ratio": round(rs[len(rs) // 2], 2) if rs else None,
             "baseline_style": u.a.words[a_idx[0]].style.css(), "candidate_style": u.b.words[b_idx[0]].style.css()},
            types=["emphasis"], links=paired_locs(u.a, u.b, pairs, rcfg["max_locs"])))
    return findings
