"""Genuine issues: build a prod PDF, inject one real regression into stage, and assert
it is reported as a genuine issue (and that presentation changes are not)."""
import pymupdf
import pytest

from pdfval import compare, load_config

P1 = "The display turns on when you press the power button on the remote control unit."
P2 = "Connect the power cord to the wall outlet before you switch on the display panel."
MOVED = "Keep the ventilation openings free so that warm air can leave the housing easily."


def _picture(seed: int) -> bytes:
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 64, 48), False)
    for x in range(64):
        for y in range(48):
            pix.set_pixel(x, y, ((x * 4 * seed) % 256, (y * 5) % 256, ((x + y) * 3 * seed) % 256))
    return pix.tobytes("png")


def _blank() -> bytes:
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 64, 48), False)
    pix.clear_with(255)
    return pix.tobytes("png")


def _table(page, y, rows):
    x = [72, 250, 450]
    for r, cells in enumerate(rows):
        for c, text in enumerate(cells):
            page.draw_rect(pymupdf.Rect(x[c], y + r * 22, x[c + 1], y + (r + 1) * 22), color=(0, 0, 0), width=0.8)
            page.insert_text((x[c] + 4, y + r * 22 + 15), text, fontsize=10, fontname="hebo" if r == 0 and header_bold else "helv")
    return y + len(rows) * 22


header_bold = True
ROWS = [["Setting", "Value"], ["Brightness", "80 percent"], ["Contrast", "60 percent"], ["Volume", "25 steps"]]


def make(path, sections):
    """sections: [{title, paras, image: png|None, table: rows|None, link: target title|None}]"""
    doc = pymupdf.open()
    toc, pages = [], {}
    for s in sections:
        page = doc.new_page(width=595, height=842)
        pages.setdefault(s["title"], page.number)
        page.insert_text((72, 90), s["title"], fontsize=18, fontname="hebo")
        toc.append([1, s["title"], page.number + 1])
        y = 130
        for p in s.get("paras", []):
            page.insert_text((72, y), p, fontsize=10, fontname=s.get("font", "helv"))
            y += 18
        if s.get("link"):
            page.insert_text((72, y), "For details, see the next topic.", fontsize=10, fontname="helv")
            s["_link"] = (page.number, pymupdf.Rect(72, y - 10, 300, y + 3))
            y += 18
        if s.get("image"):
            page.insert_image(pymupdf.Rect(72, y + 10, 272, y + 160), stream=s["image"])
            y += 170
        if s.get("table"):
            y = _table(page, y + 10, s["table"])
    for s in sections:
        if s.get("_link"):
            pno, r = s["_link"]
            doc[pno].insert_link({"kind": pymupdf.LINK_GOTO, "from": r, "page": pages[s["link"]],
                                  "to": pymupdf.Point(72, 70)})
    doc.set_toc(toc)
    doc.save(path)
    return str(path)


def base(**over):
    secs = [
        {"title": "Overview", "paras": [P1, MOVED], "link": "Setup"},
        {"title": "Installation", "paras": [P2], "image": _picture(1)},
        {"title": "Setup", "paras": [P1, P2], "table": ROWS},
        {"title": "Maintenance", "paras": [P2, P1]},
    ]
    for k, v in over.items():
        secs = v(secs)
    return secs


@pytest.fixture
def cfg():
    c = load_config()
    c["sections"]["front_matter"] = False
    return c


def genuine(result):
    return [(s["title"], f) for s in result["sections"] for f in s["findings"] if f["genuine"]]


def issues(result):
    return {f["issue"] for _, f in genuine(result)}


def test_identical_has_no_genuine_issue(tmp_path, cfg):
    a = make(tmp_path / "a.pdf", base())
    assert genuine(compare(a, a, cfg)) == []


def test_style_change_is_not_genuine(tmp_path, cfg):
    a = make(tmp_path / "a.pdf", base())
    secs = base()
    secs[3]["font"] = "tiro"  # same text in another font: CSS, not a genuine issue
    b = make(tmp_path / "b.pdf", secs)
    r = compare(a, b, cfg)
    assert any(f["check"] == "style" for s in r["sections"] for f in s["findings"])
    assert genuine(r) == []


