"""Self-tests for the engine: build a baseline PDF, inject known regressions
into a candidate, and assert each check catches exactly what was injected."""
import pymupdf
import pytest

from pdfval import compare, load_config

BODY = "The quick brown fox jumps over the lazy dog near the river bank."


def make_pdf(path, *, heading_size=18, body_color=(0, 0, 0), body_text=BODY, indent=0, extra_section=False,
             second_title="Installation"):
    doc = pymupdf.open()
    toc = []
    for n, title in enumerate(["Overview", second_title], start=1):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 90), title, fontsize=heading_size, fontname="hebo")
        y = 130
        for k in range(4):
            page.insert_text((72 + (indent if k == 2 else 0), y), f"{k + 1}. {body_text}", fontsize=11,
                             fontname="helv", color=body_color)
            y += 16
        toc.append([1, title, n])
        if extra_section and n == 1:
            page.insert_text((72, 260), "Extra part", fontsize=heading_size, fontname="hebo")
            toc.append([2, "Extra part", 1])
    doc.set_toc(toc)
    doc.save(path)
    return str(path)


@pytest.fixture
def cfg():
    c = load_config()
    c["sections"]["front_matter"] = False
    return c


def checks(result, name):
    return [f for s in result["sections"] for f in s["findings"] if f["check"] == name]


def test_identical_pdfs_pass(tmp_path, cfg):
    a = make_pdf(tmp_path / "a.pdf")
    r = compare(a, a, cfg)
    assert r["summary"]["result"] == "pass"
    assert [s["title"] for s in r["sections"]] == ["Overview", "Installation"]


def test_content_change_detected(tmp_path, cfg):
    a = make_pdf(tmp_path / "a.pdf")
    b = make_pdf(tmp_path / "b.pdf", body_text=BODY.replace("lazy", "sleepy"))
    f = checks(compare(a, b, cfg), "content")
    assert f and all("lazy" in x["message"] and "sleepy" in x["message"] for x in f)


def test_style_changes_detected(tmp_path, cfg):
    a = make_pdf(tmp_path / "a.pdf")
    b = make_pdf(tmp_path / "b.pdf", heading_size=14, body_color=(0.8, 0, 0))
    props = {p["property"] for f in checks(compare(a, b, cfg), "style") for p in f["detail"]["props"]}
    assert {"font-size", "color"} <= props


def test_indent_detected(tmp_path, cfg):
    a = make_pdf(tmp_path / "a.pdf")
    b = make_pdf(tmp_path / "b.pdf", indent=24)
    f = checks(compare(a, b, cfg), "layout")
    assert any(x["detail"].get("property") == "indent" for x in f)


def test_structure_missing_and_extra(tmp_path, cfg):
    a = make_pdf(tmp_path / "a.pdf", extra_section=True)
    b = make_pdf(tmp_path / "b.pdf", second_title="Setup")
    msgs = [f["message"] for f in checks(compare(a, b, cfg), "structure")]
    assert any("Extra part" in m and "not found" in m for m in msgs)
    assert any("Installation" in m and "not found" in m for m in msgs)
    assert any("Extra section" in m and "Setup" in m for m in msgs)


def section_by(result, title):
    return next(s for s in result["sections"] if s["title"] == title)


def test_punctuation_and_spacing_are_content(tmp_path, cfg):
    a = make_pdf(tmp_path / "a.pdf")
    b = make_pdf(tmp_path / "b.pdf", body_text=BODY.replace("bank.", "bank").replace("over the", "over  the"))
    r = compare(a, b, cfg)
    ops = {f["detail"].get("op") for f in checks(r, "content")}
    assert "spacing" in ops and "replace" in ops
    assert section_by(r, "Overview")["content"]["match_pct"] < 100


def test_css_does_not_change_content_percentage(tmp_path, cfg):
    a = make_pdf(tmp_path / "a.pdf")
    b = make_pdf(tmp_path / "b.pdf", heading_size=14, body_color=(0.8, 0, 0))
    s = section_by(compare(a, b, cfg), "Overview")
    assert s["content"]["match_pct"] == 100 and s["content"]["status"] == "pass"
    assert s["css"]["issues"] > 0 and s["critical"] == 0


