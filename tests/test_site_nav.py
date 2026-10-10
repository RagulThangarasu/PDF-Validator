"""Site navigation of a web guide (pdfval/site_nav.py): left navigation vs the PDF's L1 TOC,
navigation links, Download PDF, next / previous topic, On this page, product subtitle."""
import functools
import re
import http.server
import threading

import pymupdf
import pytest

from pdfval import load_config, site_nav
from pdfval.engine import compare_url

CHAPTERS = [("overview", "Product overview", ["Specifications", "Package contents"]),
            ("install", "Installation", ["Unboxing", "Mounting"]),
            ("operate", "Basic operations", ["Power button", "Home screen"])]


def make_pdf(path):
    doc = pymupdf.open()
    cover = doc.new_page()
    cover.insert_text((72, 200), "SX1000 Series", fontsize=28)
    cover.insert_text((72, 240), "User Manual", fontsize=40)
    toc = []
    for _, title, subs in CHAPTERS:
        p = doc.new_page()
        p.insert_text((72, 80), title, fontsize=20)
        toc.append([1, title, doc.page_count])
        y = 120
        for s in subs:
            p.insert_text((72, y), s, fontsize=15)
            p.insert_text((72, y + 24), f"Text about {s.lower()} for the SX1000.", fontsize=11)
            toc.append([2, s, doc.page_count])
            y += 80
    doc.set_toc(toc)
    doc.save(path)


def make_site(root, *, drop_nav=None, wrong_next=None, broken_anchor=None, subtitle=None, bad_h1_css=False,
              breadcrumb_broken=False, breadcrumb_gap=0, extra_body=""):
    """One page per chapter in root/guide, with the chrome of the BenQ guide."""
    site = root / "guide"
    site.mkdir()
    make_pdf(site / "manual.pdf")
    nav = "".join(f'<li><a href="{s}.html">{t}</a></li>' for s, t, _ in CHAPTERS if t != drop_nav)
    h1_css = "font-weight:400;font-size:20px;color:#333333;margin:0 0 0 0" if bad_h1_css else \
             "font-family:Poppins,sans-serif;font-weight:700;font-size:32px;line-height:40px;color:#000000;margin:0 0 0 0"
    for i, (slug, title, subs) in enumerate(CHAPTERS):
        otp = "".join(f'<li><a href="#{"nope" if h == broken_anchor else f"s{k}"}">{h}</a></li>' for k, h in enumerate(subs))
        body = "".join(f'<h3 id="s{k}">{h}</h3><p>Text about {h.lower()} for the SX1000.</p>' for k, h in enumerate(subs))
        nxt = CHAPTERS[i + 1] if i + 1 < len(CHAPTERS) else None
        if nxt and slug == wrong_next:
            nxt = CHAPTERS[0]
        prv = CHAPTERS[i - 1] if i else None
        pager = (f'<div><a href="{prv[0]}.html">← PREVIOUS TOPIC</a><div>{prv[1]}</div></div>' if prv else "") + \
                (f'<div><a href="{nxt[0]}.html">NEXT TOPIC →</a><div>{nxt[1]}</div></div>' if nxt else "")
        (site / f"{slug}.html").write_text(f"""<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<style>
h1{{{h1_css}}}
h2{{font-family:Poppins,sans-serif;font-weight:700;font-size:28px;line-height:32px;color:#000000;margin:24px 0 16px 0}}
h3{{font-family:Poppins,sans-serif;font-weight:700;font-size:20px;line-height:24px;color:#000000;margin:16px 0 8px 0}}
p{{font-family:Roboto,sans-serif;font-weight:400;font-size:16px;line-height:24px;color:#000000;margin:0 0 16px 0}}
a{{font-family:Roboto,sans-serif;font-weight:400;font-size:16px;line-height:24px;color:#7231C6;text-decoration:underline}}
</style></head>
<body style="margin:0"><header><a href="/"><svg width="30" height="20"></svg><span>Guide</span></a>
<div><span class="product-title" style="font-weight:700">{subtitle or 'SX1000 Series'}</span>
<a href="manual.pdf">Download PDF</a></div></header>
<div style="display:flex"><nav style="width:220px"><p>Table of Contents</p><ul>
{nav.replace(f'href="{slug}.html"', f'href="{slug}.html" aria-current="page"')}</ul></nav>
<main style="width:700px"><nav class="breadcrumb" style="margin:0 0 {breadcrumb_gap}px 0"><a href="overview.html">{subtitle or 'SX1000 Series'}</a> &gt; 
<a href="{'nope.html' if breadcrumb_broken else slug + '.html'}">{title}</a></nav><h1>{title}</h1>{body}{extra_body if slug == "overview" else ""}<div class="pager">{pager}</div></main>
<aside style="width:220px"><p>On this page</p><ul>{otp}</ul></aside></div>
<footer>SX1000 Series user manual</footer></body></html>""")
    return site / "manual.pdf"


