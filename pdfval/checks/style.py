"""Style ("CSS") check: font family, weight, italic, size and colour of the
same words on both sides, grouped by role and by the exact mismatch."""
from __future__ import annotations

from collections import defaultdict

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
