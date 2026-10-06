"""“Image pixelated”: stage's bitmap is far coarser than prod's bitmap of the same picture. A picture as coarse
in prod as in stage is fine - not reported."""
import io

import pytest
from PIL import Image, ImageDraw

import test_genuine as g
from pdfval import compare, load_config


@pytest.fixture
def cfg():
    c = load_config()
    c["typography"]["enabled"] = False
    return c


def _photo(px: int) -> bytes:
    """The same picture at px pixels across: smooth shapes, so a small copy is a real low-resolution image."""
    big = Image.new("RGB", (1200, 900), "white")
    d = ImageDraw.Draw(big)
    d.ellipse([150, 120, 620, 590], fill=(40, 90, 160))
    d.rectangle([560, 380, 1050, 780], fill=(200, 120, 40))
    d.polygon([(100, 820), (480, 300), (860, 820)], outline=(20, 20, 20), width=14)
    buf = io.BytesIO()
    big.resize((px, px * 3 // 4), Image.LANCZOS).save(buf, "PNG")
    return buf.getvalue()


def _doc(path, px) -> str:
    secs = g.base()
    secs[1]["image"] = _photo(px)
    return g.make(path, secs)


def _pixelated(r):
    return [f["message"] for s in r["sections"] for f in s["findings"] if "image pixelated" in f["types"]]


def test_as_coarse_in_prod_as_in_stage_is_not_reported(tmp_path, cfg):
    assert _pixelated(compare(_doc(tmp_path / "a.pdf", 120), _doc(tmp_path / "b.pdf", 120), cfg)) == []


def test_far_coarser_in_stage_than_in_prod_is_reported(tmp_path, cfg):
    msgs = _pixelated(compare(_doc(tmp_path / "a.pdf", 1200), _doc(tmp_path / "b.pdf", 120), cfg))
    assert msgs and "pixels per inch" in msgs[0]