def make_table_pdf(path, rows, table=True):
    """rows: (label, value) or a single string = one cell spanning both columns (merged row)."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 90), "Specs", fontsize=18, fontname="hebo")
    x0, x1, xm, y = 72, 520, 240, 120
    for k, row in enumerate(rows):
        if table:
            page.draw_rect(pymupdf.Rect(x0, y, x1, y + 24), color=(0, 0, 0), width=0.7)
        if isinstance(row, str):
            page.insert_text((x0 + 6, y + 16), row, fontsize=10, fontname="helv")
        else:
            if table:
                page.draw_line((xm, y), (xm, y + 24), color=(0, 0, 0), width=0.7)
            page.insert_text((x0 + 6, y + 16), row[0], fontsize=10, fontname="helv")
            page.insert_text((xm + 6 if table else x0 + 6 + 8 * len(row[0]), y + 16), row[1], fontsize=10, fontname="helv")
        y += 24
    page.insert_text((72, y + 40), "End of the specification list.", fontsize=11, fontname="helv")
    doc.set_toc([[1, "Specs", 1]])
    doc.save(path)
    return str(path)


def test_missing_table_row_is_critical(tmp_path, cfg):
    rows = [("Model", "SL4304"), ("Storage", "64 GB"), ("Operating system", "Android 13"), ("Memory", "8 GB DDR4")]
    a = make_table_pdf(tmp_path / "a.pdf", rows)
    b = make_table_pdf(tmp_path / "b.pdf", [r for r in rows if r[0] != "Storage"])
    r = compare(a, b, cfg)
    missing = [f for f in checks(r, "tables") if "missing row" in f["types"]]
    assert len(missing) == 1 and "Storage" in missing[0]["message"] and missing[0]["critical"]
    assert section_by(r, "Specs")["status"] == "fail"


def test_missing_section_is_critical(tmp_path, cfg):
    a = make_pdf(tmp_path / "a.pdf")
    b = make_pdf(tmp_path / "b.pdf", second_title="Setup")
    crit = [f for s in compare(a, b, cfg)["sections"] for f in s["findings"] if f["critical"]]
    assert any("Installation" in f["message"] for f in crit)


def make_icon_pdf(path, tmp_path, inline: bool):
    """'2. Select [icon] to open the menu.' with the icon inline, or dropped below 'Select'."""
    from PIL import Image as PILImage, ImageDraw
    icon = tmp_path / "icon.png"
    im = PILImage.new("RGB", (40, 40), "white")
    ImageDraw.Draw(im).rectangle([6, 6, 34, 34], outline="black", width=4)
    ImageDraw.Draw(im).line([6, 6, 34, 34], fill="black", width=4)
    im.save(icon)
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 90), "Blank screen", fontsize=18, fontname="hebo")
    page.insert_text((72, 130), "1. Open the Settings menu.", fontsize=11, fontname="helv")
    page.insert_text((72, 150), "2. Select", fontsize=11, fontname="helv")
    if inline:
        page.insert_image(pymupdf.Rect(122, 139, 136, 153), filename=str(icon))
        page.insert_text((140, 150), "to open the menu.", fontsize=11, fontname="helv")
    else:
        page.insert_image(pymupdf.Rect(90, 156, 104, 170), filename=str(icon))
        page.insert_text((72, 186), "to open the menu.", fontsize=11, fontname="helv")
    page.insert_text((72, 220), "3. Confirm the change on the display.", fontsize=11, fontname="helv")
    doc.set_toc([[1, "Blank screen", 1]])
    doc.save(path)
    return str(path)


def test_inline_icon_dropped_below_text_is_flagged(tmp_path, cfg):
    a = make_icon_pdf(tmp_path / "a.pdf", tmp_path, inline=True)
    same = make_icon_pdf(tmp_path / "same.pdf", tmp_path, inline=True)
    moved = make_icon_pdf(tmp_path / "b.pdf", tmp_path, inline=False)
    placement = lambda r: [f for s in r["sections"] for f in s["findings"]
                           if f["category"] == "images" and "placement" in f["types"]]
    assert placement(compare(a, same, cfg)) == []
    found = placement(compare(a, moved, cfg))
    assert len(found) == 1 and "dropped out of its line" in found[0]["message"] and "Select" in found[0]["message"]


ROWS = [("Model", "SL4304"), ("Storage", "64 GB"), ("Operating system", "Android 13"), ("Memory", "8 GB DDR4")]


def test_rows_merged_are_flagged(tmp_path, cfg):
    a = make_table_pdf(tmp_path / "a.pdf", ROWS)
    b = make_table_pdf(tmp_path / "b.pdf", [ROWS[0], "Storage 64 GB Operating system Android 13", ROWS[3]])
    types = [t for f in checks(compare(a, b, cfg), "tables") for t in f["types"]]
    assert "rows merged" in types


def test_missing_table_is_critical(tmp_path, cfg):
    a = make_table_pdf(tmp_path / "a.pdf", ROWS)
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 90), "Specs", fontsize=18, fontname="hebo")
    page.insert_text((72, 160), "End of the specification list.", fontsize=11, fontname="helv")
    doc.set_toc([[1, "Specs", 1]])
    doc.save(tmp_path / "b.pdf")
    fs = [f for f in checks(compare(a, str(tmp_path / "b.pdf"), cfg), "tables") if "missing table" in f["types"]]
    assert len(fs) == 1 and fs[0]["critical"]


def test_content_types_case_punctuation_spacing(tmp_path, cfg):
    a = make_pdf(tmp_path / "a.pdf", body_text="See the quick brown fox jumps over the lazy dog, near the bank.")
    b = make_pdf(tmp_path / "b.pdf", body_text="SEE the quick brown fox jumps over the lazy dog; near the bank .")
    r = compare(a, b, cfg)
    types = {t for s in r["sections"] for f in s["findings"] if f["category"] == "content" for t in f["types"]}
    assert {"case", "punctuation", "spacing"} <= types


def test_callout_labels_are_house_style(tmp_path, cfg):
    """"Tips" -> "TIPS:", "Note" -> "NOTE:", "Warning" -> "WARNING:" are expected; "Tip" -> "NOTE:" is not."""
    a = make_pdf(tmp_path / "a.pdf", body_text="Tips Keep the remote control dry. Note Unplug the cord. Warning Hot surface.")
    b = make_pdf(tmp_path / "b.pdf", body_text="TIPS: Keep the remote control dry. NOTE: Unplug the cord. WARNING: Hot surface.")
    r = compare(a, b, cfg)
    assert not [f for s in r["sections"] for f in s["findings"] if f["category"] == "content"]
    c = make_pdf(tmp_path / "c.pdf", body_text="NOTE: Keep the remote control dry. NOTE: Unplug the cord. WARNING: Hot surface.")
    r = compare(a, c, cfg)
    assert [f for s in r["sections"] for f in s["findings"] if f["category"] == "content" and "Tips" in f["message"]]
    assert all(f["category"] == "content" for s in r["sections"] for f in s["findings"] if f["check"] == "content")


def make_toc_pdf(path, toc_entries, headings):
    """Page 1: printed TOC ('Title ....... 2' at indent per level); page 2+: the headings."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 70), "Table of contents", fontsize=16, fontname="hebo")
    y = 100
    for level, title, pg in toc_entries:
        x = 72 + 14 * (level - 1)
        page.insert_text((x, y), title + " " + "." * (60 - len(title) - 2 * level) + f" {pg}", fontsize=10, fontname="helv")
        y += 16
    bookmarks = []
    for title in headings:
        p = doc.new_page(width=595, height=842)
        p.insert_text((72, 90), title, fontsize=16, fontname="hebo")
        p.insert_text((72, 130), f"Body text about {title.lower()} for the reader.", fontsize=11, fontname="helv")
        bookmarks.append([1, title, doc.page_count])
    doc.set_toc(bookmarks)
    doc.save(path)
    return str(path)


