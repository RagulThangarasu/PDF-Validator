"""Findings that were wrong in the audit of real manual pairs (W5800, SL04/SH04, Screenbar Halo 2),
each rebuilt small: the logic that reported them and the fix."""
import io

import numpy as np
import pymupdf
import pytest
from PIL import Image as PILImage

from conftest import all_checks
from pdfval import compare, load_config, normalize
from pdfval.checks import layout
from pdfval.extract import _symbol_map


@pytest.fixture
def cfg():
    c = load_config()
    c["sections"]["front_matter"] = False
    return all_checks(c)  # these tests cover the CSS / layout detectors the default reports leave out


def found(r, check=None):
    return [f for s in r["sections"] for f in s["findings"] if check is None or f["check"] == check]


def _xref_link_pdf(path, stage: bool):
    """“… not be included with the projector (see Shipping contents on page 8). They are …”: prod
    links “Shipping contents on page 8”; stage links “see Shipping contents” with one rectangle
    around both whole lines. Both jump to page 2 (the “Shipping contents” section)."""
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 60), "Connection", fontsize=18, fontname="hebo")
    l1 = "In the connections above, some cables may not be included with the projector (see"
    l2 = "Shipping contents)." if stage else "Shipping contents on page 8)."
    p.insert_text((72, 100), l1, fontsize=10)
    p.insert_text((72, 114), l2 + " They are commercially available from electronics stores.", fontsize=10)
    q = doc.new_page()
    q.insert_text((72, 60), "Shipping contents", fontsize=18, fontname="hebo")
    q.insert_text((72, 90), "The package holds the projector and its cables.", fontsize=10)
    if stage:
        r = pymupdf.Rect(70, 90, 500, 117)  # whole lines
    else:
        r = pymupdf.Rect(72, 104, 72 + pymupdf.get_text_length("Shipping contents on page 8", fontsize=10), 117)
    doc[0].insert_link({"kind": pymupdf.LINK_GOTO, "page": 1, "from": r, "to": pymupdf.Point(72, 50)})
    doc.save(path)
    return str(path)


def test_same_sentence_linked_on_both_sides_is_not_an_extra_link(tmp_path, cfg):
    r = compare(_xref_link_pdf(tmp_path / "a.pdf", False), _xref_link_pdf(tmp_path / "b.pdf", True), cfg)
    bad = [f["message"] for f in found(r, "integrity") if f["detail"].get("kind") in ("extra-link", "missing-link")]
    assert not bad, bad


def _styled_xref_pdf(path, clickable: bool):
    """“See Timing chart.” - the xref purple and underlined; a real link in prod, none in stage."""
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 60), "Troubleshooting", fontsize=18, fontname="hebo")
    p.insert_text((72, 100), "If the picture flickers, lower the input signal. See", fontsize=10)
    x = 72 + pymupdf.get_text_length("If the picture flickers, lower the input signal. See ", fontsize=10)
    w = pymupdf.get_text_length("Timing chart", fontsize=10)
    p.insert_text((x, 100), "Timing chart", fontsize=10, color=(0.45, 0.19, 0.78))
    p.draw_line((x, 102), (x + w, 102), color=(0.45, 0.19, 0.78), width=0.6)
    p.insert_text((x + w, 100), ".", fontsize=10)
    q = doc.new_page()
    q.insert_text((72, 60), "Timing chart", fontsize=18, fontname="hebo")
    q.insert_text((72, 90), "Supported timings for the HDMI input.", fontsize=10)
    if clickable:
        doc[0].insert_link({"kind": pymupdf.LINK_GOTO, "page": 1, "from": pymupdf.Rect(x, 90, x + w, 103),
                            "to": pymupdf.Point(72, 45)})
    doc.save(path)
    return str(path)


def test_link_styled_but_not_clickable_is_named_as_such(tmp_path, cfg):
    r = compare(_styled_xref_pdf(tmp_path / "a.pdf", True), _styled_xref_pdf(tmp_path / "b.pdf", False), cfg)
    f = [f for f in found(r, "integrity") if f["detail"].get("kind") == "missing-link"]
    assert f and f[0]["message"].startswith("Link not clickable in stage: “Timing chart") and "looks like a link" in f[0]["message"], \
        [x["message"] for x in f]
    assert "“Timing chart”" in f[0]["message"].split("goes to")[1] and "plain text" not in f[0]["message"]


