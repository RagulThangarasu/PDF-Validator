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
        # AEM login: never written to disk by this tool; kept in the macOS Keychain (when available) so a
        # restart of the UI does not silently turn every GUID link into a link to the editor's Explorer
        self.aem_password = os.environ.get("PDFVAL_AEM_PASSWORD", "") or _keychain_get(self._saved_user())
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
        elif options.get("aem_map"):  # stage PDF generated in AEM Guides at the start of the run
            candidate = f"AEM: {options['aem_map']}"
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

    def stage_from_aem(self, baseline: str, map_hint: str = "") -> dict:
        """Start (in the background) finding the prod PDF's map in AEM, generating its stage PDF with the
        map's preset and downloading it into the uploads. Poll with stage_task()."""
        tid = uuid.uuid4().hex[:8]
        self.tasks = getattr(self, "tasks", {})
        self.tasks[tid] = {"id": tid, "state": "running", "message": "Looking for the map in AEM"}

        def work():
            t = self.tasks[tid]
            try:
                acfg = {**aem.merge_settings(engine.load_config().get("aem", {}), self.aem_settings()),
                        "password": self.aem_password}
                mp = aem.resolve_map(map_hint, acfg) if map_hint else aem.find_map_for(Path(baseline).name, acfg)
                preset = aem.preset_for(mp, acfg)
                t.update(map=mp, preset=preset, message=f"Generating “{preset}” for {mp.rsplit('/', 1)[-1]}")
                name = f"{mp.rsplit('/', 1)[-1].rsplit('.', 1)[0]}_{preset.replace(' ', '-')}_AEM.pdf"
                dest = self.dir / "_uploads" / f"{uuid.uuid4().hex[:8]}_{name}"
                aem.generate_pdf(mp, preset, str(dest), acfg, progress=lambda m: t.update(message=m))
                t.update(state="done", path=str(dest), name=name, message=f"Stage PDF generated in AEM ({preset})")
            except Exception as e:
                t.update(state="error", message=f"{e}")
        threading.Thread(target=work, daemon=True).start()
        return self.tasks[tid]

    def stage_task(self, tid: str) -> dict:
        return getattr(self, "tasks", {}).get(tid) or {"state": "error", "message": "unknown task"}

    def _learn_product(self, result: dict) -> None:
        """Remember the product folder AEM reported for a map (e.g. w2720i -> .../Consumer/Projector/w2720i),
        so GUID links open the product's map even when a later run has no AEM login."""
        a = result.get("aem") or {}
        key, folder = a.get("product"), a.get("folder")
        saved = self.aem_settings()
        if key and folder and (saved.get("products") or {}).get(key) != folder:
            self.save_aem_settings({**saved, "products": {**(saved.get("products") or {}), key: folder}})

    def _saved_user(self) -> str:
        return str(self.aem_settings().get("user") or "")

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
        author = str(req.get("author") or "").strip().rstrip("/")
        if author:  # typed on the New comparison page: saved like the one in the AEM topics tab
            cfg["author"] = author
            self.save_aem_settings({**self.aem_settings(), "author": author})
        ok, msg = aem.check_login(cfg)
        if ok:
            self.aem_password = cfg["password"]
            _keychain_set(cfg["user"], cfg["password"])
            self.save_aem_settings({**self.aem_settings(), "user": cfg["user"]})
        return {"ok": ok, "message": msg, "user": cfg["user"], "author": cfg.get("author", "")}

    def relink(self, jid: str) -> dict:
        """Apply the current AEM settings to a finished run: results.json, the CSV and the genuine-issues PDF."""
        from ..report import pdf_report
        run_dir = self.path(jid)
        result = json.loads((run_dir / "results.json").read_text())
        acfg = self.aem_config()
        if not result.get("aem") and Path(result["meta"]["candidate"].get("path", "")).is_file():
            aem.annotate(result, {"aem": acfg})  # a run made before GUIDs were traced
        if aem.relink(result, acfg):
            self._learn_product(result)
            (run_dir / "results.json").write_text(json.dumps(result, indent=1, ensure_ascii=False))
            writer.write_genuine_csv(result, run_dir / "genuine-issues.csv")
            writer.write_viewer(result, run_dir)
            pdf_report.build(result, run_dir, options=pdf_report.GENUINE, filename="genuine-issues.pdf")
            pdf_report.build(result, run_dir, options=pdf_report.CSS_REPORT, filename="css-issues.pdf")
            pdf_report.build(result, run_dir, options=pdf_report.IMAGE_REPORT, filename="image-issues.pdf")
            if (run_dir / "report.pdf").exists():
                from ..report import shots as shotmod
                sev = shotmod.SEVERITIES.get(result["meta"].get("screenshots", "all")) or None
                pdf_report.build(result, run_dir, severities=sev)
        return result.get("aem") or {}

    def source(self, jid: str, side: str) -> Path:
        """Source export (raw data + rebuilt DITA; stage: + the real AEM topic files when logged in)
        of the run's prod / stage PDF, or both in one zip. Built once, kept in the run folder."""
        import zipfile
        from .. import source_export
        run_dir = self.path(jid)
        job = self.get(jid)
        if side == "both":
            out = run_dir / "source-prod-and-stage.zip"
            if not out.exists():
                parts = [("prod", self.source(jid, "baseline"))]
                if job.get("mode", "pdf") != "html":
                    parts.append(("stage", self.source(jid, "candidate")))
                with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
                    for prefix, path in parts:
                        with zipfile.ZipFile(path) as src:
                            for n in src.namelist():
                                z.writestr(f"{prefix}/{n}", src.read(n))
            return out
        if side == "candidate" and job.get("mode", "pdf") == "html":
            raise ValueError("The stage of this run is a web page: its source is the page itself")
        tag = "prod" if side == "baseline" else "stage"
        out = run_dir / f"source-{tag}.zip"
        if not out.exists():
            pdf = run_dir / ("baseline.pdf" if side == "baseline" else "candidate.pdf")
            pdf = pdf if pdf.exists() else Path(job["baseline" if side == "baseline" else "candidate"])
            cfg = engine.load_config()
            _apply_options(cfg, job.get("options", {}))
            acfg = self.aem_config() if side == "candidate" and self.aem_config().get("author") else None
            source_export.export(str(pdf), out, cfg, label=tag, aem_cfg=acfg)
        return out

    def delete(self, jid: str) -> None:
        shutil.rmtree(self.path(jid), ignore_errors=True)

    def _run(self, jid: str) -> None:
        with self.run_lock:
            job = self.update(jid, status="running", message="Starting")
            try:
                if job["options"].get("aem_map"):  # generate the stage PDF in AEM first (Native PDF, map's preset)
                    acfg = {**aem.merge_settings(engine.load_config().get("aem", {}), self.aem_settings()),
                            "password": self.aem_password}
                    map_path = aem.resolve_map(job["options"]["aem_map"], acfg)
                    preset = aem.preset_for(map_path, acfg)
                    dest = str(self.path(jid) / "stage_from_aem.pdf")
                    self.update(jid, message=f"Generating the stage PDF in AEM ({preset})", aem_map_path=map_path,
                                aem_preset=preset)
                    aem.generate_pdf(map_path, preset, dest, acfg, progress=lambda m: self.update(jid, message=m))
                    job = self.update(jid, candidate=dest)
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
                self._learn_product(result)
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


