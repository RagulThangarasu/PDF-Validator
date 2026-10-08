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
from . import Unit, insertion_loc, insertion_loc_by, locs, paired_locs, snippet

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


def _marker_glyph(d, i: int) -> bool:
    """The word is a list item's marker glyph: one symbol (maybe repeated: "-", "•", "·", "–", "▪", "--") that starts
    its line, with the item's text after it on that row. A dash inside a sentence ("10 - 20") is not."""
    import unicodedata
    w = d.words[i]
    t = w.text.strip()
    if not t or len(t) > 3 or len(set(t)) != 1 or t[0] in ".,:;!?/\\'\"()[]{}@#%&_|" \
            or unicodedata.category(t[0]) not in ("Po", "Pd", "So"):
        return False
    if not w.line_start:
        return False
    nxt = d.words[i + 1] if i + 1 < len(d.words) else None
    return nxt is not None and nxt.page == w.page and nxt.bbox[0] > w.bbox[0] and \
        abs((nxt.bbox[1] + nxt.bbox[3]) / 2 - (w.bbox[1] + w.bbox[3]) / 2) <= max(4.0, 0.7 * (w.bbox[3] - w.bbox[1]))


def _wrap_only(u: Unit, a_idx: list[int], b_idx: list[int]) -> bool:
    """Same characters on both sides, and every place where the split into words
    differs is a line break right after a hyphen/slash on the side that has the
    split ("SL6504/" | "SL7504/", "Cortex-" | "A73", soft-hyphenated "But-" | "ton"): the text is present, it only
    wraps to the next line somewhere else (layout, not content). A break anywhere
    else stands for a space, so "code." | "The" vs "code.The" stays a content diff."""
    def bounds(doc, idx):
        out, off = {}, 0
        for k, i in enumerate(idx[:-1]):
            t = re.sub(r"\s+", "", doc.words[i].norm)
            off += len(t)
            # a hyphen / slash / dash, or a soft hyphen ("But\xad" | "ton": the PDF shows "But-" only where the
            # line breaks; normalising drops it from the word)
            nxt = doc.words[idx[k + 1]]
            # ... or no hyphen at all: a word too long for its narrow table cell, broken where the cell ends
            # ("Connec" | "tor", "Recept" | "acle") - the piece is the only word on its line and the next line goes
            # on in lower case. Two words of a sentence that lost their space ("your" | "laptop") share their
            # lines with other words: still a difference.
            # (the same when the cell's line also holds the next cell's text: the rest of the word then sits right
            # below its first piece - in a sentence a wrap runs from the line's right end to the next line's left start)
            w0 = doc.words[i]
            over = min(w0.bbox[2], nxt.bbox[2]) - max(w0.bbox[0], nxt.bbox[0])
            below = w0.page == nxt.page and 0 < nxt.bbox[1] - w0.bbox[1] <= 2.5 * max(w0.bbox[3] - w0.bbox[1], 1) and \
                over >= 0.5 * min(w0.bbox[2] - w0.bbox[0], nxt.bbox[2] - nxt.bbox[0])
            forced = t[-1:].isalpha() and (nxt.norm[:1].islower()) and \
                (len(doc.lines[w0.line].text.split()) == 1 or below)
            out[off] = doc.words[i].line != nxt.line and len(t) > 1 and \
                (t[-1] in "-/–—" or doc.words[i].text.endswith("\u00ad") or forced)
        return out
    if "".join(u.a.words[i].norm for i in a_idx).replace(" ", "") != "".join(u.b.words[j].norm for j in b_idx).replace(" ", ""):
        return False
    ba, bb = bounds(u.a, a_idx), bounds(u.b, b_idx)
    diff = [ba[o] for o in ba.keys() - bb.keys()] + [bb[o] for o in bb.keys() - ba.keys()]
    return bool(diff) and all(diff)


def _code_wrap(u: Unit, a_idx: list[int], b_idx: list[int]) -> bool:
    """A run of model / part codes ("T420/T650/TL550/TL650/" - letters, digits, slashes, no lower-case
    prose) can wrap to a new line anywhere a narrow table cell forces it: unlike a prose word, there is
    no hyphen/slash marking the split, and either side may pick up a stray hyphen at the break that is
    not part of the code. Same letters and digits once the break's own space/hyphen is taken out -
    layout, not content."""
    raw_a = "".join(u.a.words[i].text or "" for i in a_idx)
    raw_b = "".join(u.b.words[j].text or "" for j in b_idx)
    if not raw_a or not raw_b or re.search(r"[a-z]", raw_a) or re.search(r"[a-z]", raw_b):
        return False  # real lower-case prose: a dropped/added space there is a genuine content diff
    strip = lambda s: re.sub(r"[-‐‑–—\s]", "", s)
    na = strip("".join(u.a.words[i].norm or "" for i in a_idx))
    nb = strip("".join(u.b.words[j].norm or "" for j in b_idx))
    return bool(na) and na == nb


def _wrapped_apart(u: Unit, a_idx: list[int], b_idx: list[int]):
    """A changed block where most of the text is the same characters, only cut into words at other places
    because a line breaks elsewhere (“RP700/R” | “P701/RP552” in a narrow cell vs “RP700/RP701/” | “RP552/”),
    with a real difference somewhere inside: (prod words, stage words) that hold the difference - the rest
    is the same text on another line. None when the block is not of that kind: the two sides are unrelated,
    nothing is cut differently, or a cut that only one side has is not a line break (then a space is
    missing or extra, which is a difference of its own)."""
    def flat(doc, idx):
        text, owner, ends = "", [], {}
        for k, i in enumerate(idx):
            t = re.sub(r"\s+", "", doc.words[i].norm or "")
            text += t
            owner += [k] * len(t)
            if k + 1 < len(idx):
                ends[len(text)] = k
        return text, owner, ends
    A, wa, ea = flat(u.a, a_idx)
    B, wb, eb = flat(u.b, b_idx)
    if A == B or min(len(A), len(B)) < 8 or max(len(A), len(B)) > 4000:
        return None
    sm = SequenceMatcher(None, A, B, autojunk=False)
    if sm.ratio() < 0.6:
        return None
    da, db, to_a = set(), set(), {}
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            to_a.update((j1 + t, i1 + t) for t in range(i2 - i1))
            continue
        da.update(wa[i1:i2])
        db.update(wb[j1:j2])
        # text only one side has: the other side's word it would sit in / next to does not change
    # a place where only one side ends a word must be that side's line break, after a hyphen / slash or inside
    # a model code (no lower-case prose) - anywhere else the break stands for a space
    def is_wrap(doc, idx, k) -> bool:
        w, nxt = doc.words[idx[k]], doc.words[idx[k + 1]]
        t = re.sub(r"\s+", "", w.norm or "")
        code = not re.search(r"[a-z]", (w.text or "") + (nxt.text or ""))
        return w.line != nxt.line and len(t) > 1 and (t[-1] in "-/–—" or (w.text or "").endswith("\u00ad") or code)
    cuts_a = {o: k for o, k in ea.items() if k not in da and k + 1 not in da}
    cuts_b = {}
    for o, k in eb.items():
        if k in db or k + 1 in db or o not in to_a or o - 1 not in to_a or to_a[o] != to_a[o - 1] + 1:
            continue
        cuts_b[to_a[o]] = k
    differ = [is_wrap(u.a, a_idx, k) for o, k in cuts_a.items() if o not in cuts_b] + \
        [is_wrap(u.b, b_idx, k) for o, k in cuts_b.items() if o not in cuts_a]
    if not differ or not all(differ) or not (da or db):
        return None
    if len(da) == len(a_idx) and len(db) == len(b_idx):
        return None
    return [a_idx[k] for k in sorted(da)], [b_idx[k] for k in sorted(db)]


def _break_hyphen_only(u: Unit, a_idx: list[int], b_idx: list[int]) -> bool:
    """The only difference is a hyphen where one side breaks a word at the end of a line: “(Insta-” |
    “Show™)” in a narrow table cell vs “(InstaShow™)” on one line (either side). Each side's
    line-end hyphens followed by the word's rest on the next line are taken out; the texts must then
    be the same, and at least one such hyphen must have made the difference (a hyphen inside a line,
    “Insta-Show” vs “InstaShow”, stays a punctuation difference)."""
    def text(doc, idx) -> tuple[str, int]:
        out, n = [], 0
        for k, i in enumerate(idx):
            t = re.sub(r"\s+", "", doc.words[i].norm)
            nxt = idx[k + 1] if k + 1 < len(idx) else None
            if nxt is not None and doc.words[nxt].line != doc.words[i].line and len(t) > 1 \
                    and t[-1] in "-‐‑" and t[-2].isalnum():
                t, n = t[:-1], n + 1
            else:
                # the extractor already joined the word over the line break (“equip-” | “ment.” read as one
                # “components/equip-ment.”): the hyphen where the printed word ended is the break's
                raw = re.sub(r"\s+", "", doc.words[i].text)
                j = i + 1
                if len(raw) > 1 and raw[-1] in "-‐‑" and raw[-2].isalnum() and j < len(doc.words) \
                        and doc.words[j].line != doc.words[i].line and not doc.words[j].norm \
                        and t.startswith(raw) and len(t) > len(raw):
                    t, n = t[:len(raw) - 1] + t[len(raw):], n + 1
            out.append(t)
        return "".join(out), n
    ta, na = text(u.a, a_idx)
    tb, nb = text(u.b, b_idx)
    return (na or nb) > 0 and ta == tb


def _parts(tokens) -> Counter:
    """Tokens split after a hyphen/slash: "non-condensing" and "non-" + "condensing" count the
    same, whichever side joined the word across the line break."""
    return Counter(p for t in tokens for p in re.split(r"(?<=[-/–—])", t) if p)


def _mostly_relocated(c: Counter, pool: Counter, overlap: float) -> bool:
    """`c`'s words are found elsewhere in the document (lost on one side, extra on the other) for at
    least `overlap` share of them - not strictly every one: a big block read in another table/column
    order (so most of it is a straight relocation) should not stay "missing" just because a handful
    of its words (a reworded aside, an OCR slip) did not resolve to the exact same token."""
    if overlap >= 1.0:
        return not c - pool
    total = sum(c.values())
    if not total:
        return True
    return sum((c - pool).values()) <= (1 - overlap) * total


def _document_contains(doc: Doc, words: list[int], max_pages_away: int = 3) -> bool:
    """Check if all unique words from indices exist in document (normalized),
    but only on nearby pages (within ~max_pages_away). Detects reorganized/reformatted
    content in the same area (e.g., table restructured on adjacent page).
    Returns False for content that moved far away (likely to different section)."""
    if not words:
        return True
    if not doc.words:
        return False
    
    # Determine approximate location of expected content
    first_idx = min((i for i in words if i < len(doc.words)), default=None)
    if first_idx is None:
        return False
    first_page = doc.words[first_idx].page
    latest_page = first_page + max_pages_away
    
    # Words to find
    want = {(doc.words[i].norm or "").lower() for i in words if i < len(doc.words) and doc.words[i].norm}
    if not want:
        return True
    
    # Search only in nearby pages for efficiency and to avoid matching wrong sections
    found = set()
    for k, word in enumerate(doc.words):
        if word.page > latest_page:
            break  # Stop searching - we've gone too far, it's a different section
        if word.norm:
            w = word.norm.lower()
            if w in want:
                found.add(w)
    
    # All words must be found nearby (same area) to suppress "missing"
    return want <= found


