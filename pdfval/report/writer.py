"""Write results.json, junit.xml, summary.md, report.pdf (issues + screenshots) and the interactive viewer."""
from __future__ import annotations

import csv
import json
import os
import shutil
from pathlib import Path
from typing import Callable
from xml.sax.saxutils import escape, quoteattr

from . import pdf_report
from . import shots as shotmod

VIEWER = Path(__file__).with_name("viewer.html")


# built when first opened (batch runs): the full report and the CSS report, with every finding's screenshot
DEFERRED = ("report.pdf",)


# the consolidated report: the issue reports, in this order, merged into one file named after the prod PDF.
# report.pdf is left out on purpose - it is the exhaustive internal report (coverage, style map, every
# passing section) and would duplicate most of what is here at twice the size. site.pdf is left out too -
# a site run's own TOC/pager/CSS/layout checklist is a separate deliverable, not folded into this one.
_CONSOLIDATED = (("genuine-issues.pdf", "Issues — content, design spec and layout"),
                 ("image-issues.pdf", "Pictures — numbers, labels, artwork"))


def report_name(result: dict) -> str:
    """The prod PDF's own name, for the consolidated report's file name: a reader with twenty manuals to
    get through should see which one a report belongs to without opening it."""
    meta = result.get("meta") or {}
    stem = Path((meta.get("baseline") or {}).get("path") or "").stem or (meta.get("name") or "validation")
    safe = "".join(ch if (ch.isalnum() or ch in " ._-") else "-" for ch in stem).strip(" .-")
    return (safe or "validation")[:120]


_TYPESCALE_HEADING = "Design spec (Figma)"


def _typescale_vocabulary(result: dict) -> tuple[set[str], set[str]]:
    """(style names, every other cell the Figma table can print), taken from the spec the report was
    built against - so a page is recognised as part of that table by its own data, not by guesswork."""
    styles = ((result.get("meta") or {}).get("typography") or {}).get("styles") or []
    names, cells = set(), set()
    for st in styles:
        name = str(st.get("style") or "").strip()
        if not name:
            continue
        names.add(name)
        cells.add(str(st.get("font") or "").strip())
        weight = str(st.get("weight") or "").strip()
        cells |= {weight, f"{weight}, underlined"}
        for key in ("size", "line_height"):
            if isinstance(st.get(key), (int, float)):
                cells.add(f"{st[key]:g} pt")
        cells.add(str(st.get("color") or "").strip())
    return names, {c for c in cells if c}


def _is_typescale_continuation(page, names: set[str], cells: set[str]) -> bool:
    """True when the page holds nothing but more rows of that table (it runs over the page break).

    Every line must be one the table itself prints: a style name, one of its cells, the "Stage (actual)"
    column (always "x · y · z" or "not used in stage"), or the page footer. One line that is none of
    those and the page is left alone - a real page is never dropped to tidy up a table.
    """
    blocks = page.get_text("blocks")
    # the footer (page number + product name) is on every page: found by its own text, then everything
    # from there down is ignored, so the product name is not mistaken for something outside the table
    foot = min([b[1] for b in blocks if "page " in b[4] and " of " in b[4]], default=page.rect.y1)
    lines = [ln.strip() for b in blocks if b[3] <= foot + 1 for ln in b[4].splitlines() if ln.strip()]
    if not lines or not any(ln in names for ln in lines):
        return False
    for ln in lines:
        if ln in names or ln in cells or " · " in ln or ln == "not used in stage":
            continue
        return False  # something that is not part of the table: keep the page
    return True


_TOC_HEADING = "TOC comparison"
_NAV_HEADING = "Left navigation"


