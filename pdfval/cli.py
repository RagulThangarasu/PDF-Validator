"""Command line entry point.

  python -m pdfval compare --baseline prod.pdf --candidate stage.pdf --out reports/run1
  python -m pdfval serve reports/run1            # open the interactive viewer
  python -m pdfval ui                            # web UI: pick PDFs, run, browse issues + screenshots
"""
from __future__ import annotations

import argparse
import functools
import http.server
import sys
import threading
import webbrowser
from pathlib import Path

from . import engine
from .report import writer


def serve(directory: str, port: int = 8765, open_browser: bool = True) -> http.server.ThreadingHTTPServer:
    handler = functools.partial(_QuietHandler, directory=str(Path(directory).resolve()))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/index.html"
    print(f"Serving {directory} at {url}")
    if open_browser:
        webbrowser.open(url)
    return httpd


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pdfval", description="Section-by-section PDF parity validation")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("compare", help="compare candidate PDF against baseline PDF")
    c.add_argument("--baseline", required=True, help="reference PDF (prod)")
    c.add_argument("--candidate", required=True, help="PDF under test (stage)")
    c.add_argument("--config", help="TOML overrides merged over config/default.toml")
    c.add_argument("--out", default="reports/latest", help="report directory")
    c.add_argument("--only", help="regex: validate only sections whose title matches")
    c.add_argument("--name", help="product / publication name shown in the reports (default: the stage file name)")
    c.add_argument("--fail-on", choices=["error", "warning", "genuine", "never"], default="error",
                   help="exit non-zero when a finding of this severity exists")
    c.add_argument("--screenshots", choices=["all", "warnings", "errors", "none"], default="all",
                   help="which issues get prod/stage screenshots")
    c.add_argument("--open", action="store_true", help="serve and open the viewer afterwards")

    s = sub.add_parser("serve", help="serve a report directory and open the viewer")
    s.add_argument("dir")
    s.add_argument("--port", type=int, default=8765)

    u = sub.add_parser("ui", help="start the web UI (upload/pick PDFs, run, browse issues with screenshots)")
    u.add_argument("--port", type=int, default=8700)
    u.add_argument("--root", help="folder whose PDFs are offered for picking (default: parent of this project)")
    u.add_argument("--runs", help="where runs are stored (default: <project>/runs)")
    u.add_argument("--no-browser", action="store_true")

    x = sub.add_parser("source", help="export a PDF's source: raw data + rebuilt AEM Guides DITA (zip)")
    x.add_argument("pdf")
    x.add_argument("--out", help="zip file (default: <pdf name>-source.zip next to the PDF)")
    x.add_argument("--config", help="TOML overrides merged over config/default.toml")

    md = sub.add_parser("metadata", help="check Document Title / Page Title of every product map in AEM against the sheet")
    md.add_argument("--sheet", help="migration sheet .xlsx (default: newest in <project>/metadata)")
    md.add_argument("--out", default="reports/metadata", help="report directory")
    md.add_argument("--lang", default="en", help="language folder of the maps in AEM")
    md.add_argument("--config", help="TOML overrides merged over config/default.toml")

    cl = sub.add_parser("classes", help="list the classes (outputclass) used in every topic of a map in AEM")
    cl.add_argument("map", help="the map's path in AEM (…/Maps/<map>.ditamap) or its XML-editor URL")
    cl.add_argument("--out", default="reports/classes", help="folder for the CSV lists")
    cl.add_argument("--config", help="TOML overrides merged over config/default.toml")

    dp = sub.add_parser("aem-pdfs", help="download the PDF of every product map in AEM, one folder per product")
    dp.add_argument("--out", default="aem-map-pdfs", help="download folder")
    dp.add_argument("--lang", help="only this language folder (default: all languages)")
    dp.add_argument("--all-versions", action="store_true", help="also every other PDF generated from each map")
    dp.add_argument("--generate", action="store_true",
                    help="generate a NEW PDF of every map in AEM with its brand's preset (Education: BenQ EDU With Image, "
                         "Consumer/Business: BenQ With Image, ZOWIE: Zowie, INFTYLAB: INFTY; Arabic maps: '<preset> Arabic') "
                         "and download it")
    dp.add_argument("--parallel", type=int, default=3, help="maps generated at the same time (--generate)")
    dp.add_argument("--config", help="TOML overrides merged over config/default.toml")

    cb = sub.add_parser("combined", help="one report with the PDF validation and the AEM site validation of every "
                                         "product side by side (the newest run of each kind)")
    cb.add_argument("--runs", default="runs", help="the runs folder to read (default: runs)")
    cb.add_argument("--out", default="", help="where to write it (default: <runs>/_combined)")

    args = ap.parse_args(argv)
    if args.cmd == "combined":
        from .report import combined_report
        pdf, csv_ = combined_report.build(args.runs, args.out or (Path(args.runs) / "_combined"))
        ps = combined_report.pairs(args.runs)
        t = combined_report.totals(ps)
        print(f"{t['products']} product(s), {t['both']} validated both ways "
              f"(PDF: {t['pdf']['runs']} run(s), site: {t['site']['runs']} run(s))")
        print(f"report: {pdf}\n   csv: {csv_}")
        return 0
    if args.cmd == "classes":
        import os
        from . import aem, aem_classes
        from .app.server import _keychain_get
        acfg = aem.merge_settings(engine.load_config(args.config).get("aem", {}), _saved_aem_settings())
        acfg["password"] = os.environ.get("PDFVAL_AEM_PASSWORD") or _keychain_get(acfg.get("user", ""))
        res = aem_classes.crawl(acfg, args.map)
        path = aem_classes.write(res, args.out)
        print(f"{res['map']}\n{res['topics_read']} of {res['topics_in_map']} topics read"
              + (f" ({len(res['topics_missing'])} not found)" if res["topics_missing"] else "")
              + (f" ({len(res['topics_failed'])} could not be read)" if res["topics_failed"] else "") + f" · {len(res['classes'])} classes\n")
        print(f"{'class':<34}{'uses':>7}{'topics':>8}  on elements")
        for c in res["classes"]:
            print(f"{c['class']:<34}{c['uses']:>7}{c['topics']:>8}  {c['elements']}")
        print(f"\nlists: {path}  (+ -combinations.csv, -elements.csv)\nWord:  {str(path)[:-4]}.docx")
        return 0
    if args.cmd == "aem-pdfs":
        import os
        from . import aem, aem_pdfs
        from .app.server import _keychain_get
        acfg = aem.merge_settings(engine.load_config(args.config).get("aem", {}), _saved_aem_settings())
        acfg["password"] = os.environ.get("PDFVAL_AEM_PASSWORD") or _keychain_get(acfg.get("user", ""))
        if args.generate:
            recs = aem_pdfs.generate(acfg, args.out, args.lang, parallel=args.parallel)
            bad = [r for r in recs if not r["files"]]
            print(f"{len(recs)} maps: {len(recs) - len(bad)} new PDF(s) generated and saved to {args.out}/ · {len(bad)} failed")
            for r in bad:
                print(f"  failed: {r['map']}  [{r['preset']}]  {r['error']}")
            return 1 if bad else 0
        recs = aem_pdfs.run(acfg, args.out, args.lang, args.all_versions)
        n = sum(len(r["files"]) for r in recs)
        bad = [r for r in recs if not r["files"]]
        print(f"{len(recs)} maps: {n} PDF(s) saved to {args.out}/ · {len(bad)} map(s) without a PDF")
        for r in bad:
            print(f"  no PDF: {r['map']}  ({r['error']})")
        return 0
    if args.cmd == "metadata":
        import os
        from . import aem, metadata
        from .app.server import _keychain_get
        acfg = aem.merge_settings(engine.load_config(args.config).get("aem", {}), _saved_aem_settings())
        acfg["password"] = os.environ.get("PDFVAL_AEM_PASSWORD") or _keychain_get(acfg.get("user", ""))
        res = metadata.run(acfg, args.out, args.sheet, args.lang, progress=lambda f, m: print(f"{int(f * 100):3d}%  {m}"))
        sm = res["excel"]
        print(f"{sm['rows']} Excel rows: {sm['pass']} pass, {sm['fail']} mismatch, {sm['missing']} empty in AEM, "
              f"{sm['case']} case/spacing only, {sm['not_in_aem']} no map in AEM")
        print(f"report: {Path(args.out) / 'metadata-report.pdf'}  ·  {Path(args.out) / 'metadata-report.csv'}")
        return 1 if sm["fail"] or sm["missing"] or sm["case"] else 0
    if args.cmd == "source":
        from . import source_export
        out = args.out or str(Path(args.pdf).with_name(Path(args.pdf).stem + "-source.zip"))
        path = source_export.export(args.pdf, out, engine.load_config(args.config), label=Path(args.pdf).stem,
                                    progress=lambda f, m: print(f"{int(f * 100):3d}%  {m}"))
        print(f"source: {path}")
        return 0
    if args.cmd == "ui":
        from .app import server
        server.run(args.port, args.root, args.runs, open_browser=not args.no_browser)
        return 0
    if args.cmd == "serve":
        serve(args.dir, args.port)
        _block()
        return 0

    cfg = engine.load_config(args.config)
    result = engine.compare(args.baseline, args.candidate, cfg, only=args.only)
    result["meta"]["name"] = args.name or Path(args.candidate).stem
    index = writer.write_all(result, args.out, args.screenshots)
    sm = result["summary"]
    print(f"{sm['result'].upper()}: {sm['sections']} sections — {sm['fail']} fail, {sm['warn']} warn, {sm['pass']} pass")
    print("findings: " + ", ".join(f"{k}={v}" for k, v in sm["by_check"].items()))
    gen = sm["genuine"]
    print(f"genuine issues: {gen['total']}" + (" (" + ", ".join(f"{v} × {k}" for k, v in gen["by_issue"].items()) + ")"
                                               if gen["total"] else ""))
    print(f"report:   {index}")
    print(f"genuine:  {Path(args.out) / 'genuine-issues.pdf'}  ·  {Path(args.out) / 'genuine-issues.docx'}  ·  {Path(args.out) / 'genuine-issues.csv'}")
    if args.open:
        serve(args.out)
        _block()
    if args.fail_on == "never":
        return 0
    if args.fail_on == "genuine":
        return 1 if gen["total"] else 0
    bad = {"error": ("fail",), "warning": ("fail", "warn")}[args.fail_on]
    return 1 if any(x["status"] in bad for x in result["sections"]) else 0


def _saved_aem_settings() -> dict:
    """AEM user / author / products saved from the web UI."""
    import json
    try:
        return json.loads((Path(__file__).resolve().parents[1] / "runs" / "aem-settings.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _block() -> None:
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