@pytest.fixture
def serve(tmp_path):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    http.server.SimpleHTTPRequestHandler.log_message = lambda *a: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/guide/"
    srv.shutdown()


def run(tmp_path, base, **defects):
    pdf = make_site(tmp_path, **defects)
    cfg = load_config()
    # subtitle / download rows are off by default (not on the active validation list) - these tests
    # exercise that underlying logic, so opt back in explicitly; pager / otp are on by default already
    cfg["site"].update(check_download=True, check_subtitle=True)
    r = compare_url(str(pdf), base + "overview.html", str(tmp_path / "run"), cfg)
    return r["site"]


def failed(site, group):
    return [r for r in site["rows"] if r["group"] == group and r["status"] == "fail"]


def test_clean_site_has_no_failure(tmp_path, serve):
    site = run(tmp_path, serve)
    assert site["summary"]["fail"] == 0, [r for r in site["rows"] if r["status"] == "fail"]
    assert site["summary"]["groups"]["nav"]["pass"] >= 3
    assert site["download"]["is_pdf"]


def test_defects_are_reported(tmp_path, serve):
    site = run(tmp_path, serve, drop_nav="Basic operations", wrong_next="overview", broken_anchor="Mounting",
               subtitle="Monitor arm BSH Series")
    assert any(r["item"] == "Basic operations" for r in failed(site, "nav"))
    assert any(r["item"] == "Next topic" and "overview" in r["page"] for r in failed(site, "pager"))
    assert any(r["item"] == "Mounting" for r in failed(site, "otp"))
    assert any(r["item"] == "Matches the prod PDF" for r in failed(site, "subtitle"))


def test_old_checks_are_off_by_default(tmp_path, serve):
    """subtitle / download rows: off unless [site] check_* is turned back on."""
    pdf = make_site(tmp_path, drop_nav="Basic operations", wrong_next="overview", broken_anchor="Mounting")
    r = compare_url(str(pdf), serve + "overview.html", str(tmp_path / "run"), load_config())
    site = r["site"]
    for group in ("subtitle",):
        assert not [row for row in site["rows"] if row["group"] == group]


def test_otp_is_on_by_default_and_jumps_to_the_right_h3(tmp_path, serve):
    """On this page: on the active list by default. A broken entry (wrong/missing anchor) is a fail;
    a correct one (clicking it lands on its own h3) is a pass."""
    pdf = make_site(tmp_path, broken_anchor="Mounting")
    r = compare_url(str(pdf), serve + "overview.html", str(tmp_path / "run"), load_config())
    site = r["site"]
    assert any(row["item"] == "Mounting" and row["status"] == "fail" for row in site["rows"] if row["group"] == "otp")
    assert any(row["item"] == "Unboxing" and row["status"] == "pass" for row in site["rows"] if row["group"] == "otp")


def _h2_page(tmp_path, serve, cfg):
    """The overview page with its sub-headings as h2 (the shape of a BenQ topic page: H1 title, H2 sections),
    its "On this page" listing them."""
    pdf = make_site(tmp_path)
    page = tmp_path / "guide" / "overview.html"
    page.write_text(page.read_text().replace("<h3 ", "<h2 ").replace("</h3>", "</h2>"))
    r = compare_url(str(pdf), serve + "overview.html", str(tmp_path / "run"), cfg)
    return [row for row in r["site"]["rows"] if row["group"] == "otp" and "overview" in row["page"]]


