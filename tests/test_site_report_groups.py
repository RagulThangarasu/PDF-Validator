"""site.pdf only prints the site run's own TOC/pager checklist:

    Left navigation (TOC)    the whole TOC, entry by entry, against the PDF's own bookmarks
    Next / previous topic    pager logic (first page no "previous", last page no "next", ...)

It is no longer merged into the consolidated report either (see tests/test_consolidated_report.py) -
content validation lives in genuine-issues.pdf. Everything else the site run also checks
(navigation/content links, Download PDF, "On this page", breadcrumb, product subtitle, layout/pictures,
CSS vs the design spec) is still run and still counted in results.json - this is a reporting choice
(site_report.SKIP_GROUPS), not a change to the checks.
"""
import pymupdf
import pytest

from pdfval import site_nav
from pdfval.report import site_report

pytest.importorskip("pymupdf")


def _row(group, status, item, url="https://docs.example.com/a"):
    return {"group": group, "status": status, "item": item, "expected": "", "actual": "",
            "url": url, "note": "", "where": ""}


def _result(rows):
    groups = {}
    for r in rows:
        g = groups.setdefault(r["group"], {"pass": 0, "warn": 0, "fail": 0, "info": 0})
        g[r["status"]] += 1
    return {"meta": {"generated": "2026-10-11T12:00:00",
                     "candidate": {"url": "https://docs.example.com/guide"},
                     "baseline": {"path": "/prod/x.pdf"}},
            "sections": [],
            "site": {"rows": rows, "pages": [], "groups": [{"id": g, "title": t} for g, t in site_nav.GROUPS],
                     "summary": {"status": "fail", "pages": 3, "groups": groups,
                                 "fail": sum(g["fail"] for g in groups.values()),
                                 "warn": sum(g["warn"] for g in groups.values()),
                                 "pass": sum(g["pass"] for g in groups.values())}}}


ROWS = [
    _row("nav", "fail", "Chapter missing from the left navigation"),
    _row("links", "fail", "Navigation link not working"),
    _row("download", "fail", "Download PDF button not found"),
    _row("pager", "pass", "Next / previous topic"),
    _row("otp", "fail", "On this page entry missing"),
    _row("breadcrumb", "pass", "Breadcrumb"),
    _row("subtitle", "fail", "Product subtitle differs"),
    _row("typography", "fail", "Headline 1 font size"),
    _row("typography", "warn", "Body line height"),
    _row("layout", "fail", "Picture not loaded"),
    _row("layout", "warn", "List out of the text margin"),
]


def _text(tmp_path, rows=ROWS):
    out = site_report.build(_result(rows), tmp_path)
    return "\n".join(p.get_text() for p in pymupdf.open(out))


def test_only_toc_and_pager_are_printed(tmp_path):
    text = _text(tmp_path)
    for kept in ("Left navigation", "Chapter missing from the left navigation", "Next / previous topic"):
        assert kept in text, kept
    for hidden in ("Navigation link not working", "Download PDF", "On this page entry missing",
                   "Breadcrumb", "Product subtitle differs", "Headline 1 font size", "Body line height",
                   "Page layout and pictures", "Picture not loaded", "List out of the text margin"):
        assert hidden not in text, hidden


def test_the_dashboard_counts_only_what_is_printed(tmp_path):
    """The run's own summary still counts every group's fails - a header above the one shown fail
    (nav) must not silently include the other six, which this report does not print."""
    text = _text(tmp_path)
    head = text[:text.index("Left navigation")]          # the dashboard table, above the first section
    shown = {g for g, _ in site_report.report_groups()}
    printed_fails = sum(1 for r in ROWS if r["status"] == "fail" and r["group"] in shown)
    assert printed_fails == 1                            # nav only; the other six fails are hidden
    assert _result(ROWS)["site"]["summary"]["fail"] == 7  # the run itself still counts all seven
    assert str(printed_fails) in head, head


def test_the_skipped_groups_are_named_in_one_place(tmp_path):
    assert site_report.SKIP_GROUPS == {"links", "download", "otp", "breadcrumb", "subtitle", "typography", "layout"}
    assert [g for g, _ in site_report.report_groups()] == ["nav", "pager"]
    assert [g for g, _ in site_report.report_groups()] == \
        [g for g, _ in site_nav.GROUPS if g not in site_report.SKIP_GROUPS]


def test_a_caller_can_still_pass_its_own_groups(tmp_path):
    """aem_site_report builds the same report with its own four sections."""
    out = site_report.build(_result(ROWS), tmp_path, filename="custom.pdf",
                            groups=[("nav", "Left navigation — whole TOC")])
    text = "\n".join(p.get_text() for p in pymupdf.open(out))
    assert "Left navigation" in text and "Navigation links" not in text
