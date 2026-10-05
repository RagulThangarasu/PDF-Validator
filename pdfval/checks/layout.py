"""Alignment / layout check on lines whose first word matched textually.

* indent      – line x-offset from the page's content-box left edge
* text-align  – left / center / right relative to the content box
* line-height – baseline-to-baseline distance of consecutive matched lines (em)
* space-above – gap above the section heading (em)
* bullets     – list items (a bullet / number before the text): where the marker
                sits relative to the line above, the gap between marker and text,
                and how wrapped lines align (hanging indent). Measured relative to
                the surrounding text, in em, so a note box or different page
                margins do not count – only the list's own alignment does.
* line-wrap   – the text is the same but wraps to the next line at a different
                word (or a word is hyphenated/split over two lines on one side
                only). Content treats these as a match; they are layout issues.

Offsets are measured relative to each document's own margins, so the check
works even when page size and margins differ between the two PDFs.
"""
from __future__ import annotations

import re
from collections import defaultdict

from ..model import Doc, Finding, Line, Loc
from . import Unit, locs, paired_locs
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
    if not lcfg.get("compare_with_prod", True):
        # layout is judged on stage against the spec; the list markers themselves (a bullet or number
        # missing, another numbering style / format, out of sequence) are content and still compared;
        # where the bullets sit (marker position, gap to the text, hanging indent) too, unless turned off
        return _bullets(u, positions=lcfg.get("compare_bullets_with_prod", True))
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

    findings = _wraps(u) + _bullets(u)
    for (role, prop, x, y), pairs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if len(pairs) < lcfg.get("min_lines", 1):
            continue
        findings.append(Finding(
            "layout", lcfg.get("severity", {}).get(prop, "warning"),
            f"[{role}] {prop}: {x} → {y} ({len(pairs)} lines)",
            locs(A, [p[0] for p in pairs], rcfg["max_locs"]), locs(B, [p[1] for p in pairs], rcfg["max_locs"]),
            {"role": role, "property": prop, "baseline": x, "candidate": y, "lines": len(pairs)},
            types=[prop], links=paired_locs(A, B, pairs, rcfg["max_locs"]),
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
                links=paired_locs(A, B, [(u.a_anchor.word, u.b_anchor.word)]),
            ))
    return findings


def _wraps(u: Unit) -> list[Finding]:
    """Same text, wrapped to the next line at a different place on one side.
    Two consecutive matched words with a line break between them on one side only
    (not a paragraph break - that is content), plus the words the content check
    found split over lines differently ("config-/uration", "SL6504/ / SL7504/").
    One finding per role; each place is marked by the words either side of the break."""
    lcfg, rcfg = u.cfg["layout"], u.cfg["report"]
    if not lcfg.get("check_wrap", False):
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
            + "; ".join(examples),
            a_locs, b_locs,
            {"role": role, "property": "line-wrap", "places": len(ps), "examples": examples[:50]},
            types=["line-wrap"],
            links=[l for a, b, _ in ps for l in _wrap_links(A, B, a, b)][: rcfg["max_locs"]],
        ))
    return findings


def _wrap_links(A: Doc, B: Doc, a: list[int], b: list[int]) -> list:
    """Link the lines around one wrap place: the prod lines of the words with the stage
    lines of the same words (zip is exact for the same words; when the split differs,
    the whole place is one box per side)."""
    if len(a) == len(b):
        return paired_locs(A, B, list(zip(a, b)))
    la, lb = locs(A, a), locs(B, b)
    box = lambda ls: (min(l.bbox[0] for l in ls), min(l.bbox[1] for l in ls), max(l.bbox[2] for l in ls), max(l.bbox[3] for l in ls))
    if la and lb and len({l.page for l in la}) == 1 and len({l.page for l in lb}) == 1:
        return [(Loc(la[0].page, box(la)), Loc(lb[0].page, box(lb)))]
    return []


def _marker_test(lcfg: dict):
    """Is a word a list marker? An enumerator matching `list_marker` (1. 2) a. (iv)),
    or one character (maybe repeated) whose Unicode category is in `list_symbol_categories`
    (bullets are punctuation / other symbols: • * - ▪ ✓; math symbols such as × + | are
    not, so "32.8 × 55.6" wrapped after 32.8 does not start a list)."""
    import unicodedata
    enum = re.compile(lcfg["list_marker"]) if lcfg.get("list_marker") else None
    cats = set(lcfg.get("list_symbol_categories", []))
    # a bullet is one glyph, maybe repeated ("•", "--"): "◄/►" starting a wrapped line is not a bullet
    # ordinary punctuation is not a bullet: a "." "," "/" wrapped to the start of a line ends the text before it
    plain = set(".,:;!?/\\'\"()[]{}@#%&_|")
    # Chinese / Japanese sentence punctuation (。，、：「」（）…) wraps to the start of a line like any
    # character: it is never a bullet
    plain |= set("。，、；：！？「」『』（）【】〈〉《》…・")
    return lambda t: bool(t) and ((enum is not None and enum.match(t) is not None)
                                  or (bool(cats) and len(set(t)) == 1 and len(t) <= 3 and t[0] not in plain
                                      and all(unicodedata.category(c) in cats for c in t)))