def test_toc_levels_missing_extra_and_not_content(tmp_path, cfg):
    heads = ["Overview", "Setup", "Mounting", "Settings"]
    a = make_toc_pdf(tmp_path / "a.pdf", [(1, "Overview", 2), (2, "Setup", 3), (3, "Mounting", 4), (1, "Settings", 5)], heads)
    b = make_toc_pdf(tmp_path / "b.pdf", [(1, "Overview", 2), (2, "Setup", 3), (2, "Mounting", 4), (2, "Extras", 4)], heads)
    cfg["toc"]["max_level"] = 0  # every level
    r = compare(a, b, cfg)
    status = {(row["baseline"] or row["candidate"])["title"]: row["status"] for row in r["toc"]["rows"]}
    assert status == {"Overview": "match", "Setup": "match", "Mounting": "level differs",
                      "Settings": "missing in stage", "Extras": "extra in stage"}
    content = [f for s in r["sections"] for f in s["findings"] if f["category"] == "content"]
    assert not any("...." in f["message"] or "Extras" in f["message"] for f in content)
    assert {t for s in r["sections"] for f in s["findings"] if f["category"] == "toc" for t in f["types"]} >= \
        {"level differs", "missing entry", "extra entry"}


def test_toc_top_level_only_by_default(tmp_path, cfg):
    """Only level-1 entries are validated: deeper entries are ignored unless the title is level 1
    on the other side (then it is a level difference, not a missing entry)."""
    heads = ["Overview", "Setup", "Mounting", "Settings"]
    a = make_toc_pdf(tmp_path / "a.pdf", [(1, "Overview", 2), (2, "Setup", 3), (1, "Mounting", 4), (1, "Settings", 5)], heads)
    b = make_toc_pdf(tmp_path / "b.pdf", [(1, "Overview", 2), (2, "Extras", 3), (2, "Mounting", 4), (1, "Settings", 5)], heads)
    r = compare(a, b, cfg)
    status = {(row["baseline"] or row["candidate"])["title"]: row["status"] for row in r["toc"]["rows"]}
    assert status == {"Overview": "match", "Mounting": "level differs", "Settings": "match"}


