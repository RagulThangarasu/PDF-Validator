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
    """Any size change past [assets] size_ratio_tolerance (5 %) is reported, smaller or bigger - not
    only a shrink past 50 %; a change within tolerance stays quiet."""
    a = g.make(tmp_path / "a.pdf", g.base())
    assert [f["types"] for f in _pictures(compare(a, _resized(tmp_path, 0.4), cfg))] == [["image smaller"]]
    assert [f["types"] for f in _pictures(compare(a, _resized(tmp_path, 0.6), cfg))] == [["image smaller"]]
    assert [f["types"] for f in _pictures(compare(a, _resized(tmp_path, 1.4), cfg))] == [["image bigger"]]
    assert _pictures(compare(a, _resized(tmp_path, 0.97), cfg)) == []


def test_small_size_change_past_5_percent_is_now_reported(tmp_path, cfg):
    """A size change just over 5 % (e.g. 8 % smaller) used to be inside the old 20 % tolerance and was
    never reported at all - no pass, no fail, nothing. It must now be captured, not silently dropped."""
    a = g.make(tmp_path / "a.pdf", g.base())
    fs = _pictures(compare(a, _resized(tmp_path, 0.92), cfg))
    assert [f["types"] for f in fs] == [["image smaller"]]


def test_size_note_flags_a_web_page_whose_wider_column_disagrees_with_the_percent():
    """"Smaller"/"bigger" is decided by share of content width (a print page and a web page's content
    column are different widths) - real case: a stage picture drawn at MORE points (42pt prod -> 61pt
    stage) but a SMALLER share of stage's wider column (9% -> 8%), reported as "smaller" by percentage.
    Without a note, that headline flatly contradicts the point sizes printed right next to it."""
    from pdfval.checks.assets import _size_note
    grow = 0.081 / 0.087 - 1  # the real case: percentage shrinks (grow < 0) ...
    assert grow < 0
    assert _size_note(41.7, 61.1, grow, web=True) != ""          # ... while the point size grows: disagreement
    assert _size_note(41.7, 61.1, grow, web=False) == ""         # print vs print: never applies
    assert _size_note(61.1, 41.7, grow, web=True) == ""          # percentage AND points agree (both shrink): no note
    assert _size_note(41.7, 41.9, grow, web=True) == ""          # negligible point difference: no note


def _drawn_mount(pg, cx=105, y=160):
    """Two vector clusters SIDE BY SIDE (not stacked), each with enough bezier curves to count as a
    figure (checks/tables.py::_figure_rects) - like two monitor side-view silhouettes next to each
    other: together they form one ~70x66pt illustration."""
    for dx in (0, 40):
        for i in range(3):
            pg.draw_circle((cx + dx, y + i * 18), 15, color=(0, 0, 0), width=1.5)


def _mount_png(w=140, h=132) -> bytes:
    """A raster of the same two-clusters-side-by-side pattern as _drawn_mount, scaled to fit w x h -
    the stage bitmap must actually look like the prod artwork for the artwork search to pair them."""
    import io
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(im)
    for dx in (0, w // 2):
        for i in range(3):
            cx, cy = w // 5 + dx, h // 5 + i * h // 4
            d.ellipse([cx - w // 5, cy - w // 5, cx + w // 5, cy + w // 5], outline="black", width=3)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def test_two_side_by_side_vector_drawings_match_one_smaller_stage_picture(tmp_path, cfg):
    """Prod draws an illustration as two separate vector clusters placed side by side (e.g. two monitor
    side views, not one above the other). Stage shows the same illustration as one bitmap, much smaller.
    The size difference must still be reported - not silently dropped just because the two vector parts
    sit next to each other instead of stacked (the only arrangement [assets] used to recognise as "the
    same picture, drawn in parts")."""
    secs = g.base()
    secs[1]["image"] = None  # drawn as vectors on the page directly, below
    doc = pymupdf.open(g.make(tmp_path / "a0.pdf", secs))
    _drawn_mount(doc[1])
    a_path = str(tmp_path / "a.pdf")
    doc.save(a_path)
    doc.close()

    doc = pymupdf.open(g.make(tmp_path / "b0.pdf", g.base()))
    pg = doc[1]
    r = pg.get_image_rects(pg.get_images()[0][0])[0]
    pg.add_redact_annot(r)
    pg.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_REMOVE)
    pg.insert_image(pymupdf.Rect(r.x0, r.y0, r.x0 + 40, r.y0 + 38), stream=_mount_png())  # same shape, much smaller
    b_path = str(tmp_path / "b.pdf")
    doc.save(b_path)
    doc.close()

    fs = _pictures(compare(a_path, b_path, cfg))
    assert [f["types"] for f in fs] == [["image smaller"]], fs


def test_shrunk_to_icon_size_side_by_side_drawings_still_reported(tmp_path, cfg):
    """Same two side-by-side prod vector drawings as above, but the stage picture shrinks so far that
    it alone measures under [assets] icon_max_width (8 % of the content box) - a wide web content column
    makes this easy even for a real illustration, not just a true icon. The size difference must still
    be reported: _stacked_drawings (and the size check after it) must not be gated on the single stage
    picture's own icon size - only prod's combined shape decides whether this is a real illustration."""
    secs = g.base()
    secs[1]["image"] = None
    doc = pymupdf.open(g.make(tmp_path / "a0.pdf", secs))
    _drawn_mount(doc[1])
    a_path = str(tmp_path / "a.pdf")
    doc.save(a_path)
    doc.close()

    doc = pymupdf.open(g.make(tmp_path / "b0.pdf", g.base()))
    pg = doc[1]
    r = pg.get_image_rects(pg.get_images()[0][0])[0]
    pg.add_redact_annot(r)
    pg.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_REMOVE)
    pg.insert_image(pymupdf.Rect(r.x0, r.y0, r.x0 + 28, r.y0 + 26), stream=_mount_png(28, 26))  # icon-sized (7 %)
    b_path = str(tmp_path / "b.pdf")
    doc.save(b_path)
    doc.close()

    fs = _pictures(compare(a_path, b_path, cfg))
    assert [f["types"] for f in fs] == [["image smaller"]], fs

