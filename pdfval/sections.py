"""Section detection (outline or heading heuristics) and baseline<->candidate matching."""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from . import normalize
from .model import Anchor, Doc


def build_anchors(doc: Doc, cfg: dict) -> list[Anchor]:
    scfg = cfg["sections"]
    source = scfg.get("source", "auto")
    if source == "outline" or (source == "auto" and len(doc.outline) >= 3):
        anchors = _from_outline(doc, scfg)
        if scfg.get("unlisted_headings", True):
            anchors = _inject_unlisted_headings(doc, anchors)
    else:
        anchors = _from_headings(doc)
    skip = [re.compile(p, re.I) for p in scfg.get("skip", [])]
    anchors = [a for a in anchors if not any(s.search(a.norm) for s in skip)]
    if not scfg.get("front_matter", False):
        anchors = _drop_cover(doc, anchors, scfg.get("cover_max_words", 40))
    return anchors


def _drop_cover(doc: Doc, anchors: list[Anchor], max_words: int) -> list[Anchor]:
    """Headings / bookmarks on the cover page (the product code and manual title on page 1, the real
    chapters on later pages) are front matter, not sections: comparing them would report the cover as
    missing or changed sections. Only when the cover holds just a few words and chapters follow."""
    later = [a for a in anchors if a.page > 0]
    if not later or len(later) == len(anchors) or len(doc.pages) <= 2:
        return anchors
    if sum(1 for w in doc.words if w.page == 0) > max_words:
        return anchors
    # a cover has no running text: no body-size line of a few words (a paragraph starts a real section)
    if any(ln.page == 0 and abs(ln.size - doc.body_size) <= 0.6 and len(ln.text.split()) >= 5 for ln in doc.lines):
        return anchors
    return later


def _inject_unlisted_headings(doc: Doc, anchors: list[Anchor]) -> list[Anchor]:
    """A heading styled like one (bold, noticeably larger than body text) but never given its own PDF
    bookmark - e.g. a "Typographics" callout sitting on the same page as "General warranty information" -
    would otherwise be swallowed into whatever bookmarked section's word range happens to span that part
    of the page, and reported as that section's text missing from the matching stage page. Scan the gap
    between each pair of consecutive outline anchors for such lines and add them as unlisted anchors, one
    level deeper than the anchor whose gap they were found in, so they get matched (or reported missing)
    as sections of their own."""
    if not anchors:
        return anchors
    located = sorted((a for a in anchors if a.located), key=lambda a: a.word)
    out = list(anchors)
    seen_lines = {doc.words[a.word].line for a in located if 0 <= a.word < len(doc.words)}
    bounds = [(a.word, doc.words[a.word].line if 0 <= a.word < len(doc.words) else -1) for a in located]
    bounds.append((len(doc.words), len(doc.lines)))
    for k, a in enumerate(located):
        start_line, end_line = bounds[k][1] + 1, bounds[k + 1][1]
        for li in range(max(0, start_line), min(end_line, len(doc.lines))):
            if li in seen_lines:
                continue
            ln = doc.lines[li]
            if ln.size < doc.body_size + 1.5 or len(ln.text.split()) > 15 or ln.text.strip().isdigit():
                continue
            if ln.first_word < 0 or ln.first_word >= len(doc.words):
                continue
            w = doc.words[ln.first_word]
            if w.style.weight < 600:
                continue  # noticeably larger alone also catches body lines in a bigger running font
            title = ln.text.strip()
            out.append(Anchor(title, normalize.title(title), a.level + 1, ln.page, ln.bbox[1], ln.first_word))
            seen_lines.add(li)
    out.sort(key=lambda a: a.word)
    return out


