"""Text left highlighted on the web page.

A word of the guide drawn on top of a coloured inline background - a <mark>, or a span given a
background-colour - is editing residue: someone highlighted a phrase while working on the topic and
the highlight was published with it. It is read from the DOM in `html_source.py` (`Style.bg`), so
only a crawled site can produce it; a PDF leaves `bg` empty and this check finds nothing.

Backgrounds that belong to the design are not highlights and never reach here: `bgOf` only looks at
*inline* ancestors, so a NOTE / WARNING callout's panel, a table header bar or a card is skipped.

Reported per run of consecutive words sharing one background colour, as "text highlighted".
"""
from __future__ import annotations

from ..model import Finding, Loc
from . import Unit, locs, snippet


def _runs(u: Unit):
    """Consecutive stage words sharing one highlight colour -> (colour, [word indices])."""
    out, cur, colour = [], [], ""
    for j in range(*u.b_range):
        bg = u.b.words[j].style.bg
        if bg and bg == colour and cur and j == cur[-1] + 1:
            cur.append(j)
            continue
        if cur:
            out.append((colour, cur))
        cur, colour = ([j], bg) if bg else ([], "")
    if cur:
        out.append((colour, cur))
    return out


def check(u: Unit) -> list[Finding]:
    cfg = u.cfg.get("highlights", {})
    if not cfg.get("enabled", True):
        return []
    ignore = {c.strip().lower() for c in cfg.get("ignore_colors", []) if c.strip()}
    max_words = cfg.get("max_words", 0) or None  # a highlight longer than this is a design background, not residue
    sev = cfg.get("severity", "error")
    limit = u.cfg["report"]["max_locs"]
    back = {b: a for a, b in u.pairs + u.style_pairs}

    findings = []
    for colour, idxs in _runs(u):
        if colour.lower() in ignore or (max_words and len(idxs) > max_words):
            continue
        phrase = snippet(u.b, idxs)
        page = u.b.words[idxs[0]].page + 1
        where = f"(stage p.{page})"
        at = None
        for j in idxs:  # the same spot in prod, so the report can show both sides
            if j in back:
                w = u.a.words[back[j]]
                at = Loc(w.page, w.bbox)
                break
        findings.append(Finding(
            "content", sev,
            f"Text highlighted in stage: “{phrase}” is drawn on a {colour} highlight {where}",
            [], locs(u.b, idxs, limit),
            {"kind": "text-highlight", "color": colour, "words": len(idxs), "text": phrase,
             "expected": "No highlight behind the text",
             "actual": f"{colour} highlight behind “{phrase}”"},
            baseline_at=at, types=["text highlight"]))
    return findings
