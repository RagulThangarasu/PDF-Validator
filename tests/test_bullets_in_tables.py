"""A list inside a table cell is not checked as a list.

A cell is laid out by the table and its markers are the table's own styling, so a bullet that is "•"
in prod and "-" in stage inside a cell, or missing there, is not a list issue. The same difference in
the body text of the page still is. [layout] check_bullets_in_tables = true brings the marker checks
back for cells (where they sit is never compared either way).
"""
import pymupdf
import pytest

from pdfval import engine, extract


@pytest.fixture
def cfg():
    c = engine.load_config()
    c["typography"]["enabled"] = False  # prod vs stage only, no design spec
    return c


def _doc(path, marker: str, where: str) -> str:
    """A page with a two-column ruled table and a paragraph list under it. `where` says which of the
    two carries `marker` before its items; the other always uses "•"."""
    doc = pymupdf.open()
    pg = doc.new_page(width=595.28, height=841.89)
    pg.insert_text((40, 60), "Package contents", fontsize=16, fontname="hebo")
    x0, xm, x1, h, y = 40, 250, 555, 26, 90
    rows = [("Item", "Details"), ("Cables", "HDMI cable"), ("Adapters", "USB-C adapter")]
    cell_mark = marker if where == "table" else "•"
    for r, (label, value) in enumerate(rows):
        pg.draw_rect(pymupdf.Rect(x0, y, x1, y + h), color=(0.4, 0.4, 0.4), width=0.6)
        pg.draw_line((xm, y), (xm, y + h), color=(0.4, 0.4, 0.4), width=0.6)
        pg.insert_text((x0 + 6, y + 17), label, fontsize=11, fontname="helv")
        text = value if r == 0 else f"{cell_mark} {value}"
        pg.insert_text((xm + 6, y + 17), text, fontsize=11, fontname="helv")
        y += h
    body_mark = marker if where == "body" else "•"
    pg.insert_text((40, y + 40), "In the box:", fontsize=11, fontname="helv")
    for n, item in enumerate(("Power supply", "Quick start guide")):
        pg.insert_text((52, y + 62 + n * 18), f"{body_mark} {item}", fontsize=11, fontname="helv")
    doc.save(path)
    return str(path)


def _bullet_findings(tmp_path, cfg, where, stage_marker="-"):
    a = extract.load(_doc(tmp_path / "a.pdf", "•", where), "baseline", cfg)
    b = extract.load(_doc(tmp_path / "b.pdf", stage_marker, where), "candidate", cfg)
    result = engine.compare_docs(a, b, cfg) if hasattr(engine, "compare_docs") else engine.compare(
        str(tmp_path / "a.pdf"), str(tmp_path / "b.pdf"), cfg)
    return [f for s in result["sections"] for f in s["findings"]
            if "bullet" in (f.get("issue") or "").lower() or "bullet" in " ".join(f.get("types") or [])
            or "marker" in " ".join(f.get("types") or [])]


def test_a_bullet_that_differs_inside_a_table_cell_is_not_reported(tmp_path, cfg):
    cfg["layout"]["ignore_bullet_glyph"] = False   # the glyph difference would otherwise be ignored anyway
    assert _bullet_findings(tmp_path, cfg, "table") == []


def test_the_same_difference_in_the_body_text_is_still_reported(tmp_path, cfg):
    cfg["layout"]["ignore_bullet_glyph"] = False
    assert _bullet_findings(tmp_path, cfg, "body"), "a bullet difference outside a table must still be found"


def test_the_switch_brings_the_cell_markers_back(tmp_path, cfg):
    cfg["layout"]["ignore_bullet_glyph"] = False
    cfg["layout"]["check_bullets_in_tables"] = True
    assert _bullet_findings(tmp_path, cfg, "table"), "check_bullets_in_tables = true must check them again"
