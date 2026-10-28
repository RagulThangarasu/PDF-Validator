"""An AEM site validation reports only the issue kinds asked for: content missing / extra, and a
picture that is not there - missing from the page, or there but not loading.

Everything else a PDF run reports (links, icons, tables, layout structure, bold / italic, wording,
spacing, case, punctuation) stays out of a web run, and so does how a picture is laid out: a web page
reflows its pictures, so size, placement, sharpness and which heading a picture sits under are not
validated for a site. The engine intersects [html] report_types with [genuine] types, so a name that
is not in both reports nothing at all - the reason this is pinned.
"""
from pdfval import engine

WANTED = {
    "missing text", "extra text", "missing section", "extra section",
    "missing image", "broken image",
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
              "image changed", "image pixelated", "image distorted", "image order", "extra image",
              # how a picture is laid out: a web page reflows its pictures
              "image in wrong section", "image bigger", "image smaller",
              "image alignment", "image vertical alignment"):
        assert t not in out, t


# --- PDF <-> PDF: hyperlinks are checked for whether they work, not for where they land -------------

def test_a_pdf_run_still_reports_a_link_that_does_not_work():
    g = set(engine.load_config()["genuine"]["types"])
    for t in ("broken link", "missing link", "extra link", "link target differs", "page zero"):
        assert t in g, t


def test_where_a_working_link_lands_is_reported_for_the_content_review():
    """"link lands elsewhere" is back on: content.docx is built around hyperlink redirection, so a
    working link that arrives somewhere else than prod's does is a genuine issue again."""
    cfg = engine.load_config()
    assert "link lands elsewhere" in set(cfg["genuine"]["types"])
    assert "link lands elsewhere" not in set(cfg["ignore"]["types"])


def test_the_other_link_destination_checks_stay_off():
    """The PDF and the site number their pages differently, so these fire on documents whose links are
    all fine - and unlike where the link lands, they are not part of the content review."""
    cfg = engine.load_config()
    for t in ("link page number", "link to wrong section"):
        assert t in set(cfg["ignore"]["types"]), t
        assert t not in set(cfg["genuine"]["types"]), t
        assert t not in set(cfg["html"]["report_types"]), t


# --- how a table header's text sits in its column is not validated at all --------------------------

def test_table_header_alignment_is_not_validated():
    """Left-aligned or centred, a table header is fine. The rule fired on tables that are otherwise
    correct (prod "Item/Function/Range" left-aligned vs stage indented 9pt) and is not something a
    reader of the manual notices."""
    cfg = engine.load_config()
    assert "table header alignment" in set(cfg["ignore"]["types"])
    assert "table header alignment" not in set(cfg["genuine"]["types"])
    assert "table header alignment" not in set(cfg["html"]["report_types"])


def test_the_rest_of_the_table_checks_are_untouched():
    """Only the header's alignment goes - a header or row missing, merged or split cells, and the
    header's own text all still report."""
    g = set(engine.load_config()["genuine"]["types"])
    for t in ("missing header", "missing row", "missing table", "rows merged", "row split",
              "cells merged", "cells split", "cell differs", "single column alignment"):
        assert t in g, t


# --- the content verdict's threshold ----------------------------------------------------------------

def test_a_section_above_95_percent_passes():
    """[content] pass_pct: a score of 95 % or better is a pass, 90-95 % a warning, below that a fail."""
    cfg = engine.load_config()
    assert cfg["content"]["pass_pct"] == 95.0 and cfg["content"]["warn_pct"] == 90.0
    from pdfval.report.pdf_report import _pct_status
    c = lambda pct: {"match_pct": pct, "pass_pct": cfg["content"]["pass_pct"], "warn_pct": cfg["content"]["warn_pct"]}
    assert [_pct_status(c(p)) for p in (100.0, 97.3, 95.0)] == ["pass", "pass", "pass"]
    assert [_pct_status(c(p)) for p in (94.9, 90.0)] == ["warn", "warn"]
    assert _pct_status(c(89.9)) == "fail"


# --- a callout set with only its icon -----------------------------------------------------------

def test_an_icon_stands_for_the_callout_label():
    """Prod (InDesign) sets a note with its icon alone where stage prints the label. The icon says
    which label it stands for, so "NOTE" against a pencil is house style, not a difference - and
    "NOTE" against a warning triangle is "callout type differs"."""
    cfg = engine.load_config()["content"]
    assert cfg["check_callout_icon_label"] is True
    icons = {k: {l.lower() for l in v} for k, v in cfg["callout_icons"].items()}
    assert set(icons) == {"pencil", "lightbulb", "exclamation", "triangle"}
    # each icon's English label, and its label in the languages the manuals are published in
    # (see tests/test_callout_label_languages.py)
    assert {"note", "注記", "참고"} <= icons["pencil"]
    assert {"tip", "提示"} <= icons["lightbulb"]
    assert {"important", "重要"} <= icons["exclamation"]
    assert {"warning", "警告"} <= icons["triangle"]
    assert "note" not in icons["triangle"] and "warning" not in icons["pencil"]
    assert "callout type differs" in set(engine.load_config()["genuine"]["types"])
