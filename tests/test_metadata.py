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


def test_map_matched_by_its_document_title_when_the_folder_has_another_name():
    """Folder "stylus" holds the map of the row "BenQ Board Pens": its Document Title is the row's title."""
    rows = [{**_row(78, "BenQ Board Pens", "PT03,PT06", "Stylus_UM_EN_V1.02"), "exp_doc": "IFP accessory BenQ Board Pens user manual"},
            {**_row(64, "TEY41", "TEY41"), "exp_doc": "IFP accessory TEY41 user manual"}]
    assert m.match("pens", "pens", rows, "IFP accessory BenQ Board Pens user manual")["row"] == 78
    assert m.match("stylus", "stylus", rows)["row"] == 78  # and by the file name: Stylus_UM_EN_V1.02 -> stylus


def test_meta_description_counts_and_models_do_not():
    base = {"row": 5, "brand": "BenQ", "category": "Monitor", "model": "GW2291", "file": "", "product": "gw2291", "url": "", "map": "m", "path": "p",
            "exp_doc": "Monitor GW2291 user manual", "exp_page": "Monitor GW2291", "doc": "Monitor GW2291 user manual", "page": "Monitor GW2291",
            "exp_desc": "Learn how to set up Monitor GW2291 and its settings, and optimize performance."}
    ok = {**base, "desc": base["exp_desc"], "models": "other", "models_status": "fail"}
    bad = {**base, "desc": "Learn how to set up [Product] [model] and its settings, and optimize performance."}
    assert m.sheet_rows({"maps": [ok], "not_in_aem": []})[0]["status"] == "pass"
    assert m.sheet_rows({"maps": [bad], "not_in_aem": []})[0]["status"] == "fail"