def test_toc_sequence_swapped_entries(tmp_path, cfg):
    heads = ["Overview", "Setup", "Mounting", "Settings", "Support"]
    toc_a = [(1, "Overview", 2), (1, "Setup", 3), (1, "Mounting", 4), (1, "Settings", 5), (1, "Support", 6)]
    toc_b = [(1, "Overview", 2), (1, "Setup", 3), (1, "Settings", 5), (1, "Support", 6), (1, "Mounting", 4)]
    r = compare(make_toc_pdf(tmp_path / "a.pdf", toc_a, heads), make_toc_pdf(tmp_path / "b.pdf", toc_b, heads), cfg)
    t = r["toc"]
    moved = [row for row in t["rows"] if row["status"] == "order differs"]
    assert len(moved) == 1 and moved[0]["baseline"]["title"] == "Mounting"
    assert moved[0]["pos"] == {"baseline": 3, "candidate": 5}
    assert t["summary"]["sequence_ok"] is False and t["summary"]["status"] == "fail"


def _wrapped_pdf(path, lines):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 90), "Overview", fontsize=18, fontname="hebo")
    y = 130
    for t in lines:
        page.insert_text((72, y), t, fontsize=11, fontname="helv")
        y += 14
    doc.set_toc([[1, "Overview", 1]])
    doc.save(path)
    return str(path)


def test_line_wrap_is_layout_not_content(tmp_path, cfg):
    a = _wrapped_pdf(tmp_path / "a.pdf", ["Supported models SL6504/", "SL7504/ and the quick brown fox jumps over", "the lazy dog."])
    b = _wrapped_pdf(tmp_path / "b.pdf", ["Supported models SL6504/SL7504/ and the quick brown fox", "jumps over the lazy dog."])
    cfg["layout"]["check_wrap"] = True  # off by default
    r = compare(a, b, cfg)
    assert not checks(r, "content")
    assert section_by(r, "Overview")["content"]["match_pct"] == 100
    wraps = [f for f in checks(r, "layout") if f["detail"].get("property") == "line-wrap"]
    assert wraps and wraps[0]["detail"]["places"] >= 2


def test_pdf_vs_web_page_toc_driven(tmp_path, cfg):
    """PDF vs HTML: sections come from the PDF TOC matched to the page's headings;
    per-section text differences, heading level and order differences are reported,
    and site navigation is not treated as content."""
    from pdfval.engine import compare_url
    cfg["toc"]["max_level"] = 0  # the page's heading levels are part of this test
    heads = ["Overview", "Setup", "Mounting", "Settings"]
    pdf = make_toc_pdf(tmp_path / "a.pdf", [(1, "Overview", 2), (2, "Setup", 3), (2, "Mounting", 4), (1, "Settings", 5)], heads)
    body = {h: f"Body text about {h.lower()} for the reader." for h in heads}
    body["Setup"] = "Body text about setup for the Reader."  # case change
    html = ("<html><body><nav>Home Docs Products</nav><main>"
            "<h1>Overview</h1><p>" + body["Overview"] + "</p>"
            "<h1>Setup</h1><p>" + body["Setup"] + "</p>"          # level 1 in HTML, level 2 in the PDF TOC
            "<h1>Settings</h1><p>" + body["Settings"] + "</p>"
            "<h2>Mounting</h2><p>" + body["Mounting"] + "</p>"     # moved after Settings
            "</main><footer>Footer text</footer></body></html>")
    (tmp_path / "page.html").write_text(html)
    cfg2 = dict(cfg)
    r = compare_url(pdf, (tmp_path / "page.html").as_uri(), str(tmp_path / "run"), cfg2)
    status = {(row["baseline"] or row["candidate"])["title"]: row["status"] for row in r["toc"]["rows"]}
    assert status["Setup"] == "level differs" and status["Mounting"] == "order differs"
    assert r["toc"]["summary"]["sequence_ok"] is False
    content = [f for s in r["sections"] for f in s["findings"] if f["category"] == "content"]
    assert any("case" in f["types"] and "reader" in f["message"].lower() for f in content)
    assert not any("Home Docs" in f["message"] or "Footer" in f["message"] for f in content)
    mounting = next(s for s in r["sections"] if s["title"] == "Mounting")
    assert mounting["content"]["match_pct"] == 100.0  # moved section compared with its own counterpart


def test_cells_merged_is_flagged(tmp_path, cfg):
    """A row whose two cells became one spanning cell (the code path that crashed on PD06U)."""
    a = make_table_pdf(tmp_path / "a.pdf", ROWS)
    b = make_table_pdf(tmp_path / "b.pdf", [ROWS[0], "Storage 64 GB", ROWS[2], ROWS[3]])
    types = [t for f in checks(compare(a, b, cfg), "tables") for t in f["types"]]
    assert "cells merged" in types


