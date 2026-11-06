"""The icon beside a callout, prod against stage.

Both sides label the note the same ("Note"), so nothing in the text says they differ - but prod draws
an info circle where stage (AEM Guides) draws a pencil, and that is the first thing the reader sees.

The rule compares icon against icon, which is what keeps the house-style case quiet: prod's note set
with only its pencil, and stage's same pencil with "NOTE" written beside it, is the same icon and is
never reported.
"""
import pymupdf
import pytest

from pdfval import engine


def _info(pg, x, y):
    """An "i" on a ring: the dot on top, the bar under it."""
    s = pg.new_shape()
    s.draw_circle(pymupdf.Point(x + 9, y + 9), 9)
    s.finish(color=(0, 0, 0), width=1.2)
    s.draw_rect(pymupdf.Rect(x + 8, y + 8, x + 10, y + 15))
    s.finish(fill=(0, 0, 0), color=(0, 0, 0))
    s.draw_rect(pymupdf.Rect(x + 8, y + 3.5, x + 10, y + 5.5))
    s.finish(fill=(0, 0, 0), color=(0, 0, 0))
    s.commit()


def _pencil(pg, x, y):
    """A pencil on a tile: its marks along one line slanted 45 degrees."""
    s = pg.new_shape()
    s.draw_rect(pymupdf.Rect(x, y, x + 18, y + 18))
    s.finish(fill=(0.85, 0.9, 1), color=(0.85, 0.9, 1))
    for a, b in ((4, 13), (7, 10), (10, 7), (13, 4)):
        s.draw_rect(pymupdf.Rect(x + a, y + b, x + a + 3, y + b + 3))
        s.finish(fill=(0, 0, 0), color=(0, 0, 0))
    s.commit()


def _pdf(path, drawer, label):
    """Three notes, each with `drawer`'s icon and `label` beside it (None = the icon alone)."""
    doc = pymupdf.open()
    pg = doc.new_page()
    pg.insert_text((72, 70), "Getting started", fontsize=16, fontname="hebo")
    for i, y in enumerate((120, 200, 280)):
        drawer(pg, 72, y - 12)
        if label:
            pg.insert_text((96, y), label, fontsize=11, fontname="hebo")
        pg.insert_text((96, y + 16), f"Actual product's features are subject to change {i}.", fontsize=10)
    doc.set_toc([[1, "Getting started", 1]])
    doc.save(path)
    return str(path)


@pytest.fixture(scope="module")
def cfg():
    return engine.load_config()


def _icon_findings(a, b, cfg):
    r = engine.compare(a, b, cfg)
    return [f for s in r["sections"] for f in s["findings"]
            if "callout icon differs" in (f.get("types") or [])]


def test_another_icon_beside_the_same_note_is_reported(tmp_path, cfg):
    a = _pdf(tmp_path / "a.pdf", _info, "Note")
    b = _pdf(tmp_path / "b.pdf", _pencil, "NOTE")
    got = _icon_findings(a, b, cfg)
    assert got, "prod's info circle against stage's pencil is a difference the reader sees"
    assert "info circle" in got[0]["message"] and "pencil" in got[0]["message"]
    assert got[0]["genuine"], "it belongs in the delivered reports, not only the full one"


def test_the_same_icon_with_the_label_added_is_not_reported(tmp_path, cfg):
    """House style, and the user's standing rule: prod's pencil alone, stage's pencil plus "NOTE"."""
    a = _pdf(tmp_path / "a.pdf", _pencil, None)
    b = _pdf(tmp_path / "b.pdf", _pencil, "NOTE")
    assert not _icon_findings(a, b, cfg)


def test_the_same_icon_on_both_sides_is_not_reported(tmp_path, cfg):
    a = _pdf(tmp_path / "a.pdf", _pencil, "Note")
    b = _pdf(tmp_path / "b.pdf", _pencil, "NOTE")
    assert not _icon_findings(a, b, cfg)


def test_the_rule_can_be_turned_off(tmp_path, cfg):
    cfg = {**cfg, "notes": {**cfg["notes"], "check_icon": False}}
    a = _pdf(tmp_path / "a.pdf", _info, "Note")
    b = _pdf(tmp_path / "b.pdf", _pencil, "NOTE")
    assert not _icon_findings(a, b, cfg)


def test_an_icon_the_detector_cannot_name_is_left_alone(tmp_path, cfg):
    """A blank tile is not a kind: an unrecognised icon is never guessed at."""
    blank = lambda pg, x, y: pg.draw_rect(pymupdf.Rect(x, y, x + 18, y + 18), fill=(0.9, 0.9, 0.9))
    a = _pdf(tmp_path / "a.pdf", _pencil, "Note")
    b = _pdf(tmp_path / "b.pdf", blank, "NOTE")
    assert not _icon_findings(a, b, cfg)