def test_h2_sections_listed_on_this_page_are_not_faulted(tmp_path, serve):
    """A page whose sections are h2 and whose "On this page" lists them is correct: its entries are
    checked and pass, and it is NOT told to hide the panel (the H1 is the page title, so an H2 is a
    section of the page, not the page itself)."""
    rows = _h2_page(tmp_path, serve, load_config())
    assert not any(row["status"] == "warn" and "hidden" in row["expected"] for row in rows), rows
    assert any(row["status"] == "pass" for row in rows), rows


def test_otp_hidden_when_the_page_has_no_section_of_its_own(tmp_path, serve):
    """A guide that lists only h3/h4 ([site] on_this_page_min_level = 3): the same page then has nothing
    to list, so showing the panel anyway is a warn."""
    cfg = load_config()
    cfg["site"]["on_this_page_min_level"] = 3
    rows = _h2_page(tmp_path, serve, cfg)
    assert any(row["status"] == "warn" and "hidden" in row["expected"] for row in rows), rows


def test_a_page_with_sections_but_an_empty_list_is_reported(tmp_path, serve):
    """"On this page" rendered with no entries, on a page that does have sections: the reader is given an
    empty list where the page's own sections should be - reported, not passed over in silence."""
    pdf = make_site(tmp_path)
    page = tmp_path / "guide" / "overview.html"
    html = page.read_text().replace("<h3 ", "<h2 ").replace("</h3>", "</h2>")
    html = re.sub(r"<aside style=\"width:220px\"><p>On this page</p><ul>.*?</ul></aside>",
                  '<aside style="width:220px"><p>On this page</p><ul></ul></aside>', html, flags=re.S)
    page.write_text(html)
    r = compare_url(str(pdf), serve + "overview.html", str(tmp_path / "run"), load_config())
    rows = [row for row in r["site"]["rows"] if row["group"] == "otp" and "overview" in row["page"]]
    assert any(row["status"] == "fail" and row["item"] == "On this page" for row in rows), rows


def test_pager_is_on_by_default(tmp_path, serve):
    """Next/previous topic: on the active list, checked without opting in. First page must have no
    "previous", last page must have no "next", every other page must have a "next"."""
    pdf = make_site(tmp_path)
    r = compare_url(str(pdf), serve + "overview.html", str(tmp_path / "run"), load_config())
    site = r["site"]
    passed = [row for row in site["rows"] if row["group"] == "pager" and row["status"] == "pass"]
    assert any(row["item"] == "Previous topic" and row["expected"] == "none (first page)" for row in passed)
    assert any(row["item"] == "Next topic" and row["expected"] == "none (last page)" for row in passed)
    assert any(row["item"] == "Next topic" and "overview" in row["page"] for row in passed)


def test_css_not_matching_the_design_spec_is_reported(tmp_path, serve):
    """Headline 1 set 400/20px/#333333 instead of the spec's 700/32px/#000000: bold missing + size + colour."""
    pdf = make_site(tmp_path, bad_h1_css=True)
    r = compare_url(str(pdf), serve + "overview.html", str(tmp_path / "run"), load_config())
    site = r["site"]
    typo = failed(site, "typography")
    assert any(row["item"] == "Headline 1 — bold missing" for row in typo)
    assert any(row["item"] == "Headline 1 — font size" for row in typo)
    assert any(row["item"] == "Headline 1 — colour" for row in typo)


def test_table_header_centred_is_reported_not_left_aligned(tmp_path, serve):
    """Design spec: table headers are always left-aligned. A centred <th> must fail, a left-aligned one passes."""
    extra = '<table><thead><tr><th style="text-align:center">Spec</th></tr></thead><tbody><tr><td>12V</td></tr></tbody></table>'
    pdf = make_site(tmp_path, extra_body=extra)
    r = compare_url(str(pdf), serve + "overview.html", str(tmp_path / "run"), load_config())
    typo = failed(r["site"], "typography")
    assert any(row["item"] == "Table header — alignment" and row["expected"] == "left" and row["actual"] == "center" for row in typo)


