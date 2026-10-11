"""The order of the TOC entries is compared only when both sides really print a table of contents.

Prod without one has its entries read from its PDF bookmarks - the document's reading order, not a
printed list - so stage listing its TOC in another order is not a TOC defect ("sequence wrong"); the
content checks report content that moved. See pdfval/toc.py::compare and [toc] compare_sequence_always.
"""
import pytest

from pdfval import engine, toc
from pdfval.toc import Toc, TocEntry

TITLES = ["Video input", "Adjusting the screen resolution", "Adjusting the screen refresh rate", "Preset display mode"]


def _toc(source: str, titles: list[str]) -> Toc:
    from pdfval import normalize
    return Toc(source, pages=[1] if source == "printed" else [],
               entries=[TocEntry(t, normalize.title(t), 1, k + 3, 1, (0, 0, 0, 0), 0.0, k + 3)
                        for k, t in enumerate(titles)])


@pytest.fixture
def cfg():
    return engine.load_config()


def _info(ta, tb, cfg):
    info, findings = toc.compare(ta, tb, cfg)
    return info["summary"], [f for f in findings if "order differs" in (f.types or [])]


def test_no_printed_toc_in_prod_means_the_order_is_not_judged(cfg):
    """Prod has no TOC: its bookmarks are in document order, stage prints its TOC alphabetically."""
    sm, bad = _info(_toc("bookmarks", TITLES), _toc("printed", sorted(TITLES)), cfg)
    assert not bad
    assert sm["sequence_compared"] is False and sm["sequence_ok"] is None
    assert sm["order differs"] == 0 and sm["match"] == len(TITLES)
    assert "no printed table of contents" in sm["sequence_why"]
    assert sm["status"] == "pass"


def test_both_sides_print_a_toc_so_the_order_is_judged(cfg):
    sm, bad = _info(_toc("printed", TITLES), _toc("printed", sorted(TITLES)), cfg)
    assert bad and sm["sequence_compared"] is True
    assert sm["order differs"] and sm["sequence_ok"] is False and sm["status"] == "fail"


def test_the_old_behaviour_can_be_switched_back_on(cfg):
    cfg["toc"]["compare_sequence_always"] = True
    sm, bad = _info(_toc("bookmarks", TITLES), _toc("printed", sorted(TITLES)), cfg)
    assert bad and sm["order differs"] and sm["sequence_ok"] is False


def test_entries_missing_or_added_are_still_reported_without_a_prod_toc(cfg):
    """Only the order goes: an entry stage does not list is still a TOC issue."""
    info, findings = toc.compare(_toc("bookmarks", TITLES), _toc("printed", TITLES[:2]), cfg)
    assert [f for f in findings if "missing entry" in (f.types or [])]
    assert info["summary"]["missing in stage"] == 2
