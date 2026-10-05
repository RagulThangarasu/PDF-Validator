"""Note style, prod against stage: a bar on the left in prod, a filled box in stage is one finding per type."""
import pymupdf

from pdfval import compare, load_config
from pdfval.checks import notes


class _Doc:
    def __init__(self, path):
        self.path = str(path)


def _pdf(path, look, n=3):
    doc = pymupdf.open()
    p = doc.new_page()
    for k in range(n):
        y = 80 + 120 * k
        if look == "box":
            p.draw_rect(pymupdf.Rect(60, y - 20, 500, y + 50), color=None, fill=(0.855, 0.91, 0.949))
        elif look == "bar":
            p.draw_rect(pymupdf.Rect(60, y - 20, 62, y + 50), color=None, fill=(0.67, 0.58, 0.75))
        p.insert_text((72, y), "Note", fontsize=11)
        p.insert_text((72, y + 20), "Keep the device away from water.", fontsize=11)
    doc.save(path)
    return _Doc(path)


def test_note_drawn_differently_is_one_finding(tmp_path):
    cfg = load_config()
    f = notes.compare(_pdf(tmp_path / "a.pdf", "bar"), _pdf(tmp_path / "b.pdf", "box"), cfg)
    assert len(f) == 1 and f[0].types == ["callout style"]
    assert "prod — bar on the left #" in f[0].message
    assert "filled box #DAE8F2" in f[0].message and len(f[0].baseline) == 3 and len(f[0].candidate) == 3


def test_same_look_or_too_few_notes_is_no_finding(tmp_path):
    cfg = load_config()
    assert not notes.compare(_pdf(tmp_path / "a.pdf", "box"), _pdf(tmp_path / "b.pdf", "box"), cfg)
    assert not notes.compare(_pdf(tmp_path / "c.pdf", "bar", n=1), _pdf(tmp_path / "d.pdf", "box"), cfg)


def _panel_pdf(path, fill, n=2):
    doc = pymupdf.open()
    p = doc.new_page()
    for k in range(n):
        y = 100 + 120 * k
        p.draw_rect(pymupdf.Rect(60, y - 16, 500, y + 8), color=None, fill=fill)
        p.draw_rect(pymupdf.Rect(60, y - 16, 62, y + 8), color=None, fill=(0.35, 0.17, 0.51))
        p.insert_text((72, y), f"Available when input source {k} is set to Google TV.", fontsize=10)
    doc.save(path)
    return _Doc(path)


def test_shaded_box_in_another_colour_is_reported(tmp_path):
    """“Available when input source is set to Google TV.”: a light purple box in prod, light grey in stage."""
    cfg = load_config()
    f = notes.compare_panels(_panel_pdf(tmp_path / "a.pdf", (0.935, 0.917, 0.951)),
                             _panel_pdf(tmp_path / "b.pdf", (0.973, 0.973, 0.973)), cfg)
    assert len(f) == 1 and "Shaded box style differs" in f[0].message and "filled box #EEEAF" in f[0].message
    assert "#F8F8F8" in f[0].message and f[0].detail["boxes"] == 2
    assert not notes.compare_panels(_panel_pdf(tmp_path / "c.pdf", (0.935, 0.917, 0.951)),
                                    _panel_pdf(tmp_path / "d.pdf", (0.935, 0.917, 0.951)), cfg)


def test_icon_only_note_takes_the_type_its_stage_twin_is_titled(tmp_path):
    """Prod: a purple “!” circle and the text, no title. Stage: the same text under “IMPORTANT” in a box.
    The prod note is an Important note - compared as one."""
    cfg = load_config()
    texts = ["Your product complies with the local wireless regulations.", "Use only the supplied power adapter here."]
    a, b = pymupdf.open(), pymupdf.open()
    pa, pb = a.new_page(), b.new_page()
    for k, t in enumerate(texts):
        y = 100 + 120 * k
        pa.draw_oval(pymupdf.Rect(60, y - 12, 78, y + 6), color=None, fill=(0.27, 0.11, 0.53))
        pa.insert_text((84, y), t, fontsize=10)
        pb.draw_rect(pymupdf.Rect(60, y - 30, 520, y + 20), color=None, fill=(0.89, 0.92, 0.89))
        pb.insert_text((84, y - 14), "IMPORTANT", fontsize=10)
        pb.insert_text((84, y), t, fontsize=10)
    a.save(tmp_path / "a.pdf")
    b.save(tmp_path / "b.pdf")
    f = notes.compare(_Doc(tmp_path / "a.pdf"), _Doc(tmp_path / "b.pdf"), cfg)
    assert any(x.message.startswith("Important style differs") and "2 of 2 Important notes" in x.message for x in f), \
        [x.message for x in f]