def test_callout_title_not_upper_case_is_reported(tmp_path, serve):
    """Design spec: callout titles (IMPORTANT/NOTE/TIP/WARNING) are printed in upper case."""
    extra = '<p><b style="font-weight:700">note</b> Keep the device dry.</p>'
    pdf = make_site(tmp_path, extra_body=extra)
    r = compare_url(str(pdf), serve + "overview.html", str(tmp_path / "run"), load_config())
    typo = failed(r["site"], "typography")
    assert any(row["item"] == "Callout title — case" and row["expected"] == "UPPER CASE" for row in typo)


def test_every_paragraph_gets_its_own_typography_status_not_one_sample(tmp_path, serve):
    """Every visible instance of a role is checked, not one sample standing in for the whole page: a
    page with a correctly-styled paragraph AND a mis-styled one must report BOTH - one pass, one fail -
    not just the first paragraph found (which would silently hide the second one's real problem)."""
    extra = ('<p>A correctly styled paragraph about the SX1000.</p>'
            '<p style="font-size:11px">A second paragraph with the wrong font size.</p>')
    pdf = make_site(tmp_path, extra_body=extra)
    r = compare_url(str(pdf), serve + "overview.html", str(tmp_path / "run"), load_config())
    rows = [row for row in r["site"]["rows"] if row["group"] == "typography" and "overview" in row["page"]
           and row["item"].startswith("Body default")]
    assert any(row["status"] == "pass" for row in rows), rows
    assert any(row["status"] == "fail" and "wrong font size" in row["note"] for row in rows), rows

def test_opacity_zero_content_is_still_sampled(tmp_path, serve):
    """A scroll-reveal element (opacity:0 until a reader scrolls to it - common on real sites) must still
    be sampled for typography: a single evaluate() never scrolls the page first, so treating opacity:0 as
    "invisible" silently sampled nothing for sites that use this pattern, validating nothing against the
    design spec without ever reporting it (see site_nav.py's `visible()` - checkOpacity is not set)."""
    extra = '<p><strong style="opacity:0">Important setting</strong> affects performance.</p>'
    pdf = make_site(tmp_path, extra_body=extra)
    r = compare_url(str(pdf), serve + "overview.html", str(tmp_path / "run"), load_config())
    rows = [row for row in r["site"]["rows"] if row["group"] == "typography" and "overview" in row["page"]]
    assert any(row["item"].startswith("Body strong") for row in rows), rows
    assert not any("nothing found to sample" in (row.get("actual") or "") for row in rows)


def test_empty_page_is_reported_not_silently_skipped(tmp_path):
    """A page where nothing at all could be sampled (every CSS role came back null - a page that failed to
    render, or whose content selector matched nothing) must produce an explicit warning, not look exactly
    like a clean pass by contributing zero rows."""
    make_pdf(pdf_path := tmp_path / "baseline.pdf")
    site = {"pages": [{"url": "https://example.com/empty.html", "css": {}, "css_sampled": False}], "links": {}}
    cfg = {"check_download": False, "check_pager": False, "check_otp": False, "check_breadcrumb": False,
          "check_subtitle": False, "check_picture_alignment": False, "check_image_center": False}
    r = site_nav.evaluate(site, str(pdf_path), cfg, {})
    typo = [row for row in r["rows"] if row["group"] == "typography"]
    assert any(row["status"] == "warn" and "nothing found to sample" in row["actual"] for row in typo), typo


def test_breadcrumb_is_on_by_default_and_flags_a_broken_redirect(tmp_path, serve):
    """Breadcrumb: on the active list by default. Present above the H1 with a gap matching the design
    spec (0px, flush) -> pass; a crumb whose link goes nowhere (404) is a fail, with a screenshot crop
    of that crumb."""
    run_dir = tmp_path / "run"
    pdf = make_site(tmp_path, breadcrumb_broken=True)
    r = compare_url(str(pdf), serve + "overview.html", str(run_dir), load_config())
    site = r["site"]
    assert any(row["item"] == "Space above H1 (breadcrumb)" and row["status"] == "pass" for row in site["rows"] if row["group"] == "breadcrumb")
    broken = [row for row in site["rows"] if row["group"] == "breadcrumb" and row["status"] == "fail" and row["item"] != "Space above H1 (breadcrumb)"]
    assert broken and broken[0].get("shot")


