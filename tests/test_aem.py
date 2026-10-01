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


def test_guid_opens_the_product_map_with_all_topics():
    """<product folder>/Maps/<map file named in the stage PDF>, opened in the editor."""
    cfg = {"author": "http://aem:4502", "maps_folder": "Maps", "link": "{author}/e?src={path}",
           "map_link": "{author}/e?src={path}&appMode=author",
           "products": {"sl04_and_sh04": "/content/dam/g/en/Education/Signage/SL04-and-SH04"}}
    product, folder = aem.product_of("sl04_and_sh04.ditamap", cfg)
    a = {"guid": T1, "lang": "en", "map": "sl04_and_sh04.ditamap", "product": product, "folder": folder}
    assert aem.url_for(a, cfg) == "http://aem:4502/e?src=/content/dam/g/en/Education/Signage/SL04-and-SH04/Maps/sl04_and_sh04.ditamap&appMode=author"
    found = {**a, "path": "/content/dam/g/en/Education/Signage/SL04-and-SH04/Topics/Product overview.dita"}
    assert aem.url_for(found, cfg, "topic").endswith("/Topics/Product%20overview.dita")
    assert aem.url_for(found, cfg).endswith("/Topics/Product%20overview.dita")  # default: the topic once it is found
    assert aem.url_for(found, cfg, "map").endswith("/Maps/sl04_and_sh04.ditamap&appMode=author")
    assert aem.url_for({**a, "folder": ""}, cfg).startswith("http://aem:4502/libs/fmdita")  # no product folder: Explorer


def test_generate_pdf_starts_the_preset_and_downloads_the_new_output(tmp_path):
    """A fake AEM: the preset's id is looked up by its title, the generation request names the map and
    that id in the query string (a form body gets “400 Request Data has already been read”); the
    query then lists the new PDF, which is downloaded."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse

    seen = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            self.rfile.read(n)
            u = urlparse(self.path)
            seen["post"] = (u.path, parse_qs(u.query), self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/bin/querybuilder.json" and "folderprofiles" in u.query:
                body = json.dumps({"hits": [{"jcr:path": "/var/dxml/folderprofiles/p/presets/fd4ac90a",
                                             "fmdita-outputTitle": "BenQ EDU With Image", "fmdita-outputType": "pdf"}]}).encode()
            elif u.path == "/bin/querybuilder.json":
                body = json.dumps({"hits": [{"jcr:path": "/content/dam/out/w2720i.pdf"}]}).encode()
            else:
                seen["get"] = u.path
                body = b"%PDF-1.7 fake"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cfg = {"author": f"http://127.0.0.1:{srv.server_address[1]}", "user": "u", "password": "p",
           "generate": {"rules": [["/Education/", "BenQ EDU with images"]], "poll_s": 0.05, "timeout_s": 5}}
    mp = "/content/dam/g/en/Education/Signage/w2720i/Maps/w2720i.ditamap"
    preset = aem.preset_for(mp, cfg)
    assert preset == "BenQ EDU with images"
    out = aem.generate_pdf(mp, preset, str(tmp_path / "stage.pdf"), cfg)
    srv.shutdown()
    path, form, auth = seen["post"]
    assert path == "/bin/publishlistener" and form["source"] == [mp] and form["outputName"] == ["fd4ac90a"]
    assert form["operation"] == ["GENERATEOUTPUT"] and auth.startswith("Basic ")
    assert seen["get"] == "/content/dam/out/w2720i.pdf" and open(out, "rb").read().startswith(b"%PDF")


def test_map_is_found_from_the_prod_pdf_file_name():
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            q = parse_qs(urlparse(self.path).query)
            hits = [{"jcr:path": "/content/dam/g/en/Consumer/Projector/w2720i/Maps/w2720i.ditamap"}] \
                if q.get("nodename") == ["w2720i.ditamap"] else []
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"hits": hits}).encode())

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cfg = {"author": f"http://127.0.0.1:{srv.server_address[1]}", "user": "u", "password": "p"}
    assert aem.map_candidates("aeedd66f_W2720i_V1.03_EN.pdf") == ["w2720i"]
    assert aem.find_map_for("aeedd66f_W2720i_V1.03_EN.pdf", cfg).endswith("/w2720i/Maps/w2720i.ditamap")
    assert aem.preset_for("/content/dam/g/en/Consumer/Projector/w2720i/Maps/w2720i.ditamap", {}) == "BenQ with images"
    srv.shutdown()


def test_map_found_whatever_its_case_or_language_suffix():
    """“EW270Q-en.ditamap” is the map ew270q.ditamap: case, a language suffix and punctuation do not
    matter; the search-root / English copy wins over other DAM folders."""
    maps = ["/content/dam/hashout/x/en/Monitor/ew270q/Maps/ew270q.ditamap",
            "/content/dam/benq-aem-guides/ar-me/Monitor/ew270q/Maps/ew270q.ditamap",
            "/content/dam/benq-aem-guides/en/Consumer/Monitor/ew270q/Maps/ew270q.ditamap",
            "/content/dam/benq-aem-guides/en/Education/Signage/SL04-and-SH04/Maps/sl04_and_sh04.ditamap"]
    root = "/content/dam/benq-aem-guides"
    assert aem.map_key("EW270Q-en.ditamap") == aem.map_key("ew270q.ditamap") == "ew270q"
    assert aem.best_map("EW270Q-en.ditamap", maps, root) == maps[2]
    assert aem.best_map("SL04_and_SH04_EN", maps, root) == maps[3]
    assert aem.best_map("nothing-here", maps, root) == ""
