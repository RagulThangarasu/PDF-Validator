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


def _drop_moved_phrases(d, idx: list[int], relocated: Counter, min_words: int = 3) -> tuple[list[int], Counter]:
    """Remove runs of >= min_words words that all sit unmatched on the other side (the phrase only
    moved) from a diff block that also holds other text. Returns (what is left, relocated minus the
    removed words). A block that is all moved text is handled by the caller."""
    keep, run, left = [], [], Counter(relocated)

    def flush():
        nonlocal left
        parts = _parts(d.words[i].norm for i in run)
        if len(run) >= min_words and not parts - left:
            left = left - parts
        else:
            keep.extend(run)
        run.clear()

    for i in idx:
        if _parts([d.words[i].norm]) - left:  # this word did not move: ends a run
            flush()
            keep.append(i)
        else:
            run.append(i)
    flush()
    if not keep:  # all of it moved: leave the decision to the caller
        return idx, relocated
    return (keep, left) if len(keep) < len(idx) else (idx, relocated)


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
    moves: list[tuple[Finding, list[int], list[int]]] = []
    words_of: dict[int, tuple[list[int], list[int]]] = {}  # finding -> (prod words, stage words)
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
                moves.append((findings[-1], list(a_idx), list(b_idx)))
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
            # a moved phrase glued to other text: "1 x Webcam accessory" (a list item read in another
            # column order) + "NOTE:" - drop the phrase, report only what is left
            a_idx, relocated_a = _drop_moved_phrases(u.a, a_idx, relocated_a)
            b_idx, relocated_b = _drop_moved_phrases(u.b, b_idx, relocated_b)
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
        shown = snippet(u.b, b_idx) if b_idx else snippet(u.a, a_idx) if a_idx else ""
        ctx = f" in “{ctx}”" if ctx and ctx != shown and max(len(a_idx), len(b_idx)) <= 3 else ""
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
        words_of[id(findings[-1])] = (list(a_idx), list(b_idx))

    spacing = _spacing(u, findings) if ccfg.get("check_spacing", True) else 0
    spacing += _paragraphs(u, findings) if ccfg.get("check_paragraphs", False) else 0
    unmatched_a, pool = +unmatched_a, +pool  # drop zero counts left by hyphenation matches
    matched += hyphen_matched
    reordered = sum((unmatched_a & pool).values())  # present in stage, only in another order
    missing = sum((unmatched_a - pool).values())
    extra = sum((pool - unmatched_a).values())
    matched += reordered
    total = len(at)
    in_place = {id(f) for f, a_idx, b_idx in moves if _visually_in_place(u, a_idx, b_idx)}
    findings = [f for f in findings if id(f) not in in_place]
    findings = _split_unrelated(u, findings, words_of)
    findings = _pair_near(findings)
    findings = _pair_parts(u, findings, words_of)
    _house_style(u, findings, words_of)
    scripts = _scripts(u, sev)
    findings += scripts
    # every content difference counts: prod words missing or changed, words stage adds, spacing and
    # super/subscript errors - 100 % means the section's text is identical
    denom = total + extra
    u.content = {
        "baseline_words": total,
        "candidate_words": len(bt),
        "matched_words": matched,
        "missing_words": missing,
        "extra_words": extra,
        "moved_words": moved_words + reordered,
        "spacing_issues": spacing,
        "script_issues": len(scripts),
        "match_pct": round(100.0 * max(matched - spacing - len(scripts), 0) / denom, 2) if denom else 100.0,
    }
    return findings


def _visual_rank(doc, rng) -> dict[int, int]:
    """Word -> its place when the page is read by eye: page, then line top to bottom, then left to right."""
    idx = [i for i in range(*rng) if doc.words[i].norm]
    key = lambda i: (doc.words[i].page, round(doc.lines[doc.words[i].line].bbox[1] / 2), doc.words[i].bbox[0])
    return {i: k for k, i in enumerate(sorted(idx, key=key))}


