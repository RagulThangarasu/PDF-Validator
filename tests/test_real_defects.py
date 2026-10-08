"""Every kind of content defect is caught on a real manual.

One known defect is planted in a copy of the real prod PDF (the SL04 manual, InDesign): a
missing word, extra text, a missing space or punctuation mark, a removed or re-targeted link,
a removed or replaced image, a removed table row or table, two tables merged, and a section
removed, moved or duplicated. Comparing the original with the copy must report that defect as
a genuine issue. Skipped when the sample PDF is not on disk (PDFs are never committed).
"""
from pathlib import Path

import pymupdf
import pytest

from pdfval import compare, load_config

PROD = str(Path(__file__).resolve().parents[2] / "prod" / "SL04&SH04_UM_V1.2_EN.pdf")


def _size_at(page, rect):
    for b in page.get_text("dict", clip=rect)["blocks"]:
        for l in b.get("lines", []):
            for s in l["spans"]:
                return s["size"]
    return 10


def replace_text(doc, pno, old, new, nth=0):
    page = doc[pno]
    r = page.search_for(old)[nth]
    size = _size_at(page, r)
    page.add_redact_annot(r)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
    if new:
        page.insert_text((r.x0, r.y1 - size * 0.25), new, fontsize=size, fontname="helv")


def redact(doc, pno, rect, graphics=False):
    page = doc[pno]
    page.add_redact_annot(rect)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                          graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED if graphics else pymupdf.PDF_REDACT_LINE_ART_NONE)


def toc_without(doc, title):
    doc.set_toc([e for e in doc.get_toc() if e[1] != title])


def d_missing_word(doc):
    replace_text(doc, 15, "environment ", "")


def d_extra_text(doc):
    doc[15].insert_text((83, 600), "Keep the original packaging to transport the display later.", fontsize=11, fontname="helv")


def d_missing_space(doc):
    replace_text(doc, 15, "meets the", "meetsthe")


def d_missing_punct(doc):
    replace_text(doc, 15, "requirements.", "requirements")


def d_link_removed(doc):
    page = doc[39]
    link = next(l for l in page.get_links() if "Video connection" in page.get_textbox(l["from"]))
    page.delete_link(link)


def d_link_wrong_target(doc):
    page = doc[62]
    link = next(l for l in page.get_links() if "Display settings" in page.get_textbox(l["from"]))
    page.delete_link(link)
    page.insert_link({"kind": pymupdf.LINK_GOTO, "from": link["from"], "page": 25, "to": pymupdf.Point(0, 60)})


def d_image_removed(doc):
    page = doc[7]
    page.delete_image(page.get_images()[0][0])


def d_image_replaced(doc):
    page = doc[7]
    other = pymupdf.Pixmap(doc, doc[12].get_images()[0][0])
    if other.alpha or other.colorspace.n > 3:
        other = pymupdf.Pixmap(pymupdf.csRGB, other)
    page.replace_image(page.get_images()[0][0], pixmap=other)


def _tables(doc, pno):
    return [t for t in doc[pno].find_tables().tables if t.col_count >= 2]


def d_row_removed(doc):
    t = _tables(doc, 5)[1]
    r = t.rows[3].bbox  # the whole row across the table (the detector's row box may be the label cell only)
    redact(doc, 5, pymupdf.Rect(t.bbox[0], r[1], t.bbox[2], r[3]) + (1, 1, -1, -1))


def d_table_removed(doc):
    t = _tables(doc, 6)[1]  # a data table ("Environment", 5 rows) - the page 14 box is a note, not a table
    redact(doc, 6, pymupdf.Rect(t.bbox) + (-2, -2, 2, 2), graphics=True)


