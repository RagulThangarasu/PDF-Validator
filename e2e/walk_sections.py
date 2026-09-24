"""End-to-end walker: opens the report viewer in Chromium, scrolls BOTH PDFs to
every section, asserts the section start is on screen in each pane, records the
engine's verdict and captures a side-by-side screenshot per section.

  python e2e/walk_sections.py reports/latest                 # all sections
  python e2e/walk_sections.py reports/latest --only "Mount"  # regex on title
  python e2e/walk_sections.py reports/latest --headed --slow 800

Outputs <report>/e2e/: one PNG per section + e2e_results.json. Exit code is 1
when any walked section failed validation or could not be scrolled into view.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pdfval.cli import serve  # noqa: E402


def walk(report_dir: str, only: str | None = None, headed: bool = False, slow: int = 0,
         focus_findings: int = 0, width: int = 1800, height: int = 1000) -> dict:
    report = Path(report_dir)
    shots = report / "e2e"
    shots.mkdir(exist_ok=True)
    httpd = serve(str(report), port=0, open_browser=False)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/index.html"
    results = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not headed)
            page = browser.new_page(viewport={"width": width, "height": height})
            page.goto(url)
            page.wait_for_function("window.pdfval && window.pdfval.ready", timeout=180_000)
            sections = page.evaluate("pdfval.sections()")
            rx = re.compile(only, re.I) if only else None
            for s in sections:
                if rx and not rx.search(s["title"]):
                    continue
                r = page.evaluate("id => pdfval.goto(id)", s["id"])
                page.wait_for_timeout(250 + slow)  # let canvases paint
                shot = shots / f"{s['id']}.png"
                page.locator("main").screenshot(path=str(shot))
                extra = []
                for k in range(focus_findings):  # optionally step into the first N findings
                    if page.evaluate(f"pdfval.data.sections.find(x => x.id === '{s['id']}').findings.length") <= k:
                        break
                    page.evaluate(f"pdfval.focusFinding({k})")
                    page.wait_for_timeout(600 + slow)
                    fshot = shots / f"{s['id']}__finding{k}.png"
                    page.locator("main").screenshot(path=str(fshot))
                    extra.append(fshot.name)
                scrolled = r["baselineVisible"] and r["candidateVisible"]
                results.append({**r, "title": s["title"], "scrolled": scrolled, "screenshot": shot.name, "finding_shots": extra,
                                "ok": scrolled and r["status"] != "fail"})
                print(f"{'OK  ' if results[-1]['ok'] else 'FAIL'} {r['status']:4} scroll={'y' if scrolled else 'N'} "
                      f"findings={r['findings']:<3} {s['title']}")
            browser.close()
    finally:
        httpd.shutdown()
    out = {"walked": len(results), "ok": sum(r["ok"] for r in results),
           "scroll_failures": [r["id"] for r in results if not r["scrolled"]], "sections": results}
    (shots / "e2e_results.json").write_text(json.dumps(out, indent=1))
    print(f"\n{out['ok']}/{out['walked']} sections OK · scroll failures: {len(out['scroll_failures'])} · screenshots in {shots}")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("report_dir")
    ap.add_argument("--only")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--slow", type=int, default=0, help="extra ms to pause on each section")
    ap.add_argument("--focus-findings", type=int, default=0, help="also scroll to and screenshot the first N findings")
    a = ap.parse_args()
    res = walk(a.report_dir, a.only, a.headed, a.slow, a.focus_findings)
    sys.exit(0 if res["ok"] == res["walked"] else 1)
