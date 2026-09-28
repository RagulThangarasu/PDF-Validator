"""Other languages: every character is compared (Chinese/Japanese per character), right-to-left
text works, nothing is reported for an identical copy, and text running outside a table cell border
is caught, in prod vs stage pairs built with real scripts."""
import pymupdf
import pytest

from pdfval import compare, load_config

TEXT = {
    "zh": dict(rtl=False, h=["概述", "安装"],
               p=["在打开显示器之前，请将电源线连接到墙上插座。", "保持通风口畅通，以便热空气可以轻松排出。"],
               t=[["设置", "数值"], ["亮度", "这是一个很长的数值会超出表格单元格的边框范围并且继续向右延伸"], ["对比度", "百分之六十"]]),
    "ar": dict(rtl=True, h=["نظرة عامة", "التركيب"],
               p=["قم بتوصيل سلك الطاقة بمقبس الحائط قبل تشغيل الشاشة.", "حافظ على فتحات التهوية مفتوحة حتى يخرج الهواء الساخن بسهولة."],
               t=[["الإعداد", "القيمة"], ["السطوع", "قيمة طويلة جدا تتجاوز حدود الخلية في الجدول"], ["التباين", "ستون بالمائة"]]),
    "ru": dict(rtl=False, h=["Обзор", "Установка"],
               p=["Подключите шнур питания к розетке перед включением дисплея.", "Не закрывайте вентиляционные отверстия, чтобы тёплый воздух выходил."],
               t=[["Параметр", "Значение"], ["Яркость", "очень длинное значение выходит за границу ячейки таблицы"], ["Контраст", "шестьдесят процентов"]]),
}


def build(path, lang, paras=None, overflow=False):
    c = TEXT[lang]
    style = f"font-size:11pt;{'direction:rtl;text-align:right;' if c['rtl'] else ''}"
    doc, toc = pymupdf.open(), []
    for k in range(2):
        pg = doc.new_page(width=595, height=842)
        pg.insert_htmlbox(pymupdf.Rect(60, 60, 535, 90), f"<h1 style='font-size:18pt;{style}'>{c['h'][k]}</h1>")
        toc.append([1, c["h"][k], pg.number + 1])
        if k == 0:
            for n, t in enumerate(paras or c["p"]):
                pg.insert_htmlbox(pymupdf.Rect(60, 110 + 45 * n, 535, 150 + 45 * n), f"<p style='{style}'>{t}</p>")
            continue
        xs = [60, 250, 535] if not c["rtl"] else [60, 345, 535]
        top = 110
        for r, row in enumerate(c["t"]):
            h = 60 if r == 1 else 26
            cells = row if not c["rtl"] else row[::-1]
            for ci, txt in enumerate(cells):
                rect = pymupdf.Rect(xs[ci], top, xs[ci + 1], top + h)
                pg.draw_rect(rect, color=(0, 0, 0), width=0.8)
                long = r == 1 and ci == (0 if c["rtl"] else 1)
                box = rect + (4, 5, -4, 0)
                if overflow and long:  # the same text on one line, past the cell border
                    box = pymupdf.Rect(8, rect.y0 + 5, rect.x1 - 4, rect.y1) if c["rtl"] else \
                        pymupdf.Rect(rect.x0 + 4, rect.y0 + 5, 590, rect.y1)
                nowrap = "white-space:nowrap;" if overflow and long else ""
                pg.insert_htmlbox(box, f"<p style='font-size:10pt;{style}{nowrap}'>{txt}</p>")
            top += h
    doc.set_toc(toc)
    doc.save(path)
    return str(path)


@pytest.fixture
def cfg():
    return load_config()


def genuine(r):
    return [f for s in r["sections"] for f in s["findings"] if f.get("genuine")]


@pytest.mark.parametrize("lang", list(TEXT))
def test_identical_copy_has_no_issue(tmp_path, cfg, lang):
    r = compare(build(tmp_path / "a.pdf", lang), build(tmp_path / "b.pdf", lang), cfg)
    assert genuine(r) == []


@pytest.mark.parametrize("lang", list(TEXT))
def test_text_outside_table_border(tmp_path, cfg, lang):
    r = compare(build(tmp_path / "a.pdf", lang), build(tmp_path / "b.pdf", lang, overflow=True), cfg)
    assert [f for f in genuine(r) if f["issue"] == "Text outside table border"]


def test_chinese_is_compared_per_character(tmp_path, cfg):
    p = TEXT["zh"]["p"]
    a = build(tmp_path / "a.pdf", "zh")
    changed = compare(a, build(tmp_path / "b.pdf", "zh", [p[0], p[1].replace("热", "冷")]), cfg)
    assert [f["message"] for f in genuine(changed)] == ["Changed text: “热” → “冷” in “保持通风口畅通，以便冷空气可以轻松排出。”"]
    missing = compare(a, build(tmp_path / "c.pdf", "zh", [p[0], p[1].replace("热空气", "")]), cfg)
    assert [f["issue"] for f in genuine(missing)] == ["Data missing"] and "热空气" in genuine(missing)[0]["message"]


