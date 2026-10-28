"""The Figma design-spec table ("Design spec (Figma) — … : expected headings and text") is the spec
the typography findings are raised against - the font, weight, size, line height and colour each style
must have - so the consolidated PDF, the one file the reviewer is handed, keeps it. Each merged part
carries its own copy, so the first one stays and the repeats are taken out
- see pdfval/report/writer.py::strip_typescale.

It is not on a page of its own: it starts at the foot of a summary page and runs over onto the next.
"""
import json

import pymupdf
import pytest

from pdfval.report import writer

STYLES = [
    {"style": "Headline 1", "font": "Poppins", "weight": "Bold", "size": 20, "line_height": 24, "color": "#7231C6"},
    {"style": "Body default", "font": "Roboto", "weight": "Regular", "size": 12, "line_height": 16, "color": "#000000"},
    {"style": "Caption", "font": "Roboto", "weight": "Regular", "size": 11, "line_height": 14, "color": "#757575"},
]


def _result():
    return {"meta": {"baseline": {"path": "/prod/SW272_EN.pdf"}, "candidate": {"path": "/stage/x.pdf"},
                     "typography": {"format": "A4 PDF (View)", "page": [595, 842], "styles": STYLES}}}


def _rows_html(styles):
    return "".join(f"<p>{s['style']}</p><p>{s['font']}</p><p>{s['weight']}</p><p>{s['size']:g} pt</p>"
                   f"<p>{s['line_height']:g} pt</p><p>{s['color']}</p>"
                   f"<p>{s['font']} · {s['weight']} · {s['size']:g} pt (80% of 90 words)</p>" for s in styles)


def _part(path, *, with_table=True, after="Issue 1 — missing text"):
    """A report part: a summary page whose foot holds the table, the table running onto page 2, then a
    real page. Each page carries the footer every report page has."""
    foot = "<p>PDF Parity Report · page N of 3</p><p>SW272_EN vs stage</p>"
    table = (f"<h2>Design spec (Figma) — A4 PDF (View): expected headings and text</h2>{_rows_html(STYLES[:1])}"
             if with_table else "")
    pages = [f"<h1>PDF Report — FAIL</h1><p>Score 99.6%</p><p>KEEP-THIS-SUMMARY-LINE</p>{table}{foot}"]
    if with_table:
        pages.append(_rows_html(STYLES[1:]) + foot)
    pages.append(f"<h2>{after}</h2><p>KEEP-THIS-ISSUE-PAGE</p>{foot}")
    doc = pymupdf.open()
    for body in pages:
        story = pymupdf.Story(body)
        w = pymupdf.DocumentWriter(str(path) + ".part")
        rect = pymupdf.paper_rect("a4-l")
        dev = w.begin_page(rect)
        story.place(rect + (28, 28, -28, -28))
        story.draw(dev)
        w.end_page()
        w.close()
        with pymupdf.open(str(path) + ".part") as one:
            doc.insert_pdf(one)
    doc.save(str(path))
    doc.close()
    return path


def test_the_table_is_in_the_consolidated_file(tmp_path):
    """The design spec itself - what each style must be - is part of the deliverable."""
    _part(tmp_path / "genuine-issues.pdf")
    out = writer.write_consolidated(_result(), tmp_path)
    text = "\n".join(p.get_text() for p in pymupdf.open(out))
    assert text.count("Design spec (Figma)") == 1
    assert "Headline 1" in text and "Bold" in text and "Body default" in text


def test_the_rest_of_the_summary_page_survives(tmp_path):
    """The page the table sits on keeps everything above it, and its footer."""
    _part(tmp_path / "genuine-issues.pdf")
    doc = pymupdf.open(writer.write_consolidated(_result(), tmp_path))
    first = doc[0].get_text()
    assert "PDF Report" in first and "Score 99.6%" in first and "KEEP-THIS-SUMMARY-LINE" in first
    assert "page N of 3" in first, "the page footer must not be redacted away"


def test_one_part_is_merged_whole(tmp_path):
    """Nothing is a repeat yet, so every page of the only part is kept."""
    _part(tmp_path / "genuine-issues.pdf")
    doc = pymupdf.open(writer.write_consolidated(_result(), tmp_path))
    assert doc.page_count == 3, [p.get_text()[:60] for p in doc]
    assert "KEEP-THIS-ISSUE-PAGE" in doc[2].get_text()


def test_a_report_without_the_table_is_untouched(tmp_path):
    _part(tmp_path / "genuine-issues.pdf", with_table=False)
    doc = pymupdf.open(writer.write_consolidated(_result(), tmp_path))
    assert doc.page_count == 2 and "KEEP-THIS-ISSUE-PAGE" in doc[1].get_text()


def test_only_the_repeats_are_removed(tmp_path):
    """Two parts, a copy of the table in each: the first stays, the second goes."""
    _part(tmp_path / "genuine-issues.pdf")
    _part(tmp_path / "image-issues.pdf", after="Issue 2 — picture missing")
    doc = pymupdf.open(writer.write_consolidated(_result(), tmp_path))
    text = "\n".join(p.get_text() for p in doc)
    assert text.count("Design spec (Figma)") == 1
    assert doc.page_count == 5          # 3 kept + (3 - its table page) for the second part
    assert "KEEP-THIS-ISSUE-PAGE" in doc[2].get_text() and "KEEP-THIS-ISSUE-PAGE" in doc[4].get_text()


