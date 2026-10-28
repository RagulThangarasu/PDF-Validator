"""`new.pdf` - a second report per publication, holding one fixed set of checks.

The run's own reports stay as they are. This one is written beside them as `new.pdf`, and is handed
over under the publication's name: the page downloads it as “<publication> new.pdf” and the batch zip
stores it under that name. It lists only:

  1. Lists and emphasis   a number or letter that changed (1,2,3 → a,b,c), a numbering sequence out of
                          order, a bullet point missing or changed, and bold / italic that differs
  2. Picture size         the same picture at 40 % less or 40 % more than its size in prod
  3. Picture position     the same picture moved left / centre / right
  4. Icon wrapping        an inline icon pushed onto the next line (and one off its line's alignment)
  5. Table margin         a table wider than the text column, breaking the page margin
  6. Table of contents     any mismatch on a level 1 entry - missing, extra, renamed, relevelled, wrong page
  7. Text alignment       text set to another alignment than prod (left / centred / right / justified),
                          a list or table row out of line, a single column moved
  8. Text spacing         a gap opened up between words, lines set further apart, a paragraph break
                          stage has that prod does not

The size rule is applied here, on the finding's own numbers, so the report holds what it says whatever
threshold the run was configured with ([assets] smaller_report_under and friends only decide what the
run reports at all; a run that reports every size change still gets a 40 % report out of this).

Built by report.writer alongside genuine-issues.pdf; [report] new_report = false switches it off.
"""
from __future__ import annotations

from pathlib import Path

from . import pdf_report

# 1, 3, 4, 5 - straight from the finding's type
LISTS_AND_EMPHASIS = ("numbering style", "numbering format", "numbering sequence",
                      "bullet", "bullet marker", "marker glued", "emphasis")
# where a bullet point sits is layout, not a bullet that is missing or changed: a finding carrying one
# of these is about the marker's position, its gap to the text or its level, and is left out - the same
# line the genuine report draws (genuine.tag drops "bullet" from a finding tagged "indent")
BULLET_LAYOUT = ("indent", "list level", "wrap under number")
PICTURE_POSITION = ("image alignment", "placement")
ICON_WRAP = ("icon wraps to next line", "icon alignment")
TABLE_MARGIN = ("table past margin",)
# 7 - how the TEXT itself is set: aligned differently from prod (left / centred / right / justified,
#     a list or table row out of line, a single column moved) ...
TEXT_ALIGNMENT = ("spec text-align", "list alignment", "row alignment", "single column alignment",
                  "vertical alignment", "table cell alignment", "table header alignment")
# 8 - ... and the space in and around it: a gap opened up between words, lines set further apart,
#     a paragraph break that is not in prod
TEXT_SPACING = ("spacing", "wide gap", "word gap", "line height", "paragraph break")
# the badge beside a section title in this report (pdf_report's own FAIL / WARN wording is a verdict;
# this report is a list of things to check by eye)
MANUAL_REVIEW = ("MANUAL REVIEW", "#b45309")
# 6 - a TOC finding is kept only when the entry it is about is a level 1 one
TOC_TYPES = ("missing entry", "extra entry", "title differs", "level differs", "wrong page", "heading differs")
# 2 - these carry the two widths; the 40 % is applied to them below
SIZE_TYPES = ("image smaller", "image bigger")

# The run's metrics header and issue-count tiles belong to the main report; this one is the list and
# the screenshots. Leaving them out also keeps the report from depending on result["summary"] /
# result["toc"] being there at all, so it is built from whatever a run folder holds.
INCLUDE = {"summary": False, "categories": False, "genuine": False, "css": False,
           "critical": False, "coverage": False, "truncated": False, "sections": False,
           "toc": False, "toc_compare": False, "stylemap": False, "issues": True, "screenshots": True}


def _size_change(f: dict) -> float | None:
    """How much the picture's width changed, as a fraction of prod's (-0.4 = 40 % smaller, +0.4 = 40 %
    bigger). None when the finding does not carry both widths."""
    d = f.get("detail") or {}
    a, b = d.get("baseline_width"), d.get("candidate_width")
    if not a or b is None:
        return None
    try:
        return b / a - 1
    except ZeroDivisionError:
        return None