def _list_cores(tokens) -> Counter:
    """The words without a list separator (comma / semicolon) at their end: “RP8604,” and “RP8604” are the
    same list item, whichever of them ends its list."""
    return Counter(c for t in tokens if (c := re.sub(r"[,;]+$", "", t)))


def _drop_moved_phrases(d, idx: list[int], relocated: Counter, min_words: int = 3,
                        loose: Counter | None = None) -> tuple[list[int], Counter]:
    """Remove runs of >= min_words words that all sit unmatched on the other side (the phrase only
    moved) from a diff block that also holds other text. Returns (what is left, relocated minus the
    removed words). A block that is all moved text is handled by the caller.
    loose (the words unmatched on both sides, without a list separator at their end - _list_cores): a word
    that ends a run but is on the other side too save for its list comma / semicolon - “RP8604,” where the
    other side's list ends with “RP8604” - moved with the run: one such word per run. Any other punctuation
    change in moved text is still a difference.)"""
    keep, run, left = [], [], Counter(relocated)
    near: set[int] = set()  # words of the run matched without their list separator
    loose_left = Counter(loose) if loose is not None else None

    def flush():
        nonlocal left
        parts = _parts(d.words[i].norm for i in run if i not in near)
        if len(run) >= min_words and not parts - left:
            left = left - parts
        else:
            keep.extend(run)
        run.clear()

    for i in idx:
        if not (_parts([d.words[i].norm]) - left):
            run.append(i)
            continue
        lb = _list_cores([d.words[i].norm]) if loose_left is not None else None
        if lb and run and not any(k in near for k in run) and not lb - loose_left:
            run.append(i)  # the run's last word, with another separator after it
            near.add(i)
            loose_left = loose_left - lb
            continue
        flush()  # this word did not move: ends a run
        keep.append(i)
    flush()
    if not keep:  # all of it moved: leave the decision to the caller
        return idx, relocated
    return (keep, left) if len(keep) < len(idx) else (idx, relocated)


def _landed(u: Unit, moved: list[int], kept: list[int], after: int) -> Loc | None:
    """Where in stage the moved part of a prod block was read, next to the part that is left: the stage
    word matching the moved run's last word (the rest follows it) or its first word (the rest precedes
    it), found by the run's last / first three words (letters and digits), from stage word `after` on."""
    if not moved or not kept:
        return None
    key = lambda d, k: _letters(d.words[k].norm)
    tail = moved[-1] < kept[0]
    if not tail and not moved[0] > kept[-1]:
        return None  # the moved words are scattered through the block
    want = [k for k in (key(u.a, i) for i in (moved[-3:] if tail else moved[:3])) if k]
    if len(want) < 2:
        return None
    pos = [j for j in range(max(after, u.b_range[0]), u.b_range[1]) if key(u.b, j)]
    for s0 in range(len(pos) - len(want) + 1):
        if [key(u.b, j) for j in pos[s0:s0 + len(want)]] == want:
            w = u.b.words[pos[s0 + len(want) - 1] if tail else pos[s0]]
            return Loc(w.page, w.bbox)
    return None


def _repeated_header(d, idx: list[int]) -> list[int]:
    """The leading words of idx when they are the first row of a page and the same row
    text already appeared earlier in the document: a table header repeated on a
    continuation page ("Menu item | Description" at the top of p.48). Else []."""
    if not idx:
        return []
    if idx[0] > 0 and d.words[idx[0] - 1].page == d.words[idx[0]].page:
        return _repeated_header_rows(d, idx)  # a later part of a header read column by column
    row = []
    for i in idx:
        if not same_row(d, idx[0], i):
            break
        row.append(i)
    nxt = row[-1] + 1
    if nxt < len(d.words) and same_row(d, idx[0], nxt):
        return _repeated_header_rows(d, idx)  # the row goes on beyond the block: maybe a header of rows
    ws = sorted((d.words[i] for i in row), key=lambda w: w.bbox[0])
    if not any(b.bbox[0] - a.bbox[2] > 2 * a.style.size for a, b in zip(ws, ws[1:])):
        rows = _repeated_header_rows(d, idx)  # one cell on the first row: a header of rows, or a label
        if not rows and _bar_header(d, row, idx[0]):
            return row  # a one-cell header bar ("GR10", white on the dark bar) repeated on the continuation page
        return rows
    seq, n = [d.words[i].norm for i in row], len(row)
    if any([w.norm for w in d.words[k:k + n]] == seq for k in range(idx[0] - n + 1)):
        # the first row may be only part of a header of several rows ("Resolution | Mode" beside cells
        # that wrap above and below it: "Vertical frequency (Hz)"): the whole repeated header, when longer
        rows = _repeated_header_rows(d, idx)
        return rows if len(rows) > len(row) else row
    return _repeated_header_rows(d, idx)


def _page_top_headers(d, idx: list[int]) -> set[int]:
    """Words of table headers repeated at the top of a page inside idx (a section's words): at each page
    start, the leading run that repeats, word for word, a run earlier in the document over 2+ rows
    (_repeated_header_rows), and sits in a table of that page (on its header bar or inside its box)."""
    from . import tables as tables_mod
    out: set[int] = set()
    for k in range(1, len(idx)):
        if d.words[idx[k]].page == d.words[idx[k - 1]].page:
            continue
        rest = [i for i in idx[k:] if d.words[i].page == d.words[idx[k]].page]
        run = _repeated_header_rows(d, rest, max_rows_pt=220)  # a header of 4-5 rows with wrapped cells is tall
        if not run:
            continue
        w = d.words[run[0]]
        try:
            boxes = [t[1] for t in tables_mod._raw(d, w.page)]
        except Exception:
            boxes = []
        cx, cy = (w.bbox[0] + w.bbox[2]) / 2, (w.bbox[1] + w.bbox[3]) / 2
        if _on_bar(d, run[0]) or any(b[0] - 2 <= cx <= b[2] + 2 and b[1] - 2 <= cy <= b[3] + 2 for b in boxes):
            out.update(run)
    return out


def _bar_header(d, row: list[int], before: int) -> bool:
    """The row is white text on a header bar, and the same white text came earlier in the document: the
    header bar of a table, repeated where the table goes on on the next page. (Plain text is not enough -
    one cell on top of a page may be a label that is repeated for a reason.)"""
    light = lambda w: len(w.style.color) == 7 and sum(int(w.style.color[k:k + 2], 16) for k in (1, 3, 5)) > 600
    if not all(light(d.words[i]) for i in row) or not _on_bar(d, row[0]):
        return False
    seq, n = [d.words[i].norm for i in row], len(row)
    return any([w.norm for w in d.words[k:k + n]] == seq and all(light(w) for w in d.words[k:k + n])
               for k in range(before - n + 1))


def _on_bar(d, i: int) -> bool:
    """The word sits on a dark band at least 40 % of the page wide: a table's header bar (not a white
    letter on a round callout marker)."""
    import pymupdf
    from . import tables as tables_mod
    w = d.words[i]
    try:
        pg = (tables_mod._DOCS.get(d.path) or tables_mod._DOCS.setdefault(d.path, pymupdf.open(d.path)))[w.page]
        c = pymupdf.Point((w.bbox[0] + w.bbox[2]) / 2, (w.bbox[1] + w.bbox[3]) / 2)
        return any(dr.get("fill") and sum(dr["fill"][:3]) < 1.2 and dr["rect"].width >= 0.4 * pg.rect.width
                   and dr["rect"].contains(c) for dr in pg.get_drawings())
    except Exception:
        return False


def _repeated_header_rows(d, idx: list[int], max_rows_pt: float = 90) -> list[int]:
    """A header of two or more rows repeated on a continuation page ("Screen Size | Distance from
    screen (mm)" over "Diagonal | H (mm) | W (mm) | Min length (max. zoom) | …", cells wrapping):
    the longest run of idx's leading words, in the band `max_rows_pt` below the top of the page's text,
    that repeats a run of the same words earlier in the document word for word, at least 4 words over
    2+ rows (a header read column by column comes as several such runs)."""
    page = d.words[idx[0]].page
    first = idx[0]
    while first > 0 and d.words[first - 1].page == page:
        first -= 1
    top = d.words[first].bbox[1]  # the header band: the top of the page's text
    if d.words[idx[0]].bbox[1] > top + max_rows_pt:
        return []
    lead = [i for i in idx if d.words[i].page == page and d.words[i].bbox[1] <= top + max_rows_pt]
    lead = idx[:next((k for k, i in enumerate(idx) if i not in set(lead)), len(idx))]
    norms = [d.words[i].norm for i in lead]
    best = 0
    for k in range(idx[0]):
        if d.words[k].norm != norms[0]:
            continue
        n = 0
        while n < len(norms) and k + n < idx[0] and d.words[k + n].norm == norms[n]:
            n += 1
        best = max(best, n)
    run = lead[:best]
    # the run ends where the header ends, not inside the first data cell under it: the first row of the
    # table's part on the earlier page may start with the same word as this page's first row (“OFF | HDR
    # content” there, “OFF (grayed out) | Non-HDR content” here) - that word is data of this page
    while run and run[-1] + 1 < len(d.words):
        w, nx = d.words[run[-1]], d.words[run[-1] + 1]
        if not (same_row(d, run[-1], run[-1] + 1) and -1 <= nx.bbox[0] - w.bbox[2] < 1.5 * w.style.size):
            break  # the next word is in another cell or row: the run ends on a whole cell
        run = run[:-1]
        while run and same_row(d, run[-1], run[-1] + 1) and d.words[run[-1] + 1].bbox[0] - d.words[run[-1]].bbox[2] < 1.5 * d.words[run[-1]].style.size:
            run = run[:-1]  # the rest of that cell
    if len(run) < 4 or len({round(d.words[i].bbox[1]) for i in run}) < 2:
        return []
    return run


def _letters(t: str) -> str:
    return "".join(c for c in t.lower() if c.isalnum())


def _loose_bag(tokens) -> Counter:
    """Letter / digit pieces of the words: "3GP(.3gp," and "3GP (.3gp," hold the same pieces
    (3gp, 3gp) however spaces and brackets split them, so re-spaced data is not absent data."""
    return Counter(k for t in tokens for k in re.findall(r"[^\W_]+", t.lower()))


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