def strip_nav(doc) -> list[int]:
    """Take the Left navigation listing out of the consolidated copy.

    It is the whole TOC set against the left navigation entry by entry - measured by the site
    report that owns it, and pages of listing in the one file a reviewer is handed. Same
    treatment as the TOC comparison: a page that opens with the heading goes whole, otherwise
    the block is redacted where it starts.
    """
    import pymupdf

    drop, n = [], 0
    while n < doc.page_count:
        page = doc[n]
        hits = page.search_for(_NAV_HEADING)
        # the words also appear as a COLUMN heading inside the table; only the section
        # heading sits near the top of the page with nothing above it but the margin
        hits = [r for r in hits if r.y0 <= page.rect.y0 + 140]
        if not hits:
            n += 1
            continue
        top = min(r.y0 for r in hits) - 2
        if top <= page.rect.y0 + 90:
            drop.append(n)
        else:
            feet = [b[1] for b in page.get_text("blocks")
                    if "page " in b[4] and " of " in b[4] and b[1] > top]
            page.add_redact_annot(pymupdf.Rect(page.rect.x0, top, page.rect.x1,
                                               min(feet or [page.rect.y1]) - 2))
            page.apply_redactions()
        n += 1
    if drop:
        doc.delete_pages(drop)
    return drop


def strip_toc_compare(doc) -> list[int]:
    """Take the TOC comparison out of the consolidated copy.

    It belongs to the report that measures it; in the one file a reviewer is handed it is pages
    of entry-by-entry listing that the issues and metrics already account for. Returns the
    0-based pages dropped, so bookmarks can move with them.
    """
    import pymupdf

    drop, n = [], 0
    while n < doc.page_count:
        page = doc[n]
        hits = page.search_for(_TOC_HEADING)
        if not hits:
            n += 1
            continue
        top = min(r.y0 for r in hits) - 2
        head = page.get_text("blocks")
        # a page that OPENS with the heading is nothing but the comparison: drop it whole
        if top <= page.rect.y0 + 90:
            drop.append(n)
        else:
            feet = [b[1] for b in head if "page " in b[4] and " of " in b[4] and b[1] > top]
            page.add_redact_annot(pymupdf.Rect(page.rect.x0, top, page.rect.x1,
                                               min(feet or [page.rect.y1]) - 2))
            page.apply_redactions()
        n += 1
        # the listing runs on: every following page that still opens with a TOC row
        while n < doc.page_count and _is_toc_continuation(doc[n]):
            drop.append(n)
            n += 1
    if drop:
        doc.delete_pages(drop)
    return drop


def _is_toc_continuation(page) -> bool:
    """A page that is only more of the TOC comparison: its rows carry the status words that
    table uses and nothing else of substance."""
    text = page.get_text()
    if _TOC_HEADING in text:
        return False
    rows = sum(text.count(w) for w in ("Pass", "Fail", "Extra on stage"))
    return rows >= 6 and len(text.strip()) < 4000


def strip_typescale(doc, result: dict) -> list[int]:
    """Take the Figma design-spec table out of the consolidated copy entirely.
    Returns the 0-based pages that were dropped, so bookmarks can be moved with them.

    Every copy is taken out: the design spec is measured by the report that owns it, and the
    consolidated file is for the issues and the metrics.

    The table is not on a page of its own: it sits at the foot of a report's summary page and runs over
    onto the next. So the block is redacted where it starts, and a page that is only its continuation is
    dropped. Nothing is reflowed - the summary page simply ends where the table began.
    """
    import pymupdf

    names, cells = _typescale_vocabulary(result)
    if not names:
        return []
    drop, n = [], 0
    while n < doc.page_count:
        page = doc[n]
        hits = page.search_for(_TYPESCALE_HEADING)
        if not hits:
            n += 1
            continue
        # EVERY copy goes, not just the repeats: the design-spec table is measured by the
        # report that owns it and is not wanted in the consolidated file at all
        top = min(r.y0 for r in hits) - 2
        # down to the footer line, so the page number at the bottom survives
        feet = [b[1] for b in page.get_text("blocks") if "page " in b[4] and " of " in b[4] and b[1] > top]
        page.add_redact_annot(pymupdf.Rect(page.rect.x0, top, page.rect.x1, min(feet or [page.rect.y1]) - 2))
        page.apply_redactions()
        n += 1
        while n < doc.page_count and _is_typescale_continuation(doc[n], names, cells):
            drop.append(n)
            n += 1
    if drop:
        doc.delete_pages(drop)
    return drop


