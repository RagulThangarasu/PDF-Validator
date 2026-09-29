"""Findings that were wrong in the audit of real manual pairs (W5800, SL04/SH04, Screenbar Halo 2),
each rebuilt small: the logic that reported them and the fix."""
import io

import numpy as np
import pymupdf
import pytest
from PIL import Image as PILImage

from pdfval import compare, load_config, normalize
from pdfval.checks import layout
from pdfval.extract import _symbol_map


@pytest.fixture
def cfg():
    c = load_config()
    c["sections"]["front_matter"] = False
    return c


def found(r, check=None):
    return [f for s in r["sections"] for f in s["findings"] if check is None or f["check"] == check]


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
    # a callout reference is not a quoted cross-reference: kept
    c = [_W(t) for t in "see 7 on page 10.".split()]
    assert normalize.fold_xref_pages(type("D", (), {"words": c})()) == 0


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
