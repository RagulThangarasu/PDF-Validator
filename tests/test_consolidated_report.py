"""The consolidated report: one PDF per run, named after the prod PDF, with every issue report merged
into it and each part reachable as a bookmark."""
import pymupdf

from pdfval.report import writer


def _stub(tmp_path, *names, baseline="EW-version_UM_EN_safety.pdf"):
    """A run directory holding the given reports, each a one-page PDF with its own name printed on it."""
    for n in names:
        doc = pymupdf.open()
        doc.new_page().insert_text((72, 100), f"this is {n}", fontsize=14)
        doc.save(str(tmp_path / n))
        doc.close()
    return {"meta": {"baseline": {"path": f"/prod/{baseline}"}, "candidate": {"path": "/stage/x.pdf"}}}


def test_every_report_is_merged_under_the_pdf_name(tmp_path):
    result = _stub(tmp_path, "genuine-issues.pdf", "css-issues.pdf", "image-issues.pdf", "site.pdf")
    out = writer.write_consolidated(result, tmp_path)
    assert out == tmp_path / "EW-version_UM_EN_safety-validation-report.pdf" and out.exists()
    doc = pymupdf.open(out)
    assert doc.page_count == 4
    text = "\n".join(p.get_text() for p in doc)
    for n in ("genuine-issues.pdf", "css-issues.pdf", "image-issues.pdf", "site.pdf"):
        assert f"this is {n}" in text, n
    # each part is a bookmark, in the order the reader works through them
    assert [page for _, _, page in doc.get_toc()] == [1, 2, 3, 4]


def test_missing_parts_are_skipped_not_faked(tmp_path):
    """A run with no pictures and no CSS report still gets a consolidated file - of the parts it has."""
    result = _stub(tmp_path, "genuine-issues.pdf", "site.pdf")
    out = writer.write_consolidated(result, tmp_path)
    doc = pymupdf.open(out)
    assert doc.page_count == 2
    assert [t for _, t, _ in doc.get_toc()] == ["Issues — content, design spec and layout",
                                                "Site checks — navigation, links, layout, CSS per page"]


def test_a_clean_run_writes_no_consolidated_report(tmp_path):
    assert writer.write_consolidated({"meta": {"baseline": {"path": "/prod/x.pdf"}}}, tmp_path) is None
    assert not list(tmp_path.glob("*-validation-report.pdf"))


def test_rebuilding_does_not_merge_the_previous_copy_into_itself(tmp_path):
    """Running twice into the same directory must not fold the last consolidated file into the new one."""
    result = _stub(tmp_path, "genuine-issues.pdf", "site.pdf")
    first = pymupdf.open(writer.write_consolidated(result, tmp_path)).page_count
    second = pymupdf.open(writer.write_consolidated(result, tmp_path)).page_count
    assert first == second == 2


def test_an_awkward_pdf_name_still_makes_one_safe_file_name(tmp_path):
    result = _stub(tmp_path, "genuine-issues.pdf", baseline="EW/version: safety?.pdf")
    out = writer.write_consolidated(result, tmp_path)
    assert out.exists() and out.name.endswith("-validation-report.pdf")
    assert not set(out.stem) & set('/:?*"<>|')
