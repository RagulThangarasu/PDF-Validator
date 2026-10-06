"""Red highlight marks drawn over a screenshot in prod only: the pictures look alike, but stage is a different image."""
import io

import pymupdf
import pytest
from conftest import every_picture_issue
from PIL import Image, ImageDraw

from pdfval import engine


@pytest.fixture
def cfg():
    c = engine.load_config()
    c["typography"]["enabled"] = False  # prod vs stage only
    return every_picture_issue(c)


def _screenshot() -> bytes:
    """A dark UI screenshot with a few fields."""
    im = Image.new("RGB", (800, 500), (60, 64, 110))
    d = ImageDraw.Draw(im)
    for k, y in enumerate((260, 330, 400)):
        d.rectangle((150, y, 650, y + 50), outline=(220, 220, 230), width=3, fill=(70, 74, 120) if k < 2 else (120, 120, 130))
    d.rectangle((200, 60, 600, 200), outline=(240, 240, 240), width=4)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _doc(path, red: bool) -> str:
    doc = pymupdf.open()
    pg = doc.new_page(width=595.28, height=841.89)
    pg.insert_text((40, 60), "Logging into the web management interface", fontsize=16, fontname="hebo")
    pg.insert_text((40, 90), "Enter the account and password, then click Login.", fontsize=11, fontname="helv")
    box = pymupdf.Rect(100, 120, 500, 370)
    pg.insert_image(box, stream=_screenshot())
    if red:  # the red outline prod draws around the login fields
        pg.draw_rect(pymupdf.Rect(185, 245, 415, 355), color=(0.85, 0.05, 0.1), width=1.5)
    pg.insert_text((40, 400), "You will successfully log into the web management interface.", fontsize=11, fontname="helv")
    doc.save(path)
    return str(path)


def _marks(r):
    # highlight marks on one side only are an image overlay: reported in the image report (image_findings)
    return [f for s in r["sections"] for f in s["findings"] + (s.get("image_findings") or [])
            if f["detail"].get("kind") == "marks"]


def test_red_marks_in_prod_only_is_a_different_image(tmp_path, cfg):
    r = engine.compare(_doc(tmp_path / "prod.pdf", True), _doc(tmp_path / "stage.pdf", False), cfg)
    got = _marks(r)
    assert len(got) == 1
    f = got[0]
    assert f["message"].startswith("Image different in stage: red highlight marks missing")
    assert "\nProd: red marks drawn on the picture\nStage: the same picture without the red marks" in f["message"]
    # an image overlay: in the image report only (never the PDF report / verdict)
    from pdfval.report import image_report
    assert f["detail"].get("image_report_only") and any(name == "Red overlay missing" for _, _, name in image_report.issues(r))
    x0, y0, x1, y1 = f["baseline"][0]["bbox"]  # the highlight surrounds the red outline
    assert x0 <= 185 <= x1 and y0 <= 245 <= y1 and x0 <= 415 <= x1 and y0 <= 355 <= y1
    assert f["color"] != "#dc2626"  # not a red highlight over red marks


def test_red_marks_in_stage_only_are_reported_as_added(tmp_path, cfg):
    got = _marks(engine.compare(_doc(tmp_path / "prod.pdf", False), _doc(tmp_path / "stage.pdf", True), cfg))
    assert len(got) == 1 and "red highlight marks added" in got[0]["message"]


def test_same_red_marks_on_both_sides_is_no_finding(tmp_path, cfg):
    assert not _marks(engine.compare(_doc(tmp_path / "prod.pdf", True), _doc(tmp_path / "stage.pdf", True), cfg))


def test_artwork_search_stays_in_its_section(tmp_path, cfg):
    """The same product drawing in "Package contents" and again in "Product overview": the search for the
    Product overview picture leaves out the part of the page above its heading (another section)."""
    from pdfval import extract
    from pdfval.checks.assets import _outside
    doc = pymupdf.open()
    pg = doc.new_page(width=420, height=595)
    pg.insert_text((30, 40), "Package contents", fontsize=16, fontname="hebo")
    pg.insert_text((30, 250), "Product overview", fontsize=16, fontname="hebo")
    pg.insert_text((30, 480), "1. HDMI Connector", fontsize=10, fontname="helv")
    p2 = doc.new_page(width=420, height=595)
    p2.insert_text((30, 40), "Specifications", fontsize=16, fontname="hebo")
    path = str(tmp_path / "doc.pdf")
    doc.save(path)
    d = extract.load(path, "prod", cfg)
    start = next(i for i, w in enumerate(d.words) if w.text == "Product")
    end = next(i for i, w in enumerate(d.words) if w.text == "Specifications")
    out = _outside(d, (start, end), 0)
    assert len(out) == 1 and out[0][0] == 0
    x0, y0, x1, y1 = out[0][1]
    assert (x0, y0) == (0, 0) and 200 < y1 < 240  # everything above the "Product overview" heading
    nxt = _outside(d, (start, end), 1)[0][1]
    assert nxt[1] < 40 and nxt[3] == 595  # on the next page: from the next heading down
