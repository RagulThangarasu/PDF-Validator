"""AEM Sites: “On this page” must list the headings down to H4, and each page is checked for breaking issues
and picture problems (not loaded, stretched, outside its area, sideways scroll)."""
import pymupdf

from pdfval import load_config
from pdfval.engine import compare_url
from test_site_nav import AEM_BODY, make_site, serve  # noqa: F401  (the fixture)


def _png(path, w, h):
    pm = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, w, h), False)
    pm.clear_with(180)
    pm.save(str(path))


def _site(tmp_path, base, extra_html, **site_cfg):
    pdf = make_site(tmp_path)
    page = tmp_path / "guide" / "overview.html"
    _png(tmp_path / "guide" / "pic.png", 200, 100)
    page.write_text(page.read_text().replace('<div class="pager">', extra_html + '<div class="pager">'))
    cfg = load_config()
    cfg["site"]["check_otp"] = True  # otp is off by default (old check) - this file tests it directly
    cfg["site"].update(site_cfg)
    return compare_url(str(pdf), base + "overview.html", str(tmp_path / "run"), cfg)["site"]


def rows(site, group, status="fail"):
    return [r for r in site["rows"] if r["group"] == group and r["status"] == status]


def test_h4_missing_from_on_this_page_is_reported(tmp_path, serve):
    site = _site(tmp_path, serve, '<h3 id="x1">Panel</h3><p>About the panel.</p><h4 id="x2">Touch layer</h4><p>About the touch layer.</p>')
    missing = {r["item"]: r["note"] for r in rows(site, "otp")}
    assert missing.get("Touch layer") == "h4 not listed" and missing.get("Panel") == "h3 not listed"


_PICTURES = ('<p>Good picture:</p><img src="pic.png" alt="good" width="200" height="100">'
             '<p>Stretched:</p><img src="pic.png" alt="stretched" style="width:200px;height:200px">'
             '<p>Too wide:</p><img src="pic.png" alt="wide" style="width:900px;height:450px;max-width:none">'
             '<p>Missing:</p><img src="nope.png" alt="gone" width="100" height="50">')


def test_a_picture_that_is_not_there_is_reported(tmp_path, serve):
    """A site run asks one thing of a picture: is it there? A picture that does not load is reported;
    one that is stretched or runs wide is not - a web page reflows its pictures, so how one is sized
    or scaled is not validated against a printed page's rules."""
    got = {(r["item"], r["actual"]) for r in rows(_site(tmp_path, serve, _PICTURES), "layout")}
    assert ("Picture not loaded", "gone") in got
    assert not {i for i, _ in got} & {"Picture stretched", "Picture outside its area",
                                      "Picture enlarged (blurred)", "Picture alignment",
                                      "Picture alignment (design spec)"}, got
    assert not any(a == "good" for _, a in got)


def test_picture_layout_breaks_are_reported_when_asked_for(tmp_path, serve):
    """[site] check_picture_layout = true brings the layout checks back, unchanged."""
    site = _site(tmp_path, serve, _PICTURES, check_picture_layout=True)
    got = {(r["item"], r["actual"]) for r in rows(site, "layout")}
    assert ("Picture stretched", "stretched") in got
    assert ("Picture outside its area", "wide") in got
    assert ("Picture not loaded", "gone") in got
    assert not any(a == "good" for _, a in got)


def test_clean_page_has_no_layout_failure(tmp_path, serve):
    # centred (design spec: every picture is centred) - an un-styled inline <img> would otherwise sit left,
    # which is exactly what check_image_center now correctly flags; this test is about a genuinely clean page
    site = _site(tmp_path, serve, '<p>Picture:</p><img src="pic.png" alt="ok" width="200" height="100" style="display:block;margin:0 auto">')
    assert not rows(site, "layout") and rows(site, "layout", "pass")


_LEFT_PICTURE = '<p>Picture:</p><img src="pic.png" alt="left" width="200" height="100">'


def test_a_left_aligned_picture_is_not_a_site_issue(tmp_path, serve):
    """Where a picture sits in the text column is a printed page's rule: a web page places its pictures
    by its own stylesheet, so alignment is not validated for a site."""
    assert not [r for r in rows(_site(tmp_path, serve, _LEFT_PICTURE), "layout")
                if r["item"].startswith("Picture alignment")]


