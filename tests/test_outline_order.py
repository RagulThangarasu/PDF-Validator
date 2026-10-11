"""A PDF's outline is not always written in document order: a prod PDF assembled from map topics can
bookmark the chapter on page 3 before the ones on pages 1-2. Locating walks the lines forward, so taken
in the outline's own order every later bookmark is searched past where its heading really is, lands
unlocated, and the whole document collapses into the first chapter (see pdfval/sections.py::_from_outline).
"""
import pymupdf

from pdfval import compare, extract, load_config
from pdfval.sections import build_anchors

CHAPTERS = [("Adjusting the screen resolution", "For the best display performance, set the display to its maximum resolution."),
            ("Preset display mode", "The timing needs to work with the specified input ports."),
            ("Video input", "The color space of the input signal is listed per frame frequency.")]


def _pdf(path, toc):
    doc = pymupdf.open()
    for title, body in CHAPTERS:
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 60), title, fontsize=18, fontname="hebo")
        page.insert_text((72, 110), body, fontsize=10, fontname="helv")
    doc.set_toc(toc)
    doc.save(path)
    return str(path)


def _ordered(path):
    return _pdf(path, [[1, t, n] for n, (t, _) in enumerate(CHAPTERS, start=1)])


def _shuffled(path):
    """The last chapter's bookmark listed first - the order these prod PDFs come in."""
    return _pdf(path, [[1, "Video input", 3], [1, "Adjusting the screen resolution", 1], [1, "Preset display mode", 2]])


def test_outline_out_of_document_order_still_locates_every_heading(tmp_path):
    cfg = load_config()
    doc = extract.load(_shuffled(tmp_path / "a.pdf"), "baseline", cfg)
    anchors = build_anchors(doc, cfg)
    assert [a.title for a in anchors] == [t for t, _ in CHAPTERS]  # document order, not outline order
    assert all(a.located for a in anchors)
    assert [a.page for a in anchors] == [0, 1, 2]


def test_out_of_order_outline_compares_clean_against_the_same_pages(tmp_path):
    """The same three chapters on both sides: a scrambled outline must not report them as missing."""
    cfg = load_config()
    r = compare(_shuffled(tmp_path / "a.pdf"), _ordered(tmp_path / "b.pdf"), cfg)
    assert r["summary"]["content"]["missing_words"] == 0
    assert r["summary"]["content"]["extra_words"] == 0
    assert not [f for s in r["sections"] for f in s["findings"] if f["check"] == "structure"]
