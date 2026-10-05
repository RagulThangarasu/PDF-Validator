"""“Tables merged / split”: parts of one table running over several pages are one table, also when a page
in between is not among the parts compared (p.43 and p.45 of a table that fills p.44 too). Two tables on
one page, or pages apart with no table between them, are two tables."""
from types import SimpleNamespace

from pdfval.checks import tables

DOC = SimpleNamespace(pages=[SimpleNamespace(height=842)] * 60, words=[])


def _t(page, y0, y1):
    return tables.TTable((page, 0), page, (45, y0, 547, y1), [])


def _raw(per_page):
    return lambda doc, page: [(0, box, [], []) for box in per_page.get(page, [])]


def test_parts_two_pages_apart_with_the_table_running_through_the_page_between(monkeypatch):
    monkeypatch.setattr(tables, "_raw", _raw({43: [(48, 57, 551, 754)]}))
    assert tables._one_table([], [_t(42, 79, 762), _t(44, 57, 130)], DOC)


def test_no_table_on_the_page_between_is_two_tables(monkeypatch):
    monkeypatch.setattr(tables, "_raw", _raw({}))
    assert not tables._one_table([], [_t(42, 79, 762), _t(44, 57, 130)], DOC)


def test_a_short_table_on_the_page_between_does_not_link_them(monkeypatch):
    monkeypatch.setattr(tables, "_raw", _raw({43: [(48, 300, 551, 420)]}))
    assert not tables._one_table([], [_t(42, 79, 762), _t(44, 57, 130)], DOC)


def test_two_tables_on_one_page_are_two_tables(monkeypatch):
    monkeypatch.setattr(tables, "_raw", _raw({}))
    assert not tables._one_table([], [_t(53, 414, 491), _t(53, 523, 600)], DOC)


def test_consecutive_pages_running_on_are_one_table(monkeypatch):
    monkeypatch.setattr(tables, "_raw", _raw({}))
    assert tables._one_table([], [_t(42, 79, 762), _t(43, 57, 754)], DOC)
