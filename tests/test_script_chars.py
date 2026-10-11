"""Raised and lowered characters must be caught in any language, including the ones a font encodes
as their own code point ("Nº", "m²", "H₂O", "USB-C™") rather than as a smaller span above the
baseline - and a report must print them as the reader sees them, not NFKC-folded to "No" / "USB-CTM".
"""
import pymupdf
import pytest

from pdfval import compare, extract, load_config, normalize


def _pdf(path, text, *, rise=0.0, size=11):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 60), "Specifications", fontsize=18, fontname="hebo")
    tw = pymupdf.TextWriter(page.rect)
    font = pymupdf.Font("figo")  # a font with the superscript / subscript glyphs
    tw.append((72, 110), text, font=font, fontsize=size)
    tw.write_text(page)
    doc.save(path)
    return str(path)


@pytest.mark.parametrize("ch, pos", [("²", "^"), ("ⁿ", "^"), ("º", "^"), ("ᵒ", "^"),
                                     ("™", "^"), ("₂", "_"), ("ₒ", "_")])
def test_every_raised_or_lowered_code_point_is_known(ch, pos):
    assert normalize.SCRIPT_CHARS[ch] == pos


def test_code_point_superscript_is_read_as_a_script(tmp_path):
    cfg = load_config()
    doc = extract.load(_pdf(tmp_path / "a.pdf", "Fläche m² und Nº 5"), "baseline", cfg)
    m2 = next(w for w in doc.words if w.text.startswith("m"))
    assert m2.script == ".^" and m2.norm == "m2"  # compared as "m2", still known to be raised
    no = next(w for w in doc.words if w.text.startswith("N"))
    assert no.script == ".^" and no.norm == "No"


def test_raised_on_one_side_only_is_reported_in_any_language(tmp_path):
    """Japanese body text with “m²” in prod and a flat “m2” in stage: NFKC folds both to “m2”, so the
    words match and only the script tells them apart - and it is a difference the reader sees, so it
    is a genuine issue, not the cosmetic “superscript” type that [ignore] types drops."""
    cfg = load_config()
    r = compare(_pdf(tmp_path / "a.pdf", "値は 5 m² です"), _pdf(tmp_path / "b.pdf", "値は 5 m2 です"), cfg)
    found = [f for s in r["sections"] for f in s["findings"] if "superscript character" in (f["types"] or [])]
    assert found, [f["message"] for s in r["sections"] for f in s["findings"]]
    assert "“m²” → “m2”" in found[0]["message"]
    assert found[0]["genuine"]


def test_the_same_character_raised_by_the_typesetter_stays_cosmetic(tmp_path):
    """“m2” with the 2 set raised in prod and flat in stage is the same text: not a genuine issue."""
    cfg = load_config()
    r = compare(_pdf(tmp_path / "a.pdf", "値は 5 m² です"), _pdf(tmp_path / "b.pdf", "値は 5 m² です"), cfg)
    assert not [f for s in r["sections"] for f in s["findings"] if f["check"] == "content"]


def test_report_text_keeps_the_character_the_reader_sees():
    assert normalize.display("USB-C™ H₂O Nº") == "USB-C™ H₂O Nº"
