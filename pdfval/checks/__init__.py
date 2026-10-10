"""Per-section checks. Each check takes a Unit and returns a list of Findings.

A Unit is one matched section: a word range in the baseline and in the
candidate. The content check runs first and fills `unit.pairs` (word-index
pairs that are textually equal); style and layout checks compare those pairs,
so a CSS/alignment finding always refers to the *same text* on both sides.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field

from ..model import Anchor, Doc, Loc, Word


@dataclass
class Unit:
    id: str
    title: str
    a: Doc
    b: Doc
    a_range: tuple[int, int]
    b_range: tuple[int, int]
    a_anchor: Anchor | None
    b_anchor: Anchor | None
    cfg: dict
    pairs: list[tuple[int, int]] = field(default_factory=list)
    similarity: float = 1.0
    content: dict = field(default_factory=dict)  # content % and word counts, set by the content check
    image_pairs: list = field(default_factory=list)  # (prod Image, stage Image) matched by the assets check
    # same word on both sides although its text differs (case/punctuation/changed block):
    # not a content match, but its style can still be compared
    style_pairs: list[tuple[int, int]] = field(default_factory=list)
    wraps: list = field(default_factory=list)  # (prod word idxs, stage word idxs): same text, split over lines differently


class Aligner:
    """Maps a word position in one PDF to the corresponding position in the other,
    using the word pairs the content diff found identical (the ground truth for
    "where is this spot in the other document"). Built after the content check."""

    def __init__(self, u: Unit):
        self.u = u
        self.by_a = sorted(u.pairs)
        self.by_b = sorted(u.pairs, key=lambda p: p[1])
        self.a_keys = [p[0] for p in self.by_a]
        self.b_keys = [p[1] for p in self.by_b]
        self.a2b = dict(u.pairs)
        self.b2a = {b: a for a, b in u.pairs}

    def loc_in_b(self, i: int) -> Loc | None:
        """Where baseline word position i falls in the candidate (insertion point)."""
        n = bisect.bisect_left(self.a_keys, i)
        prv = self.by_a[n - 1][1] if n > 0 else None
        nxt = self.by_a[n][1] if n < len(self.by_a) else None
        o_prv = self.by_a[n - 1][0] if n > 0 else None
        o_nxt = self.by_a[n][0] if n < len(self.by_a) else None
        return insertion_loc_by(self.u.b, prv, nxt, self.u.a, [i] if i < len(self.u.a.words) else [], o_prv, o_nxt)

    def loc_in_a(self, j: int) -> Loc | None:
        n = bisect.bisect_left(self.b_keys, j)
        prv = self.by_b[n - 1][0] if n > 0 else None
        nxt = self.by_b[n][0] if n < len(self.by_b) else None
        o_prv = self.by_b[n - 1][1] if n > 0 else None
        o_nxt = self.by_b[n][1] if n < len(self.by_b) else None
        return insertion_loc_by(self.u.a, prv, nxt, self.u.b, [j] if j < len(self.u.b.words) else [], o_prv, o_nxt)

    def to_b(self, i: int) -> int | None:
        """Candidate word at/after baseline word i (next paired word)."""
        if not self.by_a:
            return None
        return self.by_a[min(bisect.bisect_left(self.a_keys, i), len(self.by_a) - 1)][1]

    def to_a(self, j: int) -> int | None:
        if not self.by_b:
            return None
        return self.by_b[min(bisect.bisect_left(self.b_keys, j), len(self.by_b) - 1)][0]

    @staticmethod
    def word_at(doc: Doc, rng: tuple[int, int], page: int, y: float) -> int | None:
        """First word in rng at or below (page, y)."""
        for i in range(*rng):
            w = doc.words[i]
            if (w.page, w.bbox[3]) >= (page, y):
                return i
        return None


def insertion_loc(doc: Doc, prev: int | None, nxt: int | None) -> Loc | None:
    """Position between two consecutive matched words: at the top of the next
    word, unless the next word is on a later page, then just below the previous
    one (so a gap at the end of a page is marked on that page, not the next)."""
    if nxt is not None and (prev is None or doc.words[prev].page == doc.words[nxt].page):
        w = doc.words[nxt]
        return Loc(w.page, w.bbox)
    if prev is not None:
        w = doc.words[prev]
        return Loc(w.page, (w.bbox[0], w.bbox[3] + 4, w.bbox[2], w.bbox[3] + 6))
    return None


def insertion_loc_by(doc: Doc, prev: int | None, nxt: int | None,
                     other: Doc, idx: list[int], o_prev: int | None, o_next: int | None) -> Loc | None:
    """Where text that exists on the other side only belongs on this side: next to the same neighbour it
    sits with over there. A table header row stage added directly above "Connect to 5GHz Wi-Fi" goes above
    prod's "Connect to 5GHz Wi-Fi" - even on the next page - not after the note that happens to precede it."""
    if idx and o_prev is not None and o_next is not None and prev is not None and nxt is not None:
        gap = lambda a, b: (abs(other.words[b].page - other.words[a].page) * 10000
                            + abs(other.words[b].bbox[1] - other.words[a].bbox[3]))
        if gap(idx[-1], o_next) < gap(o_prev, idx[0]):
            w = doc.words[nxt]
            return Loc(w.page, w.bbox)
        w = doc.words[prev]
        return Loc(w.page, (w.bbox[0], w.bbox[3] + 4, w.bbox[2], w.bbox[3] + 6))
    return insertion_loc(doc, prev, nxt)


