"""A table is not a picture. Its rules, its header bar and its filled rows are drawn shapes like any
other, so the artwork test used to count them and pass a specifications table off as a drawing - and a
prod table that stage rebuilds as a real table was then reported as a critical "Image missing in
candidate" (every timing-table manual raised one per table). A drawing inside a table cell is still a
picture: the test is that the region IS the table, not that it touches one.
"""
import pymupdf

from pdfval import compare, extract, load_config
from pdfval.checks import assets

ROWS = [("Resolution", "Frequency", "HDMI"), ("640x480", "60", "V"), ("1280x720", "60", "V"),
        ("1920x1080", "120", "V"), ("2560x1440", "144", "V")]


def _table_page(doc, *, with_drawing=False):
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 70), "Preset display mode", fontsize=18, fontname="hebo")
    x0, y0, w, h = 72, 110, 150, 24
    for r, row in enumerate(ROWS):
        for c, cell in enumerate(row):
            box = pymupdf.Rect(x0 + c * w, y0 + r * h, x0 + (c + 1) * w, y0 + (r + 1) * h)
            # a banded table, as the timing manuals set them: every cell filled, which is what used to
            # count as "filled shapes" and pass the table off as a drawing
            fill = (0.2, 0.3, 0.6) if r == 0 else ((0.93, 0.93, 0.95) if r % 2 else (1, 1, 1))
            page.draw_rect(box, color=(0.4, 0.4, 0.4), fill=fill, width=0.7)
            page.insert_text((box.x0 + 4, box.y1 - 8), cell, fontsize=10,
                             fontname="hebo" if r == 0 else "helv",
                             color=(1, 1, 1) if r == 0 else (0, 0, 0))
    if with_drawing:  # an illustration drawn in the last cell of the last row
        cell = pymupdf.Rect(x0 + 2 * w + 10, y0 + 4 * h + 2, x0 + 3 * w - 10, y0 + 5 * h + 40)
        page.draw_circle((cell.x0 + 20, cell.y0 + 20), 14, color=(0, 0, 0), width=1.2)
        page.draw_bezier((cell.x0 + 4, cell.y1 - 6), (cell.x0 + 30, cell.y1 - 30),
                         (cell.x0 + 60, cell.y1 - 4), (cell.x0 + 90, cell.y1 - 24), color=(0, 0, 0))
        page.draw_line((cell.x0 + 6, cell.y0 + 44), (cell.x0 + 86, cell.y0 + 70), color=(0, 0, 0))
        page.draw_sector((cell.x0 + 110, cell.y0 + 30), (cell.x0 + 130, cell.y0 + 10), 140, fill=(0.1, 0.1, 0.1))
    return page


def _pdf(path, *, with_drawing=False):
    doc = pymupdf.open()
    _table_page(doc, with_drawing=with_drawing)
    doc.save(path)
    return str(path)


def test_a_table_is_not_artwork(tmp_path):
    cfg = load_config()
    path = _pdf(tmp_path / "a.pdf")
    doc = extract.load(path, "baseline", cfg)
    with pymupdf.open(path) as pdf:
        box = pdf[0].find_tables().tables[0].bbox
    assert assets._is_table(doc, 0, box)
    assert not assets._has_artwork(doc, 0, box)


def test_a_drawing_inside_a_cell_is_still_artwork(tmp_path):
    cfg = load_config()
    path = _pdf(tmp_path / "a.pdf", with_drawing=True)
    doc = extract.load(path, "baseline", cfg)
    with pymupdf.open(path) as pdf:
        page = pdf[0]
        box = page.find_tables().tables[0].bbox
        cluster = max(page.cluster_drawings(), key=lambda r: r.get_area() if r.y0 > box[1] + 80 else 0)
    assert not assets._is_table(doc, 0, cluster), "a cell's drawing is not the whole table"


def test_without_the_table_rule_the_same_table_would_pass_for_artwork(tmp_path, monkeypatch):
    """What the rule is for: a banded table draws enough filled cells to clear the artwork test on its
    own, which is how a prod table came to be reported as a picture missing from stage."""
    cfg = load_config()
    path = _pdf(tmp_path / "a.pdf")
    doc = extract.load(path, "baseline", cfg)
    with pymupdf.open(path) as pdf:
        box = pdf[0].find_tables().tables[0].bbox
    monkeypatch.setattr(assets, "_is_table", lambda *a, **k: False)
    assert assets._has_artwork(doc, 0, box)


def test_a_prod_table_is_not_reported_as_a_picture(tmp_path):
    """Both sides hold the same table: nothing about a picture may be reported."""
    cfg = load_config()
    r = compare(_pdf(tmp_path / "a.pdf"), _pdf(tmp_path / "b.pdf"), cfg)
    pics = [f for s in r["sections"] for f in s["findings"]
            if any("image" in t or "picture" in t for t in (f["types"] or []))]
    assert not pics, [f["message"] for f in pics]
