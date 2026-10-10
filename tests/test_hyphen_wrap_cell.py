"""Prod prints a narrow table cell, so long words are cut at the line break and carry the break's hyphen
("con-" | "nect"); the web page wraps elsewhere and writes them whole ("connect"). Across a table a "line"
runs through every cell of the row, so the word after the break in reading order is the NEXT CELL's, not
the rest of the word - joining by reading order alone both leaves the halves apart and glues a neighbour's
word onto the stub. Either way the cell stops matching the page and is reported as data missing."""
import pymupdf
import pytest

from conftest import all_checks
from pdfval import compare, load_config
from pdfval.extract import load

# the cell as the print cuts it, beside the cell to its right - the two share their baselines, as a table row does
CUT = ["Laptop cannot con-", "nect the SSID with the correct pass-", "word by Wi-Fi."]
WHOLE = ["Laptop cannot connect the SSID with the correct password by Wi-Fi.", "", ""]
BESIDE = ["Laptop Wi-Fi module", "cannot support", "802.11 AC."]


def _pdf(path, left, right=BESIDE):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 90), "Troubleshooting", fontsize=18, fontname="hebo")
    y = 130
    for l, r in zip(left, right):
        if l:
            page.insert_text((72, y), l, fontsize=9, fontname="helv")
        if r:
            page.insert_text((300, y), r, fontsize=9, fontname="helv")
        y += 14
    doc.set_toc([[1, "Troubleshooting", 1]])
    doc.save(path)
    return str(path)


@pytest.fixture
def cfg():
    c = load_config()
    c["sections"]["front_matter"] = False
    return all_checks(c)


def _content(r):
    return [f["message"] for s in r["sections"] for f in s["findings"] if f["check"] == "content"]


def test_the_two_halves_are_joined_not_the_next_cell(tmp_path, cfg):
    """The halves of the cut word find each other, and the cell beside the break keeps its own word."""
    doc = load(_pdf(tmp_path / "a.pdf", CUT), "prod", cfg)
    norms = [w.norm for w in doc.words if w.norm]
    assert "con-nect" in norms and "pass-word" in norms, norms
    assert "cannot" in norms and not any("-cannot" in n for n in norms), norms


def test_a_cell_cut_at_line_breaks_is_not_data_missing(tmp_path, cfg):
    r = compare(_pdf(tmp_path / "a.pdf", CUT), _pdf(tmp_path / "b.pdf", WHOLE), cfg)
    assert not _content(r), _content(r)


def test_the_same_holds_when_the_page_is_the_one_that_wraps(tmp_path, cfg):
    r = compare(_pdf(tmp_path / "a.pdf", WHOLE), _pdf(tmp_path / "b.pdf", CUT), cfg)
    assert not _content(r), _content(r)


def test_text_really_missing_from_the_cell_is_still_reported(tmp_path, cfg):
    """The guard must not swallow a real loss: the same cut cell against a page that drops the sentence."""
    gone = ["Forgot the account and password.", "", ""]
    r = compare(_pdf(tmp_path / "a.pdf", CUT), _pdf(tmp_path / "b.pdf", gone), cfg)
    assert any("Laptop" in m or "missing" in m.lower() for m in _content(r)), _content(r)
