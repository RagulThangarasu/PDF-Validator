"""Per-issue report screenshots (pdfval/report/shots.py). A web page's screenshot is the whole
browser viewport - left nav, content, "on this page" panel side by side, all sharing the same
y-coordinates. The marker line that shows where missing content belongs must stay inside the
content column (xbounds), not sweep the whole image width and cross through unrelated nav text."""
import pymupdf

from pdfval.report import shots


def _blank_pdf(path, width=1440, height=900):
    doc = pymupdf.open()
    doc.new_page(width=width, height=height)
    doc.save(path)


def test_content_bounds_reads_the_margins_and_rejects_too_narrow_ones():
    assert shots._content_bounds({"margins": {"odd": [430.0, 1040.0]}}) == (430.0, 1040.0)
    assert shots._content_bounds({"margins": {"odd": [430.0, 460.0]}}) is None  # < 50pt: not real content
    assert shots._content_bounds({"margins": {}}) is None
    assert shots._content_bounds({}) is None


def test_marker_line_stays_inside_the_content_column_not_the_full_page(tmp_path):
    path = tmp_path / "page.pdf"
    _blank_pdf(path)
    pc = shots._PageCache(str(path), zoom=1.0)
    color = (220, 38, 38)
    full = shots._crop_marker(pc, 0, 400.0, color, "note", (0.0, 900.0))
    bounded = shots._crop_marker(pc, 0, 400.0, color, "note", (0.0, 900.0), xbounds=(430.0, 1040.0))

    def marked(img, x0, x1, y):
        # the dashed line has 12px-on/10px-off segments every 22px: scan a wide enough band that at
        # least one dash is caught regardless of phase
        return any(img.getpixel((x, y)) == color for x in range(x0, x1))

    my = int(400.0 - 3)  # see _crop_marker: my = (y - 3 - y0) * z, z=1, y0=0
    assert marked(full, 40, 90, my), "the unbounded marker must reach near the left edge (full width)"
    assert not marked(bounded, 40, 90, my), "the bounded marker must not reach into the nav area"
    assert marked(bounded, 700, 750, my), "the bounded marker must still be drawn inside the content column"
