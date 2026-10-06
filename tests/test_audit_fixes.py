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


def _duplicate_link_pdf(path, linked: bool):
    """The same cross-reference text, "Shipping contents", printed twice on one page, both jumping
    to page 2: linked=True for prod (two annotations, same target), plain text both times for stage."""
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 60), "Connection", fontsize=18, fontname="hebo")
    w = pymupdf.get_text_length("Shipping contents", fontsize=10)
    p.insert_text((72, 100), "See Shipping contents for what is in the box.", fontsize=10)
    p.insert_text((72, 120), "Shipping contents lists every included part.", fontsize=10)
    q = doc.new_page()
    q.insert_text((72, 60), "Shipping contents", fontsize=18, fontname="hebo")
    q.insert_text((72, 90), "The package holds the projector and its cables.", fontsize=10)
    if linked:
        x = 72 + pymupdf.get_text_length("See ", fontsize=10)
        doc[0].insert_link({"kind": pymupdf.LINK_GOTO, "page": 1, "from": pymupdf.Rect(x, 90, x + w, 103),
                            "to": pymupdf.Point(72, 50)})
        doc[0].insert_link({"kind": pymupdf.LINK_GOTO, "page": 1, "from": pymupdf.Rect(72, 110, 72 + w, 123),
                            "to": pymupdf.Point(72, 50)})
    doc.save(path)
    return str(path)


def test_same_link_twice_on_a_page_highlights_both_places(tmp_path, cfg):
    r = compare(_duplicate_link_pdf(tmp_path / "a.pdf", True), _duplicate_link_pdf(tmp_path / "b.pdf", False), cfg)
    f = [f for f in found(r, "integrity") if f["detail"].get("kind") == "missing-link"]
    assert len(f) == 1, [x["message"] for x in f]
    assert len({(l["page"], tuple(l["bbox"])) for l in f[0]["baseline"]}) == 2


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
    assert rows and "“GR10 Mobile Dock” 19 pt lower" in rows[0]["message"] and rows[0]["genuine"]  # in the PDF report
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


def test_table_with_row_rules_only_is_read_row_by_row(tmp_path, cfg):
    """“Possible cause | Remedy” drawn with rules between rows only (each rule in two segments, one per cell),
    a heading under it: the left column must not run on into the heading (“…been correctly selected.
    Blurred image”, then “key. Select the correct input …”)."""
    from pdfval import extract
    doc = pymupdf.open()
    p = doc.new_page()
    rows = [("The projector is not", "correctly connected to the", "input signal device."),
            ("The input signal has", "not been correctly", "selected by the user."),
            ("The projection lens is", "not correctly focused", "on the screen surface."),
            ("The remote control", "batteries are out of", "power completely now.")]
    right = ["Check the connection and the cable.", "Select the correct input signal with the key.",
             "Adjust the focus of the projection lens.", "Replace both of the batteries with new ones."]
    y = 80
    for (l1, l2, l3), r in zip(rows, right):
        p.draw_line((40, y - 12), (200, y - 12), width=0.25)
        p.draw_line((200, y - 12), (520, y - 12), width=0.25)
        for k, t in enumerate((l1, l2, l3)):
            p.insert_text((42, y + 13 * k), t, fontsize=10)
        p.insert_text((205, y), r, fontsize=10)
        y += 50
    p.draw_line((40, y - 12), (520, y - 12), width=0.25)
    p.insert_text((60, y + 20), "Blurred image", fontsize=10)
    doc.save(tmp_path / "t.pdf")
    words = [w.text for w in extract.load(str(tmp_path / "t.pdf"), "baseline", cfg).words]
    text = " ".join(words)
    assert "selected by the user. Select the correct input signal with the key." in text, text
    assert text.index("Blurred") > text.index("batteries with new ones."), text