def test_breadcrumb_gap_not_matching_spec_is_reported(tmp_path, serve):
    """The spec (config/typography.toml heading_spacing.h1.margin_top) wants the H1 flush against the
    breadcrumb (0px). A page with extra air between them must fail with the exact expected/actual px,
    not silently pass just because a breadcrumb exists above the H1."""
    pdf = make_site(tmp_path, breadcrumb_gap=24)
    r = compare_url(str(pdf), serve + "overview.html", str(tmp_path / "run"), load_config())
    site = r["site"]
    bad = [row for row in site["rows"] if row["group"] == "breadcrumb" and row["item"] == "Space above H1 (breadcrumb)"]
    assert any(row["status"] == "fail" and row["expected"] == "0px" and row["actual"] == "24px" for row in bad)


def test_typography_sampling_skips_breadcrumb_and_pager_links(tmp_path, serve):
    """body_hyperlink's CSS sample must come from a real in-content link, not the breadcrumb or the pager
    bar (both chrome, styled differently on purpose here) - regression test for a real false positive found
    in production: a site's breadcrumb link was sampled as "Body hyperlink" and failed a spec it was never
    meant to follow."""
    pdf = make_site(tmp_path)
    page = tmp_path / "guide" / "overview.html"
    html = page.read_text()
    bad = 'style="color:#000000;font-size:10px;text-decoration:none"'
    html = html.replace('<a href="overview.html">Home</a>', f'<a href="overview.html" {bad}>Home</a>')
    html = html.replace('<a href="install.html">NEXT TOPIC →</a>', f'<a href="install.html" {bad}>NEXT TOPIC →</a>')
    html = html.replace('<p>Text about specifications for the SX1000.</p>',
                        '<p>Text about specifications for the SX1000. See the '
                        '<a href="#" style="font-family:Roboto;font-weight:400;font-size:16px;'
                        'line-height:24px;color:#7231C6;text-decoration:underline">full spec sheet</a>.</p>')
    page.write_text(html)
    r = compare_url(str(pdf), serve + "overview.html", str(tmp_path / "run"), load_config())
    site = r["site"]
    hyperlink_rows = [row for row in site["rows"] if row["group"] == "typography" and row["item"].startswith("Body hyperlink")]
    assert hyperlink_rows and hyperlink_rows[0]["status"] == "pass", hyperlink_rows


def test_spacing_is_validated_margins_and_block_gaps(tmp_path, serve):
    """Space vs the design spec: a heading's own margin (H2 should be 24px above / 16px below) and the
    gap between two directly adjacent content blocks (two paragraphs with nothing between them, including
    no heading, should be 16px)."""
    pdf = make_site(tmp_path)
    page = tmp_path / "guide" / "overview.html"
    html = page.read_text().replace(
        "h3{font-family:Poppins,sans-serif;font-weight:700;font-size:20px;line-height:24px;color:#000000;margin:16px 0 8px 0}",
        "h3{font-family:Poppins,sans-serif;font-weight:700;font-size:20px;line-height:24px;color:#000000;margin:4px 0 2px 0}")
    html = html.replace("<p>Text about specifications for the SX1000.</p>",
                        '<p>Text about specifications for the SX1000.</p>'
                        '<p style="margin-top:40px">A second paragraph right after it.</p>')
    page.write_text(html)
    r = compare_url(str(pdf), serve + "overview.html", str(tmp_path / "run"), load_config())
    rows = [row for row in r["site"]["rows"] if row["group"] == "typography" and row["status"] == "fail"]
    assert any(row["item"] == "Headline 3 — space above" for row in rows)
    assert any(row["item"] == "Headline 3 — space below" for row in rows)
    assert any(row["item"] == "Space: paragraph → paragraph" and row["expected"] == "16px" for row in rows)



