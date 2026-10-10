"""A phrase published with an editing highlight still on it (<mark>, or a span with a background
colour) is a genuine issue on a crawled web guide. See pdfval/checks/highlights.py.

A background on a *block* - a NOTE / WARNING callout's panel, a table header bar, a card - is part
of the design and must stay unreported, so each case is run through a real browser end to end.
"""
import pymupdf
import pytest

from pdfval import engine

BODY = {
    "clean": "<h1>Using a screen cleaner</h1><p>Use screen cleaning wipes which are alcohol-free.</p>",
    # the seeded defect: the first word of the heading left highlighted
    "mark": "<h1><mark>Using</mark> a screen cleaner</h1><p>Use screen cleaning wipes which are alcohol-free.</p>",
    "span": "<h1>Using a screen cleaner</h1><p>Use <span style='background:#cce0ff'>screen cleaning wipes</span> "
            "which are alcohol-free.</p>",
    # design backgrounds - a callout panel and a table header bar, both on block elements
    "callout": "<h1>Using a screen cleaner</h1><div style='background:#dae8f2;padding:10px'><b>NOTE</b> "
               "Never spray anything directly onto the screen.</div>",
    "table": "<h1>Using a screen cleaner</h1><table><tr style='background:#005a5a;color:#fff'><th>Item</th>"
             "<th>Note</th></tr><tr><td>Wipes</td><td>Alcohol-free</td></tr></table>",
}


def _prod(path) -> str:
    doc = pymupdf.open()
    doc.new_page(width=595, height=842)
    pg = doc.new_page(width=595, height=842)
    pg.insert_text((72, 80), "Using a screen cleaner", fontsize=18, fontname="hebo")
    pg.insert_text((72, 110), "Use screen cleaning wipes which are alcohol-free.", fontsize=11)
    doc.set_toc([[1, "Using a screen cleaner", 2]])
    doc.save(path)
    return str(path)


def _page(path, body: str) -> str:
    path.write_text(f"""<!doctype html><html><head><meta charset='utf-8'><title>Guide</title>
<style>body{{font-family:Arial;margin:40px;width:700px}} table{{border-collapse:collapse}}
td,th{{border:1px solid #000;padding:6px 14px}}</style></head><body>{body}</body></html>""", encoding="utf-8")
    return "file://" + str(path)


def _run(tmp_path, key: str):
    cfg = engine.load_config()
    cfg["typography"]["enabled"] = False
    cfg.setdefault("site", {})["enabled"] = False
    try:
        r = engine.compare_url(_prod(tmp_path / "prod.pdf"), _page(tmp_path / "page.html", BODY[key]),
                               str(tmp_path / "run"), cfg, html={"crawl": False})
    except Exception as e:  # no browser installed for the capture
        if "playwright" in f"{type(e).__module__} {e}".lower() or "executable" in str(e).lower():
            pytest.skip(f"web capture not available: {e}")
        raise
    return [f for s in r["sections"] for f in s["findings"] if "text highlight" in f["types"]]


def test_a_highlighted_heading_word_is_reported(tmp_path):
    fs = _run(tmp_path, "mark")
    assert len(fs) == 1, fs
    assert "Using" in fs[0]["message"]
    assert fs[0]["detail"]["kind"] == "text-highlight"
    assert fs[0]["candidate"], "the highlighted word must be located for the screenshot"


def test_a_highlighted_phrase_in_a_paragraph_is_reported(tmp_path):
    fs = _run(tmp_path, "span")
    assert len(fs) == 1, fs
    assert fs[0]["detail"]["text"] == "screen cleaning wipes"
    assert fs[0]["detail"]["color"] == "#cce0ff"


def test_a_clean_page_reports_nothing(tmp_path):
    assert _run(tmp_path, "clean") == []


@pytest.mark.parametrize("key", ["callout", "table"])
def test_a_design_background_on_a_block_is_not_a_highlight(tmp_path, key):
    assert _run(tmp_path, key) == []