def _visually_in_place(u: Unit, a_idx: list[int], b_idx: list[int]) -> bool:
    """A "moved" block that sits between the same neighbouring text on the page in both PDFs has not
    moved: only the order the PDF stores its text in differs (a wrapped line written as its own
    run). Neighbours are the nearest paired words before and after the block in reading-by-eye order."""
    if not a_idx or not b_idx:
        return False
    if _same_table_row(u, a_idx, b_idx) or _repeated_block(u, a_idx):
        return True
    ra, rb = _visual_rank(u.a, u.a_range), _visual_rank(u.b, u.b_range)
    a2b = dict(u.pairs)
    block = set(a_idx)
    order_a = sorted(ra, key=ra.get)
    lo, hi = min(ra.get(i, 0) for i in a_idx), max(ra.get(i, 0) for i in a_idx)
    before = next((i for i in reversed(order_a[:lo]) if i in a2b and i not in block), None)
    after = next((i for i in order_a[hi + 1:] if i in a2b and i not in block), None)
    b_lo, b_hi = min(rb.get(j, 0) for j in b_idx), max(rb.get(j, 0) for j in b_idx)
    ok_before = before is None or rb.get(a2b[before], -1) < b_lo
    ok_after = after is None or rb.get(a2b[after], 10 ** 9) > b_hi
    return (before is not None or after is not None) and ok_before and ok_after


def _row_of(doc, i: int):
    """(page, table, row) of a word inside a detected table, else None."""
    from .tables import _raw
    w = doc.words[i]
    cx, cy = (w.bbox[0] + w.bbox[2]) / 2, (w.bbox[1] + w.bbox[3]) / 2
    for t, tb, rows, _ in _raw(doc, w.page):
        if tb[0] - 1 <= cx <= tb[2] + 1 and tb[1] - 1 <= cy <= tb[3] + 1:
            for r, (rb, _) in enumerate(rows):
                if rb[1] - 1 <= cy <= rb[3] + 1:
                    return (w.page, t, r)
    return None


def _repeated_block(u: Unit, a_idx: list[int]) -> bool:
    """The block's text occurs several times in the section, as often in stage as in prod (a column
    header row repeated over several tables): which copy pairs with which is arbitrary, so the
    copies have not moved."""
    seq = [u.a.words[i].norm for i in a_idx]
    def count(doc, rng):
        toks = [w.norm for w in doc.words[rng[0]:rng[1]] if w.norm]
        n = len(seq)
        return sum(1 for k in range(len(toks) - n + 1) if toks[k:k + n] == seq)
    ca = count(u.a, u.a_range)
    return ca >= 2 and ca == count(u.b, u.b_range)


def _same_table_row(u: Unit, a_idx: list[int], b_idx: list[int]) -> bool:
    """The block is a table cell that sits beside the same (paired) cells on both sides: a label set
    in the middle of its row in one PDF and at the top in the other reads in another order, but it
    has not moved. The row comes from whichever side has a detected table (a table without ruling
    lines is not detected); on the other side the block must sit beside the row's other cells."""
    a2b = dict(u.pairs)
    b2a = {j: i for i, j in a2b.items()}
    for X, Y, xi, yi, fwd in ((u.a, u.b, a_idx, b_idx, a2b), (u.b, u.a, b_idx, a_idx, b2a)):
        row = _row_of(X, xi[0])
        if row is None:
            continue
        rng = u.a_range if X is u.a else u.b_range
        block = set(xi)
        mates = [fwd[i] for i in range(*rng) if i not in block and i in fwd and X.words[i].page == row[0]
                 and _row_of(X, i) == row]
        page = Y.words[yi[0]].page
        ys = [(Y.words[j].bbox[1] + Y.words[j].bbox[3]) / 2 for j in mates if Y.words[j].page == page]
        if len(ys) < 2:
            continue
        cy = sum((Y.words[j].bbox[1] + Y.words[j].bbox[3]) / 2 for j in yi) / len(yi)
        h = max(Y.words[j].bbox[3] - Y.words[j].bbox[1] for j in yi)
        if min(ys) - h <= cy <= max(ys) + h:
            return True
    return False