def locs(doc: Doc, idxs, limit: int = 40) -> list[Loc]:
    """Merge word boxes into one rectangle per line."""
    out: list[Loc] = []
    last_line = None
    for i in idxs:
        w: Word = doc.words[i]
        if w.line == last_line:
            x0, y0, x1, y1 = out[-1].bbox
            out[-1].bbox = (min(x0, w.bbox[0]), min(y0, w.bbox[1]), max(x1, w.bbox[2]), max(y1, w.bbox[3]))
        else:
            if len(out) >= limit:
                break
            out.append(Loc(w.page, w.bbox))
            last_line = w.line
    return out


def paired_locs(a: Doc, b: Doc, pairs, limit: int = 40) -> list[tuple[Loc, Loc]]:
    """(prod box, stage box) per run of word pairs that sit on the same line on both
    sides, so both screenshots can highlight exactly the same words."""
    out: list[tuple[Loc, Loc]] = []
    last = None

    def grow(loc: Loc, box) -> None:
        x0, y0, x1, y1 = loc.bbox
        loc.bbox = (min(x0, box[0]), min(y0, box[1]), max(x1, box[2]), max(y1, box[3]))
    for i, j in sorted(pairs):
        wa, wb = a.words[i], b.words[j]
        key = (wa.line, wb.line)
        if key == last:
            grow(out[-1][0], wa.bbox)
            grow(out[-1][1], wb.bbox)
            continue
        if len(out) >= limit:
            break
        out.append((Loc(wa.page, wa.bbox), Loc(wb.page, wb.bbox)))
        last = key
    return out


def snippet(doc: Doc, idxs, n: int = 0) -> str:
    """The words as text - all of them (n is ignored: an issue is never shortened with "…")."""
    from .. import normalize
    return normalize.join_words(doc.words[i] for i in idxs)


from . import joined, brackets, highlights, assets, content, integrity, layout, placement, rows, style, tables, typography, inline_icons, picture_rows  # noqa: E402

# content must run first: it produces the word pairs every other check relies on;
# placement needs the image pairs from assets
PIPELINE = [content.check, joined.check, brackets.check, highlights.check, style.check, typography.check, layout.check, rows.check, tables.check, assets.check, inline_icons.check,
            placement.check, picture_rows.check, integrity.check]