def test_cell_spanning_two_columns_is_read_top_to_bottom(tmp_path, cfg):
    """“Adaptive-Sync | Sets on/off … NOTE Pay attention … switch.”: the Function cell spans two grid columns
    (lower rows have a sub-item column); its short lines (“NOTE”, “switch.”) must not be read before its
    first line just because their centre falls in the first of the two columns."""
    from pdfval import extract
    doc = pymupdf.open()
    p = doc.new_page()
    X = (40, 120, 200, 470, 560)
    def grid_row(y0, y1, cols):
        p.draw_rect(pymupdf.Rect(X[0], y0, X[-1], y1), width=0.5)
        for x in cols:
            p.draw_line((x, y0), (x, y1), width=0.5)
    grid_row(60, 80, (X[1], X[3]))
    p.insert_text((45, 74), "Item", fontsize=9); p.insert_text((125, 74), "Function", fontsize=9); p.insert_text((475, 74), "Range", fontsize=9)
    grid_row(80, 200, (X[1], X[3]))                      # Function spans the sub-item and description columns
    p.insert_text((45, 96), "Adaptive-Sync", fontsize=9)
    p.insert_text((125, 96), "Sets on/off to enable or disable the variable refresh rate (VRR).", fontsize=9)
    p.draw_rect(pymupdf.Rect(140, 110, 460, 150), width=0.5, fill=(0.85, 0.9, 0.95))
    p.insert_text((150, 122), "NOTE", fontsize=8)
    p.insert_text((150, 134), "Pay attention to the on-screen messages before the function", fontsize=8)
    p.insert_text((150, 146), "switch.", fontsize=8)
    p.insert_text((475, 96), "ON", fontsize=9)
    grid_row(200, 240, (X[1], X[2], X[3]))               # a row with all four columns
    p.insert_text((45, 216), "Headphone", fontsize=9); p.insert_text((125, 216), "Volume", fontsize=9)
    p.insert_text((205, 216), "Adjusts the audio volume.", fontsize=9); p.insert_text((475, 216), "0 ~ 100", fontsize=9)
    doc.save(tmp_path / "t.pdf")
    text = " ".join(w.text for w in extract.load(str(tmp_path / "t.pdf"), "candidate", cfg).words)
    assert "Adaptive-Sync Sets on/off" in text and "(VRR). NOTE Pay attention" in text and "function switch." in text, text


def _ports_pdf(path, above: bool):
    """“5. WAN/LAN port”, “6. HDMI port” … - the number beside its item (prod), or a line above it (stage)."""
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 60), "Receiver", fontsize=18, fontname="hebo")
    y = 100
    for n, title in enumerate(["WAN/LAN port", "HDMI port", "Power port", "Lid"], start=5):
        p.insert_text((72, y - (9 if above else 0)), f"{n}.", fontsize=11)
        p.insert_text((90, y), title, fontsize=12, fontname="hebo")
        p.insert_text((90, y + 18), "Connect the Receiver to the matching device for this purpose.", fontsize=11)
        y += 56
    doc.set_toc([[1, "Receiver", 1]])
    doc.save(path)
    return str(path)


def test_list_number_a_line_above_its_item_is_reported(tmp_path, cfg):
    r = compare(_ports_pdf(tmp_path / "a.pdf", False), _ports_pdf(tmp_path / "b.pdf", True), cfg)
    fs = [f for f in found(r) if "marker above" in f["types"]]
    assert len(fs) == 1 and "“5.”, “6.”, “7.”, “8.”" in fs[0]["message"] and fs[0]["genuine"], [f["message"] for f in found(r)]
    assert not [f for f in found(r) if "List number missing" in f["message"]]
    same = compare(_ports_pdf(tmp_path / "c.pdf", False), _ports_pdf(tmp_path / "d.pdf", False), cfg)
    assert not [f for f in found(same) if "marker above" in f["types"]]


def _screenshot_pdf(path, callouts: bool):
    """A screenshot with the callout numbers 1-4 beside it (prod) or without them (stage)."""
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 60), "Password Setup", fontsize=18, fontname="hebo")
    p.insert_text((72, 90), "Enter the old password, the new one twice and press change password.", fontsize=11)
    art = pymupdf.open()
    a = art.new_page(width=300, height=180)
    a.draw_rect(a.rect, fill=(0.2, 0.2, 0.35))
    for k in range(4):
        a.draw_rect(pymupdf.Rect(20, 20 + 38 * k, 180, 44 + 38 * k), color=(0.8, 0.8, 0.9), width=1)
    p.insert_image(pymupdf.Rect(150, 110, 450, 290), stream=a.get_pixmap(dpi=150).tobytes("png"))
    if callouts:
        for k in range(4):
            y = 130 + 38 * k
            p.draw_rect(pymupdf.Rect(128, y - 9, 138, y + 2), color=(0.8, 0.1, 0.1), width=0.6)
            p.insert_text((130, y), str(k + 1), fontsize=8)
            p.draw_line((138, y - 4), (170, y - 4), color=(0.8, 0.1, 0.1), width=0.6)
    p.insert_text((72, 320), "The new password takes effect after the next sign-in.", fontsize=11)
    doc.set_toc([[1, "Password Setup", 1]])
    doc.save(path)
    return str(path)


