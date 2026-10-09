"""Site navigation of a web guide (pdfval/site_nav.py): left navigation vs the PDF's L1 TOC,
navigation links, Download PDF, next / previous topic, On this page, product subtitle."""
import functools
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
<main style="width:700px"><nav class="breadcrumb" style="margin:0 0 {breadcrumb_gap}px 0"><a href="overview.html">Home</a> &gt; 
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


def test_otp_hidden_when_no_h3_or_h4(tmp_path, serve):
    """A page whose sub-headings are h2 (no h3/h4): no otp row at all when no "On this page" list is shown,
    and a warn when one is shown anyway (nothing to list)."""
    pdf = make_site(tmp_path)
    page = tmp_path / "guide" / "overview.html"
    page.write_text(page.read_text().replace("<h3 ", "<h2 ").replace("</h3>", "</h2>"))
    r = compare_url(str(pdf), serve + "overview.html", str(tmp_path / "run"), load_config())
    rows = [row for row in r["site"]["rows"] if row["group"] == "otp" and "overview" in row["page"]]
    assert any(row["status"] == "warn" and "hidden" in row["expected"] for row in rows)


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
