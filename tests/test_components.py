"""Design-spec components (checks/components.py): the theme from the cover, callouts, page numbers."""
import io

import pymupdf
import pytest
from PIL import Image as PILImage

from pdfval import engine, extract
from pdfval.checks import Unit, components


@pytest.fixture
def cfg():
    return engine.load_config()


def _rgb(hex_: str) -> tuple:
    return tuple(int(hex_[k:k + 2], 16) / 255 for k in (1, 3, 5))


def _icon() -> bytes:
    im = PILImage.new("RGB", (28, 28), "white")
    for k in range(4, 24):
        im.putpixel((k, k), (0, 0, 0))
        im.putpixel((k, 27 - k), (0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _cover(doc: pymupdf.Document, color: str) -> None:
    pg = doc.new_page(width=595.28, height=841.89)
    pg.draw_rect(pg.rect, color=None, fill=_rgb(color))
    pg.insert_text((39, 700), "Projector X1 user manual", fontsize=36, fontname="helv", color=(1, 1, 1))


def _topic(doc: pymupdf.Document, number: int | None, callouts=()) -> pymupdf.Page:
    pg = doc.new_page(width=595.28, height=841.89)
    pg.insert_text((28, 60), "Some body text of the topic page.", fontsize=12, fontname="helv")
    y = 100
    for title, fill, icon, picture in callouts:
        box = pymupdf.Rect(28, y, 567, y + 90)
        pg.draw_rect(box, color=None, fill=_rgb(fill))
        if icon:
            pg.insert_image(pymupdf.Rect(36, y + 8, 50, y + 22), stream=_icon())
        pg.insert_text((56, y + 20), title, fontsize=12, fontname="hebo", color=_rgb("#333333"))
        pg.insert_text((56, y + 38), "The callout text says what to watch out for.", fontsize=12, fontname="helv")
        if picture:
            pg.insert_image(pymupdf.Rect(56, y + 44, 200, y + 86), stream=_icon())
        y += 110
    if number is not None:
        pg.insert_text((293, 830), str(number), fontsize=12, fontname="helv", color=_rgb("#808080"))
    return pg


def _save(doc: pymupdf.Document, tmp_path, name: str) -> str:
    path = str(tmp_path / name)
    doc.save(path)
    return path


@pytest.mark.parametrize("color, key, accent", [("#008C94", "edu", "#00626B"), ("#482581", "benq", "#7231C6")])
def test_theme_is_read_from_the_cover(tmp_path, cfg, color, key, accent):
    doc = pymupdf.open()
    _cover(doc, color)
    _topic(doc, 1)
    th = components.theme(extract.load(_save(doc, tmp_path, f"{key}.pdf"), "stage", cfg), cfg)
    assert th["cover"] and th["matched"] and th["key"] == key and th["accent"] == accent


def test_no_cover_uses_the_default_theme(tmp_path, cfg):
    doc = pymupdf.open()
    _topic(doc, 1)
    th = components.theme(extract.load(_save(doc, tmp_path, "nocover.pdf"), "stage", cfg), cfg)
    assert not th["cover"] and th["key"] == cfg["typography"]["default_theme"]


def _unit(path: str, cfg) -> Unit:
    d = extract.load(path, "stage", cfg)
    return Unit("t", "t", d, d, (0, len(d.words)), (0, len(d.words)), None, None, cfg)


def test_callouts(tmp_path, cfg):
    doc = pymupdf.open()
    _cover(doc, "#482581")
    _topic(doc, 1, [("NOTE", "#DAE8F2", True, False),        # as Figma
                    ("NOTE:", "#DAE8F2", True, False),       # the colon only: one document-level finding
                    ("WARNING", "#DAE8F2", True, False),     # a warning in the note's blue
                    ("TIP", "#F2F2F2", False, False),        # no icon
                    ("IMPORTANT", "#E3EAE3", True, True)])   # a picture inside
    u = _unit(_save(doc, tmp_path, "callouts.pdf"), cfg)
    fmt = cfg["typography"]["formats"]["a4_view"]
    cos = components.callouts(u.b, cfg)
    assert [c["type"] for c in cos] == ["note", "note", "warning", "tip", "important"]
    msgs = [f.message for f in components._callout_findings(u, fmt)]
    assert "Callout background — 1 callout(s)\nFigma: WARNING #F2DFDA\nStage: #DAE8F2" in msgs  # headline, Figma, stage
    assert any(m.startswith("TIP callout icon missing") and "\nStage: no icon" in m for m in msgs)
    assert any("Picture inside a IMPORTANT callout" in m for m in msgs)
    assert not any(m.startswith("Callout title") for m in msgs)  # "NOTE:" is not reported per callout ...
    colons = components.callout_colons(u.b, cfg, fmt)
    assert len(colons) == 1 and "“NOTE:” ×1" in colons[0].message  # ... but once for the document


def test_page_numbers_count_from_one_after_the_cover(tmp_path, cfg):
    fmt = cfg["typography"]["formats"]["a4_view"]
    doc = pymupdf.open()
    _cover(doc, "#482581")
    for n in (1, 2, 4):  # the third page shows 4
        _topic(doc, n)
    _topic(doc, None)  # no number
    d = extract.load(_save(doc, tmp_path, "numbers.pdf"), "stage", cfg)
    msgs = [f.message for f in components.page_numbers(d, cfg, fmt, components.theme(d, cfg))]
    assert any(m.startswith("Page number missing — 1 page(s)") and "Stage: no page number on p.5" in m for m in msgs)
    assert any("p.4 shows 4 (should be 3)" in m for m in msgs)
    assert not any(m.startswith("Page numbering") for m in msgs)

    doc = pymupdf.open()
    _cover(doc, "#482581")
    for n in (2, 3):  # the cover counted as page 1
        _topic(doc, n)
    d = extract.load(_save(doc, tmp_path, "numbers2.pdf"), "stage", cfg)
    msgs = [f.message for f in components.page_numbers(d, cfg, fmt, components.theme(d, cfg))]
    assert any("Figma starts at 1, stage at 2" in m and "Stage: the page after the cover is page 2 (the cover is counted)" in m
               for m in msgs) and not any("out of sequence" in m for m in msgs)


def test_edu_accent_is_not_reported_as_off_spec(tmp_path, cfg):
    """An EDU manual's teal headline is the theme's accent, not a colour off the (BenQ purple) spec."""
    from pdfval.checks import typography
    doc = pymupdf.open()
    _cover(doc, "#008C94")
    _topic(doc, 1)
    d = extract.load(_save(doc, tmp_path, "edu_h1.pdf"), "stage", cfg)
    styles = typography.themed(cfg["typography"]["formats"]["a4_view"]["styles"], components.theme(d, cfg))
    assert styles["h1"]["color"] == "#00626B" and styles["body_hyperlink"]["color"] == "#00626B"
    assert styles["h2"]["color"] == "#000000"  # not an accent style
    ref = typography.reference(d, cfg)
    assert ref["theme"]["name"] == "BenQ Education" and {s["color"] for s in ref["styles"]} >= {"#00626B"}