def _split_table_pdf(path, repeat_header: bool):
    """A table with a two-row header (“Screen Size | Distance from screen (mm)” over
    “Diagonal | Inches | mm | Min length | Average | Max length”) that breaks across two pages;
    stage repeats the whole header at the top of the second page, prod does not."""
    head = [(110, ["Screen Size", "Distance from screen (mm)"]), (126, ["Diagonal", "Inches", "mm", "Min length", "Average", "Max length"])]
    xs = [72, 150, 210, 270, 360, 450]

    def header(p, y0):
        for dy, cells in head:
            for x, t in zip(xs if len(cells) > 2 else (72, 270), cells):
                p.insert_text((x, y0 + dy - 110), t, fontsize=9, fontname="hebo")

    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 60), "Projection screen size", fontsize=18, fontname="hebo")
    p.insert_text((72, 90), "Find the screen size in the first column and read the distance.", fontsize=10)
    header(p, 110)
    rows = [[str(d), str(d * 25), str(d * 25 + 3), str(d * 33), str(d * 40), str(d * 47)] for d in range(60, 600, 10)]
    y = 146
    for k, r in enumerate(rows):
        if y > 780:
            p = doc.new_page()
            y = 60
            if repeat_header:
                header(p, y)
                y += 36
        for x, t in zip(xs, r):
            p.insert_text((x, y), t, fontsize=9)
        y += 16
    doc.save(path)
    return str(path)


def test_table_header_repeated_on_the_next_page_is_not_extra_content(tmp_path, cfg):
    cfg = {**cfg, "ignore": {}}  # the detector itself (the default [ignore] list leaves it out)
    r = compare(_split_table_pdf(tmp_path / "a.pdf", False), _split_table_pdf(tmp_path / "b.pdf", True), cfg)
    extra = [f["message"] for f in found(r, "content") if f.get("genuine")]
    assert not extra, extra
    # a header repeated on the next page is expected: no finding at all
    assert not [f for f in found(r, "content") if "repeated header" in f["types"]]


def _caption_row_pdf(path, drop: float, stack: bool = False):
    """Four captions side by side under a row of pictures; stage sets the first one `drop` pt lower
    (or, with stack, puts all four in one column - a reflow, not a misalignment)."""
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 60), "Package contents", fontsize=18, fontname="hebo")
    p.insert_text((72, 90), "The package holds the following items.", fontsize=10)
    for k, t in enumerate(["GR10 Mobile Dock", "Quick start guide", "Safety statements", "Warranty card"]):
        x, y = (72, 130 + 20 * k) if stack else (72 + 125 * k, 200 + (drop if k == 0 else 0))
        p.insert_text((x, y), t, fontsize=10)
    p.insert_text((72, 300), "Compatibility", fontsize=14, fontname="hebo")
    p.insert_text((72, 320), "Works with USB-C laptops and tablets.", fontsize=10)
    doc.save(path)
    return str(path)


def test_captions_side_by_side_not_level_in_stage(tmp_path, cfg):
    a = _caption_row_pdf(tmp_path / "a.pdf", 0)
    r = compare(a, _caption_row_pdf(tmp_path / "b.pdf", 19), cfg)
    rows = [f for f in found(r) if "row alignment" in f["types"]
            or any("row alignment" in p["types"] for p in f["detail"].get("parts", []))]
    assert rows and "“GR10 Mobile Dock” 19 pt lower" in rows[0]["message"] and not rows[0]["genuine"]  # layout: not in the PDF report, [f["message"] for f in found(r)]
    # level in stage, or stacked into one column (a reflow): no row finding
    for b in (_caption_row_pdf(tmp_path / "c.pdf", 0), _caption_row_pdf(tmp_path / "d.pdf", 0, stack=True)):
        assert not [f for f in found(compare(a, b, cfg)) if "row alignment" in f["types"]]


def _menu_pdf(path, sub_as_heading: bool):
    """Two menu chapters, each with a "Structure" sub-part: plain text (prod) or a bookmarked
    heading (stage, repeated under every chapter)."""
    doc = pymupdf.open()
    toc = []
    for n, title in enumerate(("Picture menu", "Display menu")):
        p = doc.new_page()
        p.insert_text((72, 80), title, fontsize=18, fontname="hebo")
        toc.append([1, title, doc.page_count])
        p.insert_text((72, 120), "Structure", fontsize=14, fontname="hebo" if sub_as_heading else "helv")
        if sub_as_heading:
            toc.append([2, "Structure", doc.page_count])
        p.insert_text((72, 150), f"The {title.lower()} lists its settings in this order.", fontsize=11)
    doc.set_toc(toc)
    doc.save(path)
    return str(path)