def _sentence(doc, k: int, limit: int = 45) -> str:
    """The sentence holding word k on its page: back to the previous sentence end, on to the next one."""
    from .. import normalize
    end = lambda w: w.text.endswith((".", "!", "?", ":")) and not re.fullmatch(r"\d+\.|[A-Za-z]\.", w.text)
    page = doc.words[k].page
    lo = k
    while lo > 0 and k - lo < limit and doc.words[lo - 1].page == page and not end(doc.words[lo - 1]):
        lo -= 1
    hi = k
    while hi + 1 < len(doc.words) and hi - k < limit and doc.words[hi + 1].page == page and not end(doc.words[hi]):
        hi += 1
    return normalize.join_words(doc.words[i] for i in range(lo, hi + 1))


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
    return text  # the whole line: an issue is never shortened with "…"


def _space(n: int | None) -> str:
    return "no space" if n == 0 else "1 space" if n == 1 else f"{n} spaces"


def _peel_labels(ops, at, bt):
    """A callout label ("IMPORTANT:", folded to <label:…>) at the start or end of a changed block belongs to
    the next / previous note, not to the text beside it: 'signal"' -> 'signal <label:important>' is a
    quote change plus a label, not one changed text. The labels become their own insert / delete."""
    out = []
    lab = lambda t: t.startswith("<label:")
    for tag, i1, i2, j1, j2 in ops:
        if tag != "replace":
            out.append((tag, i1, i2, j1, j2))
            continue
        pre, post = [], []
        # also when the label is all the block has on its side: “Ambient light sensor” -> “NOTE:” is prod
        # text on its own plus a stage-only label (template), never one changed text
        while j2 - j1 >= 1 and lab(bt[j2 - 1]) and not any(lab(t) for t in at[i1:i2]):
            post.insert(0, ("insert", i2, i2, j2 - 1, j2)); j2 -= 1
        while j2 - j1 >= 1 and lab(bt[j1]) and not any(lab(t) for t in at[i1:i2]):
            pre.append(("insert", i1, i1, j1, j1 + 1)); j1 += 1
        while i2 - i1 >= 1 and lab(at[i2 - 1]) and not any(lab(t) for t in bt[j1:j2]):
            post.insert(0, ("delete", i2 - 1, i2, j2, j2)); i2 -= 1
        while i2 - i1 >= 1 and lab(at[i1]) and not any(lab(t) for t in bt[j1:j2]):
            pre.append(("delete", i1, i1 + 1, j1, j1)); i1 += 1
        rest = ("replace" if i1 < i2 and j1 < j2 else "delete" if i1 < i2 else "insert" if j1 < j2 else None, i1, i2, j1, j2)
        out += pre + ([rest] if rest[0] else []) + post
    return out


def _peel_list_edges(ops, at, bt):
    """Items dropped from (or added to) the end / start of a list: “RP7504, RP8604, RE6504D, … RE9804FVD” ->
    “RP7504, RP8604” is the missing items “RE6504D, … RE9804FVD”, not a changed text that names “RP8604,” as
    missing too. The word at the edge of a changed block that is the same on both sides but for its list
    separator (“RP8604,” / “RP8604”) is matched; what is left on one side only is the missing / extra block,
    placed right after (before) that word. Only when nothing but that is left: any other change stays whole."""
    core = lambda t: re.sub(r"[,;]+$", "", t)  # a list separator only: “Boost .” / “Boost.” is a word gap
    out = []
    for tag, i1, i2, j1, j2 in ops:
        if tag != "replace":
            out.append((tag, i1, i2, j1, j2))
            continue
        a1, a2, b1, b2 = i1, i2, j1, j2
        pre, post = [], []
        while a1 < a2 and b1 < b2 and core(at[a1]) and core(at[a1]) == core(bt[b1]):
            pre.append(("equal", a1, a1 + 1, b1, b1 + 1)); a1 += 1; b1 += 1
        while a1 < a2 and b1 < b2 and core(at[a2 - 1]) and core(at[a2 - 1]) == core(bt[b2 - 1]):
            post.insert(0, ("equal", a2 - 1, a2, b2 - 1, b2)); a2 -= 1; b2 -= 1
        if (pre or post) and (a1 == a2) != (b1 == b2):  # one side used up, the other has the items left over
            out += pre + [("delete" if a1 < a2 else "insert", a1, a2, b1, b2)] + post
        else:
            out.append((tag, i1, i2, j1, j2))
    return out


_QUOTES = "\"'“”‘’«»„"


def _merge_quotes(u: Unit, findings: list[Finding], words_of: dict) -> list[Finding]:
    """Quotation marks added / dropped around a phrase ("Switching input signal" -> Switching input signal)
    are one difference, reported as such with both quotes marked, however the phrase wraps."""
    # quotes around a word only - at its start, or at its end before closing punctuation ('signal".');
    # an apostrophe inside a word (projector's) is not a quotation mark
    qs = re.escape(_QUOTES)
    strip = lambda t: re.sub(f"[{qs}]+(?=[.,;:!?)]*$)", "", re.sub(f"^[{qs}]+", "", t))

    def by_box(d, rng, ls):  # word indices under a finding's boxes (for findings rebuilt by later steps)
        out = []
        for k in range(*rng):
            w = d.words[k]
            cx, cy = (w.bbox[0] + w.bbox[2]) / 2, (w.bbox[1] + w.bbox[3]) / 2
            if any(l.page == w.page and l.bbox[0] - 1 <= cx <= l.bbox[2] + 1 and l.bbox[1] - 1 <= cy <= l.bbox[3] + 1 for l in ls):
                out.append(k)
        return out
    for f in findings:
        if id(f) not in words_of or not all(words_of[id(f)]):
            words_of[id(f)] = (by_box(u.a, u.a_range, f.baseline), by_box(u.b, u.b_range, f.candidate))
    quote_only = []
    for f in findings:
        a_idx, b_idx = words_of.get(id(f), ([], []))
        if "punctuation" in (f.types or []) and a_idx and b_idx and len(a_idx) == len(b_idx) and \
                all(strip(u.a.words[i].norm) == strip(u.b.words[j].norm) for i, j in zip(a_idx, b_idx)):
            quote_only.append(f)
    out, used = [], set()
    for f in findings:
        if id(f) in used or f not in quote_only:
            if id(f) not in used:
                out.append(f)
            continue
        a_idx, b_idx = words_of[id(f)]
        # its partner: the next quote-only change within 12 words (the closing quote)
        mate = next((g for g in quote_only if g is not f and id(g) not in used
                     and 0 < words_of[id(g)][0][0] - a_idx[-1] <= 12), None)
        ia = a_idx + (words_of[id(mate)][0] if mate else [])
        ib = b_idx + (words_of[id(mate)][1] if mate else [])
        span_a = list(range(ia[0], ia[-1] + 1))
        span_b = list(range(ib[0], ib[-1] + 1))
        qa = sum(u.a.words[i].text.count(q) for i in ia for q in _QUOTES)
        qb = sum(u.b.words[j].text.count(q) for j in ib for q in _QUOTES)
        what = "missing in stage" if qa > qb else "added in stage" if qb > qa else "differ"
        phrase_a = " ".join(u.a.words[i].text for i in span_a)
        phrase_b = " ".join(u.b.words[j].text for j in span_b)
        rcfg = u.cfg["report"]
        g = Finding("content", f.severity, f"Quotation marks {what}: “{phrase_a}” in prod → “{phrase_b}” in stage",
                    locs(u.a, ia, rcfg["max_locs"]), locs(u.b, ib, rcfg["max_locs"]),
                    {**f.detail, "kind": "quotes", "baseline_text": phrase_a, "candidate_text": phrase_b},
                    types=["punctuation"], links=paired_locs(u.a, u.b, list(zip(ia, ib)), rcfg["max_locs"]))
        words_of[id(g)] = (ia, ib)
        used.add(id(f))
        if mate:
            used.add(id(mate))
        out.append(g)
    return out


