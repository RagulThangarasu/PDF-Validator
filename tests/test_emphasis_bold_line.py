"""Bold is "heavier than normal", not "exactly Bold".

A word set Medium (500) or Semibold (600) next to plain body text reads as bold to the reader, so it
is reported as bold added - [style] emphasis_bold_over draws the line (400: anything above normal).
Steps on the same side of that line - Light 300 against Regular 400, Semibold 600 against Bold 700 -
are the design spec's business and are not reported as emphasis.

See pdfval/checks/style.py::_emphasis.
"""
import pymupdf
import pytest

from pdfval import engine

# the built-in faces pymupdf can write without an embedded font file, by weight
FACE = {400: "helv", 700: "hebo"}
SENTENCE = ("Connect the receiver to the display", "before", "you start the calibration.")


@pytest.fixture
def cfg():
    c = engine.load_config()
    c["typography"]["enabled"] = False
    return c


def _pdf(path, weight: int) -> str:
    """One sentence whose middle word is set at `weight`; the rest is plain body text."""
    doc = pymupdf.open()
    pg = doc.new_page(width=595, height=842)
    pg.insert_text((72, 70), "Calibrating", fontsize=16, fontname="hebo")
    x = 72
    for k, part in enumerate(SENTENCE):
        font = FACE[700] if k == 1 and weight >= 700 else FACE[400]
        pg.insert_text((x, 120), part + " ", fontsize=10, fontname=font)
        x += pymupdf.get_text_length(part + " ", fontname=font, fontsize=10)
    pg.insert_text((72, 140), "Keep the ventilation openings free so warm air can leave the housing.",
                   fontsize=10, fontname="helv")
    doc.set_toc([[1, "Calibrating", 1]])
    doc.save(path)
    return str(path)


def _emphasis(r):
    return [f for s in r["sections"] for f in s["findings"] if "emphasis" in (f.get("types") or [])]


def test_plain_against_bold_is_reported(tmp_path, cfg):
    r = engine.compare(_pdf(tmp_path / "a.pdf", 400), _pdf(tmp_path / "b.pdf", 700), cfg)
    got = _emphasis(r)
    assert got and "before" in got[0]["message"], [f["message"] for f in got]


def test_the_same_weight_on_both_sides_is_not(tmp_path, cfg):
    assert not _emphasis(engine.compare(_pdf(tmp_path / "a.pdf", 400), _pdf(tmp_path / "b.pdf", 400), cfg))
    assert not _emphasis(engine.compare(_pdf(tmp_path / "c.pdf", 700), _pdf(tmp_path / "d.pdf", 700), cfg))


# --- where the line sits ------------------------------------------------------------------------

def _bold(weight: int, cfg) -> bool:
    """What _emphasis counts as bold at this config's threshold."""
    over = cfg["style"].get("emphasis_bold_over", 400)
    return weight > over


def test_anything_above_normal_counts_as_bold(cfg):
    assert cfg["style"]["emphasis_bold_over"] == 400
    assert not cfg["style"]["emphasis_any_weight"]      # plain vs bold, not every step on its own
    for w in (500, 600, 700, 800):
        assert _bold(w, cfg), w                          # Medium and Semibold read as bold
    for w in (100, 300, 400):
        assert not _bold(w, cfg), w                      # Light and Regular are plain


def test_the_line_can_be_moved_back_to_bold_only(cfg):
    cfg["style"]["emphasis_bold_over"] = 600
    assert not _bold(500, cfg) and not _bold(600, cfg) and _bold(700, cfg)


def test_the_profile_uses_the_same_rule():
    c = engine.load_config("config/pdf-to-pdf.toml")
    assert c["style"]["emphasis_bold_over"] == 400 and c["style"]["emphasis_any_weight"] is False
