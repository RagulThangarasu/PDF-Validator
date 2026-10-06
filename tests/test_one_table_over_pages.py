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


# --- a table at the top of the next page under a header of its own is the next table, not a continuation
from collections import Counter


def _table(page, y0, y1, header, fills):
    rows = [tables.TRow((page, 0), page, (45, y0 + 20 * k, 547, y0 + 20 * (k + 1)), [], [], Counter(text.lower().split()))
            for k, text in enumerate([header, "static blue the device is presenting", "off the device is powered off"])]
    t = tables.TTable((page, 0), page, (45, y0, 547, y1), rows)
    for r, fill in zip(rows, fills):
        _FILLS[(page, r.box)] = fill
    return t


_FILLS: dict = {}
BAR, PLAIN = "#9f9fa0", "#ffffff"


def _bg(doc, page, box):
    return _FILLS.get((page, box))


def test_another_header_at_the_top_of_the_next_page_is_another_table(monkeypatch):
    monkeypatch.setattr(tables, "_background", _bg)
    monkeypatch.setattr(tables, "_raw", _raw({}))
    a = _table(9, 411, 606, "LED indicator on the Button Status Description", (BAR, PLAIN, PLAIN))
    b = _table(10, 57, 214, "LED indicator on the Receiver Status Description", (BAR, PLAIN, PLAIN))
    assert tables._own_header(DOC, a, b) and not tables._one_table([a, b], [a, b], DOC)


def test_the_same_header_repeated_is_a_continuation(monkeypatch):
    monkeypatch.setattr(tables, "_background", _bg)
    monkeypatch.setattr(tables, "_raw", _raw({}))
    a = _table(9, 411, 606, "LED indicator on the Button Status Description", (BAR, PLAIN, PLAIN))
    b = _table(10, 57, 214, "LED indicator on the Button Status Description", (BAR, PLAIN, PLAIN))
    assert not tables._own_header(DOC, a, b) and tables._one_table([a, b], [a, b], DOC)


def test_a_data_row_at_the_top_of_the_next_page_is_a_continuation(monkeypatch):
    monkeypatch.setattr(tables, "_background", _bg)
    monkeypatch.setattr(tables, "_raw", _raw({}))
    a = _table(9, 411, 606, "LED indicator on the Button Status Description", (BAR, PLAIN, PLAIN))
    b = _table(10, 57, 214, "Flashing red The device is unable to connect", (PLAIN, PLAIN, PLAIN))
    assert not tables._own_header(DOC, a, b) and tables._one_table([a, b], [a, b], DOC)