def test_callout_numbers_of_a_picture_are_not_reported(tmp_path, cfg):
    """The 1-4 callouts with leader lines over prod's screenshot are artwork: stage's picture without them
    is no missing text."""
    r = compare(_screenshot_pdf(tmp_path / "a.pdf", True), _screenshot_pdf(tmp_path / "b.pdf", False), cfg)
    bad = [f for f in found(r, "content") if f["severity"] != "info" and any(t in f["message"] for t in ("“1", "2 3"))]
    assert not bad, [f["message"] for f in bad]


def _arrow_png(up: bool) -> bytes:
    art = pymupdf.open()
    a = art.new_page(width=24, height=24)
    a.draw_rect(a.rect, fill=(0.2, 0.2, 0.3))
    pts = [(5, 15), (12, 8), (19, 15)] if up else [(5, 9), (12, 16), (19, 9)]
    a.draw_polyline(pts, color=(1, 1, 1), width=2.5)
    return a.get_pixmap(dpi=200).tobytes("png")


UI_ROWS = [("Pop-up messages", "Enable/Disable pop-up notifications."),
           ("Expand/Collapse", "Show/Hide the update details."),
           ("Delete", "Tap this to delete the notification."),
           ("Clear All", "Tap this to clear all notifications.")]


def _ui_table_pdf(path, stage: bool):
    """Prod: a header bar and a rule under each row, the icons “⌄ / ⌃” in a column of their own beside
    “Expand/Collapse”. Stage: a bordered two-column table, the icons “⌃ / ⌄” above the name in its cell."""
    down, up = _arrow_png(False), _arrow_png(True)
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 60), "Managing notifications", fontsize=16, fontname="hebo")
    x0, x1, y = 60, 540, 100
    if not stage:
        p.draw_rect(pymupdf.Rect(x0, y, x1, y + 20), color=None, fill=(0.6, 0.6, 0.6))
        p.insert_text((150, y + 14), "UI element", fontsize=10, color=(1, 1, 1))
        p.insert_text((330, y + 14), "Description", fontsize=10, color=(1, 1, 1))
        y += 20
        for name, desc in UI_ROWS:
            if name == "Expand/Collapse":
                p.insert_image(pymupdf.Rect(66, y + 6, 84, y + 24), stream=down)
                p.insert_text((86, y + 20), "/", fontsize=10)
                p.insert_image(pymupdf.Rect(92, y + 6, 110, y + 24), stream=up)
            p.insert_text((150, y + 20), name, fontsize=10)
            p.insert_text((330, y + 20), desc, fontsize=10)
            y += 30
            p.draw_line((x0, y), (x1, y), width=0.4)
    else:
        def cellrow(yy, h, a, b, bold=False):
            p.draw_rect(pymupdf.Rect(x0, yy, x1, yy + h), width=0.6)
            p.draw_line((200, yy), (200, yy + h), width=0.6)
            f = "hebo" if bold else "helv"
            p.insert_text((66, yy + h - 8), a, fontsize=10, fontname=f)
            p.insert_text((206, yy + 16), b, fontsize=10, fontname=f)
        cellrow(y, 24, "UI element", "Description", bold=True)
        y += 24
        for name, desc in UI_ROWS:
            h = 46 if name == "Expand/Collapse" else 26
            if name == "Expand/Collapse":
                p.insert_image(pymupdf.Rect(66, y + 4, 84, y + 22), stream=up)
                p.insert_text((88, y + 18), "/", fontsize=10)
                p.insert_image(pymupdf.Rect(94, y + 4, 112, y + 22), stream=down)
            cellrow(y, h, name, desc)
            y += h
    doc.set_toc([[1, "Managing notifications", 1]])
    doc.save(path)
    return str(path)


