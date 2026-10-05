"""A note's icon read as its kind: a pencil (Note) or an exclamation mark (Warning) drawn on a disc."""
import pymupdf

from pdfval.checks import callout_icons
from pdfval.model import Loc
from types import SimpleNamespace

PURPLE, WHITE = (0.27, 0.11, 0.53), (1, 1, 1)


def _doc(path) -> str:
    doc = pymupdf.open()
    pg = doc.new_page(width=595, height=842)
    for y, kind in ((100, "pencil"), (200, "exclamation")):
        pg.draw_circle((70, y + 11), 11, color=None, fill=PURPLE)
        if kind == "pencil":  # a slanted bar with a point, lower left to upper right
            pg.draw_polyline([(63, y + 17), (75, y + 5), (78, y + 8), (66, y + 20), (63, y + 17)], color=None, fill=WHITE)
            pg.draw_polyline([(63, y + 17), (61, y + 21), (66, y + 20)], color=None, fill=WHITE)
        else:
            pg.draw_rect(pymupdf.Rect(68.5, y + 3, 71.5, y + 14), color=None, fill=WHITE)
            pg.draw_circle((70, y + 17.5), 1.7, color=None, fill=WHITE)
        pg.insert_text((90, y + 14), f"A {kind} note: keep the Receiver away from water.", fontsize=10)
    doc.save(path)
    return str(path)


def test_pencil_and_exclamation_icons(tmp_path):
    d = SimpleNamespace(path=_doc(tmp_path / "icons.pdf"))
    got = []
    for y in (100, 200):
        box = callout_icons.icon_left_of(d, Loc(0, (90, y + 5, 300, y + 15)))
        got.append(callout_icons.kind(d, 0, box) if box else None)
    assert got == ["pencil", "exclamation"]