def check(u: Unit) -> list[Finding]:
    ccfg, rcfg = u.cfg["content"], u.cfg["report"]
    ai = [i for i in range(*u.a_range) if u.a.words[i].norm]
    bi = [i for i in range(*u.b_range) if u.b.words[i].norm]
    # the section's own heading line is compared as the section heading (structure check), not as body
    # text: normally it aligns as a trivial match and stripping it is a no-op, but a section that starts
    # before its heading (text beside the heading read first, see engine.pull_back) can leave it unpaired
    # on one side only - reported as "extra"/"missing" content for text that both sides actually show
    def head(d, anc) -> set[int]:
        if anc is None or not anc.located or anc.word >= len(d.words):
            return set()
        # the heading is its title's words, on however many lines it wraps (prod: "...Kit/Pin:" | "PL490/.../PH5502",
        # stage: "...Kit/Pin: PL490/PL552/PL553/" | "PH5501/PH5502") - not just its first line, or the rest of a
        # heading wrapped at another word is compared as body text and reads as changed
        key = lambda t: re.sub(r"[^\w]+", "", (t or "").lower())
        want, got, k = key(anc.title), "", anc.word
        while want and k < len(d.words) and len(got) < len(want):
            nxt = got + key(d.words[k].text)
            if not want.startswith(nxt):
                break
            got, k = nxt, k + 1
        if want and got == want:
            return set(range(anc.word, k))
        k, line = anc.word, d.words[anc.word].line
        while k < len(d.words) and d.words[k].line == line:
            k += 1
        return set(range(anc.word, k))

    ha, hb = head(u.a, u.a_anchor), head(u.b, u.b_anchor)
    ai, bi = [i for i in ai if i not in ha], [j for j in bi if j not in hb]
    if ccfg.get("ignore_repeated_headers", True):
        # a table header repeated at the top of a continuation page is pagination: left out before the text
        # is matched (matched first, its words pair with look-alike words nearby - "Color space" of the
        # repeated header against the row label under it - and what is left no longer reads as a header)
        ra, rb = _page_top_headers(u.a, ai), _page_top_headers(u.b, bi)
        if ra or rb:
            u.repeated_headers = (sorted(ra), sorted(rb))
            ai, bi = [i for i in ai if i not in ra], [j for j in bi if j not in rb]
    at = [u.a.words[i].norm for i in ai]
    bt = [u.b.words[i].norm for i in bi]

    sm = SequenceMatcher(None, at, bt, autojunk=False)
    u.similarity = sm.ratio() if (at or bt) else 1.0
    ops = _peel_list_edges(_peel_labels(sm.get_opcodes(), at, bt), at, bt)
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
    # same, ignoring punctuation: a block of bare numbers (a picture's own callout digits, "10 11 12")
    # read out of a table whose own column numbers them "10." "11." "12." is the same digits, only
    # the trailing dot differs - used as a fallback when the block is only such markers (not prose,
    # where "Note" / "NOTE:" must stay distinct)
    loose_relocated = _loose_bag(lost.elements()) & _loose_bag(pool.elements())
    marker_like = lambda idx, d: bool(idx) and all(re.fullmatch(r"\(?\d{1,4}[.)]?", d.words[i].norm or "") for i in idx)
    unmatched_a: Counter = Counter()
    findings = []
    moves: list[tuple[Finding, list[int], list[int]]] = []
    words_of: dict[int, tuple[list[int], list[int]]] = {}  # finding -> (prod words, stage words)
    matched = moved_words = hyphen_matched = 0

    def find_run(src, idx, dst, rng, near):
        """Where the words src[idx] sit on the other side: the same run of words (compared on letters
        and digits, so "projector," is "projector."), unmatched there if possible, nearest to `near`."""
        want = [k for k in (_letters(src.words[i].norm) for i in idx) if k]
        if not want:
            return None
        pos = [(k, t) for k in range(*rng) if (t := _letters(dst.words[k].norm))]
        toks = [t for _, t in pos]
        taken = {j for _, j in u.pairs} if dst is u.b else {i for i, _ in u.pairs}
        best = None
        for s0 in range(len(toks) - len(want) + 1):
            if toks[s0:s0 + len(want)] == want:
                run = [pos[s0 + m][0] for m in range(len(want))]
                key = (sum(k in taken for k in run), abs(run[0] - near) if near is not None else 0)
                if best is None or key < best[0]:
                    best = (key, run)
        return best[1] if best else None

    def moved_note(a_idx, b_idx, why: str, kind: str = "moved text"):
        """Text left out of the content difference (it is there in stage, only elsewhere): still
        reported, as info, so nothing prod has is dropped silently. Both sides point at the same
        words: the other side's run is looked up by its text, not taken from the diff block (which
        may hold other text that was unmatched at the same spot)."""
        if not a_idx and not b_idx:
            return
        if a_idx:
            b_run = find_run(u.a, a_idx, u.b, u.b_range, b_idx[0] if b_idx else None)
            if b_run:
                b_idx = b_run
        else:
            a_run = find_run(u.b, b_idx, u.a, u.a_range, None)
            if a_run:
                a_idx = a_run
        if kind == "repeated header":
            return  # a table header repeated on the next page is expected: nothing to report
        if kind == "moved text":
            # the same words are on both sides (that is what makes them "relocated"), only read in
            # another order or on another line: a list number "3." read after its text, "1. Power /"
            # vs "Power / 1.". Position is not an issue. What counts is how they are set - bold vs
            # plain, font, size, colour - so they go to the style comparison, and no finding
            if a_idx and b_idx:
                u.style_pairs.extend(same_words(u, a_idx, b_idx))
            return
        src = (u.a, a_idx) if a_idx else (u.b, b_idx)
        findings.append(Finding(
            "content", "info", f"{why}: “{snippet(*src)}”",
            locs(u.a, a_idx, rcfg["max_locs"]), locs(u.b, b_idx, rcfg["max_locs"]),
            {"op": "relocated", "baseline_text": snippet(u.a, a_idx, 200) if a_idx else "",
             "candidate_text": snippet(u.b, b_idx, 200) if b_idx else "", "words": max(len(a_idx), len(b_idx))},
            types=[kind], links=paired_locs(u.a, u.b, same_words(u, a_idx, b_idx), rcfg["max_locs"]) if a_idx and b_idx else []))

    for n, (tag, i1, i2, j1, j2) in enumerate(ops):
        landed = None  # the stage spot of this block's moved part, when it has one
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
        a_at = insertion_loc_by(u.a, ai[i1 - 1] if i1 > 0 else None, ai[i1] if i1 < len(ai) else None, u.b, b_idx, bi[j1 - 1] if j1 > 0 else None, bi[j2] if j2 < len(bi) else None) if not a_idx else None
        b_at = insertion_loc_by(u.b, bi[j1 - 1] if j1 > 0 else None, bi[j1] if j1 < len(bi) else None, u.a, a_idx, ai[i1 - 1] if i1 > 0 else None, ai[i2] if i2 < len(ai) else None) if not b_idx else None
        # the same text cut into words at other places (a model list wrapping elsewhere in its cell) is judged
        # as what it is - a wrap or a word gap - before its odd pieces can count as words stage lacks
        kind = classify(at[i1:i2], bt[j1:j2]) if tag == "replace" else ""
        critical = tag in ("delete", "replace") and absent >= crit_words and absent >= 0.5 * len(a_idx) \
            and kind not in ("spacing", "hyphenation")
        ctype = ("missing text" if tag == "delete" or critical else "extra text" if tag == "insert" else kind)
        sup_gap = ctype == "spacing" and u.cfg["content"].get("ignore_superscript_spacing", True) and \
            any(_superscript(u.a.words[k]) for k in a_idx) or ctype == "spacing" and \
            u.cfg["content"].get("ignore_superscript_spacing", True) and any(_superscript(u.b.words[k]) for k in b_idx)
        if ccfg.get("ignore_footnote_marks", True) and (
                (tag == "replace" and _footnote_mark_only(u, a_idx, b_idx))
                or (tag != "replace" and _lone_footnote_mark(u.a if a_idx else u.b, a_idx or b_idx))):
            # "button" vs "button¹": the same word with a raised footnote number on one side only (the note itself
            # is compared as text) - how the reference is set, not a changed word
            u.pairs.extend(zip(a_idx, b_idx))
            unmatched_a.subtract(at[i1:i2])
            pool.subtract(bt[j1:j2])
            hyphen_matched += i2 - i1
            continue
        if u.cfg["layout"].get("ignore_bullet_glyph", True) and (a_idx or b_idx) \
                and all(_marker_glyph(u.a, i) for i in a_idx) and all(_marker_glyph(u.b, j) for j in b_idx):
            # the glyph that marks a list item: "•" in one PDF, "-" in the other (or read as text on one side only,
            # the bullet being styling on the other) - the template's choice, not a text difference. Whether an
            # item has a marker at all is the list check's (bullet marker).
            u.pairs.extend(zip(a_idx, b_idx))
            unmatched_a.subtract(at[i1:i2])
            pool.subtract(bt[j1:j2])
            hyphen_matched += i2 - i1
            continue
        if ctype == "hyphenation" or (ctype == "spacing" and (_wrap_only(u, a_idx, b_idx) or sup_gap
                or _code_wrap(u, a_idx, b_idx))) \
                or (ctype == "punctuation" and (_break_hyphen_only(u, a_idx, b_idx) or _code_wrap(u, a_idx, b_idx))):
            # a word split / wrapped across lines on one side only: same text, a layout difference
            u.pairs.extend(zip(a_idx, b_idx))
            u.wraps.append((a_idx, b_idx))
            unmatched_a.subtract(at[i1:i2])
            pool.subtract(bt[j1:j2])
            hyphen_matched += i2 - i1
            continue
        apart = _wrapped_apart(u, a_idx, b_idx) if tag == "replace" else None
        if apart:
            # ... and with a real difference inside it: only the words that hold the difference go on, the
            # rest is the same text on another line
            wa_, wb_ = [i for i in a_idx if i not in set(apart[0])], [j for j in b_idx if j not in set(apart[1])]
            u.wraps.append((wa_, wb_))
            unmatched_a.subtract(u.a.words[i].norm for i in wa_)
            pool.subtract(u.b.words[j].norm for j in wb_)
            hyphen_matched += len(wa_)
            a_idx, b_idx = apart
            ta, tb = [u.a.words[i].norm for i in a_idx], [u.b.words[j].norm for j in b_idx]
            ctype = "missing text" if not b_idx else "extra text" if not a_idx else classify(ta, tb)
        if ccfg.get("ignore_repeated_headers", True):
            # a table header repeated at the top of a continuation page: pagination, not content
            ha, hb = _repeated_header(u.a, a_idx), _repeated_header(u.b, b_idx)
            if ha:
                unmatched_a.subtract(u.a.words[i].norm for i in ha)
                hyphen_matched += len(ha)
            if ha or hb:
                moved_note(ha, hb, "Repeated table header (continuation page)", "repeated header")
            a_idx, b_idx = a_idx[len(ha):], b_idx[len(hb):]

            # ... also after a page break inside the block: “Requirements” (last row of p.11) | “GR10” (the
            # header bar repeated on top of p.12) | “System” - the header is pagination, the rest is compared on
            def after_break(d, ids):
                for k in range(1, len(ids)):
                    if d.words[ids[k]].page != d.words[ids[k - 1]].page:
                        return _repeated_header(d, ids[k:])
                return []
            ma, mb = after_break(u.a, a_idx), after_break(u.b, b_idx)
            if (ma or mb) and ccfg.get("ignore_relocated", True):
                # only when the rest of the block then is text read in another order (and so no finding):
                # else the header stays in the block and the block is judged as before
                ra = _parts(u.a.words[i].norm for i in a_idx if i not in set(ma))
                rb = _parts(u.b.words[j].norm for j in b_idx if j not in set(mb))
                if ra - relocated_a or rb - relocated_b:
                    ma = mb = []
            elif not ccfg.get("ignore_relocated", True):
                ma = mb = []
            if ma:
                unmatched_a.subtract(u.a.words[i].norm for i in ma)
                hyphen_matched += len(ma)
            if ma or mb:
                moved_note(ma, mb, "Repeated table header (continuation page)", "repeated header")
                a_idx = [i for i in a_idx if i not in set(ma)]
                b_idx = [j for j in b_idx if j not in set(mb)]
        swap = _near_swap(ops, n, moved, at, bt, i1, i2, j1, j2) if ccfg.get("ignore_relocated", True) else None
        if swap and not _on_picture(u.a if a_idx else u.b, a_idx or b_idx) \
                and not _on_picture(u.b if a_idx else u.a, [(bi if a_idx else ai)[k] for k in swap]):
            # a short text unmatched on one side, the same words unmatched on the other a few blocks away: a cell
            # label read before its text in one PDF and after it in the other ("3D Format | Shows the current 3D
            # mode." vs "Shows the current 3D mode. | 3D Format") - the text is there, only in another order
            # counted like any relocated text, so the words are not available to explain another block as well
            relocated_a = relocated_a - _parts(u.a.words[i].norm for i in a_idx)
            relocated_b = relocated_b - _parts(u.b.words[j].norm for j in b_idx)
            moved_note(a_idx, b_idx, "Text on another line / in another order in stage")
            continue
        if ccfg.get("ignore_relocated", True):
            # each side on its own: words that sit unmatched on the other side only moved
            # (another line, another cell order) - "Sound mode" read before or after its cell text
            # the tolerance below only applies to a block of some size: a tiny one or two words is
            # already close to 100% "covered" by definition, so relaxing it there would swallow a
            # genuine one-word difference (a changed punctuation mark) along with the move
            overlap = ccfg.get("relocated_overlap", 0.85)
            min_words = ccfg.get("relocated_overlap_min_words", 15)
            ca, cb = _parts(u.a.words[i].norm for i in a_idx), _parts(u.b.words[j].norm for j in b_idx)
            a_rel = _mostly_relocated(ca, relocated_a, overlap if len(a_idx) >= min_words else 1.0)
            b_rel = _mostly_relocated(cb, relocated_b, overlap if len(b_idx) >= min_words else 1.0)
            # a side that is only several bare numbers (not prose, not a lone list marker): also try
            # ignoring the trailing dot/paren, so a picture's own callout digits ("10 11 12 ...") match
            # its table's own numbering ("10." "11." ...) read elsewhere
            if not a_rel and len(a_idx) >= 6 and marker_like(a_idx, u.a):
                a_rel = _mostly_relocated(_loose_bag(u.a.words[i].norm for i in a_idx), loose_relocated, overlap)
            if not b_rel and len(b_idx) >= 6 and marker_like(b_idx, u.b):
                b_rel = _mostly_relocated(_loose_bag(u.b.words[j].norm for j in b_idx), loose_relocated, overlap)
            if a_rel and b_rel:
                relocated_a, relocated_b = relocated_a - ca, relocated_b - cb  # counted as reordered in the match %
                u.style_pairs.extend(same_words(u, a_idx, b_idx) if a_idx and b_idx else [])  # style still compared
                moved_note(a_idx, b_idx, "Text on another line / in another order in stage")
                continue
            # one side alone only when the two sides are unrelated text and the moved part is
            # a phrase: "Tip" -> "TIP:" or a lone "2." stay a real difference
            if ctype == "changed text" and a_rel and len(a_idx) >= 2:
                moved_note(a_idx, [], "Text on another line / in another order in stage")
                relocated_a, a_idx = relocated_a - ca, []
            if ctype == "changed text" and b_rel and len(b_idx) >= 2:
                moved_note([], b_idx, "Text on another line / in another order in stage")
                relocated_b, b_idx = relocated_b - cb, []
            # a moved phrase glued to other text: "1 x Webcam accessory" (a list item read in another
            # column order) + "NOTE:" - drop the phrase, report only what is left
            sep_relocated = _list_cores(lost.elements()) & _list_cores(pool.elements())
            a_keep, relocated_a = _drop_moved_phrases(u.a, a_idx, relocated_a, loose=sep_relocated)
            b_keep, relocated_b = _drop_moved_phrases(u.b, b_idx, relocated_b, loose=sep_relocated)
            moved_note([i for i in a_idx if i not in set(a_keep)], [j for j in b_idx if j not in set(b_keep)],
                       "Text on another line / in another order in stage")
            # what is left of a prod block whose other part moved (a table cell going on on the next stage page:
            # “… RP7504, RP8604,” read there, “RE6504D, …” gone) belongs where that part landed in stage, not
            # where the surrounding text aligns
            landed = _landed(u, [i for i in a_idx if i not in set(a_keep)], a_keep,
                             bi[j1 - 1] if j1 > 0 else u.b_range[0]) if a_keep and not b_keep else None
            a_idx, b_idx = a_keep, b_keep
        if not a_idx and not b_idx:
            continue
        # one finding per line of text: a diff block can run on into words that only follow in reading
        # order - figure callouts "1 2" below "See Setting up your ideaCam." - and must not be reported
        # as one changed phrase; each separate piece is judged (and shown) on its own
        whole = (list(a_idx), list(b_idx))
        # only a change is split (words on both sides): text missing or extra on one side stays one
        # finding - the labels of one picture, a long missing content block
        short = bool(a_idx and b_idx) and max(len(a_idx), len(b_idx)) <= 12
        pieces_a = _pieces(u.a, a_idx) if short else ([a_idx] if a_idx else [])
        pieces_b = _pieces(u.b, b_idx) if short else ([b_idx] if b_idx else [])
        for k in range(max(len(pieces_a), len(pieces_b))):
            a_idx = pieces_a[k] if k < len(pieces_a) else []
            b_idx = pieces_b[k] if k < len(pieces_b) else []
            if (a_idx, b_idx) != whole or whole != (ai[i1:i2], bi[j1:j2]):  # narrowed: describe what is left
                ta, tb = [u.a.words[i].norm for i in a_idx], [u.b.words[j].norm for j in b_idx]
                tag = "replace" if a_idx and b_idx else "delete" if a_idx else "insert"
                absent = sum((_loose_bag(ta) - _loose_bag(pool.elements())).values())
                a_at = insertion_loc_by(u.a, ai[i1 - 1] if i1 > 0 else None, ai[i1] if i1 < len(ai) else None, u.b, b_idx, bi[j1 - 1] if j1 > 0 else None, bi[j2] if j2 < len(bi) else None) if not a_idx else None
                b_at = insertion_loc_by(u.b, bi[j1 - 1] if j1 > 0 else None, bi[j1] if j1 < len(bi) else None, u.a, a_idx, ai[i1 - 1] if i1 > 0 else None, ai[i2] if i2 < len(ai) else None) if not b_idx else None
                if not b_idx and landed is not None:
                    b_at = landed
                kind = classify(ta, tb) if tag == "replace" else ""
                critical = tag in ("delete", "replace") and absent >= crit_words and absent >= 0.5 * len(a_idx) \
                    and kind not in ("spacing", "hyphenation")
                ctype = ("missing text" if tag == "delete" or critical else "extra text" if tag == "insert" else kind)
            # before giving up on a block missing on stage: the exact same words, in the exact same order,
            # may still sit somewhere else in this unit's own stage range (a table cell that goes on, on a
            # later stage page the opcode diff did not pair up with this spot) - crawl the whole range once
            # more for an exact run before reporting it gone. Only for a block of some size: one or two
            # common words ("Overview", "Note") can coincide with unrelated, already-matched text elsewhere
            # in the same section and would wrongly swallow a genuine short difference.
            if tag == "delete" and len(a_idx) >= 3 and not b_idx:
                run = find_run(u.a, a_idx, u.b, u.b_range, None)
                if run:
                    moved_note(a_idx, run, "Text on another stage page")
                    continue
            same =same_words(u, a_idx, b_idx) if a_idx and b_idx else []
            u.style_pairs.extend(same)
            label = "Missing content block" if critical else _TYPE_LABEL.get(ctype, _KIND[tag])
            # what the reader sees: with the cross-reference "on page 36" that was left out of the comparison
            # (its full stop moved onto "interface"), so the text and the highlight are the real spot
            a_show, b_show = _shown(u.a, a_idx), _shown(u.b, b_idx)
            # a short difference (one character in Chinese/Japanese, a word or two) is shown in its line,
            # so the reader can find it: “废” → “州” in “有关 China WEEE 州弃电器电子产品回收处理”
            ctx = _context(u.b, b_idx) if b_idx else _context(u.a, a_idx) if a_idx else ""
            shown = snippet(u.b, b_show) if b_idx else snippet(u.a, a_show) if a_idx else ""
            ctx = f" in “{ctx}”" if ctx and ctx != shown and max(len(a_idx), len(b_idx)) <= 3 else ""
            ta_s, tb_s = snippet(u.a, a_show), snippet(u.b, b_show)
            note = _gap_note(ta_s, tb_s) if ctype == "spacing" else ""
            findings.append(Finding(
                "content", "error" if critical else sev,
                f"{label}: " + (note + " — " if note else "")
                + (f"“{ta_s}”" if a_idx else "")
                + (" → " if a_idx and b_idx else "") + (f"“{tb_s}”" if b_idx else "") + ("" if note else ctx),
                locs(u.a, a_show, rcfg["max_locs"]), locs(u.b, b_show, rcfg["max_locs"]),
                {"op": tag, "baseline_text": ta_s, "candidate_text": tb_s,
                 "words": max(len(a_idx), len(b_idx)), "absent_words": absent},
                baseline_at=a_at, candidate_at=b_at, critical=critical, types=[ctype],
            ))
            words_of[id(findings[-1])] = (list(a_idx), list(b_idx))
            # the whole sentence on each side, so a short difference reads in its place:
            # “… on page 20 for more information.” vs “… on page 19 on for more information.”
            near = lambda idx, all_, k1: idx[0] if idx else (all_[k1] if k1 < len(all_) else all_[k1 - 1] if k1 > 0 else None)
            sa, sb = near(a_idx, ai, i1), near(b_idx, bi, j1)
            findings[-1].detail["baseline_sentence"] = _sentence(u.a, sa) if sa is not None else ""
            findings[-1].detail["candidate_sentence"] = _sentence(u.b, sb) if sb is not None else ""

    spacing = _spacing(u, findings) if ccfg.get("check_spacing", True) else 0
    spacing += _space_before_stop(u, findings) if ccfg.get("check_spacing", True) else 0
    spacing += _paragraphs(u, findings) if ccfg.get("check_paragraphs", False) else 0
    unmatched_a, pool = +unmatched_a, +pool  # drop zero counts left by hyphenation matches
    matched += hyphen_matched
    reordered = sum((unmatched_a & pool).values())  # present in stage, only in another order
    missing = sum((unmatched_a - pool).values())
    extra = sum((pool - unmatched_a).values())
    matched += reordered
    total = len(at)
    # a move that stays at its place on the page, or is only list numbers / bullets ("2." read after
    # its item's text): reading order, not a difference (the words are paired, their style compared)
    marker_only = lambda idx: all(re.fullmatch(r"\(?\w{1,3}[.)]|[^\w\s]{1,3}", u.a.words[i].norm or "") for i in idx)
    # ... or too little to be text that moved: a callout number "1" (a circled badge in the sentence), a symbol, a
    # word or two read at another point - the order of a section's text is judged on its words, whatever the
    # page breaks, and such a token is still matched (not missing), only not reported as reordered
    min_words = ccfg.get("reorder_min_words", 3)
    too_small = lambda idx: sum(any(c.isalpha() for c in (u.a.words[i].norm or "")) for i in idx) < min_words
    in_place = {id(f) for f, a_idx, b_idx in moves
                if _visually_in_place(u, a_idx, b_idx) or marker_only(a_idx) or too_small(a_idx)}
    findings = [f for f in findings if id(f) not in in_place]
    findings = _split_unrelated(u, findings, words_of)
    findings = _pair_near(findings)
    findings = _pair_parts(u, findings, words_of)
    _house_style(u, findings, words_of)
    findings = [f for f in findings if not f.detail.get("icon_matches_label")]
    findings = [f for f in findings if not _looks_the_same(u, f, words_of)]
    findings = _merge_quotes(u, findings, words_of)
    findings, found_m, found_e = _present_unmatched(u, findings)
    if found_m or found_e:  # the words are in the other PDF's section: matched, not missing / extra
        reordered += found_m
        matched += found_m
        missing, extra = max(0, missing - found_m), max(0, extra - found_e)
    findings, swapped = _swapped_blocks(u, findings)
    if swapped:  # those words are on both sides: matched, not missing / extra
        reordered += swapped
        matched += swapped
        missing, extra = max(0, missing - swapped), max(0, extra - swapped)
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


