"""“<publication> new.pdf”: a second report per publication, holding one fixed set of checks.

Lists and bold, a picture 40 % smaller or bigger, a picture moved left / centre / right, an icon
wrapped onto the next line, a table past the page margin, and any mismatch on a level 1 TOC entry.
Nothing else from the run reaches it. See pdfval/report/new_report.py.
"""
import pymupdf
import pytest

from pdfval import engine
from pdfval.report import new_report


def _f(fid, types, check="content", detail=None):
    return {"id": fid, "types": list(types), "check": check, "severity": "warning",
            "message": " ".join(types), "detail": detail or {}, "baseline": [], "candidate": []}


def _result(findings, toc_rows=None):
    """A run reduced to what this report reads: one section and its findings."""
    return {"meta": {"baseline": {"path": "/tmp/RD270Q_UM_EN.pdf"}},
            "sections": [{"id": "001", "title": "Overview", "status": "fail", "level": 1,
                          "findings": findings}],
            "toc": {"rows": toc_rows or []}}


# --- which issues it lists --------------------------------------------------------------------------

@pytest.mark.parametrize("typ", ["numbering style", "numbering format", "numbering sequence",
                                 "bullet", "bullet marker", "marker glued", "emphasis",
                                 "image alignment", "placement",
                                 "icon wraps to next line", "icon alignment", "table past margin"])
def test_the_listed_kinds_are_in(typ):
    assert new_report.issue_ids(_result([_f("1", [typ])])) == ["1"]


@pytest.mark.parametrize("typ", ["missing text", "extra text", "changed text", "spacing",
                                 "missing image", "image blurred", "cell differs", "broken link",
                                 "indent", "line-height", "text-align"])
def test_everything_else_is_out(typ):
    assert new_report.issue_ids(_result([_f("1", [typ])])) == []


# --- 2. a picture 40 % smaller or bigger ------------------------------------------------------------

@pytest.mark.parametrize("a,b,listed", [
    (0.30, 0.18, True),    # 40 % smaller exactly
    (0.30, 0.15, True),    # half size
    (0.30, 0.21, False),   # 30 % smaller: not enough
    (0.30, 0.42, True),    # 40 % bigger
    (0.30, 0.36, False),   # 20 % bigger
])
def test_the_size_rule(a, b, listed):
    f = _f("1", ["image smaller" if b < a else "image bigger"], "assets",
           {"kind": "size", "baseline_width": a, "candidate_width": b})
    assert new_report.issue_ids(_result([f])) == (["1"] if listed else [])


def test_a_size_finding_without_numbers_is_not_guessed_at():
    f = _f("1", ["image smaller"], "assets", {"kind": "size"})
    assert new_report.issue_ids(_result([f])) == []


def test_the_size_change_is_configurable():
    f = _f("1", ["image smaller"], "assets", {"kind": "size", "baseline_width": 0.3, "candidate_width": 0.24})
    assert new_report.issue_ids(_result([f])) == []                      # 20 %: out at the default 40 %
    assert new_report.issue_ids(_result([f]), size_change=0.15) == ["1"]  # ... in at 15 %


# --- 6. the table of contents: level 1 only ----------------------------------------------------------

def _toc_result(level, typ="missing entry"):
    rows = [{"status": typ, "baseline": {"title": "Specifications", "level": level}, "candidate": None}]
    return _result([_f("1", [typ], "toc", {"row": 0})], rows)


@pytest.mark.parametrize("typ", ["missing entry", "extra entry", "title differs", "level differs", "wrong page"])
def test_a_level_1_toc_mismatch_is_listed(typ):
    assert new_report.issue_ids(_toc_result(1, typ)) == ["1"]


def test_a_deeper_toc_entry_is_not(typ="missing entry"):
    assert new_report.issue_ids(_toc_result(2)) == []
    assert new_report.issue_ids(_toc_result(3)) == []


def test_a_toc_finding_with_no_row_is_left_out():
    assert new_report.issue_ids(_result([_f("1", ["missing entry"], "toc", {})], [])) == []


# --- the file itself ---------------------------------------------------------------------------------

def _pdf(path, *, letters: bool) -> str:
    """Three numbered steps - 1. 2. 3. in prod, a. b. c. in stage."""
    doc = pymupdf.open()
    pg = doc.new_page(width=595, height=842)
    pg.insert_text((72, 70), "Getting started", fontsize=16, fontname="hebo")
    marks = ("a.", "b.", "c.") if letters else ("1.", "2.", "3.")
    steps = ("Connect the receiver to the display.", "Power on the computer and the monitor.",
             "Set to extend your screen from the operating system.")
    for k, (m, t) in enumerate(zip(marks, steps)):
        pg.insert_text((72, 120 + 18 * k), f"{m} {t}", fontsize=10, fontname="helv")
    doc.set_toc([[1, "Getting started", 1]])
    doc.save(path)
    return str(path)