_KEYCHAIN_SERVICE = "pdfval-aem"


def _keychain_get(user: str) -> str:
    """The AEM password saved in the macOS Keychain for this user ("" when none / not macOS)."""
    import shutil
    import subprocess
    if not user or not shutil.which("security"):
        return ""
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-a", user, "-w"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.rstrip("\n") if r.returncode == 0 else ""
    except Exception:
        return ""


def _keychain_set(user: str, password: str) -> None:
    """Save (or update) the AEM password in the macOS Keychain; silently nothing elsewhere."""
    import shutil
    import subprocess
    if not user or not password or not shutil.which("security"):
        return
    try:
        subprocess.run(["security", "add-generic-password", "-U", "-s", _KEYCHAIN_SERVICE, "-a", user, "-w", password],
                       capture_output=True, timeout=10)
    except Exception:
        pass


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
    for k in ("check_spacing", "normalize_typography", "ignore_quote_style"):
        if k in o:
            cfg["content"][k] = bool(o[k])
    if "css_vs_prod" in o:  # off (default): typography / CSS / layout on stage only, against the design spec
        cfg["style"]["compare_with_prod"] = cfg["layout"]["compare_with_prod"] = bool(o["css_vs_prod"])
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
    meta = MetadataCheck(jobs)

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
                if m := re.fullmatch(r"/api/aem/stage/(\w+)", p):
                    return self._json(jobs.stage_task(m[1]))
                if p == "/api/aem":
                    c = jobs.aem_config()
                    return self._json({**{k: v for k, v in c.items() if k != "password"}, "has_password": bool(c["password"])})
                if p == "/api/metadata":
                    return self._json(meta.status())
                if m := re.fullmatch(r"/api/metadata/(metadata-report\.(?:pdf|csv))", p):
                    return self._file(meta.dir / m[1], download=m[1])
                if p == "/api/library":
                    return self._json(LIBRARY.status())
                if p == "/api/files":
                    return self._json(_list_pdfs(root, jobs.dir))
                if m := re.fullmatch(r"/api/runs/([\w-]+)/source", p):
                    side = parse_qs(u.query).get("side", ["both"])[0]
                    if side not in ("baseline", "candidate", "both"):
                        return self._json({"error": "side must be baseline, candidate or both"}, 400)
                    job = jobs.get(m[1])
                    stem = {"baseline": Path(job["baseline"]).stem, "candidate": Path(job["candidate"]).stem,
                            "both": re.sub(r"[^\w.-]+", "-", job["name"]).strip("-")}[side]
                    tag = {"baseline": "prod", "candidate": "stage", "both": "prod-and-stage"}[side]
                    try:
                        path = jobs.source(m[1], side)
                    except ValueError as e:
                        return self._json({"error": str(e)}, 400)
                    return self._file(path, download=f"{stem[:60]}-{tag}-source.zip")
                if m := re.fullmatch(r"/runs/([\w-]+)/(.+)", p):
                    base = jobs.path(m[1]).resolve()
                    target = (base / m[2]).resolve()
                    if not target.is_relative_to(base):
                        return self._json({"error": "forbidden"}, 403)
                    dl = parse_qs(u.query).get("download", [None])[0]
                    if target.name == "image-issues.pdf" and not target.exists() and (base / "results.json").exists():
                        # a run finished before the image report existed: build it on first download
                        from ..report import pdf_report as _pr
                        _pr.build(json.loads((base / "results.json").read_text()), base,
                                  options=_pr.IMAGE_REPORT, filename="image-issues.pdf")
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
                if p.path == "/api/metadata/sheet":
                    name = Path(parse_qs(p.query).get("name", ["sheet.xlsx"])[0]).name
                    if not name.lower().endswith(".xlsx"):
                        return self._json({"error": f"{name} is not an .xlsx file"}, 400)
                    (PROJECT / "metadata").mkdir(exist_ok=True)
                    (PROJECT / "metadata" / name).write_bytes(self._body())
                    return self._json(meta.status())
                if p.path == "/api/metadata":
                    return self._json(meta.start(json.loads(self._body() or b"{}")))
                if p.path == "/api/library/extract":
                    return self._json(LIBRARY.start())
                if p.path == "/api/aem":
                    return self._json(jobs.save_aem_settings(json.loads(self._body() or b"{}")))
                if p.path == "/api/aem/stage":  # find the map of the prod PDF, generate + download the stage PDF
                    req = json.loads(self._body() or b"{}")
                    return self._json(jobs.stage_from_aem(req.get("baseline", ""), req.get("map", "")))
                if p.path == "/api/aem/preset":  # which preset a map gets, and its link in the map console
                    req = json.loads(self._body() or b"{}")
                    acfg = jobs.aem_config()
                    try:
                        mp = aem.resolve_map(req.get("map", ""), acfg)
                    except RuntimeError as e:
                        return self._json({"error": str(e)}, 400)
                    return self._json({"map_path": mp, "preset": aem.preset_for(mp, acfg),
                                       "open": aem.url_for({"guid": "", "map": mp.rsplit("/", 1)[-1], "map_path": mp}, acfg, "map")})
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
            label = re.sub(r"[^\w.-]+", "-", opts.get("label") or "custom").strip("-")[:40] or "custom"
            if opts.get("format") == "csv":  # the same selection as a spreadsheet
                from ..report import writer
                path = run_dir / f"_custom_{uuid.uuid4().hex[:8]}.csv"
                writer.write_issues_csv(result, pdf_report.select_issues(result, opts.get("filter") or {}), path)
                try:
                    return self._file(path, download=f"parity-issues-{jid}-{label}.csv")
                finally:
                    path.unlink(missing_ok=True)
            name = f"_custom_{uuid.uuid4().hex[:8]}.pdf"
            path = pdf_report.build(result, run_dir, options={"include": opts.get("include") or {},
                                                              "filter": opts.get("filter") or {}}, filename=name)
            try:
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
    skip = {runs_dir.resolve(), (PROJECT / "reports").resolve(), (PROJECT / ".venv").resolve(),
            (PROJECT / "pdf-prod-pdfs").resolve(), (PROJECT / "pdf-prod").resolve()}
    for p in sorted(root.rglob("*.pdf")):
        rp = p.resolve()
        if any(rp.is_relative_to(s) for s in skip) or any(part.startswith(".") for part in p.parts):
            continue
        out.append({"path": str(rp), "name": p.name, "folder": str(p.parent.relative_to(root)) if p.parent != root else ".",
                    "size": p.stat().st_size})
        if len(out) >= limit:
            break
    return out