def _list_items(doc: Doc, rng: tuple[int, int], is_marker, tol_em: float, max_gap_em: float) -> dict[int, dict]:
    """List items in a word range, keyed by the item's first text word:
    marker word, the x of the line that introduces the list (label / paragraph before
    it: every item of a shifted list counts, and so does one item out of line with its
    siblings), and where its wrapped lines start.
    A line is an item when it starts with a list marker followed by text, or when a
    marker-only line sits on the same visual row just left of it (PDFs often store the
    bullet as its own line, before or after the text in reading order). Lines in the
    item text's block that follow it are its wrapped lines."""
    if rng[0] >= rng[1]:
        return {}
    l0, l1 = doc.words[rng[0]].line, doc.words[rng[1] - 1].line + 1
    words_of = lambda li: range(doc.lines[li].first_word,
                                doc.lines[li + 1].first_word if li + 1 < len(doc.lines) else len(doc.words))
    lone = {li for li in range(l0, l1) if len(words_of(li)) == 1 and is_marker(doc.words[doc.lines[li].first_word].text)}
    lone_by_page: dict[int, list[int]] = defaultdict(list)
    for li in sorted(lone):
        lone_by_page[doc.lines[li].page].append(doc.lines[li].first_word)

    row_lines: dict[int, list[int]] = defaultdict(list)
    for li in range(l0, l1):
        row_lines[doc.lines[li].page].append(li)

    def marker_left_of(f: int) -> int | None:
        """The nearest marker-only line on the same visual row, left of word f, with no
        other text between them (a "/" between two icons in another table cell is not
        the bullet of the text in the next cell)."""
        # a marker printed with no gap ("10.Do not ...") ends where the text starts, give or take
        # a fraction of a point: `touch` of slack both ways
        touch = 1.5
        left = [m for m in lone_by_page.get(doc.words[f].page, [])
                if same_row(doc, m, f) and doc.words[m].bbox[2] <= doc.words[f].bbox[0] + touch]
        m = max(left, key=lambda m: doc.words[m].bbox[2], default=None)
        if m is None:
            return None
        a, b = doc.words[m].bbox[2], doc.words[f].bbox[0]
        between = any(a - touch <= doc.lines[li].bbox[0] < b - touch and same_row(doc, doc.lines[li].first_word, f)
                      for li in row_lines[doc.words[f].page])
        return None if between else m

    def marker_above(f: int) -> int | None:
        """A marker-only line just above word f, ending where its text starts (“5.” set a line higher
        than “WAN/LAN port”): the item's number, out of line with its text."""
        wf = doc.words[f]
        em = max(wf.style.size, 1)
        cands = [m for m in lone_by_page.get(wf.page, []) if m not in marker_claims and not same_row(doc, m, f)
                 and wf.bbox[1] - 1.5 * em <= doc.words[m].bbox[3] <= wf.bbox[1] + 0.25 * em
                 and doc.words[m].bbox[1] < wf.bbox[1] - 0.3 * em
                 and -1.5 <= wf.bbox[0] - doc.words[m].bbox[2] <= 3 * em]
        return max(cands, key=lambda m: doc.words[m].bbox[3], default=None)

    items: dict[int, dict] = {}
    owner: dict[int, dict | None] = {}  # line -> the list item it belongs to (None: plain text line)
    marker_claims: dict[int, int] = {}  # marker word -> first text word of the item it introduces
    cur = page = None
    for li in range(l0, l1):  # reading order: items and their wrapped lines
        if li in lone:
            continue
        ws = words_of(li)
        f = ws[0]
        if f < rng[0] or f >= rng[1]:
            continue
        w = doc.words[f]
        if w.page != page:
            page, cur = w.page, None
        m = t = None
        above = False
        if len(ws) >= 2 and is_marker(w.text) and not is_marker(doc.words[f + 1].text):
            m, t = f, f + 1
        elif not is_marker(w.text):
            m, t = marker_left_of(f), f
            if m is None:  # no number on the row: one set a line above the text
                m = marker_above(f)
                above = m is not None
        if m is not None and m in marker_claims:
            # The same marker can be seen from multiple extracted lines (e.g. wrapped text in a table cell).
            # Reuse the original item instead of creating duplicate "marker missing" findings.
            cur = items.get(marker_claims[m])
        elif m is not None:
            # out of line with its text: the number's middle clearly higher than the text's (“5.” set over
            # “WAN/LAN port”) - however the PDF stores the two (one line or two)
            mw_, tw_ = doc.words[m], doc.words[t]
            above = above or ((tw_.bbox[1] + tw_.bbox[3]) - (mw_.bbox[1] + mw_.bbox[3])) / 2 > 0.4 * max(tw_.style.size, 1)
            cur = items[t] = {"marker": m, "text": t, "ref": None, "hang": None, "line": li, "above": above}
            marker_claims[m] = t
        elif cur is not None and doc.lines[li].block == doc.lines[doc.words[cur["text"]].line].block:
            if cur["hang"] is None:  # first wrapped line of the item
                cur["hang"] = w.bbox[0] - doc.words[cur["text"]].bbox[0]
        else:
            cur = None
        owner[li] = cur

    # the line introducing each item's list, by geometry (reading order can run down one
    # column of a table first): the closest line physically above that overlaps the start
    # of the item (marker + first word) - the same column, never a callout further right.
    # A plain line is the list's introduction; an item line (or its wrapped line) passes
    # on its own introduction. A list with no line close above it (the first list in a
    # table cell) is measured against its own first marker: its items must line up.
    by_page: dict[int, list[int]] = defaultdict(list)
    for li in owner:
        by_page[doc.lines[li].page].append(li)
    for it in sorted(items.values(), key=lambda it: (doc.lines[it["line"]].page, doc.lines[it["line"]].bbox[1])):
        mw, top = doc.words[it["marker"]], doc.lines[it["line"]].bbox[1]
        x0, x1 = mw.bbox[0], doc.words[it["text"]].bbox[2]
        slack = tol_em * mw.style.size
        # text left of the marker, level with the item's block, is another column (a
        # table cell such as "Environment" beside the list): the introduction must
        # start right of it
        blk = doc.lines[doc.words[it["text"]].line].block
        y1 = max(doc.lines[k].bbox[3] for k in by_page[mw.page] if doc.lines[k].block == blk)
        bound = max((doc.lines[k].bbox[2] for k in by_page[mw.page]
                     if doc.lines[k].bbox[2] <= x0 and doc.lines[k].bbox[1] < y1 and doc.lines[k].bbox[3] > top),
                    default=None)
        above = [li for li in by_page[mw.page] if doc.lines[li].bbox[3] <= top + slack and li != it["line"]
                 and doc.lines[li].bbox[0] <= x1 and doc.lines[li].bbox[2] >= x0
                 and (bound is None or doc.lines[li].bbox[0] >= bound)]
        li = max(above, key=lambda k: doc.lines[k].bbox[3], default=None)
        if li is not None and owner[li] is not None and "ref_kind" in owner[li]:
            it["ref"], it["ref_kind"] = owner[li]["ref"], owner[li]["ref_kind"]  # same list: its introduction
        # (an item above that is not measured yet - side-by-side lists, or one starting level with
        # this one - gives nothing to inherit: measure this item on its own below)
        elif li is not None and top - doc.lines[li].bbox[3] <= max_gap_em * mw.style.size:
            it["ref"], it["ref_kind"] = doc.lines[li].bbox[0], "intro"
        else:
            it["ref"], it["ref_kind"] = x0, "first item"
        # which list the item belongs to: the item above at the same marker position
        # (walking out of any sub-list in between); an item further right starts a sub-list
        o = owner[li] if li is not None else None
        while o is not None and doc.words[o["marker"]].bbox[0] > x0 + slack:
            o = o.get("parent")
        if o is not None and "list" not in o:  # the item above is not placed yet (side-by-side lists)
            o = None
        if o is not None and abs(doc.words[o["marker"]].bbox[0] - x0) <= slack:
            it["list"], it["parent"] = o["list"], o.get("parent")
        else:
            it["list"], it["parent"] = (doc.words[it["marker"]].page, it["marker"]), o
    return items


