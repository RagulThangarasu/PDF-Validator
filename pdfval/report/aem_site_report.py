"""AEM site validation report (aem-site-issues.pdf / .csv): the four issues an AEM site review asks
for, and nothing else -

    Link missing          text that is a link in the prod PDF but plain text on the site
    Link not working      a link that does not open (HTTP error, anchor not found) or opens the wrong page
    Link contains a GUID  an address or label still carrying a DITA GUID (GUID-17f6ffdc-…): a reference
                          AEM published without resolving it to the topic's page
    List out of margin    a list whose markers (1. 2. 3. / bullets) do not start at the left edge of the
                          paragraph above it

The first comes from the comparison's own findings ("missing link"), the other three from the site run's
rows (pdfval/site_nav.py). The full reports are untouched: this one is a filtered view of the same run.
"""
from __future__ import annotations

import csv
from pathlib import Path

from . import site_report

# the report's sections, in order: (id, heading, which site-row kinds belong to it)
GROUPS = [
    ("aem-link-missing", "Link missing"),
    ("aem-link-broken", "Link not working"),
    ("aem-link-guid", "Link contains a GUID"),
    ("aem-list-margin", "List out of the text margin"),
]
ROW_KINDS = {"link-broken": "aem-link-broken", "link-wrong-target": "aem-link-broken",
             "link-guid": "aem-link-guid", "list-margin": "aem-list-margin"}
# findings of the comparison that are a missing link ("Link not clickable in stage: …")
MISSING_LINK_TYPES = {"missing link"}
BROKEN_LINK_TYPES = {"broken link"}


def rows(result: dict) -> list[dict]:
    """The four kinds of issue as site-report rows, in the report's own groups."""
    out = []
    for r in (result.get("site") or {}).get("rows") or []:
        gid = ROW_KINDS.get(r.get("kind", ""))
        if gid and r["status"] in ("fail", "warn"):
            out.append({**r, "group": gid})
    # the comparison's own link findings: text that is a link in prod and plain on the site, and links
    # the stage document itself reports as broken - the site rows only cover the links the crawl opened
    url = ((result.get("meta") or {}).get("candidate") or {}).get("url", "")
    for sec in result.get("sections") or []:
        for f in sec.get("findings") or []:
            types = set(f.get("types") or [])
            gid = ("aem-link-missing" if types & MISSING_LINK_TYPES else
                   "aem-link-broken" if types & BROKEN_LINK_TYPES else "")
            if not gid:
                continue
            out.append({"group": gid, "status": "fail" if f.get("severity") == "error" else "warn",
                        "item": sec.get("title") or "Link", "expected": "", "actual": "",
                        "page": url, "note": f.get("message", ""), "where": "",
                        "bug": f.get("bug") or f.get("id", ""), "kind": "link-missing"})
    return out


def write_csv(rws: list[dict], path: Path) -> Path:
    titles = dict(GROUPS)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["Bug ID", "Issue", "Status", "Item", "Expected", "Actual", "Page", "Note", "Element"])
        for r in rws:
            w.writerow([r.get("bug", ""), titles.get(r["group"], r["group"]), r["status"], r.get("item", ""),
                        r.get("expected", ""), r.get("actual", ""), r.get("page", ""), r.get("note", ""),
                        r.get("where", "")])
    return path


def build(result: dict, out_dir: str | Path, *, filename: str = "aem-site-issues.pdf") -> Path | None:
    """Write aem-site-issues.csv and aem-site-issues.pdf. Both are removed when the run has none of
    these four issues, so the folder never carries an empty report."""
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
    view = {**result, "site": {**site, "rows": rws,
                               "summary": {"status": "fail" if any(r["status"] == "fail" for r in rws) else "warn",
                                           "pages": (site.get("summary") or {}).get("pages", 0),
                                           "fail": sum(c["fail"] for c in counts.values()),
                                           "warn": sum(c["warn"] for c in counts.values()), "pass": 0,
                                           "groups": counts}}}
    return site_report.build(view, out_dir, filename=filename, groups=GROUPS,
                             title="AEM site validation — links and list margins")
