"""A running footer that names the current section is furniture, not content.

Prod (InDesign) prints the section's own title at the foot of each of its pages. That footer repeats
on those pages only - three of them in a 22-page manual - so the document-wide "same line at the same
height on 30 % of the pages" rule never reaches it, and a PDF without bookmarks gives nothing else to
recognise it by. Left in, the section heading's words are in the word stream four times over, the
other side's heading gets paired with one of the footers, and the finding is reported on a page of
running furniture instead of on the heading (the shot then shows an unrelated page).

[extract] edge_repeat_pages: in the narrow header/footer band, the same text at the same height on
that many pages is furniture whatever the document's length. See pdfval/extract.py.
"""
import pymupdf
import pytest

from pdfval import engine, extract

SECTION = "Calibrating two monitors in a row"
BODY = ["Connect two monitors to a computer properly.", "Power on the computer and the monitors.",
        "Set to extend your screen from the operating system of your computer."]


@pytest.fixture
def cfg():
    c = engine.load_config()
    c["typography"]["enabled"] = False
    return c


def _prod(path, *, footer: bool = True, pages: int = 12) -> str:
    """A 12-page manual whose first three pages are one section: its heading at the top of the first
    and - as prod sets it - the section's title repeated at the foot of all three, alternating left
    and right. Three pages out of twelve is under the document-wide repeat_threshold (30 %), which is
    the whole point: only the narrow-band rule catches it."""
    doc = pymupdf.open()
    for k in range(pages):
        pg = doc.new_page(width=595, height=842)
        if k >= 3:  # the rest of the manual: other content, no footer of this section
            pg.insert_text((72, 120), f"Other content on page {k + 1} of the manual.", fontsize=10, fontname="helv")
            continue
        if k == 0:
            pg.insert_text((72, 62), SECTION, fontsize=16, fontname="hebo")
        for i, t in enumerate(BODY):
            pg.insert_text((72, 120 + 16 * i + 40 * k), t, fontsize=10, fontname="helv")
        if footer:
            pg.insert_text((28 if k % 2 == 0 else 420, 822), SECTION, fontsize=8, fontname="helv")
    doc.save(path)   # no bookmarks: the footer cannot be recognised by the PDF's own chapter list
    return str(path)


def _stage(path) -> str:
    """The same section in stage, its heading numbered and set bold, with no running footer."""
    doc = pymupdf.open()
    pg = doc.new_page(width=595, height=842)
    pg.insert_text((72, 62), "IV. " + SECTION, fontsize=16, fontname="hebo")
    for i, t in enumerate(BODY):
        pg.insert_text((72, 120 + 16 * i), t, fontsize=10, fontname="helv")
    doc.save(path)
    return str(path)


def _where(doc) -> list:
    return [(w.page, round(w.bbox[1])) for w in doc.words if w.text == "Calibrating"]


def test_the_running_footer_is_dropped_and_the_heading_kept(tmp_path, cfg):
    d = extract.load(_prod(tmp_path / "prod.pdf"), "prod", cfg)
    assert _where(d) == [(0, 45)], _where(d)  # the heading only - the three footers are gone


def test_without_the_footer_nothing_changes(tmp_path, cfg):
    d = extract.load(_prod(tmp_path / "plain.pdf", footer=False), "prod", cfg)
    assert _where(d) == [(0, 45)]


def test_the_rule_can_be_turned_off(tmp_path, cfg):
    cfg["extract"]["edge_repeat_pages"] = 0
    d = extract.load(_prod(tmp_path / "prod.pdf"), "prod", cfg)
    assert len(_where(d)) == 4  # the heading and all three footers

def test_a_heading_is_not_paired_with_a_footer_of_the_same_words(tmp_path, cfg):
    """The point of it: with the footers in, prod's heading finding landed on the last footer page."""
    r = engine.compare(_prod(tmp_path / "prod.pdf"), _stage(tmp_path / "stage.pdf"), cfg)
    fs = [f for s in r["sections"] for f in s["findings"] if SECTION in f["message"]]
    assert fs, "the renamed heading is still reported"
    for f in fs:
        assert all(l["page"] == 0 for l in f["baseline"]), f["message"]   # prod's own heading page
    # and the footer text is no longer content missing from stage
    assert not [f for f in fs if "missing text" in (f.get("types") or [])], [f["message"] for f in fs]
