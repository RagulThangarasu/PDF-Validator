"""Local web UI: pick/upload prod + stage PDFs, run the comparison in the
background with live progress, browse issues with prod/stage screenshots,
open the side-by-side viewer and download the PDF report.

  python -m pdfval ui [--port 8700] [--root DIR]

Standard library only (http.server + threads). Binds to 127.0.0.1.
"""
from __future__ import annotations

import json
import os
import mimetypes
import re
import shutil
import threading
import traceback
import uuid
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .. import aem, engine
from ..report import writer

STATIC = Path(__file__).with_name("static")
PROJECT = Path(__file__).resolve().parents[2]
mimetypes.add_type("image/webp", ".webp")


class Jobs:
    """Runs live in <runs>/<id>/ with a job.json; one comparison runs at a time."""

    def __init__(self, runs_dir: Path):
        self.dir = runs_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "_uploads").mkdir(exist_ok=True)
        self.lock = threading.Lock()
        self.run_lock = threading.Lock()
        # web-page passwords live in memory only (never in job.json), per (site, user),
        # so Rerun works until the server restarts
        self.passwords: dict[tuple[str, str], str] = {}
        self.aem_password = os.environ.get("PDFVAL_AEM_PASSWORD", "")  # AEM login: memory only, never on disk
        for job in self.list():  # server restarted mid-run
            if job["status"] in ("queued", "running"):
                self.update(job["id"], status="error", message="Interrupted (server restarted)")

    def path(self, jid: str) -> Path:
        if not re.fullmatch(r"[\w-]+", jid):
            raise KeyError(jid)
        return self.dir / jid

    def get(self, jid: str) -> dict:
        return json.loads((self.path(jid) / "job.json").read_text())

    def update(self, jid: str, **kw) -> dict:
        with self.lock:
            job = self.get(jid)
            job.update(kw)
            (self.path(jid) / "job.json").write_text(json.dumps(job, indent=1))
            return job

    def list(self) -> list[dict]:
        jobs = []
        for p in self.dir.glob("*/job.json"):
            try:
                jobs.append(json.loads(p.read_text()))
            except (OSError, json.JSONDecodeError):
                pass
        return sorted(jobs, key=lambda j: j["created"], reverse=True)

    def create(self, baseline: str, candidate: str, name: str, options: dict) -> dict:
        """candidate is a PDF path, or a URL when options['mode'] == 'html'."""
        if not Path(baseline).is_file():
            raise ValueError(f"File not found: {baseline}")
        if options.get("mode") == "html":
            if not re.match(r"^https?://\S+$", candidate or ""):
                raise ValueError("Enter a full web address starting with http:// or https://")
            from ..html_source import split_login
            candidate, url_user, url_password = split_login(candidate)  # never store a login in the URL
            options = {**options, "html_user": options.get("html_user") or url_user}
            password = options.pop("html_password", "") or url_password
            if options["html_user"] and password:
                self.passwords[(urlparse(candidate).netloc, options["html_user"])] = password
        elif not Path(candidate).is_file():
            raise ValueError(f"File not found: {candidate}")
        jid = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        self.path(jid).mkdir()
        job = {"id": jid, "name": name or f"{Path(baseline).stem} vs {Path(candidate).stem}",
               "created": datetime.now().isoformat(timespec="seconds"), "baseline": baseline, "candidate": candidate,
               "options": options, "mode": options.get("mode", "pdf"), "status": "queued", "progress": 0.0,
               "message": "Queued", "summary": None}
        (self.path(jid) / "job.json").write_text(json.dumps(job, indent=1))
        threading.Thread(target=self._run, args=(jid,), daemon=True).start()
        return job

    def aem_settings(self) -> dict:
        """AEM link settings saved from the UI (author URL, link template, DAM folder per ditamap)."""
        try:
            return json.loads((self.dir / "aem-settings.json").read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def aem_config(self) -> dict:
        """[aem] config + saved settings + the in-memory password."""
        return {**aem.merge_settings(engine.load_config().get("aem", {}), self.aem_settings()),
                "password": self.aem_password}

    def save_aem_settings(self, req: dict) -> dict:
        if req.get("password"):
            self.aem_password = str(req["password"])
        keep = {k: str(req[k]).strip() for k in ("author", "link", "dam_root", "user", "search_root") if k in req}
        keep["products"] = {str(k).strip(): str(v).strip() for k, v in (req.get("products") or {}).items()
                            if str(k).strip() and str(v).strip()}
        (self.dir / "aem-settings.json").write_text(json.dumps(keep, indent=1))
        return keep

    def aem_login(self, req: dict) -> dict:
        """Check an AEM user/password against AEM; keep them (password in memory only) when accepted."""
        cfg = {**self.aem_config(), "user": str(req.get("user", "")).strip(),
               "password": str(req.get("password") or "") or self.aem_password}
        ok, msg = aem.check_login(cfg)
        if ok:
            self.aem_password = cfg["password"]
            self.save_aem_settings({**self.aem_settings(), "user": cfg["user"]})
        return {"ok": ok, "message": msg, "user": cfg["user"]}

    def relink(self, jid: str) -> dict:
        """Apply the current AEM settings to a finished run: results.json, the CSV and the genuine-issues PDF."""
        from ..report import pdf_report
        run_dir = self.path(jid)
        result = json.loads((run_dir / "results.json").read_text())
        acfg = self.aem_config()
        if not result.get("aem") and Path(result["meta"]["candidate"].get("path", "")).is_file():
            aem.annotate(result, {"aem": acfg})  # a run made before GUIDs were traced
        if aem.relink(result, acfg):
            (run_dir / "results.json").write_text(json.dumps(result, indent=1, ensure_ascii=False))
            writer.write_genuine_csv(result, run_dir / "genuine-issues.csv")
            writer.write_viewer(result, run_dir)
            pdf_report.build(result, run_dir, options=pdf_report.GENUINE, filename="genuine-issues.pdf")
            if (run_dir / "report.pdf").exists():
                from ..report import shots as shotmod
                sev = shotmod.SEVERITIES.get(result["meta"].get("screenshots", "all")) or None
                pdf_report.build(result, run_dir, severities=sev)
        return result.get("aem") or {}

    def delete(self, jid: str) -> None:
        shutil.rmtree(self.path(jid), ignore_errors=True)

    def _run(self, jid: str) -> None:
        with self.run_lock:
            job = self.update(jid, status="running", message="Starting")
            try:
                if job["options"].get("mode") != "html" and _sha256(job["baseline"]) == _sha256(job["candidate"]):
                    job = self.update(jid, warning="Prod and stage are the same file (identical SHA-256): there is nothing "
                                                   "to compare, so the result is 100 % by definition. Pick the other version "
                                                   "of the document as the candidate.")
                cfg = engine.load_config()
                _apply_options(cfg, job["options"])
                cfg["aem"] = {**aem.merge_settings(cfg.get("aem", {}), self.aem_settings()), "password": self.aem_password}
                o = job["options"]
                step = lambda f, m: self.update(jid, progress=round(0.45 * f, 3), message=m)
                if o.get("mode") == "html":
                    result = engine.compare_url(job["baseline"], job["candidate"], str(self.path(jid)), cfg, progress=step,
                                                html={"root": o.get("html_root", ""), "exclude": o.get("html_exclude", ""),
                                                      "user": o.get("html_user", ""),
                                                      "crawl": o.get("html_crawl", True),
                                                      "max_pages": o.get("html_max_pages") or 0,
                                                      "password": self.passwords.get(
                                                          (urlparse(job["candidate"]).netloc, o.get("html_user", "")), ""),
                                                      "width": o.get("html_width") or 1280, "wait_ms": o.get("html_wait") or 1500})
                else:
                    result = engine.compare(job["baseline"], job["candidate"], cfg, progress=step)
                writer.write_all(result, str(self.path(jid)), job["options"].get("screenshots", "all"),
                                 progress=lambda f, m: self.update(jid, progress=round(0.45 + 0.55 * f, 3), message=m))
                self.update(jid, status="done", progress=1.0, message="Done", summary=result["summary"])
            except Exception as e:  # surface the failure in the UI
                traceback.print_exc()
                self.update(jid, status="error", message=f"{type(e).__name__}: {e}")


def _sha256(path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _apply_options(cfg: dict, o: dict) -> None:
    """Map the UI's option form onto the engine config."""
    num = lambda k: o.get(k) not in (None, "")
    if num("color_tolerance"):
        cfg["style"]["color_tolerance"] = float(o["color_tolerance"])
    if num("size_tolerance"):
        cfg["style"]["size_tolerance"] = float(o["size_tolerance"])
    if num("indent_tolerance"):
        cfg["layout"]["indent_tolerance"] = float(o["indent_tolerance"])
    for k in ("pass_pct", "warn_pct"):
        if num(k):
            cfg["content"][k] = float(o[k])
    if num("critical_missing_words"):
        cfg["content"]["critical_missing_words"] = int(o["critical_missing_words"])
    for k in ("check_spacing", "normalize_typography"):
        if k in o:
            cfg["content"][k] = bool(o[k])
    if "case_sensitive" in o:
        cfg["content"]["case_sensitive"] = bool(o["case_sensitive"])
    # the cover (front matter) is never validated from the UI: [sections] front_matter in the config
    if o.get("skip"):
        cfg["sections"]["skip"] = [s.strip() for s in o["skip"].splitlines() if s.strip()]
    if o.get("ignore"):
        cfg["extract"]["ignore_patterns"] += [s.strip() for s in o["ignore"].splitlines() if s.strip()]
    for prop in ("color", "font-family", "font-size", "font-weight"):
        if o.get(f"sev_{prop}"):
            cfg["style"]["severity"][prop] = o[f"sev_{prop}"]


def make_handler(jobs: Jobs, root: Path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        # -- helpers
        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _file(self, path: Path, download: str | None = None):
            if not path.is_file():
                return self._json({"error": "not found"}, 404)
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            size = path.stat().st_size
            rng = self.headers.get("Range")
            start, end = 0, size - 1
            if rng and (m := re.match(r"bytes=(\d*)-(\d*)", rng)):  # PDF.js range requests
                start = int(m[1]) if m[1] else max(0, size - int(m[2]))
                end = int(m[2]) if m[1] and m[2] else size - 1
                self.send_response(HTTPStatus.PARTIAL_CONTENT)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if download:
                self.send_header("Content-Disposition", f'attachment; filename="{download}"')
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)

        def _body(self) -> bytes:
            return self.rfile.read(int(self.headers.get("Content-Length", 0)))

        # -- routes
        def do_GET(self):
            u = urlparse(self.path)
            p = unquote(u.path)
            try:
                if p in ("/", "/index.html"):
                    return self._file(STATIC / "index.html")
                if p == "/api/runs":
                    return self._json(jobs.list())
                if m := re.fullmatch(r"/api/runs/([\w-]+)", p):
                    return self._json(jobs.get(m[1]))
                if p == "/api/aem":
                    c = jobs.aem_config()
                    return self._json({**{k: v for k, v in c.items() if k != "password"}, "has_password": bool(c["password"])})
                if p == "/api/files":
                    return self._json(_list_pdfs(root, jobs.dir))
                if m := re.fullmatch(r"/runs/([\w-]+)/(.+)", p):
                    base = jobs.path(m[1]).resolve()
                    target = (base / m[2]).resolve()
                    if not target.is_relative_to(base):
                        return self._json({"error": "forbidden"}, 403)
                    dl = parse_qs(u.query).get("download", [None])[0]
                    return self._file(target, download=dl)
                return self._json({"error": "not found"}, 404)
            except (KeyError, FileNotFoundError):
                return self._json({"error": "not found"}, 404)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            p = urlparse(self.path)
            try:
                if p.path == "/api/upload":
                    name = Path(parse_qs(p.query).get("name", ["upload.pdf"])[0]).name
                    data = self._body()
                    if not data.startswith(b"%PDF"):
                        return self._json({"error": f"{name} is not a PDF"}, 400)
                    dest = jobs.dir / "_uploads" / f"{uuid.uuid4().hex[:8]}_{name}"
                    dest.write_bytes(data)
                    return self._json({"path": str(dest), "name": name, "size": len(data)})
                if p.path == "/api/aem":
                    return self._json(jobs.save_aem_settings(json.loads(self._body() or b"{}")))
                if p.path == "/api/aem/login":
                    return self._json(jobs.aem_login(json.loads(self._body() or b"{}")))
                if m := re.fullmatch(r"/api/runs/([\w-]+)/relink", p.path):
                    return self._json(jobs.relink(m[1]))
                if m := re.fullmatch(r"/api/runs/([\w-]+)/rerun", p.path):
                    return self._rerun(m[1])
                if m := re.fullmatch(r"/api/runs/([\w-]+)/report", p.path):
                    return self._custom_report(m[1], json.loads(self._body() or b"{}"))
                if p.path == "/api/runs":
                    req = json.loads(self._body() or b"{}")
                    job = jobs.create(req.get("baseline", ""), req.get("candidate", ""), req.get("name", ""),
                                      req.get("options", {}))
                    return self._json(job, 201)
                return self._json({"error": "not found"}, 404)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except (KeyError, FileNotFoundError):
                return self._json({"error": "run not found"}, 404)

        def _custom_report(self, jid: str, opts: dict):
            """Build a PDF report with the chosen parts and issue filter, send it, delete it."""
            from ..report import pdf_report
            run_dir = jobs.path(jid)
            result = json.loads((run_dir / "results.json").read_text())
            name = f"_custom_{uuid.uuid4().hex[:8]}.pdf"
            path = pdf_report.build(result, run_dir, options={"include": opts.get("include") or {},
                                                              "filter": opts.get("filter") or {}}, filename=name)
            try:
                label = re.sub(r"[^\w.-]+", "-", opts.get("label") or "custom").strip("-")[:40] or "custom"
                return self._file(path, download=f"parity-report-{jid}-{label}.pdf")
            finally:
                path.unlink(missing_ok=True)

        def _rerun(self, jid: str):
            old = jobs.get(jid)
            return self._json(jobs.create(old["baseline"], old["candidate"], old["name"], old.get("options", {})), 201)

        def do_DELETE(self):
            if m := re.fullmatch(r"/api/runs/([\w-]+)", urlparse(self.path).path):
                jobs.delete(m[1])
                return self._json({"ok": True})
            return self._json({"error": "not found"}, 404)

    return Handler


def _list_pdfs(root: Path, runs_dir: Path, limit: int = 200) -> list[dict]:
    """PDFs under the root folder, to pick prod/stage without uploading."""
    out = []
    skip = {runs_dir.resolve(), (PROJECT / "reports").resolve(), (PROJECT / ".venv").resolve()}
    for p in sorted(root.rglob("*.pdf")):
        rp = p.resolve()
        if any(rp.is_relative_to(s) for s in skip) or any(part.startswith(".") for part in p.parts):
            continue
        out.append({"path": str(rp), "name": p.name, "folder": str(p.parent.relative_to(root)) if p.parent != root else ".",
                    "size": p.stat().st_size})
        if len(out) >= limit:
            break
    return out


def run(port: int = 8700, root: str | None = None, runs: str | None = None, open_browser: bool = True) -> None:
    root_p = Path(root or PROJECT.parent).resolve()
    jobs = Jobs(Path(runs or PROJECT / "runs").resolve())
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(jobs, root_p))
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"PDF Parity UI at {url}\n  PDFs listed from: {root_p}\n  runs stored in:   {jobs.dir}\nCtrl+C to stop")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