def test_crawl_follows_navigation_links_to_the_repository_path():
    """AEM's left navigation links /content/guide/<guide folder>/page.html; the site redirects it to
    /<guide folder>/page.html. The crawl must follow those links as pages of the same guide."""
    from pdfval.html_source import _guide_links

    class Page:
        def evaluate(self, _js):
            return ["https://site/content/guide/edu/tey1c/en/port-overview.html",
                    "https://site/edu/tey1c/en/safety.html",
                    "https://site/content/guide/edu/tey1c/en/port-overview.html#top",  # same page
                    "https://site/content/guide/edu/other/en/intro.html",               # another guide
                    "https://site/content/dam/edu/tey1c/en/manual.pdf"]                 # a download
    assert _guide_links(Page(), "https://site/edu/tey1c/en/package-contents.html") == [
        "https://site/edu/tey1c/en/port-overview.html", "https://site/edu/tey1c/en/safety.html"]


def test_blank_page_error_points_at_its_screenshot(tmp_path, serve):
    """A page that opens fine (no HTTP error, no login form) but has no text anywhere on it must not just
    say "no text found" - it must point at the screenshot captured for it, so the blank capture itself (a
    session / login / network problem, not a selector problem) can be seen right away, instead of someone
    having to dig through runs/<id>/site-shots/ by hand to discover the page rendered empty."""
    pdf = tmp_path / "baseline.pdf"
    make_pdf(pdf)
    (tmp_path / "guide").mkdir(exist_ok=True)
    (tmp_path / "guide" / "blank.html").write_text("<!doctype html><html><body></body></html>")
    with pytest.raises(RuntimeError, match="screenshot was saved to"):
        compare_url(str(pdf), serve + "blank.html", str(tmp_path / "run"), load_config())


def _crumb_page(url, trail, crumbs, subtitle="Identity and Access Management (IAM)"):
    return {"url": url, "breadcrumb": trail, "subtitle": {"text": subtitle}, "page_shot": "site-shots/a.png",
            "breadcrumb_items": [{"text": t, "href": h, "where": "nav.breadcrumb a", "box": [0, 0, 10, 10]}
                                 for t, h in crumbs]}


def test_breadcrumb_missing_the_product_name_is_reported():
    """A trail that opens straight at the page ("Introduction") never names the product the page
    belongs to: it must be a fail, not a silent pass just because a breadcrumb exists."""
    pages = [_crumb_page("http://x/intro.html", "Introduction", [("Introduction", "intro.html")])]
    rows = site_nav._breadcrumb_product_rows(pages, "")
    assert len(rows) == 1 and rows[0]["status"] == "fail"
    assert rows[0]["item"] == "Product name in the breadcrumb"
    assert "Identity and Access Management (IAM)" in rows[0]["expected"]
    assert rows[0]["shot"] == {"page_shot": "site-shots/a.png", "box": [0, 0, 10, 10]}  # crop of the crumb


def test_breadcrumb_starting_with_the_product_passes_and_a_late_product_fails():
    """The product must be the FIRST crumb: deeper in the trail is still wrong, and the note says so."""
    good = _crumb_page("http://x/a.html", "Identity and Access Management (IAM) > Introduction",
                       [("Identity and Access Management (IAM)", "index.html"), ("Introduction", "a.html")])
    late = _crumb_page("http://x/b.html", "Home > Identity and Access Management (IAM) > Introduction",
                       [("Home", "/"), ("Identity and Access Management (IAM)", "index.html"), ("Introduction", "b.html")])
    rows = site_nav._breadcrumb_product_rows([good, late], "")
    assert [r["status"] for r in rows] == ["pass", "fail"]
    assert "not the first crumb" in rows[1]["note"]


def test_breadcrumb_product_name_can_be_set_in_config():
    """[site] breadcrumb_product names the product when the header's title is not the right source."""
    p = _crumb_page("http://x/a.html", "IFP software IAM > Introduction",
                    [("IFP software IAM", "index.html"), ("Introduction", "a.html")])
    assert site_nav._breadcrumb_product_rows([p], "IFP software IAM")[0]["status"] == "pass"
    assert site_nav._breadcrumb_product_rows([p], "")[0]["status"] == "fail"  # header title differs


