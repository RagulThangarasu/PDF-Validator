"""Where a section starts, when a heading is read out of order.

engine's pull_back exists for a two-column page: the heading sits in one column and its own text in
the other, and the PDF's text stream holds that text BEFORE the heading. Those words start the
heading's section, or they would be reported missing from it and extra in the one before.

It must not reach a line ABOVE the heading. It used to measure its window as half the heading word's
glyph box, and a glyph box is far taller than the type it holds - a 22pt heading measures 36pt - so
the window cleared a whole line and swallowed the last line of the previous section's paragraph.
That line was then reported "content in the wrong section", CRITICAL, on a document whose text is in
exactly the right place (real case: "Copyright and disclaimer", the tail of the Disclaimer paragraph
pulled into "Servicing").

The window was then measured from the word's OWN box, which fails the same way wherever a font
inflates its boxes: on a CJK page a glyph box is about two and a half times its type, so the line
above a heading still reached past it - see test_engine_synthetic's note-above-a-heading tests. The
rule now compares the two top edges, with a margin of half the word's type size.
"""
import pymupdf
import pytest

from pdfval import engine


def _word(text, x0, y0, x1, y1, size=12.0):
    from pdfval.model import Style, Word
    return Word(text, text.lower(), 0, (x0, y0, x1, y1), Style("Roboto", 400, False, size, "#000000"), 0)


_beside_or_below = engine.beside_or_below   # the predicate pull_back walks on


# the real geometry from the run that showed the bug: a 22pt heading whose glyph box is 36pt tall,
# and the previous paragraph's last line ending 1.8pt above it
HEADING = _word("Servicing", 56.7, 420.7, 156.9, 456.7, size=22.0)
LINE_ABOVE = _word("accessories.", 445.1, 402.9, 517.0, 418.9)


def test_the_line_above_a_heading_does_not_start_its_section():
    assert not _beside_or_below(LINE_ABOVE, HEADING)


def test_the_old_window_would_have_taken_it():
    """Guards the diagnosis: half the heading's glyph box really does clear the line above."""
    old_top = HEADING.bbox[1] - 0.5 * (HEADING.bbox[3] - HEADING.bbox[1])
    assert LINE_ABOVE.bbox[1] >= old_top


def test_text_beside_the_heading_still_starts_its_section():
    """What pull_back is for: the other column, read first, on the heading's own row."""
    beside = _word("Power", 300.0, 425.0, 340.0, 437.0)
    assert _beside_or_below(beside, HEADING)


def test_text_below_the_heading_still_starts_its_section():
    below = _word("Power", 300.0, 470.0, 340.0, 482.0)
    assert _beside_or_below(below, HEADING)


def test_a_line_that_merely_grazes_the_heading_box_is_still_above_it():
    """Two inflated glyph boxes can overlap by a hair without being on the same row."""
    grazing = _word("failure)", 486.5, 405.0, 530.0, 421.5)   # bottom 0.8pt into the heading's box
    assert not _beside_or_below(grazing, HEADING)


# --- end to end ------------------------------------------------------------------------------------

def _pdf(path, tail_on_its_own_line: bool):
    """A page whose Disclaimer paragraph ends just above the Servicing heading."""
    doc = pymupdf.open()
    doc.new_page(width=595, height=842)
    pg = doc.new_page(width=595, height=842)
    pg.insert_text((57, 100), "Copyright and disclaimer", fontsize=22, fontname="hebo")
    pg.insert_text((57, 140), "Disclaimer", fontsize=22, fontname="hebo")
    pg.insert_text((57, 180), "It is the sole responsibility of the user if problems", fontsize=12)
    if tail_on_its_own_line:
        pg.insert_text((57, 196), "occurred due to non-original accessories.", fontsize=12)
    pg.insert_text((57, 230), "Servicing", fontsize=22, fontname="hebo")
    pg.insert_text((57, 270), "Should you have any queries, contact customer support.", fontsize=12)
    doc.set_toc([[1, "Copyright and disclaimer", 2], [2, "Disclaimer", 2], [2, "Servicing", 2]])
    doc.save(path)
    return str(path)


def test_a_paragraph_ending_above_a_heading_is_not_in_the_wrong_section(tmp_path):
    same = _pdf(tmp_path / "a.pdf", True)
    r = engine.compare(same, _pdf(tmp_path / "b.pdf", True), engine.load_config())
    wrong = [f["message"] for s in r["sections"] for f in s["findings"]
             if "content in wrong section" in (f.get("types") or [])]
    assert wrong == [], wrong