def _toc_level(result: dict, f: dict) -> int | None:
    """The level of the TOC entry a TOC finding is about, from either side of its row."""
    rows = ((result.get("toc") or {}).get("rows")) or []
    i = (f.get("detail") or {}).get("row")
    if not isinstance(i, int) or not 0 <= i < len(rows):
        return None
    row = rows[i]
    for side in ("baseline", "candidate"):
        if row.get(side) and row[side].get("level") is not None:
            return row[side]["level"]
    return None


def issue_ids(result: dict, size_change: float = 0.40) -> list[str]:
    """The ids of the findings this report lists, in the run's own order."""
    keep = []
    for s in result.get("sections") or []:
        for f in s.get("findings") or []:
            types = set(f.get("types") or [])
            listed = types & set(LISTS_AND_EMPHASIS)
            if types & set(BULLET_LAYOUT):
                # where a bullet sits is layout, so a finding about its position drops "bullet" - but
                # only that. A changed number or letter (1,2,3 -> a,b,c), a numbering sequence out of
                # order and bold are reported whatever else the same finding is tagged with: they are
                # the point of the report and must never fall out of it with the indent.
                listed = listed - {"bullet"}
            if listed or types & set(PICTURE_POSITION + ICON_WRAP + TABLE_MARGIN
                                     + TEXT_ALIGNMENT + TEXT_SPACING):
                keep.append(f["id"])
            elif types & set(SIZE_TYPES):
                change = _size_change(f)
                # a hair of tolerance: 0.18 / 0.30 is 0.6000000000000001 in binary, and a picture at
                # exactly 40 % less must not fall out of a 40 % report on that
                if change is not None and abs(change) >= size_change - 1e-9:
                    keep.append(f["id"])
            elif types & set(TOC_TYPES) and f.get("check") == "toc":
                if _toc_level(result, f) == 1:
                    keep.append(f["id"])
    return keep


def build(result: dict, out_dir: str | Path, size_change: float = 0.40) -> Path | None:
    """Write the run's `new.pdf`. None when this run has none of these issues - and any copy left from
    an earlier run is removed, so a clean run never leaves a stale report.

    The file is `new.pdf`, like every other report in a run folder (`genuine-issues.pdf`,
    `image-issues.pdf`): one fixed name the page and the batch zip can both reach without knowing what
    the publication is called. The publication's name goes on the way out - the UI downloads it as
    “<publication> new.pdf” and the zip stores it under that name.
    """
    out = Path(out_dir)
    path = out / "new.pdf"
    for old in out.glob("* new.pdf"):     # the publication-named file this report used to write
        old.unlink(missing_ok=True)
    ids = issue_ids(result, size_change)
    if not ids:
        path.unlink(missing_ok=True)
        return None
    # [report] exclude_groups drops whole groups from the other PDF reports ("formatting" by default).
    # This report is a named list of checks, so a chosen issue must not fall out of it because of the
    # group it belongs to: the run is handed over with that list empty, without touching the original.
    meta = {**(result.get("meta") or {})}
    meta["report"] = {**(meta.get("report") or {}), "exclude_groups": []}
    # pdf_report.build reads the run's summary and document metadata for its front page before it
    # looks at `include`, so a result that carries neither (a run folder written by an older version,
    # a result assembled by hand) would fail there. The report is the issue list either way: the
    # defaults only keep the front page from being the thing that stops it being written.
    blank_doc = {"path": "", "pages": 0, "page_size": [0, 0], "body_size": 0}
    for side in ("baseline", "candidate"):
        meta[side] = {**blank_doc, **(meta.get(side) or {})}
    scoped = {**result, "meta": {"generated": "", **meta},
              "summary": {"result": "fail", "fail": 0, "warn": 0, "pass": 0, **(result.get("summary") or {})}}
    # these checks are flagged for a person to look at, not judged: the section badge says so
    return pdf_report.build(scoped, out, options={"include": INCLUDE, "filter": {"ids": ids},
                                                  "status_label": MANUAL_REVIEW},
                            filename=path.name)
