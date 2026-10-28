"""link-check-failures.pdf carries the failing links and nothing else.

A guide whose every link opens, a topic whose every link opens, and a passing link inside a topic that
has other failures must all be absent - the report is the list to work through, not the whole run.
"""
import pymupdf
import pytest

from pdfval import linkcheck
from pdfval.report import link_report

GUID = "GUID-9d9e6dbc-df62-4e86-bd8d-459d4bbae10d"


def _link(text, href, status="pass", kind="opens", note="HTTP 200"):
    return {"text": text, "href": href, "status": status, "kind": kind, "note": note}


def _topic(name, links, error=""):
    return {"url": f"http://aem.test/{name}.html", "nav_text": name, "title": name, "guid": "",
            "guid_from": "", "error": error, "links": links}


@pytest.fixture
def result():
    bad = _link("Product support", f"http://aem.test/p.html#{GUID}", "fail", "GUID in the link",
                f"The link's address still carries the topic GUID {GUID}")
    clean_guide = {"start": "http://aem.test/clean/en/intro.html", "title": "Clean guide", "error": "",
                   "nav_items": 2, "not_crawled": 0,
                   "pages": [_topic("CleanTopicA", [_link("Fine one", "http://aem.test/ok.html")]),
                             _topic("CleanTopicB", [_link("Also fine", "http://aem.test/ok2.html")])]}
    mixed_guide = {"start": "http://aem.test/gv32/en/intro.html", "title": "Mixed guide", "error": "",
                   "nav_items": 3, "not_crawled": 0,
                   "pages": [_topic("PassingTopic", [_link("Passing link", "http://aem.test/ok3.html")]),
                             # the same failing link four times over, plus one that opens
                             _topic("FailingTopic", [bad, dict(bad), dict(bad), dict(bad),
                                                     _link("Quiet passer", "http://aem.test/ok4.html")])]}
    guides = [clean_guide, mixed_guide]
    return {"guides": guides, "totals": linkcheck.totals(guides)}


def _text(path) -> str:
    """The PDF's text as one line: the report wraps cells over several lines ("Product\nsupport", a GUID
    broken mid-word), so what is asserted on is the text with its line breaks and hyphenation taken out."""
    import re
    with pymupdf.open(path) as doc:
        raw = "\n".join(pg.get_text() for pg in doc)
    return re.sub(r"\s+", " ", raw.replace("-\n", "-"))


@pytest.fixture
def reports(result, tmp_path):
    full = link_report.build_pdf(result, tmp_path / "all.pdf")
    fails = link_report.build_pdf(result, tmp_path / "fails.pdf", failures_only=True)
    return _text(full), _text(fails)


def test_a_guide_with_no_failure_is_not_in_the_failures_report(reports):
    full, fails = reports
    assert "Clean guide" in full
    assert "Clean guide" not in fails


def test_a_topic_with_no_failure_is_not_in_the_failures_report(reports):
    full, fails = reports
    assert "PassingTopic" in full
    assert "PassingTopic" not in fails


def test_a_passing_link_inside_a_failing_topic_is_left_out(reports):
    full, fails = reports
    assert "Quiet passer" in full
    assert "FailingTopic" in fails and "Quiet passer" not in fails


def test_the_failing_link_is_there(reports):
    full, fails = reports
    assert "Product support" in fails and GUID in fails


def test_the_failures_report_never_says_a_topic_is_fine(reports):
    full, fails = reports
    assert "Every link in this topic opens" not in fails
    assert "all fine" not in fails


def test_the_same_failing_link_repeated_is_one_row_with_its_count(reports):
    full, fails = reports
    assert fails.count("Product support") == 1
    assert "× 4" in fails


def test_the_failures_report_says_what_it_is(reports):
    full, fails = reports
    assert "failures only" in fails.lower()


def test_a_topic_that_did_not_open_is_a_failure_and_stays_in(tmp_path):
    guides = [{"start": "http://aem.test/g/en/intro.html", "title": "Broken guide", "error": "",
               "nav_items": 1, "not_crawled": 0,
               "pages": [_topic("DeadTopic", [], error="The topic did not open: HTTP 500")]}]
    out = link_report.build_pdf({"guides": guides, "totals": linkcheck.totals(guides)},
                                tmp_path / "f.pdf", failures_only=True)
    text = _text(out)
    assert "DeadTopic" in text and "HTTP 500" in text
    assert "No links in this topic" not in text
