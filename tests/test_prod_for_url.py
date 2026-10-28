"""PDF ⇄ Web page: the prod PDF is found from the stage guide's URL (product folder + language), in the prod
library - as a stage PDF is paired with its prod PDF."""
import pytest

from pdfval import metadata
from pdfval.app import server


@pytest.fixture
def lib(tmp_path, monkeypatch):
    for rel in ("FM/W2720i_EN_V1.03/W2720i_EN_V1.03/W2720i_V1.03_EN.pdf", "FM/W2720i_FR_V1.03/W2720i_FR_V1.03/W2720i_V1.03_FR.pdf",
                "INDD/GV32_EN_V1.00/GV32_EN_V1.00/GV32_UM_V1.00_EN.pdf"):
        f = tmp_path / "pdfs" / rel
        f.parent.mkdir(parents=True)
        f.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(server, "LIBRARY", server.Library(tmp_path / "src", tmp_path / "pdfs"))
    monkeypatch.setattr(metadata, "load_sheet", lambda *_: [])  # no Excel: by name
    return server.Pairs(tmp_path / "stage")


def test_product_and_language_from_the_url(lib):
    r = lib.prod_for_url("http://aem:4502/content/guide/consumer/projector/w2720i/en/positioning/mounting.html?wcmmode=disabled")
    assert r["name"] == "W2720i_V1.03_EN.pdf" and (r["product"], r["lang"], r["via"]) == ("w2720i", "en", "name")


def test_the_guide_language_picks_that_languages_prod_pdf(lib):
    assert lib.prod_for_url("http://aem:4502/content/guide/consumer/projector/w2720i/fr/x.html")["name"] == "W2720i_V1.03_FR.pdf"


def test_another_product(lib):
    assert lib.prod_for_url("http://aem:4502/content/guide/consumer/projector/gv32/en/overview.html")["name"] == "GV32_UM_V1.00_EN.pdf"


def test_unknown_product_finds_nothing(lib):
    r = lib.prod_for_url("https://docs.example.com/manual/unknown-thing/en/index.html")
    assert r["prod"] == "" and r["product"] == "unknown-thing"


def test_a_timing_guide_takes_its_own_prod_pdf_not_the_manual(tmp_path, monkeypatch):
    """…/pd2732u-timing/en/… is PD2732U_timing_V0, not the manual PD2732U_EN_V0 whose name it starts with."""
    for rel in ("FM/PD2732U_EN_V0/PD2732U_EN_V0/PD2732U-en.pdf", "FM/PD2732U_timing_V0/PD2732U_timing_V0/Vertical_screenresolution-PD2732U-en.pdf"):
        f = tmp_path / "pdfs" / rel
        f.parent.mkdir(parents=True)
        f.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(server, "LIBRARY", server.Library(tmp_path / "src", tmp_path / "pdfs"))
    monkeypatch.setattr(metadata, "load_sheet", lambda *_: [])
    pairs = server.Pairs(tmp_path / "stage")
    assert pairs.prod_for_url("http://aem:4502/content/guide/consumer/monitor/pd2732u-timing/en/preset-display-modes.html")["name"] == "Vertical_screenresolution-PD2732U-en.pdf"
    assert pairs.prod_for_url("http://aem:4502/content/guide/consumer/monitor/pd2732u/en/x.html")["name"] == "PD2732U-en.pdf"


def _lib(tmp_path, monkeypatch, rels):
    for rel in rels:
        f = tmp_path / "pdfs" / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(server, "LIBRARY", server.Library(tmp_path / "src", tmp_path / "pdfs"))
    monkeypatch.setattr(metadata, "load_sheet", lambda *_: [])
    return server.Pairs(tmp_path / "stage")


def test_a_manual_never_falls_back_to_the_timing_table(tmp_path, monkeypatch):
    """The library holds only PD30's timing table. "pd30" starts the same name, so the manual used to
    be validated against the timing table; now it reports no prod PDF instead."""
    pairs = _lib(tmp_path, monkeypatch, ["FM/PD30-timing/PD30-timing/PD30_timing_EN.pdf"])
    r = pairs.prod_for_url("http://aem:4502/content/guide/consumer/monitor/pd30/en/overview.html")
    assert r["prod"] == "" and r["product"] == "pd30"
    # ... while the timing guide still finds it
    assert pairs.prod_for_url("http://aem:4502/content/guide/consumer/monitor/pd30-timing/en/x.html")["name"] \
        == "PD30_timing_EN.pdf"


def test_the_language_token_cannot_pull_a_timing_file_over_the_exact_match(tmp_path, monkeypatch):
    """The manual's own file carries no language token and the timing file does: picking "the one in
    the guide's language" used to hand the manual its product's timing table."""
    pairs = _lib(tmp_path, monkeypatch, ["FM/RD270Q-EM-V0/RD270Q-EM-V0/RD270Q_UM.pdf",
                                         "FM/RD270Q_Timing_EN_V1/RD270Q_Timing_EN_V1/RD270Q_timing_en.pdf"])
    assert pairs.prod_for_url("http://aem:4502/content/guide/consumer/monitor/rd270q/en/x.html")["name"] == "RD270Q_UM.pdf"
    assert pairs.prod_for_url("http://aem:4502/content/guide/consumer/monitor/rd270q-timing/en/x.html")["name"] \
        == "RD270Q_timing_en.pdf"


def test_a_timing_guide_with_no_timing_file_reports_nothing(tmp_path, monkeypatch):
    pairs = _lib(tmp_path, monkeypatch, ["FM/MA270U_EN_V2/MA270U_EN_V2/MA270U-en.pdf"])
    assert pairs.prod_for_url("http://aem:4502/content/guide/consumer/monitor/ma270u-timing/en/x.html")["prod"] == ""


def test_the_misspelt_timming_counts_as_timing():
    assert server._is_timing("RD270Q_Timming_Table.pdf") and server._is_timing("pd30-timing")
    assert not server._is_timing("RD270Q-EM-V0")


def test_two_folders_pair_timing_with_timing_only(tmp_path):
    """The A <-> B folder match: files whose names normalise alike but where one is a timing table
    and the other the manual are listed as unpaired, not compared against each other."""
    a, b = tmp_path / "a", tmp_path / "b"
    for root, names in ((a, ["RD270Q timing user manual.pdf", "RD270Q user manual.pdf"]),
                        (b, ["RD270Q timing user manual.pdf", "RD270Q user manual.pdf"])):
        root.mkdir()
        for n in names:
            (root / n).write_bytes(b"%PDF-1.4")
    m = server._match_folders(a, b)
    assert {x["name"] for x in m["matched"]} == {"RD270Q timing user manual.pdf", "RD270Q user manual.pdf"}
