"""The consolidated batch report and the batch (fast) report mode."""
import json

from pdfval import compare, load_config
from pdfval.report import batch_report, writer
from test_engine_synthetic import BODY, make_pdf  # tests/ is on sys.path under pytest


def _job(jid, name, pct, status="done", words=1000):
    return {"id": jid, "name": name, "created": "2026-10-01T10:00:00", "finished": "2026-10-01T10:02:05",
            "status": status, "message": "Worker stopped: boom" if status == "error" else "Done",
            "summary": None if status != "done" else {
                "sections": 4, "fail": 1, "warn": 1, "pass": 2, "critical": {"total": 2}, "genuine": {"total": 7},
                "css": {"issues": 3}, "content": {"match_pct": pct, "pass_pct": 98.0, "warn_pct": 90.0,
                                                  "baseline_words": words, "missing_words": 5, "extra_words": 1}}}


def test_consolidated_lists_every_product_worst_first(tmp_path):
    jobs = [_job("r1", "gw2291 (en)", 99.1), _job("r2", "ma270s (en)", 25.0, words=3000),
            _job("r3", "pd20u (en)", 93.5), _job("r4", "w5800 (en)", None, status="error")]
    rows = batch_report.rows(jobs, tmp_path)
    assert [r["product"] for r in rows] == ["ma270s (en)", "pd20u (en)", "gw2291 (en)", "w5800 (en)"]
    assert [r["band"] for r in rows] == ["fail", "warn", "pass", ""] and rows[0]["seconds"] == 125
    t = batch_report.totals(rows)
    assert (t["products"], t["done"], t["error"], t["pass"], t["warn"], t["fail"]) == (4, 3, 1, 1, 1, 1)
    assert t["average"] == round((99.1 + 25.0 + 93.5) / 3, 2) and t["weighted"] < t["average"]  # the big manual weighs more
    pdf, csv_ = batch_report.build("batch-x", jobs, tmp_path, tmp_path / "out")
    assert pdf.stat().st_size > 1000 and "ma270s (en)" in csv_.read_text(encoding="utf-8-sig")


def test_batch_mode_builds_only_the_delivered_reports_then_the_rest_on_demand(tmp_path):
    cfg = load_config()
    cfg["sections"]["front_matter"] = False
    a = make_pdf(tmp_path / "a.pdf")
    b = make_pdf(tmp_path / "b.pdf", body_text=BODY.replace("lazy", "sleepy"))
    out = tmp_path / "run"
    writer.write_all(compare(a, b, cfg), str(out), "reports", full=False)
    made = {p.name for p in out.glob("*.pdf")}
    assert "genuine-issues.pdf" in made and not made & set(writer.DEFERRED)
    # the image report only when the publication has image issues: a PASS (this text-only pair) has none
    images = json.loads((out / "results.json").read_text())["summary"]["images"]
    assert images == {"issues": 0, "result": "pass"} and "image-issues.pdf" not in made
    writer.build_deferred(out, "report.pdf")
    assert set(writer.DEFERRED) <= {p.name for p in out.glob("*.pdf")}
    assert json.loads((out / "results.json").read_text())["meta"]["deferred"] == []
