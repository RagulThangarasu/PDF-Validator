"""The Word report holds the issues of the PDF report - the same ones - and nothing else: no metrics, no tables."""
import re

import pytest

docx = pytest.importorskip("docx")

from pdfval.report import docx_report, pdf_report


def _finding(fid, issue, message):
    return {"id": fid, "check": "content", "category": "content", "severity": "warning", "message": message, "description": message,
            "issue": issue, "genuine": True, "types": ["missing text"], "baseline": [{"page": 3, "bbox": [0, 0, 10, 10]}], "candidate": [],
            "detail": {}, "critical": False}


def test_same_issues_as_the_pdf_report_and_no_tables(tmp_path):
    section = {"id": 0, "title": "Getting started", "status": "fail", "findings": [
        _finding("000-001", "Data missing", "Missing text: “Press OK”"),
        _finding("000-002", "Extra content", "Extra text: “Press Cancel”"),
        {**_finding("000-003", "", "a note"), "genuine": False}]}
    result = {"meta": {"name": "Test product"}, "sections": [section], "summary": {}}
    expected = [f["id"] for _, f in pdf_report.select_issues(result, pdf_report.GENUINE["filter"], None)]
    doc = docx.Document(str(docx_report.build(result, tmp_path)))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert re.findall(r"#(\d{3}-\d{3})", text) == expected == ["000-001", "000-002"]
    assert not doc.tables
    assert "Getting started" in text and "Score" not in text