def test_rule_only_table_rows_and_icons_are_validated(tmp_path, cfg):
    """A table with rules between its rows only (no cell borders) is a table: its rows are compared, and the
    icons of each row - here swapped (⌄ / ⌃ → ⌃ / ⌄) and set above the text instead of beside it."""
    from pdfval.checks import tables
    a = _ui_table_pdf(tmp_path / "a.pdf", False)
    assert [t for t in tables.detect(pymupdf.open(a)[0]) if len(t[2]) >= 4], "the rule-only table is not detected"
    r = compare(a, _ui_table_pdf(tmp_path / "b.pdf", True), cfg)
    kinds = {t for f in found(r, "tables") for t in f["types"]}
    assert {"icon order", "icon above text"} <= kinds, [f["message"] for f in found(r, "tables")]
    assert not kinds & {"missing row", "extra row", "rows merged", "row split"}, [f["message"] for f in found(r, "tables")]


def test_text_on_a_picture_is_left_out_of_the_comparison(tmp_path, cfg):
    """“a b c d” callouts and port names on a connection diagram are the picture's artwork: never compared
    (no missing text, no font differences); the sentence under the picture is still content."""
    from pdfval import extract, genuine
    doc = pymupdf.open()
    p = doc.new_page()
    art = pymupdf.open()
    a = art.new_page(width=300, height=160)
    a.draw_rect(a.rect, fill=(0.95, 0.95, 0.95))
    a.draw_circle((150, 80), 50, color=(0, 0, 0))
    p.insert_image(pymupdf.Rect(100, 80, 400, 240), stream=a.get_pixmap(dpi=100).tobytes("png"))
    for k, t in enumerate("abcd"):
        p.insert_text((150 + 40 * k, 200), t, fontsize=9)
    p.insert_text((130, 120), "USB-C port", fontsize=8)
    p.insert_text((72, 270), "Before making any wired connections, use the correct cable for each source.", fontsize=11)
    doc.save(tmp_path / "d.pdf")
    D = extract.load(str(tmp_path / "d.pdf"), "baseline", cfg)
    genuine.skip_picture_text(D, cfg)
    kept = " ".join(w.text for w in D.words if w.norm)
    assert "Before making any wired connections" in kept
    assert not any(w.norm for w in D.words if w.text in ("a", "b", "c", "d", "USB-C", "port")), kept


def _hyphen_note_pdf(path, stage: bool):
    """Prod: “… ceiling mount components/equip-” | “ment. When …” (hyphenated at the line end). Stage: “…
    components/” | “equipment. When …”, its bullet stored as a line of its own 1 pt lower than the text."""
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 60), "Choosing a location", fontsize=16, fontname="hebo")
    p.insert_text((72, 100), "Select this location with the projector elevated from the ceiling.", fontsize=10)
    if stage:
        p.insert_text((80, 131), "•", fontsize=10)
        p.insert_text((90, 130), "The projector does not feature ceiling mount components/", fontsize=10)
        p.insert_text((90, 144), "equipment. When choosing a ceiling location, place it on a shelf.", fontsize=10)
    else:
        p.insert_text((80, 130), "•", fontsize=10)
        p.insert_text((90, 130), "The projector does not feature ceiling mount components/equip-", fontsize=10)
        p.insert_text((90, 144), "ment. When choosing a ceiling location, place it on a shelf.", fontsize=10)
    doc.set_toc([[1, "Choosing a location", 1]])
    doc.save(path)
    return str(path)


def test_hyphen_break_and_low_bullet_are_not_punctuation(tmp_path, cfg):
    r = compare(_hyphen_note_pdf(tmp_path / "a.pdf", False), _hyphen_note_pdf(tmp_path / "b.pdf", True), cfg)
    bad = [f for f in found(r, "content") if f["severity"] != "info"]
    assert not bad, [f["message"] for f in bad]


def test_image_report_has_the_missing_callout_numbers(tmp_path, cfg):
    """The 1-4 callouts (with leader lines) prod's screenshot has and stage's lacks: an entry of the image report
    (prod and stage picture only), never a content issue."""
    from pdfval.report import image_report
    cfg["ignore"]["cover_pages"] = False  # the test PDF is one page: its first page is no cover
    r = compare(_screenshot_pdf(tmp_path / "a.pdf", True), _screenshot_pdf(tmp_path / "b.pdf", False), cfg)
    rows = image_report.issues(r)
    assert any("Callout numbers missing" in name for _, _, name in rows), [f["message"] for s in r["sections"]
                                                                           for f in s.get("image_findings") or []]
    out = image_report.build(r, tmp_path)
    assert pymupdf.open(out).page_count == len(rows)