def write_consolidated(result: dict, out_dir: str | Path) -> Path | None:
    """One PDF per run, named after the prod PDF: every issue report merged in order, each part a
    bookmark so the reader can jump to it. Returns None when no part was built (a clean run)."""
    import pymupdf

    out = Path(out_dir)
    # the issues part is built for this file rather than taken from genuine-issues.pdf: the same
    # issues and the same metrics, without the Figma type scale and the TOC comparison, which belong
    # to the report that measures them. Falls back to genuine-issues.pdf if that build fails.
    body = out / "_consolidated-body.pdf"
    body.unlink(missing_ok=True)
    if (out / "genuine-issues.pdf").exists():
        try:
            # [report] exclude_groups drops whole groups from the PDF reports ("formatting" by
            # default, which is where every picture and layout finding sits). This file lists a
            # NAMED set of types, so a chosen issue must not fall out of it because of the group
            # it belongs to - the run is handed over with that list empty, original untouched.
            meta = {**(result.get("meta") or {})}
            meta["report"] = {**(meta.get("report") or {}), "exclude_groups": []}
            pdf_report.build({**result, "meta": meta}, out,
                             options=pdf_report.CONSOLIDATED_BODY, filename=body.name)
        except Exception:
            import traceback
            traceback.print_exc()
    if body.exists():
        # the body already holds the picture issues this report lists (drawn smaller, moved, not
        # loading); merging image-issues.pdf as well would put every other picture issue back in
        parts = [(body, _CONSOLIDATED[0][1])]
    else:
        parts = [(out / f, title) for f, title in _CONSOLIDATED if (out / f).exists()]
    if not parts:
        return None
    path = out / f"{report_name(result)}-validation-report.pdf"
    path.unlink(missing_ok=True)  # never merge a previous run's copy into this one
    merged, toc = pymupdf.open(), []
    try:
        for src_path, title in parts:
            with pymupdf.open(src_path) as src:
                if not src.page_count:
                    continue
                toc.append([1, title, merged.page_count + 1])
                merged.insert_pdf(src)
        if not merged.page_count:
            return None
        # the Figma type scale is reference material for the typography findings: useful in the report that
        # raises them, repeated once per merged part here - so it is left out of the consolidated file only
        # Applied one after the other, and the bookmarks moved after EACH: the second strip
        # numbers its pages in the document the first one already shortened, so the two lists
        # cannot be added together.
        for dropped in (strip_typescale(merged, result), strip_toc_compare(merged),
                        strip_nav(merged)):
            # a bookmark points at a page number: move it up by however many pages went before it
            for entry in toc:
                entry[2] -= sum(1 for d in dropped if d < entry[2] - 1)
        merged.set_toc([t for t in toc if 1 <= t[2] <= merged.page_count])
        merged.save(str(path), garbage=3, deflate=True)
    finally:
        merged.close()
        body.unlink(missing_ok=True)   # a working file: the merged report is the deliverable
    # the viewer offers it for download by name: recorded here so the page never has to guess how the
    # prod PDF's name was turned into a file name
    result.setdefault("meta", {})["consolidated"] = path.name
    return path


def image_summary(result: dict) -> dict:
    """{"issues": n, "result": "pass" | "fail"}: PASS = no image issue, or a publication whose content
    matches prod to within [content] pass_pct.

    A product scoring above pass_pct reads PASS everywhere, the picture verdict included - the issue
    count is still carried in "issues" and the image report is still built, so nothing is hidden, it is
    just not called a failure. (`result: "fail" if n else "pass"` restores counting alone.)
    """
    from . import image_report
    n = len(image_report.issues(result))
    content = ((result.get("summary") or {}).get("content") or {})
    pct, pass_pct = content.get("match_pct"), content.get("pass_pct", 95.0)
    passes = n == 0 or (pct is not None and pct >= pass_pct)
    return {"issues": n, "result": "pass" if passes else "fail"}