def test_heading_prod_does_not_have_is_not_a_duplicate(tmp_path, cfg):
    """“Structure” twice in stage, 0 times in prod: not “section duplicated”."""
    r = compare(_menu_pdf(tmp_path / "a.pdf", False), _menu_pdf(tmp_path / "b.pdf", True), cfg)
    assert not [f for f in found(r) if "duplicate section" in f["types"]]


def test_wingdings3_arrows_read_as_the_arrows_they_draw():
    m = _symbol_map("ABCDEF+Wingdings3")
    assert m and (m["p"], m["q"], m["t"], m["u"]) == ("▲", "▼", "◄", "►")
    assert _symbol_map("Roboto-Regular") is None


def test_list_markers(cfg):
    is_marker = layout._marker_test(cfg["layout"])
    assert is_marker("•") and is_marker("--") and is_marker("3.") and is_marker("(b)") and is_marker("iv.")
    assert not is_marker("(mm)")   # millimetres, not item 2000 in Roman numerals
    assert not is_marker("◄/►")    # arrow keys starting a wrapped line, not a bullet


class _W:
    def __init__(self, t):
        self.text, self.norm = t, t


def test_cross_reference_page_numbers_are_the_template():
    a = [_W(t) for t in 'see "Controls and functions" on page 12. for details'.split()]
    b = [_W(t) for t in 'see "Controls and functions". for details'.split()]
    doc = type("D", (), {"words": a})()
    assert normalize.fold_xref_pages(doc) == 1
    assert [w.norm for w in a if w.norm] == [w.norm for w in b]
    # an unquoted reference ("Projection dimensions on page 17") has its page number folded too
    c = [_W(t) for t in "see Projection dimensions on page 17.".split()]
    assert normalize.fold_xref_pages(type("D", (), {"words": c})()) == 1
    assert [w.norm for w in c if w.norm] == ["see", "Projection", "dimensions."]


def _note_pdf(path, label: bool):
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 60), "Mounting", fontsize=18, fontname="hebo")
    p.insert_text((72, 90), "Fix the bracket to the wall with the supplied screws.", fontsize=11)
    p.draw_rect(pymupdf.Rect(66, 105, 520, 140), color=(0.5, 0.5, 0.8))
    x = 72
    if label:
        p.insert_text((x, 125), "NOTE:", fontsize=11, fontname="hebo")
        x += 40
    p.insert_text((x, 125), "Use the screws that come with the bracket.", fontsize=11)
    doc.save(path)
    return str(path)


def test_note_label_next_to_an_icon_is_house_style(tmp_path, cfg):
    cfg["genuine"]["everything"] = False  # the selective genuine report: only the listed types
    r = compare(_note_pdf(tmp_path / "a.pdf", False), _note_pdf(tmp_path / "b.pdf", True), cfg)
    f = [f for f in found(r, "content") if "NOTE" in f["message"]]
    assert f and all("label only" in x["types"] and not x["genuine"] for x in f), [x["message"] for x in f]


def _picture(seed: int) -> bytes:
    rng = np.random.default_rng(seed)
    a = (rng.random((24, 32)) > 0.5).astype(np.uint8) * 255
    im = PILImage.fromarray(a).resize((320, 240), PILImage.NEAREST).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _swapped_pictures_pdf(path, swap: bool):
    """A section with two pictures far apart (more than the 80-word pairing window): stage shows
    them the other way round, so the picture at each text spot is the other one."""
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 60), "Safety instructions", fontsize=18, fontname="hebo")
    filler = " ".join(f"Keep the projector away from heat, water and dust at all times, rule {k}." for k in range(12))
    seeds = (2, 1) if swap else (1, 2)
    p.insert_image(pymupdf.Rect(72, 80, 272, 230), stream=_picture(seeds[0]))
    p.insert_textbox(pymupdf.Rect(72, 240, 520, 600), filler, fontsize=10)
    p.insert_image(pymupdf.Rect(72, 610, 272, 760), stream=_picture(seeds[1]))
    doc.save(path)
    return str(path)


def test_pictures_in_another_order_are_paired_by_look(tmp_path, cfg):
    r = compare(_swapped_pictures_pdf(tmp_path / "a.pdf", False), _swapped_pictures_pdf(tmp_path / "b.pdf", True), cfg)
    bad = [f["message"] for f in found(r, "assets") if f["detail"].get("kind") in ("missing", "extra", "changed")]
    assert not bad, bad