_ROMAN = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}


def _roman(t: str) -> int | None:
    t = t.lower()
    if not t or any(c not in _ROMAN for c in t):
        return None
    total = 0
    for k, c in enumerate(t):
        v = _ROMAN[c]
        total += -v if k + 1 < len(t) and _ROMAN[t[k + 1]] > v else v
    return total


def _enumerator(t: str) -> tuple[str, str] | None:
    """('b', '(X)') for "(b)": the enumerator and its format, None for a bullet glyph."""
    core = t.strip("()[].:")
    return (core, t.replace(core, "X", 1)) if core and core.isalnum() else None


def _values(core: str) -> dict[str, int]:
    """Every numbering style the enumerator can be read in, with its value."""
    out = {}
    if core.isdigit():
        out["1, 2, 3"] = int(core)
    if len(core) == 1 and core.isalpha():
        out["a, b, c" if core.islower() else "A, B, C"] = ord(core.lower()) - 96
    r = _roman(core)
    if r is not None and (core.islower() or core.isupper()):
        out["i, ii, iii" if core.islower() else "I, II, III"] = r
    return out


def _numbering(doc: Doc, items: dict[int, dict]) -> None:
    """Numbered lists: each item's numbering style ("a, b, c", "i, ii, iii", "1, 2, 3" ...),
    its value, format ("X." "X)" "(X)") and whether it continues its list's sequence.
    A list's style is the reading that makes its sequence count up best, so "i" after
    "h" is a letter and "i, ii, iii" is roman."""
    lists: dict[tuple, list[dict]] = defaultdict(list)
    for t in sorted(items):
        it = items[t]
        e = _enumerator(doc.words[it["marker"]].text)
        if e:
            it["enum"] = e
            lists[it["list"]].append(it)
    for members in lists.values():
        cands = [_values(it["enum"][0]) for it in members]
        styles = set().union(*cands)

        def score(st):
            vals = [c.get(st) for c in cands]
            return (sum(v is not None for v in vals),
                    sum(1 for a, b in zip(vals, vals[1:]) if a is not None and b is not None and b == a + 1),
                    vals[0] == 1)
        style = max(sorted(styles), key=score) if styles else None
        prev = None
        for it, c in zip(members, cands):
            v = c.get(style)
            it["style"], it["value"] = style, v
            # counting up by one; starting again at the first number (1, a, i) is a new list
            it["in_sequence"] = prev is None or v is None or v == prev + 1 or v == 1
            it["expected"] = None if prev is None else prev + 1
            it["previous"] = prev
            prev = v if v is not None else prev


