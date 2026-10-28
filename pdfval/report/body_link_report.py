"""Body hyperlink report (body-link-issues.pdf / .csv): the hyperlinks in the page body only, and
nothing else -

    Broken link              a body link that does not open (HTTP error, network error) or whose
                             #anchor does not exist on the target page
    Redirects to a GUID      a readable address that lands on the raw DITA GUID page
                             (GUID-17f6ffdc-….html): the link opens, but the reader ends on an
                             address AEM never resolved to the topic's page
    GUID in the link         the body link's own address or label still carries a DITA GUID
    Not verified             a body link the crawl could not check (not http(s), or not opened)

The chrome is deliberately out: the left navigation, breadcrumb, next/previous and "On this page" links
are the site report's and the AEM site report's business. This report covers what a reader clicks inside
the text of the topic, from the site run's own rows (pdfval/site_nav.py, rows tagged `body`). The links
that opened cleanly are listed too, as pass lines, so the report also says how many body links were
checked. Removed when a run has no body links at all.
"""
from __future__ import annotations

import csv
from pathlib import Path

from . import site_report

# the report's sections, in order: (id, heading)
GROUPS = [
    ("body-link-broken", "Broken link"),
    ("body-link-guid-redirect", "Link redirects to a GUID address"),
    ("body-link-guid", "GUID in the link (address or label)"),
    ("body-link-unchecked", "Link not verified"),
    ("body-link-ok", "Links that open"),
]
# which site-row kind belongs to which section. "link-wrong-target" (a working link whose target page is
# titled differently) stays out, as in the AEM site report: where a working body link lands is not
# reported - a cross-reference's text is a phrase in a sentence, not the target page's title
ROW_KINDS = {"link-broken": "body-link-broken", "link-guid-redirect": "body-link-guid-redirect",
             "link-guid": "body-link-guid", "link-not-http": "body-link-unchecked",
             "link-unchecked": "body-link-unchecked", "link-ok": "body-link-ok"}


def rows(result: dict) -> list[dict]:
    """Every body-hyperlink row of the site run, in this report's own groups."""
    out = []
    for r in (result.get("site") or {}).get("rows") or []:
        if not r.get("body"):
            continue  # the chrome: left navigation, breadcrumb, pager, "On this page"
        gid = ROW_KINDS.get(r.get("kind", ""))
        if gid:
            out.append({**r, "group": gid})
    return out


def summary(result: dict) -> dict | None:
    """{links, fail, warn, pass, broken, guid, status} for the UI, or None when the run has no body
    links (a plain PDF ⇄ PDF compare, or a site run whose pages carry no links in their text)."""
    rws = rows(result)
    if not rws:
        return None
    n = lambda g: sum(1 for r in rws if r["group"] == g)
    counts = {k: sum(1 for r in rws if r["status"] == k) for k in ("fail", "warn", "pass")}
    return {"links": len(rws), **counts, "broken": n("body-link-broken"),
            "guid": n("body-link-guid-redirect") + n("body-link-guid"),
            "status": "fail" if counts["fail"] else "warn" if counts["warn"] else "pass"}


def write_csv(rws: list[dict], path: Path) -> Path:
    titles = dict(GROUPS)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["Bug ID", "Issue", "Status", "Link text", "Expected", "Actual", "Page", "Note", "Element"])
        for r in rws:
            # the same Bug_NNN number the other reports give this row (a passing link has none)
            w.writerow([r.get("bug", ""), titles.get(r["group"], r["group"]), r["status"], r.get("item", ""),
                        r.get("expected", ""), r.get("actual", ""), r.get("page", ""), r.get("note", ""),
                        r.get("where", "")])
    return path


def build(result: dict, out_dir: str | Path, *, filename: str = "body-link-issues.pdf") -> Path | None:
    """Write body-link-issues.csv and body-link-issues.pdf. Both are removed when the run has no body
    links, so the folder never carries an empty report."""
    out_dir = Path(out_dir)
    pdf_path, csv_path = out_dir / filename, out_dir / (Path(filename).stem + ".csv")
    rws = rows(result)
    if not rws:
        pdf_path.unlink(missing_ok=True)
        csv_path.unlink(missing_ok=True)
        return None
    write_csv(rws, csv_path)
    counts: dict[str, dict] = {}
    for r in rws:
        c = counts.setdefault(r["group"], {"pass": 0, "warn": 0, "fail": 0, "info": 0})
        c[r["status"]] = c.get(r["status"], 0) + 1
    site = result.get("site") or {}
    sm = summary(result) or {}
    view = {**result, "site": {**site, "rows": rws,
                               "summary": {"status": sm.get("status", "pass"),
                                           "pages": (site.get("summary") or {}).get("pages", 0),
                                           "fail": sm.get("fail", 0), "warn": sm.get("warn", 0),
                                           "pass": sm.get("pass", 0), "groups": counts}}}
    return site_report.build(view, out_dir, filename=filename, groups=GROUPS,
                             title="Body hyperlinks — broken links and GUID addresses")
