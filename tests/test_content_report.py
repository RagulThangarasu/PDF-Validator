"""content.docx - the content review on its own.

It answers one question: does stage SAY what prod says, wherever the saying happens - in the running
text, inside a table, in a callout, on a picture, or in the words of a language copy. So it reports
text missing or added, a heading reworded or set at another level, the language-copy differences
(case, script punctuation, diacritics, raised characters), bold / italic on one side only, hyperlinks
that do not work or arrive elsewhere, a table or a row gone, a picture or its label gone, a picture
drawn tiny against prod's, a callout drawn with another icon, bullets, and how the text is aligned.

Not reported: fonts, colours, sizes, spacing, wrapping, borders, backgrounds, column widths, the TOC
and the design spec - see pdf_report.CONTENT_TYPES.

Every issue states Expected (prod) and Actual (stage), and the manuals' own text goes through verbatim,
whatever script it is written in.
"""
import pytest

from pdfval import engine
from pdfval.report import docx_report, pdf_report, writer

pytest.importorskip("docx")


def _finding(fid, types, message, expected="", actual="", **kw):
    return {"id": fid, "bug": f"Bug_{fid}", "check": kw.get("check", "content"),
            "category": kw.get("category", "content"), "severity": "warning", "message": message,
            "types": list(types), "genuine": True, "critical": False, "issue": kw.get("issue", types[0]),
            "baseline": [{"page": 0, "bbox": [0, 0, 10, 10]}], "candidate": [{"page": 0, "bbox": [0, 0, 10, 10]}],
            "detail": {"expected": expected, "actual": actual}, "color": "#333333"}


def _result(*findings):
    return {"meta": {"name": "Test product", "baseline": {"path": "/prod/x.pdf"}, "candidate": {"path": "/stage/x.pdf"}},
            "summary": {"content": {"match_pct": 99.0, "pass_pct": 95.0}},
            "sections": [{"id": "s1", "title": "Package contents", "findings": list(findings)}]}


def _text(path):
    from docx import Document
    return "\n".join(p.text for p in Document(str(path)).paragraphs)


def _heads(path):
    from docx import Document
    return [p.text for p in Document(str(path)).paragraphs if p.style.name.startswith(("Title", "Heading 1"))]


# --- what it reports ------------------------------------------------------------------------------

def test_the_types_it_covers_are_exactly_the_ones_asked_for():
    assert pdf_report.CONTENT_TYPES == {
        # the text itself, and the sections it sits in
        "missing text", "extra text", "missing section", "extra section",
        "changed text", "heading text", "outline level",
        # a language copy says the same thing in another script: these are its differences
        "case", "case + punctuation", "punctuation character", "diacritic", "superscript character",
        "emphasis",
        # hyperlinks that do not work, or that arrive somewhere else than prod's
        "broken link", "missing link", "link target differs", "link lands elsewhere",
        # how the text is aligned
        "spec text-align", "list alignment", "row alignment",
        # content missing INSIDE a table: a row, a whole table, or what a cell says
        "missing row", "missing table", "cell differs", "table to text",
        # a picture gone from a section, or kept but drawn tiny against prod's
        "missing image", "broken image", "image smaller",
        # a callout the reader is meant to read, and the labels on a picture
        "callout style", "callout icon differs",
        "callout numbers missing", "image label missing", "missing image label",
        # an icon that no longer sits on the same line as its word, or sits off that line
        "icon wraps to next line", "icon alignment", "icon missing inline",
        # a bullet point prod sets that stage does not, or sets with another marker
        "bullet", "bullet marker",
    }


def test_every_type_it_covers_is_actually_reported():
    """A type that is ignored or not genuine would be filtered out before it ever reached the report."""
    cfg = engine.load_config()
    genuine, ignored = set(cfg["genuine"]["types"]), set(cfg["ignore"]["types"])
    for t in pdf_report.CONTENT_TYPES:
        assert t not in ignored, f"{t} is in [ignore] types: never produced"
        assert t in genuine, f"{t} is not a genuine issue: the content report filters it out"


@pytest.mark.parametrize("types,bucket", [
    (["missing text"], 1), (["extra text"], 1), (["missing section"], 1),
    (["broken link"], 2), (["missing link"], 2), (["link target differs"], 2), (["link lands elsewhere"], 2),
    (["spec text-align"], 3), (["list alignment"], 3), (["row alignment"], 3),
    (["emphasis"], 4),
    (["image smaller"], 5),
])
def test_each_issue_lands_in_the_right_part(types, bucket):
    assert docx_report.content_bucket({"types": types}) == bucket