def test_the_report_is_written_under_one_fixed_name(tmp_path):
    cfg = engine.load_config()
    cfg["typography"]["enabled"] = False
    a = _pdf(tmp_path / "RD270Q_UM_EN.pdf", letters=False)
    b = _pdf(tmp_path / "stage.pdf", letters=True)
    r = engine.compare(a, b, cfg)
    out = tmp_path / "run"
    out.mkdir()
    from pdfval.report import writer
    writer.write_pdf_report(r, out)
    made = out / "new.pdf"
    assert made.exists(), [p.name for p in out.iterdir()]
    assert made.stat().st_size > 1000


def test_a_clean_run_leaves_no_stale_report(tmp_path):
    out = tmp_path / "run"
    out.mkdir()
    stale = out / "new.pdf"
    older = out / "RD270Q_UM_EN new.pdf"   # what the report was called before the fixed name
    stale.write_bytes(b"%PDF-1.4 old")
    older.write_bytes(b"%PDF-1.4 older")
    assert new_report.build(_result([_f("1", ["missing text"])]), out) is None
    assert not stale.exists() and not older.exists()


# --- it is handed on: in the batch zip and under one URL per run -------------------------------------

def test_the_batch_zip_carries_each_publication_s_new_pdf(tmp_path):
    """The zip is what gets handed on, so the publication's own new.pdf goes in it, named after the
    publication - and a run that has nothing but a new.pdf is no longer skipped."""
    import zipfile
    from pdfval.app.server import _batch_zip
    from test_batch_report import _Jobs, _job

    jobs = [{**_job("r1", "rd270q (en)", 90.0), "batch": "batch-n"}]
    (tmp_path / "r1").mkdir()
    (tmp_path / "r1" / "new.pdf").write_bytes(b"%PDF-1.4\n%new\n")
    path = _batch_zip(_Jobs(tmp_path, jobs), "batch-n")
    try:
        names = set(zipfile.ZipFile(path).namelist())
    finally:
        path.unlink(missing_ok=True)
    assert "rd270q (en) new.pdf" in names, sorted(names)


def test_an_older_run_keeps_working(tmp_path):
    """A run made while the report was still named after the publication: the zip and the page both
    fall back to that file, so nothing has to be re-run."""
    from pdfval.app.server import _batch_zip
    from test_batch_report import _Jobs, _job
    import zipfile

    jobs = [{**_job("r1", "rd270q (en)", 90.0), "batch": "batch-o"}]
    (tmp_path / "r1").mkdir()
    (tmp_path / "r1" / "RD270Q_UM_EN new.pdf").write_bytes(b"%PDF-1.4\n%new\n")
    path = _batch_zip(_Jobs(tmp_path, jobs), "batch-o")
    try:
        names = set(zipfile.ZipFile(path).namelist())
    finally:
        path.unlink(missing_ok=True)
    assert "rd270q (en) new.pdf" in names, sorted(names)


def test_the_report_can_be_switched_off(tmp_path):
    from pdfval.report import writer
    r = _result([_f("1", ["bullet"])])
    r["meta"]["report"] = {"new_report": False}
    out = tmp_path / "run"
    out.mkdir()
    assert writer.write_new_report(r, out) is None
    assert not (out / "new.pdf").exists()


# --- numbering is the point of the report: it must never fall out with an indent --------------------

@pytest.mark.parametrize("number_type", ["numbering style", "numbering format", "numbering sequence",
                                         "bullet marker", "marker glued", "emphasis"])
@pytest.mark.parametrize("layout_type", ["indent", "list level", "wrap under number"])
def test_a_number_change_is_listed_even_when_the_same_finding_is_also_an_indent(number_type, layout_type):
    """A changed number or letter tagged with its indent used to be dropped whole."""
    assert new_report.issue_ids(_result([_f("1", [number_type, layout_type])])) == ["1"]


def test_only_the_bullet_position_is_dropped():
    assert new_report.issue_ids(_result([_f("1", ["bullet", "indent"])])) == []   # where it sits
    assert new_report.issue_ids(_result([_f("1", ["bullet"])])) == ["1"]          # the bullet itself


def test_the_run_says_whether_it_has_one(tmp_path):
    """summary.new_report.issues: the page offers the download only where there is one, instead of
    linking at a 404. Counted on a real run, so the number is the report's own."""
    from pdfval.report import writer
    cfg = engine.load_config()
    cfg["typography"]["enabled"] = False
    out = tmp_path / "run"
    out.mkdir()

    changed = engine.compare(_pdf(tmp_path / "a.pdf", letters=False), _pdf(tmp_path / "b.pdf", letters=True), cfg)
    writer.write_new_report(changed, out)
    assert changed["summary"]["new_report"]["issues"] > 0 and (out / "new.pdf").exists()

    same = engine.compare(_pdf(tmp_path / "c.pdf", letters=False), _pdf(tmp_path / "d.pdf", letters=False), cfg)
    writer.write_new_report(same, out)
    assert same["summary"]["new_report"] == {"issues": 0} and not (out / "new.pdf").exists()