def _present_unmatched(u: Unit, findings: list[Finding]) -> tuple[list[Finding], int, int]:
    """A missing (or extra) block of 3+ words whose every word is among the other PDF's words of this section
    that the comparison left unmatched: the text is there, only read at another point (table rows or cells in
    another order, a block set before / after a picture). Not a content difference: the finding is dropped and
    its words count as matched. Each unmatched word explains one block only. Returns (findings, prod words
    found, stage words found)."""
    min_words = u.cfg["content"].get("present_min_words", 3)
    key = lambda t: re.sub(r"[^\w°%+]+", "", t.lower())
    pa, pb = {i for i, _ in u.pairs}, {j for _, j in u.pairs}
    free_a = Counter(k for i in range(*u.a_range) if i not in pa and u.a.words[i].norm for k in [key(u.a.words[i].text)] if k)
    free_b = Counter(k for j in range(*u.b_range) if j not in pb and u.b.words[j].norm for k in [key(u.b.words[j].text)] if k)
    out, got_m, got_e = [], 0, 0
    # larger blocks first: a long block is the surest match for the words it needs
    order = sorted(findings, key=lambda f: -int((f.detail or {}).get("words", 0)))
    drop = set()
    for f in order:
        types = f.types or []
        if types == ["missing text"]:
            text, pool = f.detail.get("baseline_text") or "", free_b
        elif types == ["extra text"]:
            text, pool = f.detail.get("candidate_text") or "", free_a
        else:
            continue
        bag = Counter(k for k in (key(t) for t in text.split()) if k)
        n = sum(bag.values())
        # a short block (1-2 words) only when its words are distinctive ("6.9W", "Standby"), not "to" / "the"
        distinct = all(any(ch.isdigit() for ch in k) or len(k) >= 4 for k in bag)
        if n == 0 or (n < min_words and not distinct) or any(pool[k] < c for k, c in bag.items()):
            continue
        pool.subtract(bag)
        drop.add(id(f))
        if types == ["missing text"]:
            got_m += n
        else:
            got_e += n
    return [f for f in findings if id(f) not in drop], got_m, got_e


