"""The same picture with its labels drawn into the image in prod (“A. Tabletop  B. Pole mount”) and
without them in stage is the same artwork: not a different image. Another drawing stays one."""
import io

import pymupdf
import pytest
from conftest import every_picture_issue
from PIL import Image, ImageDraw, ImageFont

from pdfval import compare, load_config
from pdfval import ocr


@pytest.fixture
def cfg():
    c = load_config()
    c["typography"]["enabled"] = False
    return every_picture_issue(c)


def _font(size):
    for name in ("Arial.ttf", "Helvetica.ttc", "DejaVuSans.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _picture(labels: bool, shapes: str = "mounts") -> bytes:
    """Three line drawings side by side (a table, a pole, a wall bracket), with or without a label row."""
    w, h = 1200, 400 if labels else 260
    im = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(im)
    top = 140 if labels else 0
    if labels:  # a bold label row, as tall as half the drawings
        f = _font(64)
        for k, t in enumerate(("A. Table", "B. Pole", "C. Wall")):
            d.text((40 + k * 400, 25), t, fill=0, font=f, stroke_width=2, stroke_fill=0)
    for k in range(3):
        x = 60 + k * 400
        if shapes == "mounts":
            d.rectangle([x, top + 40, x + 260, top + 90], outline=0, width=6)
            d.line([x + 20, top + 90, x + 20, top + 220], fill=0, width=6)
            d.line([x + 240, top + 90, x + 240, top + 220], fill=0, width=6)
            d.ellipse([x + 90, top + 10, x + 170, top + 60], outline=0, width=6)
        else:  # other artwork altogether
            d.polygon([(x + 130, top + 10), (x + 10, top + 230), (x + 250, top + 230)], outline=0, width=6)
            d.line([x, top + 120, x + 260, top + 120], fill=0, width=6)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _doc(path, png) -> str:
    doc = pymupdf.open()
    pg = doc.new_page(width=595, height=842)
    pg.insert_text((72, 80), "Setting up the Receiver", fontsize=16, fontname="hebo")
    pg.insert_text((72, 110), "You are provided with three different ways to position the Receiver.", fontsize=10)
    im = Image.open(io.BytesIO(png))
    pg.insert_image(pymupdf.Rect(72, 130, 72 + 450, 130 + 450 * im.height / im.width), stream=png)
    pg.insert_text((72, 330), "Placing the Receiver on a table: attach the lid to the Receiver first.", fontsize=10)
    doc.set_toc([[1, "Setting up the Receiver", 1]])
    doc.save(path)
    return str(path)


def _image_diffs(r):
    return [f for s in r["sections"] for f in s["findings"] if f["check"] == "assets" and f["detail"].get("kind") == "changed"]


@pytest.mark.skipif(not ocr.available(), reason="tesseract not installed")
def test_labels_drawn_in_prod_picture_only_is_not_a_different_image(tmp_path, cfg):
    a, b = _doc(tmp_path / "a.pdf", _picture(True)), _doc(tmp_path / "b.pdf", _picture(False))
    assert _image_diffs(compare(a, b, cfg)) == []


@pytest.mark.skipif(not ocr.available(), reason="tesseract not installed")
def test_other_artwork_with_labels_is_still_a_different_image(tmp_path, cfg):
    a, b = _doc(tmp_path / "a.pdf", _picture(True)), _doc(tmp_path / "b.pdf", _picture(False, "other"))
    r = compare(a, b, cfg)
    assert _image_diffs(r) or any("image" in f["message"].lower() for s in r["sections"] for f in s["findings"])
