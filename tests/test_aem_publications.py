"""Listing the publications under a folder of AEM Sites: the map-level parent page of each one.

A published AEM Guides topic records the map it came from (basePath) and the publication's parent page
(indexPath), so grouping the topics under the folder by indexPath is AEM's own answer to which
publications live there - at whatever depth they sit.
"""
import io
import json

import pytest

from pdfval import aem

CFG = {"author": "http://aem.test:4502", "user": "ragul", "password": "secret"}
ROOT = "http://aem.test:4502/sites.html/content/guide/rs-joshi/en"

HITS = [
    {"basePath": "/content/dam/g/rs/pdp.ditamap", "indexPath": "/content/guide/rs-joshi/en/pdp-rs-classa"},
    {"basePath": "/content/dam/g/rs/pdp.ditamap", "indexPath": "/content/guide/rs-joshi/en/pdp-rs-classa"},
    {"basePath": "/content/dam/g/rs/ne001.ditamap", "indexPath": "/content/guide/rs-joshi/en/ne001"},
    {"basePath": "/content/dam/g/rs/ne001-v2.ditamap", "indexPath": "/content/guide/rs-joshi/en/ne001"},
    {"basePath": "", "indexPath": "/content/guide/rs-joshi/en/ignored"},      # not published from a map
    {"basePath": "/content/dam/g/other.ditamap", "indexPath": ""},            # no parent recorded
]
CHILDREN = {"/content/guide/rs-joshi/en/pdp-rs-classa": {"introduction": {"jcr:primaryType": "cq:Page"},
                                                         "jcr:content": {"jcr:primaryType": "cq:PageContent"}},
            "/content/guide/rs-joshi/en/ne001": {}}


@pytest.fixture
def aem_server(monkeypatch):
    """urlopen against a stand-in AEM: the QueryBuilder listing, and each page's children."""
    asked = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        url = req.full_url
        asked.append(url)
        assert req.headers.get("Authorization", "").startswith("Basic "), "the query must be authenticated"
        if "querybuilder.json" in url:
            return Resp(json.dumps({"hits": HITS}).encode())
        path = url.split(":4502", 1)[1].replace(".1.json", "")
        return Resp(json.dumps(CHILDREN.get(path, {})).encode())

    # publications() imports urllib.request inside itself, so patching it there reaches the call
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return asked


def test_every_publication_under_the_folder_is_listed_once(aem_server):
    got = aem.publications(ROOT, CFG)
    assert [p["site"] for p in got["publications"]] == [
        "/content/guide/rs-joshi/en/ne001", "/content/guide/rs-joshi/en/pdp-rs-classa"]
    assert got["count"] == 2


def test_the_sites_console_prefix_is_not_part_of_the_path_queried(aem_server):
    asked = aem_server
    aem.publications(ROOT, CFG)
    assert "path=%2Fcontent%2Fguide%2Frs-joshi%2Fen" in asked[0]
    assert "sites.html" not in asked[0]


def test_each_publication_carries_its_map_and_its_topic_count(aem_server):
    pubs = {p["site"]: p for p in aem.publications(ROOT, CFG)["publications"]}
    pdp = pubs["/content/guide/rs-joshi/en/pdp-rs-classa"]
    assert pdp["map"] == "/content/dam/g/rs/pdp.ditamap" and pdp["pages"] == 2
    assert pubs["/content/guide/rs-joshi/en/ne001"]["maps"] == [
        "/content/dam/g/rs/ne001-v2.ditamap", "/content/dam/g/rs/ne001.ditamap"]


def test_the_crawl_starts_at_the_first_topic_when_the_parent_is_a_landing_page(aem_server):
    pubs = {p["site"]: p for p in aem.publications(ROOT, CFG)["publications"]}
    assert pubs["/content/guide/rs-joshi/en/pdp-rs-classa"]["start"] == \
        "http://aem.test:4502/content/guide/rs-joshi/en/pdp-rs-classa/introduction.html"


def test_a_publication_with_no_child_page_starts_at_its_own_page(aem_server):
    pubs = {p["site"]: p for p in aem.publications(ROOT, CFG)["publications"]}
    assert pubs["/content/guide/rs-joshi/en/ne001"]["start"] == \
        "http://aem.test:4502/content/guide/rs-joshi/en/ne001.html"


def test_a_topic_with_no_map_or_no_parent_is_not_a_publication(aem_server):
    sites = [p["site"] for p in aem.publications(ROOT, CFG)["publications"]]
    assert "/content/guide/rs-joshi/en/ignored" not in sites


def test_the_language_can_be_filtered_on(aem_server):
    assert aem.publications(ROOT, CFG, lang="en")["count"] == 0  # these parents end in the product, not "en"
    assert aem.publications(ROOT, CFG, lang="")["count"] == 2


def test_without_a_login_it_says_so(aem_server):
    with pytest.raises(RuntimeError, match="Log in to AEM first"):
        aem.publications(ROOT, {"author": "http://aem.test:4502"})


def test_the_children_lookup_can_be_skipped(aem_server):
    asked = aem_server
    aem.publications(ROOT, CFG, with_start=False)
    assert len(asked) == 1  # the QueryBuilder query alone
