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