def test_left_aligned_picture_fails_the_design_spec_when_asked_for(tmp_path, serve):
    """Design spec ("Image rules": Alignment = Center) with [site] check_image_center = true: a picture
    left in its column is reported even though it is the only picture on the page (check_picture_alignment's
    relative/majority check would see nothing to disagree with - this is the separate, absolute check)."""
    site = _site(tmp_path, serve, _LEFT_PICTURE, check_image_center=True)
    bad = rows(site, "layout")
    assert any(r["item"] == "Picture alignment (design spec)" and r["expected"] == "centred" and "left" in r["actual"] for r in bad)


_STEP_TEXT = ("1. Connect the USB cable between the PC and the monitor via the upstream USB port. This "
             "upstream USB port transmits data between the PC and the USB devices connected to the monitor.")


def test_numbered_step_without_hanging_indent_is_reported(tmp_path, serve):
    """A step number typed as plain text ("1. ...", not a real <li> counter) that wraps to a second line
    must hang-indent: the wrapped line has to start to the right of the "1.", clearing it - not flush
    beneath it, which prints the next line's text directly under the number instead of under the step's
    own text."""
    site = _site(tmp_path, serve, f'<p>{_STEP_TEXT}</p>')
    bad = rows(site, "layout")
    assert any(r["item"] == "Wrapped line not indented under the text" for r in bad), bad


def test_numbered_step_with_hanging_indent_is_not_reported(tmp_path, serve):
    """The same wrapped step, this time with the standard hanging-indent CSS trick (padding-left +
    negative text-indent) applied: the wrapped line clears the marker, so nothing is reported."""
    site = _site(tmp_path, serve, f'<p style="padding-left:1.5em;text-indent:-1.5em">{_STEP_TEXT}</p>')
    assert not any(r["item"] == "Wrapped line not indented under the text" for r in rows(site, "layout"))


_STICKY = '<div style="position:fixed;top:0;left:0;right:0;height:60px;background:#fff;z-index:10">Sticky bar</div>'
# enough height that scrolling the heading below it to the top of the viewport is actually possible
# (the page must be taller than the viewport by more than the bar's height, or the browser clamps the
# scroll short of fully aligning the heading, and the bar never gets the chance to cover it)
_FILLER = '<div style="height:2000px">spacer</div>'


def test_heading_under_sticky_header_is_reported(tmp_path, serve):
    """A heading far down the page (so jumping to it, like a reader clicking it in the nav, actually
    scrolls): if a sticky/fixed top bar stays pinned over the viewport and the page does not reserve
    space for it, the jump lands with the heading's top partly covered by the bar - reported even though
    nothing is wrong at the page's own load scroll position, which this bug never shows at."""
    # a spacer after the heading too: the browser can only scroll a heading all the way to the top of the
    # viewport if there is enough page left below it to make that scroll position reachable at all
    site = _site(tmp_path, serve, _STICKY + _FILLER + '<h2 id="jump">Remote controller exterior view</h2>' + _FILLER)
    bad = rows(site, "layout")
    hit = next((r for r in bad if r["item"] == "Jump target hidden under sticky header"
                and r["actual"] == "Remote controller exterior view"), None)
    assert hit is not None, bad


def test_heading_clear_of_sticky_header_is_not_reported(tmp_path, serve):
    """The same sticky bar, but the heading reserves space for it (scroll-margin-top matching the bar's
    height, the standard fix): the browser's own scrollIntoView honours it, so the jump lands clear of
    the bar - not reported."""
    site = _site(tmp_path, serve, _STICKY + _FILLER + '<h2 id="jump" style="scroll-margin-top:64px">Clear heading</h2>' + _FILLER)
    assert not any(r["item"] == "Jump target hidden under sticky header" and r["actual"] == "Clear heading"
                   for r in rows(site, "layout"))


