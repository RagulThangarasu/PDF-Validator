"""A batch must not overload one server, and one unreachable page must not end a run."""
import threading
import time

import pytest

from pdfval import html_source
from pdfval.hostgate import host_slot


def test_host_slot_caps_simultaneous_loads(tmp_path, monkeypatch):
    monkeypatch.setattr("pdfval.hostgate._DIR", tmp_path / "gate")
    live, peak, lock = 0, 0, threading.Lock()

    def one():
        nonlocal live, peak
        with host_slot("aem.example", 3):
            with lock:
                live += 1
                peak = max(peak, live)
            time.sleep(0.05)
            with lock:
                live -= 1

    threads = [threading.Thread(target=one) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak <= 3


def test_host_slot_separates_hosts(tmp_path, monkeypatch):
    monkeypatch.setattr("pdfval.hostgate._DIR", tmp_path / "gate")
    with host_slot("a.example", 1):  # a busy host must not hold up a different one
        with host_slot("b.example", 1):
            pass


def test_host_slot_off_when_limit_is_zero(tmp_path, monkeypatch):
    monkeypatch.setattr("pdfval.hostgate._DIR", tmp_path / "gate")
    with host_slot("a.example", 0), host_slot("a.example", 0):
        pass


class _Page:
    """Just enough of a Playwright page: `fail` first navigations time out, then it answers."""

    def __init__(self, fail=0, status=200):
        self.fail, self.status, self.tries, self.url = fail, status, 0, "http://x/"

    def goto(self, url, **kw):
        self.tries += 1
        if self.tries <= self.fail:
            raise TimeoutError("Page.goto: Timeout 45000ms exceeded.")
        return type("R", (), {"status": self.status})()

    def wait_for_timeout(self, ms):
        pass


@pytest.fixture(autouse=True)
def _no_settle_no_gate(monkeypatch, tmp_path):
    monkeypatch.setattr(html_source, "_settle", lambda page, extra_ms=0: None)
    monkeypatch.setattr("pdfval.hostgate._DIR", tmp_path / "gate")


def test_a_timed_out_page_is_retried_and_succeeds():
    page = _Page(fail=2)
    assert html_source._goto_retry(page, "http://x/a", 1000).status == 200
    assert page.tries == 3


def test_a_page_that_never_answers_raises_after_its_tries():
    page = _Page(fail=99)
    with pytest.raises(TimeoutError):
        html_source._goto_retry(page, "http://x/a", 1000)
    assert page.tries == 3


def test_a_server_error_is_retried():
    page = _Page(status=503)
    assert html_source._goto_retry(page, "http://x/a", 1000).status == 503
    assert page.tries == 3  # tried again rather than skipped on the first 503


def test_an_unreachable_page_is_one_skipped_page_not_a_failed_run(monkeypatch):
    monkeypatch.setattr(html_source, "_login_form", lambda p: False)
    out = html_source._visit_in_tab(_Page(fail=99), "http://x/a", "", "", 0, 1800, 1000, None)
    assert "timed out" in out["skipped"]  # returned, not raised