class Library:
    """Prod archives (.7z / .zip under pdf-prod/<FM|INDD>/) with only their PDFs extracted to
    pdf-prod-pdfs/<FM|INDD>/<archive name>/..., keeping the folder layout, so a prod PDF can be picked by
    folder or searched by name. Folders already unpacked in pdf-prod are listed too."""

    ARCHIVES = (".7z", ".zip")

    def __init__(self, src: Path, dest: Path):
        self.src, self.dest = src, dest
        self.lock = threading.Lock()
        self.state = {"running": False, "done": 0, "total": 0, "current": "", "errors": []}

    def _archives(self) -> list[Path]:
        return sorted(p for p in self.src.rglob("*") if p.suffix.lower() in self.ARCHIVES and p.is_file()
                      and not any(part.startswith(".") for part in p.relative_to(self.src).parts))

    def _target(self, archive: Path) -> Path:
        rel = archive.relative_to(self.src)
        return self.dest / rel.parent / archive.stem

    def start(self) -> dict:
        with self.lock:
            if not self.state["running"]:
                if not shutil.which("7z"):
                    raise ValueError("7z is not installed (brew install p7zip)")
                todo = [a for a in self._archives() if not (self._target(a) / ".done").exists()]
                self.state = {"running": bool(todo), "done": 0, "total": len(todo), "current": "", "errors": []}
                if todo:
                    threading.Thread(target=self._extract, args=(todo,), daemon=True).start()
        return self.status()

    def _extract(self, todo: list[Path]) -> None:
        import subprocess
        for a in todo:
            self.state["current"] = str(a.relative_to(self.src))
            out = self._target(a)
            out.mkdir(parents=True, exist_ok=True)
            r = subprocess.run(["7z", "x", str(a), f"-o{out}", "-r", "-y", "-aoa", "*.pdf", "*.PDF"],
                               capture_output=True, text=True)
            if r.returncode in (0, 1):  # 1 = warnings only
                (out / ".done").write_text(a.name)
            else:
                self.state["errors"].append(f"{a.name}: {(r.stderr or r.stdout).strip().splitlines()[-1:] or ['failed']}")
            self.state["done"] += 1
        self.state.update(running=False, current="")

    def status(self) -> dict:
        files = []
        for base, origin in ((self.dest, "archive"), (self.src, "folder")):
            if not base.is_dir():
                continue
            for p in base.rglob("*"):
                if p.suffix.lower() != ".pdf" or not p.is_file():
                    continue
                rel = p.relative_to(base)
                if any(part.startswith(".") for part in rel.parts) or len(rel.parts) < 3:
                    continue  # expected: <FM|INDD>/<product>/.../file.pdf
                files.append({"path": str(p.resolve()), "name": p.name, "group": rel.parts[0], "product": rel.parts[1],
                              "sub": "/".join(rel.parts[2:-1]), "size": p.stat().st_size, "origin": origin})
        # the manual first, figures (images/ subfolders) and small files after
        files.sort(key=lambda f: (f["group"], f["product"].lower(), "images" in f["sub"].lower(), -f["size"]))
        pending = sum(1 for a in self._archives() if not (self._target(a) / ".done").exists()) if self.src.is_dir() else 0
        return {**self.state, "files": files, "pending": pending, "src": str(self.src), "dest": str(self.dest)}