def test_empty_on_this_page_box_does_not_swallow_the_page(tmp_path, serve):
    """A topic with no sub-headings renders its "On this page" box empty - no links of its own. The panel
    search must not climb out of that empty box and up past the content root looking for one, or the
    page's whole container is taken for the panel and every heading and paragraph in it counts as chrome:
    the typography checks then sample nothing at all and report a clean page instead of its real issues."""
    site = _site(tmp_path, serve, '<div class="toc topic-toc-root is-toc-empty"><p>On this page</p></div>')
    assert not [r for r in site["rows"] if "nothing found to sample" in r.get("actual", "")]
    # the content really was sampled: the page's own H1 was measured against the design spec
    assert [r for r in site["rows"] if r["group"] == "typography" and r["item"].startswith("Headline 1")]



def test_a_design_spec_issue_still_carries_its_shot_though_the_docx_no_longer_lists_it(tmp_path, serve):
    """A picture left in its column fails the design spec and the row still carries a shot (the data to
    show it exists), even though genuine-issues.docx no longer prints a design-spec appendix for it -
    removed on request: it duplicated site.pdf's own "Page layout and pictures" section."""
    from docx import Document

    from pdfval.report import writer
    pdf = make_site(tmp_path)
    page = tmp_path / "guide" / "overview.html"
    _png(tmp_path / "guide" / "pic.png", 200, 100)
    page.write_text(page.read_text().replace(
        '<div class="pager">', '<p>Picture:</p><img src="pic.png" alt="left" width="200" height="100"><div class="pager">'))
    run_dir = tmp_path / "run"
    cfg = load_config()
    cfg["site"]["check_image_center"] = True  # off by default: a site's pictures are checked for being there
    result = compare_url(str(pdf), serve + "overview.html", str(run_dir), cfg)
    rows = [r for r in result["site"]["rows"] if r["item"] == "Picture alignment (design spec)"]
    assert rows and rows[0].get("shot"), rows
    writer.write_pdf_report(result, run_dir)
    doc = Document(run_dir / "genuine-issues.docx")
    assert not any(t.rows[0].cells[0].text == "Bug" for t in doc.tables)


def test_a_margin_break_is_reported_in_the_bug_report_with_its_picture(tmp_path, serve):
    """A table running outside the page's own content area is a structural break on a web page, not a
    cosmetic one - it gets its own "Layout — breaking the page margins" section in the bug report
    (genuine-issues.pdf and .docx), with a crop of the actual overflow, not only in site.pdf."""
    from docx import Document

    from pdfval.report import writer
    pdf = make_site(tmp_path, extra_body='<table style="width:2000px"><tr><td>Overflow</td><td>cell</td></tr></table>')
    run_dir = tmp_path / "run"
    result = compare_url(str(pdf), serve + "overview.html", str(run_dir), load_config())
    rows = [r for r in result["site"]["rows"] if r["group"] == "layout" and r.get("kind") == "block-overflow"]
    assert rows and rows[0].get("shot"), rows
    writer.write_pdf_report(result, run_dir)
    pdf_doc = pymupdf.open(run_dir / "genuine-issues.pdf")
    pdf_text = "\n".join(p.get_text() for p in pdf_doc)
    pdf_doc.close()
    assert "Layout — breaking the page margins" in pdf_text
    # the column wraps long detail text onto its own lines - compare with whitespace collapsed
    assert "outside the content area" in " ".join(pdf_text.split())
    doc = Document(run_dir / "genuine-issues.docx")
    table = next(t for t in doc.tables if t.rows[0].cells[0].text == "Bug")
    assert any("outside the content area" in r.cells[2].text for r in table.rows[1:])


def test_a_scrollable_table_is_not_reported_as_breaking_the_margins(tmp_path, serve):
    """A wide table whose container scrolls (overflow-x: auto on a wrapper, not the table's own
    immediate parent) is a deliberate, responsive pattern - not a structural break."""
    wrapped = ('<div class="table-wrap"><div class="table-scroll" style="overflow-x:auto">'
              '<table style="width:2000px"><tr><td>Overflow</td><td>cell</td></tr></table></div></div>')
    site = _site(tmp_path, serve, wrapped)
    assert not [r for r in site["rows"] if r["group"] == "layout" and r.get("kind") == "block-overflow"]


