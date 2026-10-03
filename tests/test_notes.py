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
