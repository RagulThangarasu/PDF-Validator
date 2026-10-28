"""A table header on the background bar must leave space between the cell border and its text.

The design spec pads every cell; a stage header whose text starts at the column's border reads as
glued to it. Judged on stage alone, against the spec, and only where stage really draws the header
bar - see pdfval/checks/tables.py::_header_padding and [tables] check_header_padding.
"""
import pymupdf
import pytest

from pdfval import engine

ROWS = [("Cable length (mm)", "200"), ("Weight (g)", "188"), ("Ports", "2"), ("Warranty (years)", "3")]


@pytest.fixture
def cfg():
    c = engine.load_config()
    c["typography"]["enabled"] = False  # prod vs stage only, no design-spec typography noise
    return c


def _doc(path, header_pad: float, bar: bool = True) -> str:
    """A two-column table whose header row sits on a dark bar (unless bar=False), its text starting
    `header_pad` pt from the cell border. The data rows are always padded 6pt."""
    doc = pymupdf.open()
    pg = doc.new_page(width=595.28, height=841.89)
    pg.insert_text((40, 60), "Specifications", fontsize=16, fontname="hebo")
    x0, xm, x1, h, y = 40, 250, 555, 24, 90
    if bar:
        pg.draw_rect(pymupdf.Rect(x0, y, x1, y + h), color=None, fill=(0.2, 0.2, 0.2))
    colour = (1, 1, 1) if bar else (0, 0, 0)
    for x, t in ((x0 + header_pad, "Item"), (xm + header_pad, "Value")):
        pg.insert_text((x, y + 16), t, fontsize=11, fontname="helv", color=colour)
    y += h
    for label, value in ROWS:
        pg.draw_rect(pymupdf.Rect(x0, y, x1, y + h), color=(0.8, 0.8, 0.8), width=0.6)
        pg.draw_line((xm, y), (xm, y + h), color=(0.8, 0.8, 0.8), width=0.6)
        pg.insert_text((x0 + 6, y + 16), label, fontsize=11, fontname="helv")
        pg.insert_text((xm + 6, y + 16), value, fontsize=11, fontname="helv")
        y += h
    doc.save(path)
    return str(path)


def _padding_findings(tmp_path, cfg, stage_pad: float, bar: bool = True):
    prod = _doc(tmp_path / "prod.pdf", 6)
    stage = _doc(tmp_path / "stage.pdf", stage_pad, bar=bar)
    result = engine.compare(prod, stage, cfg)
    return [f for s in result["sections"] for f in s["findings"]
            if "table header padding" in (f.get("types") or [])]


def test_a_header_glued_to_the_cell_border_is_reported(tmp_path, cfg):
    fs = _padding_findings(tmp_path, cfg, 0.0)
    assert fs, "a header with no padding must be reported"
    assert "against the cell border" in fs[0]["message"]
    assert "touching the border" in fs[0]["message"]


def test_a_padded_header_is_not_reported(tmp_path, cfg):
    assert _padding_findings(tmp_path, cfg, 6.0) == []


def test_padding_under_the_minimum_is_reported_with_its_measurement(tmp_path, cfg):
    fs = _padding_findings(tmp_path, cfg, 2.0)
    assert fs and "pt from the border" in fs[0]["message"]


def test_a_table_with_no_header_bar_is_not_judged(tmp_path, cfg):
    """The background bar is what marks the header row: without one there is no header to judge."""
    assert _padding_findings(tmp_path, cfg, 0.0, bar=False) == []


def test_the_check_can_be_turned_off(tmp_path, cfg):
    cfg["tables"]["check_header_padding"] = False
    assert _padding_findings(tmp_path, cfg, 0.0) == []


def _nested_header_doc(path, sub_pad: float) -> str:
    """A merged bar header ("Screen size" over two sub-columns) with a plain white sub-header row
    right below it ("Inch" / "mm") - the sub-header carries the same padding rule, even though it has
    no background bar of its own (real case: a projection-distance table)."""
    doc = pymupdf.open()
    pg = doc.new_page(width=595.28, height=841.89)
    pg.insert_text((40, 60), "Projection dimensions", fontsize=16, fontname="hebo")
    x0, x1, x2, x3, x4 = 40, 180, 280, 400, 555
    y = 90
    pg.draw_rect(pymupdf.Rect(x0, y, x4, y + 18), color=None, fill=(0.2, 0.2, 0.2))
    pg.insert_text((x0 + 4, y + 13), "Screen size", fontsize=10, fontname="helv", color=(1, 1, 1))
    pg.insert_text((x2 + 4, y + 13), "W (mm)", fontsize=10, fontname="helv", color=(1, 1, 1))
    pg.insert_text((x3 + 4, y + 13), "H (mm)", fontsize=10, fontname="helv", color=(1, 1, 1))
    y += 18
    pg.draw_rect(pymupdf.Rect(x0, y, x4, y + 16), color=(0.7, 0.7, 0.7), width=0.6)
    pg.draw_line((x1, y), (x1, y + 16), color=(0.7, 0.7, 0.7), width=0.6)
    pg.draw_line((x2, y), (x2, y + 56), color=(0.7, 0.7, 0.7), width=0.6)
    pg.draw_line((x3, y), (x3, y + 56), color=(0.7, 0.7, 0.7), width=0.6)
    pg.insert_text((x0 + sub_pad, y + 12), "Inch", fontsize=9, fontname="helv")
    pg.insert_text((x1 + sub_pad, y + 12), "mm", fontsize=9, fontname="helv")
    y += 16
    for vals in (("50", "1270", "1107", "623"), ("60", "1524", "1328", "747")):
        pg.draw_rect(pymupdf.Rect(x0, y, x4, y + 20), color=(0.8, 0.8, 0.8), width=0.6)
        pg.draw_line((x1, y), (x1, y + 20), color=(0.8, 0.8, 0.8), width=0.6)
        for x, v in zip((x0, x1, x2, x3), vals):
            pg.insert_text((x + 6, y + 14), v, fontsize=9, fontname="helv")
        y += 20
    doc.save(path)
    return str(path)


def test_a_nested_subheader_glued_to_the_border_is_reported(tmp_path, cfg):
    """"Inch" / "mm" right under the "Screen size" bar: no bar of its own, but still a header cell."""
    prod = _nested_header_doc(tmp_path / "prod.pdf", 6)
    stage = _nested_header_doc(tmp_path / "stage.pdf", 0)
    result = engine.compare(prod, stage, cfg)
    fs = [f for s in result["sections"] for f in s["findings"] if "table header padding" in (f.get("types") or [])]
    assert fs, "the sub-header row under the bar must be judged too"
    assert "mm" in fs[0]["message"] and "touching the border" in fs[0]["message"]


def test_a_padded_nested_subheader_is_not_reported(tmp_path, cfg):
    prod = _nested_header_doc(tmp_path / "prod.pdf", 6)
    stage = _nested_header_doc(tmp_path / "stage.pdf", 6)
    result = engine.compare(prod, stage, cfg)
    assert not [f for s in result["sections"] for f in s["findings"] if "table header padding" in (f.get("types") or [])]
