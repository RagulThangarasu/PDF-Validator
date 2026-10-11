"""Every picture issue has to be in a report somebody is handed.

Two things used to swallow them: picture findings sat in the "formatting" group, which
[report] exclude_groups drops from the PDF reports, and image-issues.pdf was only written when the
publication FAILED - and a publication whose text matches prod reads PASS however many pictures are
wrong. Between the two, a run could report 20 picture issues in results.json and none in any PDF.
"""
import json

import pymupdf

from pdfval.report import pdf_report, writer


def _finding(fid, category, types, genuine=True, check=None):
    return {"id": fid, "category": category, "check": check or ("assets" if category == "images" else "content"),
            "types": types, "severity": "error", "genuine": genuine, "critical": False,
            "message": f"{types[0]} somewhere", "description": f"{types[0]} somewhere",
            "baseline": [{"page": 1, "bbox": [0, 0, 10, 10]}], "candidate": [{"page": 1, "bbox": [0, 0, 10, 10]}],
            "detail": {}, "shots": {}}


def _result():
    return {"meta": {"name": "x", "baseline": {"path": "/prod/x.pdf"}, "candidate": {"path": "/stage/x.pdf"}},
            "summary": {"content": {"match_pct": 99.6, "pass_pct": 95.0}},
            "sections": [{"id": "s1", "title": "Connections",
                          "findings": [_finding("000-000", "content", ["missing text"]),
                                       _finding("000-001", "images", ["image smaller"]),
                                       _finding("000-002", "images", ["extra image"]),
                                       _finding("000-003", "images", ["placement"])],
                          "image_findings": []}]}


def test_picture_findings_are_their_own_report_group():
    assert pdf_report.group_of(_finding("x", "images", ["image smaller"])) == "images"
    assert "images" not in pdf_report.DEFAULT_EXCLUDE_GROUPS
    n, label, _ = pdf_report.group_number("images", {"formatting"})
    assert (n, label) == (3, "Images")


def test_the_genuine_report_keeps_every_picture_issue():
    r = _result()
    picked = pdf_report.select_issues(r, pdf_report.GENUINE["filter"], None)
    assert {f["id"] for _, f in picked} == {"000-000", "000-001", "000-002", "000-003"}


def test_dropping_the_formatting_group_does_not_drop_pictures():
    r = _result()
    r["meta"]["report"] = {"exclude_groups": ["formatting"]}
    picked = pdf_report.select_issues(r, pdf_report.GENUINE["filter"], None)
    assert sum(1 for _, f in picked if f["category"] == "images") == 3


def test_the_image_report_is_written_for_a_publication_that_passes_on_text(tmp_path, monkeypatch):
    """PASS on content, pictures still wrong: the verdict stays PASS, the report is still written."""
    r = _result()
    monkeypatch.setattr("pdfval.report.image_report.issues", lambda result: [("s", "f", "Artwork missing")])
    def _build(result, out):
        doc = pymupdf.open()
        doc.new_page().insert_text((72, 100), "picture issues", fontsize=12)
        doc.save(str(out / "image-issues.pdf"))
        return out / "image-issues.pdf"
    monkeypatch.setattr("pdfval.report.image_report.build", _build)
    out = writer.write_image_report(r, tmp_path)
    assert r["summary"]["images"] == {"issues": 1, "result": "pass"}   # the verdict is unchanged
    assert out is not None and (tmp_path / "image-issues.pdf").exists()


def test_a_run_with_no_picture_issue_writes_no_image_report(tmp_path, monkeypatch):
    monkeypatch.setattr("pdfval.report.image_report.issues", lambda result: [])
    (tmp_path / "image-issues.pdf").write_bytes(b"stale")
    assert writer.write_image_report(_result(), tmp_path) is None
    assert not (tmp_path / "image-issues.pdf").exists()
