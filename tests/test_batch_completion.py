"""The batch's consolidated report is written to disk by itself, once every product has executed.

Built on demand it only ever covers the runs finished so far, so a batch of 30 products read while 21
are still queued gives a 2-product report. Jobs._finish_batch runs after each run ends and does nothing
until the last one is in. See pdfval/app/server.py.
"""
import json

import pytest

from pdfval.app import server


def _jobs(tmp_path) -> server.Jobs:
    return server.Jobs(tmp_path / "runs", recover=False, parallel=1)


def _write(jobs: server.Jobs, jid: str, name: str, status: str, batch: str = "batch-1") -> dict:
    d = jobs.dir / jid
    d.mkdir(parents=True, exist_ok=True)
    job = {"id": jid, "name": name, "created": "2026-10-10T14:50:39", "finished": "2026-10-10T14:52:44",
           "status": status, "message": "Done" if status == "done" else "boom", "batch": batch,
           "baseline": "/prod/x.pdf", "candidate": "http://site/x.html", "options": {"mode": "html"},
           "summary": {"sections": 2, "fail": 0, "warn": 0, "pass": 2, "critical": {"total": 0},
                       "genuine": {"total": 0}, "css": {"issues": 0},
                       "content": {"match_pct": 99.0, "pass_pct": 98.0, "warn_pct": 90.0,
                                   "baseline_words": 500, "missing_words": 0, "extra_words": 0}} if status == "done" else None}
    (d / "job.json").write_text(json.dumps(job), encoding="utf-8")
    return job


def _report(jobs: server.Jobs):
    return jobs.dir / "_batches" / "batch-1-consolidated.pdf"


def test_nothing_is_written_while_products_are_still_queued(tmp_path):
    jobs = _jobs(tmp_path)
    _write(jobs, "r1", "gr10 (en)", "done")
    _write(jobs, "r2", "gw2291 (en)", "done")
    _write(jobs, "r3", "pd30 (en)", "queued")
    jobs._finish_batch("batch-1")
    assert not _report(jobs).exists()


def test_nothing_is_written_while_a_product_is_still_running(tmp_path):
    jobs = _jobs(tmp_path)
    _write(jobs, "r1", "gr10 (en)", "done")
    _write(jobs, "r2", "gw2291 (en)", "running")
    jobs._finish_batch("batch-1")
    assert not _report(jobs).exists()


def test_the_report_is_written_once_the_last_product_has_executed(tmp_path):
    jobs = _jobs(tmp_path)
    _write(jobs, "r1", "gr10 (en)", "done")
    _write(jobs, "r2", "gw2291 (en)", "done")
    _write(jobs, "r3", "w2720i (en)", "error")  # a product that failed still counts as executed
    jobs._finish_batch("batch-1")
    pdf = _report(jobs)
    assert pdf.exists() and pdf.stat().st_size > 0
    csv_ = pdf.with_suffix(".csv")
    assert csv_.exists()
    names = csv_.read_text(encoding="utf-8-sig")
    for product in ("gr10 (en)", "gw2291 (en)", "w2720i (en)"):
        assert product in names, product


def test_it_is_built_only_once_per_batch(tmp_path):
    jobs = _jobs(tmp_path)
    _write(jobs, "r1", "gr10 (en)", "done")
    _write(jobs, "r2", "gw2291 (en)", "done")
    jobs._finish_batch("batch-1")
    first = _report(jobs).stat().st_mtime_ns
    jobs._finish_batch("batch-1")  # the other run of the pair ends a moment later
    assert _report(jobs).stat().st_mtime_ns == first


def test_a_run_outside_any_batch_writes_nothing(tmp_path):
    jobs = _jobs(tmp_path)
    _write(jobs, "r1", "gr10 (en)", "done", batch="")
    jobs._finish_batch("")
    assert not (jobs.dir / "_batches").exists() or not list((jobs.dir / "_batches").glob("*.pdf"))


@pytest.mark.parametrize("statuses,complete", [
    (["done", "done"], True), (["done", "error"], True), (["done", "stopped"], True),
    (["done", "queued"], False), (["done", "running"], False)])
def test_the_ui_is_told_whether_the_batch_is_complete(tmp_path, statuses, complete):
    jobs = _jobs(tmp_path)
    for n, st in enumerate(statuses):
        _write(jobs, f"r{n}", f"p{n} (en)", st)
    assert server._batches(jobs)[0]["complete"] is complete