def _item_pairs(u: Unit, ia: dict, ib: dict, min_words: int = 3) -> list[tuple[int, int]]:
    """The text diff's word pairs, with list items paired right when the lists are read in another
    order (a two-column list read row by row on one side, column by column on the other): the
    diff then pairs one item's "1 x" with another item's. Two item starts are paired only when
    their first lines have the same words (the shorter line when one side wraps earlier); items
    left over are paired by that text when it names one item on each side."""
    def text(d, k):
        out = []
        for w in d.words[k:]:
            if w.line != d.words[k].line:
                break
            if w.norm:
                out.append(w.norm)
        return tuple(out)

    # the same first line - however short (“WAN/LAN port”, “Lid”) - or, from min_words on, one the start of the other
    same = lambda x, y: (bool(x) and x == y) or (min(len(x), len(y)) >= min_words and x[:len(y)] == y[:len(x)])
    pairs = [(i, j) for i, j in u.pairs if not (i in ia and j in ib) or same(text(u.a, i), text(u.b, j))]
    done_a, done_b = {i for i, _ in pairs}, {j for _, j in pairs}
    ta = {i: text(u.a, i) for i in ia if i not in done_a}
    tb = {j: text(u.b, j) for j in ib if j not in done_b}
    out = pairs
    for i, x in ta.items():
        hits = [j for j, y in tb.items() if same(x, y)]
        if len(hits) == 1 and sum(1 for z in ta.values() if same(z, tb[hits[0]])) == 1:
            out.append((i, hits[0]))
            del tb[hits[0]]
    return out


MARKER_KINDS = {"bullet marker", "numbering style", "numbering format", "numbering sequence", "marker glued"}

# interchangeable bullet glyphs ("-" used as a bullet, "•" ...): a design choice, not a content/marker
# issue - stage using one where prod uses another is not reported
_BULLET_GLYPHS = set("•●▪■◦‣·-–—*")


def _marker_same_row(doc: Doc, wb, mark_text: str) -> bool:
    """The marker's exact text sits elsewhere on the word's row (a table's own "No." column, read as
    its own cell next to the text, not glued before it like a hanging-indent list): present, only
    laid out differently - not a missing marker."""
    cy = (wb.bbox[1] + wb.bbox[3]) / 2
    half = max((wb.bbox[3] - wb.bbox[1]) / 2, 1.0)
    return any(w.page == wb.page and w is not wb and w.text == mark_text
               and abs((w.bbox[1] + w.bbox[3]) / 2 - cy) <= half for w in doc.words)


def _in_table(d: Doc, k: int) -> bool:
    """Word k inside a table the detector found (grid or rules only)."""
    from . import tables
    w = d.words[k]
    cx, cy = (w.bbox[0] + w.bbox[2]) / 2, (w.bbox[1] + w.bbox[3]) / 2
    return any(t[1][0] - 1 <= cx <= t[1][2] + 1 and t[1][1] - 1 <= cy <= t[1][3] + 1 and len(t[2]) >= 2
               for t in tables._raw(d, w.page))