def write_image_report(result: dict, out_dir: str | Path) -> Path | None:
    """The image report (image-issues.pdf) - whenever the run found something about a picture, however
    well the text scores. The verdict in the summary is another question: a publication whose content
    matches prod still reads PASS (image_summary), but a picture issue that exists has to be in a
    report somebody is handed. Only a run with no picture issue at all writes none, and one left from
    an earlier run is removed."""
    from . import image_report
    out = Path(out_dir)
    result.setdefault("summary", {})["images"] = sm = image_summary(result)
    if not sm["issues"]:
        (out / "image-issues.pdf").unlink(missing_ok=True)
        return None
    return image_report.build(result, out)


def assign_bug_ids(result: dict) -> int:
    """Every issue gets a running number for bug tracking - Bug_001, Bug_002, … - in the order of the PDF
    report (content, links, formatting; section by section), then the picture issues of the image report, then
    the failed / warned site checks of a web run. The same issue has the same number in every report and CSV."""
    from . import image_report
    n, seen = 0, set()

    def give(f: dict) -> None:
        nonlocal n
        if id(f) in seen:
            return
        seen.add(id(f))
        n += 1
        f["bug"] = f"Bug_{n:03d}"

    for s in result.get("sections", []):
        for f in s.get("findings", []) + (s.get("image_findings") or []):
            f.pop("bug", None)
    for _, f in pdf_report.select_issues(result, pdf_report.GENUINE["filter"], None):
        give(f)
    for s in result.get("sections", []):
        for f in s.get("findings", []):
            if f.get("genuine") or pdf_report.is_image_issue(f):
                give(f)
    by_id = {f["id"]: f for sec in result.get("sections", []) for f in sec.get("findings", []) + (sec.get("image_findings") or [])}
    try:
        for _, f, _name in image_report.issues(result):  # (the image report works on copies: number the finding itself)
            if f.get("id") in by_id:
                give(by_id[f["id"]])
    except Exception:  # the image report is optional: the other numbers stand
        pass
    for r in (result.get("site") or {}).get("rows", []):
        r.pop("bug", None)
        if r.get("status") in ("fail", "warn"):
            n += 1
            r["bug"] = f"Bug_{n:03d}"
    return n


def write_pdf_report(result: dict, out_dir: str | Path) -> Path | None:
    """The PDF report (genuine-issues.pdf) - only when there is at least one genuine issue: a clean
    pass needs no report, and one left from an earlier run is removed."""
    out = Path(out_dir)
    assign_bug_ids(result)
    # a web run whose only issues are against the design spec (fonts, sizes, line heights, spacing, layout)
    # still has a bug report to write: those are reported here too, so "no content issue" is not "no issue"
    spec_issues = any(r["group"] in ("typography", "layout") and r["status"] in ("fail", "warn")
                      for r in (result.get("site") or {}).get("rows", []))
    if not (result.get("summary") or {}).get("genuine", {}).get("total") and not spec_issues:
        (out / "genuine-issues.pdf").unlink(missing_ok=True)
        (out / "genuine-issues.docx").unlink(missing_ok=True)
        (out / "content.docx").unlink(missing_ok=True)
        return None
    path = pdf_report.build(result, out, options=pdf_report.GENUINE, filename="genuine-issues.pdf")
    write_docx_report(result, out)
    write_content_report(result, out)   # content.docx: the content review on its own
    write_new_report(result, out)       # "<publication> new.pdf": its own fixed set of checks
    return path


