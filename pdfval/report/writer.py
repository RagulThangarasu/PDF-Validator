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
DEFERRED = ("report.pdf", "css-issues.pdf")


def image_summary(result: dict) -> dict:
    """{"issues": n, "result": "pass" | "fail"} of the image report: PASS = no image issue."""
    from . import image_report
    n = len(image_report.issues(result))
    return {"issues": n, "result": "fail" if n else "pass"}


def write_image_report(result: dict, out_dir: str | Path) -> Path | None:
    """The image report (image-issues.pdf) - only when the publication has image issues (FAIL): a PASS needs no
    report, and one left from an earlier run is removed. The verdict goes into the summary (consolidated report)."""
    from . import image_report
    out = Path(out_dir)
    result.setdefault("summary", {})["images"] = sm = image_summary(result)
    if sm["result"] == "pass":
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
    if not (result.get("summary") or {}).get("genuine", {}).get("total"):
        (out / "genuine-issues.pdf").unlink(missing_ok=True)
        (out / "genuine-issues.docx").unlink(missing_ok=True)
        return None
    path = pdf_report.build(result, out, options=pdf_report.GENUINE, filename="genuine-issues.pdf")
    write_docx_report(result, out)
    return path


def write_docx_report(result: dict, out_dir: str | Path) -> Path | None:
    """The Word report (genuine-issues.docx), beside the PDF report: the same issues, issues only. Built when
    python-docx is installed; without it the PDF report is still complete."""
    out = Path(out_dir)
    try:
        from . import docx_report
        return docx_report.build(result, out)
    except ImportError:
        return None


def write_all(result: dict, out_dir: str, shots: str = "all",
              progress: Callable[[float, str], None] | None = None, full: bool = True) -> Path:
    """shots: all | warnings | errors | reports | none — which findings get prod/stage screenshots
    (and therefore appear with images in report.pdf). progress(fraction, message) is optional.
    full=False (batch runs): only the genuine-issues and image reports are built now, with their
    screenshots; report.pdf and css-issues.pdf are built by build_deferred() when first opened."""
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
    if full:
        report(0.99, "Building CSS report")
        pdf_report.build(result, out, options=pdf_report.CSS_REPORT, filename="css-issues.pdf")
    report(0.995, "Building image report")
    write_image_report(result, out)  # pictures only: missing numbers / labels / leader lines, overlay, pixelated
    if (result.get("site") or {}).get("rows"):
        from ..site_nav import write_csv
        write_csv(result["site"], out / "site-navigation.csv")
        from . import site_report
        site_report.build(result, out)
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
        elif n == "css-issues.pdf":
            pdf_report.build(result, out, options=pdf_report.CSS_REPORT, filename="css-issues.pdf")
    result["meta"]["deferred"] = []
    result["meta"]["screenshots"] = "all"
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
              "| Section | Status | Content % | Missing / extra | Critical | CSS issues |",
              "|---|---|---:|---:|---:|---:|"]
    for s in result["sections"]:
        lines.append(f"| {s['title']} | {s['status']} | {s['content']['match_pct']:.2f}% ({s['content']['status']}) | "
                     f"{s['content']['missing_words']} / {s['content']['extra_words']} | {s['critical']} | {s['css']['issues']} |")
    lines += ["", "## Global style map (prod → stage)", "", "| Role | Prod | Stage | Words | Sections |", "|---|---|---|---:|---:|"]
    for m in result["style_map"]:
        lines.append(f"| {m['role']} | {m['baseline']} | {m['candidate']} | {m['words']} | {m['sections']} |")
    return "\n".join(lines) + "\n"
