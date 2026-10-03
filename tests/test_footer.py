"""Running footers, prod vs stage (checks/footer.py)."""
import pymupdf
import pytest

from pdfval import engine


@pytest.fixture
def cfg():
    c = engine.load_config()
    c["sections"]["front_matter"] = False
    # footer validation is off by default ([footer] enabled = false): these tests check the footer check itself
    c.setdefault("footer", {})["enabled"] = True
    c["ignore"]["types"] = [t for t in c["ignore"]["types"] if t not in ("footer", "header", "spec page number")]
    return c

TITLES = ["Package contents", "Positioning", "Connection", "Operation", "Maintenance", "Specifications"]
WORDS = ("projector surface stable switched first time air vents free cover lens lamp supplied power cord only "
         "remote batteries screen image focus zoom keystone menu source input signal cable port audio speaker").split()


def _manual(path, footer):
    """Six pages of body text (different on every page, or it would be taken for page furniture);
    footer(page_no) -> [(text, x, fontsize, colour)] drawn near the page foot."""
    doc = pymupdf.open()
    for k in range(6):
        pg = doc.new_page(width=595, height=842)
        pg.insert_text((60, 80), TITLES[k], fontsize=18)
        y = 120
        for n in range(8):
            text = " ".join(WORDS[(k * 7 + n * 3 + m) % len(WORDS)] for m in range(28)) + "."
            pg.insert_textbox(pymupdf.Rect(60, y, 535, y + 60), text, fontsize=11)
            y += 70
        for text, x, size, color in footer(k + 1):
            pg.insert_text((x, 815), text, fontsize=size, color=color)
    doc.set_toc([[1, t, k + 1] for k, t in enumerate(TITLES)])
    doc.save(path)
    return str(path)


def _footer_findings(r):
    return [f for s in r["sections"] for f in s["findings"] if "footer" in (f.get("types") or [])]


def test_footer_chapter_name_and_page_number_place(tmp_path, cfg):
    # prod: "3  Operation" on the outer edge (left on even pages, right on odd), 9 pt black
    prod = _manual(tmp_path / "a.pdf", lambda n: [(f"{n}", 60 if n % 2 == 0 else 525, 9, (0, 0, 0)),
                                                   ("Operation", 80 if n % 2 == 0 else 440, 9, (0, 0, 0))])
    # stage: only a centred page number, 12 pt grey
    stage = _manual(tmp_path / "b.pdf", lambda n: [(f"{n}", 294, 12, (0.5, 0.5, 0.5))])
    msgs = [f["message"] for f in _footer_findings(engine.compare(prod, stage, cfg))]
    assert any(m.startswith("Footer text missing in stage") and "“Operation”" in m for m in msgs), msgs
    assert any(m.startswith("Footer page number position:") and "centred in stage" in m
               and "left" in m and "right" in m for m in msgs), msgs
    assert any(m.startswith("Footer page number style:") for m in msgs), msgs


def test_same_footer_is_no_finding(tmp_path, cfg):
    same = lambda n: [(f"{n}", 294, 10, (0, 0, 0))]
    r = engine.compare(_manual(tmp_path / "a.pdf", same), _manual(tmp_path / "b.pdf", same), cfg)
    assert not _footer_findings(r)
