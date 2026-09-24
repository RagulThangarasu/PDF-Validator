"""Content check: word-level diff of the section text (reading order).

Content is strict about the *text*: words, case, punctuation (. , : quotes
dashes) and the spacing between words on the same line (double space, missing
space). It ignores everything that is presentation: font weight/colour/size
(→ style check), line wrapping, hyphenation at a line break, bullet glyphs.
Text that is all there but wraps to the next line at a different place
("SL6504/SL7504/" split over two lines on one side only) is a match here and
is handed to the layout check (unit.wraps) as a line-wrap difference.

The section verdict is a percentage, not a count of diffs:
  content match % = prod words present unchanged in stage – in order, moved as
                    a block, or reordered (e.g. table cells extracted in another
                    order) – minus spacing errors, divided by prod words.
A diff block of ≥ content.critical_missing_words prod words is CRITICAL only if
its words are genuinely absent from stage (not merely in another order).
"""
from __future__ import annotations

import re
from collections import Counter
from difflib import SequenceMatcher

from ..model import Finding
from . import Unit, insertion_loc, locs, snippet

_KIND = {"replace": "Changed text", "delete": "Missing text", "insert": "Extra text"}
_TYPE_LABEL = {"case": "Uppercase/lowercase differs", "punctuation": "Punctuation differs",
               "case + punctuation": "Uppercase/lowercase and punctuation differ", "spacing": "Word gap differs"}


def classify(a: list[str], b: list[str]) -> str:
    """Type of a replaced text run: spacing / case / punctuation / case + punctuation / changed text."""
    A, B = " ".join(a), " ".join(b)
    nospace = lambda t: re.sub(r"\s+", "", t)
    nopunct = lambda t: re.sub(r"[^\w\s]|_", "", t)
    if len(a) == len(b) == 1 and A != B and A.replace("-", "") == B.replace("-", ""):
        return "hyphenation"  # "config-uration" vs "configuration": a line-break hyphen, not content
    if nospace(A) == nospace(B):
        return "spacing"  # "details,see" vs "details, see", "470- 635" vs "470-635"
    if A.lower() == B.lower():
        return "case"
    if nospace(nopunct(A)) == nospace(nopunct(B)):
        return "punctuation"
    if nospace(nopunct(A)).lower() == nospace(nopunct(B)).lower():
        return "case + punctuation"
    return "changed text"


def _wrap_only(u: Unit, a_idx: list[int], b_idx: list[int]) -> bool:
    """Same characters on both sides, and every place where the split into words
    differs is a line break right after a hyphen/slash on the side that has the
    split ("SL6504/" | "SL7504/", "Cortex-" | "A73"): the text is present, it only
    wraps to the next line somewhere else (layout, not content). A break anywhere
    else stands for a space, so "code." | "The" vs "code.The" stays a content diff."""
    def bounds(doc, idx):
        out, off = {}, 0
        for k, i in enumerate(idx[:-1]):
            t = re.sub(r"\s+", "", doc.words[i].norm)
            off += len(t)
            out[off] = doc.words[i].line != doc.words[idx[k + 1]].line and len(t) > 1 and t[-1] in "-/–—"
        return out
    if "".join(u.a.words[i].norm for i in a_idx).replace(" ", "") != "".join(u.b.words[j].norm for j in b_idx).replace(" ", ""):
        return False
    ba, bb = bounds(u.a, a_idx), bounds(u.b, b_idx)
    diff = [ba[o] for o in ba.keys() - bb.keys()] + [bb[o] for o in bb.keys() - ba.keys()]
    return bool(diff) and all(diff)


def _space(n: int | None) -> str:
    return "no space" if n == 0 else "1 space" if n == 1 else f"{n} spaces"