def test_an_issue_with_two_types_is_listed_once():
    assert docx_report.content_bucket({"types": ["emphasis", "missing text"]}) == 1


# --- the file -------------------------------------------------------------------------------------

def test_it_is_written_with_its_own_parts(tmp_path):
    out = writer.write_content_report(_result(
        _finding("001", ["missing text"], "Missing text", "“Press OK”", "Not in stage"),
        _finding("002", ["broken link"], "Broken link", "https://benq.com/a", "404", category="links"),
        _finding("003", ["emphasis"], "Bold differs", "plain in prod", "bold in stage", check="style", category="css"),
        _finding("004", ["list alignment"], "List numbers not aligned", "one left edge", "item 3 further in"),
    ), tmp_path)
    assert out and out.name == "content.docx"
    assert _heads(out) == ["Content report - Test product", "Content — missing and extra (1)",
                           "Hyperlinks — broken and redirected (1)", "Alignment (1)", "Bold / italic (1)"]


def test_it_leaves_out_everything_that_is_not_content(tmp_path):
    """How a thing is DRAWN is not what it says: fonts, colours, borders and column widths stay out,
    while a row or a picture missing is content gone and stays in."""
    out = writer.write_content_report(_result(
        _finding("001", ["missing text"], "Missing text", "“Press OK”", "Not in stage"),
        _finding("002", ["spec font-size"], "Font size differs", category="css", check="style"),
        _finding("003", ["column width"], "Column width differs", category="tables", check="tables"),
        _finding("004", ["cell background"], "Cell background differs", category="tables", check="tables"),
    ), tmp_path)
    text = _text(out)
    assert "missing text" in text and "Press OK" in text
    for gone in ("spec font-size", "column width", "cell background"):
        assert gone not in text, gone


def test_content_missing_inside_a_table_or_a_picture_is_kept(tmp_path):
    """Content is content wherever it is said: in a table row, on a picture, or beside a callout."""
    out = writer.write_content_report(_result(
        _finding("001", ["missing row"], "Table row missing", category="tables", check="tables"),
        _finding("002", ["missing image"], "Image missing in stage", category="images", check="assets"),
        _finding("003", ["callout icon differs"], "Note icon differs", category="css", check="style"),
    ), tmp_path)
    text = _text(out)
    for kept in ("missing row", "missing image", "callout icon differs"):
        assert kept in text, kept


def test_a_run_with_no_content_issue_writes_no_file(tmp_path):
    (tmp_path / "content.docx").write_bytes(b"stale")      # one left from an earlier run
    out = writer.write_content_report(_result(
        _finding("001", ["spec font-size"], "Font size differs", category="css", check="style")), tmp_path)
    assert out is None and not (tmp_path / "content.docx").exists()


def test_every_issue_states_expected_and_actual(tmp_path):
    out = writer.write_content_report(_result(
        _finding("001", ["missing text"], "Missing text", "“Press the OK key”", "Not in stage")), tmp_path)
    text = _text(out)
    assert "Expected (prod):" in text and "Actual (stage):" in text
    assert "Press the OK key" in text


# --- all languages --------------------------------------------------------------------------------

@pytest.mark.parametrize("script,prod,stage", [
    ("Chinese", "按下 OK 鍵以確認設定", "按下 OK 鍵"),
    ("Japanese", "電源ボタンを押してください", "電源ボタンを押す"),
    ("Korean", "전원 버튼을 누르십시오", "전원 버튼"),
    ("Arabic", "اضغط على زر التشغيل للمتابعة", "اضغط على زر التشغيل"),
    ("Thai", "กดปุ่มเปิดปิดเพื่อดำเนินการต่อ", "กดปุ่มเปิดปิด"),
    ("Russian", "Нажмите кнопку питания", "Нажмите кнопку"),
    ("German", "Drücken Sie die Ein-/Aus-Taste größer", "Drücken Sie die Taste"),
])
def test_the_manuals_own_text_survives_in_any_script(tmp_path, script, prod, stage):
    """The report quotes the manual back; a guide in any language must come through unmangled."""
    out = writer.write_content_report(_result(
        _finding("001", ["missing text"], f"Missing text: {prod}", prod, stage)), tmp_path)
    text = _text(out)
    assert prod in text, (script, text[:300])
    assert stage in text, (script, text[:300])