def write_new_report(result: dict, out_dir: str | Path) -> Path | None:
    """“<publication> new.pdf” - lists, picture size and position, icon wrapping, the table margin and
    the level 1 TOC entries, and nothing else (pdfval/report/new_report.py)."""
    rcfg = ((result.get("meta") or {}).get("report") or {})
    if not rcfg.get("new_report", True):
        return None
    try:
        from . import new_report
        n = len(new_report.issue_ids(result, float(rcfg.get("new_report_size_change", 0.40))))
        # the run says whether it has one and how many issues are in it, so the page can offer the
        # download only where there is something to download instead of linking at a 404
        result.setdefault("summary", {})["new_report"] = {"issues": n}
        return new_report.build(result, out_dir, size_change=float(rcfg.get("new_report_size_change", 0.40)))
    except Exception:   # a second report must never cost the run its main ones
        import traceback
        traceback.print_exc()
        return None


def write_docx_report(result: dict, out_dir: str | Path) -> Path | None:
    """The Word report (genuine-issues.docx), beside the PDF report: the same issues, issues only. Built when
    python-docx is installed; without it the PDF report is still complete."""
    out = Path(out_dir)
    try:
        from . import docx_report
        return docx_report.build(result, out)
    except ImportError:
        return None


def write_content_report(result: dict, out_dir: str | Path) -> Path | None:
    """content.docx - the content review on its own: text missing from or added to stage, bold / italic on
    one side only, hyperlinks that do not work or that arrive somewhere else, and how the text is aligned.
    Nothing about pictures, tables, the TOC, fonts, colours, sizes or spacing (pdf_report.CONTENT_TYPES).

    Every issue carries its Expected (prod) and Actual (stage) text, as in the other Word report.
    Removed when a run has none of these issues, so an empty file never has to be opened."""
    out = Path(out_dir)
    path = out / "content.docx"
    try:
        from . import docx_report, pdf_report
    except ImportError:
        return None
    if not pdf_report.select_issues(result, pdf_report.CONTENT_REPORT["filter"], None):
        path.unlink(missing_ok=True)
        return None
    try:
        return docx_report.build(result, out, "content.docx", options=pdf_report.CONTENT_REPORT,
                                 title="Content report", groups=docx_report.CONTENT_GROUPS,
                                 bucket=docx_report.content_bucket, extras=False)
    except ImportError:
        return None


def write_all(result: dict, out_dir: str, shots: str = "all",
              progress: Callable[[float, str], None] | None = None, full: bool = True) -> Path:
    """shots: all | warnings | errors | reports | none — which findings get prod/stage screenshots
    (and therefore appear with images in report.pdf). progress(fraction, message) is optional.
    full=False (batch runs): only the genuine-issues and image reports are built now, with their
    screenshots; report.pdf is built by build_deferred() when first opened.

    There is no CSS report: fonts, sizes, colours and line heights are not validated as a deliverable
    of their own. CSS findings are still produced and still live in results.json and report.pdf."""
    report = progress or (lambda f, m: None)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    result["meta"]["screenshots"] = shots
    assign_bug_ids(result)  # Bug_001, Bug_002, …: before any report is built, so every report names an issue the same
    shotmod.render(result, out, shots, progress=lambda f, m: report(0.72 * f, m))
    report(0.72, "Rendering TOC pages")
    _toc_images(result, out)
    report(0.75, "Building PDF report")
    if full:
        # every issue of every severity: the screenshot setting only decides which issues get pictures
        pdf_report.build(result, out, progress=lambda f, m: report(0.75 + 0.23 * f, m))
    else:
        result["meta"]["deferred"] = list(DEFERRED)
    report(0.98, "Building PDF report (issues)")
    write_pdf_report(result, out)
    write_genuine_csv(result, out / "genuine-issues.csv")
    report(0.995, "Building image report")
    write_image_report(result, out)  # pictures only: missing numbers / labels / leader lines, overlay, pixelated
    if (result.get("site") or {}).get("rows"):
        from ..site_nav import write_csv
        write_csv(result["site"], out / "site-navigation.csv")
        from . import site_report
        site_report.build(result, out)
        # the AEM site review's own report: only missing / broken / GUID links and lists out of the
        # text margin (removed when the run has none of them)
        from . import aem_site_report
        aem_site_report.build(result, out)
        # the body hyperlinks on their own: broken links and addresses that still land on a DITA GUID,
        # for the links inside the text of the page (not the navigation / breadcrumb / pager chrome)
        from . import body_link_report
        body_link_report.build(result, out)
    report(0.997, "Building consolidated report")
    write_consolidated(result, out)  # one file per run, named after the prod PDF
    (out / "results.json").write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    (out / "junit.xml").write_text(junit(result), encoding="utf-8")
    (out / "summary.md").write_text(markdown(result), encoding="utf-8")
    for name, meta in (("baseline.pdf", result["meta"]["baseline"]), ("candidate.pdf", result["meta"]["candidate"])):
        _link_or_copy(meta["path"], out / name)
    return write_viewer(result, out)


