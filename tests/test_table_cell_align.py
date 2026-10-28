"""Every row of a table column starts at the same left edge - the header rows included.

A column's own cell padding is read from the table (whatever it is); a row that does not sit at it -
a header row set against the cell border while the data rows are indented, or a centred column whose
rows sit at as many edges as they have widths - is reported, once per table, BY POSITION: which
column, which rows, how far in. The data printed inside the cells is never quoted.

See pdfval/checks/tables.py::_cell_align and [tables] check_cell_align.
"""
import pymupdf
import pytest

from pdfval import engine

ROWS = [("Cable length (mm)", "200"), ("Weight (g)", "188"), ("Ports", "2"), ("Warranty (years)", "3")]


@pytest.fixture
def cfg():
    c = engine.load_config()
    c["typography"]["enabled"] = False  # prod vs stage only
    return c


def _doc(path, value_align: str = "left", header_pad: float = 6) -> str:
    """A two-column table: the value column left-aligned, centred or right-aligned, and the header
    row at its own padding (header_pad) - the rest of the rows are always padded 6pt."""
    doc = pymupdf.open()
    pg = doc.new_page(width=595.28, height=841.89)
    pg.insert_text((40, 60), "Specifications", fontsize=16, fontname="hebo")
    x0, xm, x1, h, y = 40, 250, 555, 24, 90
    pg.draw_rect(pymupdf.Rect(x0, y, x1, y + h), color=None, fill=(0.2, 0.2, 0.2))
    for x, t in ((x0 + header_pad, "Item"), (xm + header_pad, "Value")):
        pg.insert_text((x, y + 16), t, fontsize=11, fontname="helv", color=(1, 1, 1))
    y += h
    for label, value in ROWS:
        pg.draw_rect(pymupdf.Rect(x0, y, x1, y + h), color=(0.8, 0.8, 0.8), width=0.6)
        pg.draw_line((xm, y), (xm, y + h), color=(0.8, 0.8, 0.8), width=0.6)
        pg.insert_text((x0 + 6, y + 16), label, fontsize=11, fontname="helv")
        w = pymupdf.get_text_length(value, fontname="helv", fontsize=11)
        x = {"left": xm + 6, "center": (xm + x1) / 2 - w / 2, "right": x1 - 6 - w}[value_align]
        pg.insert_text((x, y + 16), value, fontsize=11, fontname="helv")
        y += h
    doc.save(path)
    return str(path)


def _found(r):
    return [f for s in r["sections"] for f in s["findings"] if "table cell alignment" in (f.get("types") or [])]


def test_one_left_edge_per_column_is_not_reported(tmp_path, cfg):
    """Header and data at the same 6pt padding: a normal table, whatever the padding happens to be."""
    r = engine.compare(_doc(tmp_path / "prod.pdf"), _doc(tmp_path / "stage.pdf"), cfg)
    assert not _found(r)


def test_a_header_row_at_another_edge_than_the_data_rows_is_reported(tmp_path, cfg):
    """The case from the real timing tables: the header rows sit against the cell border (1pt) while
    every data row is indented (9pt)."""
    r = engine.compare(_doc(tmp_path / "prod.pdf"), _doc(tmp_path / "stage.pdf", header_pad=1), cfg)
    got = _found(r)
    assert len(got) == 1, [f["message"] for f in got]
    m = got[0]["message"]
    assert "row 1 at 1pt" in m and "row(s) at 6pt" in m
    assert "Item" not in m and "Cable length" not in m  # the cells' own data is never printed
    assert "must start at the same left edge" in m
    assert got[0]["detail"]["columns"] == 2 and got[0]["detail"]["rows_off"] == 2
    assert got[0]["genuine"]  # a design-spec deviation the reader sees: in the genuine report


def test_a_centred_column_is_reported(tmp_path, cfg):
    """Prod centres the value column too: the rule is read from stage alone, so it is still reported."""
    r = engine.compare(_doc(tmp_path / "prod.pdf", "center"), _doc(tmp_path / "stage.pdf", "center"), cfg)
    got = _found(r)
    m = got[0]["message"]
    assert "column 2" in m and "at 6pt" in m
    assert "200" not in m and "Value" not in m  # by position only


def test_a_right_aligned_column_is_reported(tmp_path, cfg):
    r = engine.compare(_doc(tmp_path / "prod.pdf"), _doc(tmp_path / "stage.pdf", "right"), cfg)
    assert _found(r)


def test_one_finding_per_table_not_one_per_cell(tmp_path, cfg):
    """The columns are summarised in a single finding: a 25-row timing table used to report 82 of them."""
    r = engine.compare(_doc(tmp_path / "prod.pdf"), _doc(tmp_path / "stage.pdf", "center", header_pad=1), cfg)
    assert len(_found(r)) == 1


def test_the_rule_can_be_turned_off(tmp_path, cfg):
    cfg["tables"]["check_cell_align"] = False
    r = engine.compare(_doc(tmp_path / "prod.pdf"), _doc(tmp_path / "stage.pdf", header_pad=1), cfg)
    assert not _found(r)


def test_the_type_is_registered_and_not_ignored():
    from pdfval import genuine
    c = engine.load_config()
    assert "table cell alignment" in set(c["genuine"]["types"])
    assert "table cell alignment" not in set(c["ignore"]["types"])
    assert "table cell alignment" in genuine._WHY