def _bullets(u: Unit, positions: bool = True) -> list[Finding]:
    """Bullet / numbered list alignment, compared on items whose text matched.
    positions=False: only the markers (missing, different, numbering), not where they sit."""
    lcfg, rcfg = u.cfg["layout"], u.cfg["report"]
    if not lcfg.get("check_bullets", True):
        return []
    positions = positions and lcfg.get("check_bullet_position", True)
    marker, tol = _marker_test(lcfg), lcfg.get("bullet_tolerance_em", lcfg["line_height_tolerance_em"])
    A, B = u.a, u.b
    gap = lcfg["bullet_intro_max_gap_em"]
    ia, ib = _list_items(A, u.a_range, marker, tol, gap), _list_items(B, u.b_range, marker, tol, gap)
    _numbering(A, ia)
    _numbering(B, ib)
    # the text starts its own line (in a table: its own cell), i.e. nothing before it that a
    # marker could hide behind
    line_start = lambda d, k: d.words[k].line_start
    kind_of = lambda d, x: (f"numbered {x['style']}" if x.get("style") else f"bullet “{d.words[x['marker']].text}”")
    groups: dict[tuple, list] = defaultdict(list)
    for i, j in _item_pairs(u, ia, ib):
        xa, xb = ia.get(i), ib.get(j)
        if not xa and not xb:
            continue
        wa, wb = A.words[i], B.words[j]
        if (xa and _cells_apart(A, xa["marker"], i)) or (xb and _cells_apart(B, xb["marker"], j)):
            # the “marker” is a table's numbering column (“12.” in the “No.” cell, “Dashboard” in the next one):
            # a column of the table, not a list marker - neither its presence nor its place is a list issue
            continue
        if not (xa and xb):  # a bullet / number on one side only, the other side's text starts its line plainly
            if (xa and line_start(B, j)) or (xb and line_start(A, i)):
                groups[(wa.role, "bullet marker", kind_of(A, xa) if xa else "none",
                        kind_of(B, xb) if xb else "none")].append((xa, xb, i, j))
            continue
        em_a, em_b = max(wa.style.size, 1), max(wb.style.size, 1)
        ma, mb = A.words[xa["marker"]], B.words[xb["marker"]]
        symbol = lambda t: not any(c.isalnum() for c in t)  # a bullet glyph; numbers / letters are content
        both_bullets = ma.text.strip() in _BULLET_GLYPHS and mb.text.strip() in _BULLET_GLYPHS
        if ma.text != mb.text and symbol(ma.text) and symbol(mb.text) and not both_bullets:
            groups[(wa.role, "bullet marker", f"“{ma.text}”", f"“{mb.text}”")].append((xa, xb, i, j))
        if xa.get("enum") and xb.get("enum"):
            # numbering: style (a, b, c vs i, ii, iii vs 1, 2, 3), format (a. vs a) vs (a)), sequence
            if xa["style"] != xb["style"]:
                groups[(wa.role, "numbering style", xa["style"], xb["style"])].append((xa, xb, i, j))
            elif xa["enum"][1] != xb["enum"][1]:
                groups[(wa.role, "numbering format", xa["enum"][1].replace("X", xa["enum"][0]),
                        xb["enum"][1].replace("X", xb["enum"][0]))].append((xa, xb, i, j))
            if xa["in_sequence"] and not xb["in_sequence"]:
                exp = _label(xb["style"], xb["expected"])
                what_b = ("repeated" if xb["value"] == xb["previous"] else
                          "skips a number" if xb["value"] is not None and xb["value"] > xb["expected"] else "out of order")
                groups[(wa.role, "numbering sequence", f"“{ma.text}”",
                        f"“{mb.text}” {what_b}" + (f" (expected “{xb['enum'][1].replace('X', exp)}”)" if exp else ""))
                       ].append((xa, xb, i, j))
        elif (xa.get("enum") is None) != (xb.get("enum") is None):  # numbered on one side, bullet on the other
            groups[(wa.role, "numbering style", xa.get("style") or f"bullet “{ma.text}”",
                    xb.get("style") or f"bullet “{mb.text}”")].append((xa, xb, i, j))
        # the marker touching its text ("15.12 V trigger jack", "16.RS-232"): no visible space in stage
        # where prod has one. Checked on its own - the gap is far smaller than the alignment tolerance
        glue = lcfg.get("bullet_glued_em", 0.12)
        gap_a, gap_b = (wa.bbox[0] - ma.bbox[2]) / em_a, (wb.bbox[0] - mb.bbox[2]) / em_b
        if gap_b < glue <= gap_a / 2 and wb.page == mb.page and abs(wb.bbox[1] - mb.bbox[1]) < em_b:
            groups[(wa.role, "marker glued", "", "")].append((xa, xb, i, j))  # one finding per role: gaps in the message
        if xb.get("above") and not xa.get("above"):
            # the number a line above its text (“5.” over “WAN/LAN port”), where prod sets it beside the text
            groups[(wa.role, "marker above", "beside its text", "a line above its text")].append((xa, xb, i, j))
        if not positions:
            continue
        if _cells_apart(A, xa["marker"], i) or _cells_apart(B, xb["marker"], j):
            # “A.” in a table's “No.” column, “Lid” in the “Item” column: two cells, not a list item - the
            # space between them is the column layout, not a bullet gap / indent
            continue
        if _in_table(A, i) or _in_table(B, j):
            continue  # a list inside a table cell: where its bullets sit is the cell's own layout
        vals = {
            # only comparable when both are measured against the same kind of line
            "bullet indent": ((ma.bbox[0] - xa["ref"]) / em_a, (mb.bbox[0] - xb["ref"]) / em_b)
                             if xa["ref_kind"] == xb["ref_kind"] else (None, None),
            "bullet gap": ((wa.bbox[0] - ma.bbox[2]) / em_a, (wb.bbox[0] - mb.bbox[2]) / em_b),
            "hanging indent": (None if xa["hang"] is None else xa["hang"] / em_a,
                               None if xb["hang"] is None else xb["hang"] / em_b),
        }
        for prop, (va, vb) in vals.items():
            if va is not None and vb is not None and abs(va - vb) > tol:
                groups[(wa.role, prop, f"{va:+.1f}em", f"{vb:+.1f}em")].append((xa, xb, i, j))

    # which item a paragraph belongs to is the list's structure (like a missing bullet), not a
    # measurement: compared with prod even when positions are judged against the spec only
    level_findings = _levels(u, ia, ib, tol, marker)

    what = {"bullet indent": "marker position from the line introducing the list", "bullet gap": "space between marker and text",
            "marker glued": "no space between the list number / bullet and its text in stage",
            "hanging indent": "wrapped lines from the item text", "bullet marker": "list marker",
            "numbering style": "how the list is numbered", "numbering format": "punctuation around the number",
            "numbering sequence": "the number does not follow the item before it",
            "marker above": "the list number is not on its item's line"}
    findings = []
    for (role, prop, x, y), items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if len(items) < lcfg.get("min_lines", 1):
            continue
        # the marker itself (added, missing, another bullet or dash, numbering, no space after it) is
        # content; only where it sits (indent, gap, hanging indent) is layout
        check = "content" if prop in MARKER_KINDS else "layout"
        a_idx = [k for xa, _, i, _ in items for k in ([xa["marker"], i] if xa else [i])]
        b_idx = [k for _, xb, _, j in items for k in ([xb["marker"], j] if xb else [j])]
        # link each marker to its counterpart - or, when it is missing on one side, to the spot
        # where it should be (the item's first word), so both screenshots show the same item
        pairs = [p for xa, xb, i, j in items
                 for p in [(xa["marker"] if xa else i, xb["marker"] if xb else j), (i, j)]]
        eg = "; ".join(f"“{_item_start(A, i)}”" for _, _, i, _ in items)
        text = f"[{role}] {prop} ({what[prop]}): {x} → {y} ({len(items)} items, e.g. {eg})"
        if prop == "bullet marker" and "none" in (x, y):
            # a bullet / number added or dropped: one finding per item, so each place is listed and shown
            added = x == "none"
            for xa, xb, i, j in items:
                mark = (B.words[xb["marker"]] if added else A.words[xa["marker"]]).text
                if not added and _marker_same_row(B, B.words[j], mark):
                    continue  # the number is there, read as its own table cell/column, not glued to the text
                # a number far left of the text on its row is not that text's list number: a step number beside a
                # figure ("2." under the picture column, "Remark:" in the text column 250 pt to its right)
                mw, tw = (B.words[xb["marker"]], B.words[j]) if added else (A.words[xa["marker"]], A.words[i])
                if tw.bbox[0] - mw.bbox[2] > 6 * max(tw.style.size, 1):
                    continue
                a1, b1 = ([i] if added else [xa["marker"], i]), ([xb["marker"], j] if added else [j])
                findings.append(Finding(
                    check, lcfg.get("severity", {}).get("indent", "warning"),
                    (f"{_marker_name(mark)} added in stage: “{mark}” before “{_item_start(A, i)}” (none in prod)" if added else
                     f"{_marker_name(mark)} missing in stage: “{mark}” before “{_item_start(A, i)}” in prod, none in stage"),
                    locs(A, a1, rcfg["max_locs"]), locs(B, b1, rcfg["max_locs"]),
                    {"role": role, "property": "indent", "kind": prop, "baseline": x, "candidate": y, "lines": 1},
                    types=["bullet marker", "indent", "bullet"],
                    links=paired_locs(A, B, [(xa["marker"] if xa else i, xb["marker"] if xb else j), (i, j)], rcfg["max_locs"])))
            continue
        if False:
            side, d, key = ("stage", A, 0) if y == "none" else ("prod", B, 1)
            marks = ", ".join(dict.fromkeys(f"“{d.words[it[key]['marker']].text}”" for it in items))
            has = "prod" if side == "stage" else "stage"
            text = (f"[{role}] List marker missing in {side}: {marks} before the items in {has}, "
                    f"none in {side} ({len(items)} items, e.g. {eg})")
        if prop == "marker above":
            marks = ", ".join(dict.fromkeys(f"“{B.words[it[1]['marker']].text}”" for it in items))
            text = (f"[{role}] List number not aligned with its item in stage: {marks} sit a line above the item text "
                    f"instead of beside it as in prod ({len(items)} items, e.g. {eg})")
        if prop == "marker glued":
            marks = ", ".join(dict.fromkeys(f"“{B.words[it[1]['marker']].text}”" for it in items))
            gap = lambda d, k, it: (d.words[it[k + 2]].bbox[0] - d.words[it[k]["marker"]].bbox[2]) / max(d.words[it[k + 2]].style.size, 1)
            x = f"{sum(gap(A, 0, it) for it in items) / len(items):+.2f}em"
            y = f"{sum(gap(B, 1, it) for it in items) / len(items):+.2f}em"
            text = (f"[{role}] List number / bullet touches its text in stage: no space after {marks} "
                    f"(gap {y} in stage, {x} in prod; {len(items)} items, e.g. {eg})")
        findings.append(Finding(
            check, lcfg.get("severity", {}).get("indent", "warning"),
            text,
            locs(A, a_idx, rcfg["max_locs"]), locs(B, b_idx, rcfg["max_locs"]),
            {"role": role, "property": "indent", "kind": prop, "baseline": x, "candidate": y, "lines": len(items)},
            types=[prop, "bullet"] if prop in MARKER_KINDS else ["marker above", "bullet"] if prop == "marker above"
            else ["indent", "bullet"],
            links=paired_locs(A, B, pairs, rcfg["max_locs"]),
        ))
    return findings + level_findings