def _list_pdf(path, bullet_dx, gap=6, marker_after=False):
    """A label line and three bullet items; bullet_dx: marker offset from the label."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 90), "Overview", fontsize=18, fontname="hebo")
    page.insert_text((72, 130), "Important", fontsize=11, fontname="hebo")
    y = 146
    for t in ("Keep the box for transport.", "Do not leave bags near children.", "Recycle the carton."):
        if not marker_after:
            page.insert_text((72 + bullet_dx, y), "•", fontsize=11, fontname="helv")
        page.insert_text((72 + bullet_dx + gap, y), t, fontsize=11, fontname="helv")
        if marker_after:  # bullet stored after its text in reading order
            page.insert_text((72 + bullet_dx, y), "•", fontsize=11, fontname="helv")
        y += 14
    doc.set_toc([[1, "Overview", 1]])
    doc.save(path)
    return str(path)


def test_bullet_alignment_is_an_indent_issue(tmp_path, cfg):
    a = _list_pdf(tmp_path / "a.pdf", 0)
    b = _list_pdf(tmp_path / "b.pdf", 9, marker_after=True)
    bullets = [f for f in checks(compare(a, b, cfg), "layout") if "bullet" in f["types"]]
    assert bullets and all("indent" in f["types"] for f in bullets)
    assert any(f["detail"]["kind"] == "bullet indent" and f["detail"]["lines"] == 3 for f in bullets)  # the whole list shifted
    same = [f for f in checks(compare(a, _list_pdf(tmp_path / "c.pdf", 0, marker_after=True), cfg), "layout")
            if "bullet" in f["types"]]
    assert not same  # same alignment, bullet only stored elsewhere in reading order


def _numbered_pdf(path, labels):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 90), "Overview", fontsize=18, fontname="hebo")
    page.insert_text((72, 130), "Remove the box as follows.", fontsize=11, fontname="helv")
    for k, (lab, t) in enumerate(zip(labels, ("Cut the straps.", "Unfasten the clips.", "Lift the cover."))):
        page.insert_text((84, 146 + 14 * k), lab, fontsize=11, fontname="helv")
        page.insert_text((100, 146 + 14 * k), t, fontsize=11, fontname="helv")
    doc.set_toc([[1, "Overview", 1]])
    doc.save(path)
    return str(path)


def test_list_numbering_style_format_and_sequence(tmp_path, cfg):
    a = _numbered_pdf(tmp_path / "a.pdf", ["a.", "b.", "c."])
    kinds = lambda b: {(f["detail"]["kind"], f["detail"]["baseline"], f["detail"]["candidate"])
                       for f in checks(compare(a, b, cfg), "layout") if "bullet" in f["types"]}
    roman = kinds(_numbered_pdf(tmp_path / "b.pdf", ["i.", "ii.", "ii."]))
    assert ("numbering style", "a, b, c", "i, ii, iii") in roman
    assert any(k == "numbering sequence" and "expected “iii.”" in c for k, _, c in roman)
    paren = kinds(_numbered_pdf(tmp_path / "c.pdf", ["a)", "b)", "c)"]))
    assert {k for k, _, _ in paren} == {"numbering format"}
    assert not kinds(_numbered_pdf(tmp_path / "d.pdf", ["a.", "b.", "c."]))


def _note_pdf(path, image_inside):
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 30), False)
    for xx in range(40):
        for yy in range(30):
            pix.set_pixel(xx, yy, ((xx * 6) % 256, (yy * 8) % 256, 90))
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 90), "Overview", fontsize=18, fontname="hebo")
    page.insert_text((72, 130), "Place the display on a flat surface.", fontsize=11, fontname="helv")
    page.draw_rect(pymupdf.Rect(66, 150, 480, 260), color=None, fill=(0.85, 0.9, 0.95))  # the note panel
    page.insert_text((76, 170), "Note: keep the ventilation openings clear.", fontsize=11, fontname="helv")
    r = pymupdf.Rect(76, 185, 156, 245) if image_inside else pymupdf.Rect(76, 280, 156, 340)
    page.insert_image(r, stream=pix.tobytes("png"))
    page.insert_text((72, 380), "Then connect the power cord.", fontsize=11, fontname="helv")
    doc.set_toc([[1, "Overview", 1]])
    doc.save(path)
    return str(path)


def test_image_outside_its_note_box(tmp_path, cfg):
    a = _note_pdf(tmp_path / "a.pdf", True)
    out = [f for f in checks(compare(a, _note_pdf(tmp_path / "b.pdf", False), cfg), "assets")
           if "image outside box" in f["types"]]
    assert out and "Note: keep the ventilation" in out[0]["message"] and out[0]["genuine"]
    same = [f for f in checks(compare(a, _note_pdf(tmp_path / "c.pdf", True), cfg), "assets")
            if "image outside box" in f["types"]]
    assert not same


def _steps_cell_pdf(path, numbered):
    """A table-like row: an action on the left, numbered steps in the right cell."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 90), "Overview", fontsize=18, fontname="hebo")
    page.insert_text((72, 140), "Change the language", fontsize=11, fontname="helv")
    for k, t in enumerate(("Select Languages.", "Select a language from the list.")):
        if numbered:
            page.insert_text((280, 140 + 16 * k), f"{k + 1}.", fontsize=11, fontname="helv")
        page.insert_text((296, 140 + 16 * k), t, fontsize=11, fontname="helv")
    doc.set_toc([[1, "Overview", 1]])
    doc.save(path)
    return str(path)