def _pair_near(findings: list[Finding]) -> list[Finding]:
    """A missing and an extra piece of text at the same place that differ only in spacing,
    punctuation or case are one change: “requirements.” -> “requirements”, “meets the” ->
    “meetsthe”. (They come apart when stage draws the edited text as a separate run, so the
    diff meets it out of reading order.)"""
    miss = [f for f in findings if f.detail.get("op") == "delete" and f.baseline]
    extra = [f for f in findings if f.detail.get("op") == "insert" and f.candidate]
    drop = set()
    for m in miss:
        at = m.candidate_at
        for e in extra:
            if id(e) in drop:
                continue
            c = e.candidate[0]
            if at is not None and (at.page != c.page or abs(at.bbox[1] - c.bbox[1]) > 40):
                continue
            ta, tb = (m.detail.get("baseline_text") or "").split(), (e.detail.get("candidate_text") or "").split()
            kind = classify(ta, tb)
            if kind not in ("spacing", "punctuation", "case", "case + punctuation"):
                continue
            m.message = f"{_TYPE_LABEL[kind]}: “{m.detail['baseline_text']}” → “{e.detail['candidate_text']}”"
            m.candidate, m.candidate_at, m.types = e.candidate, None, [kind]
            m.links = [(m.baseline[0], e.candidate[0])]
            m.detail = {**m.detail, "op": "replace", "candidate_text": e.detail["candidate_text"], "absent_words": 0}
            drop.add(id(e))
            break
    return [f for f in findings if id(f) not in drop]


def _letters_alike(ta: list[str], tb: list[str]) -> float:
    ka, kb = "".join(re.findall(r"\w", " ".join(ta).lower())), "".join(re.findall(r"\w", " ".join(tb).lower()))
    return SequenceMatcher(None, ka, kb, autojunk=False).ratio() if ka and kb else 0.0


def _split_unrelated(u: Unit, findings: list[Finding], words_of: dict) -> list[Finding]:
    """A "changed text" whose two sides have nothing in common is two differences at one spot: prod
    text missing and other stage text added ("Amplifier or speaker" → "NOTE:" where a figure label
    of prod is drawn in the stage picture and a note follows). Split, each side is judged on its
    own (a label in a picture, a house-style label, text that moved)."""
    out = []
    for f in findings:
        a_idx, b_idx = words_of.get(id(f), ([], []))
        na, nb = [u.a.words[i].norm for i in a_idx], [u.b.words[j].norm for j in b_idx]
        # one side is only a callout label or list numbers ("NOTE:", "4."): the other side's text
        # did not turn into it - a reworded sentence ("7 on page 10." → "Anti-theft security bar.")
        # stays one change, and so does a renumbered item ("1." → "2.")
        marker = lambda ns: bool(ns) and all(n.startswith("<label:") or re.fullmatch(r"\(?\w{1,3}[.):]", n) for n in ns)
        if "changed text" not in f.types or not a_idx or not b_idx or marker(na) == marker(nb) \
                or _letters_alike(na, nb) >= 0.3:
            out.append(f)
            continue
        rcfg = u.cfg["report"]
        miss = Finding("content", f.severity, f"Missing text: “{snippet(u.a, a_idx)}”",
                       locs(u.a, a_idx, rcfg["max_locs"]), [],
                       {"op": "delete", "baseline_text": snippet(u.a, a_idx, 200), "candidate_text": "",
                        "words": len(a_idx), "absent_words": f.detail.get("absent_words", len(a_idx))},
                       candidate_at=locs(u.b, b_idx[:1])[0], critical=f.critical, types=["missing text"])
        extra = Finding("content", u.cfg["content"].get("severity", "warning"), f"Extra text: “{snippet(u.b, b_idx)}”",
                        [], locs(u.b, b_idx, rcfg["max_locs"]),
                        {"op": "insert", "baseline_text": "", "candidate_text": snippet(u.b, b_idx, 200),
                         "words": len(b_idx), "absent_words": 0},
                        baseline_at=locs(u.a, a_idx[:1])[0], types=["extra text"])
        words_of[id(miss)], words_of[id(extra)] = (a_idx, []), ([], b_idx)
        out += [miss, extra]
    return out


