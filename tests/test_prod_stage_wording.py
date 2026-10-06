"""The report states an issue as what prod has and what stage has - nothing else (no explanation, no pages)."""
import pytest

from pdfval.genuine import prod_stage

CASES = [
    ("Not aligned in stage (2 places): “1. Front Table”, “2. Rear Table” are side by side on one row in prod (p.16); "
     "in stage (p.15) “1. Front Table” 16 pt higher than “2. Rear Table”; “A”, “B” are side by side on one row in prod (p.16)",
     "“1. Front Table”, “2. Rear Table” side by side on one row", "“1. Front Table” 16 pt higher than “2. Rear Table”"),
    ("Tables merged in stage: 2 prod tables are one table in stage (stage p.53, “Cause Remedy”)", "2 separate tables", "One table"),
    ("Link points to the wrong section: “Fast Mode on page 37.” goes to “Basic menu” in prod but to “Advanced menu” in stage (stage p.38)",
     "“Fast Mode on page 37.” goes to “Basic menu”", "“Fast Mode on page 37.” goes to “Advanced menu”"),
    ("Table header not centred in stage (1 cell): “H (mm)” left-aligned — header text must be centred in its column",
     "Header text centred in its column", "“H (mm)” left-aligned"),
    ("Image bigger in stage by 74%: the same picture (prod p.23 ↔ stage p.23): 173×97 pt → 234×132 pt, width 18% → 31% of content box",
     "The picture at 173×97 pt", "The same picture at 234×132 pt (74% bigger)"),
    ("Callout type differs: stage labels the note “IMPORTANT”, prod's note has the exclamation icon (a WARNING)",
     "A WARNING note (exclamation icon)", "Labelled “IMPORTANT”"),
]


@pytest.mark.parametrize("message, prod, stage", CASES)
def test_message_as_prod_and_stage(message, prod, stage):
    assert prod_stage({"message": message, "detail": {}, "check": "layout", "types": []}) == (prod, stage)


def test_text_change_has_no_page_numbers():
    f = {"check": "content", "types": ["changed text"], "message": "Changed text: “1-2” → “1–3”",
         "detail": {"op": "replace", "baseline_text": "1-2", "candidate_text": "1–3",
                    "baseline_sentence": "Repeat steps 1-2 to search.", "candidate_sentence": "Repeat steps 1–3 to search."},
         "baseline": [{"page": 27, "bbox": [0, 0, 1, 1]}], "candidate": [{"page": 27, "bbox": [0, 0, 1, 1]}]}
    ps = prod_stage(f)
    assert ps and "prod p." not in ps[0] and "stage p." not in ps[1] and "1-2" in ps[0] and "1–3" in ps[1]