def test_list_numbers_missing_in_a_table_cell(tmp_path, cfg):
    r = compare(_steps_cell_pdf(tmp_path / "a.pdf", True), _steps_cell_pdf(tmp_path / "b.pdf", False), cfg)
    f = [f for f in checks(r, "layout") if f["detail"].get("kind") == "bullet marker"]
    assert f and f[0]["detail"]["lines"] == 2 and "“1.”, “2.”" in f[0]["message"] and "missing in stage" in f[0]["message"]
    assert f[0]["genuine"]


def test_toc_issues_stay_out_of_the_genuine_report(tmp_path, cfg):
    """A wrong TOC page number is reported (full report, TOC tab) but is not a genuine issue,
    unless `[genuine] exclude_checks` no longer lists "toc"."""
    heads = ["Overview", "Setup", "Mounting", "Settings"]
    a = make_toc_pdf(tmp_path / "a.pdf", [(1, "Overview", 2), (1, "Setup", 3), (1, "Mounting", 4), (1, "Settings", 5)], heads)
    b = make_toc_pdf(tmp_path / "b.pdf", [(1, "Overview", 2), (1, "Setup", 3), (1, "Mounting", 9), (1, "Settings", 5)], heads)
    toc = lambda r: [f for s in r["sections"] for f in s["findings"] if f["check"] == "toc" and "Mounting" in f["message"]]
    r = compare(a, b, cfg)
    assert toc(r) and not any(f["genuine"] for s in r["sections"] for f in s["findings"] if f["check"] == "toc")
    cfg["genuine"]["exclude_checks"] = []
    assert [f for f in toc(compare(a, b, cfg)) if f.get("issue") == "TOC page number wrong"]


def test_genuine_issues_are_never_capped(tmp_path, cfg):
    """The per-check cap thins CSS/layout findings only; every genuine issue is kept."""
    words = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa".split()
    a = make_pdf(tmp_path / "a.pdf", body_text=" ".join(words))
    b = make_pdf(tmp_path / "b.pdf", body_text=" ".join(w.upper() if i % 4 == 1 else w for i, w in enumerate(words)))
    count = lambda r: len([f for s in r["sections"] for f in s["findings"] if f.get("genuine") and f["category"] == "content"])
    full = count(compare(a, b, cfg))
    cfg["report"]["max_findings_per_check"] = 1
    assert full >= 3 and count(compare(a, b, cfg)) == full


def _two_column_list(path, rows_a, rows_b, gap, note):
    """A package-contents list in two columns: "• 1 x ..." items, bullet `gap` pt before the text."""
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 60), "Package Contents", fontsize=18)
    p.insert_text((72, 84), "The package should include the following items.", fontsize=11)
    for x, ys, items in ((72, rows_a, ["ScreenBar lamp with cord", "Wireless controller", "Quick Start Guide"]),
                         (300, rows_b, ["Warranty information", "Power adapter", "Webcam accessory"])):
        for y, t in zip(ys, items):
            p.insert_text((x, y), "•", fontsize=11)
            p.insert_text((x + 5 + gap, y), f"1 x {t}", fontsize=11)
    y = max(rows_a + rows_b) + 30
    if note:
        p.insert_text((72, y), "NOTE:", fontsize=11)
    p.insert_text((72 + (40 if note else 0), y), "The illustrations are for your reference only.", fontsize=11)
    doc.save(path)
    return str(path)


def test_two_column_list_read_in_another_order(tmp_path, cfg):
    """Prod's two columns have different line spacing, so reading by height interleaves them
    ("Webcam accessory" before "Quick Start Guide"); stage reads it last, just before "NOTE:".
    The moved item is not extra content, and the bullet gap is compared on all six items."""
    a = _two_column_list(tmp_path / "a.pdf", [110, 129, 140], [110, 121, 132], gap=0, note=False)
    b = _two_column_list(tmp_path / "b.pdf", [110, 127, 144], [110, 127, 144], gap=6, note=True)
    r = compare(a, b, cfg)
    content = [f["message"] for f in checks(r, "content") if "reordered" not in (f.get("types") or [])]
    assert not [m for m in content if "Webcam" in m], content
    assert [m for m in content if "NOTE:" in m], content
    gap = [f for f in checks(r, "layout") if f["detail"].get("kind") == "bullet gap"]
    assert gap and gap[0]["detail"]["lines"] == 6 and "Webcam" not in gap[0]["message"].split("e.g.")[0]