def test_the_bookmarks_still_land_on_their_part(tmp_path):
    """Dropping pages moves every later part up: a bookmark must follow it."""
    _part(tmp_path / "genuine-issues.pdf")
    _part(tmp_path / "image-issues.pdf", after="Issue 2 — picture missing")
    doc = pymupdf.open(writer.write_consolidated(_result(), tmp_path))
    toc = doc.get_toc()
    assert [t[1] for t in toc] == ["Issues — content, design spec and layout",
                                   "Pictures — numbers, labels, artwork"]
    for _, _, pno in toc:
        assert "PDF Report" in doc[pno - 1].get_text() or "Score" in doc[pno - 1].get_text(), \
            (pno, doc[pno - 1].get_text()[:80])
    assert toc[1][2] == 4, toc   # the second part starts right after the first part's 3 kept pages


def test_a_run_with_no_design_spec_changes_nothing(tmp_path):
    _part(tmp_path / "genuine-issues.pdf")
    result = _result()
    result["meta"]["typography"] = {}
    doc = pymupdf.open(writer.write_consolidated(result, tmp_path))
    assert doc.page_count == 3     # nothing to recognise the table by: left exactly as merged


# --- what the consolidated report is now: the metrics and the issues ---------------------------------

def test_the_consolidated_body_leaves_out_the_spec_table_and_the_toc():
    """The one file per publication answers "what is wrong with this manual". How it was measured -
    the Figma type scale and the entry-by-entry TOC comparison - stays in genuine-issues.pdf."""
    from pdfval.report import pdf_report
    inc = pdf_report.CONSOLIDATED_BODY["include"]
    assert inc["typescale"] is False and inc["toc"] is False and inc["toc_compare"] is False
    assert inc["summary"] is True and inc["issues"] is True      # the metrics and the issues stay
    assert pdf_report.CONSOLIDATED_BODY["filter"]["genuine_only"] is True


def test_a_real_run_s_consolidated_file_holds_the_metrics_and_the_issues(tmp_path):
    """End to end: the spec table and the TOC comparison are not in the consolidated file, the run's
    metrics are, and genuine-issues.pdf still carries both for whoever wants them."""
    from pdfval import engine
    from pdfval.report import writer as w

    def pdf(path, *, heading):
        doc = pymupdf.open()
        pg = doc.new_page(width=595, height=842)
        pg.insert_text((72, 70), heading, fontsize=16, fontname="hebo")
        for k, t in enumerate(("Connect the receiver to the display before you start.",
                               "Power on the computer and the monitor.",
                               "Keep the ventilation openings free so warm air can leave.")):
            pg.insert_text((72, 120 + 18 * k), f"{k + 1}. {t}", fontsize=10, fontname="helv")
        doc.set_toc([[1, heading, 1]])
        doc.save(str(path))
        doc.close()
        return str(path)

    cfg = engine.load_config()
    r = engine.compare(pdf(tmp_path / "SW272_EN.pdf", heading="Getting started"),
                       pdf(tmp_path / "stage.pdf", heading="Getting started now"), cfg)
    out = tmp_path / "run"
    out.mkdir()
    w.write_all(r, str(out), "none")
    merged = out / r["meta"]["consolidated"]
    text = "\n".join(pg.get_text() for pg in pymupdf.open(merged))
    assert "Design spec (Figma)" not in text
    assert "TOC comparison" not in text and "Table of contents: prod vs stage" not in text
    assert "Score" in text                                   # the metrics stay
    assert not (out / "_consolidated-body.pdf").exists()     # the working file is cleaned up


def test_the_merge_time_strip_still_guards_the_fallback():
    """write_consolidated falls back to genuine-issues.pdf when the body cannot be built; the pages
    merged from it are still de-duplicated, which is what the tests above this one cover."""
    assert hasattr(writer, "strip_typescale")


# --- what the consolidated report lists -------------------------------------------------------------

def test_only_content_differences_are_listed():
    """The consolidated file answers one question - does stage say what prod says. Pictures, icons,
    tables, links, bullets, the TOC and the design spec are still found and still written in full to
    genuine-issues.pdf and the image report; they are not in this one."""
    from pdfval.report import pdf_report
    t = set(pdf_report.CONSOLIDATED_TYPES)
    assert pdf_report.CONSOLIDATED_BODY["filter"]["types"] == sorted(t)
    for wanted in ("missing text", "extra text", "changed text", "spacing", "wide gap",
                   "punctuation character", "duplicate content", "content in wrong section",
                   "emphasis", "heading text", "missing section", "extra section"):
        assert wanted in t, wanted


def test_nothing_but_content_is_listed():
    from pdfval.report import pdf_report
    t = set(pdf_report.CONSOLIDATED_TYPES)
    for unwanted in (
            # pictures and icons
            "image smaller", "image bigger", "image alignment", "placement", "broken image",
            "missing image", "image changed", "icon missing", "icon differs", "icon alignment",
            "icon wraps to next line", "callout type differs",
            # tables, links, lists and layout
            "table past margin", "table cell alignment", "missing row", "cell differs",
            "broken link", "missing link", "link lands elsewhere", "page zero",
            "bullet", "bullet marker", "indent", "numbering style", "list alignment",
            # the table of contents and the design spec
            "missing entry", "extra entry", "wrong page", "title differs",
            "spec text-align", "spec font-weight", "spec image alignment"):
        assert unwanted not in t, unwanted
    assert not [x for x in t if x.startswith("spec ")]


def test_the_design_spec_is_out_of_the_file_entirely():
    """Both halves of it: the Figma type scale on the front page, and every "spec ..." finding."""
    from pdfval.report import pdf_report
    inc = pdf_report.CONSOLIDATED_BODY["include"]
    assert inc["typescale"] is False and inc["stylemap"] is False
    assert not [x for x in pdf_report.CONSOLIDATED_TYPES if x.startswith("spec ")]
