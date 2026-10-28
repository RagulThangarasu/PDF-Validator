"""One PDF for a whole batch: every publication's validation report behind a contents page."""
import pymupdf
import pytest

from pdfval.report import bundle


def _report(path, product: str, pages: int, result: str = "PASS", issues: int = 2):
    """A stand-in for a run's <name>-validation-report.pdf: the same cover fields the bundle reads."""
    doc = pymupdf.open()
    pg = doc.new_page()
    pg.insert_text((40, 60), f"PDF Report — {result}", fontsize=14)
    pg.insert_text((40, 90), "Product", fontsize=9)
    pg.insert_text((40, 102), product, fontsize=9)
    pg.insert_text((40, 130), "Score", fontsize=9)
    pg.insert_text((40, 142), "98.50%", fontsize=9)
    pg.insert_text((40, 170), "Critical / breaking", fontsize=9)
    pg.insert_text((40, 182), "2 2 × image missing", fontsize=9)
    pg.insert_text((40, 210), f"{issues} issue(s) · genuine issues only", fontsize=9)
    for k in range(pages - 1):
        doc.new_page().insert_text((40, 60), f"issue {k + 1}", fontsize=10)
    doc.set_toc([[1, "Issues — content, design spec and layout", 1]])
    doc.save(path)
    return str(path)


@pytest.fixture
def folder(tmp_path):
    _report(tmp_path / "AAA_UM_EN-validation-report.pdf", "aaa (en)", 3)
    _report(tmp_path / "BBB_UM_EN-validation-report.pdf", "bbb (en)", 2, result="FAIL", issues=7)
    return tmp_path


def test_every_publication_is_in_the_one_pdf(folder):
    out = bundle.build(folder)
    with pymupdf.open(out) as d:
        contents = sum(1 for t in d.get_toc() if t[0] == 1 and t[1].endswith("contents"))
        assert contents == 1
        # the contents page, then every report in full
        assert d.page_count == d.get_toc()[1][2] - 1 + 3 + 2
        names = [t[1] for t in d.get_toc() if t[0] == 1]
        assert names[1:] == ["AAA_UM_EN — PASS", "BBB_UM_EN — FAIL"]
        first = d[0].get_text()
        for bit in ("aaa (en)", "bbb (en)", "98.50%", "2 × image missing", "FAIL"):
            assert bit in first, first


def test_each_report_keeps_its_own_bookmarks_one_level_deeper(folder):
    out = bundle.build(folder)
    with pymupdf.open(out) as d:
        toc = d.get_toc()
        inner = [t for t in toc if t[0] == 2]
        assert len(inner) == 2 and all(t[1].startswith("Issues —") for t in inner)
        # each one points inside its own report, not at the contents page
        tops = [t[2] for t in toc if t[0] == 1][1:]
        assert [t[2] for t in inner] == tops


def test_the_bundle_is_never_bundled_into_itself(folder):
    out = bundle.build(folder)
    assert out.name == bundle.OUT_NAME and out.exists()
    assert out not in bundle.reports(folder)
    bundle.build(folder)  # again: still two publications, not three
    with pymupdf.open(out) as d:
        assert len([t for t in d.get_toc() if t[0] == 1]) == 3


def test_a_folder_with_no_report_says_so(tmp_path):
    with pytest.raises(FileNotFoundError):
        bundle.build(tmp_path)
