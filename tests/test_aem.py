"""AEM Guides topic tracing: position -> GUID, product folder, link template."""
from pathlib import Path

import pytest

from pdfval import aem

T1, T2 = "GUID-17f6ffdc-ff83-4a35-9a6e-5d5bc655db60", "GUID-3029b721-1b3f-4a44-bf44-5138de7b30c8"
STAGE = Path(__file__).resolve().parents[2] / "stage" / "SH04 & SL04 User Manual (12).pdf"


def test_locator_picks_topic_and_nearest_element():
    loc = aem.Locator([aem.Anchor(4, 22, T1, "en", "", "Product overview"),
                       aem.Anchor(4, 86, T1, "en", "section_1", "Specifications"),
                       aem.Anchor(19, 22, T2, "en", "", "Installation")])
    topic, near = loc.at(4, 300)
    assert (topic.guid, near.element) == (T1, "section_1")
    topic, near = loc.at(18, 700)  # last page of the first topic
    assert topic.guid == T1
    assert loc.at(19, 30)[0].guid == T2
    assert loc.at(2, 100) is None  # cover / TOC: before the first topic


def test_link_opens_the_looked_up_file_never_a_guessed_one():
    """The topic file name is not derived from the GUID: without a path looked up in AEM the link
    opens the editor (Explorer), never a guessed <folder>/<GUID>.dita that does not exist (404)."""
    cfg = {"link": "{author}/editor?src={path}", "fallback_link": "{author}/editor?leftPanel=repository_panel",
           "dam_root": "/content/dam/g/en", "products": {"sl04_and_sh04": "/content/dam/g/en/sl04"}}
    product, folder = aem.product_of("sl04_and_sh04.ditamap", cfg)
    assert (product, folder) == ("sl04_and_sh04", "/content/dam/g/en/sl04")
    a = {"guid": T1, "lang": "en", "product": product, "folder": folder}
    assert aem.url_for(a, cfg) == ""  # no author URL -> no link
    cfg["author"] = "https://author/"
    assert aem.url_for(a, cfg) == "https://author/editor?leftPanel=repository_panel"
    assert ".dita" not in aem.url_for(a, cfg)
    found = {**a, "path": "/content/dam/g/en/board/Product overview.dita"}
    assert aem.url_for(found, cfg) == "https://author/editor?src=/content/dam/g/en/board/Product%20overview.dita"


def test_login_check_reports_a_refused_login(monkeypatch):
    import io
    import json
    from urllib.error import HTTPError

    def fake(req, timeout=0):
        if req.get_header("Authorization") == "Basic dTpw":  # u:p
            return io.BytesIO(json.dumps({"authorizableId": "u"}).encode())
        raise HTTPError(req.full_url, 401, "no", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake)
    cfg = {"author": "http://aem:4502", "user": "u"}
    assert aem.check_login({**cfg, "password": "p"}) == (True, "Logged in to AEM as u")
    assert aem.check_login({**cfg, "password": "x"})[0] is False
    assert aem.check_login({**cfg, "password": ""}) == (False, "Enter the AEM user and password")


@pytest.mark.skipif(not STAGE.exists(), reason="sample stage PDF not present")
def test_reads_guid_destinations_from_aem_pdf():
    items, info = aem.anchors(str(STAGE))
    assert info["map"] == "sl04_and_sh04.ditamap"
    topics = [a for a in items if not a.element]
    assert len(topics) == 11 and topics[0].title == "Product overview"
    assert all(a.title != "•" for a in items)


def test_resolve_asks_aem_for_the_topic_path(monkeypatch):
    """The DAM path comes from AEM's QueryBuilder; the hit in the topic's language folder wins."""
    import io
    import json
    from urllib.error import HTTPError

    seen = []

    def fake_urlopen(req, timeout=0):
        seen.append(req.full_url)
        if req.get_header("Authorization") != "Basic dTpw":  # u:p
            raise HTTPError(req.full_url, 401, "no", {}, None)
        hits = [{"jcr:path": f"/content/dam/g/ar-me/sl04/{T1}.dita"}, {"jcr:path": f"/content/dam/g/en/board/sl04/{T1}.dita"}]
        return io.BytesIO(json.dumps({"hits": hits}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    aem._PATHS.clear()
    cfg = {"author": "http://aem:4502", "user": "u", "password": "p", "search_root": "/content/dam/g",
           "link": "{author}/editor?src={path}&appMode=author"}
    paths, err = aem.resolve([(T1, "en")], cfg)
    assert err == "" and paths[T1] == f"/content/dam/g/en/board/sl04/{T1}.dita"
    assert "bin/querybuilder.json" in seen[0] and T1 in seen[0]
    assert aem.url_for({"guid": T1, "lang": "en", "path": paths[T1]}, cfg) == \
        f"http://aem:4502/editor?src=/content/dam/g/en/board/sl04/{T1}.dita&appMode=author"
    aem._PATHS.clear()
    assert aem.resolve([(T1, "en")], {**cfg, "password": "bad"}) == ({}, "AEM refused the login (user name or password wrong)")
    assert aem.resolve([(T1, "en")], {**cfg, "password": ""}) == ({}, "")  # no login: nothing asked