def test_full_width_punctuation_is_content(tmp_path, cfg):
    """"，" -> "," is a visible change in Chinese text (not folded away as a width variant)."""
    p = TEXT["zh"]["p"]
    r = compare(build(tmp_path / "a.pdf", "zh"), build(tmp_path / "b.pdf", "zh", [p[0], p[1].replace("，", ",")]), cfg)
    assert [f for f in genuine(r) if "，" in f["message"] and "," in f["message"]]


def test_arabic_missing_word(tmp_path, cfg):
    p = TEXT["ar"]["p"]
    r = compare(build(tmp_path / "a.pdf", "ar"), build(tmp_path / "b.pdf", "ar", [p[0], p[1].replace("الساخن ", "")]), cfg)
    assert [f for f in genuine(r) if f["issue"] == "Data missing" and "ساخن" in f["message"]]  # the test font maps "ال" oddly


def _cr(path, raised: bool):
    """“(Cr+6)” with “+6” on the baseline, or raised and smaller (superscript)."""
    doc = pymupdf.open()
    pg = doc.new_page(width=595, height=842)
    pg.insert_text((72, 80), "Overview", fontsize=18, fontname="hebo")
    pg.insert_text((72, 130), "Hexavalent chromium (Cr", fontsize=12, fontname="helv")
    x = 72 + pymupdf.get_text_length("Hexavalent chromium (Cr", "helv", 12)
    if raised:
        pg.insert_text((x, 125), "+6", fontsize=8, fontname="helv")
        x += pymupdf.get_text_length("+6", "helv", 8)
    else:
        pg.insert_text((x, 130), "+6", fontsize=12, fontname="helv")
        x += pymupdf.get_text_length("+6", "helv", 12)
    pg.insert_text((x, 130), ") is restricted in this product.", fontsize=12, fontname="helv")
    doc.set_toc([[1, "Overview", 1]])
    doc.save(path)
    return str(path)


def test_superscript_is_reported(tmp_path, cfg):
    r = compare(_cr(tmp_path / "a.pdf", False), _cr(tmp_path / "b.pdf", True), cfg)
    hits = [f for f in genuine(r) if f["issue"] == "Superscript / subscript differs"]
    assert hits and "(Cr⁺⁶)" in hits[0]["message"] and hits[0]["baseline"] and hits[0]["candidate"]
    assert not genuine(compare(_cr(tmp_path / "c.pdf", True), _cr(tmp_path / "d.pdf", True), cfg))


def _rohs(path, label: str, wrap: bool):
    """A RoHS-style table; the label cell of row 3 wraps over two lines when `wrap`."""
    doc = pymupdf.open()
    pg = doc.new_page(width=595, height=842)
    pg.insert_htmlbox(pymupdf.Rect(60, 60, 535, 90), "<h1 style='font-size:18pt'>台灣RoHS</h1>")
    rows = [["單元", "鉛", "汞", "鎘"], ["塑料外框", "○", "○", "○"], ["後殼", "○", "○", "○"], [label, "－", "○", "○"],
            ["電路板組件", "－", "○", "○"]]
    xs, y = [60, 170, 290, 410, 535], 110
    for r, row in enumerate(rows):
        h = 44 if r == 3 else 26
        for c, txt in enumerate(row):
            rect = pymupdf.Rect(xs[c], y, xs[c + 1], y + h)
            pg.draw_rect(rect, color=(0, 0, 0), width=0.8)
            cell = rect + (4, 4, -4, 0)
            if c > 0:  # values sit in the middle of the row
                cell = pymupdf.Rect(rect.x0 + 4, rect.y0 + (h - 16) / 2, rect.x1 - 4, rect.y1)
            pg.insert_htmlbox(cell, f"<p style='font-size:11pt'>{txt}</p>")
        y += h
    doc.set_toc([[1, "台灣RoHS", 1]])
    doc.save(path)
    return str(path)


def test_changed_row_label_in_wrapped_cell(tmp_path, cfg):
    """Prod “液晶面板” wraps in its cell (“液晶面 / 板”), stage says “液晶螢幕”: one changed-text finding
    with both sides boxed, not an extra row / row split / missing block."""
    r = compare(_rohs(tmp_path / "a.pdf", "液晶面<br/>板", True), _rohs(tmp_path / "b.pdf", "液晶螢幕", False), cfg)
    g = genuine(r)
    assert [f["issue"] for f in g] == ["Text changed"], [f["message"] for f in g]
    assert "面板" in g[0]["message"] and "螢幕" in g[0]["message"] and g[0]["baseline"] and g[0]["candidate"]