def _button_receiver_pdf(path, stage: bool):
    """“Button” then “Receiver”. In stage the Button text wraps so that a body line is just “Receiver.” - above
    the real “Receiver” heading (bold, the same size as the body, as AEM sets sub-headings)."""
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 60), "Button", fontsize=12, fontname="hebo")
    p.insert_text((72, 84), "1. Present key: press to start or stop presenting from this device.", fontsize=12)
    if stage:
        p.insert_text((72, 104), "2. Split screen key: switch to split-screen mode, or pair with an InstaShow", fontsize=12)
        p.insert_text((72, 120), "Receiver.", fontsize=12)
    else:
        p.insert_text((72, 104), "2. Split screen key: switch to split-screen mode, or pair with an InstaShow Receiver.",
                      fontsize=12)
    p.insert_text((72, 144), "3. USB-C connector: connect to a computer or laptop for power and video.", fontsize=12)
    p.insert_text((72, 164), "4. Reset: poke the reset hole to reset the device if it stops responding.", fontsize=12)
    p.insert_text((72, 240), "Receiver", fontsize=12, fontname="hebo")
    p.insert_text((72, 264), "1. Standby button: press to put the Receiver in standby or wake it up again.", fontsize=12)
    p.insert_text((72, 284), "2. Power switch: slide to power the Receiver on or off before moving it.", fontsize=12)
    doc.set_toc([[1, "Button", 1, {"kind": pymupdf.LINK_GOTO, "page": 0, "to": pymupdf.Point(72, 48)}],
                 [1, "Receiver", 1, {"kind": pymupdf.LINK_GOTO, "page": 0, "to": pymupdf.Point(72, 228)}]])
    doc.save(path)
    return str(path)


def test_body_line_that_reads_like_the_next_heading_does_not_start_the_section(tmp_path, cfg):
    """Stage's “…pair with an InstaShow / Receiver.” is body text of “Button”: the “Receiver” section starts at
    its heading (where the bookmark lands), so items 3-4 are not “content in the wrong section”."""
    from pdfval import extract, sections
    b = _button_receiver_pdf(tmp_path / "b.pdf", True)
    doc = extract.load(b, "candidate", cfg)
    an = {x.title: x for x in sections._from_outline(doc, cfg["sections"])}
    assert doc.words[an["Receiver"].word].text == "Receiver" and an["Receiver"].y > 200, \
        (doc.words[an["Receiver"].word].text, an["Receiver"].y)   # the heading (y 230), not the body line (y 110)



def test_layout_diagram_grid_is_a_figure_not_a_table():
    """Boxes holding one code each - “(1.1)” “(2.1)”, “(H+1.V=1)”, or the displays numbered 1-9 - are a layout
    diagram: its text is artwork (image report), never missing content / table rows in the PDF report. A data
    table (words, values, a header) is not."""
    from pdfval.checks.tables import diagram_grid
    assert diagram_grid(["(1.1)", "(2.1)", "(1.2)", "(2.2)"], 4)
    assert diagram_grid(["(H=1.V=1)", "(H+1.V=1)", "(H=1.V+1", "(H+1.V+1"], 4)
    assert diagram_grid(["1", "2", "3", "4", "5", "6", "7", "8", "9"], 9)
    assert not diagram_grid(["Resolution", "Frame", "640x480", "60", "V"], 6)      # a timing table
    assert not diagram_grid(["Model", "SL4304", "Storage", "64", "GB"], 4)          # a spec table
    assert not diagram_grid(["640x480", "60", "V", "V"], 4)                         # values, not codes