GOOD_TABLE = """<table class="good" style="border-collapse:collapse;border-radius:1px">
<tr><th style="background:#333333;border:1px solid #DCDCDC;padding:12px;color:#fff">Port</th></tr>
<tr><td style="border:1px solid #DCDCDC;padding:12px">HDMI 1</td></tr></table>"""
BAD_TABLE = """<table class="bad" style="border-collapse:collapse;border-radius:0">
<tr><th style="background:#444444;border:2px solid #EEEEEE;padding:8px;color:#fff">Port</th></tr>
<tr><td style="border:2px solid #EEEEEE;padding:8px">HDMI 2</td></tr></table>"""


def test_table_style_vs_the_design_spec(tmp_path, serve):
    """The table component spec ([typography.formats.html_web.table]: {900-dark-gary} header bar, 1px
    {Stroke/Regular} border, 12px cell padding, {Radius/md} corners). A table built to it passes; one
    with a lighter header, a 2px border in the wrong grey, 8px padding and square corners fails on each
    of those four properties separately."""
    pdf = make_site(tmp_path, extra_body=GOOD_TABLE + BAD_TABLE)
    r = compare_url(str(pdf), serve + "overview.html", str(tmp_path / "run"), load_config())
    rows = [row for row in r["site"]["rows"] if row["item"].startswith("Table style")]
    assert any(row["status"] == "pass" and "table.good" in row["where"] for row in rows), rows
    bad = {row["item"]: (row["expected"], row["actual"]) for row in rows
           if row["status"] == "fail" and "table.bad" in row["where"]}
    assert bad["Table style — header bar colour"] == ("#333333", "#444444")
    assert bad["Table style — border width"] == ("1px", "2px")
    assert bad["Table style — border colour"] == ("#dcdcdc", "#eeeeee")
    assert bad["Table style — cell padding"][0] == "12px" and bad["Table style — cell padding"][1].startswith("8px")
    assert bad["Table style — corner radius"] == ("1px", "0px")
    assert all(row.get("shot") for row in rows)


def _table_page(**style):
    tb = {"header_background": "#333333", "border_width": 1, "border_color": "#DCDCDC", "radius": 1,
          "padding": [12, 12, 12, 12], "cell": "th", "text": "Port", "where": "table", "box": [0, 0, 9, 9]}
    return {"url": "http://x/a.html", "table_styles": [{**tb, **style}]}


def test_table_style_check_is_per_property_and_tolerates_nothing():
    spec = {"formats": {"html_web": {"table": {"header_background": "#333333", "border_color": "#DCDCDC",
                                               "border_width": 1, "cell_padding": 12, "radius": 1}}}}
    assert [r["status"] for r in site_nav._table_style_rows([_table_page()], spec)] == ["pass"]
    # one padding side off is still off: the spec is exact, and the row names every side
    off = site_nav._table_style_rows([_table_page(padding=[12, 16, 12, 12])], spec)
    assert len(off) == 1 and off[0]["item"] == "Table style — cell padding" and off[0]["actual"].startswith("12px/16px")
    # a table whose cells are <td> only has no header bar to judge - the rest is still checked
    body_only = site_nav._table_style_rows([_table_page(cell="td", header_background="#FFFFFF", radius=4)], spec)
    assert [r["item"] for r in body_only] == ["Table style — corner radius"]


def test_a_crawl_that_captures_one_page_says_so(tmp_path, serve):
    """"All pages of the guide" asked for, one page captured: the whole PDF then reads as missing
    content. The run must name the cause (no navigation, no guide links) instead of looking like the
    guide lost its text - the usual case is an AEM /editor.html/… URL, whose guide sits in an iframe."""
    pdf = make_site(tmp_path)
    (tmp_path / "guide" / "solo.html").write_text(
        "<!doctype html><meta charset=utf-8><title>Product overview</title>"
        "<main><h1>Product overview</h1><p>Text about specifications for the SX1000.</p></main>")
    r = compare_url(str(pdf), serve + "solo.html", str(tmp_path / "run"), load_config())
    warn = r["meta"].get("crawl_warning", "")
    assert "Only 1 page was captured" in warn and "3 chapters" in warn, warn


