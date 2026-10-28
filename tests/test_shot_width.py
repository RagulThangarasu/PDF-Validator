"""Every screenshot is rendered to the same pixel width, whatever the document's page size.

Publications are not all the same shape - the sample pair is an InDesign page 668pt wide against an
A4 stage page - so at a fixed zoom their report pictures come out at different widths and the reports
do not line up. [report] shot_width fixes the width; the zoom follows from the page.
"""
import pymupdf
import pytest

from pdfval.report.shots import _PageCache


def _pdf(path, width, height=841.89):
    doc = pymupdf.open()
    pg = doc.new_page(width=width, height=height)
    pg.insert_text((40, 60), "Specifications", fontsize=16, fontname="hebo")
    doc.save(path)
    return str(path)


def test_two_page_sizes_render_to_the_same_width(tmp_path):
    a4 = _PageCache(_pdf(tmp_path / "a4.pdf", 595.28), zoom=2.5, width_px=1500)
    indesign = _PageCache(_pdf(tmp_path / "id.pdf", 668.0, 915.0), zoom=2.5, width_px=1500)
    assert a4.get(0).width == indesign.get(0).width == 1500


def test_the_zoom_follows_from_the_page_size(tmp_path):
    pc = _PageCache(_pdf(tmp_path / "a4.pdf", 595.28), zoom=2.5, width_px=1500)
    assert pc.zoom == pytest.approx(1500 / 595.28)


def test_without_a_width_the_fixed_zoom_is_kept(tmp_path):
    """0 (or no width) = the old behaviour: 2.5x the page, so the widths differ with the page size."""
    a4 = _PageCache(_pdf(tmp_path / "a4.pdf", 595.28), zoom=2.5)
    indesign = _PageCache(_pdf(tmp_path / "id.pdf", 668.0, 915.0), zoom=2.5)
    assert a4.zoom == indesign.zoom == 2.5
    assert a4.get(0).width != indesign.get(0).width


def test_a_taller_page_keeps_its_proportions(tmp_path):
    """Only the width is pinned: the picture is not squashed to a fixed box."""
    pc = _PageCache(_pdf(tmp_path / "tall.pdf", 595.28, 1200.0), zoom=2.5, width_px=1500)
    img = pc.get(0)
    assert img.width == 1500
    assert img.height == pytest.approx(1500 * 1200.0 / 595.28, abs=2)


def test_the_stand_in_cards_match_the_page_width(monkeypatch):
    """A "not in stage" / design-spec card stands beside a page capture in the report: same width."""
    from pdfval.report import shots
    monkeypatch.setattr(shots, "_CARD_W", 1500)
    assert shots._spec_card("body: 12pt", (220, 38, 38)).width == 1500
    assert shots._error_card("http://x.test/p.html", "HTTP 500", (220, 38, 38)).width == 1500
