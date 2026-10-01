"""Table rows shaded in prod but plain in stage, and a one-cell header bar repeated on a continuation page."""
import pymupdf
import pytest

from pdfval import engine

ROWS = [("Dimension & Weight", None), ("Cable length (mm)", "200 mm"), ("Weight (g)", "188 g"),
        ("Power mode", None), ("Self-powered", "By USB Bus power"), ("Networking", None),
        ("2.5 Gigabit Ethernet RJ-45", "1")]


@pytest.fixture
def cfg():
    c = engine.load_config()
    c["typography"]["enabled"] = False  # prod vs stage only
    return c


def _table(pg, y, rows, shade_groups: bool, header: str | None = "GR10"):
    """A bordered two-column table; group rows (one cell) grey when shade_groups."""
    x0, xm, x1, h = 40, 250, 555, 24
    if header:
        pg.draw_rect(pymupdf.Rect(x0, y, x1, y + h), color=None, fill=(0.2, 0.2, 0.2))
        pg.insert_text((x0 + 6, y + 16), header, fontsize=11, fontname="helv", color=(1, 1, 1))
        y += h
    for label, value in rows:
        r = pymupdf.Rect(x0, y, x1, y + h)
        if value is None and shade_groups:
            pg.draw_rect(r, color=None, fill=(0.93, 0.93, 0.94))
        pg.draw_rect(r, color=(0.8, 0.8, 0.8), width=0.6)
        if value is not None:
            pg.draw_line((xm, y), (xm, y + h), color=(0.8, 0.8, 0.8), width=0.6)
            pg.insert_text((xm + 6, y + 16), value, fontsize=11, fontname="helv")
        pg.insert_text((x0 + 6, y + 16), label, fontsize=11, fontname="helv")
        y += h
    return y


def _doc(path, shade_groups: bool, split: bool = False) -> str:
    doc = pymupdf.open()
    pg = doc.new_page(width=595.28, height=841.89)
    pg.insert_text((40, 60), "Specifications", fontsize=16, fontname="hebo")
    if split:  # the table goes on on the next page, below its repeated header bar
        _table(pg, 90, ROWS[:5], shade_groups)
        pg2 = doc.new_page(width=595.28, height=841.89)
        _table(pg2, 40, ROWS[5:], shade_groups)
    else:
        _table(pg, 90, ROWS, shade_groups)
    doc.save(path)
    return str(path)


def _findings(r):
    return [f for s in r["sections"] for f in s["findings"]]


def test_group_rows_shaded_in_prod_plain_in_stage(tmp_path, cfg):
    cfg = {**cfg, "ignore": {}}  # the detector itself (the default [ignore] list leaves it out)
    r = engine.compare(_doc(tmp_path / "prod.pdf", True), _doc(tmp_path / "stage.pdf", False), cfg)
    got = [f for f in _findings(r) if "row background" in f["types"]]
    assert len(got) == 1
    m = got[0]["message"]
    assert m.startswith("Table row background — 3 row(s) shaded in prod, plain in stage")
    assert "“Dimension & Weight”" in m and "“Power mode”" in m and "“Networking”" in m
    assert "\nProd: rows shaded" in m and "\nStage: the same rows plain #FFFFFF" in m
    assert got[0]["genuine"]
    assert len(got[0]["links"]) == 3  # both screenshots mark the same rows


def test_same_shading_is_no_finding(tmp_path, cfg):
    r = engine.compare(_doc(tmp_path / "prod.pdf", True), _doc(tmp_path / "stage.pdf", True), cfg)
    assert not [f for f in _findings(r) if "row background" in f["types"]]


def test_header_bar_repeated_on_the_next_page_is_not_extra_text(tmp_path, cfg):
    cfg = {**cfg, "ignore": {}}  # the detector itself (the default [ignore] list leaves it out)
    """Stage breaks the table over two pages and repeats its one-cell header bar ("GR10") on the second:
    pagination, not extra content."""
    r = engine.compare(_doc(tmp_path / "prod.pdf", False), _doc(tmp_path / "stage.pdf", False, split=True), cfg)
    fs = _findings(r)
    assert not [f for f in fs if f["check"] == "content" and f.get("genuine")], [f["message"] for f in fs if f.get("genuine")]
    # a header repeated on the next page is expected: not reported at all
    assert not [f for f in fs if "GR10" in f["message"] and f["check"] in ("content", "tables")], [f["message"] for f in fs]


def test_word_soft_hyphenated_at_a_line_break_is_no_difference(tmp_path, cfg):
    """Prod hyphenates “Button's” at the line end with a soft hyphen (shown as “But-”), stage has it whole:
    where a line breaks is layout, not a word gap difference."""
    def page(path, lines):
        doc = pymupdf.open()
        pg = doc.new_page(width=595.28, height=841.89)
        pg.insert_text((40, 60), "Setting up and powering a Button", fontsize=16, fontname="hebo")
        # an embedded font: the soft hyphen stays U+00AD in the text (a built-in font writes it as "-")
        pg.insert_font(fontname="F0", fontbuffer=pymupdf.Font("notos").buffer)
        for k, t in enumerate(lines):
            pg.insert_text((40, 100 + 16 * k), t, fontsize=11, fontname="F0")
        doc.save(path)
        return str(path)
    prod = page(tmp_path / "prod.pdf", ["For laptops with a USB-C port that supports DisplayPort functionality, connect the But­",
                                        "ton's USB-C connector to the corresponding input of the laptop."])
    stage = page(tmp_path / "stage.pdf", ["For laptops with a USB-C port that supports DisplayPort functionality, connect the Button's",
                                          "USB-C connector to the corresponding input of the laptop."])
    fs = [f for f in _findings(engine.compare(prod, stage, cfg)) if f["check"] == "content" and f["severity"] != "info"]
    assert not fs, [f["message"] for f in fs]



def test_near_swap_pairs_a_label_with_the_same_words_a_few_blocks_away():
    """The W5850 case: stage reads the cell label “3D Format” before its text, prod after it (where it sits
    in a block that also holds the NOTE label). The label is the same text in another order."""
    from pdfval.checks.content import _near_swap
    at = ["the", "used", "mode.", "Shows", "the", "current", "3D", "mode.", "3D", "Format", "3D", "Format", "is", "only"]
    bt = ["the", "used", "mode.", "3D", "Format", "Shows", "the", "current", "3D", "mode.", "<label:note>", "3D", "Format", "is", "only"]
    ops = [("equal", 0, 3, 0, 3), ("insert", 3, 3, 3, 5), ("equal", 3, 8, 5, 10), ("replace", 8, 10, 10, 11), ("equal", 10, 14, 11, 15)]
    assert _near_swap(ops, 1, {}, at, bt, 3, 3, 3, 5) == [8, 9]  # prod's “3D Format” in block 3
    far = [("equal", 0, 3, 0, 3), ("insert", 3, 3, 3, 5)] + [("equal", 3, 4, 5, 6)] * 5 + [("replace", 8, 10, 10, 11)]
    assert _near_swap(far, 1, {}, at, bt, 3, 3, 3, 5) is None  # too many blocks apart
    long_bt = bt[:3] + ["a", "b", "c", "d", "e", "f", "g"] + bt[5:]
    assert _near_swap([("insert", 3, 3, 3, 10)], 0, {}, at, long_bt, 3, 3, 3, 10) is None  # not a short label
