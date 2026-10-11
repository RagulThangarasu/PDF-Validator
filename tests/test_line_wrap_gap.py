"""A gap that differs only where one side breaks the line is layout, not content.

A line break IS the separator: a PDF cannot say whether a space was typed there. Prod wraps
"Support.BenQ." / "com" over two lines where stage sets "Support.BenQ.com" on one, and the extracted
text differs by a space that nobody typed - reported as "Space after a word differs".

_wrap_only already forgave a break after a hyphen, slash or dash (the right rule for deciding whether
a WORD was split). _wrap_gap is the looser rule for a GAP, and is used for spacing alone - two words
that really did lose their space in stage are still reported, as long as prod has them on one line.

Hyphens at a break were already handled both ways (_break_hyphen_only): one added in stage, or one in
prod that stage does not have, is a wrap, not a punctuation difference.
"""
import pymupdf
import pytest

from pdfval import engine


@pytest.fixture(scope="module")
def cfg():
    c = engine.load_config()
    c["typography"]["enabled"] = False
    return c


def _pdf(path, lines):
    doc = pymupdf.open()
    doc.new_page(width=595, height=842)
    pg = doc.new_page(width=595, height=842)
    pg.insert_text((57, 100), "System requirements", fontsize=18, fontname="hebo")
    for n, text in enumerate(lines):
        pg.insert_text((57, 140 + n * 16), text, fontsize=11)
    doc.set_toc([[1, "System requirements", 2]])
    doc.save(path)
    return str(path)


def _types(r, want):
    return [f["message"] for s in r["sections"] for f in s["findings"] if want in (f.get("types") or [])]


def test_a_url_wrapped_in_prod_is_not_a_spacing_difference(tmp_path, cfg):
    """The real case: prod breaks the URL across two lines, stage sets it on one."""
    prod = _pdf(tmp_path / "a.pdf", ["Check whether it can be downloaded from Support.BenQ.",
                                     "com > model name > Software and Driver."])
    stage = _pdf(tmp_path / "b.pdf", ["Check whether it can be downloaded from Support.BenQ.com",
                                      "> model name > Software and Driver."])
    r = engine.compare(prod, stage, cfg)
    assert _types(r, "spacing") == []


def test_it_works_the_other_way_round(tmp_path, cfg):
    """Stage is the side that wraps."""
    prod = _pdf(tmp_path / "a.pdf", ["Check whether it can be downloaded from Support.BenQ.com",
                                     "> model name > Software and Driver."])
    stage = _pdf(tmp_path / "b.pdf", ["Check whether it can be downloaded from Support.BenQ.",
                                      "com > model name > Software and Driver."])
    r = engine.compare(prod, stage, cfg)
    assert _types(r, "spacing") == []


def test_a_space_really_lost_inside_a_line_is_still_reported(tmp_path, cfg):
    """The guard must not hide a genuine missing space: here neither side wraps."""
    prod = _pdf(tmp_path / "a.pdf", ["Connect your laptop to the monitor now."])
    stage = _pdf(tmp_path / "b.pdf", ["Connect yourlaptop to the monitor now."])
    r = engine.compare(prod, stage, cfg)
    assert _types(r, "spacing") or _types(r, "changed text"), "a real missing space must still be found"


def test_a_hyphen_added_at_a_break_in_stage_is_fine(tmp_path, cfg):
    prod = _pdf(tmp_path / "a.pdf", ["Use the InstaShow transmitter with the monitor."])
    stage = _pdf(tmp_path / "b.pdf", ["Use the Insta-", "Show transmitter with the monitor."])
    r = engine.compare(prod, stage, cfg)
    assert _types(r, "punctuation") == [] and _types(r, "spacing") == []


def test_a_hyphen_in_prod_that_stage_does_not_have_is_fine(tmp_path, cfg):
    prod = _pdf(tmp_path / "a.pdf", ["Use the Insta-", "Show transmitter with the monitor."])
    stage = _pdf(tmp_path / "b.pdf", ["Use the InstaShow transmitter with the monitor."])
    r = engine.compare(prod, stage, cfg)
    assert _types(r, "punctuation") == [] and _types(r, "spacing") == []


def test_different_text_at_a_break_is_still_a_content_difference(tmp_path, cfg):
    """Only the GAP is forgiven - the characters still have to match."""
    prod = _pdf(tmp_path / "a.pdf", ["Download it from Support.BenQ.", "com today."])
    stage = _pdf(tmp_path / "b.pdf", ["Download it from Support.Acme.com today."])
    r = engine.compare(prod, stage, cfg)
    assert _types(r, "changed text") or _types(r, "missing text"), "the text itself changed"