def test_a_table_scrollable_on_itself_is_not_reported_as_breaking_the_margins(tmp_path, serve):
    """overflow-x: auto can also sit directly on the table, with no wrapper div at all."""
    wrapped = '<table style="width:2000px;overflow-x:auto;display:block"><tr><td>Overflow</td><td>cell</td></tr></table>'
    site = _site(tmp_path, serve, wrapped)
    assert not [r for r in site["rows"] if r["group"] == "layout" and r.get("kind") == "block-overflow"]


def test_a_font_size_issue_is_reported_in_the_bug_report(tmp_path, serve):
    """The H1's font size against the Figma spec gets its own "Font size — design spec (Figma)" section
    in the bug report (genuine-issues.pdf and .docx) - only the size mismatch, not every CSS property
    the page also gets wrong (font family, weight, colour, ... stay in results.json only)."""
    from docx import Document

    from pdfval.report import writer
    pdf = make_site(tmp_path, bad_h1_css=True)
    run_dir = tmp_path / "run"
    result = compare_url(str(pdf), serve + "overview.html", str(run_dir), load_config())
    rows = [r for r in result["site"]["rows"]
            if r["group"] == "typography" and "font size" in r["item"] and r["status"] == "fail"]
    assert rows, [r["item"] for r in result["site"]["rows"] if r["group"] == "typography"]
    writer.write_pdf_report(result, run_dir)
    pdf_doc = pymupdf.open(run_dir / "genuine-issues.pdf")
    pdf_text = "\n".join(p.get_text() for p in pdf_doc)
    pdf_doc.close()
    assert "Font size — design spec (Figma)" in pdf_text
    assert "Headline 1 — font size" in pdf_text
    doc = Document(run_dir / "genuine-issues.docx")
    table = next(t for t in doc.tables if t.rows[0].cells[0].text == "Bug")
    assert any("Headline 1 — font size" in r.cells[1].text for r in table.rows[1:])


def test_an_image_not_loading_is_reported_in_the_bug_report(tmp_path, serve):
    """A picture that does not load on the live page gets its own "Images — not loading on the live
    page" section in the bug report, with a crop of the broken spot."""
    from docx import Document

    from pdfval.report import writer
    pdf = make_site(tmp_path, extra_body='<img src="nope.png" alt="gone" width="100" height="50">')
    run_dir = tmp_path / "run"
    result = compare_url(str(pdf), serve + "overview.html", str(run_dir), load_config())
    rows = [r for r in result["site"]["rows"] if r["group"] == "layout" and r.get("kind") == "image-broken"]
    assert rows and rows[0].get("shot"), rows
    writer.write_pdf_report(result, run_dir)
    pdf_doc = pymupdf.open(run_dir / "genuine-issues.pdf")
    pdf_text = "\n".join(p.get_text() for p in pdf_doc)
    pdf_doc.close()
    assert "Images — not loading on the live page" in pdf_text
    assert "Picture not loaded" in pdf_text
    doc = Document(run_dir / "genuine-issues.docx")
    table = next(t for t in doc.tables if t.rows[0].cells[0].text == "Bug")
    assert any("Picture not loaded" in r.cells[1].text for r in table.rows[1:])


def test_hyperlink_issues_are_reported_in_the_bug_report(tmp_path, serve):
    """A link that still carries a DITA GUID, or does not open, gets its own "Hyperlinks" section in
    the bug report - the left navigation's links and the content's own cross-references alike."""
    from docx import Document

    from pdfval.report import writer
    pdf = make_site(tmp_path, extra_body=AEM_BODY)
    run_dir = tmp_path / "run"
    result = compare_url(str(pdf), serve + "overview.html", str(run_dir), load_config())
    rows = [r for r in result["site"]["rows"] if r["group"] == "links" and r.get("kind") == "link-guid"]
    assert rows, [r.get("kind") for r in result["site"]["rows"] if r["group"] == "links"]
    writer.write_pdf_report(result, run_dir)
    pdf_doc = pymupdf.open(run_dir / "genuine-issues.pdf")
    pdf_text = "\n".join(p.get_text() for p in pdf_doc)
    pdf_doc.close()
    assert "Hyperlinks" in pdf_text
    assert "GUID in the link" in pdf_text
    doc = Document(run_dir / "genuine-issues.docx")
    table = next(t for t in doc.tables if t.rows[0].cells[0].text == "Bug")
    assert any("GUID in the link" in r.cells[1].text for r in table.rows[1:])
