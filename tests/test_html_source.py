"""html_source.py: building the web capture's synthetic Doc (_to_doc) and measuring it (_measure,
reused from extract.py). Without a measured content-box left/right, every page defaults to (0, 0) -
a report screenshot's marker line then spans the whole browser-viewport width (left nav, content,
"on this page" panel all sharing the same y-coordinates), instead of just the content column."""
from pdfval import html_source


def _word(text, x0, y0, x1, y1, px=16):
    # (t, x0, y0, x1, y1, fam, wt, it, px, col, block, hlevel, href, after) - html_source._to_doc's shape
    return (text, x0, y0, x1, y1, "Roboto", 400, False, px, "#000000", 0, None, None, 1)


def test_to_doc_plus_measure_finds_the_content_columns_left_right():
    rows = [[_word(f"word{i}", 430 + i * 90, y, 430 + i * 90 + 80, y + 16) for i in range(6)]
            for y in (100, 120, 140, 160, 180)]  # 5 lines, each word0..word5 spanning x 430 to 960
    data = {"width": 1440, "words": [w for row in rows for w in row], "images": [], "tables": [],
           "headings": [], "links": []}
    doc = html_source._to_doc(data, cuts=[0, 900], path="candidate_source.pdf")
    html_source._measure(doc)
    assert doc.pages[0].left == 430
    assert doc.pages[0].right == 960
    assert doc.body_size == 12.0  # 16 CSS px -> pt (x 0.75)
