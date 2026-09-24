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
    else:
        anchors = _from_headings(doc)
    skip = [re.compile(p, re.I) for p in scfg.get("skip", [])]
    return [a for a in anchors if not any(s.search(a.norm) for s in skip)]


def _from_outline(doc: Doc, scfg: dict) -> list[Anchor]:
    thr = scfg.get("locate_threshold", 0.8)
    anchors, min_line = [], 0
    for level, title, page in doc.outline:
        if page < 1:
            continue
        norm = normalize.title(title)
        best = None
        for p in (page - 1, page):  # bookmark page, then next page as fallback
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
                if score >= thr and (best is None or (score, ln.size) > (best[0], best[1])):
                    best = (score, ln.size, li)
            if best:
                break
        if best:
            li = best[2]
            ln = doc.lines[li]
            anchors.append(Anchor(title.strip(), norm, level, ln.page, ln.bbox[1], ln.first_word))
            min_line = li + 1
        else:  # heading text not found: anchor at top of bookmarked page
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


def match_anchors(a: list[Anchor], b: list[Anchor], cfg: dict) -> list[tuple[int, int, float]]:
    """Order-preserving fuzzy alignment (LCS / Needleman-Wunsch without gap cost)."""
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
    W = lambda x: 1.0 if x == 1.0 else 0.45 * x  # exact titles outweigh fuzzy ones
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            best = max(dp[i + 1][j], dp[i][j + 1])
            if S[i][j]:
                best = max(best, dp[i + 1][j + 1] + W(S[i][j]))
            dp[i][j] = best
    pairs, i, j = [], 0, 0
    while i < n and j < m:
        if S[i][j] and dp[i][j] == dp[i + 1][j + 1] + W(S[i][j]):
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
    return sorted(pairs)
