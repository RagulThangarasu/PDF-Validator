"""A background run that is gone must not keep the page locked.

The Links page refuses a second run while one is going ("A comparison is already running"). The flag
lived in memory with nothing watching the worker, so a thread that never reached its `finally` - the
server interrupted, the thread never started - left the flag set for the life of the process and every
later run was refused although nothing was running. Each runner now reads the flag together with its
worker: no live worker, no run. See pdfval/app/server.py::SiteDiffer._unstick.
"""
import threading

import pytest

from pdfval.app import server


class _Jobs:
    def __init__(self, tmp):
        self.dir = tmp

    def aem_config(self):
        return {}


@pytest.fixture
def jobs(tmp_path):
    return _Jobs(tmp_path)


def _stuck(runner):
    """What the process looks like after a run was interrupted: the flag set, the worker gone."""
    runner.state["running"] = True
    runner.worker = None


def test_sitediff_refuses_a_second_run_while_one_is_really_going(jobs):
    sd = server.SiteDiffer(jobs)
    done = threading.Event()
    sd.state["running"] = True
    sd.worker = threading.Thread(target=done.wait, daemon=True)
    sd.worker.start()
    try:
        with pytest.raises(ValueError, match="already running"):
            sd.start({"pairs": [{"product": "p", "a": "/a", "b": "/b"}]})
    finally:
        done.set()


def test_sitediff_recovers_when_the_worker_is_gone(jobs, monkeypatch):
    sd = server.SiteDiffer(jobs)
    _stuck(sd)
    assert sd.status()["running"] is False          # the page stops showing it as running
    assert "stopped before it finished" in sd.status()["error"]
    monkeypatch.setattr(sd, "_run", lambda *a, **k: sd.state.update(running=False, message="Done"))
    sd.start({"pairs": [{"product": "p", "a": "/a", "b": "/b"}]})        # and a new run is allowed
    sd.worker.join(2)
    assert sd.status()["error"] == "" and sd.status()["running"] is False


def test_linkcheck_recovers_when_the_worker_is_gone(jobs, monkeypatch):
    lc = server.LinkChecker(jobs)
    _stuck(lc)
    assert lc.status()["running"] is False
    monkeypatch.setattr(lc, "_run", lambda *a, **k: lc.state.update(running=False, message="Done"))
    lc.start({"urls": "https://example.com/guide"})
    lc.worker.join(2)
    assert lc.status()["error"] == "" and lc.status()["running"] is False


def test_linkcheck_refuses_a_second_run_while_one_is_really_going(jobs):
    lc = server.LinkChecker(jobs)
    done = threading.Event()
    lc.state["running"] = True
    lc.worker = threading.Thread(target=done.wait, daemon=True)
    lc.worker.start()
    try:
        with pytest.raises(ValueError, match="already running"):
            lc.start({"urls": "https://example.com/guide"})
    finally:
        done.set()


def test_a_run_that_cannot_start_a_thread_leaves_no_lock(jobs, monkeypatch):
    """Thread.start() raising used to leave `running` true with no worker behind it."""
    sd = server.SiteDiffer(jobs)
    monkeypatch.setattr(threading.Thread, "start", lambda self: (_ for _ in ()).throw(RuntimeError("can't start new thread")))
    with pytest.raises(ValueError, match="Could not start"):
        sd.start({"pairs": [{"product": "p", "a": "/a", "b": "/b"}]})
    assert sd.state["running"] is False
