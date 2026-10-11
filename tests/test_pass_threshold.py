"""A publication scoring at or above [content] pass_pct (95 %) reads PASS in the metrics: the verdict
at the top of its report, its row in the consolidated / combined reports, and its picture verdict.

Only the metrics. The sections inside a report keep their own status, so a section holding a critical
issue still reads FAIL where that issue is listed - the summary says the publication matches prod,
the body still says exactly what is wrong with it.
"""
import pytest

from pdfval.report.writer import image_summary


@pytest.fixture
def n_image_issues(monkeypatch):
    """image_report.issues() walks the real PDFs; here only how many it finds matters."""
    from pdfval.report import image_report

    def set_to(n):
        monkeypatch.setattr(image_report, "issues", lambda result: [("s", "f", "kind")] * n)
    return set_to


def _result(pct, pass_pct=95.0):
    return {"summary": {"content": {"match_pct": pct, "pass_pct": pass_pct}}, "sections": []}


# --- the picture verdict in the metrics ------------------------------------------------------------

@pytest.mark.parametrize("pct,issues,expected", [
    (96.2, 29, "pass"),    # above pass_pct: PASS even with 29 picture issues
    (99.6, 2, "pass"),
    (95.0, 5, "pass"),     # exactly at the threshold
    (94.9, 5, "fail"),     # below it: the picture issues decide again
    (80.0, 1, "fail"),
    (80.0, 0, "pass"),     # no picture issue at all is always a PASS
])
def test_the_image_verdict_follows_the_content_score(n_image_issues, pct, issues, expected):
    n_image_issues(issues)
    assert image_summary(_result(pct))["result"] == expected


def test_the_issue_count_is_still_carried(n_image_issues):
    """PASS does not mean "no issues": the count is still there, and the image report is still built."""
    n_image_issues(29)
    sm = image_summary(_result(99.0))
    assert sm["result"] == "pass" and sm["issues"] == 29


def test_a_run_with_no_content_score_falls_back_to_counting(n_image_issues):
    n_image_issues(1)
    assert image_summary({"summary": {}, "sections": []})["result"] == "fail"


# --- the publication verdict, and what it must NOT touch -------------------------------------------

def test_the_publication_verdict_is_the_score_not_the_section_statuses():
    import inspect

    from pdfval import engine
    src = inspect.getsource(engine.compare)
    verdict = src[src.index('"result":'):src.index('"result":') + 260]
    assert "pass_pct" in verdict, verdict
    assert 'if status["fail"]' not in verdict, "the verdict must not be driven by failing sections"


def test_a_section_keeps_its_own_status():
    """The inner status is untouched: a critical issue still fails the section it is in."""
    import inspect

    from pdfval import engine
    src = inspect.getsource(engine.compare)
    assert 'status = ("fail" if critical or content_status == "fail" or other == 2' in src
