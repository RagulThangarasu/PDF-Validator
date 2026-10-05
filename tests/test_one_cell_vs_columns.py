import pymupdf
import pytest

from pdfval import engine


@pytest.fixture
def cfg():
    c = engine.load_config()
    c["typography"]["enabled"] = False  # prod vs stage only
    return c

ROWS = [("Model", "SL4304", "43 inch"), ("Weight", "10 kg", "12 kg"), ("Power", "100 W", "120 W")]


def _doc(path, columns: bool):
    """“Specifications” then the data: prod one bordered cell holding every row as a line, stage a ruled
    3-column table."""
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 60), "Specifications", fontsize=18)
    p.insert_text((72, 90), "The table below lists the main values of the display for each model.", fontsize=11)
    x0, y0, w, h = 72, 110, 420, 24
    if columns:
        for r, row in enumerate(ROWS):
            for c, txt in enumerate(row):
                p.draw_rect(pymupdf.Rect(x0 + c * w / 3, y0 + r * h, x0 + (c + 1) * w / 3, y0 + (r + 1) * h), color=(0, 0, 0))
                p.insert_text((x0 + c * w / 3 + 4, y0 + r * h + 16), txt, fontsize=10)
    else:
        p.draw_rect(pymupdf.Rect(x0, y0, x0 + w, y0 + h * len(ROWS)), color=(0, 0, 0))
        for r, row in enumerate(ROWS):
            p.insert_text((x0 + 4, y0 + r * h + 16), " ".join(row), fontsize=10)
    p.insert_text((72, 220), "Care", fontsize=18)
    p.insert_text((72, 250), "Clean the screen with a soft dry cloth and keep it away from water.", fontsize=11)
    doc.set_toc([[1, "Specifications", 1], [1, "Care", 1]])
    doc.save(path)
    return str(path)


def test_one_cell_in_prod_is_three_columns_in_stage(tmp_path, cfg):
    r = engine.compare(_doc(tmp_path / "prod.pdf", False), _doc(tmp_path / "stage.pdf", True), cfg)
    fs = [f for s in r["sections"] for f in s["findings"] if "cells split" in f["types"]]
    assert fs and "one cell in prod" in fs[0]["message"] and "3-column" in fs[0]["message"] and fs[0]["genuine"]
