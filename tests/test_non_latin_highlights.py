"""Highlights on a page written in a dense non-Latin script (CJK, Thai, Arabic, Hebrew, Devanagari).

What buries the text is the outline, not the tint: a border per line cuts straight through glyphs that
fill the line's height, and on a Traditional Chinese page the manual cannot be read at all. Such a page
keeps the background wash and loses the border - the marked words read like text under a highlighter.

The decision is per PAGE, not per document: a regulatory booklet carries the same notice in a dozen
languages, so its English pages still get boxes while its Chinese pages get underlines. The report
screenshots already worked this way (shots.py::_english); the viewer did not.
"""
import types

import pytest

from pdfval import engine

ZH = "安全警告與注意事項本產品必須接地為確保操作安全您必須僅將針腳插頭插入至標準針腳電源插座"
JA = "電源ボタンを押してください設定を確認するために次の手順に従って操作してくださいこちら"
AR = "اضغط على زر التشغيل للمتابعة ثم اتبع التعليمات الظاهرة على الشاشة لإكمال الإعداد بنجاح"
TH = "กดปุ่มเปิดปิดเพื่อดำเนินการต่อจากนั้นทำตามคำแนะนำบนหน้าจอเพื่อตั้งค่าให้เสร็จสมบูรณ์"
EN = "Safety warnings and precautions This product must be earthed To ensure safe operation you must"


def _doc(pages):
    """A stand-in Doc: the detectors only read .words (text + page)."""
    words = [types.SimpleNamespace(text=t, page=p) for p, text in enumerate(pages) for t in text.split() or list(text)]
    return types.SimpleNamespace(words=words)


# --- which pages are non-Latin --------------------------------------------------------------------

@pytest.mark.parametrize("script,text", [("Chinese", ZH), ("Japanese", JA), ("Arabic", AR), ("Thai", TH)])
def test_a_page_in_a_dense_script_is_found(script, text):
    assert engine._non_latin_pages(_doc([text])) == [0], script


def test_an_english_page_is_left_with_its_boxes():
    assert engine._non_latin_pages(_doc([EN])) == []


def test_only_the_pages_that_need_it_are_underlined():
    """The real case: a booklet of the same notice in several languages."""
    assert engine._non_latin_pages(_doc([EN, EN, ZH, EN, JA, EN])) == [2, 4]


def test_a_page_with_a_few_foreign_words_keeps_its_boxes():
    """A model name or a quoted term in an English page is not a non-Latin page."""
    assert engine._non_latin_pages(_doc([EN + " " + EN + " 安全警告"])) == []


def test_a_nearly_empty_page_is_not_judged():
    assert engine._non_latin_pages(_doc(["安全"])) == []


# --- the document-wide flag is sampled across the whole document ----------------------------------

def test_the_document_flag_does_not_judge_by_the_opening_pages():
    """It used to sample the first 4000 words: a booklet that opens in English and turns to Chinese
    was called a Latin document, and every Chinese page was buried under boxes."""
    doc = _doc([EN] * 3 + [ZH] * 20)
    assert engine._non_latin_script(doc) is True


def test_a_document_that_is_mostly_english_is_still_latin():
    assert engine._non_latin_script(_doc([EN] * 20 + [ZH])) is False


# --- the viewer uses it per page ------------------------------------------------------------------

def _viewer_css():
    from pathlib import Path

    from pdfval import report
    return (Path(report.__file__).parent / "viewer.html").read_text(encoding="utf-8")


def test_the_viewer_marks_such_a_page_with_a_background_and_no_border():
    src = _viewer_css()
    assert ".hl.nl:not(.at) { border-width: 0 !important;" in src, "the border must go, not the tint"
    assert "border-width: 0 0 2px 0" not in src, "left over from the underline version"
    for cat in ("content", "style", "layout", "assets", "tables", "integrity", "structure"):
        assert f".hl.nl.{cat}:not(.at) {{ background:" in src, cat


def test_the_viewer_decides_per_page():
    src = _viewer_css()
    assert "this.nonLatin.has(loc.page)" in src, "the box does not consult the page's script"
    for side in ("baseline", "candidate"):
        assert f"DATA.meta.{side}.non_latin_pages" in src, side


def test_the_screenshots_use_the_same_wash():
    """A report screenshot and the viewer must mark the same page the same way."""
    import inspect

    from pdfval.report import shots
    src = inspect.getsource(shots)
    assert "d.rectangle(r, fill=color + (56,))" in src, "the dense branch should tint, not draw a line"
    assert "_WASH_OVER" in src and "_UNDERLINE_OVER" not in src


def test_the_page_list_is_published_in_the_run_metadata():
    import inspect
    src = inspect.getsource(engine._doc_meta)
    assert '"non_latin_pages": _non_latin_pages(d)' in src


# --- how a non-Latin line is marked ----------------------------------------------------------------

ZH_PAGE = "安全警告與注意事項本產品必須接地為確保操作安全您必須僅將針腳插頭插入至標準針腳電源插座"
EN_PAGE = "Safety warnings and precautions. This product must be earthed to ensure safe operation."


def _page_pdf(path, text, font):
    import pymupdf
    doc = pymupdf.open()
    pg = doc.new_page(width=595, height=842)
    for k in range(6):
        pg.insert_text((60, 100 + 28 * k), text, fontsize=11, fontname=font)
    doc.save(str(path))
    doc.close()
    return str(path)


def _marked(tmp_path, text, font):
    """(the rendered crop, the colour used) with the first three lines marked."""
    import pymupdf
    from pdfval.report import shots
    src = _page_pdf(tmp_path / "p.pdf", text, font)
    pc = shots._PageCache(src, 2.0)
    with pymupdf.open(src) as d:
        boxes = [tuple(b[:4]) for b in d[0].get_text("blocks")[:3] if len(b[4].strip()) > 5]
    return shots._crop(pc, 0, boxes, (217, 45, 32), None, (80, 260)).convert("RGB"), shots._english(pc, 0)


def _red_rows(img):
    """How many pixel rows carry red, and how red the reddest row is - a box or a wash paints the
    whole line height, an underline only a few rows."""
    import collections
    rows = collections.Counter()
    px = img.load()
    for y in range(img.height):
        for x in range(0, img.width, 3):
            r, g, b = px[x, y]
            if r > 150 and g < 110 and b < 110:
                rows[y] += 1
    return rows


def test_a_cjk_line_is_underlined_not_washed(tmp_path):
    img, english = _marked(tmp_path, ZH_PAGE, "china-ts")
    assert english is False
    rows = _red_rows(img)
    assert rows, "nothing was marked"
    # three marked lines, a few pixel rows each - not the ~40 rows a line box would paint
    assert len(rows) <= 3 * 12, len(rows)


def test_an_english_line_keeps_its_box(tmp_path):
    img, english = _marked(tmp_path, EN_PAGE, "helv")
    assert english is True
    rows = _red_rows(img)
    assert len(rows) > 3 * 12, len(rows)      # the outline spans each line's full height


def test_a_page_whose_script_does_not_tokenise_is_not_english(tmp_path):
    """Thai and CJK are written without spaces and Arabic arrives as presentation forms: counting
    .isalpha() words left such a page looking English, and it got boxes drawn through it."""
    from pdfval.report import shots
    src = _page_pdf(tmp_path / "th.pdf", ZH_PAGE, "china-ts")
    assert shots._english(shots._PageCache(src, 2.0), 0) is False
