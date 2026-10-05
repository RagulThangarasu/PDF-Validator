"""A word broken inside a narrow table cell with no hyphen (“Check if complete” | “d”) is the cell's
wrapping, not a missing space; a missing space in running text is still reported."""
import pymupdf
import pytest

from pdfval import compare, load_config


@pytest.fixture
def cfg():
    c = load_config()
    c["typography"]["enabled"] = False
    return c


def _doc(path, header_lines, note) -> str:
    doc = pymupdf.open()
    pg = doc.new_page(width=595, height=842)
    pg.insert_text((72, 80), "Ghost touch", fontsize=16, fontname="hebo")
    xs, y, h = [72, 330, 372, 460], 110, 40   # the middle column is 42 pt wide: “complete” fills it
    rows = [("Check item", header_lines, "Does it exist?"), ("Clean the frame carefully.", [""], "Yes"),
            ("Draw lines in Paint.", [""], "No")]
    for r, cells in enumerate(rows):
        for c, text in enumerate(cells):
            box = pymupdf.Rect(xs[c], y + r * h, xs[c + 1], y + (r + 1) * h)
            pg.draw_rect(box, color=(0, 0, 0), width=0.6)
            for k, line in enumerate(text if isinstance(text, list) else [text]):
                if line:
                    pg.insert_text((box.x0 + 2, box.y0 + 12 + k * 11), line, fontsize=9, fontname="helv")
    pg.insert_text((72, 260), note, fontsize=10, fontname="helv")
    doc.set_toc([[1, "Ghost touch", 1]])
    doc.save(path)
    return str(path)


def _gaps(r):
    return [f["message"] for s in r["sections"] for f in s["findings"] if "spacing" in f.get("types", [])]


def test_word_broken_in_a_narrow_cell_is_not_a_word_gap(tmp_path, cfg):
    note = "Keep your hand away from the touchscreen."
    a = _doc(tmp_path / "a.pdf", ["Check if", "complete", "d"], note)
    b = _doc(tmp_path / "b.pdf", ["Check if", "completed"], note)
    assert _gaps(compare(a, b, cfg)) == []


def test_missing_space_in_running_text_is_still_reported(tmp_path, cfg):
    a = _doc(tmp_path / "a.pdf", ["Check if", "completed"], "Keep your hand away from the touchscreen.")
    b = _doc(tmp_path / "b.pdf", ["Check if", "completed"], "Keep your handaway from the touchscreen.")
    assert _gaps(compare(a, b, cfg))
