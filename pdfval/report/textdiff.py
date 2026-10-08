"""What differs between the prod text and the stage text of an issue, for the reports to print in red: the words
only prod has (missing in stage) and the words only stage has (extra in stage).

Works in every language: text written with spaces is compared word by word; Chinese, Japanese, Korean and Thai,
which run on without spaces, character by character - so one changed character is red, not the whole sentence."""
from __future__ import annotations

import difflib
import re

# scripts written without spaces between words: each character is a unit
_NOSPACE = "฀-๿⺀-鿿가-힯豈-﫿＀-￯"
_TOKEN = re.compile(rf"(\s*)([{_NOSPACE}]|[^\s{_NOSPACE}]+)")


def _tokens(text: str) -> list[tuple[str, str]]:
    """[(space before, token)] - the space is kept so the pieces join back to the text as written."""
    return [(" " if sp else "", tok) for sp, tok in _TOKEN.findall(text or "")]


def pieces(expected: str, actual: str) -> tuple[list[tuple[str, bool]], list[tuple[str, bool]]]:
    """(prod pieces, stage pieces), each [(text, changed)]; the texts of a side joined give that side's text."""
    ta, tb = _tokens(expected), _tokens(actual)
    left: list = []
    right: list = []

    def add(out: list, toks: list, changed: bool) -> None:
        for sp, tok in toks:
            if sp:  # the space between two words is never red on its own
                out.append((sp, changed and bool(out) and out[-1][1]))
            out.append((tok, changed))

    sm = difflib.SequenceMatcher(None, [t for _, t in ta], [t for _, t in tb], autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        add(left, ta[i1:i2], op != "equal")
        add(right, tb[j1:j2], op != "equal")
    merge = lambda ps: [(t, c) for t, c in _merged(ps)]
    if left and left[0][0] == " ":
        left = left[1:]
    if right and right[0][0] == " ":
        right = right[1:]
    return merge(left), merge(right)


def _merged(ps: list) -> list:
    out: list = []
    for t, c in ps:
        if out and out[-1][1] == c:
            out[-1] = (out[-1][0] + t, c)
        else:
            out.append((t, c))
    return out


def flags(ps: list[tuple[str, bool]]) -> tuple[str, list[bool]]:
    """The side's text and, for each of its characters, whether it is part of a difference."""
    text = "".join(t for t, _ in ps)
    return text, [c for t, c in ps for _ in t]
