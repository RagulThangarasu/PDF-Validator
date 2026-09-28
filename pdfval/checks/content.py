"""Content check: word-level diff of the section text (reading order).

Content is strict about the *text*: words, case, punctuation (. , : quotes
dashes) and the spacing between words on the same line (double space, missing
space). It ignores everything that is presentation: font weight/colour/size
(→ style check), line wrapping, hyphenation at a line break, bullet glyphs.
Text that is all there but wraps to the next line at a different place
("SL6504/SL7504/" split over two lines on one side only) is a match here and
is handed to the layout check (unit.wraps), which reports it only with
layout.check_wrap. Likewise a missing/extra block whose words all sit unmatched
on the other side (moved to the next line, or table cells read in another order)
is not reported (content.ignore_relocated), and paragraph breaks present on one
side only are reported only with content.check_paragraphs.

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

from ..model import Finding, Loc
from . import Unit, insertion_loc, locs, paired_locs, snippet

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


def _parts(tokens) -> Counter:
    """Tokens split after a hyphen/slash: "non-condensing" and "non-" + "condensing" count the
    same, whichever side joined the word across the line break."""
    return Counter(p for t in tokens for p in re.split(r"(?<=[-/–—])", t) if p)


def _repeated_header(d, idx: list[int]) -> list[int]:
    """The leading words of idx when they are the first row of a page and the same row
    text already appeared earlier in the document: a table header repeated on a
    continuation page ("Menu item | Description" at the top of p.48). Else []."""
    if not idx or (idx[0] > 0 and d.words[idx[0] - 1].page == d.words[idx[0]].page):
        return []
    row = []
    for i in idx:
        if not same_row(d, idx[0], i):
            break
        row.append(i)
    nxt = row[-1] + 1
    if nxt < len(d.words) and same_row(d, idx[0], nxt):
        return []  # the row goes on beyond the block: not a whole row
    ws = sorted((d.words[i] for i in row), key=lambda w: w.bbox[0])
    if not any(b.bbox[0] - a.bbox[2] > 2 * a.style.size for a, b in zip(ws, ws[1:])):
        return []  # one cell only (a "WARNING:" label): not a table header row
    seq, n = [d.words[i].norm for i in row], len(row)
    return row if any([w.norm for w in d.words[k:k + n]] == seq for k in range(idx[0] - n + 1)) else []


def _letters(t: str) -> str:
    return "".join(c for c in t.lower() if c.isalnum())


def _loose_bag(tokens) -> Counter:
    return Counter(k for t in tokens if (k := _letters(t)))


def same_words(u: Unit, a_idx: list[int], b_idx: list[int]) -> list[tuple[int, int]]:
    """Inside a text diff, the words that are still the same word ignoring case and
    punctuation ("Note" / "NOTE:", "B" / "(B).") - their style is comparable and they
    anchor the highlight of the diff on both sides."""
    ka = [_letters(u.a.words[i].norm) for i in a_idx]
    kb = [_letters(u.b.words[j].norm) for j in b_idx]
    out = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, ka, kb, autojunk=False).get_opcodes():
        if tag == "equal":
            out.extend((a_idx[i], b_idx[j]) for i, j in zip(range(i1, i2), range(j1, j2)) if ka[i])
    return out


def _context(doc, idx: list[int], width: int = 60) -> str:
    """The line holding the first of these words, trimmed around them."""
    from .. import normalize
    w = doc.words[idx[0]]
    line = [k for k in range(doc.lines[w.line].first_word, len(doc.words)) if doc.words[k].line == w.line] \
        if doc.lines[w.line].first_word >= 0 else []
    if not line:
        return ""
    at = line.index(idx[0]) if idx[0] in line else 0
    text_before = normalize.join_words(doc.words[k] for k in line[:at])
    text = normalize.join_words(doc.words[k] for k in line)
    if len(text) <= width:
        return text
    start = max(0, min(len(text_before) - width // 3, len(text) - width))
    return ("…" if start else "") + text[start:start + width] + ("…" if start + width < len(text) else "")


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
    lost = Counter()  # prod words not in any equal/moved block
    for n, (tag, i1, i2, j1, j2) in enumerate(ops):
        if tag in ("insert", "replace") and n not in moved:
            pool.update(bt[j1:j2])
        if tag in ("delete", "replace") and n not in moved:
            lost.update(at[i1:i2])
    # words unmatched on both sides: the same text that only sits on another line or in another
    # cell order ("SL6504/ SL7504/" wrapped inside a table cell) - not a content difference
    relocated_a = relocated_b = _parts(lost.elements()) & _parts(pool.elements())
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
                    types=["reordered"], links=paired_locs(u.a, u.b, list(zip(a_idx, b_idx)), rcfg["max_locs"])))
            continue
        unmatched_a.update(at[i1:i2])
        if max(i2 - i1, j2 - j1) < ccfg.get("min_diff_words", 1):
            continue
        a_idx, b_idx = ai[i1:i2], bi[j1:j2]
        # prod words of this block found nowhere in stage (ignoring case/punctuation: "Note" is in "NOTE:")
        absent = sum((_loose_bag(at[i1:i2]) - _loose_bag(pool.elements())).values())
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
        if ccfg.get("ignore_repeated_headers", True):
            # a table header repeated at the top of a continuation page: pagination, not content
            ha, hb = _repeated_header(u.a, a_idx), _repeated_header(u.b, b_idx)
            if ha:
                unmatched_a.subtract(u.a.words[i].norm for i in ha)
                hyphen_matched += len(ha)
            a_idx, b_idx = a_idx[len(ha):], b_idx[len(hb):]
        if ccfg.get("ignore_relocated", True):
            # each side on its own: words that sit unmatched on the other side only moved
            # (another line, another cell order) - "Sound mode" read before or after its cell text
            ca, cb = _parts(u.a.words[i].norm for i in a_idx), _parts(u.b.words[j].norm for j in b_idx)
            a_rel, b_rel = not ca - relocated_a, not cb - relocated_b
            if a_rel and b_rel:
                relocated_a, relocated_b = relocated_a - ca, relocated_b - cb  # counted as reordered in the match %
                u.style_pairs.extend(same_words(u, a_idx, b_idx) if a_idx and b_idx else [])  # style still compared
                continue
            # one side alone only when the two sides are unrelated text and the moved part is
            # a phrase: "Tip" -> "TIP:" or a lone "2." stay a real difference
            if ctype == "changed text" and a_rel and len(a_idx) >= 2:
                relocated_a, a_idx = relocated_a - ca, []
            if ctype == "changed text" and b_rel and len(b_idx) >= 2:
                relocated_b, b_idx = relocated_b - cb, []
        if not a_idx and not b_idx:
            continue
        if (a_idx, b_idx) != (ai[i1:i2], bi[j1:j2]):  # narrowed: describe what is left
            ta, tb = [u.a.words[i].norm for i in a_idx], [u.b.words[j].norm for j in b_idx]
            tag = "replace" if a_idx and b_idx else "delete" if a_idx else "insert"
            absent = sum((_loose_bag(ta) - _loose_bag(pool.elements())).values())
            a_at = insertion_loc(u.a, ai[i1 - 1] if i1 > 0 else None, ai[i1] if i1 < len(ai) else None) if not a_idx else None
            b_at = insertion_loc(u.b, bi[j1 - 1] if j1 > 0 else None, bi[j1] if j1 < len(bi) else None) if not b_idx else None
            critical = tag in ("delete", "replace") and absent >= crit_words and absent >= 0.5 * len(a_idx)
            ctype = ("missing text" if tag == "delete" or critical else "extra text" if tag == "insert"
                     else classify(ta, tb))
        same =same_words(u, a_idx, b_idx) if a_idx and b_idx else []
        u.style_pairs.extend(same)
        label = "Missing content block" if critical else _TYPE_LABEL.get(ctype, _KIND[tag])
        # a short difference (one character in Chinese/Japanese, a word or two) is shown in its line,
        # so the reader can find it: “废” → “州” in “有关 China WEEE 州弃电器电子产品回收处理”
        ctx = _context(u.b, b_idx) if b_idx else _context(u.a, a_idx) if a_idx else ""
        ctx = f" in “{ctx}”" if ctx and max(len(a_idx), len(b_idx)) <= 3 else ""
        findings.append(Finding(
            "content", "error" if critical else sev,
            f"{label}: "
            + (f"“{snippet(u.a, a_idx)}”" if a_idx else "")
            + (" → " if a_idx and b_idx else "") + (f"“{snippet(u.b, b_idx)}”" if b_idx else "") + ctx,
            locs(u.a, a_idx, rcfg["max_locs"]), locs(u.b, b_idx, rcfg["max_locs"]),
            {"op": tag, "baseline_text": snippet(u.a, a_idx, 200), "candidate_text": snippet(u.b, b_idx, 200),
             "words": max(len(a_idx), len(b_idx)), "absent_words": absent},
            baseline_at=a_at, candidate_at=b_at, critical=critical, types=[ctype],
        ))

    spacing = _spacing(u, findings) if ccfg.get("check_spacing", True) else 0
    spacing += _paragraphs(u, findings) if ccfg.get("check_paragraphs", False) else 0
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
    findings += _scripts(u, sev)
    return findings


_SUP = str.maketrans("0123456789+-=()nia", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱᵃ")
_SUB = str.maketrans("0123456789+-=()", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎")


def _show_script(text: str, script: str) -> str:
    """'(Cr+6)' with '+6' raised -> '(Cr⁺⁶)'; characters without a Unicode form -> ^(…) / _(…)."""
    if not script:
        return text
    out, k = [], 0
    while k < len(text):
        pos = script[k] if k < len(script) else "."
        n = k
        while n < len(text) and (script[n] if n < len(script) else ".") == pos:
            n += 1
        part = text[k:n]
        if pos == "^":
            t = part.translate(_SUP)
            out.append(t if t != part or not part.strip() else f"^({part})")
        elif pos == "_":
            t = part.translate(_SUB)
            out.append(t if t != part else f"_({part})")
        else:
            out.append(part)
        k = n
    return "".join(out)


def _scripts(u: Unit, sev: str) -> list[Finding]:
    """The same text raised (superscript) or lowered (subscript) on one side only: "Cr+6" vs "Cr⁺⁶",
    "10^6", "H₂O", footnote marks. The style check sees only a word's first character, so this is
    compared per character."""
    out = []
    for i, j in sorted(u.pairs):
        wa, wb = u.a.words[i], u.b.words[j]
        if wa.script == wb.script or not wa.norm:
            continue
        raised = "".join(c for c, p in zip(wb.text, wb.script or "." * len(wb.text)) if p != ".")
        lowered = "".join(c for c, p in zip(wa.text, wa.script or "." * len(wa.text)) if p != ".")
        what = (f"“{raised}” is {'superscript' if '^' in wb.script else 'subscript'} in stage" if raised else "") + \
               ("; " if raised and lowered else "") + \
               (f"“{lowered}” is {'superscript' if '^' in wa.script else 'subscript'} in prod" if lowered else "")
        out.append(Finding(
            "content", sev,
            f"Superscript / subscript differs: “{_show_script(wa.text, wa.script)}” → “{_show_script(wb.text, wb.script)}” ({what})",
            [Loc(wa.page, wa.bbox)], [Loc(wb.page, wb.bbox)],
            {"op": "script", "baseline_script": wa.script, "candidate_script": wb.script},
            types=["superscript"], links=[(Loc(wa.page, wa.bbox), Loc(wb.page, wb.bbox))]))
    return out


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
            {"op": "spacing", "baseline_spaces": sa, "candidate_spaces": sb, "words": 1}, types=["spacing"],
            links=paired_locs(u.a, u.b, [(i, j), (i2, j2)])))
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
                                {"op": "paragraph", "words": 1}, types=["paragraph break"],
                                links=paired_locs(A, B, [(i, j), (i2, j2)])))
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
