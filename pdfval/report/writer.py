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


def write_all(result: dict, out_dir: str, shots: str = "all",
              progress: Callable[[float, str], None] | None = None) -> Path:
    """shots: all | warnings | errors | none — which findings get prod/stage screenshots
    (and therefore appear with images in report.pdf). progress(fraction, message) is optional."""
    report = progress or (lambda f, m: None)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    result["meta"]["screenshots"] = shots
    shotmod.render(result, out, shots, progress=lambda f, m: report(0.72 * f, m))
    report(0.72, "Rendering TOC pages")
    _toc_images(result, out)
    report(0.75, "Building PDF report")
    # every issue of every severity: the screenshot setting only decides which issues get pictures
    pdf_report.build(result, out, progress=lambda f, m: report(0.75 + 0.23 * f, m))
    report(0.98, "Building PDF report (issues)")
    pdf_report.build(result, out, options=pdf_report.GENUINE, filename="genuine-issues.pdf")
    write_genuine_csv(result, out / "genuine-issues.csv")
    report(0.99, "Building CSS report")
    pdf_report.build(result, out, options=pdf_report.CSS_REPORT, filename="css-issues.pdf")
    report(0.995, "Building image report")
    pdf_report.build(result, out, options=pdf_report.IMAGE_REPORT, filename="image-issues.pdf")
    if (result.get("site") or {}).get("rows"):
        from ..site_nav import write_csv
        write_csv(result["site"], out / "site-navigation.csv")
    (out / "results.json").write_text(json.dumps(result, indent=1, ensure_ascii=False))
    (out / "junit.xml").write_text(junit(result))
    (out / "summary.md").write_text(markdown(result))
    for name, meta in (("baseline.pdf", result["meta"]["baseline"]), ("candidate.pdf", result["meta"]["candidate"])):
        _link_or_copy(meta["path"], out / name)
    return write_viewer(result, out)


def write_viewer(result: dict, out: Path) -> Path:
    data = json.dumps(result, ensure_ascii=False).replace("</", "<\\/")
    (out / "index.html").write_text(VIEWER.read_text().replace("/*__DATA__*/", data))
    return out / "index.html"


def write_genuine_csv(result: dict, path: Path) -> Path:
    """One row per genuine issue (opens in Excel): where it is and what is wrong."""
    from ..genuine import where
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["#", "Section", "Issue", "Severity", "Prod pages", "Stage pages", "AEM topic", "GUID", "Element",
                    "Open in AEM", "Description", "Why it matters", "Prod screenshot", "Stage screenshot"])
        for s in result["sections"]:
            for f in s["findings"]:
                if f.get("genuine"):
                    pa, pc = where(f)
                    shots = f.get("shots") or {}
                    a = f.get("aem") or {}
                    # Excel shows the GUID as a link that opens the topic in AEM
                    guid = f'=HYPERLINK("{a["url"]}","{a["guid"]}")' if a.get("url") else a.get("guid", "")
                    w.writerow([f["id"], s["title"], f["issue"], f["severity"], pa, pc, a.get("topic", ""), guid,
                                a.get("element", ""), a.get("url", ""), f["description"],
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
            w.writerow([f["id"], s["title"], f.get("category", ""), ", ".join(f.get("types") or []), f["severity"],
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
