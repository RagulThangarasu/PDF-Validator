"""PDF to PDF: upload A and B, report seven things and nothing else.

config/pdf-to-pdf.toml is the whole contract — content missing, bold differs, alignment differs, a
table past the right margin, image position, an image under 60 % of the original, icon alignment. The
run is A -> B; every other check the tool has is switched off. Two of the seven are new checks:
tables._past_margin and the alignment branch of inline_icons.check.
"""
import pymupdf
import pytest

from pdfval import engine

PROFILE = "config/pdf-to-pdf.toml"
WANTED = {"missing text", "emphasis", "text-align", "table past margin",
          "image alignment", "image smaller", "icon alignment", "wide gap", "callout type differs"}


@pytest.fixture
def cfg():
    return engine.load_config(PROFILE)


def _findings(r, kind=None):
    fs = [f for s in r["sections"] for f in s["findings"]]
    return [f for f in fs if kind in (f.get("types") or [])] if kind else fs


# --- the contract ----------------------------------------------------------------------------------

def test_the_profile_reports_exactly_the_seven_kinds(cfg):
    assert set(cfg["genuine"]["types"]) == WANTED


def test_nothing_wanted_is_also_ignored(cfg):
    """A name in both lists reports nothing at all, which is the quiet way to lose a check."""
    assert WANTED & set(cfg["ignore"]["types"]) == set()


def test_the_checks_behind_them_are_on(cfg):
    assert cfg["style"]["check_emphasis"] and not cfg["style"]["compare_with_prod"]   # bold only
    assert cfg["layout"]["compare_with_prod"]                                         # alignment A vs B
    assert cfg["tables"]["check_table_margin"] and cfg["tables"]["enabled"]
    assert cfg["assets"]["check_alignment"] and cfg["assets"]["smaller_report_under"] == 0.5
    assert cfg["content"]["check_wide_gap"] and cfg["content"]["gap_min_chars"] == 3
    assert not cfg["content"]["check_spacing"]        # a single space is never an issue
    assert cfg["content"]["check_callout_icon_label"]
    assert cfg["inline_icons"]["check_alignment"]


def test_the_picture_kinds_survive_the_assets_gate(cfg):
    """[assets] report_types drops every picture finding whose type is not in it, before [genuine]
    types is ever consulted - a name missing there is lost silently."""
    gate = set(cfg["assets"]["report_types"])
    assert {"image alignment", "image smaller", "icon alignment"} <= gate
    assert gate & WANTED == {"image alignment", "image smaller", "icon alignment"}


def test_the_rest_is_off(cfg):
    assert not cfg["typography"]["enabled"]          # the design-spec pass
    assert "toc" in cfg["genuine"]["exclude_checks"]
    for k in ("check_borders", "check_cell_align", "check_header_align", "check_icons",
              "check_single_column_center", "report_cell_differs"):
        assert cfg["tables"][k] is False, k
    for k in ("check_bullets", "check_placement", "check_picture_rows"):
        assert cfg["layout"][k] is False, k
    for k in ("glyphs", "offpage", "links"):
        assert cfg["integrity"][k] is False, k


# --- 4. a table past the right margin ---------------------------------------------------------------

def _margin_pdf(path, table_right: float) -> str:
    """A page of body text in a 72..523pt column, with a table whose right edge is `table_right`."""
    doc = pymupdf.open()
    pg = doc.new_page(width=595, height=842)
    pg.insert_text((72, 70), "Specifications", fontsize=16, fontname="hebo")
    for k in range(6):   # the body lines the page's margins are measured from
        pg.insert_text((72, 100 + 14 * k), "The receiver supports the resolutions listed below in the table.",
                       fontsize=10, fontname="helv")
    y = 200
    for r in range(4):
        pg.draw_rect(pymupdf.Rect(72, y, table_right, y + 22), color=(0, 0, 0), width=0.6)
        pg.draw_line((300, y), (300, y + 22), color=(0, 0, 0), width=0.6)
        pg.insert_text((78, y + 15), f"Item {r + 1}", fontsize=10, fontname="helv")
        pg.insert_text((306, y + 15), f"Value {r + 1}", fontsize=10, fontname="helv")
        y += 22
    doc.set_toc([[1, "Specifications", 1]])
    doc.save(path)
    return str(path)


def test_a_table_inside_the_column_is_not_reported(tmp_path, cfg):
    a = _margin_pdf(tmp_path / "a.pdf", 523)
    b = _margin_pdf(tmp_path / "b.pdf", 523)
    assert not _findings(engine.compare(a, b, cfg), "table past margin")


