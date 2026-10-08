"""Picture grids: pictures side by side on one row in prod must be side by side in stage (and the reverse)."""
from types import SimpleNamespace

from pdfval.checks import picture_rows

CFG = {"layout": {}, "assets": {"icon_max_width": 0.08}}


def _im(page, x, y, w=120, h=80):
    return SimpleNamespace(page=page, bbox=(x, y, x + w, y + h))


def _unit(pairs, monkeypatch):
    monkeypatch.setattr(picture_rows.assets, "icon_max", lambda doc, im, acfg: (im.bbox[2] - im.bbox[0]) < 30)
    return SimpleNamespace(cfg=CFG, a=None, b=None, image_pairs=pairs)


def test_a_row_of_three_stacked_in_stage_is_reported(monkeypatch):
    pairs = [(_im(0, 50, 100), _im(0, 50, 100)), (_im(0, 220, 100), _im(0, 50, 220)), (_im(0, 390, 100), _im(0, 50, 340))]
    (f,) = picture_rows.check(_unit(pairs, monkeypatch))
    assert f.types == ["picture row"] and "3 pictures side by side" in f.message and "take 3 rows in stage" in f.message


def test_the_same_grid_is_not_reported(monkeypatch):
    pairs = [(_im(0, 50, 100), _im(2, 30, 400)), (_im(0, 220, 100), _im(2, 260, 400)), (_im(0, 50, 300), _im(2, 30, 600))]
    assert picture_rows.check(_unit(pairs, monkeypatch)) == []


def test_stacked_in_prod_and_in_a_row_in_stage_is_reported(monkeypatch):
    pairs = [(_im(0, 50, 100), _im(0, 50, 100)), (_im(0, 50, 220), _im(0, 220, 100))]
    (f,) = picture_rows.check(_unit(pairs, monkeypatch))
    assert "set side by side on one row in stage" in f.message


def test_icons_are_left_out(monkeypatch):
    pairs = [(_im(0, 50, 100, 20, 20), _im(0, 50, 100, 20, 20)), (_im(0, 90, 100, 20, 20), _im(0, 50, 140, 20, 20))]
    assert picture_rows.check(_unit(pairs, monkeypatch)) == []