def build_deferred(run_dir: str | Path, name: str) -> Path:
    """A report a batch run left out (DEFERRED): render the screenshots it needs, then build it."""
    out = Path(run_dir)
    result = json.loads((out / "results.json").read_text(encoding="utf-8"))
    shotmod.render(result, out, "all")
    for n in result["meta"].get("deferred", []):
        if n == "report.pdf":
            pdf_report.build(result, out)
    result["meta"]["deferred"] = []
    result["meta"]["screenshots"] = "all"
    write_consolidated(result, out)  # the deferred parts exist now: fold them into the one-file report
    (out / "results.json").write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    write_viewer(result, out)
    return out / name


def write_viewer(result: dict, out: Path) -> Path:
    data = json.dumps(result, ensure_ascii=False).replace("</", "<\\/")
    (out / "index.html").write_text(VIEWER.read_text(encoding="utf-8").replace("/*__DATA__*/", data), encoding="utf-8")
    return out / "index.html"


def write_genuine_csv(result: dict, path: Path) -> Path:
    """One row per genuine issue (opens in Excel): where it is and what is wrong."""
    from ..genuine import where
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["Bug ID", "#", "Section", "Issue", "Severity", "Prod pages", "Stage pages", "AEM topic", "GUID", "Element",
                    "Open in AEM", "Description", "Why it matters", "Prod screenshot", "Stage screenshot"])
        from .pdf_report import is_image_issue
        # in Bug ID order (Bug_001 first): the order of the PDF report
        listed = sorted(((s, f) for s in result["sections"] for f in s["findings"] if f.get("genuine") or is_image_issue(f)),
                        key=lambda sf: sf[1].get("bug") or "Bug_99999")
        for s, f in listed:
            for _ in (0,):
                if True:  # the same issues as the PDF report
                    pa, pc = where(f)
                    shots = f.get("shots") or {}
                    a = f.get("aem") or {}
                    # Excel shows the GUID as a link that opens the topic in AEM
                    guid = f'=HYPERLINK("{a["url"]}","{a["guid"]}")' if a.get("url") else a.get("guid", "")
                    w.writerow([f.get("bug", ""), f["id"], s["title"], f.get("issue") or f.get("check", ""), pdf_report.SEV_LABEL.get(f["severity"], f["severity"]).lower(), pa, pc, a.get("topic", ""), guid,
                                a.get("element", ""), a.get("url", ""), f.get("description") or f.get("message", ""),
                                f.get("why", ""), shots.get("baseline", ""), shots.get("candidate", "")])
        # the live page's CSS / layout against the design spec (site_nav): the same Bug_NNN numbers, in the
        # same file - they were in site-navigation.csv only, which a reader of the bug list never opens
        for r in (result.get("site") or {}).get("rows", []):
            if r["group"] not in ("typography", "layout") or r["status"] not in ("fail", "warn"):
                continue
            w.writerow([r.get("bug", ""), "", r.get("page", ""), r["item"], r["status"], "", "", "", "",
                        r.get("where", ""), "",
                        f'Design spec: expected {r["expected"] or "—"}, stage has {r["actual"] or "—"}'
                        + (f' — “{r["note"]}”' if r.get("note") else ""),
                        "The page must match the Figma design spec (font, size, line height, colour, spacing).",
                        "", ""])
    return path


