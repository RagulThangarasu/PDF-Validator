"""`new.pdf` - a second report per publication, holding one fixed set of checks.

The run's own reports stay as they are. This one is written beside them as `new.pdf`, and is handed
over under the publication's name: the page downloads it as “<publication> new.pdf” and the batch zip
stores it under that name. It holds CONTENT and IMAGE issues only - what a reader sees in the words and the pictures. Layout
checks (icon wrapping, the table margin, text alignment and text spacing) are deliberately left out:
they are page-setting, not content, and the ones that matter here are expected anyway (a wide timing
table past the margin is fine, and left alignment on a single column or a table header is fine).

It lists only:

  1. Lists and emphasis   a number or letter that changed (1,2,3 → a,b,c), a numbering sequence out of
                          order, a bullet point missing or changed, and bold / italic that differs
  2. Picture size         the same picture at 40 % less or 40 % more than its size in prod
  3. Picture position     the same picture moved left / centre / right
  4. Table of contents    any mismatch on a level 1 entry - missing, extra, renamed, relevelled, wrong page

Nothing in it is a verdict: every section and every finding is badged REVIEW, for a person to look at
by eye. The report carries no PASS / FAIL / HIGH / WARNING wording.

The size rule is applied here, on the finding's own numbers, so the report holds what it says whatever
threshold the run was configured with ([assets] smaller_report_under and friends only decide what the
run reports at all; a run that reports every size change still gets a 40 % report out of this).

Built by report.writer alongside genuine-issues.pdf; [report] new_report = false switches it off.
"""
from __future__ import annotations

import re
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
# Icon wrapping, the table margin, text alignment and text spacing used to be listed here. They are
# page-setting rather than content, so this report no longer carries them; the run's own reports still
# do. Removing them also drops the two findings that are expected on these publications anyway -
# "table past margin" and single-column / table-header alignment.
# the badge beside a section title in this report (pdf_report's own FAIL / WARN wording is a verdict;
# this report is a list of things to check by eye)
MANUAL_REVIEW = ("REVIEW", "#b45309")
# the same wording on every finding, in place of HIGH / WARNING / INFO
REVIEW_SEVERITY = ("REVIEW", "#b45309")
# 4 - a TOC finding is kept only when the entry it is about is a level 1 one
TOC_TYPES = ("missing entry", "extra entry", "title differs", "level differs", "wrong page", "heading differs")
# 2 - these carry the two widths; the 40 % is applied to them below
SIZE_TYPES = ("image smaller", "image bigger")

# every type this report can list, in one place: report/shots.py reads it to make sure a finding that
# reaches this report has a screenshot rendered for it. Keeping it here is what stops the two drifting
# apart when the report's scope changes.
REPORT_TYPES = frozenset(LISTS_AND_EMPHASIS + PICTURE_POSITION + SIZE_TYPES + TOC_TYPES)

# The run's metrics header and issue-count tiles belong to the main report; this one is the list and
# the screenshots. Leaving them out also keeps the report from depending on result["summary"] /
# result["toc"] being there at all, so it is built from whatever a run folder holds.
# The live-site checklist sections are off too: "nav" is the Left navigation / TOC comparison and
# "typescale" + "site_fontsize" are the Figma design-spec font-size table, neither of which is a
# content or picture issue; the margin break and the hyperlink listing go with them. They are still
# written in full to the run's own reports. "site_images" stays on - pictures are in scope here.
INCLUDE = {"summary": False, "categories": False, "genuine": False, "css": False,
           "critical": False, "coverage": False, "truncated": False, "sections": False,
           "toc": False, "toc_compare": False, "stylemap": False, "issues": True, "screenshots": True,
           "nav": False, "typescale": False, "site_fontsize": False, "site_margin": False,
           "site_links": False}


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
            if listed or types & set(PICTURE_POSITION):
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

    The file carries the publication's name on disk: “<publication> new.pdf”. The page and the batch
    zip both reach it through the `* new.pdf` glob they already fall back to, so nothing has to know
    the name in advance; a run whose publication is unknown keeps the plain `new.pdf`.
    """
    out = Path(out_dir)
    name = str(((result.get("meta") or {}).get("name") or "")).strip()
    name = re.sub(r"[^\w .()&+-]+", "_", name).strip()
    path = out / (f"{name} new.pdf" if name else "new.pdf")
    for old in out.glob("* new.pdf"):     # a copy under an earlier publication name
        if old != path:
            old.unlink(missing_ok=True)
    (out / "new.pdf").unlink(missing_ok=True) if path.name != "new.pdf" else None
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
                                                  "status_label": MANUAL_REVIEW,
                                                  "severity_label": REVIEW_SEVERITY},
                            filename=path.name)