LIBRARY = Library(PROJECT / "pdf-prod", PROJECT / "pdf-prod-pdfs")


class MetadataCheck:
    """Document Title / Page Title of every product map in AEM against the migration sheet
    (pdfval.metadata); one check at a time, the last result kept in <runs>/_metadata/."""

    def __init__(self, jobs: Jobs):
        self.jobs = jobs
        self.dir = jobs.dir / "_metadata"
        self.state = {"running": False, "progress": 0.0, "message": "", "error": ""}

    def sheets(self) -> list[dict]:
        folder = PROJECT / "metadata"
        return [{"name": p.name, "size": p.stat().st_size} for p in
                sorted(folder.glob("*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True) if not p.name.startswith("~$")]

    def status(self) -> dict:
        try:
            last = json.loads((self.dir / "metadata.json").read_text())
        except (FileNotFoundError, ValueError):
            last = None
        return {**self.state, "sheets": self.sheets(), "result": last}

    def start(self, req: dict) -> dict:
        from .. import metadata
        if self.state["running"]:
            return self.status()
        name = Path(str(req.get("sheet") or "")).name
        sheet = PROJECT / "metadata" / name if name else metadata.default_sheet()
        if not sheet or not sheet.is_file():
            raise ValueError("Pick the metadata sheet (.xlsx) first")
        cfg = self.jobs.aem_config()
        if not cfg.get("password"):
            raise ValueError("Log in to AEM first (New comparison page, AEM login)")
        self.state = {"running": True, "progress": 0.0, "message": "Starting", "error": ""}

        def work():
            try:
                metadata.run(cfg, self.dir, sheet, str(req.get("lang") or "en"),
                             progress=lambda f, m: self.state.update(progress=round(f, 3), message=m))
                self.state.update(running=False, progress=1.0, message="Done")
            except Exception as e:
                traceback.print_exc()
                self.state.update(running=False, error=f"{type(e).__name__}: {e}")
        threading.Thread(target=work, daemon=True).start()
        return self.status()


def run(port: int = 8700, root: str | None = None, runs: str | None = None, open_browser: bool = True) -> None:
    root_p = Path(root or PROJECT.parent).resolve()
    jobs = Jobs(Path(runs or PROJECT / "runs").resolve())
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(jobs, root_p))
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"PDF Parity UI at {url}\n  PDFs listed from: {root_p}\n  runs stored in:   {jobs.dir}\nCtrl+C to stop")
    try:  # the prod archives (pdf-prod/FM, pdf-prod/INDD): their PDFs extracted in the background, so every
        LIBRARY.start()  # prod PDF is in the Production PDF list without a click
    except ValueError as e:
        print(f"  prod library: {e}")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
