"""AEM Sites: “On this page” must list the headings down to H4, and each page is checked for breaking issues
and picture problems (not loaded, stretched, outside its area, sideways scroll)."""
import pymupdf

from pdfval import load_config
from pdfval.engine import compare_url
from test_site_nav import make_site, serve  # noqa: F401  (the fixture)


def _png(path, w, h):
    pm = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, w, h), False)
    pm.clear_with(180)
    pm.save(str(path))


def _site(tmp_path, base, extra_html):
    pdf = make_site(tmp_path)
    page = tmp_path / "guide" / "overview.html"
    _png(tmp_path / "guide" / "pic.png", 200, 100)
    page.write_text(page.read_text().replace('<div class="pager">', extra_html + '<div class="pager">'))
    cfg = load_config()
    cfg["site"]["check_otp"] = True  # otp is off by default (old check) - this file tests it directly
    return compare_url(str(pdf), base + "overview.html", str(tmp_path / "run"), cfg)["site"]


def rows(site, group, status="fail"):
    return [r for r in site["rows"] if r["group"] == group and r["status"] == status]


def test_h4_missing_from_on_this_page_is_reported(tmp_path, serve):
    site = _site(tmp_path, serve, '<h3 id="x1">Panel</h3><p>About the panel.</p><h4 id="x2">Touch layer</h4><p>About the touch layer.</p>')
    missing = {r["item"]: r["note"] for r in rows(site, "otp")}
    assert missing.get("Touch layer") == "h4 not listed" and missing.get("Panel") == "h3 not listed"


def test_picture_and_layout_breaks_are_reported(tmp_path, serve):
    site = _site(tmp_path, serve,
                 '<p>Good picture:</p><img src="pic.png" alt="good" width="200" height="100">'
                 '<p>Stretched:</p><img src="pic.png" alt="stretched" style="width:200px;height:200px">'
                 '<p>Too wide:</p><img src="pic.png" alt="wide" style="width:900px;height:450px;max-width:none">'
                 '<p>Missing:</p><img src="nope.png" alt="gone" width="100" height="50">')
    got = {(r["item"], r["actual"]) for r in rows(site, "layout")}
    assert ("Picture stretched", "stretched") in got
    assert ("Picture outside its area", "wide") in got
    assert ("Picture not loaded", "gone") in got
    assert not any(a == "good" for _, a in got)


def test_clean_page_has_no_layout_failure(tmp_path, serve):
    site = _site(tmp_path, serve, '<p>Picture:</p><img src="pic.png" alt="ok" width="200" height="100">')
    assert not rows(site, "layout") and rows(site, "layout", "pass")
