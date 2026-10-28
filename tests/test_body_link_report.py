"""The body hyperlink report: only the links inside the text of the page, broken ones and ones whose
address (or the address they redirect to) still carries a DITA GUID."""
import pymupdf

from pdfval import site_nav
from pdfval.report import body_link_report

GUID = "GUID-17f6ffdc-ff83-4a35-9a6e-5d5bc655db60"


def _links() -> dict:
    """What the crawl found behind each address: a 404, a redirect onto the raw GUID page, a page
    whose #anchor is not there, and a page that is simply fine."""
    return {
        site_nav.key("https://x.test/dead.html"): {"status": 404, "url": "https://x.test/dead.html", "title": "", "ids": []},
        site_nav.key("https://x.test/redirect.html"): {"status": 200, "url": f"https://x.test/{GUID}-en.html",
                                                       "title": "B", "h1": "B", "ids": []},
        site_nav.key("https://x.test/anchor.html#sec"): {"status": 200, "url": "https://x.test/anchor.html",
                                                         "title": "C", "h1": "C", "ids": ["other"]},
        site_nav.key("https://x.test/fine.html"): {"status": 200, "url": "https://x.test/fine.html",
                                                   "title": "D", "h1": "D", "ids": []},
    }


def _row(text: str, href: str, body: bool = True) -> dict:
    return site_nav._link_row("links", text, href, _links(), "https://x.test/p1.html", check_title=False, body=body)


def _result(rows: list[dict]) -> dict:
    return {"meta": {"generated": "now", "candidate": {"url": "https://x.test/p1.html"},
                     "baseline": {"path": "/tmp/prod.pdf"}},
            "site": {"rows": rows, "summary": {"pages": 1}}, "sections": []}


def test_a_body_link_that_does_not_open_is_broken():
    r = _row("dead one", "https://x.test/dead.html")
    assert (r["status"], r["kind"], r["body"]) == ("fail", "link-broken", True)


def test_a_body_link_whose_anchor_is_missing_is_broken():
    r = _row("bad anchor", "https://x.test/anchor.html#sec")
    assert (r["status"], r["kind"]) == ("fail", "link-broken")


def test_a_link_that_redirects_onto_the_guid_page_is_reported():
    """The link opens - HTTP 200 - but the reader lands on the raw GUID address, so it is still an issue."""
    r = _row("redirects", "https://x.test/redirect.html")
    assert (r["status"], r["kind"]) == ("fail", "link-guid-redirect")
    assert r["actual"] == GUID
    assert "redirects to a GUID address" in r["note"]


def test_a_working_body_link_passes():
    r = _row("fine", "https://x.test/fine.html")
    assert (r["status"], r["kind"]) == ("pass", "link-ok")


def test_the_report_leaves_the_chrome_out():
    """A broken left-navigation link is the site report's business: this report is the page body only."""
    rows = [_row("dead one", "https://x.test/dead.html"),
            _row("nav link", "https://x.test/dead.html", body=False)]
    assert [r["item"] for r in body_link_report.rows(_result(rows))] == ["dead one"]


def test_a_guid_in_the_address_or_label_is_a_body_link_issue_but_the_navs_is_not():
    rows = site_nav._guid_link_rows([{
        "url": "https://x.test/p1.html",
        "content_links": [{"text": f"see {GUID}", "href": "https://x.test/e.html", "where": "main a"}],
        "nav": {"items": [{"text": "nav", "href": f"https://x.test/{GUID}-en.html", "where": "nav a"}]},
    }])
    assert [r.get("body", False) for r in rows] == [True, False]
    kept = body_link_report.rows(_result(rows))
    assert [r["group"] for r in kept] == ["body-link-guid"]


def test_summary_counts_the_body_links_only():
    rows = [_row("dead one", "https://x.test/dead.html"),
            _row("redirects", "https://x.test/redirect.html"),
            _row("fine", "https://x.test/fine.html"),
            _row("nav link", "https://x.test/dead.html", body=False)]
    sm = body_link_report.summary(_result(rows))
    assert sm == {"links": 3, "fail": 2, "warn": 0, "pass": 1, "broken": 1, "guid": 1, "status": "fail"}


def test_no_body_link_at_all_means_no_summary_and_no_report(tmp_path):
    rows = [_row("nav link", "https://x.test/dead.html", body=False)]
    assert body_link_report.summary(_result(rows)) is None
    assert body_link_report.build(_result(rows), tmp_path) is None
    assert not (tmp_path / "body-link-issues.pdf").exists()
    assert not (tmp_path / "body-link-issues.csv").exists()


def test_the_report_is_written_as_pdf_and_csv(tmp_path):
    rows = [_row("dead one", "https://x.test/dead.html"),
            _row("redirects", "https://x.test/redirect.html"),
            _row("fine", "https://x.test/fine.html"),
            _row("nav link", "https://x.test/dead.html", body=False)]
    out = body_link_report.build(_result(rows), tmp_path)
    assert out and out.exists()
    with pymupdf.open(out) as doc:
        text = "\n".join(pg.get_text() for pg in doc)
    assert "Body hyperlinks" in text
    assert "Broken link" in text and "Link redirects to a GUID address" in text
    assert "nav link" not in text  # the chrome stays out of this report
    csv_text = (tmp_path / "body-link-issues.csv").read_text(encoding="utf-8-sig")
    assert csv_text.splitlines()[0].startswith("Bug ID,Issue,Status,Link text")
    assert "dead one" in csv_text and GUID in csv_text
