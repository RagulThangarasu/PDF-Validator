"""The deliverable report (genuine-issues.pdf, and the consolidated *-validation-report.pdf built from
it) must carry the TOC comparison and the design spec:

- the TOC is compared entry by entry whenever both PDFs have one, even when no TOC finding survives
  the report's filters ([report] exclude_groups drops the "formatting" group, which the TOC is in):
  "no TOC issue" and "the TOC was never compared" must not read the same;
- a style set lighter or heavier than the Figma spec ("spec font-weight") is a genuine issue, not
  something left out of the validation.
"""
import json
import tomllib
from pathlib import Path

import pymupdf

from pdfval.report import pdf_report

CONFIG = Path(__file__).resolve().parent.parent / "config" / "default.toml"


def _result(toc_rows=2):
    rows = [{"status": "match", "similarity": 1.0, "flags": [], "pos": {"baseline": k, "candidate": k},
             "baseline": {"title": f"Chapter {k}", "level": 1, "page": k + 1, "page_ok": True},
             "candidate": {"title": f"Chapter {k}", "level": 1, "page": k + 1, "toc_page": 1,
                           "actual_page": k + 1, "page_ok": True}} for k in range(1, toc_rows + 1)]
    return {
        "meta": {"name": "x", "mode": "pdf", "generated": "2026-10-12T00:00:00", "baseline": {"path": "/prod/x.pdf", "pages": 9, "page_size": [595, 842], "body_size": 12},
                 "candidate": {"path": "/stage/x.pdf", "pages": 9, "page_size": [595, 842], "body_size": 12}},
        "summary": {"sections": 1, "pass": 1, "warn": 0, "fail": 0, "result": "pass",
                    "content": {"match_pct": 100.0, "issues": 0, "counted_issues": 0, "issue_weight_pct": 0.1,
                                "word_match_pct": 100.0, "baseline_words": 10, "missing_words": 0, "extra_words": 0,
                                "spacing_issues": 0, "pass": 1, "warn": 0, "fail": 0, "pass_pct": 95.0, "warn_pct": 90.0},
                    "critical": {"total": 0, "by_kind": {}}, "genuine": {"total": 0, "by_issue": {}},
                    "css": {"issues": 0, "style": 0, "layout": 0}, "by_check": {}, "by_category": {},
                    "images": {"issues": 0, "result": "pass"}},
        "sections": [{"id": "s1", "title": "Chapter 1", "level": 1, "status": "pass",
                      "baseline": {"start": {"page": 0}}, "candidate": {"start": {"page": 0}},
                      "content": {"match_pct": 100.0, "status": "pass", "missing_words": 0, "extra_words": 0,
                                  "pass_pct": 95.0, "warn_pct": 90.0},
                      "critical": 0, "css": {"issues": 0, "style": 0, "layout": 0},
                      "checks": {}, "categories": {}, "findings": [], "image_findings": []}],
        "toc": {"source": {"baseline": "printed", "candidate": "printed"},
                "pages": {"baseline": [1], "candidate": [1]},
                "heading": {"baseline": "Table of contents", "candidate": "Table of contents"},
                "levels": {"baseline": 1, "candidate": 1},
                "summary": {"status": "pass", "sequence_ok": True, "order differs": 0,
                            "baseline_entries": toc_rows, "candidate_entries": toc_rows, "match": toc_rows,
                            "level differs": 0, "title differs": 0, "missing in stage": 0, "extra in stage": 0,
                            "wrong page": 0, "levels_match_pct": 100.0, "extra validated": 0},
                "rows": rows, "page_size": {"baseline": {}, "candidate": {}},
                "images": {"baseline": [], "candidate": []}},
        "style_map": [], "sync": [],
    }


def _text(path):
    with pymupdf.open(path) as d:
        return "\n".join(p.get_text() for p in d)


def test_the_genuine_report_compares_the_toc_with_no_toc_finding(tmp_path):
    out = pdf_report.build(_result(), tmp_path, options=pdf_report.GENUINE, filename="genuine-issues.pdf")
    text = _text(out)
    assert "Table of contents: prod vs stage" in text
    assert "Chapter 1" in text and "Chapter 2" in text


def test_a_run_without_a_printed_toc_has_no_toc_part(tmp_path):
    r = _result()
    r["toc"]["rows"] = []
    out = pdf_report.build(r, tmp_path, options=pdf_report.GENUINE, filename="genuine-issues.pdf")
    assert "Table of contents: prod vs stage" not in _text(out)


def test_design_spec_font_weight_is_validated_and_genuine():
    cfg = tomllib.loads(CONFIG.read_text())
    assert "spec font-weight" not in cfg["ignore"]["types"], "the design spec's weight must be checked"
    assert "spec font-weight" in cfg["genuine"]["types"], "... and reported in the genuine issues"
