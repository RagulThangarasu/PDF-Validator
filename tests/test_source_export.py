"""Source export: raw data (unfiltered) and the document rebuilt as well-formed DITA."""
import json
import zipfile
import xml.etree.ElementTree as ET

import pymupdf

from pdfval import load_config, source_export
from test_genuine import ROWS, base, make


def _export(tmp_path, secs):
    pdf = make(tmp_path / "doc.pdf", secs)
    doc = pymupdf.open(pdf)  # add a web link, an embedded file and a note callout
    page = doc[0]
    page.insert_text((72, 300), "Visit the support site for drivers.", fontsize=10, fontname="helv")
    page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(72, 290, 260, 303), "uri": "https://support.example.com"})
    page.insert_text((72, 330), "Note Unplug the display before cleaning it.", fontsize=10, fontname="helv")
    doc.embfile_add("wiring.txt", b"pin 1 = 5V")
    doc.saveIncr()
    out = source_export.export(pdf, tmp_path / "src.zip", load_config(), label="prod")
    return zipfile.ZipFile(out)


def test_raw_data_is_complete(tmp_path):
    z = _export(tmp_path, base())
    names = z.namelist()
    d = json.loads(z.read("raw/document.json"))
    assert d["page_count"] == 4 and [o["title"] for o in d["outline"]] == ["Overview", "Installation", "Setup", "Maintenance"]
    assert any(l["text"].startswith("Visit the support site") for l in d["pages"][0]["lines"])
    assert d["pages"][0]["links"][-1]["uri"] == "https://support.example.com"
    assert any(n.startswith("raw/images/") for n in names) and "raw/attachments/wiring.txt" in names
    assert any(n.startswith("raw/tables/") for n in names) and "raw/text.txt" in names and "raw/fonts.csv" in names
    table = next(t for p in d["pages"] for t in p["tables"])
    assert table["rows"][1] == ROWS[1]


def test_dita_source_is_well_formed_and_structured(tmp_path):
    z = _export(tmp_path, base())
    xml = {n: z.read(n) for n in z.namelist() if n.startswith("dita/") and n.endswith((".dita", ".ditamap"))}
    for n, data in xml.items():
        ET.fromstring(data)  # well-formed
    m = next(v for k, v in xml.items() if k.endswith(".ditamap")).decode()
    for t in ("overview", "installation", "setup", "maintenance"):
        assert f'href="topics/{t}.dita"' in m
    setup, inst, over = (xml[f"dita/topics/{t}.dita"].decode() for t in ("setup", "installation", "overview"))
    assert "<table>" in setup and "<entry>Brightness</entry>" in setup
    assert '<fig><image href="../images/' in inst and any(n.startswith("dita/images/") for n in z.namelist())
    assert '<xref href="https://support.example.com" scope="external" format="html">' in over
    assert '<note type="note">Unplug the display before cleaning it.</note>' in over
