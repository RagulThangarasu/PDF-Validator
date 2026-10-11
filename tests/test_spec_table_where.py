"""A value printed in red in the report's "Design spec (Figma)" table is a defect, so the table says
where it is: the sections (AEM topics) and pages that use it, biggest first - for the most used value
and for every other value stage uses for that style (what the "55% of 22 words" is made of)."""
from collections import Counter

from pdfval.checks import typography
from pdfval.report import pdf_report

SPEC = {"style": "Caption", "font": "Roboto", "weight": "Regular", "size": 12, "line_height": 16,
        "color": "#757575"}


def _actual(**kw):
    a = {"font": "Roboto", "weight": "Regular", "size": 12, "line_height": None, "color": "#757575",
         "share": 1.0, "words": 10, "where": [], "others": []}
    return {**a, **kw}


def test_off_spec_names_every_property_that_differs():
    a = _actual(weight="Bold", size=13.5, color="#000000")
    assert pdf_report._off_spec(SPEC, a) == ["weight Bold", "size 13.5 pt", "colour #000000"]
    assert pdf_report._off_spec(SPEC, _actual()) == []


def test_where_text_reads_as_pages_and_word_counts():
    assert pdf_report._where_text([{"section": "Video input", "words": 5, "pages": [14, 15]}]) \
        == "Video input (p. 14–15, 5 words)"
    assert pdf_report._where_text([{"section": "Preset display mode", "words": 1, "pages": [5]}]) \
        == "Preset display mode (p. 5, 1 word)"
    assert "p. 3, 9" in pdf_report._where_text([{"section": "A", "words": 2, "pages": [3, 9]}])


def test_the_cell_names_the_sections_behind_a_red_value():
    cell = pdf_report._actual_cell({**SPEC, "actual": _actual(
        color="#000000", share=0.55, words=22,
        where=[{"section": "Adjusting the screen resolution", "words": 7, "pages": [3]}],
        others=[{"font": "Roboto", "weight": "Bold", "size": 12, "color": "#000000", "words": 8,
                 "where": [{"section": "Adjusting the screen refresh rate", "words": 8, "pages": [4]}]}])})
    assert "colour #000000</b> in: Adjusting the screen resolution (p. 3, 7 words)" in cell
    assert "weight Bold, colour #000000</b> (8 words) in: Adjusting the screen refresh rate (p. 4, 8 words)" in cell


def test_a_style_that_matches_the_spec_says_nothing_extra():
    cell = pdf_report._actual_cell({**SPEC, "actual": _actual()})
    assert "in:" not in cell and "#dc2626" not in cell


def test_reference_carries_where_each_value_is_used():
    cfg = {"_typo_actual": {"caption": Counter({("Roboto", 400, 12.0, "#000000"): 7,
                                                ("Roboto", 700, 12.0, "#000000"): 3})},
           "_typo_where": {("caption", ("Roboto", 400, 12.0, "#000000")):
                           {"Adjusting the screen resolution": {"words": 7, "pages": {3, 4}}},
                           ("caption", ("Roboto", 700, 12.0, "#000000")):
                           {"Video input": {"words": 3, "pages": {15}}}}}
    a = typography._stage_actual(cfg, "caption", {"Regular": 400, "Bold": 700})
    assert a["where"] == [{"section": "Adjusting the screen resolution", "words": 7, "pages": [3, 4]}]
    assert [o["weight"] for o in a["others"]] == ["Bold"]
    assert a["others"][0]["where"] == [{"section": "Video input", "words": 3, "pages": [15]}]
