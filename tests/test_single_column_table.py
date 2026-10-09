"""A table with only one column (a key / single-field layout) is a different design-spec rule than a
normal multi-column table: its cell text (header and data) must be centred, not left-aligned - the
opposite of a normal table header. See pdfval/checks/tables.py::_single_column_center."""
import pymupdf
import pytest

from pdfval import engine


def _prod(path) -> str:
    doc = pymupdf.open()
    doc.new_page(width=595, height=842)
    pg = doc.new_page(width=595, height=842)
    pg.insert_text((72, 80), "Specifications", fontsize=18, fontname="hebo")
    doc.set_toc([[1, "Specifications", 2]])
    doc.save(path)
    return str(path)


def _page(path, align: str) -> str:
    path.write_text(f"""<!doctype html><html><head><meta charset='utf-8'><title>Guide</title>
<style>body{{font-family:Arial;margin:40px;width:700px}} td,th{{border:1px solid #000;padding:6px 14px;
text-align:{align}}} table{{border-collapse:collapse;width:300px}}</style></head><body>
<h1>Specifications</h1>
<table><tr><td>Model: XYZ-123</td></tr><tr><td>Weight: 5 kg</td></tr><tr><td>Colour: Black</td></tr></table>
</body></html>""", encoding="utf-8")
    return "file://" + str(path)


def _run(tmp_path, align: str):
    cfg = engine.load_config()
    cfg["typography"]["enabled"] = False
    cfg.setdefault("site", {})["enabled"] = False
    try:
        r = engine.compare_url(_prod(tmp_path / "prod.pdf"), _page(tmp_path / "page.html", align),
                               str(tmp_path / "run"), cfg, html={"crawl": False})
    except Exception as e:  # no browser installed for the capture
        if "playwright" in f"{type(e).__module__} {e}".lower() or "executable" in str(e).lower():
            pytest.skip(f"web capture not available: {e}")
        raise
    return [(f["types"], f["message"]) for s in r["sections"] for f in s["findings"]]


def test_centred_single_column_table_is_not_reported(tmp_path):
    fs = _run(tmp_path, "center")
    assert not [t for t, _ in fs if "single column alignment" in t]


def test_left_aligned_single_column_table_is_reported(tmp_path):
    fs = _run(tmp_path, "left")
    bad = [m for t, m in fs if "single column alignment" in t]
    assert bad and "centred" in bad[0] and "Model: XYZ-123" in bad[0]