def test_a_real_crawl_does_not_warn(tmp_path, serve):
    """The same guard must stay quiet on a guide whose navigation the crawl does follow."""
    pdf = make_site(tmp_path)
    r = compare_url(str(pdf), serve + "overview.html", str(tmp_path / "run"), load_config())
    assert not r["meta"].get("crawl_warning")


AEM_BODY = """
<p id="lead" style="margin:0">When connecting a signal source to the projector, be sure to:</p>
<ol class="offmargin" style="list-style-position:inside;padding-left:24px;margin:0">
  <li>Turn all equipment off before making any connections.</li></ol>
<p style="margin:0">A/V devices, notebook or desktop computers</p>
<ol class="level" style="list-style-position:inside;padding-left:0;margin:0">
  <li>Use the correct signal cables for each source.</li></ol>
<p style="margin:0">See also</p>
<ul class="wide" style="padding-left:40px;margin:0"><li>Ensure the cables are firmly inserted.</li></ul>
<p><a id="guid" href="GUID-17f6ffdc-ff83-4a35-9a6e-5d5bc655db60-en.html">Connection</a>
   <a id="guidtext" href="install.html">GUID-17f6ffdc-ff83-4a35-9a6e-5d5bc655db60-en</a>
   <a id="dead" href="nope-404.html">Missing page</a></p>"""


def _aem_rows(tmp_path, serve):
    pdf = make_site(tmp_path, extra_body=AEM_BODY)
    r = compare_url(str(pdf), serve + "overview.html", str(tmp_path / "run"), load_config())
    return r, r["site"]["rows"]


def test_guid_links_and_list_margins_are_reported(tmp_path, serve):
    """The AEM site review's own checks: a link whose address or label still carries a DITA GUID, and a
    list whose markers do not start at the left edge of the paragraph above it."""
    r, rows = _aem_rows(tmp_path, serve)
    guid = [row for row in rows if row.get("kind") == "link-guid"]
    assert {row["where"].rsplit("#", 1)[-1] for row in guid} >= {"guid", "guidtext"}, [row["where"] for row in guid]
    assert all(row["status"] == "fail" and "17f6ffdc" in row["actual"] for row in guid)
    margins = {row["status"]: row for row in rows if row["item"].startswith("List")}
    off = [row for row in rows if row.get("kind") == "list-margin"]
    assert any("24px right of" in row["note"] for row in off), [row["note"] for row in off]   # padding-left:24px
    assert any(row["where"].endswith("ul.wide") or "ul" in row["where"] for row in off)       # default 40px padding
    assert any(row["status"] == "pass" and row["item"] == "List margin" for row in rows)      # the level one
    assert margins  # both statuses present


def test_a_dead_in_content_link_is_not_silently_passed(tmp_path, serve):
    """A content link to a page that 404s is a "link not working" row, with its kind tagged so the AEM
    report can pick it out."""
    _, rows = _aem_rows(tmp_path, serve)
    assert any(row.get("kind") == "link-broken" for row in rows) or \
        any(row["status"] == "fail" and "nope-404" in (row["note"] or "") for row in rows)


def test_the_aem_site_report_holds_only_the_four_issues(tmp_path, serve):
    """aem-site-issues.csv / .pdf: missing links, links that do not work, links carrying a GUID and
    lists out of the text margin - and nothing else from the run (no CSS, no pager, no images)."""
    import csv as _csv

    from pdfval.report import aem_site_report

    r, _ = _aem_rows(tmp_path, serve)
    out = tmp_path / "aem"
    out.mkdir()
    assert aem_site_report.build(r, out) is not None
    issues = list(_csv.DictReader((out / "aem-site-issues.csv").read_text(encoding="utf-8-sig").splitlines()))
    kinds = {row["Issue"] for row in issues}
    assert kinds <= {"Link missing", "Link not working", "Link contains a GUID", "List out of the text margin"}
    assert "Link contains a GUID" in kinds and "List out of the text margin" in kinds
    assert (out / "aem-site-issues.pdf").exists()
    # a run with none of the four leaves no empty report behind
    clean = {"meta": r["meta"], "sections": [], "site": {"rows": [], "summary": {}}}
    assert aem_site_report.build(clean, out) is None and not (out / "aem-site-issues.csv").exists()
