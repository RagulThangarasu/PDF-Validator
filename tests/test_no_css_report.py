"""There is no CSS report. Fonts, sizes, colours, line heights and design-spec styles are not a
deliverable of their own: no css-issues.pdf is built, it is not merged into the consolidated file,
and no report counts "CSS issues" per product.

The findings themselves are untouched - they are still produced, still in results.json, and still in
the full report.pdf - so turning the report back on is a matter of building it again, not re-finding
anything.
"""
import json

import pymupdf

from pdfval.report import batch_report, writer


def test_css_issues_pdf_is_not_a_deferred_report():
    assert "css-issues.pdf" not in writer.DEFERRED


def test_the_consolidated_file_does_not_merge_a_css_part():
    assert not [f for f, _ in writer._CONSOLIDATED if "css" in f]


def test_a_css_report_left_on_disk_is_not_merged_in(tmp_path):
    """An old run still has css-issues.pdf beside its other reports: it must be ignored, not folded in."""
    for name, mark in (("genuine-issues.pdf", "GENUINE-PAGE"), ("css-issues.pdf", "CSS-PAGE")):
        doc = pymupdf.open()
        doc.new_page().insert_text((72, 100), mark, fontsize=14)
        doc.save(str(tmp_path / name))
        doc.close()
    result = {"meta": {"baseline": {"path": "/prod/SW272_EN.pdf"}, "candidate": {"path": "/stage/x.pdf"}}}
    doc = pymupdf.open(writer.write_consolidated(result, tmp_path))
    text = "\n".join(p.get_text() for p in doc)
    assert "GENUINE-PAGE" in text and "CSS-PAGE" not in text


def _job(jid, name, pct):
    return {"id": jid, "name": name, "created": "2026-10-10T10:00:00", "finished": "2026-10-10T10:02:00",
            "status": "done", "message": "Done",
            "summary": {"sections": 2, "fail": 0, "warn": 0, "pass": 2, "critical": {"total": 0},
                        "genuine": {"total": 3}, "css": {"issues": 42},
                        "by_category": {"content": {"total": 3}},
                        "content": {"match_pct": pct, "pass_pct": 98.0, "warn_pct": 90.0,
                                    "baseline_words": 500, "missing_words": 1, "extra_words": 0}}}


def test_the_batch_report_has_no_css_column(tmp_path):
    rs = batch_report.rows([_job("r1", "sw272 (en)", 99.1)], tmp_path)
    csv_ = batch_report.write_csv(rs, tmp_path / "b.csv")
    head = csv_.read_text(encoding="utf-8-sig").splitlines()[0]
    assert "CSS" not in head, head
    pdf = batch_report.build_pdf("batch-1", rs, tmp_path / "b.pdf")
    text = "\n".join(p.get_text() for p in pymupdf.open(pdf))
    assert "CSS issues" not in text
    assert "sw272 (en)" in text and "99.10" in text   # the rest of the row is unaffected


def test_a_section_header_does_not_mention_css_issues(tmp_path):
    """"CSS issues 0" sat on every section header of every report, including the consolidated one -
    a count pointing at a report that is no longer produced."""
    from pdfval.report import pdf_report
    src = open(pdf_report.__file__, encoding="utf-8").read()
    header = src[src.index("def _section_header"):src.index("def _toc_places")]
    assert "s['css']" not in header and 'CSS issues {' not in header


def test_the_markdown_summary_has_no_css_column():
    from pdfval.report import writer as w
    src = open(w.__file__, encoding="utf-8").read()
    table = src[src.index('"## Sections"'):src.index('"## Global style map')]
    assert "CSS issues" not in table and "css" not in table