def _swapped_blocks(u: Unit, findings: list[Finding]) -> tuple[list[Finding], int]:
    """A "missing" and an "extra" finding of the section with exactly the same words (case and punctuation
    ignored): the text is on both sides, only read in another order (short blocks swapped around a table or
    a heading - "Blurred image key." / "key. Blurred image."). One info note instead of two differences."""
    key = lambda t: re.sub(r"[^\w]+", "", t.lower())
    bag = lambda f, side: Counter(k for k in (key(t) for t in (f.detail.get(side + "_text") or "").split()) if k)
    miss = [f for f in findings if (f.types or []) == ["missing text"] and not f.critical]
    extra = [f for f in findings if (f.types or []) == ["extra text"]]
    drop, out_new, words = set(), [], 0
    for m in miss:
        bm = bag(m, "baseline")
        if not bm or sum(bm.values()) > 40:
            continue
        e = next((e for e in extra if id(e) not in drop and bag(e, "candidate") == bm), None)
        if e is None:
            continue
        drop.update((id(m), id(e)))
        words += sum(bm.values())
        if sum(any(c.isalpha() for c in t) for t in bm.elements()) < u.cfg["content"].get("reorder_min_words", 3):
            continue  # a number or a word or two read at another point: present, not worth a finding
        out_new.append(Finding(
            "content", u.cfg["content"].get("reorder_severity", "info"),
            f"Reordered: “{m.detail.get('baseline_text', '')}” (stage reads “{e.detail.get('candidate_text', '')}”)",
            m.baseline, e.candidate, {"op": "move", "baseline_text": m.detail.get("baseline_text", ""),
                                      "candidate_text": e.detail.get("candidate_text", ""), "words": sum(bm.values())},
            types=["reordered"]))
    if not drop:
        return findings, 0
    return [f for f in findings if id(f) not in drop] + out_new, words


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
            if kind == "spacing" and "".join(ta) == "".join(tb) and (_stacked(m.baseline, ta) or _stacked(e.candidate, tb)):
                # a word too long for its table cell, broken onto the next line with no hyphen ("Recept" | "acle" =
                # "Receptacle"): layout, not a difference - neither piece is reported
                drop.update((id(m), id(e)))
                break
            m.message = f"{_TYPE_LABEL[kind]}: “{m.detail['baseline_text']}” → “{e.detail['candidate_text']}”"
            m.candidate, m.candidate_at, m.types = e.candidate, None, [kind]
            m.links = [(m.baseline[0], e.candidate[0])]
            m.detail = {**m.detail, "op": "replace", "candidate_text": e.detail["candidate_text"], "absent_words": 0}
            drop.add(id(e))
            break
    return [f for f in findings if id(f) not in drop]


def _stacked(locs_: list, toks: list[str]) -> bool:
    """The words are pieces of one word set one below the other (each piece its own line, the next right under it,
    going on in lower case): a forced break inside a narrow cell."""
    if len(toks) < 2 or len(locs_) != len(toks) or not all(t[:1].islower() for t in toks[1:]) \
            or not all(t[-1:].isalpha() for t in toks[:-1]):
        return False
    for a, b in zip(locs_, locs_[1:]):
        over = min(a.bbox[2], b.bbox[2]) - max(a.bbox[0], b.bbox[0])
        if a.page != b.page or not 0 < b.bbox[1] - a.bbox[1] <= 2.5 * max(a.bbox[3] - a.bbox[1], 1) \
                or over < 0.5 * min(a.bbox[2] - a.bbox[0], b.bbox[2] - b.bbox[0]):
            return False
    return True


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


def _icon_vs_label(u: Unit, f: Finding, at: Loc, norms: list[str]) -> None:
    """Stage prints a callout label where prod's note has only its icon: the icon says which label it
    stands for ([content] callout_icons: a pencil is a Note, an exclamation mark a Warning). The same
    label: not an issue (the finding is dropped). Another one: the callout's type differs."""
    from . import callout_icons
    icons = u.cfg["content"].get("callout_icons") or {}
    label = next((n[len("<label:"):-1] for n in norms if n.startswith("<label:")), None)
    if not u.cfg["content"].get("check_callout_icon_label", True) or not icons or not label:
        return
    box = callout_icons.icon_left_of(u.a, at)
    kind = callout_icons.kind(u.a, at.page, box) if box else None
    if kind is None or kind not in icons:
        return  # an icon not recognised: the label-only finding stands
    if label in {l.lower() for l in icons[kind]}:
        f.detail = {**f.detail, "icon_matches_label": True}
        return
    want = " / ".join(l.upper() for l in icons[kind])
    f.severity, f.types = u.cfg["content"].get("callout_type_severity", "warning"), ["callout type differs"]
    f.message = (f"Callout type differs: stage labels the note “{label.upper()}”, prod's note has the {kind} icon "
                 f"(a {want})")
    f.baseline = [Loc(at.page, box)]
    f.detail = {**f.detail, "prod_icon": kind, "stage_label": label}


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
            # the other side's note is where the text after the label went ("NOTE: To avoid damaging…"
            # → prod's "To avoid damaging…"), not the spot the surrounding text aligns to
            # found by its words (the first few after the label), not by the word pairing, which can
            # tie a common word ("To") to another spot when the two PDFs read columns in another order
            od, orng = (u.b, u.b_range) if side == "prod" else (u.a, u.a_range)
            # the note's text: the words right of / just below the label on the page (the label may be
            # read apart from its text, e.g. beside its icon)
            lw = d.words[idx[-1]]
            box = [k for k in range(len(d.words)) if k not in idx and d.words[k].page == lw.page
                   and lw.bbox[1] - 3 <= d.words[k].bbox[1] <= lw.bbox[3] + 3 * lw.style.size
                   and d.words[k].bbox[0] >= lw.bbox[0] - 6 * lw.style.size and d.words[k].norm]
            box.sort(key=lambda k: (round(d.words[k].bbox[1] / 3), d.words[k].bbox[0]))
            want = [t for t in (_letters(d.words[k].norm) for k in box[:6]) if t]
            pos = [(k, t) for k in range(*orng) if (t := _letters(od.words[k].norm))]
            hits = [pos[s0][0] for s0 in range(len(pos) - len(want) + 1)
                    if len(want) >= 3 and [t for _, t in pos[s0:s0 + len(want)]] == want]
            ref = (f.candidate_at if side == "prod" else f.baseline_at)
            near = lambda k: (od.words[k].page != ref.page, abs(od.words[k].bbox[1] - ref.bbox[1])) if ref else (0, 0)
            nxt = min(hits, key=near) if hits else None
            if nxt is not None:
                at = Loc(od.words[nxt].page, od.words[nxt].bbox)
                if side == "prod":
                    f.candidate_at = at
                else:
                    f.baseline_at = at
                    _icon_vs_label(u, f, at, norms)
        elif len(norms) <= 8 and any(re.sub(r"\W", "", n.lower()) == "continued" for n in norms):
            f.severity, f.critical, f.types = "info", False, ["continued header"]
            f.message = (f"Continuation header only in {side}: “{snippet(d, idx)}” "
                         f"(repeated at a page break; the page breaks differ)")
        elif norms and all(re.fullmatch(r"\(?\w{1,3}[.)]", n) for n in norms) and all(
                n in {(u.b if side == "prod" else u.a).words[k].norm for k in range(*(u.b_range if side == "prod" else u.a_range))}
                for n in norms):
            # a list number alone ("3.") that the other side has too: the item is read in another order
            # (two columns), not extra or missing content - the list check compares the numbering
            f.severity, f.critical, f.types = "info", False, ["list marker"]
            f.message = f"List number read in another order: “{snippet(d, idx)}” (the {other} list has it too)"
        elif norms and all(not _letters(n) and len(n) <= 3 for n in norms) and all(d.words[i].line_start for i in idx):
            # "-" sub-bullets in prod, "•" in stage: a list marker, not data (the list check compares markers)
            f.severity, f.critical, f.types = "info", False, ["list marker"]
            f.message = f"List marker only in {side}: “{snippet(d, idx)}” (the {other} list uses another marker or none)"
        elif _page_number(d, idx):
            f.severity, f.critical, f.types = "info", False, ["page number"]
            f.message = f"Page number only in {side}: “{snippet(d, idx)}” (the page's header / footer, not content)"
        elif len(norms) <= 5 and _repeated_label(u, d, idx, side):
            f.severity, f.critical, f.types = "info", False, ["repeated label"]
            f.message = (f"Repeated label only in {side}: “{snippet(d, idx)}” (a label repeated on each page of a "
                         f"menu or table; the {other} PDF has it too, repeated on other pages)")


def _looks_the_same(u: Unit, f: Finding, words_of: dict) -> bool:
    """A text difference with nothing to see: both sides print exactly the same characters
    (“thickness.” → “thickness.”: the words were only split differently by the PDF)."""
    a_idx, b_idx = words_of.get(id(f), ([], []))
    if f.check != "content" or not a_idx or not b_idx or f.severity == "info":
        return False
    ta = "".join(u.a.words[i].text for i in a_idx)
    tb = "".join(u.b.words[j].text for j in b_idx)
    return ta == tb and " ".join(u.a.words[i].text for i in a_idx) == " ".join(u.b.words[j].text for j in b_idx)


