"""A list in a table cell that goes on on the next stage page, with its last items dropped: the part read
on the next page is the same text (not reported); only the items stage does not have are missing - not the
last item stage does have, whose list comma is gone (“RP8604,” in prod, “RP8604” ending the stage list)."""
from collections import Counter
from types import SimpleNamespace

from pdfval.checks import content


def _doc(tokens):
    return SimpleNamespace(words=[SimpleNamespace(norm=t) for t in tokens])


PROD = ["RM8604,", "RP6504,", "RP7504,", "RP8604,", "RE6504D,", "RE7504D,", "RE9804FVD"]
STAGE = ["RM8604,", "RP6504,", "RP7504,", "RP8604"]


def test_moved_run_takes_its_last_word_without_the_list_comma():
    d = _doc(PROD)
    relocated = content._parts(STAGE[:3])                       # read elsewhere in stage, word for word
    loose = content._list_cores(PROD) & content._list_cores(STAGE)  # ... and without a list comma: RP8604 too
    keep, _ = content._drop_moved_phrases(d, list(range(len(PROD))), Counter(relocated), loose=loose)
    assert [PROD[i] for i in keep] == ["RE6504D,", "RE7504D,", "RE9804FVD"]


def test_without_the_loose_words_the_edge_word_stays():
    d = _doc(PROD)
    keep, _ = content._drop_moved_phrases(d, list(range(len(PROD))), Counter(content._parts(STAGE[:3])))
    assert PROD[keep[0]] == "RP8604,"


def test_list_cut_short_in_place_is_the_missing_items_only():
    at, bt = ["RP7504,", "RP8604,", "RE6504D,", "RE7504D", "OSD"], ["RP7504,", "RP8604", "OSD"]
    ops = [("equal", 0, 1, 0, 1), ("replace", 1, 4, 1, 2), ("equal", 4, 5, 2, 3)]
    assert content._peel_list_edges(ops, at, bt) == [("equal", 0, 1, 0, 1), ("equal", 1, 2, 1, 2),
                                                     ("delete", 2, 4, 2, 2), ("equal", 4, 5, 2, 3)]


def test_a_full_stop_is_not_a_list_separator():
    at, bt = ["Mode", "Boost", "."], ["Mode", "Boost."]
    ops = [("equal", 0, 1, 0, 1), ("replace", 1, 3, 1, 2)]
    assert content._peel_list_edges(ops, at, bt) == ops


def test_another_punctuation_change_in_moved_text_is_not_absorbed():
    prod, stage = ["Press", "the", "key", "now."], ["Press", "the", "key", "now!"]
    d = _doc(prod)
    loose = content._list_cores(prod) & content._list_cores(stage)
    keep, _ = content._drop_moved_phrases(d, [0, 1, 2, 3], Counter(content._parts(stage[:3])), loose=loose)
    assert [prod[i] for i in keep] == ["now."]
