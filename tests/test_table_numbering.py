"""A table's numbering column (“A.” | “Lid”) is two cells, not a list item: its column widths are
table layout, never a bullet gap / indent, and its numbers are not list markers (missing / added / numbering)."""
import pymupdf
import pytest

from pdfval import compare, load_config
from pdfval.checks import layout

ROWS = [("No.", "Item"), ("A.", "InstaShow Receiver unit"), ("B.", "Lid for the receiver"),
        ("C.", "InstaShow Button unit"), ("D.", "Two antennas for the receiver")]


@pytest.fixture
def cfg():
    c = load_config()
    c["typography"]["enabled"] = False
    c["layout"]["check_bullet_position"] = True  # these tests cover the detector the default turns off
    return c


def _doc(path, split: float) -> str:
    doc = pymupdf.open()
    pg = doc.new_page(width=595, height=842)
    pg.insert_text((72, 80), "Package contents", fontsize=16, fontname="hebo")
    xs, y, h = [72, split, 400], 110, 22
    for r, cells in enumerate(ROWS):
        for c, text in enumerate(cells):
            box = pymupdf.Rect(xs[c], y + r * h, xs[c + 1], y + (r + 1) * h)
            pg.draw_rect(box, color=(0, 0, 0), width=0.6)
            pg.insert_text((box.x0 + 4, box.y0 + 15), text, fontsize=10, fontname="helv")
    doc.set_toc([[1, "Package contents", 1]])
    doc.save(path)
    return str(path)


def _bullet_layout(r):
    return [f for s in r["sections"] for f in s["findings"]
            if f["detail"].get("kind") in ("bullet gap", "bullet indent", "hanging indent", "bullet marker",
                                           "numbering style", "numbering format", "numbering sequence")]


def test_numbering_column_is_not_a_bullet_gap(tmp_path, cfg):
    a, b = _doc(tmp_path / "a.pdf", 100), _doc(tmp_path / "b.pdf", 130)  # wider “No.” column in stage
    assert _bullet_layout(compare(a, b, cfg)) == []


def test_without_the_cell_guard_it_was_reported(tmp_path, cfg, monkeypatch):
    monkeypatch.setattr(layout, "_cells_apart", lambda *a, **k: False)
    monkeypatch.setattr(layout, "_in_table", lambda *a, **k: False)  # the list-in-a-table-cell guard too
    a, b = _doc(tmp_path / "a.pdf", 100), _doc(tmp_path / "b.pdf", 130)
    assert _bullet_layout(compare(a, b, cfg))


def test_numbers_column_read_against_the_wrong_row_is_not_a_missing_marker(tmp_path, cfg):
    """Prod draws an icon between the number and the text (“12.” | ⚙ “Dashboard”): the list check may read the
    previous row's number as the marker - a numbering column is still never a missing list number."""
    a = _doc(tmp_path / "a.pdf", 100)
    doc = pymupdf.open(a)
    for r in range(1, len(ROWS)):  # a small icon at the start of each prod item cell
        doc[0].draw_rect(pymupdf.Rect(104, 110 + r * 22 + 6, 112, 110 + r * 22 + 14), color=None, fill=(0, 0, 0))
    doc.save(tmp_path / "a2.pdf")
    r = compare(str(tmp_path / "a2.pdf"), _doc(tmp_path / "b.pdf", 130), cfg)
    assert _bullet_layout(r) == []