def _page_number(d, idx: list[int]) -> bool:
    """A lone page number ("2", "iv") in the header or footer band of its page (top / bottom 10 %)."""
    if len(idx) != 1:
        return False
    w = d.words[idx[0]]
    if not re.fullmatch(r"\d{1,3}|[ivxlc]{1,6}|[IVXLC]{1,6}", w.text.strip()):
        return False
    h = d.pages[w.page].height
    return w.bbox[3] <= 0.1 * h or w.bbox[1] >= 0.9 * h


def _repeated_label(u: Unit, d, idx: list[int], side: str) -> bool:
    """A short label that is a text block of its own ("Picture Mode" in the left column of every page
    of a menu table, maybe wrapped over two lines) and that its PDF repeats (2+ times in the section)
    while the other PDF has it too, anywhere in the document: one copy more or less is a page
    break, not missing data."""
    def blocks(doc, rng):
        out: dict = {}
        for k in range(*rng):
            w = doc.words[k]
            if w.norm:
                out.setdefault(doc.lines[w.line].block, []).append(w.norm)
        return [" ".join(v) for v in out.values()]
    text = " ".join(d.words[i].norm for i in idx)
    blk = {d.lines[d.words[i].line].block for i in idx}
    if len(blk) != 1 or not d.words[idx[0]].line_start:
        return False
    own, other = (u.a, u.a_range), (u.b, (0, len(u.b.words)))
    if side == "stage":
        own, other = (u.b, u.b_range), (u.a, (0, len(u.a.words)))
    return blocks(*own).count(text) >= 2 and blocks(*other).count(text) >= 1


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
            if kind == "spacing" and (_soft_split_only(u, a_idx, b_idx) or _wrap_only(u, a_idx, b_idx)):
                # "pass­" | "word": prod hyphenates the word at a line break (a table cell read out of order), stage
                # has "password" - layout, not a difference; the words are explained, nothing is reported
                for donor, a_rest, b_rest in ((m, a_all[:i] + a_all[i + n:], words_of[id(m)][1]),
                                              (e, words_of[id(e)][0], [])):
                    _redescribe(u, donor, a_rest, b_rest, words_of, out)
                break
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


def _on_picture(d, idx: list[int], margin: float = 15) -> bool:
    """Any of the words lies on a picture or right beside it (a label printed on a screenshot or drawing, or
    a callout label just above / below it): its own kind of difference (image label missing), never a label
    read in another order."""
    from . import tables as tables_mod
    for i in idx:
        w = d.words[i]
        cx, cy = (w.bbox[0] + w.bbox[2]) / 2, (w.bbox[1] + w.bbox[3]) / 2
        boxes = [im.bbox for im in d.images if im.page == w.page and im.bbox[2] - im.bbox[0] > 40 and im.bbox[3] - im.bbox[1] > 30]
        boxes += list(tables_mod._figure_rects(d, w.page))
        if any(b[0] - margin <= cx <= b[2] + margin and b[1] - margin <= cy <= b[3] + margin for b in boxes):
            return True
    return False


def _near_swap(ops, n: int, moved: dict, at: list, bt: list, i1: int, i2: int, j1: int, j2: int,
               reach: int = 4, max_words: int = 6) -> list[int] | None:
    """Block n is text on one side only (up to max_words words), and the same words, in the same order, are
    unmatched on the other side within `reach` blocks before or after it. Returns their positions in the
    other side's token list (None: no such words)."""
    seq, other = (bt[j1:j2], 1) if i1 == i2 else (at[i1:i2], 0) if j1 == j2 else (None, None)
    if not seq or len(seq) > max_words or not any(any(ch.isalnum() for ch in t) for t in seq):
        return None
    for m in range(max(0, n - reach), min(len(ops), n + reach + 1)):
        tag, a1, a2, b1, b2 = ops[m]
        if m == n or tag == "equal" or m in moved:
            continue
        start = a1 if other == 1 else b1
        blk = at[a1:a2] if other == 1 else bt[b1:b2]
        for k in range(len(blk) - len(seq) + 1):
            if blk[k:k + len(seq)] == seq:
                return list(range(start + k, start + k + len(seq)))
    return None


def _soft_split_only(u: Unit, a_idx: list[int], b_idx: list[int]) -> bool:
    """The two sides read the same once a word split after a soft hyphen ("pass\u00ad" | "word") is joined:
    the only difference is where the PDF hyphenated a word at a line break."""
    def joined(d, idx):
        out = ""
        for k, i in enumerate(idx):
            out += d.words[i].norm
            if k + 1 < len(idx) and not d.words[i].text.endswith("\u00ad"):
                out += " "
        return out.split()
    soft = any(d.words[i].text.endswith("\u00ad") for d, idx in ((u.a, a_idx[:-1]), (u.b, b_idx[:-1])) for i in idx)
    return soft and joined(u.a, a_idx) == joined(u.b, b_idx)


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


_SUPER_CHARS = set("®™©℠¹²³⁰⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿ*†‡")


def _lone_footnote_mark(d, idx: list[int]) -> bool:
    """One raised number / footnote sign on its own ("¹" set as a separate small word after "button")."""
    if len(idx) != 1:
        return False
    w = d.words[idx[0]]
    t = (w.text or "").strip()
    return 0 < len(t) <= 2 and _superscript(w) and all(c.isdigit() or c in _SUPER_CHARS or c in "*†‡§" for c in t) \
        and not all(c in "®™©℠" for c in t)


def _footnote_mark_only(u: Unit, a_idx: list[int], b_idx: list[int]) -> bool:
    """The two runs read the same once raised digits / footnote signs are left out, and at least one was left out:
    "button" | "button¹" (the ¹ glued to the word), or "button" + a raised "1" as its own word."""
    def plain(d, idx):
        out, dropped = "", 0
        for i in idx:
            w = d.words[i]
            t, sc = w.text or "", w.script or ""
            for k, c in enumerate(t):
                raised = (k < len(sc) and sc[k] == "^") or c in _SUPER_CHARS
                if raised and (c.isdigit() or c in _SUPER_CHARS or c in "*†‡§"):
                    dropped += 1
                elif not c.isspace():
                    out += c
        return out, dropped
    (ta, na), (tb, nb) = plain(u.a, a_idx), plain(u.b, b_idx)
    return bool(ta) and ta == tb and (na or nb) > 0 and max(na, nb) <= 3


def _superscript(w) -> bool:
    """A raised word (footnote mark, ® ™, an exponent): the space before / after it is the typesetter's."""
    t = (w.text or "").strip()
    return "^" in (w.script or "") or bool(t) and all(c in _SUPER_CHARS for c in t)


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
        if u.cfg["content"].get("ignore_superscript_spacing", True) and \
                any(_superscript(w) for w in (A[i], A[i2], B[j], B[j2])):
            continue  # the gap beside a superscript ("Wi-Fi ®", "page 3 ¹") is not a word gap
        # A run of model codes ("T420/T650/TL550/TL650/..."): no lowercase letter means no real prose
        # space was ever meant here - a narrow table cell can force a mid-code wrap with no hyphen to mark
        # it, so any gap difference in such a run is layout, not content
        combined_a = (A[i].text or "") + (A[i2].text or "")
        combined_b = (B[j].text or "") + (B[j2].text or "")
        if combined_a and combined_b and not re.search(r"[a-z]", combined_a) and not re.search(r"[a-z]", combined_b):
            continue
        if A[i].line != A[i2].line or B[j].line != B[j2].line:
            continue  # words wrapped to different lines: spacing difference is from layout reflow, not content
        # Hyphenated word break: if one side's word ends with hyphen/dash and next word continues it,
        # the spacing difference is from hyphenation layout, not content ("Connec-" | "tor" vs "Connector")
        ta, ta_next = A[i].text or "", A[i2].text or ""
        tb, tb_next = B[j].text or "", B[j2].text or ""
        ta_norm, ta_next_norm = (A[i].norm or "").lower(), (A[i2].norm or "").lower()
        tb_norm, tb_next_norm = (B[j].norm or "").lower(), (B[j2].norm or "").lower()
        # Skip if prod's adjacent words form a hyphenated unit but stage has them as one:
        # "Connec-" + "tor" (prod) vs "Connector" (stage) - dehyphenate may have missed it
        if len(ta_norm) > 1 and ta_norm[-1] in "-‐‑–—" and ta_next_norm and \
                not any(c in ta_next_norm for c in " -‐‑–—\t"):  # next word doesn't start with hyphen/space
            # Prod side appears hyphenated; check if stage combined them
            combined_norm = ta_norm.rstrip("-‐‑–—") + ta_next_norm
            if combined_norm == tb_norm:  # Stage has the combined version
                continue  # This is a hyphenation difference, not spacing
        # Same check for stage side
        if len(tb_norm) > 1 and tb_norm[-1] in "-‐‑–—" and tb_next_norm and \
                not any(c in tb_next_norm for c in " -‐‑–—\t"):
            combined_norm = tb_norm.rstrip("-‐‑–—") + tb_next_norm
            if combined_norm == ta_norm:
                continue
        from .. import normalize
        if normalize.nospace_char(A[i].text[-1:]) or normalize.nospace_char(A[i2].text[:1]):
            # beside a Chinese / Japanese character the gap is the typesetter's CJK-Roman spacing
            # ("由 2 人" / "由2人", "燈 /" / "燈/"), not a space in the text
            continue
        count += 1
        findings.append(Finding(
            "content", sev,
            f"Word gap differs: “{A[i].text}{' ' * max(sa, 0)}{A[i2].text}” ({_space(sa)}) → "
            f"“{B[j].text}{' ' * max(sb, 0)}{B[j2].text}” ({_space(sb)})",
            locs(u.a, [i, i2]), locs(u.b, [j, j2]),
            {"op": "spacing", "baseline_spaces": sa, "candidate_spaces": sb, "words": 1}, types=["spacing"],
            links=paired_locs(u.a, u.b, [(i, j), (i2, j2)])))
    return count


