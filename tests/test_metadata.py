from pdfval import metadata as m


def _row(n, model, models, file=""):
    return {"row": n, "model": model, "models": models, "file": file}


def test_models_involved_split_into_models():
    assert m.models_of("PD2706U, PD2706UA") == ["PD2706U", "PD2706UA"]
    assert m.models_of("GW90C/GW90TC") == ["GW90C", "GW90TC"]
    assert m.models_of("EW2790Q/EW2790Q/EW3290U") == ["EW2790Q", "EW3290U"]  # listed twice: once


def test_map_named_after_one_model_of_the_row():
    rows = [_row(13, "PD06U Series", "PD2706U, PD2706UA", "PD06U-EM-V2")]
    assert m.match("PD2706UA", "PD2706UA", rows)["row"] == 13


def test_document_title_picks_the_row_when_a_model_is_in_two_rows():
    rows = [_row(39, "MA Series", "MA270U/MA320U/MA270S"), _row(40, "MA270S safety", "MA270S safety UM")]
    assert m.match("ma270s", "ma270s", rows, "Monitor MA270S safety user manual")["row"] == 40
    assert m.match("ma270s", "ma270s", rows, "Monitor MA Series user manual")["row"] == 39


def test_row_with_a_mismatched_copy_is_not_a_pass():
    """Two maps for one sheet row (TEY41 in TWY31/ and tey41/): one right, one wrong - the row is a
    mismatch, not listed under “Rows that pass”."""
    from pdfval import metadata as M
    base = {"row": 64, "brand": "BenQ", "category": "IFP-accessory", "model": "TEY41", "file": "",
            "exp_doc": "OPS Module", "exp_page": "OPS Module", "product": "tey41", "url": ""}
    res = {"maps": [{**base, "map": "a/TWY31/Maps/TEY41.ditamap", "path": "a", "doc": "Wrong", "page": "OPS Module"},
                    {**base, "map": "b/tey41/Maps/TEY41.ditamap", "path": "b", "doc": "OPS Module", "page": "OPS Module"}],
           "not_in_aem": [], "summary": {}}
    (row,) = M.sheet_rows(res)
    assert row["status"] == "fail" and sorted(m["status"] for m in row["maps"]) == ["fail", "pass"]