_BOXES: dict[tuple, list] = {}


def _boxes(d: Doc, page: int) -> list[tuple]:
    """Shaded or framed rectangles on a page (callouts, notes): text in one is set by the box."""
    key = (d.path, page)
    if key not in _BOXES:
        out = []
        try:
            import pymupdf
            with pymupdf.open(d.path) as pdf:
                for dr in pdf[page].get_drawings():
                    r = dr["rect"]
                    if r.width > 80 and r.height > 14 and (dr.get("fill") is not None or
                                                            any(it[0] == "re" for it in dr["items"])):
                        out.append(tuple(r))
        except Exception:
            pass
        _BOXES[key] = out
    return _BOXES[key]


def _in_box(d: Doc, li: int) -> tuple | None:
    b = d.lines[li].bbox
    return next((r for r in _boxes(d, d.lines[li].page)
                 if r[0] - 1 <= b[0] and b[2] <= r[2] + 1 and r[1] - 1 <= b[1] and b[3] <= r[3] + 1), None)


def _cells_apart(d: Doc, m: int, t: int) -> bool:
    """Marker word m and text word t sit in different columns of a table on their page (a numbering
    column beside an item column), by the table's column grid."""
    from . import tables
    wm, wt = d.words[m], d.words[t]
    if wm.page != wt.page:
        return False
    cx = lambda w: (w.bbox[0] + w.bbox[2]) / 2
    cy = lambda w: (w.bbox[1] + w.bbox[3]) / 2
    for _, tbox, _, grid in tables._raw(d, wm.page):
        if not all(tbox[0] - 1 <= cx(w) <= tbox[2] + 1 and tbox[1] - 1 <= cy(w) <= tbox[3] + 1 for w in (wm, wt)):
            continue
        col = lambda x: next((k for k, (x0, x1) in enumerate(grid) if x0 - 1 <= x <= x1 + 1), None)
        cm, ct = col(cx(wm)), col(wt.bbox[0] + 1)
        return cm is not None and ct is not None and cm != ct
    return False