def d_tables_merged(doc):
    """Stage drops the “Power” heading between two spec tables and joins them into one table."""
    page = doc[5]
    t1, t2 = _tables(doc, 5)[2], _tables(doc, 5)[3]
    gap = pymupdf.Rect(t1.bbox[0], t1.bbox[3], t1.bbox[2], t2.bbox[1])
    redact(doc, 5, gap + (0, 1, 0, -1))
    xs = sorted({round(c[0], 1) for row in t1.rows for c in row.cells if c} | {round(t1.bbox[2], 1)})
    for x in xs:  # column rules through the former gap: one continuous table
        page.draw_line((x, t1.bbox[3]), (x, t2.bbox[1]), color=(0.6, 0.6, 0.6), width=0.5)
    page.draw_rect(pymupdf.Rect(t1.bbox[0], t1.bbox[1], t1.bbox[2], t2.bbox[3]), color=(0.6, 0.6, 0.6), width=0.5)


def d_section_missing(doc):
    doc.delete_page(8)  # "SL5504/SH5504" is page 9 on its own (the outline shifts along by itself)
    doc.set_toc([e for e in doc.get_toc() if e[1] != "SL5504/SH5504"])


def d_section_order(doc):
    doc.move_page(9, 8)  # SL6504 page before SL5504 page
    toc = doc.get_toc()
    for e in toc:
        if e[1] == "SL5504/SH5504":
            e[2] = 10
        elif e[1] == "SL6504/SH6504":
            e[2] = 9
    toc.sort(key=lambda e: (e[2], 0))
    doc.set_toc(toc)


def d_section_duplicated(doc):
    doc.fullcopy_page(8, 12)  # SL5504 page again, after SL8604 (the outline shifts along by itself)
    toc = doc.get_toc()
    k = next(i for i, e in enumerate(toc) if e[1] == "SL8604")
    toc.insert(k + 1, [3, "SL5504/SH5504", 13])
    doc.set_toc(toc)


DEFECTS = {
    "missing word": (d_missing_word, {"Data missing"}),
    "extra text": (d_extra_text, {"Extra content"}),
    "missing space": (d_missing_space, {"Space after a word differs"}),
    "missing punctuation": (d_missing_punct, {"Punctuation differs"}),
    "link removed": (d_link_removed, {"Link not working"}),
    "link to wrong section": (d_link_wrong_target, {"Link to wrong section"}),
    "image removed": (d_image_removed, {"Image missing", "Image / icon broken"}),
    "image replaced": (d_image_replaced, {"Different image"}),
    "table row removed": (d_row_removed, {"Table row missing"}),
    "table removed": (d_table_removed, {"Table missing"}),
    "tables merged": (d_tables_merged, {"Tables merged"}),
    "section missing": (d_section_missing, {"Section missing"}),
    "section duplicated": (d_section_duplicated, {"Section duplicated"}),
}




@pytest.mark.skipif(not Path(PROD).exists(), reason="sample prod PDF not present")
@pytest.mark.parametrize("name", list(DEFECTS))
def test_planted_defect_is_a_genuine_issue(tmp_path, name):
    fn, expect = DEFECTS[name]
    doc = pymupdf.open(PROD)
    fn(doc)
    out = tmp_path / "stage.pdf"
    doc.save(out, garbage=3)
    cfg = load_config()
    cfg["ignore"]["types"] = [t for t in cfg["ignore"]["types"] if t not in ("case", "punctuation", "case + punctuation")]
    r = compare(PROD, str(out), cfg)
    got = {f["issue"] for s in r["sections"] for f in s["findings"] if f.get("genuine")}
    assert got & expect, f"{name}: expected one of {sorted(expect)}, got {sorted(got)}"


@pytest.mark.skipif(not Path(PROD).exists(), reason="sample prod PDF not present")
def test_section_order_is_not_reported(tmp_path):
    """A section moved to another position is not reported at all ([ignore] types has "order differs"):
    its content is still there, just elsewhere, so it is not treated as an issue."""
    doc = pymupdf.open(PROD)
    d_section_order(doc)
    out = tmp_path / "stage.pdf"
    doc.save(out, garbage=3)
    r = compare(PROD, str(out), load_config())
    order = [f for s in r["sections"] for f in s["findings"] if "order differs" in (f.get("types") or [])]
    assert not order
