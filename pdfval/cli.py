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

    args = ap.parse_args(argv)
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
    index = writer.write_all(result, args.out, args.screenshots)
    sm = result["summary"]
    print(f"{sm['result'].upper()}: {sm['sections']} sections — {sm['fail']} fail, {sm['warn']} warn, {sm['pass']} pass")
    print("findings: " + ", ".join(f"{k}={v}" for k, v in sm["by_check"].items()))
    gen = sm["genuine"]
    print(f"genuine issues: {gen['total']}" + (" (" + ", ".join(f"{v} × {k}" for k, v in gen["by_issue"].items()) + ")"
                                               if gen["total"] else ""))
    print(f"report:   {index}")
    print(f"genuine:  {Path(args.out) / 'genuine-issues.pdf'}  ·  {Path(args.out) / 'genuine-issues.csv'}")
    if args.open:
        serve(args.out)
        _block()
    if args.fail_on == "never":
        return 0
    if args.fail_on == "genuine":
        return 1 if gen["total"] else 0
    bad = {"error": ("fail",), "warning": ("fail", "warn")}[args.fail_on]
    return 1 if any(x["status"] in bad for x in result["sections"]) else 0


def _block() -> None:
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