def test_a_table_past_the_right_margin_is_reported(tmp_path, cfg):
    a = _margin_pdf(tmp_path / "a.pdf", 523)
    b = _margin_pdf(tmp_path / "b.pdf", 570)      # 47pt past the text column
    got = _findings(engine.compare(a, b, cfg), "table past margin")
    assert got, [f["message"] for f in _findings(engine.compare(a, b, cfg))]
    m = got[0]["message"]
    assert "past the right margin" in m and "wider than the text column" in m
    assert got[0]["detail"]["over_right"] > 30
    assert got[0]["genuine"]


def test_a_margin_prod_already_breaks_is_not_b_s_defect(tmp_path, cfg):
    """A hangs over the margin and B draws it the same way: that is how the publication is set, and
    the same copy on both sides must raise nothing."""
    a = _margin_pdf(tmp_path / "a.pdf", 570)
    b = _margin_pdf(tmp_path / "b.pdf", 570)
    assert not _findings(engine.compare(a, b, cfg), "table past margin")


def test_it_can_be_made_absolute_again(tmp_path, cfg):
    cfg["tables"]["table_margin_vs_prod"] = False
    a = _margin_pdf(tmp_path / "a.pdf", 570)
    b = _margin_pdf(tmp_path / "b.pdf", 570)
    assert _findings(engine.compare(a, b, cfg), "table past margin")


def test_the_margin_rule_can_be_turned_off(tmp_path, cfg):
    cfg["tables"]["check_table_margin"] = False
    a, b = _margin_pdf(tmp_path / "a.pdf", 523), _margin_pdf(tmp_path / "b.pdf", 570)
    assert not _findings(engine.compare(a, b, cfg), "table past margin")


# --- 6. an image under 60 % of the original ---------------------------------------------------------

def _image_pdf(path, width: float, x: float = 72) -> str:
    doc = pymupdf.open()
    pg = doc.new_page(width=595, height=842)
    pg.insert_text((72, 70), "Overview", fontsize=16, fontname="hebo")
    for k in range(5):
        pg.insert_text((72, 100 + 14 * k), "Connect the receiver to the display as shown in the picture.",
                       fontsize=10, fontname="helv")
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 240, 160))
    pix.set_rect(pymupdf.IRect(0, 0, 240, 160), (210, 90, 40))
    pix.set_rect(pymupdf.IRect(30, 30, 150, 110), (30, 60, 180))
    pg.insert_image(pymupdf.Rect(x, 200, x + width, 200 + width * 2 / 3), pixmap=pix)
    doc.set_toc([[1, "Overview", 1]])
    doc.save(path)
    return str(path)


def test_an_image_at_70_percent_is_not_reported(tmp_path, cfg):
    """Smaller, but not the half-size-or-less the profile asks for."""
    a, b = _image_pdf(tmp_path / "a.pdf", 300), _image_pdf(tmp_path / "b.pdf", 210)
    assert not _findings(engine.compare(a, b, cfg), "image smaller")


def test_an_image_under_50_percent_is_reported(tmp_path, cfg):
    a, b = _image_pdf(tmp_path / "a.pdf", 300), _image_pdf(tmp_path / "b.pdf", 120)
    got = _findings(engine.compare(a, b, cfg), "image smaller")
    assert got and got[0]["genuine"], [f["message"] for f in _findings(engine.compare(a, b, cfg))]


# --- 8. a hole between two words --------------------------------------------------------------------

def _gap_pdf(path, spaces: int) -> str:
    """One sentence whose words are separated by a single space, except one gap of `spaces`."""
    doc = pymupdf.open()
    pg = doc.new_page(width=595, height=842)
    pg.insert_text((72, 70), "Setting up", fontsize=16, fontname="hebo")
    pg.insert_text((72, 120), "Connect the receiver" + " " * spaces + "to the display before you start.",
                   fontsize=10, fontname="helv")
    pg.insert_text((72, 140), "Keep the ventilation openings free so warm air can leave the housing.",
                   fontsize=10, fontname="helv")
    doc.set_toc([[1, "Setting up", 1]])
    doc.save(path)
    return str(path)


def test_one_space_more_is_not_reported(tmp_path, cfg):
    a, b = _gap_pdf(tmp_path / "a.pdf", 1), _gap_pdf(tmp_path / "b.pdf", 2)
    assert not _findings(engine.compare(a, b, cfg), "wide gap")


