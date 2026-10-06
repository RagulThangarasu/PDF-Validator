"""A printed TOC over two pages set at different left margins (facing pages) has two levels, not four."""
from pdfval import toc


def _raw(page, x0, n, title="T"):
    return [(f"{title}{page}-{x0}-{k}", 10 + k, page, (x0, 100 + 12 * k, 400, 110 + 12 * k), x0) for k in range(n)]


def _edges(raw):
    return sorted({round(r[4]) for r in raw})


def test_second_page_shifted_sideways_is_moved_back():
    raw = _raw(0, 50.0, 8) + _raw(0, 78.0, 30) + _raw(1, 56.6, 2) + _raw(1, 85.0, 9)
    assert _edges(toc._unshift_pages(raw)) == [50, 78]


def test_page_of_sub_entries_only_is_left_alone():
    raw = _raw(0, 50.0, 8) + _raw(0, 78.0, 30) + _raw(1, 78.0, 12)   # a chapter's entries running on
    assert toc._unshift_pages(raw) == raw


def test_page_with_another_indent_scheme_is_left_alone():
    raw = _raw(0, 50.0, 8) + _raw(0, 78.0, 30) + _raw(1, 56.6, 2) + _raw(1, 70.0, 9)   # 70 - 6.6 is no edge of page 1
    assert toc._unshift_pages(raw) == raw


def test_same_margins_on_both_pages_is_unchanged():
    raw = _raw(0, 50.0, 8) + _raw(0, 78.0, 30) + _raw(1, 50.0, 3) + _raw(1, 78.0, 9)
    assert toc._unshift_pages(raw) == raw
