"""An inline icon ("press [OK] to confirm") that sits beside its word on one line in prod, but is pushed
down onto the next line in stage by reflow - a real layout defect, distinct from "icon missing" (which
means stage shows no such icon anywhere near its word at all)."""
import pymupdf

from pdfval import compare, load_config

FONT = "helv"


def _make(path, icon_dy=0.0):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 90), "Overview", fontsize=18, fontname="hebo")
    y = 160
    page.insert_text((72, y), "Press", fontsize=11, fontname=FONT)
    icon_x0 = 72 + pymupdf.get_text_length("Press ", fontsize=11, fontname=FONT)
    page.draw_rect(pymupdf.Rect(icon_x0, y - 9 + icon_dy, icon_x0 + 10, y + 1 + icon_dy), color=None, fill=(0, 0, 0))
    page.insert_text((icon_x0 + 14, y), "OK to confirm.", fontsize=11, fontname=FONT)
    doc.set_toc([[1, "Overview", 1]])
    doc.save(path)
    return str(path)


def _cfg():
    c = load_config()
    c["sections"]["front_matter"] = False
    return c


def _findings(result, typ):
    return [f for s in result["sections"] for f in s["findings"] if typ in (f.get("types") or [])]


def test_icon_beside_its_word_on_both_sides_is_not_reported(tmp_path):
    a = _make(tmp_path / "a.pdf")
    b = _make(tmp_path / "b.pdf")
    r = compare(a, b, _cfg())
    assert not _findings(r, "icon wraps to next line")
    assert not _findings(r, "icon missing inline")


def test_icon_pushed_onto_the_next_line_in_stage_is_reported(tmp_path):
    a = _make(tmp_path / "a.pdf")
    b = _make(tmp_path / "b.pdf", icon_dy=14)
    r = compare(a, b, _cfg())
    f = _findings(r, "icon wraps to next line")
    assert f, r["sections"][0]["findings"]
    assert not _findings(r, "icon missing inline")  # not reported as plain "missing" once found one line down