def check(u: Unit) -> list[Finding]:
    ccfg, rcfg = u.cfg["content"], u.cfg["report"]
    ai = [i for i in range(*u.a_range) if u.a.words[i].norm]
    bi = [i for i in range(*u.b_range) if u.b.words[i].norm]
    at = [u.a.words[i].norm for i in ai]
    bt = [u.b.words[i].norm for i in bi]

    sm = SequenceMatcher(None, at, bt, autojunk=False)
    u.similarity = sm.ratio() if (at or bt) else 1.0
    ops = sm.get_opcodes()
    moved = _moved_blocks(ops, at, bt)
    crit_words = ccfg.get("critical_missing_words", 8)
    sev = ccfg.get("severity", "warning")
    # stage words not in any equal/moved block: the pool that reordered prod words are found in
    pool = Counter()
    for n, (tag, i1, i2, j1, j2) in enumerate(ops):
        if tag in ("insert", "replace") and n not in moved:
            pool.update(bt[j1:j2])
    unmatched_a: Counter = Counter()
    findings = []
    matched = moved_words = hyphen_matched = 0
    for n, (tag, i1, i2, j1, j2) in enumerate(ops):
        if tag == "equal":
            u.pairs.extend(zip(ai[i1:i2], bi[j1:j2]))
            matched += i2 - i1
            continue
        if n in moved:
            if tag == "delete":  # report a move once, from the baseline side
                m1, m2 = moved[n]
                a_idx, b_idx = ai[i1:i2], bi[m1:m2]
                u.pairs.extend(zip(a_idx, b_idx))
                matched += len(a_idx)
                moved_words += len(a_idx)
                findings.append(Finding(
                    "content", ccfg.get("reorder_severity", "info"),
                    f"Reordered: “{snippet(u.a, a_idx)}”",
                    locs(u.a, a_idx, rcfg["max_locs"]), locs(u.b, b_idx, rcfg["max_locs"]),
                    {"op": "move", "baseline_text": snippet(u.a, a_idx, 200), "words": len(a_idx)},
                    types=["reordered"]))
            continue
        unmatched_a.update(at[i1:i2])
        if max(i2 - i1, j2 - j1) < ccfg.get("min_diff_words", 1):
            continue
        a_idx, b_idx = ai[i1:i2], bi[j1:j2]
        absent = sum((Counter(at[i1:i2]) - pool).values())  # prod words of this block found nowhere in stage
        # insertion point on the empty side: between the matched words around the gap
        a_at = insertion_loc(u.a, ai[i1 - 1] if i1 > 0 else None, ai[i1] if i1 < len(ai) else None) if not a_idx else None
        b_at = insertion_loc(u.b, bi[j1 - 1] if j1 > 0 else None, bi[j1] if j1 < len(bi) else None) if not b_idx else None
        critical = tag in ("delete", "replace") and absent >= crit_words and absent >= 0.5 * len(a_idx)
        ctype = ("missing text" if tag == "delete" or critical else "extra text" if tag == "insert"
                 else classify(at[i1:i2], bt[j1:j2]))
        if ctype == "hyphenation" or (ctype == "spacing" and _wrap_only(u, a_idx, b_idx)):
            # a word split / wrapped across lines on one side only: same text, a layout difference
            u.pairs.extend(zip(a_idx, b_idx))
            u.wraps.append((a_idx, b_idx))
            unmatched_a.subtract(at[i1:i2])
            pool.subtract(bt[j1:j2])
            hyphen_matched += i2 - i1
            continue
        label = "Missing content block" if critical else _TYPE_LABEL.get(ctype, _KIND[tag])
        findings.append(Finding(
            "content", "error" if critical else sev,
            f"{label}: "
            + (f"“{snippet(u.a, a_idx)}”" if a_idx else "")
            + (" → " if a_idx and b_idx else "") + (f"“{snippet(u.b, b_idx)}”" if b_idx else ""),
            locs(u.a, a_idx, rcfg["max_locs"]), locs(u.b, b_idx, rcfg["max_locs"]),
            {"op": tag, "baseline_text": snippet(u.a, a_idx, 200), "candidate_text": snippet(u.b, b_idx, 200),
             "words": max(i2 - i1, j2 - j1), "absent_words": absent},
            baseline_at=a_at, candidate_at=b_at, critical=critical, types=[ctype],
        ))

    spacing = _spacing(u, findings) if ccfg.get("check_spacing", True) else 0
    spacing += _paragraphs(u, findings) if ccfg.get("check_paragraphs", True) else 0
    unmatched_a, pool = +unmatched_a, +pool  # drop zero counts left by hyphenation matches
    matched += hyphen_matched
    reordered = sum((unmatched_a & pool).values())  # present in stage, only in another order
    missing = sum((unmatched_a - pool).values())
    extra = sum((pool - unmatched_a).values())
    matched += reordered
    total = len(at)
    u.content = {
        "baseline_words": total,
        "candidate_words": len(bt),
        "matched_words": matched,
        "missing_words": missing,
        "extra_words": extra,
        "moved_words": moved_words + reordered,
        "spacing_issues": spacing,
        "match_pct": round(100.0 * max(matched - spacing, 0) / total, 2) if total else (100.0 if not bt else 0.0),
    }
    return findings