def test_callout_numbers_over_a_line_drawing_are_picture_text(tmp_path, cfg):
    """A monitor's front view drawn with straight lines only (no curves): still a drawing. The “1” “1” over it
    and the “2” under it are its callouts - out of the content comparison (image report); the caption
    “(PD2720U)” above and the list under it stay text."""
    from pdfval import extract, genuine
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 60), "Front view", fontsize=16, fontname="hebo")
    p.insert_text((150, 100), "(PD2720U)", fontsize=10)
    p.draw_rect(pymupdf.Rect(110, 130, 250, 220), width=0.8)              # the screen
    p.draw_rect(pymupdf.Rect(114, 134, 246, 216), width=0.4)
    p.draw_line((180, 220), (180, 262), width=0.8); p.draw_line((188, 220), (188, 262), width=0.8)   # the stand
    p.draw_line((150, 262), (218, 262), width=0.8); p.draw_line((150, 266), (218, 266), width=0.8)
    for x in (128, 232):                                                    # leader stubs with a “1” on top
        p.draw_line((x, 122), (x, 130), width=0.4)
        p.insert_text((x - 2, 120), "1", fontsize=8)
    p.insert_text((182, 276), "2", fontsize=8)
    p.insert_text((72, 320), "1. Speakers", fontsize=10)
    p.insert_text((72, 336), "2. Power LED indicator", fontsize=10)
    doc.save(tmp_path / "m.pdf")
    D = extract.load(str(tmp_path / "m.pdf"), "baseline", cfg)
    genuine.skip_picture_text(D, cfg)
    kept = [w.text for w in D.words if w.norm]
    assert "(PD2720U)" in kept and "Speakers" in kept and "1." in kept, kept
    assert [w.text for w in D.words if not w.norm and w.bbox[1] < 300] and "1" not in kept and "2" not in kept, kept


def _model_cell_pdf(path, lines):
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 60), "Touch function", fontsize=16, fontname="hebo")
    p.insert_text((72, 90), "The models listed below support the touch function of the display.", fontsize=10)
    for n, t in enumerate(lines):
        p.insert_text((72, 120 + 12 * n), t, fontsize=10)
    p.insert_text((72, 220), "Contact your dealer when the touch function does not respond.", fontsize=10)
    doc.save(path)


def test_model_list_wrapping_elsewhere_is_not_missing_data(tmp_path, cfg):
    """A list of model codes in a narrow cell breaks wherever the cell ends (“RP700/R” | “P701/RP552”); stage
    breaks it after the slashes. Same text on other lines: nothing missing. With one model really gone in
    stage, only that part is reported - not the whole list as a missing content block."""
    prod = ["RP551/RP651/RP700/R", "P701/RP552/RP652/RP", "702/RP705/RP790/RP8", "40G/RP553K/", "RP653K/RP654K/RP704", "K/"]
    _model_cell_pdf(tmp_path / "a.pdf", prod)
    _model_cell_pdf(tmp_path / "b.pdf", ["RP551/RP651/RP700/RP701/", "RP552/RP652/RP702/RP705/", "RP790/RP840G/RP553K/",
                                         "RP653K/RP654K/RP704K/"])
    r = compare(str(tmp_path / "a.pdf"), str(tmp_path / "b.pdf"), cfg)
    assert not found(r, "content"), [f["message"] for f in found(r, "content")]

    _model_cell_pdf(tmp_path / "c.pdf", ["RP551/RP651/RP700/RP701/", "RP552/RP652/RP702/", "RP790/RP840G/RP553K/",
                                         "RP653K/RP654K/RP704K/"])
    r = compare(str(tmp_path / "a.pdf"), str(tmp_path / "c.pdf"), cfg)
    fs = found(r, "content")
    assert len(fs) == 1 and not fs[0].get("critical"), [f["message"] for f in fs]
    assert "RP705" in fs[0]["detail"]["baseline_text"] and "RP551" not in fs[0]["detail"]["baseline_text"], fs[0]["message"]


def _label_cell_pdf(path, inner: bool):
    """One tall label cell beside three bordered parts (a menu table running on over a page)."""
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 50), "Advanced menu", fontsize=16, fontname="hebo")
    p.draw_rect(pymupdf.Rect(60, 70, 520, 220), width=0.6)
    p.draw_line((150, 70), (150, 220), width=0.6)
    p.insert_text((66, 145), "Color Setting", fontsize=9, fontname="hebo")
    parts = [("Temperature Tuning", "Used for fine-tuning the white balance of the picture."),
             ("Color Management", "Provides eight sets of colors to be adjusted one by one."),
             ("Wide Color Gamut", "Complements the color gamut for playing HDR movies.")]
    for n, (head, text) in enumerate(parts):
        y = 90 + 48 * n
        p.insert_text((156, y), head, fontsize=9, fontname="hebo")
        p.insert_text((156, y + 16), text, fontsize=9)
        if inner and n:
            p.draw_line((150, y - 14), (520, y - 14), width=0.6)
    doc.save(path)