def _space_before_stop(u: Unit, findings: list[Finding]) -> int:
    """A space before a full stop / comma / colon in stage (“see connection methods .”, “the base .”):
    typically where a cross-reference's “on page 12” was dropped and its full stop left behind. Reported
    when prod has no space there (prod's sentence ends “…methods on page 12.” or “…methods.”). One
    finding per place, the stage words and the prod words of the same spot highlighted."""
    A, B = u.a, u.b
    al = {j: i for i, j in u.pairs}
    sev = u.cfg["content"].get("spacing_severity", "warning")
    count = 0
    for j in range(max(u.b_range[0] + 1, 1), u.b_range[1]):
        w, prev = B.words[j], B.words[j - 1]
        if w.text not in (".", ",", ";", ":") or prev.page != w.page or prev.line != w.line:
            continue
        if not prev.space_after or prev.space_after < 1 or not re.search(r"\w$", prev.text):
            continue  # “/ .” (a key icon left out of the text) or no space
        i = al.get(j - 1)
        if i is None:
            # the word before the mark is not matched (prod: “location on page 22.” - stage: “location .”): the
            # prod spot from the nearest matched word before it, then prod's same word just after that
            k = next((k for k in range(j - 2, max(u.b_range[0], j - 12) - 1, -1) if k in al), None)
            if k is None:
                continue
            key = re.sub(r"\W", "", prev.text.lower())
            i = next((x for x in range(al[k], min(len(A.words), al[k] + 12))
                      if re.sub(r"\W", "", A.words[x].text.lower()) == key), al[k])
        # prod: the same word followed by the same mark after a space too -> both PDFs print it so
        if i + 1 < len(A.words) and A.words[i + 1].text == w.text and (A.words[i].space_after or 0) >= 1:
            continue
        nxt = next((k for k in range(i + 1, min(len(A.words), i + 8)) if A.words[k].text.rstrip().endswith(w.text)), None)
        a_idx = list(range(i, (nxt if nxt is not None else i) + 1))
        prod_txt = " ".join(A.words[k].text for k in a_idx)
        count += 1
        findings.append(Finding(
            "content", sev,
            f"Space before “{w.text}” in stage: “{prev.text} {w.text}” → prod “{prod_txt}”",
            locs(A, a_idx), locs(B, [j - 1, j]),
            {"op": "spacing", "kind": "space before punctuation", "words": 1}, types=["spacing"],
            links=paired_locs(A, B, [(i, j - 1)])))
    return count


def _shown(d, idx: list[int]) -> list[int]:
    """idx as the reader sees it: the words left out of the comparison (a cross-reference's "on page 36",
    normalize.fold_xref_pages) inside or right after the run are shown and highlighted with it."""
    if not idx:
        return idx
    out = list(idx)
    for k in range(min(idx), max(idx)):  # folded words between two words of the run
        if k not in idx and not d.words[k].norm:
            out.append(k)
    k = max(idx) + 1
    line = d.words[max(idx)].line  # ("on page 36" right after it, on its line - never a whole TOC page left out)
    while k < len(d.words) and not d.words[k].norm and d.words[k].text.strip() and d.words[k].line == line \
            and k - max(idx) <= 8:  # folded words after it
        out.append(k)
        k += 1
    return sorted(set(out))


def _gap_note(a: str, b: str) -> str:
    """A spacing difference in words: “stage has a space before “.”” / “prod has …” / “a space is missing …”."""
    for side, x, y in (("stage", b, a), ("prod", a, b)):
        m = re.search(r"\s+([.,;:!?)\]}%])", x)
        if m and re.sub(r"\s+([.,;:!?)\]}%])", r"\1", x) == y:
            return f"{side} has a space before “{m.group(1)}”"
        m = re.search(r"([(\[{])\s+", x)
        if m and re.sub(r"([(\[{])\s+", r"\1", x) == y:
            return f"{side} has a space after “{m.group(1)}”"
    if a.replace(" ", "") == b.replace(" ", ""):
        return "prod has a space stage does not" if a.count(" ") > b.count(" ") else "stage has a space prod does not"
    return ""


def _pieces(d, idx: list[int]) -> list[list[int]]:
    """Split a run of words where the next word is not on the same line or the next line of the same
    text: another page, or another text block that starts on a new row more than a line below
    (callout labels under a sentence) or above it."""
    out: list[list[int]] = []
    for i in idx:
        if out:
            p = out[-1][-1]
            wp, wi = d.words[p], d.words[i]
            gap = wi.bbox[1] - wp.bbox[3]
            if wp.page != wi.page or (_blk(d, p) != _blk(d, i) and not same_row(d, p, i)
                                      and (gap > 1.0 * max(wp.style.size, 1) or wi.bbox[3] < wp.bbox[1])):
                out.append([i])
                continue
            out[-1].append(i)
        else:
            out.append([i])
    return out


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


_CJK_END = "。！？：；"  # full-width marks that end a sentence / clause
_CJK_NOSTART = "，、。：；！？）」』】〉》"  # full-width marks a line never starts with


def forced_break(d, i, k) -> bool:
    """Words i and k on consecutive lines of one column, and word k would have fitted after word i:
    the line was ended on purpose ("Filmmaker" / "Switches Picture Mode ..."), not wrapped because it
    was full. The column's right edge is the widest line that starts where k's line starts."""
    wi, wk = d.words[i], d.words[k]
    if wi.page != wk.page or same_row(d, i, k) or not 0 <= wk.bbox[1] - wi.bbox[3] <= 2.5 * wi.style.size:
        return False
    from .. import normalize
    if (normalize.nospace_char(wi.text[-1:]) or normalize.nospace_char(wk.text[:1])) \
            and wi.text[-1:] not in _CJK_END + ".:;!?":
        # Chinese / Japanese text is set without spaces and breaks anywhere: a line that ends
        # mid-sentence there wrapped (a picture or frame beside it narrows the column), it was not
        # ended on purpose - a CJK line is ended by 。！？：； or the end of the paragraph
        return False
    li, lk = d.lines[wi.line], d.lines[wk.line]
    right = max((ln.bbox[2] for ln in d.lines if ln.page == wk.page and abs(ln.bbox[0] - lk.bbox[0]) <= 20
                 and ln.bbox[1] < lk.bbox[1] + 400 and ln.bbox[3] > lk.bbox[1] - 400), default=lk.bbox[2])
    need = (wk.bbox[2] - wk.bbox[0]) + 0.5 * wi.style.size
    return wi.bbox[2] == li.bbox[2] and li.bbox[2] + need < right - 2


def visible_break(d, i, k) -> bool:
    """A new paragraph between words i and k that looks like one: new text block, new row,
    and either the previous text ends a sentence or the next word starts one (capital,
    digit, symbol) after a line that stopped short of the margin.
    A plain line wrap ("first" / "time", "the" / "Notifications") is none of these."""
    if _blk(d, i) == _blk(d, k) or same_row(d, i, k):
        return False
    if not _would_fit(d, i, k):
        return False  # the next piece of text had no room on the line: the line wrapped, it did not end
    prev, nxt = d.words[i].text, d.words[k].text
    if nxt[:1] in _CJK_NOSTART:
        return False  # "，即可…": a Chinese / Japanese line never starts a paragraph with a comma or closing mark
    if prev[-1:] in ".:;!?)" + _CJK_END + "）":
        return True
    return not nxt[:1].islower() and not runs_to_margin(d, i)


def _would_fit(d, i, k) -> bool:
    """Would the text that starts at word k - up to the next real space, so "(❷)." as one piece: a
    bracket, a drawn number and ")." - fit after word i on i's line, within the column? The column's
    right edge is the widest nearby line that starts where k's line starts (a column narrowed by a
    picture beside it is narrower than the page's text box)."""
    wi, wk = d.words[i], d.words[k]
    if wi.page != wk.page:
        return True
    row = sorted((w for w in d.words if w.page == wk.page and w.bbox[1] < wk.bbox[3] and w.bbox[3] > wk.bbox[1]
                  and w.bbox[0] >= wk.bbox[0] - 0.5), key=lambda w: w.bbox[0])
    end, last = wk.bbox[2], wk.text
    for w in row:
        if w is wk or w.bbox[2] <= end:
            continue
        # glued on: a bracket, number or punctuation right after it ("(" "2" ")."), not the next word
        if w.bbox[0] > end + 0.6 * wk.style.size or (w.text[:1].isalpha() and not last.endswith(("(", "-", "/"))):
            break
        end, last = max(end, w.bbox[2]), w.text
    need = (end - wk.bbox[0]) + 0.25 * wi.style.size
    lk = d.lines[wk.line]
    right = max((ln.bbox[2] for ln in d.lines if ln.page == wk.page and abs(ln.bbox[0] - lk.bbox[0]) <= 20
                 and abs(ln.bbox[1] - lk.bbox[1]) <= 3.5 * wk.style.size), default=lk.bbox[2])  # this paragraph's lines
    return d.lines[wi.line].bbox[2] + need <= right + 1


def _paragraphs(u: Unit, findings: list[Finding]) -> int:
    """Paragraph breaks (the gap between paragraphs) present on one side only.
    Two consecutive words identical on both sides: a paragraph boundary between
    them in prod, while in stage they run on in the same line (or the reverse).
    Requiring 'same line' on the joined side keeps line wrapping out of it."""
    A, B = u.a, u.b
    sev = u.cfg["content"].get("paragraph_severity", "warning")

    def column_start(d, k) -> bool:
        """Word k starts at a column position: the first word of 2+ other lines within 150 pt starts at the
        same x - a label | value table without ruling lines ("Contrast ratio   50,000:1")."""
        w = d.words[k]
        starts = [d.words[d.lines[li].first_word] for li in range(max(0, w.line - 12), min(len(d.lines), w.line + 13))
                  if li != w.line and d.lines[li].page == w.page and abs(d.lines[li].bbox[1] - w.bbox[1]) <= 150]
        return sum(1 for x in starts if abs(x.bbox[0] - w.bbox[0]) <= 2 and x is not w) >= 2

    def joined(d, i, k) -> bool:  # same row AND a normal word gap (not two table cells level with each other)
        return same_row(d, i, k) and d.words[k].bbox[0] - d.words[i].bbox[2] <= 2 * d.words[i].style.size \
            and not column_start(d, k)
    pairs = sorted(u.pairs)
    count = 0
    for (i, j), (i2, j2) in zip(pairs, pairs[1:]):
        if i2 != i + 1 or j2 != j + 1:
            continue
        if (A.words[i].norm or "").startswith("<label:"):
            continue  # "Important: Use …" vs "Important:" on a line of its own: the callout's house style
        a_break, b_break = visible_break(A, i, i2), visible_break(B, j, j2)
        if not (a_break or b_break):  # a line broken on purpose on one side, run on in one line on the other
            if forced_break(A, i, i2) and joined(B, j, j2):
                msg = (f"Line break missing in stage: “{A.words[i].text}” ends its line in prod and “{A.words[i2].text}” "
                       f"starts the next one; in stage they run on in one line")
            elif forced_break(B, j, j2) and joined(A, i, i2):
                msg = (f"Extra line break in stage: “{A.words[i].text} {A.words[i2].text}” is one line in prod; in stage "
                       f"“{A.words[i].text}” ends its line and “{A.words[i2].text}” starts the next one")
            else:
                continue
            count += 1
            findings.append(Finding("content", sev, msg, locs(u.a, [i, i2]), locs(u.b, [j, j2]),
                                    {"op": "paragraph", "kind": "line break", "words": 1}, types=["paragraph break"],
                                    links=paired_locs(A, B, [(i, j), (i2, j2)])))
            continue
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