def _spacing(u: Unit, findings: list[Finding]) -> int:
    """Whitespace between two consecutive words that are identical on both sides
    and sit on the same line on both sides (line wrapping is layout, not content)."""
    A, B = u.a.words, u.b.words
    sev = u.cfg["content"].get("spacing_severity", "warning")
    pairs = sorted(u.pairs)
    count = 0
    for (i, j), (i2, j2) in zip(pairs, pairs[1:]):
        if i2 != i + 1 or j2 != j + 1:
            continue
        sa, sb = A[i].space_after, B[j].space_after
        if sa is None or sb is None or sa == sb:
            continue
        if u.cfg["content"].get("spacing_mode", "exact") == "presence" and bool(sa) == bool(sb):
            continue  # one space vs two is invisible in a browser
        count += 1
        findings.append(Finding(
            "content", sev,
            f"Word gap differs: “{A[i].text}{' ' * max(sa, 0)}{A[i2].text}” ({_space(sa)}) → "
            f"“{B[j].text}{' ' * max(sb, 0)}{B[j2].text}” ({_space(sb)})",
            locs(u.a, [i, i2]), locs(u.b, [j, j2]),
            {"op": "spacing", "baseline_spaces": sa, "candidate_spaces": sb, "words": 1}, types=["spacing"]))
    return count


def _blk(d, i):
    return d.lines[d.words[i].line].block


def same_row(d, i, k) -> bool:
    """Words i and k visually on the same row (vertical overlap)."""
    a, b = d.words[i].bbox, d.words[k].bbox
    return d.words[i].page == d.words[k].page and min(a[3], b[3]) - max(a[1], b[1]) > 0.5 * min(a[3] - a[1], b[3] - b[1])


def runs_to_margin(d, i) -> bool:
    """Word i's line ends near the right margin: its text wrapped, the paragraph did not end."""
    ln = d.lines[d.words[i].line]
    left, right = d.left(ln.page), d.right(ln.page)
    return right > left and ln.bbox[2] >= right - 0.12 * (right - left)


def visible_break(d, i, k) -> bool:
    """A new paragraph between words i and k that looks like one: new text block, new row,
    and either the previous text ends a sentence or the next word starts one (capital,
    digit, symbol) after a line that stopped short of the margin.
    A plain line wrap ("first" / "time", "the" / "Notifications") is none of these."""
    if _blk(d, i) == _blk(d, k) or same_row(d, i, k):
        return False
    prev, nxt = d.words[i].text, d.words[k].text
    if prev[-1:] in ".:;!?)":
        return True
    return not nxt[:1].islower() and not runs_to_margin(d, i)


def _paragraphs(u: Unit, findings: list[Finding]) -> int:
    """Paragraph breaks (the gap between paragraphs) present on one side only.
    Two consecutive words identical on both sides: a paragraph boundary between
    them in prod, while in stage they run on in the same line (or the reverse).
    Requiring 'same line' on the joined side keeps line wrapping out of it."""
    A, B = u.a, u.b
    sev = u.cfg["content"].get("paragraph_severity", "warning")

    def joined(d, i, k) -> bool:  # same row AND a normal word gap (not two table cells level with each other)
        return same_row(d, i, k) and d.words[k].bbox[0] - d.words[i].bbox[2] <= 2 * d.words[i].style.size
    pairs = sorted(u.pairs)
    count = 0
    for (i, j), (i2, j2) in zip(pairs, pairs[1:]):
        if i2 != i + 1 or j2 != j + 1:
            continue
        a_break, b_break = visible_break(A, i, i2), visible_break(B, j, j2)
        if a_break and not b_break and joined(B, j, j2):
            msg = (f"Paragraph break missing in stage: “{A.words[i].text}” and “{A.words[i2].text}” are separate "
                   f"paragraphs in prod but joined in one line in stage")
        elif b_break and not a_break and joined(A, i, i2):
            msg = (f"Extra paragraph break in stage: “{A.words[i].text} {A.words[i2].text}” is one line in prod "
                   f"but split into separate paragraphs in stage")
        else:
            continue
        count += 1
        findings.append(Finding("content", sev, msg, locs(u.a, [i, i2]), locs(u.b, [j, j2]),
                                {"op": "paragraph", "words": 1}, types=["paragraph break"]))
    return count


def _moved_blocks(ops, at, bt) -> dict[int, tuple[int, int]]:
    """Pair a pure delete with a pure insert of the identical token run (text that
    only moved, e.g. table cells read in a different order). Returns
    {opcode index: (j1, j2) of the paired insert}, keyed by both the delete and the insert."""
    inserts: dict[tuple, list[int]] = {}
    for n, (tag, i1, i2, j1, j2) in enumerate(ops):
        if tag == "insert":
            inserts.setdefault(tuple(bt[j1:j2]), []).append(n)
    moved: dict[int, tuple[int, int]] = {}
    for n, (tag, i1, i2, j1, j2) in enumerate(ops):
        if tag == "delete":
            cands = inserts.get(tuple(at[i1:i2]))
            if cands:
                k = cands.pop(0)
                moved[n] = (ops[k][3], ops[k][4])
                moved[k] = (ops[k][3], ops[k][4])
    return moved