def _legend_pdf(path, warning_fill: bool) -> str:
    """A typography legend table ("Symbol | Item | Meaning") naming "Warning" once in its own cell -
    with its row carrying a bar (and, in stage, a fill too) of its own: a table style, not a callout."""
    doc = pymupdf.open()
    pg = doc.new_page(width=595.28, height=841.89)
    pg.insert_text((40, 50), "Typographics", fontsize=18, fontname="hebo")
    x0, x1, x2, x3, h, y = 40, 110, 250, 555, 26, 80
    pg.draw_rect(pymupdf.Rect(x0, y, x3, y + h), color=None, fill=(0.17, 0.17, 0.2))
    for x, t in ((x0, "Symbol"), (x1, "Item"), (x2, "Meaning")):
        pg.insert_text((x + 6, y + 18), t, fontsize=12, fontname="hebo", color=(1, 1, 1))
    y += h
    for label, meaning in (("Warning", "Information to prevent damage."), ("Tip", "Useful information."),
                          ("Note", "Supplementary information.")):
        if label == "Warning":
            bar_color = (0.867, 0.867, 0.867) if warning_fill else (0, 0, 0)
            pg.draw_rect(pymupdf.Rect(x1 - 3, y, x1, y + h), color=None, fill=bar_color)
            if warning_fill:  # stage: "filled box #DDDDDD" behind the Item cell, same row
                pg.draw_rect(pymupdf.Rect(x1, y, x2, y + h), color=None, fill=(0.867, 0.867, 0.867))
        pg.draw_rect(pymupdf.Rect(x0, y, x3, y + h), color=(0.8, 0.8, 0.8), width=0.6)
        pg.draw_line((x1, y), (x1, y + h), color=(0.8, 0.8, 0.8), width=0.6)
        pg.draw_line((x2, y), (x2, y + h), color=(0.8, 0.8, 0.8), width=0.6)
        pg.insert_text((x1 + 6, y + 18), label, fontsize=11, fontname="helv")
        pg.insert_text((x2 + 6, y + 18), meaning, fontsize=10, fontname="helv")
        y += h
    doc.set_toc([[1, "Typographics", 1]])
    doc.save(path)
    return str(path)


def test_legend_table_warning_entry_is_not_a_callout(tmp_path):
    """A typography legend names "Warning" once on each side: not a real styled callout, even when its
    row happens to carry its own bar / fill (that is the table's own style, not a note)."""
    cfg = load_config()
    cfg["typography"]["enabled"] = False
    r = compare(_legend_pdf(tmp_path / "a.pdf", warning_fill=False), _legend_pdf(tmp_path / "b.pdf", warning_fill=True), cfg)
    bad = [f["message"] for s in r["sections"] for f in s["findings"] if "callout style" in f.get("types", [])]
    assert not bad, bad


def _spec_table_pdf(path, look) -> str:
    """A spec table (PC | OS | Description), two rows holding a real "Note" box in their description
    cell (taller than the table's plain rows) and two plain rows without one."""
    doc = pymupdf.open()
    pg = doc.new_page(width=595.28, height=841.89)
    pg.insert_text((40, 50), "System requirements", fontsize=18, fontname="hebo")
    x0, x1, x2, x3 = 40, 110, 250, 555
    y, plain_h, tall_h = 80, 26, 70
    rows = (("Windows OS", "Windows 7 and above with Miracast support", True),
            ("Mac OS", "Mac OS X 10.12 and above with Mirror Screen support", True),
            ("Chrome OS", "for Cast/Mirroring support", False),
            ("Android", "Android 9 above with Mirror Screen support", False))
    for label, desc, has_note in rows:
        h = tall_h if has_note else plain_h
        pg.draw_rect(pymupdf.Rect(x0, y, x3, y + h), color=(0.8, 0.8, 0.8), width=0.6)
        pg.draw_line((x1, y), (x1, y + h), color=(0.8, 0.8, 0.8), width=0.6)
        pg.draw_line((x2, y), (x2, y + h), color=(0.8, 0.8, 0.8), width=0.6)
        pg.insert_text((x1 + 6, y + 18), label, fontsize=10, fontname="helv")
        pg.insert_text((x2 + 6, y + 18), desc, fontsize=10, fontname="helv")
        if has_note:
            ny = y + 30
            if look == "box":
                pg.draw_rect(pymupdf.Rect(x2 + 4, ny - 2, x2 + 180, ny + 34), color=None, fill=(0.855, 0.91, 0.949))
            elif look == "border":
                pg.draw_rect(pymupdf.Rect(x2 + 4, ny - 2, x2 + 180, ny + 34), color=(0.6, 0.6, 0.65), width=0.8)
            pg.insert_text((x2 + 10, ny + 10), "Note", fontsize=9, fontname="helv")
            pg.insert_text((x2 + 10, ny + 24), "Recommended configuration for best results.", fontsize=9, fontname="helv")
        y += h
    doc.set_toc([[1, "System requirements", 1]])
    doc.save(path)
    return str(path)


def test_note_nested_in_a_data_table_cell_is_still_a_callout(tmp_path):
    """A real "Note" box inside a spec table's description cell (its row taller than the table's plain
    rows, to fit the note) is still a styled callout, drawn differently prod vs stage - unlike the
    legend's bare label, this is not excluded just for sitting inside a qualifying table."""
    cfg = load_config()
    cfg["typography"]["enabled"] = False
    cfg["ignore"]["types"] = [t for t in cfg["ignore"]["types"] if t != "callout style"]
    r = compare(_spec_table_pdf(tmp_path / "a.pdf", "border"), _spec_table_pdf(tmp_path / "b.pdf", "box"), cfg)
    found = [f["message"] for s in r["sections"] for f in s["findings"] if "callout style" in f.get("types", [])]
    assert any("Note style differs" in m for m in found), found
