"""A hole between two words: several spaces in stage where prod has one.

The text diff already calls a missing or added single space a word-gap difference, and on a real
manual that fires on almost every page without telling the reader anything. What a reader does see is
a hole - three or four character widths of white between two words, left by a tab, a stretched
justification or spaces typed to position the text.

So this check looks at the gap itself, not at the characters around it: every word paired between the
two PDFs carries the number of whitespace characters before the next word on its line
(Word.space_after), and a pair is reported only when that count differs by at least
[content] gap_min_chars. One space more or less never reaches the report.

Off by default ([content] check_wide_gap); the PDF-to-PDF profile turns it on.
"""
from __future__ import annotations

from collections import defaultdict

from ..model import Finding, Loc
from . import Unit, locs


def check(u: Unit) -> list[Finding]:
    ccfg = u.cfg.get("content") or {}
    if not ccfg.get("check_wide_gap", False):
        return []
    least = int(ccfg.get("gap_min_chars", 3))
    sev = ccfg.get("gap_severity", "warning")
    wider: list[tuple[int, int, int, int]] = []   # prod word, stage word, prod spaces, stage spaces
    for i, j in u.pairs:
        wa, wb = u.a.words[i], u.b.words[j]
        sa, sb = wa.space_after, wb.space_after
        if sa is None or sb is None:        # one of them ends its line: no gap to compare
            continue
        if abs(sb - sa) >= least:
            wider.append((i, j, sa, sb))
    if not wider:
        return []
    # one finding per page pair, so a run of holes in one table reads as one issue
    by_page: dict[tuple[int, int], list] = defaultdict(list)
    for i, j, sa, sb in wider:
        by_page[(u.a.words[i].page, u.b.words[j].page)].append((i, j, sa, sb))
    out = []
    rcfg = u.cfg["report"]
    for (pa, pb), items in sorted(by_page.items()):
        eg = "; ".join(f"after “{u.a.words[i].text}”: {sa} → {sb} spaces" for i, j, sa, sb in items[:3])
        more = f" (+{len(items) - 3} more)" if len(items) > 3 else ""
        out.append(Finding(
            "content", sev,
            f"Word gap differs: {len(items)} place(s) where stage leaves a hole of "
            f"{min(abs(sb - sa) for *_, sa, sb in items)}+ character widths "
            f"(prod p.{pa + 1} ↔ stage p.{pb + 1}): {eg}{more}",
            locs(u.a, [i for i, *_ in items], rcfg["max_locs"]),
            locs(u.b, [j for _, j, *_ in items], rcfg["max_locs"]),
            {"kind": "wide gap", "places": len(items),
             "baseline_spaces": [sa for *_, sa, _ in items][:10],
             "candidate_spaces": [sb for *_, sb in items][:10]},
            types=["wide gap"]))
    return out