def _shares_row(d: Doc, li: int) -> bool:
    """Other text beside the line on the same row, or the line inside a table (it is not a
    paragraph of its own: a cell's text sits in the cell's column, not the list's)."""
    a = d.lines[li]
    from . import tables
    for t in tables._RAW.get((d.path, a.page)) or []:
        r = t[1]
        if r[0] - 1 <= a.bbox[0] and a.bbox[2] <= r[2] + 1 and r[1] - 1 <= a.bbox[1] and a.bbox[3] <= r[3] + 1:
            return True
    h = a.bbox[3] - a.bbox[1]
    for k in range(max(0, li - 40), min(len(d.lines), li + 40)):
        b = d.lines[k]
        if k == li or b.page != a.page:
            continue
        ov = min(a.bbox[3], b.bbox[3]) - max(a.bbox[1], b.bbox[1])
        if ov > 0.5 * min(h, b.bbox[3] - b.bbox[1]) and (b.bbox[0] >= a.bbox[2] or b.bbox[2] <= a.bbox[0]):
            return True
    return False


def _owners(d: Doc, items: dict, rng: tuple[int, int], tol_em: float, is_marker) -> dict[int, int | None]:
    """A list lays out in columns: the marker on the left, the item text on the right, and a nested
    list's markers in its parent's text column. For each paragraph that starts a line (not an item
    line, not a wrapped line of the paragraph above), the item whose text column it starts in - the
    paragraph belongs to that item ("Do not use if tilted ..." set under the second bullet's text is
    part of that bullet) - or None when it starts in no item's text column (body text)."""
    if rng[0] >= rng[1]:
        return {}
    by_line = {it["line"]: t for t, it in items.items()}
    out: dict[int, int | None] = {}
    stack: list[int] = []
    # x from the page's left margin: a list runs on over a page break (odd and even pages have
    # other margins) and the paragraph at the top of the next page still belongs to its item
    rel = lambda k: d.words[k].bbox[0] - d.left(d.words[k].page)
    l0, l1 = d.words[rng[0]].line, d.words[rng[1] - 1].line + 1
    for li in range(l0, l1):
        ln = d.lines[li]
        f = ln.first_word
        if f < rng[0] or f >= rng[1]:
            continue
        tol = tol_em * max(d.words[f].style.size, 1)
        if li in by_line:
            t = by_line[li]
            mx = rel(items[t]["marker"])
            while stack and rel(items[stack[-1]]["marker"]) >= mx - tol:
                stack.pop()  # a sibling or a shallower item closes the deeper ones
            stack.append(t)
            continue
        nxt = d.lines[li + 1].first_word if li + 1 < len(d.lines) else len(d.words)
        if nxt - f == 1 and is_marker(d.words[f].text):
            continue  # a bullet stored as its own line
        if _shares_row(d, li):
            continue  # a table cell, a label beside a picture: columns of their own, not a list's
        x = ln.bbox[0] - d.left(ln.page)

        def owner_by(xof, at, st) -> int | None:
            st = list(st)
            while st and xof(st[-1]) > at + tol:
                st.pop()
            return st[-1] if st and abs(xof(st[-1]) - at) <= tol else None
        # an item from an earlier page: margins measured on two pages may disagree, so it counts only
        # when the page positions and the margin-relative ones say the same
        carried = any(d.words[t].page != ln.page for t in stack)
        if carried and owner_by(rel, x, stack) != owner_by(lambda k: d.words[k].bbox[0], ln.bbox[0], stack):
            continue
        while stack and rel(stack[-1]) > x + tol:
            stack.pop()  # starts left of the item's text: not inside it
        prev = d.lines[li - 1] if li > 0 else None
        ended = d.words[f - 1].text[-1:] in ".:;!?。：" if f > 0 else True
        if prev is not None and prev.block == ln.block and prev.page == ln.page and not ended:
            continue  # the next line of a running paragraph: the hanging indent check covers it
        own = stack[-1] if stack and abs(rel(stack[-1]) - x) <= tol else None
        box = _in_box(d, li) if own is not None else None
        if box is not None and _in_box(d, items[own]["line"]) != box:
            own = None  # a note / tip box: its text is set by the box, not by the list above it
        out[li] = own
    return out