def _figure_pdf(path, labels_as_text: bool, labels=("50 cm", "60±10 cm")):
    """A section with a figure whose labels are text over the drawing (prod) or drawn into the
    picture itself (stage), on the next page and at another size."""
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 60), "Presence Detection", fontsize=18)
    p.insert_text((72, 90), "The function turns the light on when the user enters the area.", fontsize=11)
    if labels_as_text:  # a line drawing (curves, like the lamp art) with its labels as text
        p.draw_oval(pymupdf.Rect(72, 120, 372, 300), color=(0, 0, 0))
        for k, t in enumerate(labels):
            p.insert_text((90 + 150 * k, 200), t, fontsize=8)
    else:
        art = pymupdf.open()
        a = art.new_page(width=400, height=220)
        a.draw_rect(pymupdf.Rect(10, 10, 390, 210), color=(0, 0, 0))
        for k, t in enumerate(labels):
            a.insert_text((40 + 190 * k, 110), t, fontsize=14, fontname="helv")
        png = a.get_pixmap(dpi=200).tobytes("png")
        p = doc.new_page()  # the picture lands on the next page
        p.insert_image(pymupdf.Rect(72, 60, 472, 280), stream=png)
    doc.save(path)
    return str(path)


@pytest.mark.skipif(not __import__("shutil").which("tesseract"), reason="needs tesseract")
def test_labels_drawn_in_the_stage_picture_are_not_missing(tmp_path, cfg):
    a = _figure_pdf(tmp_path / "a.pdf", True)
    b = _figure_pdf(tmp_path / "b.pdf", False)
    r = compare(a, b, cfg)
    fs = [f for f in checks(r, "content") if "cm" in f["message"]]
    assert fs and all("text in image" in f["types"] and not f["genuine"] for f in fs), [f["message"] for f in fs]
    # a label the stage picture does not have stays missing
    b2 = _figure_pdf(tmp_path / "b2.pdf", False, labels=("50 cm", "Ultrasonic"))
    missing = [f for f in checks(compare(a, b2, cfg), "content") if "missing text" in f["types"]]
    assert missing and any("60±10" in f["message"] for f in missing)


def test_ocr_match_rules():
    from pdfval.ocr import found
    ocr = "/ /, 50cm: ;— Vy bO#l0 cm\n(ee V 60210 c m See 0.43cm ~6cm 4 1 A B"
    assert found("50 cm 60±10 cm", ocr)            # "±" misread, a digit read as a letter
    assert found("0.43cm ~ 6cm B 2", ocr)           # circled "❷" is not readable: ignored
    assert not found("A B 1", ocr)                  # single characters are in any picture
    assert not found("Ultrasonic sensor", ocr)


def _badge_pdf(path, badge_as_picture: bool, letter="A"):
    """“Extend the clip as shown in [Figure (A)].” – the badge is a white letter on a drawn circle
    (prod) or a picture of the badge (stage), which has no live text."""
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 60), "Installing the Lamp", fontsize=18)
    p.insert_text((72, 100), "1. Extend the clip of the lamp as shown in [Figure", fontsize=11)
    x = 72 + pymupdf.get_text_length("1. Extend the clip of the lamp as shown in [Figure", fontsize=11) + 3
    if badge_as_picture:
        art = pymupdf.open()
        a = art.new_page(width=14, height=12)
        a.draw_oval(pymupdf.Rect(0, 0, 14, 12), color=(0, 0, 0), fill=(0, 0, 0))
        a.insert_text((3.6, 9.8), letter, fontsize=10, color=(1, 1, 1))
        p.insert_image(pymupdf.Rect(x, 91, x + 14, 103), stream=a.get_pixmap(dpi=400).tobytes("png"))
    else:
        p.draw_oval(pymupdf.Rect(x, 91, x + 14, 103), color=(0, 0, 0), fill=(0, 0, 0))
        p.insert_text((x + 3.6, 100.8), letter, fontsize=10, color=(1, 1, 1))
    p.insert_text((x + 16, 100), "].", fontsize=11)
    p.insert_text((72, 130), "2. Rest the lamp on the monitor bezel so there is no visible gap.", fontsize=11)
    doc.save(path)
    return str(path)


