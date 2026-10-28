"""The link check's AEM sign-in, and what it reports about a link without opening its page.

AEM author answers a request it has no session for with its own sign-in page - HTTP 200, not 401 - so
the crawl has to fill that form in. Before this, every topic of every guide came back as the login page
itself ("AEM Sign In", with the login page's own Adobe links), which reads like a result but says
nothing about the guide.
"""
import pytest

from pdfval import linkcheck

GUID = "GUID-17f6ffdc-ff83-4a35-9a6e-5d5bc655db60"


class FakePage:
    """A page that shows the sign-in form until the form is submitted (what AEM author does)."""

    def __init__(self, *, needs_login: bool = True, accepts: bool = True):
        self.needs_login, self.accepts = needs_login, accepts
        self.url = ""
        self.opened: list[str] = []
        self.signed_in = False

    def goto(self, url, **kw):
        self.opened.append(url)
        self.url = url

    def shows_login(self) -> bool:
        return self.needs_login and not self.signed_in

    def sign_in(self):
        if self.accepts:
            self.signed_in = True


@pytest.fixture
def patched(monkeypatch):
    """_login_form / _sign_in / _settle as the crawl calls them, against FakePage."""
    monkeypatch.setattr(linkcheck, "_login_form", lambda page: page.shows_login())
    monkeypatch.setattr(linkcheck, "_sign_in", lambda page, u, p, t, s: page.sign_in())
    monkeypatch.setattr(linkcheck, "_settle", lambda page, ms: None)


def _open_page(page, user="", password="", wait_ms=0):
    """The crawl's own open_page (pdfval/linkcheck.py), the one run() calls for every topic."""
    return linkcheck.open_page(page, "http://aem.test/guide/en/copyright.html", user, password, wait_ms)


def test_the_sign_in_form_is_filled_in_and_the_page_asked_for_again(patched):
    page = FakePage()
    assert _open_page(page, "ragul", "secret") is True
    # the page that was wanted is opened again after the sign-in, not left on wherever AEM landed
    assert page.opened == ["http://aem.test/guide/en/copyright.html"] * 2
    assert page.signed_in


def test_a_page_that_needs_no_login_is_read_straight_away(patched):
    page = FakePage(needs_login=False)
    assert _open_page(page, "ragul", "secret") is False
    assert page.opened == ["http://aem.test/guide/en/copyright.html"]
    assert not page.signed_in


def test_without_a_password_the_run_stops_instead_of_crawling_the_login_page(patched):
    page = FakePage()
    with pytest.raises(linkcheck.SignInError, match="answers with a sign-in page"):
        _open_page(page, "ragul", "")


def test_a_rejected_login_stops_the_run_with_the_user_name_in_the_message(patched):
    page = FakePage(accepts=False)
    with pytest.raises(linkcheck.SignInError, match="Sign-in failed"):
        _open_page(page, "ragul", "wrong")


def test_a_guid_in_the_address_is_reported_from_the_captured_url_alone():
    """No status at all (the address was never asked anything): the GUID is still reported."""
    got = linkcheck.classify("Specifications", f"http://aem.test/{GUID}-en.html", None)
    assert (got["status"], got["kind"]) == ("fail", "GUID in the link")
    assert GUID in got["note"]


def test_a_guid_in_the_label_is_reported_too():
    got = linkcheck.classify(f"see {GUID}", "http://aem.test/specs.html", None)
    assert got["kind"] == "GUID in the link"
    assert "label" in got["note"]


def test_a_guid_link_that_also_fails_to_open_is_reported_as_the_guid_issue():
    got = linkcheck.classify("Specs", f"http://aem.test/{GUID}-en.html",
                             {"status": 404, "url": f"http://aem.test/{GUID}-en.html"})
    assert got["kind"] == "GUID in the link"


def test_a_plain_link_that_does_not_open_is_still_broken():
    got = linkcheck.classify("Learn More", "http://x.test/dead.html", {"status": 404, "url": "http://x.test/dead.html"})
    assert (got["status"], got["kind"]) == ("fail", "broken")


def test_a_link_that_lands_on_a_guid_address_is_reported_as_a_redirect():
    got = linkcheck.classify("Specs", "http://aem.test/specs.html",
                             {"status": 200, "url": f"http://aem.test/{GUID}-en.html"})
    assert got["kind"] == "redirects to a GUID address"


# ---- the link's page is asked for its status, not opened and crawled

def _serve(handler_cls):
    """A local HTTP server in a thread; yields its base URL."""
    import threading
    from http.server import HTTPServer
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


@pytest.fixture
def server():
    from http.server import BaseHTTPRequestHandler
    seen: list[tuple[str, str]] = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body: bytes = b""):
            seen.append((self.command, self.path))
            code = 404 if self.path.startswith("/dead") else 200
            self.send_response(code)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_HEAD(self):
            self._send()

        def do_GET(self):
            self._send(b"<html><h1>Page</h1><p id='sec'>x</p></html>")

    srv, base = _serve(H)
    yield base, seen
    srv.shutdown()


def test_a_plain_link_is_only_asked_for_its_status_never_downloaded(server):
    base, seen = server
    got = linkcheck._probe([f"{base}/page.html"], "")
    assert [m for m, _ in seen] == ["HEAD"]
    assert got[linkcheck.key(f"{base}/page.html")]["status"] == 200


def test_a_link_to_an_anchor_reads_the_page_so_the_anchor_can_be_checked(server):
    base, seen = server
    got = linkcheck._probe([f"{base}/page.html#sec"], "")
    assert [m for m, _ in seen] == ["GET"]
    assert "sec" in got[linkcheck.key(f"{base}/page.html")]["ids"]


def test_one_request_per_page_even_when_several_links_point_at_it(server):
    base, seen = server
    urls = [f"{base}/page.html", f"{base}/page.html#sec", f"{base}/page.html?x=1"]
    got = linkcheck._probe(urls, "")
    # one page, one request - and it is the GET, because a link to it names an anchor
    assert [m for m, _ in seen] == ["GET"]
    assert linkcheck.classify("Jump", f"{base}/page.html#sec", got[linkcheck.key(urls[1])])["status"] == "pass"


def test_a_page_that_answers_404_is_broken(server):
    base, seen = server
    got = linkcheck._probe([f"{base}/dead.html"], "")
    assert linkcheck.classify("Dead", f"{base}/dead.html", got[linkcheck.key(f"{base}/dead.html")])["kind"] == "broken"