def test_line_between_two_cells_missing_in_stage_is_reported(tmp_path, cfg):
    """Prod parts a tall cell into three with lines; stage draws none of them: one finding naming the texts the
    missing lines belong between. With the lines drawn in stage: nothing."""
    _label_cell_pdf(tmp_path / "a.pdf", True)
    _label_cell_pdf(tmp_path / "b.pdf", False)
    _label_cell_pdf(tmp_path / "c.pdf", True)
    fs = [f for f in found(compare(str(tmp_path / "a.pdf"), str(tmp_path / "b.pdf"), cfg), "tables") if f["types"] == ["cell border"]]
    assert len(fs) == 1 and len(fs[0]["baseline"]) == 2 and len(fs[0]["candidate"]) == 2, fs
    assert "Color Management" in fs[0]["message"] and "Wide Color Gamut" in fs[0]["message"]
    same = [f for f in found(compare(str(tmp_path / "a.pdf"), str(tmp_path / "c.pdf"), cfg), "tables") if f["types"] == ["cell border"]]
    assert not same, same


def _accessories_pdf(path, level: bool):
    """Three pictures of different heights side by side, a caption under each."""
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 60), "Shipping contents", fontsize=16, fontname="hebo")
    p.insert_text((72, 84), "Verify that you have all of the items shown below.", fontsize=10)
    for x, h, name in ((80, 70, "Projector"), (240, 90, "Remote control"), (400, 40, "Power cord")):
        top = 110 + (90 - h) / 2 if level else 110
        p.draw_rect(pymupdf.Rect(x, top, x + 90, top + h), width=0.8)
        p.draw_oval(pymupdf.Rect(x + 20, top + 8, x + 70, top + h - 8), width=0.8)
        p.insert_text((x + 10, 225 if level else top + h + 16), name, fontsize=10)
    doc.save(path)


def test_captions_of_a_picture_row_not_level_in_stage(tmp_path, cfg):
    """Prod sets the captions of a row of pictures on one line; in stage each hangs under its own picture, so the
    caption of the low picture sits far above the others: reported. Same layout on both sides: nothing."""
    _accessories_pdf(tmp_path / "a.pdf", True)
    _accessories_pdf(tmp_path / "b.pdf", False)
    fs = [f for f in found(compare(str(tmp_path / "a.pdf"), str(tmp_path / "b.pdf"), cfg)) if f["types"] == ["caption row"]]
    assert len(fs) == 1 and "Power cord" in fs[0]["message"] and "higher" in fs[0]["message"], fs
    assert not [f for f in found(compare(str(tmp_path / "a.pdf"), str(tmp_path / "a.pdf"), cfg)) if f["types"] == ["caption row"]]


def test_bullet_at_the_top_of_many_pages_is_not_a_running_header(tmp_path, cfg):
    """A list running over many pages starts each page with a bullet at the same height. That bullet is the
    item's, not a running header: it stays in the text (stripped, it was reported as a bullet missing)."""
    from pdfval import extract
    doc = pymupdf.open()
    names = ["Power", "Water", "Heat", "Dust", "Cables", "Batteries", "Mounting", "Cleaning"]
    for n in range(8):
        p = doc.new_page()
        p.insert_text((40, 37), chr(0x2022), fontsize=10)  # set as its own line, as the stage PDF does
        p.insert_text((80, 40), names[n] + " is the subject of this safety item.", fontsize=10)
        p.insert_text((40, 300), names[n] + " again, further down the page.", fontsize=10)
    doc.save(tmp_path / "m.pdf")
    D = extract.load(str(tmp_path / "m.pdf"), "baseline", cfg)
    # (the built-in font prints the bullet as a middle dot)
    assert sum(1 for w in D.words if len(w.text) == 1 and not w.text.isalnum()) == 8, [w.text for w in D.words][:12]