def _levels(u: Unit, ia: dict, ib: dict, tol_em: float, is_marker) -> list[Finding]:
    """A paragraph that belongs to a list item in prod (starts in that item's text column: under a
    bullet's text, not under the number of the item above it) must belong to the same item in stage,
    and body text must stay body text - the list's left / right columns are kept, not merged."""
    lcfg, rcfg = u.cfg["layout"], u.cfg["report"]
    A, B = u.a, u.b
    oa, ob = _owners(A, ia, u.a_range, tol_em, is_marker), _owners(B, ib, u.b_range, tol_em, is_marker)
    pair = dict(u.pairs)

    def where(d: Doc, items: dict, t: int | None) -> str:
        if t is None:
            return "body text (no list item)"
        mark = d.words[items[t]["marker"]].text
        return f"the text of the “{mark}” item “{_item_start(d, t)}”"

    findings = []
    for li, own_a in oa.items():
        f = A.lines[li].first_word
        j = pair.get(f)
        if j is None or not B.words[j].line_start or B.words[j].line not in ob:
            continue
        own_b = ob[B.words[j].line]
        if own_a is None and own_b is None:
            continue
        if own_a is not None and (own_a not in pair or pair[own_a] == own_b):
            continue  # same item (or its text is not matched: nothing to compare it with)
        if own_a is None and own_b is not None and own_b not in pair.values():
            continue
        a_idx, b_idx = [f] + ([own_a] if own_a is not None else []), [j] + ([own_b] if own_b is not None else [])
        findings.append(Finding(
            "layout", lcfg.get("severity", {}).get("indent", "warning"),
            f"List level differs in stage: “{_item_start(A, f, 8)}” starts under {where(A, ia, own_a)} in prod; "
            f"in stage it starts under {where(B, ib, own_b)}",
            locs(A, a_idx, rcfg["max_locs"]), locs(B, b_idx, rcfg["max_locs"]),
            {"property": "indent", "kind": "list level", "lines": 1},
            types=["list level", "indent", "bullet"],
            links=paired_locs(A, B, [(f, j)] + ([(own_a, pair[own_a])] if own_a is not None and own_a in pair else []),
                              rcfg["max_locs"])))
    return findings


def _marker_name(mark: str) -> str:
    return "List number" if any(c.isalnum() for c in mark) else "Bullet"


def _item_start(d: Doc, i: int, n: int = 4) -> str:
    """The first words of a list item, on its first line: “1 x Webcam accessory”, not just “1 x”."""
    out = []
    for w in d.words[i:i + n]:
        if w.line != d.words[i].line:
            break
        out.append(w.text)
    return " ".join(out)


def _label(style: str | None, v: int | None) -> str | None:
    """The enumerator for value v in a numbering style (3 -> "c", "iii", "3" ...)."""
    if style is None or v is None or v < 1:
        return None
    if style == "1, 2, 3":
        return str(v)
    if style in ("a, b, c", "A, B, C"):
        return chr(96 + v) if style == "a, b, c" and v <= 26 else chr(64 + v) if v <= 26 else None
    out, n = "", v
    for val, sym in ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"), (50, "l"),
                     (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
        while n >= val:
            out, n = out + sym, n - val
    return out if style == "i, ii, iii" else out.upper()
