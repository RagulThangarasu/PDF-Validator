"""Text drawn in fonts without a Unicode map is read back from the page (pdfval/glyphs.py)."""
import shutil
import subprocess

import pymupdf
import pytest

from pdfval import compare, load_config
from pdfval import extract

TEXT = ["打開產品包裝後，請檢查是否含有下列物品。", "請使用本產品隨附的電源變壓器。", "請勿將任何物體懸掛在本產品上。"]


def _has_chi_tra() -> bool:
    if not shutil.which("tesseract"):
        return False
    r = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True)
    return "chi_tra" in r.stdout


pytestmark = pytest.mark.skipif(not _has_chi_tra(), reason="needs tesseract with chi_tra")


def build(path, lines, strip: bool):
    """A Chinese page; strip=True drops the font's ToUnicode, so text extraction reads glyph
    numbers ("ᐇ䅛䌨Ẍ" for "安裝說明") - a prod PDF exported without a Unicode map."""
    font = pymupdf.Font("cjk")
    doc = pymupdf.open()
    p = doc.new_page(width=420, height=595)
    p.insert_font(fontname="F0", fontbuffer=font.buffer)
    p.insert_text((40, 60), "安裝說明", fontname="F0", fontsize=16)
    for k, line in enumerate(lines):
        p.insert_text((40, 100 + 20 * k), line, fontname="F0", fontsize=11)
    doc.set_toc([[1, "安裝說明", 1]])
    if strip:
        for x in range(1, doc.xref_length()):
            if doc.xref_get_key(x, "ToUnicode")[0] != "null":
                doc.xref_set_key(x, "ToUnicode", "null")
    doc.save(path)
    return str(path)


@pytest.fixture
def cfg():
    return load_config()


def test_unmapped_font_is_read_back(tmp_path, cfg):
    prod = build(tmp_path / "prod.pdf", TEXT, strip=True)
    stage = build(tmp_path / "stage.pdf", TEXT, strip=False)
    d = extract.load(prod, "baseline", cfg, reference=stage)
    assert d.decoded and d.decoded["decoded"] > 0
    text = "".join(w.text for w in d.words)
    assert "打開產品包裝後" in text and "懸掛" in text, text


def test_read_back_text_matches_and_a_real_change_is_still_found(tmp_path, cfg):
    prod = build(tmp_path / "prod.pdf", TEXT, strip=True)
    same = compare(prod, build(tmp_path / "same.pdf", TEXT, strip=False), cfg)
    assert same["summary"]["content"]["match_pct"] > 97
    changed = TEXT[:2] + ["請勿將任何物體放置在本產品上。"]
    r = compare(prod, build(tmp_path / "changed.pdf", changed, strip=False), cfg)
    msgs = [f["message"] for s in r["sections"] for f in s["findings"] if f["check"] == "content"]
    assert any("懸掛" in m and "放置" in m for m in msgs), msgs


def test_mapped_font_is_left_alone(tmp_path, cfg):
    stage = build(tmp_path / "stage.pdf", TEXT, strip=False)
    assert extract.load(stage, "candidate", cfg).decoded is None
