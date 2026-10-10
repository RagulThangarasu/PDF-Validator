"""The combined report: the PDF validation and the AEM site validation of each product, side by side.

The two run as separate jobs with different naming styles - an ad-hoc PDF run is named after its two
files ("SW272_EN vs bda93743_Monitor SW272 User Manual (4)"), a site run after the guide ("sw272 (en)")
- so the report pairs them by a product key, and keeps the newest run of each kind.
See pdfval/report/combined_report.py.
"""
import json

import pymupdf
import pytest

from pdfval.report import combined_report as cr


def _job(jid, name, mode, status="done", pct=99.0, created="2026-10-10T14:50:39"):
    return {"id": jid, "name": name, "created": created, "finished": "2026-10-10T15:00:00", "mode": mode,
            "status": status, "message": "" if status == "done" else "boom",
            "options": {"mode": mode}, "baseline": "/prod/x.pdf", "candidate": "/stage/x.pdf",
            "summary": None if status != "done" else {
                "sections": 4, "fail": 0, "warn": 0, "pass": 4, "critical": {"total": 0},
                "genuine": {"total": 7}, "css": {"issues": 2},
                "by_category": {"content": {"total": 5}, "images": {"total": 2}},
                "content": {"match_pct": pct, "pass_pct": 98.0, "warn_pct": 90.0,
                            "baseline_words": 900, "missing_words": 3, "extra_words": 1}}}


def _runs(tmp_path, *jobs):
    d = tmp_path / "runs"
    for j in jobs:
        (d / j["id"]).mkdir(parents=True, exist_ok=True)
        (d / j["id"] / "job.json").write_text(json.dumps(j), encoding="utf-8")
    return d


@pytest.mark.parametrize("pdf_name,site_name,same", [
    ("SW272_EN vs bda93743_Monitor SW272 User Manual (4)", "sw272 (en)", True),
    ("W2720i_V1.03_EN vs upload", "w2720i (en)", True),
    ("PD30_EN vs upload", "pd30-timing (en)", False),   # a different product, not merged
    ("GW2291_EN vs upload", "gr10 (en)", False),
])
def test_the_two_naming_styles_meet_on_the_same_product(pdf_name, site_name, same):
    assert (cr.product_key(pdf_name) == cr.product_key(site_name)) is same


def test_a_product_validated_both_ways_is_one_row_with_both_sides(tmp_path):
    d = _runs(tmp_path,
              _job("r1", "SW272_EN vs upload", "pdf", pct=99.6),
              _job("r2", "sw272 (en)", "html", pct=99.1))
    ps = cr.pairs(d)
    assert len(ps) == 1
    p = ps[0]
    assert p["product"] == "sw272 (en)"          # the guide's own name, not "<prod> vs <stage>"
    assert p["pdf"]["match_pct"] == 99.6 and p["site"]["match_pct"] == 99.1
    assert p["worst"] == 99.1
    assert cr.totals(ps)["both"] == 1


def test_a_product_validated_one_way_keeps_the_other_side_empty(tmp_path):
    d = _runs(tmp_path, _job("r1", "gr10 (en)", "html"))
    p = cr.pairs(d)[0]
    assert p["site"] is not None and p["pdf"] is None
    t = cr.totals(p and cr.pairs(d))
    assert t["both"] == 0 and t["pdf"]["missing"] == 1 and t["site"]["runs"] == 1


def test_the_newest_run_of_each_kind_wins(tmp_path):
    d = _runs(tmp_path,
              _job("r1", "sw272 (en)", "html", pct=50.0, created="2026-10-09T09:00:00"),
              _job("r2", "sw272 (en)", "html", pct=99.1, created="2026-10-10T09:00:00"))
    assert cr.pairs(d)[0]["site"]["match_pct"] == 99.1


def test_a_newer_failed_run_is_not_replaced_by_an_older_success(tmp_path):
    """The report shows the product's current state, not its best ever."""
    d = _runs(tmp_path,
              _job("r1", "sw272 (en)", "html", pct=99.1, created="2026-10-09T09:00:00"),
              _job("r2", "sw272 (en)", "html", status="error", created="2026-10-10T09:00:00"))
    site = cr.pairs(d)[0]["site"]
    assert site["status"] == "error" and site["match_pct"] is None


def test_products_are_listed_worst_first(tmp_path):
    d = _runs(tmp_path,
              _job("r1", "aaa (en)", "html", pct=99.9),
              _job("r2", "bbb (en)", "html", pct=91.0),
              _job("r3", "ccc (en)", "html", pct=95.0))
    assert [p["product"] for p in cr.pairs(d)] == ["bbb (en)", "ccc (en)", "aaa (en)"]


def test_it_writes_a_pdf_and_a_csv_naming_both_validations(tmp_path):
    d = _runs(tmp_path,
              _job("r1", "SW272_EN vs upload", "pdf", pct=99.6),
              _job("r2", "sw272 (en)", "html", pct=99.1),
              _job("r3", "gr10 (en)", "html", pct=97.0))
    pdf, csv_ = cr.build(d, tmp_path / "out")
    assert pdf.exists() and csv_.exists()
    text = "\n".join(p.get_text() for p in pymupdf.open(pdf))
    assert "PDF and AEM site validation" in text
    for product in ("sw272 (en)", "gr10 (en)"):
        assert product in text, product
    assert "not validated" in text          # gr10 has no PDF run
    head = csv_.read_text(encoding="utf-8-sig").splitlines()[0]
    assert "PDF validation" in head and "AEM site validation" in head
    assert "Issues by category" in head
