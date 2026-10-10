"""An AEM site validation reports only the issue kinds asked for: content missing / extra, a picture
missing or under the wrong heading, a picture rendered too big or too small, and picture alignment.

Everything else a PDF run reports (links, icons, tables, layout structure, bold / italic, wording,
spacing, case, punctuation) stays out of a web run. The engine intersects [html] report_types with
[genuine] types, so a name that is not in both reports nothing at all - the reason this is pinned.
"""
from pdfval import engine

WANTED = {
    "missing text", "extra text", "missing section", "extra section",
    "missing image", "image in wrong section",
    "image bigger", "image smaller",
    "image alignment", "image vertical alignment",
}


def _web_types(cfg) -> set[str]:
    """What a web run is left with: [html] report_types narrowed by [genuine] types (engine.compare)."""
    return set(cfg["genuine"]["types"]) & set(cfg["html"]["report_types"])


def test_a_web_run_reports_exactly_the_kinds_asked_for():
    assert _web_types(engine.load_config()) == WANTED


def test_every_wanted_kind_survives_the_genuine_intersection():
    """A name only in [html] report_types is silently dropped - it must be in [genuine] types too."""
    cfg = engine.load_config()
    missing = WANTED - set(cfg["genuine"]["types"])
    assert not missing, f"not in [genuine] types, so never reported: {sorted(missing)}"


def test_the_kinds_a_web_run_must_not_report():
    """The wording / link / table / icon / layout families a web guide is not validated for."""
    out = _web_types(engine.load_config())
    for t in ("changed text", "case", "punctuation", "spacing", "emphasis", "broken link", "missing link",
              "missing row", "cell differs", "missing table", "icon differs", "icon missing inline",
              "bullet", "numbering style", "list alignment", "row alignment", "placement",
              "image changed", "image pixelated", "image distorted", "image order", "extra image"):
        assert t not in out, t
