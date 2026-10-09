"""Print production marks (crop marks, registration crosses, a colour calibration bar) sit in the
bleed/marks margin OUTSIDE the page's trim box - never real body content. The vector-only "picture
missing in stage" detector (checks/assets.py::_missing_vector_only) must not mistake them for a real
picture just because they cluster into vector shapes that pass the generic artwork test."""
import pymupdf
import pytest

from pdfval import compare, load_config


@pytest.fixture
def cfg():
    return load_config()


def _mountain(page, r):
    """A small line drawing with curves - a real illustration, not a mark."""
    x0, y0, x1, y1 = r
    sh = page.new_shape()
    for k in range(12):
        sh.draw_bezier((x0, y1 - 5 * k), (x0 + 30, y0 + 3 * k), (x1 - 30, y0 + 4 * k), (x1, y1 - 6 * k))
    sh.draw_rect(pymupdf.Rect(x0 + 20, y0 - 10, x1 - 20, y0 + 10))
    sh.finish(color=(0, 0, 0), width=1)
    sh.commit()


def _color_bar(page, y0):
    """A row of small filled rectangles - the kind of colour calibration bar InDesign prints outside the
    trim box when marks are enabled. Has enough filled shapes to pass the generic artwork test on its
    own, like a real illustration would."""
    sh = page.new_shape()
    for k in range(6):
        sh.draw_rect(pymupdf.Rect(36 + 24 * k, y0, 36 + 24 * k + 20, y0 + 10))
    sh.finish(color=(0, 0, 0), fill=(0.2 * (1), 0.3, 0.4), width=0.5)
    sh.commit()


def _page(path, illustration: bool, marks: bool):
    doc = pymupdf.open()
    p = doc.new_page()  # default letter size, 595 x 842
    p.set_trimbox(pymupdf.Rect(36, 36, 559, 806))  # a real print bleed/marks margin around the trim box
    p.insert_text((72, 80), "Mounting the unit", fontsize=18, fontname="hebo")
    p.insert_text((72, 110), "Attach the bracket to the wall before placing the unit on it.", fontsize=11)
    p.insert_text((72, 305), "See the diagram below for the mounting angle.", fontsize=11)
    if illustration:
        _mountain(p, (300, 300, 450, 400))  # well inside the trim box: a real picture
    if marks:
        _color_bar(p, 10)  # above the trim box's own top edge (y=36): print marks, not content
    doc.save(path)
    return str(path)


def test_print_marks_outside_the_trim_box_are_not_a_missing_picture(tmp_path, cfg):
    """Prod has print marks above the trim box and NO real illustration; stage (a web capture) obviously
    has neither. This must not be reported as a missing picture."""
    r = compare(_page(tmp_path / "a.pdf", illustration=False, marks=True), _page(tmp_path / "b.pdf", illustration=False, marks=False), cfg)
    fs = [f for s in r["sections"] for f in s["findings"] if f["check"] == "assets" and f["detail"].get("kind") == "missing"]
    assert not fs, [f["message"] for f in fs]


def test_real_illustration_inside_the_trim_box_is_still_reported_missing(tmp_path, cfg):
    """A genuine vector illustration inside the trim box, dropped in stage, must still be caught -
    confirms the trim-box filter does not also swallow real missing pictures."""
    r = compare(_page(tmp_path / "a.pdf", illustration=True, marks=True), _page(tmp_path / "b.pdf", illustration=False, marks=False), cfg)
    fs = [f for s in r["sections"] for f in s["findings"] if f["check"] == "assets" and f["detail"].get("kind") == "missing"]
    assert fs and fs[0]["detail"].get("vector_only"), [f["message"] for f in fs]
