"""Bold label run into its text: a short bold line in prod (“Dynamic Black”) with its plain text on the
next line, where stage sets both on one line (“Dynamic Black: Adjusts frame content …”)."""
from __future__ import annotations

from ..model import Finding
from . import Unit, locs, snippet
from . import tables as tables_mod


def check(u: Unit) -> list[Finding]:
    ccfg = u.cfg["content"]
    if not ccfg.get("check_label_joined", True):
        return []
    A, B = u.a, u.b
    a2b = dict(u.pairs)
    bold = lambda w: w.style.weight >= 600
    in_table = lambda d, w: any(b[0] <= w.bbox[0] <= b[2] and b[1] <= w.bbox[1] <= b[3]
                                for _, b, _, _ in tables_mod._raw(d, w.page))
    lines: dict[int, list[int]] = {}
    for i in range(*u.a_range):
        if any(c.isalnum() for c in A.words[i].norm):  # list markers (•, 1.) are not part of a label
            lines.setdefault(A.words[i].line, []).append(i)
    order = sorted(lines, key=lambda li: (A.words[lines[li][0]].page, A.lines[li].bbox[1], A.lines[li].bbox[0]))
    findings = []
    for li, nxt in zip(order, order[1:]):
        lab, txt = lines[li], lines[nxt]
        wl, wt = A.words[lab[-1]], A.words[txt[0]]
        if len(lab) > 8 or not all(bold(A.words[i]) for i in lab) or bold(wt) or wl.page != wt.page:
            continue
        if not 0 < A.lines[nxt].bbox[1] - A.lines[li].bbox[3] < 2 * wl.style.size:  # the very next line
            continue
        if abs(A.lines[nxt].bbox[0] - A.lines[li].bbox[0]) > 3 * wl.style.size or in_table(A, wl):
            continue
        # stage may glue punctuation to the label (“Black:”), so use its last paired word
        j = next((a2b[i] for i in reversed(lab) if i in a2b), None)
        m = next((a2b[i] for i in txt[:3] if i in a2b), None)
        if j is None or m is None or B.words[j].line != B.words[m].line:
            continue
        label = snippet(A, lab)
        joined = [k for k in range(max(0, j - len(lab) + 1), min(len(B.words), m + 6)) if B.words[k].line == B.words[j].line]
        findings.append(Finding(
            "content", ccfg.get("label_joined_severity", "warning"),
            f"Bold label joined with its text in stage: “{label}” is on its own line in prod with “{snippet(A, txt[:6])}” "
            f"on the next line; stage runs them into one line: “{snippet(B, joined)}”",
            locs(A, lab + txt[:1]), locs(B, joined), {"kind": "label joined", "label": label}, types=["label joined"]))
    return findings
