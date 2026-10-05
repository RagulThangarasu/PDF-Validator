"""Note style, prod against stage: a bar on the left in prod, a filled box in stage is one finding per type."""
import pymupdf

from pdfval import load_config
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
