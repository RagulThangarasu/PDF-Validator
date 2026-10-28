"""The link check walks the whole left navigation of each guide, to the bottom of the tree.

A left navigation that only renders a branch's children while that branch is open hands the crawl one
level at a time: the children appear once their parent is the open topic. crawl_guide queues what each
page newly shows, so the guide is covered whole instead of stopping at the level the start page had.
"""
import pytest

from pdfval import linkcheck

BASE = "http://aem.test/content/guide/consumer/monitor/gr10/en"

# the guide: two top-level topics, one of which has children that only its own page lists, and one
# grandchild under those - plus a link to another product's guide, which is not this guide's topic
TREE = {
    f"{BASE}/intro.html": [("Intro", f"{BASE}/intro.html"), ("Setup", f"{BASE}/setup.html"),
                           ("Other product", "http://aem.test/content/guide/consumer/monitor/gw2291/en/intro.html")],
    f"{BASE}/setup.html": [("Intro", f"{BASE}/intro.html"), ("Setup", f"{BASE}/setup.html"),
                           ("Cables", f"{BASE}/setup/cables.html"), ("Stand", f"{BASE}/setup/stand.html")],
    f"{BASE}/setup/cables.html": [("Cables", f"{BASE}/setup/cables.html"), ("HDMI", f"{BASE}/setup/cables/hdmi.html")],
    f"{BASE}/setup/stand.html": [("Stand", f"{BASE}/setup/stand.html")],
    f"{BASE}/setup/cables/hdmi.html": [("HDMI", f"{BASE}/setup/cables/hdmi.html")],
}
ALL = set(TREE)


class FakePage:
    """A page whose left navigation shows what TREE says for the URL currently open."""

    def __init__(self, tree=None):
        self.tree = tree or TREE
        self.url = ""
        self.opened: list[str] = []

    def goto(self, url):
        self.opened.append(url)
        self.url = url

    def chrome(self) -> dict:
        items = [{"text": t, "href": h} for t, h in self.tree.get(self.url, [])]
        return {"title": "GR10", "h1": self.url.rsplit("/", 1)[-1],
                "nav": {"items": items},
                "content_links": [{"text": "a link", "href": f"{BASE}/target.html", "where": "main a"}]}


@pytest.fixture
def crawl(monkeypatch):
    """crawl_guide against FakePage: read_chrome and topic_guid come from the fake page."""
    page = FakePage()
    monkeypatch.setattr(linkcheck, "read_chrome", lambda p, scfg: p.chrome())
    monkeypatch.setattr(linkcheck, "topic_guid", lambda p: ("", ""))

    def go(start=f"{BASE}/intro.html", max_pages=0):
        page.goto(start)
        guide = {"start": start, "title": "", "pages": [], "error": ""}
        return page, linkcheck.crawl_guide(page, guide, start, {}, page.goto, max_pages)

    return page, go


def test_every_topic_of_the_tree_is_crawled_not_only_the_first_level(crawl):
    page, go = crawl
    _, guide = go()
    assert {p["url"] for p in guide["pages"]} == ALL
    assert len(guide["pages"]) == 5


def test_a_deeper_level_is_found_through_the_page_that_lists_it(crawl):
    """HDMI is in no navigation until Cables is open: it is still crawled."""
    page, go = crawl
    _, guide = go()
    assert f"{BASE}/setup/cables/hdmi.html" in {p["url"] for p in guide["pages"]}


def test_another_product_guide_is_not_crawled_as_this_guide(crawl):
    page, go = crawl
    _, guide = go()
    assert not any("gw2291" in p["url"] for p in guide["pages"])


def test_each_topic_is_opened_once(crawl):
    page, go = crawl
    _, guide = go()
    assert len(page.opened) == len(set(page.opened)) + 1  # +1: the start page, opened by the fixture


def test_the_links_in_every_topic_body_are_captured(crawl):
    page, go = crawl
    _, guide = go()
    assert all(p["links"] and p["links"][0]["href"] == f"{BASE}/target.html" for p in guide["pages"])


def test_the_guide_reports_how_much_of_its_navigation_was_crawled(crawl):
    page, go = crawl
    _, guide = go()
    assert (guide["nav_items"], guide["not_crawled"]) == (5, 0)


def test_a_limit_cuts_the_crawl_short_and_says_how_much_was_left(crawl):
    page, go = crawl
    _, guide = go(max_pages=2)
    assert len(guide["pages"]) == 2
    assert guide["not_crawled"] == guide["nav_items"] - 2 > 0


def test_a_guide_with_no_navigation_is_just_its_own_page(monkeypatch):
    page = FakePage({f"{BASE}/alone.html": []})
    monkeypatch.setattr(linkcheck, "read_chrome", lambda p, scfg: p.chrome())
    monkeypatch.setattr(linkcheck, "topic_guid", lambda p: ("", ""))
    page.goto(f"{BASE}/alone.html")
    guide = linkcheck.crawl_guide(page, {"start": f"{BASE}/alone.html", "title": "", "pages": [], "error": ""},
                                  f"{BASE}/alone.html", {}, page.goto)
    assert [p["url"] for p in guide["pages"]] == [f"{BASE}/alone.html"]


def test_a_topic_that_does_not_open_is_recorded_and_the_crawl_goes_on(crawl):
    page, go = crawl

    def refuse(url):
        if url.endswith("stand.html"):
            raise RuntimeError("HTTP 500")
        page.goto(url)

    page.goto(f"{BASE}/intro.html")
    guide = linkcheck.crawl_guide(page, {"start": f"{BASE}/intro.html", "title": "", "pages": [], "error": ""},
                                  f"{BASE}/intro.html", {}, refuse)
    broken = [p for p in guide["pages"] if p["error"]]
    assert len(broken) == 1 and "HTTP 500" in broken[0]["error"]
    assert len(guide["pages"]) == 5  # the other topics are still crawled
