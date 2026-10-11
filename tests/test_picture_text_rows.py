"""A picture and the text that belongs to it side by side in prod - the "2 x 2" step layout of an
installation chapter, drawing on the left and "1. Connect your camera …" on the right - must stay side
by side in stage. Stage stacking the step text above or below its picture is a layout difference the
reader sees, and no other check reports it: the picture-row check needs two pictures on a row, and the
row-alignment check treats text that stage stacks as a reflow.
"""
import pymupdf
import pytest

from pdfval import compare, load_config

STEPS = ["1. Connect your camera to the USB-C port on the top of the display.",
         "2. Secure the camera with the provided screws."]


def _picture(page, rect):
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 240, 160))
    pix.set_rect(pymupdf.IRect(0, 0, 240, 160), (225, 230, 245))
    pix.set_rect(pymupdf.IRect(20, 30, 220, 90), (30, 50, 150))
    page.insert_image(rect, pixmap=pix)


def _pdf(path, *, side_by_side):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 70), "Install camera", fontsize=18, fontname="hebo")
    y = 110
    for step in STEPS:
        if side_by_side:                      # picture left, its step text beside it on the right
            _picture(page, pymupdf.Rect(72, y, 272, y + 134))
            page.insert_textbox(pymupdf.Rect(300, y + 40, 540, y + 120), step, fontsize=11, fontname="helv")
        else:                                 # stacked: the step text, the picture under it
            page.insert_textbox(pymupdf.Rect(72, y, 520, y + 40), step, fontsize=11, fontname="helv")
            _picture(page, pymupdf.Rect(72, y + 44, 272, y + 178))
        y += 220
    doc.save(path)
    return str(path)


@pytest.fixture
def cfg():
    return load_config()


def _findings(r):
    return [f for s in r["sections"] for f in s["findings"] if "text beside picture" in (f["types"] or [])]


def test_text_beside_the_picture_in_prod_stacked_in_stage_is_reported(tmp_path, cfg):
    r = compare(_pdf(tmp_path / "a.pdf", side_by_side=True), _pdf(tmp_path / "b.pdf", side_by_side=False), cfg)
    fs = _findings(r)
    assert fs, [f["message"] for s in r["sections"] for f in s["findings"]]
    assert all(f["genuine"] and f["severity"] == "error" for f in fs)
    assert "no longer side by side" in fs[0]["message"]
    assert "Connect your camera" in fs[0]["message"] or "Secure the camera" in fs[0]["message"]


def test_the_same_layout_on_both_sides_is_not_reported(tmp_path, cfg):
    assert not _findings(compare(_pdf(tmp_path / "a.pdf", side_by_side=True),
                                 _pdf(tmp_path / "b.pdf", side_by_side=True), cfg))
    assert not _findings(compare(_pdf(tmp_path / "c.pdf", side_by_side=False),
                                 _pdf(tmp_path / "d.pdf", side_by_side=False), cfg))


def test_stage_setting_a_stacked_pair_side_by_side_is_reported_too(tmp_path, cfg):
    fs = _findings(compare(_pdf(tmp_path / "a.pdf", side_by_side=False),
                           _pdf(tmp_path / "b.pdf", side_by_side=True), cfg))
    assert fs and "side by side" in fs[0]["message"]


def test_the_check_can_be_turned_off(tmp_path, cfg):
    cfg["layout"]["check_picture_text_rows"] = False
    assert not _findings(compare(_pdf(tmp_path / "a.pdf", side_by_side=True),
                                 _pdf(tmp_path / "b.pdf", side_by_side=False), cfg))
