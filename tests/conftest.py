import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def pytest_addoption(parser):
    parser.addoption("--baseline", help="reference PDF (prod)")
    parser.addoption("--candidate", help="PDF under test (stage)")
    parser.addoption("--pdfval-config", help="TOML overrides")


# the CSS / typography / layout and image-text types the default config leaves out of the reports
# ([ignore] types): tests of those detectors switch them back on
CSS_IGNORED = {"font-size", "font-family", "font-weight", "font-style", "color", "line-height", "text-align",
               "space-above", "line-wrap", "row alignment", "spec font-size", "spec font-family", "spec font-weight",
               "spec color", "spec line-height", "spec text-decoration", "spec text-align", "spec bullet",
               "spec list numbering", "spec callout title", "spec callout background", "spec callout content",
               "spec callout icon", "spec cover", "spec page margin", "spec page size", "spec pagination",
               "spec page structure", "image blurred", "text as graphic", "superscript"}


def all_checks(c: dict) -> dict:
    c["ignore"]["types"] = [t for t in c["ignore"]["types"] if t not in CSS_IGNORED]
    return every_picture_issue(c)


def every_picture_issue(c: dict) -> dict:
    """Tests of the picture detectors (another version, alignment, distorted, wrong section, ...): the reports
    keep only [assets] report_types (artwork / label missing, much smaller) - the detectors are tested unfiltered."""
    c["assets"]["report_types"] = None
    return c