def _house_style(u: Unit, findings: list[Finding], words_of: dict) -> None:
    """Differences that are the template's, not the content's (info, not genuine):
    - a callout label on one side only: stage prints "NOTE:" where prod shows only the note icon
      (labels are folded to <label:…> by normalize.fold_labels)
    - a continuation header on one side only: "Lamp Control (continued)" repeated at the top of the
      next page when a table or a list breaks across pages"""
    for f in findings:
        a_idx, b_idx = words_of.get(id(f), ([], []))
        if not ({"missing text", "extra text"} & set(f.types)) or (a_idx and b_idx):
            continue
        d, idx, side, other = (u.a, a_idx, "prod", "stage") if a_idx else (u.b, b_idx, "stage", "prod")
        norms = [d.words[i].norm for i in idx]
        if norms and all(n.startswith("<label:") for n in norms):
            f.severity, f.critical, f.types = "info", False, ["label only"]
            f.message = (f"Callout label only in {side}: “{snippet(d, idx)}” "
                         f"(the {other} note has no label, only its icon or box)")
        elif len(norms) <= 8 and any(re.sub(r"\W", "", n.lower()) == "continued" for n in norms):
            f.severity, f.critical, f.types = "info", False, ["continued header"]
            f.message = (f"Continuation header only in {side}: “{snippet(d, idx)}” "
                         f"(repeated at a page break; the page breaks differ)")


def _pair_parts(u: Unit, findings: list[Finding], words_of: dict) -> list[Finding]:
    """Stage text that is a stretch of unexplained prod text, apart from punctuation, spacing or case,
    is that prod text: “supply’s” (stage) is “supply's” of the missing run “supply's projector's
    40°C/”. The pieces come apart when the two PDFs read columns in another order - the diff then
    meets each side at a different spot (as missing / extra text, or glued to an unrelated word as
    “changed text”), and neither side's marker shows the other text. Each such stretch becomes one
    change with both locations; what is left of the findings it came from is described again."""
    rcfg = u.cfg["report"]
    kinds = ("spacing", "punctuation", "case", "case + punctuation")
    letters = lambda idx, d: sum(ch.isalnum() for i in idx for ch in d.words[i].norm)
    loose = lambda f: id(f) in words_of and ("changed text" in f.types or f.detail.get("op") in ("delete", "insert")) \
        and not f.critical
    out = list(findings)
    for e in [f for f in findings if loose(f)]:
        b_idx = words_of[id(e)][1]
        if not any(x is e for x in out) or letters(b_idx, u.b) < 3:  # "1." or ":" alone matches anything
            continue
        tb = [u.b.words[j].norm for j in b_idx]
        for m in [f for f in out if f is not e and loose(f) and words_of[id(f)][0]]:
            a_all = words_of[id(m)][0]
            ta = [u.a.words[i].norm for i in a_all]
            # a window of about the same length ("40°C/" is one word, "40°C /" two)
            hit = next(((i, n) for n in sorted(range(max(1, len(tb) - 2), len(tb) + 3), key=lambda n: abs(n - len(tb)))
                        for i in range(len(ta) - n + 1) if classify(ta[i:i + n], tb) in kinds
                        and _same_neighbour(u, a_all[i:i + n], b_idx)), None)
            if not hit:
                continue
            i, n = hit
            a_idx = a_all[i:i + n]
            kind = classify(ta[i:i + n], tb)
            la, lb = locs(u.a, a_idx, rcfg["max_locs"]), locs(u.b, b_idx, rcfg["max_locs"])
            ctx = _context(u.b, b_idx)
            ctx = f" in “{ctx}”" if ctx and ctx != snippet(u.b, b_idx) and len(b_idx) <= 3 else ""
            pair = Finding("content", e.severity, f"{_TYPE_LABEL[kind]}: “{snippet(u.a, a_idx)}” → “{snippet(u.b, b_idx)}”{ctx}",
                           la, lb, {"op": "replace", "baseline_text": snippet(u.a, a_idx, 200),
                                    "candidate_text": snippet(u.b, b_idx, 200), "words": max(len(a_idx), len(b_idx)),
                                    "absent_words": 0},
                           types=[kind], links=list(zip(la, lb)) if len(la) == len(lb) else [(la[0], lb[0])])
            words_of[id(pair)] = (a_idx, b_idx)
            out.insert(next(k for k, x in enumerate(out) if x is e), pair)
            for donor, a_rest, b_rest in ((m, a_all[:i] + a_all[i + n:], words_of[id(m)][1]),
                                          (e, words_of[id(e)][0], [])):
                _redescribe(u, donor, a_rest, b_rest, words_of, out)
            break
    return out


