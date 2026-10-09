"""A heading's bookmark page number is occasionally wrong (pages inserted/removed after the PDF's
bookmarks were authored): the real heading text sits further along - build_anchors must still find it
and locate the anchor there, instead of planting it on the wrong page and swallowing every page in
between into the wrong section (see pdfval/sections.py::_from_outline)."""
import pymupdf

from pdfval import extract, load_config
from pdfval.sections import build_anchors


def _pdf(path):
    doc = pymupdf.open()
    cover = doc.new_page()
    cover.insert_text((72, 100), "SW272 Series", fontsize=28)
    p1 = doc.new_page()
    p1.insert_text((72, 60), "Navigating the main menu", fontsize=18, fontname="hebo")
    p1.insert_text((72, 100), "Display menu on page 56", fontsize=10)
    p1.insert_text((72, 114), "GamutDuo menu on page 63", fontsize=10)
    for _ in range(3):
        doc.new_page()  # filler pages the bookmark wrongly points into
    p5 = doc.new_page()
    p5.insert_text((72, 60), "Working with two sets of color settings on the same image (GamutDuo)", fontsize=16, fontname="hebo")
    p5.insert_text((72, 100), "GamutDuo helps improve your image editing efficiency.", fontsize=10)
    p6 = doc.new_page()
    p6.insert_text((72, 60), "Troubleshooting", fontsize=18, fontname="hebo")
    p6.insert_text((72, 100), "If the picture flickers, lower the input signal.", fontsize=10)
    # the bookmark for GamutDuo wrongly points at page 2 (1-based) - "Navigating the main menu" -
    # instead of its real page 6 (1-based)
    doc.set_toc([[1, "Navigating the main menu", 2],
                [1, "Working with two sets of color settings on the same image (GamutDuo)", 2],
                [1, "Troubleshooting", 7]])
    doc.save(path)
    return str(path)


def test_wrong_bookmark_page_still_locates_the_real_heading(tmp_path):
    path = _pdf(tmp_path / "a.pdf")
    cfg = load_config()
    doc = extract.load(path, "baseline", cfg)
    anchors = build_anchors(doc, cfg)
    gamutduo = next(a for a in anchors if "gamutduo" in a.norm and "navigating" not in a.norm)
    assert gamutduo.located, "the real heading text must be found, not just planted on the wrong bookmarked page"
    assert gamutduo.page == 5, f"expected page 5 (0-based), got {gamutduo.page}"
