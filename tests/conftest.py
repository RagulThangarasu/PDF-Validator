import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def pytest_addoption(parser):
    parser.addoption("--baseline", help="reference PDF (prod)")
    parser.addoption("--candidate", help="PDF under test (stage)")
    parser.addoption("--pdfval-config", help="TOML overrides")