def test_pdf_report_order_content_links_formatting_css():
    from pdfval.report.pdf_report import GROUP_ORDER, group_of, select_issues
    f = lambda check, cat: {"check": check, "category": cat, "severity": "info", "message": "m", "types": []}
    s1 = {"id": "s1", "title": "One", "findings": [f("style", "css"), f("layout", "css"), f("integrity", "links"),
                                                   f("content", "content"), f("assets", "images")]}
    s2 = {"id": "s2", "title": "Two", "findings": [f("content", "content"), f("tables", "tables")]}
    got = [(group_of(x), s["id"]) for s, x in select_issues({"sections": [s1, s2]})]
    assert [g for g, _ in got] == sorted((g for g, _ in got), key=GROUP_ORDER.get)
    assert got[:2] == [("content", "s1"), ("content", "s2")] and got[-1] == ("css", "s1")
    assert ("formatting", "s1") in got and len(got) == 7  # layout is formatting, not CSS; nothing dropped


def _package_list(path, rows_b, bold_item: str = ""):
    """Two-column list; different line spacing in the two columns makes the reading order differ."""
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 60), "Package Contents", fontsize=18, fontname="hebo")
    p.insert_text((72, 84), "The package should include the following items.", fontsize=11)
    for x, ys, items in ((72, [110, 129, 140], ["ScreenBar lamp with cord", "Wireless controller", "Quick Start Guide"]),
                         (300, rows_b, ["Warranty information", "Power adapter", "Webcam accessory"])):
        for y, t in zip(ys, items):
            p.insert_text((x, y), f"1 x {t}", fontsize=11, fontname="hebo" if t == bold_item else "helv")
    doc.save(path)
    return str(path)


def test_text_in_another_order_is_not_an_issue_but_bold_vs_plain_is(tmp_path, cfg):
    a = _package_list(tmp_path / "a.pdf", [110, 121, 132])            # read interleaved
    b = _package_list(tmp_path / "b.pdf", [110, 129, 140])            # read row by row
    r = compare(a, b, cfg)
    assert not [f for f in found(r) if {"moved text", "reordered"} & set(f["types"])], \
        [f["message"] for f in found(r) if {"moved text", "reordered"} & set(f["types"])]
    assert not [f for f in found(r, "content") if f["severity"] != "info"]
    bold = compare(a, _package_list(tmp_path / "b2.pdf", [110, 129, 140], bold_item="Webcam accessory"), cfg)
    em = [f for f in found(bold, "content") if "emphasis" in f["types"]]  # bold vs plain: a content issue
    assert em and em[0]["genuine"] and "Webcam" in em[0]["message"] and "plain in prod → bold in stage" in em[0]["message"], \
        [f["message"] for f in found(bold)]


def _nested_list_pdf(path, note_x: float):
    """A numbered item with two bullets under it and a note paragraph after the second bullet,
    starting at `note_x`: 102 = under the bullet text (part of the bullet), 90 = under the numbered
    item's text (part of the item, one level up)."""
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 60), "Safety instructions", fontsize=18, fontname="hebo")
    p.insert_text((72, 100), "1.", fontsize=11)
    p.insert_text((90, 100), "Always place the projector on a level, horizontal surface.", fontsize=11)
    p.insert_text((90, 120), "•", fontsize=11)
    p.insert_text((102, 120), "Do not place the projector on an unstable cart or stand.", fontsize=11)
    p.insert_text((90, 136), "•", fontsize=11)
    p.insert_text((102, 136), "Do not place inflammables near the projector.", fontsize=11)
    p.insert_text((note_x, 156), "Do not use if tilted at an angle of more than 10 degrees.", fontsize=11)
    p.insert_text((72, 186), "2.", fontsize=11)
    p.insert_text((90, 186), "Do not store the projector on end vertically.", fontsize=11)
    doc.set_toc([[1, "Safety instructions", 1]])
    doc.save(path)
    return str(path)


def test_paragraph_under_a_bullet_keeps_its_list_level(tmp_path, cfg):
    prod = _nested_list_pdf(tmp_path / "a.pdf", 102)
    same = compare(prod, _nested_list_pdf(tmp_path / "b.pdf", 102), cfg)
    assert not [f for f in found(same, "layout") if "list level" in f["types"]]
    moved = compare(prod, _nested_list_pdf(tmp_path / "c.pdf", 90), cfg)
    lv = [f for f in found(moved, "layout") if "list level" in f["types"]]
    assert lv and "Do not use if tilted" in lv[0]["message"] and "inflammables" in lv[0]["message"], \
        [f["message"] for f in found(moved, "layout")]
