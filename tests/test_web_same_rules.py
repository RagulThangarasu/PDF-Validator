"""Prod PDF vs a stage web page runs the same validation as PDF vs PDF: dash style, a table's numbering
column, prod's back cover, repeated points and missing text are judged the same way; borders are not
compared (a web page's PDF is a screenshot)."""
import pymupdf
import pytest

from pdfval import engine

STEPS = ["Press the menu key to open the settings menu on the screen.",
         "Use the arrow keys to select the item you want to change.",
         "Press the OK key to confirm the new value of the item.",
         "Press the back key to close the menu and save the settings."]
ROWS = [("No.", "Item"), ("A.", "InstaShow Receiver unit"), ("B.", "Lid for the receiver"), ("C.", "InstaShow Button unit")]
P1 = "Enter the user name and password as shown in steps 2 - 4 of the LAN section."
P2 = "Keep the ventilation openings free so that warm air can leave the housing easily."


def _prod(path) -> str:
    doc = pymupdf.open()
    for _ in range(5):  # cover, content, content, blank, back cover
        doc.new_page(width=595, height=842)
    doc[0].insert_text((72, 300), "User Manual", fontsize=30, fontname="hebo")
    pg, y = doc[1], 115
    pg.insert_text((72, 80), "Setup", fontsize=18, fontname="hebo")
    for t in (P1, P2):
        pg.insert_text((72, y), t, fontsize=10, fontname="helv")
        y += 16
    for t in STEPS:
        pg.insert_text((72, y), "• " + t, fontsize=10, fontname="helv")
        y += 16
    pg.insert_text((72, y + 20), "Package contents", fontsize=18, fontname="hebo")
    xs = [72, 110, 400]
    for r, cells in enumerate(ROWS):
        for c, text in enumerate(cells):
            box = pymupdf.Rect(xs[c], y + 40 + r * 22, xs[c + 1], y + 40 + (r + 1) * 22)
            pg.draw_rect(box, color=(0, 0, 0), width=0.6)
            pg.insert_text((box.x0 + 4, box.y0 + 15), text, fontsize=10, fontname="helv")
    doc[2].insert_text((72, 80), "Maintenance", fontsize=18, fontname="hebo")
    doc[2].insert_text((72, 115), "Clean the housing with a soft dry cloth once a month.", fontsize=10, fontname="helv")
    doc[4].insert_text((72, 700), "BenQ.com (c) 2025 BenQ Corporation. All rights reserved.", fontsize=10, fontname="helv")
    doc.set_toc([[1, "Setup", 2], [1, "Package contents", 2], [1, "Maintenance", 3]])
    doc.save(path)
    return str(path)


def _page(path, steps, with_p2=True) -> str:
    cell = lambda r, c: f"<{'th' if r == 0 else 'td'}>{c}</{'th' if r == 0 else 'td'}>"
    rows = "".join("<tr>" + "".join(cell(r, c) for c in cells) + "</tr>" for r, cells in enumerate(ROWS))
    path.write_text(f"""<!doctype html><html><head><meta charset='utf-8'><title>Guide</title>
<style>body{{font-family:Arial;margin:40px;width:700px}} td,th{{border:1px solid #000;padding:6px 14px}}
table{{border-collapse:collapse}}</style></head><body><h1>Setup</h1><p>{P1.replace(' - ', ' – ')}</p>
{f'<p>{P2}</p>' if with_p2 else ''}<ul>{''.join(f'<li>{t}</li>' for t in steps)}</ul>
<h1>Package contents</h1><table>{rows}</table>
<h1>Maintenance</h1><p>Clean the housing with a soft dry cloth once a month.</p></body></html>""", encoding="utf-8")
    return "file://" + str(path)


def _run(tmp_path, steps, with_p2=True):
    cfg = engine.load_config()
    cfg["typography"]["enabled"] = False
    cfg.setdefault("site", {})["enabled"] = False
    try:
        r = engine.compare_url(_prod(tmp_path / "prod.pdf"), _page(tmp_path / "page.html", steps, with_p2),
                               str(tmp_path / "run"), cfg, html={"crawl": False})
    except Exception as e:  # no browser installed for the capture
        if "playwright" in f"{type(e).__module__} {e}".lower() or "executable" in str(e).lower():
            pytest.skip(f"web capture not available: {e}")
        raise
    return [(f["types"], f["message"]) for s in r["sections"] for f in s["findings"]]


def test_the_same_page_has_no_issue(tmp_path):
    # an en dash for prod's hyphen, a real table for prod's drawn one, no cover / back cover: nothing to report
    assert _run(tmp_path, STEPS) == []


def test_repeated_points_and_missing_text_are_reported_as_in_pdf(tmp_path):
    fs = _run(tmp_path, STEPS + [STEPS[1], STEPS[2]], with_p2=False)
    assert sorted(t[0] for t, _ in fs) == ["duplicate content", "missing text"]