def write_issues_csv(result: dict, issues: list[tuple], path: Path) -> Path:
    """One row per selected issue (opens in Excel), in the report's order."""
    from ..genuine import where
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["#", "Section", "Category", "Types", "Severity", "Critical", "Genuine", "Issue", "Prod pages",
                    "Stage pages", "AEM topic", "GUID", "Open in AEM", "Prod screenshot", "Stage screenshot"])
        for s, f in issues:
            pa, pc = where(f)
            shots, a = f.get("shots") or {}, f.get("aem") or {}
            w.writerow([f["id"], s["title"], f.get("category", ""), ", ".join(f.get("types") or []), pdf_report.SEV_LABEL.get(f["severity"], f["severity"]).lower(),
                        "yes" if f.get("critical") else "", "yes" if f.get("genuine") else "",
                        f.get("description") if f.get("genuine") and f.get("description") else f["message"],
                        pa, pc, a.get("topic", ""), a.get("guid", ""), a.get("url", ""),
                        shots.get("baseline", ""), shots.get("candidate", "")])
    return path


def _toc_images(result: dict, out: Path, zoom: float = 1.6) -> None:
    """Full images of every TOC page, prod and stage, for the TOC side-by-side view."""
    import pymupdf
    t = result.get("toc")
    if not t:
        return
    (out / "toc").mkdir(exist_ok=True)
    t["images"] = {}
    for side, tag in (("baseline", "prod"), ("candidate", "stage")):
        pdf = pymupdf.open(result["meta"][side]["path"])
        t["images"][side] = []
        for page in t["pages"][side]:
            rel = f"toc/{tag}_p{page}.webp"
            pix = pdf[page - 1].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            from PIL import Image
            Image.frombytes("RGB", (pix.width, pix.height), pix.samples).save(out / rel, "WEBP", quality=82)
            t["images"][side].append({"page": page, "src": rel})


def _link_or_copy(src: str, dst: Path) -> None:
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def junit(result: dict) -> str:
    """One <testcase> per section. A section FAILS on a critical/breaking issue,
    a content match below content.warn_pct, or another error-level finding;
    CSS/layout issues are reported in system-out but never fail CI on their own."""
    cases = []
    for s in result["sections"]:
        c = s["content"]
        body = ""
        if s["status"] == "fail":
            reasons = [f"[critical/{f['check']}] {f['message']}" for f in s["findings"] if f.get("critical")]
            if c["status"] == "fail":
                reasons.insert(0, f"content match {c['match_pct']:.2f}% below threshold")
            reasons += [f"[{f['check']}] {f['message']}" for f in s["findings"]
                        if f["severity"] == "error" and not f.get("critical") and f["check"] not in ("content", "style", "layout")]
            body = (f'<failure message={quoteattr(reasons[0] if reasons else "failed")}>'
                    f'{escape(chr(10).join(reasons))}</failure>')
        info = [f"content match {c['match_pct']:.2f}% ({c['status']}), {c['missing_words']} missing / {c['extra_words']} extra words",
                f"CSS/layout issues: {s['css']['issues']}"]
        info += [f"{f['severity'].upper()} [{f['check']}] {f['message']}" for f in s["findings"] if f["severity"] != "info"]
        body += "<system-out>" + escape("\n".join(info)) + "</system-out>"
        cases.append(f'  <testcase classname="pdf.parity" name={quoteattr(s["id"] + " " + s["title"])}>{body}</testcase>')
    sm = result["summary"]
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<testsuite name="pdf-parity" tests="{sm["sections"]}" '
            f'failures="{sm["fail"]}">\n' + "\n".join(cases) + "\n</testsuite>\n")