def test_badge_drawn_as_a_picture_is_a_layout_issue_not_missing_text(tmp_path, cfg):
    a = _badge_pdf(tmp_path / "a.pdf", False)
    r = compare(a, _badge_pdf(tmp_path / "b.pdf", True), cfg)
    assert not [f for f in checks(r, "content") if "missing text" in f["types"]]
    fs = [f for f in checks(r, "layout") if "text as graphic" in f["types"]]
    assert len(fs) == 1 and not fs[0]["genuine"] and "“A”" in fs[0]["message"]
    # another letter in the stage badge: the prod text really is not there
    r2 = compare(a, _badge_pdf(tmp_path / "b2.pdf", True, letter="W"), cfg)
    assert [f for f in checks(r2, "content") if "missing text" in f["types"]]


def _safety_pdf(path, edited: bool, order=(0, 1, 2)):
    """Three numbered safety items; stage drops an apostrophe, spaces out “40°C/” and lays the items
    out in another order (another column order), so each change is met out of reading order."""
    q, t = ("", "40°C / 104°F") if edited else ("'", "40°C/ 104°F")
    items = [f"16. If the projector does become wet, disconnect it from the power supply{q}s power outlet and call BenQ.",
             "17. This product is capable of displaying inverted images for ceiling mount installation today.",
             f"20. Do not place it in locations with an ambient temperature above {t} or near fire alarms."]
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 60), "Important safety instructions", fontsize=18)
    for k, n in enumerate(order):
        p.insert_textbox(pymupdf.Rect(72, 90 + 70 * k, 540, 150 + 70 * k), items[n], fontsize=10)
    doc.save(path)
    return str(path)


def test_punctuation_change_in_reordered_text_is_one_paired_finding(tmp_path, cfg):
    a = _safety_pdf(tmp_path / "a.pdf", False)
    r = compare(a, _safety_pdf(tmp_path / "b.pdf", True, order=(2, 1, 0)), cfg)
    fs = [f for f in checks(r, "content") if "supply" in f["message"] or "40°C" in f["message"]]
    assert len(fs) == 2 and all(f["types"] in (["punctuation"], ["spacing"]) and f["baseline"] and f["candidate"]
                                for f in fs), [(f["types"], f["message"]) for f in fs]
    assert not [f for f in checks(r, "content") if {"missing text", "extra text"} & set(f["types"])]


CHAPTERS = [("Package contents", 1, "Check that all the items are in the package before you start."),
            ("Port overview", 1, "The ports are on the front and on the rear of the unit."),
            ("Front", 2, "The front has the antenna port and two USB ports for devices."),
            ("Initial setup", 1, "Secure the two antennas to the front of the unit before use."),
            ("Mounting the unit", 2, "Insert the unit into the slot of the display until it clicks.")]


def _guide_pdf(path):
    """A printed manual: cover, printed TOC, chapters (no bookmarks: levels from font sizes), back cover."""
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 200), "User Manual", fontsize=40)
    p.insert_text((72, 260), "BenQ Chromebox OPS", fontsize=24)
    p = doc.new_page()
    p.insert_text((72, 80), "Table of contents", fontsize=22)
    for k, (t, lvl, _) in enumerate(CHAPTERS):
        p.insert_text((72 + 20 * (lvl - 1), 120 + 20 * k), f"{t} {'.' * 40} {k + 3}", fontsize=11)
    for t, lvl, body in CHAPTERS:
        p = doc.new_page()
        p.insert_text((72, 90), t, fontsize=22 if lvl == 1 else 16)
        p.insert_text((72, 130), body, fontsize=11)
    p = doc.new_page()
    for k, t in enumerate(("Shape", "the Future")):
        p.insert_text((72, 300 + 40 * k), t, fontsize=30)
    doc.save(path)
    return str(path)


def _site_doc(path, cfg):
    """The web guide as the crawl builds it: one page per chapter, page title = level-1 heading."""
    from pdfval.extract import load
    doc = pymupdf.open()
    toc = []
    for t, lvl, body in CHAPTERS:
        p = doc.new_page() if lvl == 1 else doc[-1]
        y = 90 if lvl == 1 else 200
        p.insert_text((72, y), t, fontsize=22 if lvl == 1 else 16)
        p.insert_text((72, y + 40), body, fontsize=11)
        toc.append([lvl, t, doc.page_count])
    doc.set_toc(toc)
    doc.save(path)
    return load(str(path), "candidate", cfg)


def test_web_guide_skips_print_only_pages_and_compares_heading_depth(tmp_path, cfg):
    a = _guide_pdf(tmp_path / "a.pdf")
    b = _site_doc(tmp_path / "b.pdf", cfg)
    r = compare(a, b.path, cfg, candidate_doc=b, candidate_meta={"mode": "html", "page_title": "Package contents"})
    msgs = [f["message"] for f in checks(r, "structure")]
    assert not [m for m in msgs if "not found in candidate" in m], msgs  # cover, TOC page, back cover
    assert not [m for m in msgs if m.startswith("Outline level")], msgs  # H2/H3 in the PDF = h1/h2 on the web
