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
    a = make_pdf(tmp_path / "a.pdf", body_text="Note the quick brown fox jumps over the lazy dog, near the bank.")
    b = make_pdf(tmp_path / "b.pdf", body_text="NOTE the quick brown fox jumps over the lazy dog; near the bank .")
    r = compare(a, b, cfg)
    types = {t for s in r["sections"] for f in s["findings"] if f["category"] == "content" for t in f["types"]}
    assert {"case", "punctuation", "spacing"} <= types
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
    r = compare(a, b, cfg)
    status = {(row["baseline"] or row["candidate"])["title"]: row["status"] for row in r["toc"]["rows"]}
    assert status == {"Overview": "match", "Setup": "match", "Mounting": "level differs",
                      "Settings": "missing in stage", "Extras": "extra in stage"}
    content = [f for s in r["sections"] for f in s["findings"] if f["category"] == "content"]
    assert not any("...." in f["message"] or "Extras" in f["message"] for f in content)
    assert {t for s in r["sections"] for f in s["findings"] if f["category"] == "toc" for t in f["types"]} >= \
        {"level differs", "missing entry", "extra entry"}


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