def _same_neighbour(u: Unit, a_idx: list[int], b_idx: list[int]) -> bool:
    """The word before or after the stretch is the same on both sides ("power supply's power" /
    "power supply’s power"): the same passage, not a common word met somewhere else."""
    key = lambda d, k: re.sub(r"[^\w]", "", d.words[k].norm).lower() if 0 <= k < len(d.words) else None
    before = key(u.a, a_idx[0] - 1), key(u.b, b_idx[0] - 1)
    after = key(u.a, a_idx[-1] + 1), key(u.b, b_idx[-1] + 1)
    return any(x == y and x for x, y in (before, after))


def _redescribe(u: Unit, f: Finding, a_idx: list[int], b_idx: list[int], words_of: dict, out: list) -> None:
    """Rewrite finding f for the words it has left (or remove it when none are left)."""
    if not a_idx and not b_idx:
        del out[next(k for k, x in enumerate(out) if x is f)]
        return
    rcfg = u.cfg["report"]
    words_of[id(f)] = (a_idx, b_idx)
    ta, tb = [u.a.words[i].norm for i in a_idx], [u.b.words[j].norm for j in b_idx]
    tag = "replace" if a_idx and b_idx else "delete" if a_idx else "insert"
    ctype = classify(ta, tb) if tag == "replace" else "missing text" if a_idx else "extra text"
    # the other side's marker: where the words that were paired away sat
    if not b_idx and f.candidate:
        f.candidate_at = f.candidate_at or f.candidate[0]
    if not a_idx and f.baseline:
        f.baseline_at = f.baseline_at or f.baseline[0]
    f.baseline, f.candidate = locs(u.a, a_idx, rcfg["max_locs"]), locs(u.b, b_idx, rcfg["max_locs"])
    ctx = _context(u.b, b_idx) if b_idx else _context(u.a, a_idx)
    shown = snippet(u.b, b_idx) if b_idx else snippet(u.a, a_idx)
    ctx = f" in “{ctx}”" if ctx and ctx != shown and max(len(a_idx), len(b_idx)) <= 3 else ""
    f.message = (f"{_TYPE_LABEL.get(ctype, _KIND[tag])}: " + (f"“{snippet(u.a, a_idx)}”" if a_idx else "")
                 + (" → " if a_idx and b_idx else "") + (f"“{snippet(u.b, b_idx)}”" if b_idx else "") + ctx)
    f.types, f.links = [ctype], []
    f.detail = {**f.detail, "op": tag, "baseline_text": snippet(u.a, a_idx, 200), "candidate_text": snippet(u.b, b_idx, 200),
                "words": max(len(a_idx), len(b_idx)),
                "absent_words": min(f.detail.get("absent_words", len(a_idx)), len(a_idx))}


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
