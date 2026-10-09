"""site.pdf: every site-run row (left nav, links, pager, layout, typography, ...) captured, one line
each, stating what was expected and what the page has."""
import pymupdf
import pytest

from pdfval import load_config
from pdfval.engine import compare_url
from pdfval.report import site_report
from test_site_nav import make_site, serve  # noqa: F401  (the fixture)


def test_site_report_is_not_built_for_a_plain_pdf_run(tmp_path):
    assert site_report.build({"site": {}}, tmp_path) is None
    assert not (tmp_path / "site.pdf").exists()


def test_site_report_captures_every_group_and_row(tmp_path, serve):
    pdf = make_site(tmp_path, drop_nav="Basic operations", wrong_next="overview", broken_anchor="Mounting",
                     bad_h1_css=True)
    run_dir = tmp_path / "run"
    r = compare_url(str(pdf), serve + "overview.html", str(run_dir), load_config())
    out = site_report.build(r, run_dir)
    assert out and out.exists()
    doc = pymupdf.open(out)
    text = "\n".join(p.get_text() for p in doc)
    doc.close()
    # a fail row (left nav) and a pass row (CSS vs spec is on by default; the bad H1 made it fail too)
    assert "Left navigation" in text
    assert "Basic operations" in text
    assert "Headline 1" in text
    assert "bold missing" in text.lower() or "font weight" in text.lower()


def test_site_report_embeds_a_screenshot_crop_for_a_broken_image(tmp_path, serve):
    pdf = make_site(tmp_path)
    page = tmp_path / "guide" / "overview.html"
    page.write_text(page.read_text().replace('<div class="pager">',
                     '<p>Missing:</p><img src="nope.png" alt="gone" width="100" height="50"><div class="pager">'))
    run_dir = tmp_path / "run"
    r = compare_url(str(pdf), serve + "overview.html", str(run_dir), load_config())
    out = site_report.build(r, run_dir)
    assert out and out.exists()
    layout_rows = [row for row in r["site"]["rows"] if row["group"] == "layout" and row["item"] == "Picture not loaded"]
    assert layout_rows and layout_rows[0].get("shot")
    assert (run_dir / "site-shots" / "crops" / "0001.png").exists()
    doc = pymupdf.open(out)
    assert any(p.get_images() for p in doc)
    doc.close()
