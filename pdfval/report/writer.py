"""Write results.json, junit.xml, summary.md, report.pdf (issues + screenshots) and the interactive viewer."""
from __future__ import annotations

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
    pdf_report.build(result, out, severities=shotmod.SEVERITIES.get(shots) or None,
                     progress=lambda f, m: report(0.75 + 0.23 * f, m))
    (out / "results.json").write_text(json.dumps(result, indent=1, ensure_ascii=False))
    (out / "junit.xml").write_text(junit(result))
    (out / "summary.md").write_text(markdown(result))
    for name, meta in (("baseline.pdf", result["meta"]["baseline"]), ("candidate.pdf", result["meta"]["candidate"])):
        _link_or_copy(meta["path"], out / name)
    data = json.dumps(result, ensure_ascii=False).replace("</", "<\\/")
    (out / "index.html").write_text(VIEWER.read_text().replace("/*__DATA__*/", data))
    return out / "index.html"


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