def test_every_text_difference_is_genuine(tmp_path, cfg):
    """Content: extra text, each punctuation mark, the space after a word and case all count."""
    a = make(tmp_path / "a.pdf", base())
    secs = base()
    secs[3]["paras"] = [P2.replace("outlet before", "outlet  now before").replace("panel.", "panel;"),
                        P1.replace("remote control", "REMOTE control").replace("power button", "powerbutton")]
    b = make(tmp_path / "b.pdf", secs)
    types = {t for _, f in genuine(compare(a, b, cfg)) for t in f["types"]}
    assert {"extra text", "punctuation", "case", "spacing"} <= types


def test_duplicate_section(tmp_path, cfg):
    a = make(tmp_path / "a.pdf", base())
    secs = base()
    secs.insert(3, {"title": "Setup", "paras": [P1, P2]})
    r = compare(a, make(tmp_path / "b.pdf", secs), cfg)
    assert "Section duplicated" in issues(r)


def test_image_in_wrong_section(tmp_path, cfg):
    a = make(tmp_path / "a.pdf", base())
    secs = base()
    secs[3]["image"], secs[1]["image"] = secs[1]["image"], None
    r = compare(a, make(tmp_path / "b.pdf", secs), cfg)
    hits = [(t, f) for t, f in genuine(r) if f["issue"] == "Image in wrong section"]
    assert hits and hits[0][0] == "Installation" and "Maintenance" in hits[0][1]["message"]
    assert "Image missing" not in issues(r)


def test_content_in_wrong_section(tmp_path, cfg):
    a = make(tmp_path / "a.pdf", base())
    secs = base()
    secs[0]["paras"] = [P1]
    secs[3]["paras"] = [P2, P1, MOVED]
    r = compare(a, make(tmp_path / "b.pdf", secs), cfg)
    hits = [(t, f) for t, f in genuine(r) if f["issue"] == "Content in wrong section"]
    assert hits and hits[0][0] == "Overview" and "Maintenance" in hits[0][1]["message"]


def test_link_to_wrong_section(tmp_path, cfg):
    a = make(tmp_path / "a.pdf", base())
    secs = base()
    secs[0]["link"] = "Maintenance"
    r = compare(a, make(tmp_path / "b.pdf", secs), cfg)
    hits = [f for _, f in genuine(r) if f["issue"] == "Link to wrong section"]
    assert hits and "Setup" in hits[0]["message"] and "Maintenance" in hits[0]["message"]


def test_broken_image(tmp_path, cfg):
    a = make(tmp_path / "a.pdf", base())
    secs = base()
    secs[1]["image"] = _blank()
    r = compare(a, make(tmp_path / "b.pdf", secs), cfg)
    assert "Image / icon broken" in issues(r)
    assert "Image missing" not in issues(r)


def test_missing_image(tmp_path, cfg):
    a = make(tmp_path / "a.pdf", base())
    secs = base()
    secs[1]["image"] = None
    assert "Image missing" in issues(compare(a, make(tmp_path / "b.pdf", secs), cfg))


def test_table_header_missing(tmp_path, cfg):
    a = make(tmp_path / "a.pdf", base())
    secs = base()
    secs[2]["table"] = ROWS[1:]
    r = compare(a, make(tmp_path / "b.pdf", secs), cfg)
    assert "Table header missing" in issues(r)
    assert "Table row missing" not in issues(r)


def test_section_missing(tmp_path, cfg):
    a = make(tmp_path / "a.pdf", base())
    secs = [s for s in base() if s["title"] != "Maintenance"]
    assert "Section missing" in issues(compare(a, make(tmp_path / "b.pdf", secs), cfg))


def _edit(png: bytes, fn) -> bytes:
    pix = pymupdf.Pixmap(png)
    for x in range(pix.width):
        for y in range(pix.height):
            c = fn(pix, x, y)
            if c is not None:
                pix.set_pixel(x, y, c)
    return pix.tobytes("png")


