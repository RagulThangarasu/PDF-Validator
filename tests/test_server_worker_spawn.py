"""pdfval.app.server: the worker subprocess must not inherit the server's own stdin. A server that has
outlived the terminal/session that started it can have an already-invalid stdin; a new Python
interpreter then fails at startup trying to wrap that fd as sys.stdin ("Fatal Python error:
init_sys_streams ... Bad file descriptor"), reported to the user as "Worker stopped: <no Python frame>"
- on every single run, regardless of what changed, since it happens before any of pdfval's own code runs."""
import json
import subprocess
from datetime import datetime

import pymupdf
import pytest

from pdfval.app.server import Jobs


@pytest.fixture
def jobs(tmp_path):
    return Jobs(tmp_path / "runs", recover=False)


def _make_job(jobs_obj, tmp_path):
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    for p in (a, b):
        doc = pymupdf.open()
        doc.new_page()
        doc.save(p)
    jid = "20260101-000000-test"
    jobs_obj.path(jid).mkdir()
    job = {"id": jid, "name": "test", "created": datetime.now().isoformat(timespec="seconds"),
          "baseline": str(a), "candidate": str(b), "options": {}, "mode": "pdf", "status": "queued",
          "progress": 0.0, "message": "Queued", "summary": None}
    (jobs_obj.path(jid) / "job.json").write_text(json.dumps(job), encoding="utf-8")
    return jid


class _FakeProc:
    def __init__(self, args, **kw):
        self.kwargs = kw
        self.args = args
        self.returncode = 0

    def communicate(self):
        return "", ""


def test_worker_subprocess_does_not_inherit_stdin(jobs, tmp_path, monkeypatch):
    jid = _make_job(jobs, tmp_path)
    captured = {}

    def fake_popen(args, **kw):
        captured.update(kw)
        return _FakeProc(args, **kw)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    jobs._run(jid)
    assert captured.get("stdin") is subprocess.DEVNULL