def _from_outline(doc: Doc, scfg: dict) -> list[Anchor]:
    thr = scfg.get("locate_threshold", 0.8)
    anchors, min_line = [], 0
    tos = getattr(doc, "outline_to", None) or []
    flat = lambda t: "".join(t.lower().split())
    for n_entry, (level, title, page) in enumerate(doc.outline):
        if page < 1:
            continue
        norm = normalize.title(title)
        to_y = tos[n_entry] if n_entry < len(tos) else None
        best = None
        # the bookmark's own page number is occasionally wrong (pages inserted/removed after the PDF's
        # bookmarks were authored): when nothing matches right there, keep looking forward - but never past
        # the next bookmark's own page (or a configurable cap, for a very distant next bookmark), so this
        # entry can't steal the heading that really belongs to it, and a run-away scan stays bounded
        cap = scfg.get("locate_forward_pages", 60)
        next_page = next((doc.outline[k][2] for k in range(n_entry + 1, len(doc.outline)) if doc.outline[k][2] >= 1),
                         len(doc.pages))
        search_pages = [page - 1, page] + list(range(page + 1, max(page + 1, min(next_page, page + 1 + cap))))

        for p in search_pages:  # bookmark page, then next page, then forward to the next bookmark's own page
            for li in range(min_line, len(doc.lines)):
                ln = doc.lines[li]
                if ln.page < p:
                    continue
                if ln.page > p:
                    break
                cand = [ln.text]
                if li + 1 < len(doc.lines) and doc.lines[li + 1].page == p:
                    cand.append(ln.text + " " + doc.lines[li + 1].text)  # wrapped heading
                score = max(SequenceMatcher(None, norm, normalize.title(c)).ratio() for c in cand)
                if score < thr:
                    continue
                # which of several matching lines is the heading: the one the bookmark lands on (when the PDF
                # says where), written exactly like the title (a wrapped body line “Receiver.” is not the
                # heading “Receiver”), bold, larger - not simply the first one on the page
                near = int(to_y is not None and p == page - 1 and abs(ln.bbox[1] - to_y) <= 40)
                exact = int(any(flat(c) == flat(title) for c in cand))
                w = doc.words[ln.first_word] if 0 <= ln.first_word < len(doc.words) else None
                bold = int(bool(w) and w.style.weight >= 600)
                key = (round(score, 2), near, exact, bold, ln.size)
                if best is None or key > best[0]:
                    best = (key, ln.size, li)
            if best:
                break
        if best:
            li = best[2]
            ln = doc.lines[li]
            anchors.append(Anchor(title.strip(), norm, level, ln.page, ln.bbox[1], ln.first_word))
            min_line = li + 1
        else:  # heading text not found: anchor at top of bookmarked page
            if not doc.lines:
                break  # no text left at all (a scanned PDF, or everything was page furniture)
            li = next((i for i in range(min_line, len(doc.lines)) if doc.lines[i].page >= page - 1),
                      len(doc.lines) - 1)
            ln = doc.lines[li]
            anchors.append(Anchor(title.strip(), norm, level, ln.page, ln.bbox[1], ln.first_word, located=False))
            min_line = li
    return anchors


def _from_headings(doc: Doc) -> list[Anchor]:
    """Fallback when there is no outline: lines set noticeably larger than body text."""
    heads = [ln for ln in doc.lines
             if ln.size >= doc.body_size + 1.5 and len(ln.text.split()) <= 15 and not ln.text.strip().isdigit()]
    sizes = sorted({round(ln.size) for ln in heads}, reverse=True)
    return [Anchor(ln.text, normalize.title(ln.text), sizes.index(round(ln.size)) + 1,
                   ln.page, ln.bbox[1], ln.first_word) for ln in heads]


def _text_after(doc, an: Anchor, end: int | None = None, n: int = 60) -> set:
    """The words that follow a heading up to the next heading (its section's opening text), for telling
    same-titled headings apart."""
    if doc is None or not an.located:
        return set()
    out, k, end = [], an.word, len(doc.words) if end is None else end
    while k < end and len(out) < n:
        if doc.words[k].norm:
            out.append(doc.words[k].norm.lower())
        k += 1
    return set(out[len(an.norm.split()):])  # without the heading's own words