def test_image_partly_blacked_out(tmp_path, cfg):
    a = make(tmp_path / "a.pdf", base())
    secs = base()
    secs[1]["image"] = _edit(_picture(1), lambda p, x, y: (0, 0, 0) if x < p.width // 2 else None)
    r = compare(a, make(tmp_path / "b.pdf", secs), cfg)
    assert "Image blacked out" in issues(r)


def test_different_image_at_same_spot(tmp_path, cfg):
    """e.g. the rear view where prod shows the front view: here the prod picture mirrored."""
    a = make(tmp_path / "a.pdf", base())
    src = pymupdf.Pixmap(_picture(1))
    secs = base()
    secs[1]["image"] = _edit(_picture(1), lambda p, x, y: src.pixel(p.width - 1 - x, p.height - 1 - y))
    r = compare(a, make(tmp_path / "b.pdf", secs), cfg)
    assert "Different image" in issues(r)
    assert "Image missing" not in issues(r)


def test_image_duplicated_into_other_section(tmp_path, cfg):
    a = make(tmp_path / "a.pdf", base())
    secs = base()
    secs[3]["image"] = _picture(1)
    r = compare(a, make(tmp_path / "b.pdf", secs), cfg)
    hits = [(t, f) for t, f in genuine(r) if f["issue"] == "Image duplicated"]
    assert hits and hits[0][0] == "Maintenance" and "Installation" in hits[0][1]["message"]


def test_content_duplicated_into_other_section(tmp_path, cfg):
    a = make(tmp_path / "a.pdf", base())
    secs = base()
    secs[3]["paras"] = [P2, P1, MOVED]  # MOVED stays in Overview as well
    r = compare(a, make(tmp_path / "b.pdf", secs), cfg)
    hits = [(t, f) for t, f in genuine(r) if f["issue"] == "Content duplicated"]
    assert hits and hits[0][0] == "Maintenance" and "Overview" in hits[0][1]["message"]
    assert "Content in wrong section" not in issues(r)


def test_image_caption_missing(tmp_path, cfg):
    cap = "Figure 1: Rear panel connectors and power switch location."
    with_cap = lambda s: [{**x, "paras": x["paras"] + [cap]} if x["title"] == "Installation" else x for x in s]
    a = make(tmp_path / "a.pdf", base(cap=with_cap))
    r = compare(a, make(tmp_path / "b.pdf", base()), cfg)
    assert "Image label / caption missing" in issues(r)


def test_stretched_image_is_distorted(tmp_path, cfg):
    a = make(tmp_path / "a.pdf", base())
    b_path = tmp_path / "b.pdf"
    make(b_path, base())
    doc = pymupdf.open(b_path)  # redraw the Installation picture 60 % wider than its pixels
    page = doc[1]
    info = page.get_image_info(xrefs=True)[0]
    r = pymupdf.Rect(info["bbox"])
    page.add_redact_annot(r)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_REMOVE)
    page.insert_image(pymupdf.Rect(r.x0, r.y0, r.x0 + r.width * 1.6, r.y1), stream=_picture(1), keep_proportion=False)
    doc.save(tmp_path / "b2.pdf")
    r2 = compare(a, str(tmp_path / "b2.pdf"), cfg)
    assert "Image distorted" in issues(r2)
    assert "Image distorted" not in issues(compare(a, str(b_path), cfg))


def test_missing_section_marker_is_where_the_section_belongs(tmp_path, cfg):
    """The missing section repeats the previous section's notes word for word (like the RoHS
    '備考' lines): its stage marker and the markers of everything in it must sit where the section
    belongs (before the next section's heading), not at the look-alike text."""
    notes = ["Note 1: a circle means the substance is within the limit.", "Note 2: a dash means the substance is exempt."]
    secs = base()
    secs.insert(3, {"title": "Speaker models", "paras": notes + ["Speaker cabinet uses a separate substance table."]})
    secs[2]["paras"] = secs[2]["paras"] + notes
    a = make(tmp_path / "a.pdf", secs)
    b = make(tmp_path / "b.pdf", [s for s in secs if s["title"] != "Speaker models"])
    r = compare(a, b, cfg)
    doc = pymupdf.open(tmp_path / "b.pdf")
    maint = next(p for p in range(doc.page_count) if "Maintenance" in doc[p].get_text())
    hits = [f for _, f in genuine(r) if f["issue"] in ("Section missing", "Data missing") and f["candidate_at"]]
    assert any(f["issue"] == "Section missing" for f in hits)
    for f in hits:
        assert f["candidate_at"]["page"] == maint and f["candidate_at"]["bbox"][1] < 120, f["message"]