def markdown(result: dict) -> str:
    sm, meta = result["summary"], result["meta"]
    c, cr = sm["content"], sm["critical"]
    lines = [
        f"# PDF parity: {sm['result'].upper()}",
        "",
        f"- Baseline (prod): `{meta['baseline']['path']}` ({meta['baseline']['pages']} pages)",
        f"- Candidate (stage): `{meta['candidate']['path']}` ({meta['candidate']['pages']} pages)",
        f"- Sections: {sm['sections']} (pass {sm['pass']}, warn {sm['warn']}, fail {sm['fail']})",
        f"- **Content match: {c['match_pct']:.2f}%** (text, punctuation, spacing; pass ≥ {c['pass_pct']}%, "
        f"warn ≥ {c['warn_pct']}%) — {c['missing_words']} missing, {c['extra_words']} extra words, "
        f"{c['spacing_issues']} spacing issues",
        f"- **Critical / breaking: {cr['total']}** " + ", ".join(f"{v} × {k}" for k, v in cr["by_kind"].items()),
        f"- CSS / layout (separate): {sm['css']['issues']} issues (style {sm['css']['style']}, layout {sm['css']['layout']})",
        "",
    ]
    crit = [(s, f) for s in result["sections"] for f in s["findings"] if f.get("critical")]
    if crit:
        lines += ["## Critical issues", "", "| Section | Check | Issue |", "|---|---|---|"]
        lines += [f"| {s['title']} | {f['check']} | {f['message']} |" for s, f in crit]
        lines.append("")
    if result.get("aem"):
        a = result["aem"]
        lines += [f"## AEM topics with issues (map `{a['map']}`)", "",
                  "| Topic | GUID | Genuine | Critical | All issues |", "|---|---|---:|---:|---:|"]
        lines += [f"| {t['topic']} | " + (f"[{t['guid']}]({t['url']})" if t["url"] else f"`{t['guid']}`")
                  + f" | {t['genuine']} | {t['critical']} | {t['issues']} |" for t in a["topics"]]
        lines.append("")
    site = result.get("site") or {}
    if site.get("rows"):
        from ..site_nav import GROUPS
        ss = site["summary"]
        lines += [f"## Site navigation: {ss['status'].upper()} ({ss['fail']} fail, {ss['warn']} warn, {ss['pass']} pass, "
                  f"{ss['pages']} pages)", "", "| Check | Pass | Warn | Fail |", "|---|---:|---:|---:|"]
        lines += [f"| {t} | {ss['groups'][g]['pass']} | {ss['groups'][g]['warn']} | {ss['groups'][g]['fail']} |" for g, t in GROUPS]
        bad = [r for r in site["rows"] if r["status"] in ("fail", "warn")]
        if bad:
            titles = dict(GROUPS)
            cell = lambda x: str(x).replace("|", "\\|")
            lines += ["", "| Check | Status | Item | Expected | Actual | Page | Note |", "|---|---|---|---|---|---|---|"]
            lines += [f"| {titles[r['group']]} | {r['status']} | {cell(r['item'])} | {cell(r['expected'])} | {cell(r['actual'])} | "
                      f"{r['page']} | {cell(r['note'])} |" for r in bad]
        lines.append("")
    elif site.get("error"):
        lines += ["## Site navigation", "", f"Not checked: {site['error']}", ""]
    lines += ["## Sections", "",
              "| Section | Status | Content % | Missing / extra | Critical |",
              "|---|---|---:|---:|---:|"]
    for s in result["sections"]:
        lines.append(f"| {s['title']} | {s['status']} | {s['content']['match_pct']:.2f}% ({s['content']['status']}) | "
                     f"{s['content']['missing_words']} / {s['content']['extra_words']} | {s['critical']} |")
    lines += ["", "## Global style map (prod → stage)", "", "| Role | Prod | Stage | Words | Sections |", "|---|---|---|---:|---:|"]
    for m in result["style_map"]:
        lines.append(f"| {m['role']} | {m['baseline']} | {m['candidate']} | {m['words']} | {m['sections']} |")
    return "\n".join(lines) + "\n"
