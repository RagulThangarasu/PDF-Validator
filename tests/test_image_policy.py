"""The picture issues the reports keep ([assets] report_types): artwork missing, another picture, a stage picture
under half prod's size, ... - a picture only a little smaller is not reported."""
import pymupdf
import pytest

import test_genuine as g
from pdfval import compare, load_config


@pytest.fixture
def cfg():
    c = load_config()
    c["typography"]["enabled"] = False
    return c


def _pictures(r):
    return [f for s in r["sections"] for f in s["findings"] if f["check"] == "assets"]


def _resized(tmp_path, scale) -> str:
    b = tmp_path / "b.pdf"
    g.make(b, g.base())
    doc = pymupdf.open(b)
    pg = doc[1]
    r = pg.get_image_rects(pg.get_images()[0][0])[0]
    pg.add_redact_annot(r)
    pg.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_REMOVE)
    pg.insert_image(pymupdf.Rect(r.x0, r.y0, r.x0 + r.width * scale, r.y0 + r.height * scale), stream=g._picture(1))
    out = tmp_path / f"c{scale}.pdf"
    doc.save(out)
    return str(out)


def test_another_picture_is_reported(tmp_path, cfg):
    from test_image_labels_only import _picture as drawing  # two unrelated line drawings
    a, b = g.base(), g.base()
    a[1]["image"], b[1]["image"] = drawing(False), drawing(False, "other")
    fs = _pictures(compare(g.make(tmp_path / "a.pdf", a), g.make(tmp_path / "b.pdf", b), cfg))
    assert [f["types"] for f in fs] == [["image changed"]]


def test_missing_artwork_is_reported(tmp_path, cfg):
    secs = g.base()
    secs[1]["image"] = None
    fs = _pictures(compare(g.make(tmp_path / "a.pdf", g.base()), g.make(tmp_path / "b.pdf", secs), cfg))
    assert [f["types"] for f in fs] == [["missing image"]]


def test_size_difference_is_reported_either_direction(tmp_path, cfg):
    """Any size change past [assets] size_ratio_tolerance (20 %) is reported, smaller or bigger - not
    only a shrink past 50 %; a change within tolerance stays quiet."""
    a = g.make(tmp_path / "a.pdf", g.base())
    assert [f["types"] for f in _pictures(compare(a, _resized(tmp_path, 0.4), cfg))] == [["image smaller"]]
    assert [f["types"] for f in _pictures(compare(a, _resized(tmp_path, 0.6), cfg))] == [["image smaller"]]
    assert [f["types"] for f in _pictures(compare(a, _resized(tmp_path, 1.4), cfg))] == [["image bigger"]]
    assert _pictures(compare(a, _resized(tmp_path, 0.95), cfg)) == []

