"""“Text in another order” inside a table: the data is compared cell by cell. Every cell the same ->
no finding (the text diff only paired look-alike “v” marks of another table); a cell that differs ->
“Table cell differs” on that cell."""
from collections import Counter
from types import SimpleNamespace

import pymupdf
import pytest

from pdfval import engine, extract
from pdfval.checks import table_cells
from pdfval.model import Finding, Loc

HEAD = [("Color space", "YCbCr 4:2:2", "", "", ""), ("Frame frequency", "24, 25, 30", "50, 60", "24, 25, 30", "50, 60")]
FULL = HEAD + [("HDMI", "v", "v", "v", "v"), ("DisplayPort", "v", "v", "v", "v")]
SOME = [("Color space", "YCbCr 4:2:0", "", "", ""), HEAD[1], ("HDMI", "", "v", "", "v"), ("DisplayPort", "", "", "", "")]


@pytest.fixture
def cfg():
    return engine.load_config()


def _table(pg, y, rows):
    """A fully ruled grid table: label column + 4 value columns."""
    xs, h = [40, 160, 260, 360, 460, 555], 22
    for r, cells in enumerate(rows):
        for c, text in enumerate(cells):
            box = pymupdf.Rect(xs[c], y + r * h, xs[c + 1], y + (r + 1) * h)
            pg.draw_rect(box, color=(0, 0, 0), width=0.6)
            if text:
                pg.insert_text((box.x0 + 6, box.y0 + 15), text, fontsize=10, fontname="helv")
    return y + len(rows) * h


def _doc(path, first, second) -> str:
    doc = pymupdf.open()
    pg = doc.new_page(width=595.28, height=841.89)
    pg.insert_text((40, 60), "Video input", fontsize=16, fontname="hebo")
    y = _table(pg, 90, first)
    _table(pg, y + 30, second)
    doc.save(path)
    return str(path)


def _run(tmp_path, cfg, stage_first):
    a = extract.load(_doc(tmp_path / "a.pdf", FULL, SOME), "baseline", cfg)
    b = extract.load(_doc(tmp_path / "b.pdf", stage_first, SOME), "candidate", cfg)
    # the text diff's finding: the HDMI row's “v” marks of the first prod table, paired elsewhere in stage
    hdmi = [w for w in a.words if w.page == 0 and w.text == "v" and 90 + 2 * 22 < w.bbox[1] < 90 + 3 * 22]
    f = Finding("content", "info", "Reordered: “v v v v”", [Loc(0, w.bbox) for w in hdmi], [Loc(0, (0, 0, 1, 1))],
                {"op": "move"}, types=["reordered"])
    u = SimpleNamespace(a_range=(0, len(a.words)), b_range=(0, len(b.words)))
    ran = [(u, [f], Counter())]
    table_cells.validate_reordered(ran, a, b, cfg)
    return ran[0][1]


def test_same_data_in_every_cell_is_not_reported(tmp_path, cfg):
    assert _run(tmp_path, cfg, FULL) == []


def test_a_cell_that_differs_is_highlighted(tmp_path, cfg):
    stage = FULL[:2] + [("HDMI", "v", "v", "", "v"), FULL[3]]
    fs = _run(tmp_path, cfg, stage)
    assert len(fs) == 1 and fs[0].types == ["cell differs"]
    assert fs[0].detail["cells"] == 1
    assert "row “HDMI”" in fs[0].message and "“v” → (empty)" in fs[0].message
    (loc,) = fs[0].baseline
    assert 359 <= loc.bbox[0] < loc.bbox[2] <= 461  # the third value column (x 360-460), not the whole row