def match_anchors(a: list[Anchor], b: list[Anchor], cfg: dict, doc_a=None, doc_b=None) -> list[tuple[int, int, float]]:
    """Order-preserving fuzzy alignment (LCS / Needleman-Wunsch without gap cost).
    A title found more than once on a side (a sub-heading "WAN" inside "Information" and the "WAN"
    chapter itself) is paired with the heading whose opening text matches: a small bonus by content."""
    scfg = cfg["sections"]
    thr = scfg.get("title_match_threshold", 0.85)
    aliases = {normalize.title(k): normalize.title(v) for k, v in scfg.get("aliases", {}).items()}

    def sim(x: Anchor, y: Anchor) -> float:
        xn = aliases.get(x.norm, x.norm)
        if xn == y.norm:
            return 1.0
        s = SequenceMatcher(None, xn, y.norm).ratio()
        return s if s >= thr else 0.0

    n, m = len(a), len(b)
    S = [[sim(a[i], b[j]) for j in range(m)] for i in range(n)]
    # content bonus (< 0.3: never outweighs a title match) for titles that repeat on either side
    from collections import Counter as _C
    ca, cb = _C(x.norm for x in a), _C(y.norm for y in b)
    bonus: dict[tuple[int, int], float] = {}
    if doc_a is not None and doc_b is not None:
        ta: dict[int, set] = {}
        tb: dict[int, set] = {}
        nxt = lambda anchors, x: min((y.word for y in anchors if y.located and y.word > x.word), default=None)
        for i in range(n):
            for j in range(m):
                if S[i][j] and (ca[a[i].norm] > 1 or cb[b[j].norm] > 1):
                    wa = ta.setdefault(i, _text_after(doc_a, a[i], nxt(a, a[i])))
                    wb = tb.setdefault(j, _text_after(doc_b, b[j], nxt(b, b[j])))
                    if wa and wb:
                        bonus[(i, j)] = 0.25 * len(wa & wb) / max(1, min(len(wa), len(wb)))
    W0 = lambda x: 1.0 if x == 1.0 else 0.45 * x  # exact titles outweigh fuzzy ones
    W = lambda x, i=None, j=None: W0(x) + bonus.get((i, j), 0.0)
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            best = max(dp[i + 1][j], dp[i][j + 1])
            if S[i][j]:
                best = max(best, dp[i + 1][j + 1] + W(S[i][j], i, j))
            dp[i][j] = best
    pairs, i, j = [], 0, 0
    while i < n and j < m:
        if S[i][j] and dp[i][j] == dp[i + 1][j + 1] + W(S[i][j], i, j):
            pairs.append((i, j, S[i][j]))
            i, j = i + 1, j + 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    # moved sections: same title on both sides but out of order -> still compare them
    used_a, used_b = {p[0] for p in pairs}, {p[1] for p in pairs}
    for i in range(n):
        if i in used_a:
            continue
        for j in range(m):
            if j not in used_b and S[i][j] == 1.0:
                pairs.append((i, j, 1.0))
                used_a.add(i); used_b.add(j)
                break
    # renumbered sections: the same title under another number (“Appendix 3: Basic Troubleshooting Checklists
    # for X-Sign” in prod, “Appendix 4: …” in stage, the appendices in another order). The section is there -
    # its heading text differs - not a section missing in stage plus an extra one
    used_a, used_b = {p[0] for p in pairs}, {p[1] for p in pairs}
    for i in range(n):
        ka = _unnumbered(a[i].norm) if i not in used_a else None
        if not ka:
            continue
        for j in range(m):
            if j not in used_b and _unnumbered(b[j].norm) == ka:
                pairs.append((i, j, min(0.99, SequenceMatcher(None, a[i].norm, b[j].norm).ratio())))
                used_a.add(i); used_b.add(j)
                break
    return sorted(pairs)


_NUMBERED = re.compile(r"^(?:(?:appendix|chapter|part|section|annex)\s+(?:\d+(?:\.\d+)*|[ivxlc]+|[a-z])|\d+(?:\.\d+)*)\s*[:.\-–)]*\s+")


def _unnumbered(norm: str) -> str | None:
    """The title without its leading number (“appendix 3: basic troubleshooting …” -> “basic troubleshooting
    …”), when it has one and at least three words are left to tell the section by."""
    rest = _NUMBERED.sub("", norm, count=1)
    return rest if rest != norm and len(rest.split()) >= 3 else None