def test_a_hole_of_four_spaces_is_reported(tmp_path, cfg):
    a, b = _gap_pdf(tmp_path / "a.pdf", 1), _gap_pdf(tmp_path / "b.pdf", 5)
    got = _findings(engine.compare(a, b, cfg), "wide gap")
    assert got, [f["message"] for f in _findings(engine.compare(a, b, cfg))]
    assert "character widths" in got[0]["message"] and got[0]["genuine"]


def test_the_gap_rule_can_be_turned_off(tmp_path, cfg):
    cfg["content"]["check_wide_gap"] = False
    a, b = _gap_pdf(tmp_path / "a.pdf", 1), _gap_pdf(tmp_path / "b.pdf", 5)
    assert not _findings(engine.compare(a, b, cfg), "wide gap")


# --- 9. a callout's icon against the label stage prints ---------------------------------------------

def test_a_warning_triangle_is_told_from_an_important_disc():
    """The two carry the same bar-and-dot mark; only the outline differs, and the config maps the
    triangle to WARNING and the disc to IMPORTANT. Reading both as "exclamation" made every warning
    in stage look like a callout whose type had changed."""
    import numpy as np
    from pdfval.checks.callout_icons import _triangular
    n = 120
    yy, xx = np.mgrid[0:n, 0:n]
    disc = np.where(((yy - n / 2) ** 2 + (xx - n / 2) ** 2) <= (0.45 * n) ** 2, 30, 240).astype(np.uint8)
    tri = np.where((yy >= 0.1 * n) & (np.abs(xx - n / 2) <= 0.55 * (yy - 0.1 * n)) & (yy <= 0.95 * n),
                   30, 240).astype(np.uint8)
    assert _triangular(tri) and not _triangular(disc)


# --- 7. icon alignment ------------------------------------------------------------------------------

def _icon_pdf(path, icon_dy: float) -> str:
    """A sentence with a small button icon set between two of its words; `icon_dy` moves the icon up
    or down against the line it sits on."""
    doc = pymupdf.open()
    pg = doc.new_page(width=595, height=842)
    pg.insert_text((72, 70), "Using the remote", fontsize=16, fontname="hebo")
    y = 120
    for k in range(4):
        pg.insert_text((72, y + 16 * k), "Aim the remote control at the sensor on the front panel.",
                       fontsize=10, fontname="helv")
    y = 200
    pg.insert_text((72, y), "Press the", fontsize=10, fontname="helv")
    top = y - 9 + icon_dy                      # the icon's box, moved by icon_dy
    pg.draw_rect(pymupdf.Rect(118, top, 130, top + 12), color=(0.1, 0.1, 0.1), fill=(0.85, 0.85, 0.85), width=0.8)
    pg.insert_text((136, y), "key to confirm the new value of the item.", fontsize=10, fontname="helv")
    doc.set_toc([[1, "Using the remote", 1]])
    doc.save(path)
    return str(path)


def test_an_icon_on_the_same_line_position_is_not_reported(tmp_path, cfg):
    a, b = _icon_pdf(tmp_path / "a.pdf", 0), _icon_pdf(tmp_path / "b.pdf", 0)
    assert not _findings(engine.compare(a, b, cfg), "icon alignment")


def test_an_icon_that_sits_lower_on_its_line_is_reported(tmp_path, cfg):
    a, b = _icon_pdf(tmp_path / "a.pdf", 0), _icon_pdf(tmp_path / "b.pdf", 7)
    got = _findings(engine.compare(a, b, cfg), "icon alignment")
    assert got, [f["message"] for f in _findings(engine.compare(a, b, cfg))]
    m = got[0]["message"]
    assert "not aligned with its text" in m.lower() and "lower" in m
    assert got[0]["genuine"]


def test_the_icon_alignment_rule_can_be_turned_off(tmp_path, cfg):
    cfg["inline_icons"]["check_alignment"] = False
    a, b = _icon_pdf(tmp_path / "a.pdf", 0), _icon_pdf(tmp_path / "b.pdf", 7)
    assert not _findings(engine.compare(a, b, cfg), "icon alignment")


def test_a_bigger_image_is_never_held_to_that_floor(tmp_path, cfg):
    """smaller_report_under narrows shrinking only; growing is reported as it always was - and here it
    is not in the profile's list at all, so it stays out of the genuine report."""
    from pdfval.checks.assets import size_reported
    acfg = {"smaller_report_under": 0.6, "width_tolerance": 0.10, "size_ratio_tolerance": 0.05}
    assert size_reported(0.30, 0.15, acfg)        # 50 % of the original: reported
    assert not size_reported(0.30, 0.24, acfg)    # 80 %: left alone
    assert size_reported(0.30, 0.45, acfg)        # grew: unaffected by the floor
