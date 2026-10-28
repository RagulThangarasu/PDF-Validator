"""A numbered item's wrapped (continuation) line must resume under its own text, not under the
number/bullet - this is an absolute defect, caught even when prod and stage are wrong the same way
(checks/layout.py::_bullets, the "wrap_under_marker" absolute check next to the existing prod-vs-stage
relative "hanging indent" comparison)."""
import pymupdf

from pdfval import engine


def _list_pdf(path, wrap_x: float) -> str:
    """"9. Headphone jack" then "11. XL2540X+/... long text" whose continuation line starts at wrap_x -
    72 (under the "11.") is the bug, 90 (under "XL2540X+/...") is correct."""
    doc = pymupdf.open()
    page = doc.new_page(width=595.28, height=841.89)
    page.insert_text((72, 90), "Rear panel", fontsize=16, fontname="hebo")
    y = 130
    page.insert_text((72, y), "9.", fontsize=11, fontname="helv")
    page.insert_text((90, y), "Headphone jack", fontsize=11, fontname="helv")
    y += 14
    page.insert_text((72, y), "10.", fontsize=11, fontname="helv")
    page.insert_text((90, y), "S Switch socket", fontsize=11, fontname="helv")
    y += 14
    page.insert_text((72, y), "11.", fontsize=11, fontname="helv")
    page.insert_text((90, y), "XL2540X+/XL2546X/XL2540 HDMI 2.0 sockets", fontsize=11, fontname="helv")
    y += 14
    page.insert_text((wrap_x, y), "(XL2586X/XL2.1 sockets)", fontsize=11, fontname="helv")
    doc.save(path)
    return str(path)


def _kinds(result):
    return [f for s in result["sections"] for f in s["findings"] if "wrap under number" in (f.get("types") or [])]


def test_wrapped_line_under_the_number_is_reported(tmp_path):
    cfg = engine.load_config()
    a = _list_pdf(tmp_path / "a.pdf", 72)
    b = _list_pdf(tmp_path / "b.pdf", 72)
    result = engine.compare(a, b, cfg)
    fs = _kinds(result)
    assert fs, "a continuation line under the number must be reported even when both sides have it"
    assert any(f["baseline"] for f in fs), "prod side flagged"
    assert any(f["candidate"] for f in fs), "stage side flagged"


def test_wrapped_line_under_its_own_text_is_not_reported(tmp_path):
    cfg = engine.load_config()
    a = _list_pdf(tmp_path / "a.pdf", 90)
    b = _list_pdf(tmp_path / "b.pdf", 90)
    result = engine.compare(a, b, cfg)
    assert not _kinds(result), "a correctly hang-indented continuation line must not be reported"
