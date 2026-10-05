"""A hyphen added where a word breaks at the end of a line (a narrow table cell: “(Insta-” | “Show™)”)
is layout, not punctuation - but it does not hide a word that really changed."""
import pymupdf
import pytest

from pdfval import compare, load_config


@pytest.fixture
def cfg():
    c = load_config()
    c["typography"]["enabled"] = False
    return c


def _doc(path, lines) -> str:
    doc = pymupdf.open()
    pg = doc.new_page(width=595, height=842)
    pg.insert_text((72, 80), "Troubleshooting", fontsize=16, fontname="hebo")
    y = 120
    for t in lines:
        pg.insert_text((72, y), t, fontsize=10, fontname="helv")
        y += 14
    doc.set_toc([[1, "Troubleshooting", 1]])
    doc.save(path)
    return str(path)


def _punct(r):
    return [f for s in r["sections"] for f in s["findings"] if "punctuation" in f.get("types", [])]


def test_line_break_hyphen_is_not_punctuation(tmp_path, cfg):
    a = _doc(tmp_path / "a.pdf", ["After connecting the Button to your laptop, the second screen (Insta-",
                                  "Show) cannot be detected by the computer."])
    b = _doc(tmp_path / "b.pdf", ["After connecting the Button to your laptop, the second screen",
                                  "(InstaShow) cannot be detected by the computer."])
    assert _punct(compare(a, b, cfg)) == []


def test_line_break_hyphen_does_not_hide_a_changed_word(tmp_path, cfg):
    a = _doc(tmp_path / "a.pdf", ["After connecting the Button to your laptop, the second screen (Insta-",
                                  "Show) cannot be detected by the computer."])
    b = _doc(tmp_path / "b.pdf", ["After connecting the Button to your laptop, the second screen",
                                  "(InstaShare) cannot be detected by the computer."])
    r = compare(a, b, cfg)
    assert any("InstaShare" in f["message"] for s in r["sections"] for f in s["findings"])


def _spacing(r):
    return [f for s in r["sections"] for f in s["findings"] if "spacing" in f.get("types", [])]


def test_model_code_list_wrapped_mid_code_is_not_spacing(tmp_path, cfg):
    """A narrow table cell forces "T420/T650/TL550/TL650/" to break mid-code with no separator: the
    break itself introduces nothing (prod) - same codes, just wrapped differently."""
    a = _doc(tmp_path / "a.pdf", ["Applicable models: T420/T650/TL550/TL65",
                                  "0/ST U/BT U"])
    b = _doc(tmp_path / "b.pdf", ["Applicable models: T420/T650/TL550/TL650/ST U/BT U"])
    assert _spacing(compare(a, b, cfg)) == []


def test_model_code_list_wrapped_with_inserted_hyphen_is_not_reported(tmp_path, cfg):
    """Same wrap, but the break picks up a hyphen that is not part of any code: still layout, not content."""
    a = _doc(tmp_path / "a.pdf", ["Applicable models: T420/T650/TL550/TL65-",
                                  "0/ST U/BT U"])
    b = _doc(tmp_path / "b.pdf", ["Applicable models: T420/T650/TL550/TL650/ST U/BT U"])
    r = compare(a, b, cfg)
    assert _spacing(r) == []
    assert not [f for s in r["sections"] for f in s["findings"] if "punctuation" in f.get("types", [])]
