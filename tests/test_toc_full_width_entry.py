"""A TOC title that fills its line up to the page number has no dot leaders: still an entry of its own - not
glued onto the next entry with its page number inside the title."""
import pymupdf
import pytest

from pdfval import engine, extract, toc

DOTS = "." * 60


def _doc(path, rows) -> str:
    """rows: (title, page, leaders?) - the page number set flush right at x=500."""
    doc = pymupdf.open()
    pg = doc.new_page(width=595, height=842)
    pg.insert_text((72, 80), "Table of contents", fontsize=16, fontname="hebo")
    font = pymupdf.Font("helv")
    for k, (title, page, leaders) in enumerate(rows):
        y = 120 + 16 * k
        if leaders:
            text = title + " "
            while font.text_length(text + "." + f" {page}", 10) < 428:
                text += "."
            pg.insert_text((72, y), f"{text} {page}", fontsize=10, fontname="helv")
        else:  # the title runs up to the number: one line of text, a plain space before the number
            pg.insert_text((72, y), f"{title} {page}", fontsize=10, fontname="helv")
    for n in range(3):
        doc.new_page(width=595, height=842).insert_text((72, 80), f"Chapter {n}", fontsize=14, fontname="hebo")
    doc.save(path)
    return str(path)


LONG = "Connecting multiple monitors by Thunderbolt daisy chaining (for the selected monitor models only) now"
ROWS = [("Getting started", 12, True), ("Working with two video sources", 44, True), (LONG, 49, False),
        ("Working with HDR technology", 50, True), ("Troubleshooting", 61, True)]


def _entries(path):
    cfg = engine.load_config()
    return [(e.title, e.page) for e in toc.detect(extract.load(path, "baseline", cfg), cfg).entries]


def test_full_width_title_is_its_own_entry(tmp_path):
    got = _entries(_doc(tmp_path / "a.pdf", ROWS))
    assert (LONG, 49) in got and ("Working with HDR technology", 50) in got and len(got) == 5


def test_a_number_out_of_page_order_is_not_a_page_number(tmp_path):
    # “… version 3” at the end of a wrapped title's first line: 3 does not fit between p.44 and p.50
    rows = [ROWS[0], ROWS[1], (LONG.replace("now", "version"), 3, False), ROWS[3], ROWS[4]]
    got = _entries(_doc(tmp_path / "b.pdf", rows))
    assert len(got) == 4 and not any(p == 3 for _, p in got)  # joined with the next line, as a wrapped title is
