"""Site navigation of a web guide (pdfval/site_nav.py): left navigation vs the PDF's L1 TOC,
navigation links, Download PDF, next / previous topic, On this page, product subtitle."""
import functools
import http.server
import threading

import pymupdf
import pytest

from pdfval import load_config
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


def make_site(root, *, drop_nav=None, wrong_next=None, broken_anchor=None, subtitle=None):
    """One page per chapter in root/guide, with the chrome of the BenQ guide."""
    site = root / "guide"
    site.mkdir()
    make_pdf(site / "manual.pdf")
    nav = "".join(f'<li><a href="{s}.html">{t}</a></li>' for s, t, _ in CHAPTERS if t != drop_nav)
    for i, (slug, title, subs) in enumerate(CHAPTERS):
        otp = "".join(f'<li><a href="#{"nope" if h == broken_anchor else f"s{k}"}">{h}</a></li>' for k, h in enumerate(subs))
        body = "".join(f'<h2 id="s{k}">{h}</h2><p>Text about {h.lower()} for the SX1000.</p>' for k, h in enumerate(subs))
        nxt = CHAPTERS[i + 1] if i + 1 < len(CHAPTERS) else None
        if nxt and slug == wrong_next:
            nxt = CHAPTERS[0]
        prv = CHAPTERS[i - 1] if i else None
        pager = (f'<div><a href="{prv[0]}.html">← PREVIOUS TOPIC</a><div>{prv[1]}</div></div>' if prv else "") + \
                (f'<div><a href="{nxt[0]}.html">NEXT TOPIC →</a><div>{nxt[1]}</div></div>' if nxt else "")
        (site / f"{slug}.html").write_text(f"""<!doctype html><html><head><meta charset="utf-8"><title>{title}</title></head>
<body style="margin:0"><header><a href="/"><svg width="30" height="20"></svg><span>Guide</span></a>
<div><span class="product-title" style="font-weight:700">{subtitle or 'SX1000 Series'}</span>
<a href="manual.pdf">Download PDF</a></div></header>
<div style="display:flex"><nav style="width:220px"><p>Table of Contents</p><ul>
{nav.replace(f'href="{slug}.html"', f'href="{slug}.html" aria-current="page"')}</ul></nav>
<main style="width:700px"><h1>{title}</h1>{body}<div class="pager">{pager}</div></main>
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
    r = compare_url(str(pdf), base + "overview.html", str(tmp_path / "run"), load_config())
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
