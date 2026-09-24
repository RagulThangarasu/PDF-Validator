"""Section-level parity suite for CI: one pytest case per baseline section.

  pytest tests/test_pdf_parity.py --baseline prod.pdf --candidate stage.pdf -q
  pytest ... -k "Mounting"          # validate a subset of sections
"""
import pytest

from pdfval import compare, load_config


def pytest_generate_tests(metafunc):
    if "section" not in metafunc.fixturenames:
        return
    base, cand = metafunc.config.getoption("--baseline"), metafunc.config.getoption("--candidate")
    if not (base and cand):
        metafunc.parametrize("section", [pytest.param(None, marks=pytest.mark.skip("--baseline/--candidate not given"))])
        return
    result = compare(base, cand, load_config(metafunc.config.getoption("--pdfval-config")))
    metafunc.parametrize("section", result["sections"], ids=[s["title"] for s in result["sections"]])


def test_section(section):
    """Fails on critical/breaking issues or content match below the warn threshold;
    CSS/layout differences are reported by the other outputs, not failed here."""
    crit = [f for f in section["findings"] if f.get("critical")]
    c = section["content"]
    assert not crit and c["status"] != "fail", (
        f"{section['title']}: content {c['match_pct']:.2f}% ({c['status']}), {len(crit)} critical\n"
        + "\n".join(f"  [{f['check']}] {f['message']}" for f in crit[:25]))
