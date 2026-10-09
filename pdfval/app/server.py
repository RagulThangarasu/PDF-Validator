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
    """Runs live in <runs>/<id>/ with a job.json. Up to `parallel` comparisons run at the same time, each in
    its own process (pdfval.app.worker); the others wait in the queue."""

    def __init__(self, runs_dir: Path, recover: bool = True, parallel: int | None = None):
        self.dir = runs_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "_uploads").mkdir(exist_ok=True)
        self.lock = threading.Lock()
        n = parallel or int((engine.load_config().get("ui") or {}).get("parallel_runs", 0))
        if n <= 0:  # auto: every core but one (the UI and AEM downloads keep running)
            n = max(1, (os.cpu_count() or 2) - 1)
        self.run_sem = threading.BoundedSemaphore(max(1, n))
        # PDF <-> PDF runs are CPU-bound (image correlation etc.): capped near the core count above, or the
        # worker processes themselves oversubscribe the CPU. A PDF <-> web page run spends most of its time
        # waiting on the site, not the CPU, so many more can be in flight together; default a generous pool,
        # and a batch of them (Prod <-> Stage pairs, "Validate many URLs") can ask for its own size, up to it
        nh = int((engine.load_config().get("ui") or {}).get("parallel_html_runs", 0)) or max(n, 2 * (os.cpu_count() or 2))
        self.html_sem_max = max(1, nh)
        self.html_sem = threading.BoundedSemaphore(self.html_sem_max)
        self.batch_sem: dict[str, threading.BoundedSemaphore] = {}  # batch id -> its own "N at a time", if set
        self.procs: dict = {}  # run id -> its worker process (to stop it)
        self.stopping: set = set()  # runs stopped by the user (their worker's exit is not an error)
        # web-page passwords live in memory only (never in job.json), per (site, user),
        # so Rerun works until the server restarts
        self.passwords: dict[tuple[str, str], str] = {}
        # AEM login: never written to disk by this tool; kept in the macOS Keychain (when available) so a
        # restart of the UI does not silently turn every GUID link into a link to the editor's Explorer
        self.aem_password = os.environ.get("PDFVAL_AEM_PASSWORD", "") or _keychain_get(self._saved_user())
        for job in self.list() if recover else ():  # server restarted mid-run
            if job["status"] in ("queued", "running"):
                self.update(job["id"], status="error", message="Interrupted (server restarted)")

    def path(self, jid: str) -> Path:
        if not re.fullmatch(r"[\w-]+", jid):
            raise KeyError(jid)
        return self.dir / jid

    def get(self, jid: str) -> dict:
        return json.loads((self.path(jid) / "job.json").read_text(encoding="utf-8"))

    def update(self, jid: str, **kw) -> dict:
        with self.lock:
            job = self.get(jid)
            job.update(kw)
            _write_atomic(self.path(jid) / "job.json", json.dumps(job, indent=1))  # read by the UI while a worker writes
            return job

    def list(self) -> list[dict]:
        jobs = []
        for p in self.dir.glob("*/job.json"):
            try:
                jobs.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                pass
        return sorted(jobs, key=lambda j: j["created"], reverse=True)

    def create(self, baseline: str, candidate: str, name: str, options: dict, batch: str = "") -> dict:
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
            # the AEM login already known to the server (AEM login card / keychain) is used for pages of that AEM
            # when nothing is typed here: no need to enter it for every run
            acfg = self.aem_config()
            if urlparse(candidate).netloc == urlparse(acfg.get("author") or "").netloc and self.aem_password:
                options["html_user"] = options["html_user"] or acfg.get("user") or ""
                if not password and options["html_user"] == (acfg.get("user") or options["html_user"]):
                    password = self.aem_password
            if aem.is_site_root(candidate):
                # a folder of AEM Sites, not a guide page (…/sites.html/content/guide): the guide of the selected
                # prod PDF's product is searched under it, in English, and that page is validated
                found = self.site_for(baseline, candidate, options["html_user"], password)
                if not found.get("url"):
                    raise ValueError(f"No English guide found for “{found.get('product') or Path(baseline).stem}” under "
                                     f"{found.get('root')} ({found.get('guides', 0)} guides searched). Enter the guide's "
                                     f"page address instead.")
                options = {**options, "site_root": candidate, "site_map": found.get("map", ""), "site_pages": found.get("pages", 0)}
                candidate = found["url"]
            # a page of the AEM author is read in its published view (?wcmmode=disabled): without the editing frame,
            # and the server answers it in about half the time
            cu = urlparse(candidate)
            if cu.netloc == urlparse(acfg.get("author") or "").netloc and cu.path.startswith("/content/") \
                    and "wcmmode" not in cu.query:
                candidate += ("&" if cu.query else "?") + "wcmmode=disabled"
            # "Add ?wcmmode=disabled" checkbox: the host above is only guessed from the configured AEM author -
            # this covers every other URL (another AEM instance, a host not recognised as "author", ...)
            if options.get("wcmmode_disabled") and "wcmmode" not in urlparse(candidate).query:
                candidate += ("&" if urlparse(candidate).query else "?") + "wcmmode=disabled"
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
               "message": "Queued", "summary": None, **({"batch": batch} if batch else {})}
        (self.path(jid) / "job.json").write_text(json.dumps(job, indent=1), encoding="utf-8")
        threading.Thread(target=self._run, args=(jid,), daemon=True).start()
        return job

    def site_for(self, baseline: str, root_url: str, user: str = "", password: str = "") -> dict:
        """The English guide in AEM Sites of the prod PDF's product, searched under root_url (read-only)."""
        cfg = self.aem_config()
        if user and password:  # the login typed for the page, when the AEM login card is empty
            cfg = {**cfg, "user": cfg.get("user") or user, "password": cfg.get("password") or password}
        if not cfg.get("author"):
            cfg["author"] = "{0.scheme}://{0.netloc}".format(urlparse(root_url))
        try:
            return aem.site_for_pdf(baseline, root_url, cfg)
        except RuntimeError as e:
            raise ValueError(str(e))

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
            return json.loads((self.dir / "aem-settings.json").read_text(encoding="utf-8"))
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
        _write_atomic(self.dir / "aem-settings.json", json.dumps(keep, indent=1))  # several workers may learn products
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
        result = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
        acfg = self.aem_config()
        if not result.get("aem") and Path(result["meta"]["candidate"].get("path", "")).is_file():
            aem.annotate(result, {"aem": acfg})  # a run made before GUIDs were traced
        if aem.relink(result, acfg):
            self._learn_product(result)
            (run_dir / "results.json").write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
            writer.write_genuine_csv(result, run_dir / "genuine-issues.csv")
            writer.write_viewer(result, run_dir)
            writer.write_pdf_report(result, run_dir)
            if (run_dir / "css-issues.pdf").exists():  # a batch run builds it when first opened
                pdf_report.build(result, run_dir, options=pdf_report.CSS_REPORT, filename="css-issues.pdf")
            writer.write_image_report(result, run_dir)
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
        self.stop(jid)
        shutil.rmtree(self.path(jid), ignore_errors=True)

    def stop(self, jid: str) -> bool:
        """Stop a queued or running run: a queued one never starts, a running worker is ended with every
        process it started. The run stays in the list as "stopped"."""
        import signal
        try:
            job = self.get(jid)
        except (KeyError, FileNotFoundError):
            return False
        if job["status"] not in ("queued", "running"):
            return False
        self.stopping.add(jid)
        self.update(jid, status="stopped", message="Stopped by user")
        proc = self.procs.get(jid)
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except Exception:
                    os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            self.update(jid, status="stopped", message="Stopped by user")  # the worker may have written since
        return True

    def stop_all(self) -> int:
        return sum(self.stop(j["id"]) for j in self.list() if j["status"] in ("queued", "running"))

    def clear(self) -> int:
        """Stop everything, then remove every run - finished ones (and their reports) included."""
        self.stop_all()
        gone = [j["id"] for j in self.list()]
        for jid in gone:
            shutil.rmtree(self.path(jid), ignore_errors=True)
        return len(gone)

    def _run(self, jid: str) -> None:
        """Wait for a free slot, then run the comparison in its own process (CPU-bound: threads would share one
        core). The passwords go to the worker in its environment, never on disk. A PDF <-> PDF run waits on
        the shared CPU-sized pool; a PDF <-> web page run waits on the larger, I/O-bound pool instead - its
        own batch's pool, when "Validate many URLs" asked for a particular "N at a time"."""
        import subprocess
        import sys
        try:
            job = self.get(jid)
        except (KeyError, FileNotFoundError):
            return  # deleted while queued
        is_html = job.get("options", {}).get("mode") == "html"
        sem = (self.batch_sem.get(job.get("batch", "")) if is_html else None) or (self.html_sem if is_html else self.run_sem)
        with sem:
            try:
                job = self.get(jid)
            except (KeyError, FileNotFoundError):
                return  # deleted while queued
            if job["status"] == "stopped":
                return  # stopped while queued
            env = {**os.environ, "PDFVAL_AEM_PASSWORD": self.aem_password or ""}
            # one worker per CPU core already gives process-level parallelism; without this, numpy's own
            # BLAS backend (Accelerate / OpenBLAS) ALSO spawns several threads per worker for the image
            # correlation math in assets.py, so running several workers at once oversubscribes the CPU
            # many times over and can crash a worker outright (seen as "Worker stopped: <no Python frame>",
            # a native fatal error with no Python traceback) under memory/CPU pressure, not a code bug
            for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "MKL_NUM_THREADS",
                       "NUMEXPR_NUM_THREADS"):
                env.setdefault(var, "1")
            # belt-and-suspenders beside the explicit encoding="utf-8" on every report write_text/open():
            # forces Python's own default text encoding to UTF-8 in the worker regardless of the OS locale,
            # so a legacy-codepage Windows machine (cp1252, cp950, ...) can't hit UnicodeEncodeError on a
            # report that contains non-ASCII characters (arrows, CJK, accents) even from code that forgets
            # to pass encoding= explicitly
            env["PYTHONUTF8"] = "1"
            o = job.get("options", {})
            if o.get("mode") == "html":
                env["PDFVAL_HTML_PASSWORD"] = self.passwords.get((urlparse(job["candidate"]).netloc, o.get("html_user", "")), "")
            self.update(jid, status="running", message="Starting")
            # its own process group: Stop ends the worker and everything it started (browsers, OCR)
            proc = subprocess.Popen([sys.executable, "-m", "pdfval.app.worker", str(self.dir), jid], env=env,
                                    cwd=str(PROJECT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    start_new_session=True)
            self.procs[jid] = proc
            out, err = proc.communicate()
            self.procs.pop(jid, None)
            r = subprocess.CompletedProcess(proc.args, proc.returncode, out, err)
            try:
                job = self.get(jid)
            except (KeyError, FileNotFoundError):
                return
            if job["status"] == "stopped" or jid in self.stopping:
                return
            if job["status"] in ("queued", "running"):  # the worker died without reporting
                # the crash is usually a native fatal error with little in the traceback itself - keep the
                # full combined output on disk so it can be inspected after the fact, not just the 1-line
                # summary below (the previous behaviour discarded everything but that last line)
                try:
                    (self.path(jid) / "worker-crash.log").write_text(
                        f"exit code {r.returncode}\n\n--- stderr ---\n{r.stderr or ''}\n\n--- stdout ---\n{r.stdout or ''}",
                        encoding="utf-8")
                except OSError:
                    pass
                lines = (r.stderr or r.stdout or "").strip().splitlines()
                tail = lines[-1:] or [f"exit code {r.returncode}"]
                self.update(jid, status="error", message=f"Worker stopped: {tail[0][:300]}")

    def execute(self, jid: str) -> None:
        """The comparison itself (in the worker process)."""
        if True:
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
                                                      "width": o.get("html_width") or 1440, "wait_ms": o.get("html_wait") or 500})
                else:
                    result = engine.compare(job["baseline"], job["candidate"], cfg, progress=step)
                # a batch run builds only what the batch delivers (genuine-issues + image report, with their
                # screenshots); the full report and the CSS report are built when first opened
                # (a web page run the same: its report holds a short list of issue kinds - screenshots are made for
                # those issues only, not for every finding of the full report nobody may open)
                batch = bool(job.get("batch")) or job.get("mode") == "html"
                result["meta"]["name"] = job["name"]  # the product / publication: named in every report
                writer.write_all(result, str(self.path(jid)), "reports" if batch else job["options"].get("screenshots", "all"),
                                 progress=lambda f, m: self.update(jid, progress=round(0.45 + 0.55 * f, 3), message=m),
                                 full=not batch)
                self._learn_product(result)
                tm = result["meta"].get("timing") or {}
                took = (f" - web capture {tm.get('total_s', 0):.0f} s for {tm.get('web_pages', 0)} page(s) (open "
                        f"{tm.get('open_s', 0):.0f}, load {tm.get('load_s', 0):.0f}, read {tm.get('read_s', 0):.0f}, site "
                        f"checks {tm.get('site_s', 0):.0f}), comparison {tm.get('compare_s', 0):.0f} s") if tm else ""
                self.update(jid, status="done", progress=1.0, message="Done" + took, summary=result["summary"],
                            finished=datetime.now().isoformat(timespec="seconds"))
            except Exception as e:  # surface the failure in the UI
                traceback.print_exc()
                self.update(jid, status="error", message=f"{type(e).__name__}: {e}")


def _batches(jobs: Jobs) -> list[dict]:
    """Runs started together from the pairs list: progress and how many reports are ready."""
    out: dict[str, dict] = {}
    for j in jobs.list():
        if not j.get("batch"):
            continue
        b = out.setdefault(j["batch"], {"id": j["batch"], "created": j["created"], "runs": 0, "done": 0, "error": 0,
                                         "running": 0, "queued": 0})
        b["runs"] += 1
        b[j["status"] if j["status"] in ("done", "error", "running", "queued") else "error"] += 1
        b["created"] = min(b["created"], j["created"])
    return sorted(out.values(), key=lambda b: b["created"], reverse=True)


def _batch_zip(jobs: Jobs, bid: str) -> Path:
    """One zip with the PDF report (genuine issues), its Word version and the image report of every finished product
    of the batch, named after the product: "<product> - PDF report.pdf", "<product> - PDF report.docx",
    "<product> - Image report.pdf". A product with no
    genuine issues has no PDF report, one with no image issues has no image report - never an empty "0 issues" file."""
    import tempfile
    import zipfile
    runs = [j for j in jobs.list() if j.get("batch") == bid]
    if not runs:
        raise KeyError(bid)
    fd, tmp = tempfile.mkstemp(suffix=".zip", dir=jobs.dir)
    os.close(fd)
    used: set[str] = set()
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for j in sorted(runs, key=lambda j: j["name"].lower()):
            if j["status"] != "done":
                continue
            run_dir = jobs.path(j["id"])
            src = run_dir / "genuine-issues.pdf"
            img = run_dir / "image-issues.pdf"
            # the image report only for a publication whose images FAIL (a PASS has none); built here for a run
            # made before the image report / its verdict existed
            if (run_dir / "results.json").exists() and (not img.exists() or "images" not in (j.get("summary") or {})):
                try:
                    writer.write_image_report(json.loads((run_dir / "results.json").read_text(encoding="utf-8")), run_dir)
                except Exception:
                    traceback.print_exc()
            if not src.exists() and not img.exists():
                continue  # nothing to report for this product: a clean pass on both content and images
            name = re.sub(r"[^\w .()&+-]+", "_", j["name"]).strip() or j["id"]
            while name in used:
                name += "_"
            used.add(name)
            if src.exists():
                z.write(src, f"{name} - PDF report.pdf")
                # the Word report of the same issues, for every publication that has a PDF report: built here
                # when the run has none, or one made by an older version of the Word report
                word = run_dir / "genuine-issues.docx"
                try:
                    from ..report import docx_report
                    code = max(Path(docx_report.__file__).stat().st_mtime, Path(writer.__file__).stat().st_mtime)
                    if (run_dir / "results.json").exists() and (not word.exists() or word.stat().st_mtime < code):
                        res = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
                        writer.assign_bug_ids(res)
                        writer.write_docx_report(res, run_dir)
                except Exception:
                    traceback.print_exc()
                if word.exists():
                    z.write(word, f"{name} - PDF report.docx")
            if img.exists():
                z.write(img, f"{name} - Image report.pdf")
    return Path(tmp)


def _batch_consolidated(jobs: Jobs, bid: str) -> tuple[Path, Path]:
    """The batch's consolidated report (every product with its content match %), rebuilt on each request
    so it shows the runs finished so far."""
    from ..report import batch_report
    runs = [j for j in jobs.list() if j.get("batch") == bid]
    if not runs:
        raise KeyError(bid)
    return batch_report.build(bid, runs, jobs.dir, jobs.dir / "_batches")


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


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
                if p == "/api/batches":
                    return self._json(_batches(jobs))
                if m := re.fullmatch(r"/api/batches/(batch-[\w-]+)/consolidated\.(pdf|csv)", p):
                    try:
                        pdf, csv_ = _batch_consolidated(jobs, m[1])
                    except KeyError:
                        return self._json({"error": "unknown batch"}, 404)
                    return self._file(pdf if m[2] == "pdf" else csv_, download=f"{m[1]}-consolidated.{m[2]}")
                if m := re.fullmatch(r"/api/batches/(batch-[\w-]+)/image-consolidated\.(pdf|csv)", p):
                    # publication | image issues PASS / FAIL (a PASS has no image report)
                    from ..report import batch_report
                    runs = [j for j in jobs.list() if j.get("batch") == m[1]]
                    if not runs:
                        return self._json({"error": "unknown batch"}, 404)
                    pdf, csv_ = batch_report.build_images(m[1], runs, jobs.dir, jobs.dir / "_batches")
                    return self._file(pdf if m[2] == "pdf" else csv_, download=f"{m[1]}-image-consolidated.{m[2]}")
                if m := re.fullmatch(r"/api/batches/(batch-[\w-]+)/reports\.zip", p):
                    path = _batch_zip(jobs, m[1])
                    try:
                        return self._file(path, download=f"{m[1]}-reports.zip")
                    finally:
                        path.unlink(missing_ok=True)
                if p == "/api/pairs":
                    return self._json(PAIRS.suggest())
                if p == "/api/site-for":  # the English guide in AEM Sites of a prod PDF, searched under a Sites folder
                    qs = parse_qs(u.query)
                    return self._json(jobs.site_for(qs.get("pdf", [""])[0], qs.get("root", [""])[0]))
                if p == "/api/prod-for":  # the prod PDF of a stage web guide, from its URL
                    return self._json(PAIRS.prod_for_url(parse_qs(u.query).get("url", [""])[0]))
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
                    if target.name in writer.DEFERRED and not target.exists() and (base / "results.json").exists():
                        writer.build_deferred(base, target.name)  # a batch run: built on first open
                    if target.name == "side-by-side.pdf" and not target.exists() and (base / "results.json").exists():
                        # the side-by-side page report: built on first download
                        from ..report import page_report as _sp
                        _sp.build(json.loads((base / "results.json").read_text(encoding="utf-8")), base)
                    if target.name == "image-issues.pdf" and not target.exists() and (base / "results.json").exists():
                        # a run finished before the image report existed: build it on first download
                        # (nothing is built when its images PASS: the file stays missing -> 404)
                        writer.write_image_report(json.loads((base / "results.json").read_text(encoding="utf-8")), base)
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
                if p.path == "/api/prod-for-bulk":  # many stage web-guide URLs at once, each matched to its prod PDF
                    req = json.loads(self._body() or b"{}")
                    return self._json({"matches": PAIRS.prod_for_urls(req.get("urls") or [])})
                if p.path == "/api/pairs/run":
                    req = json.loads(self._body() or b"{}")
                    bid = "batch-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
                    concurrent = int(req.get("concurrent") or 0)
                    if concurrent > 0:  # "N at a time" for this batch - web-page pairs only; set before the first job starts
                        jobs.batch_sem[bid] = threading.BoundedSemaphore(min(max(1, concurrent), max(jobs.html_sem_max, 50)))
                    made = [jobs.create(x["baseline"], x["candidate"], x.get("name", ""), req.get("options") or {"mode": "pdf"},
                                        batch=bid)
                            for x in req.get("pairs", []) if x.get("baseline") and x.get("candidate")]
                    return self._json({"batch": bid, "runs": [j["id"] for j in made]}, 201)
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
                if p.path == "/api/runs/stop-all":
                    return self._json({"stopped": jobs.stop_all()})
                if p.path == "/api/runs/clear":
                    return self._json({"removed": jobs.clear()})
                if m := re.fullmatch(r"/api/runs/([\w-]+)/stop", p.path):
                    return self._json({"stopped": jobs.stop(m[1])})
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
            result = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
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
                (out / ".done").write_text(a.name, encoding="utf-8")
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


class Pairs:
    """Prod vs stage pairs for a batch of comparisons: every stage PDF downloaded from AEM (aem-map-pdfs/<lang>/
    <Brand>/<Category>/<product>/) with its prod PDF from the prod library. A stage map is paired through the
    migration Excel (the map's model row -> its file, e.g. GW2291_EN_V0 = the prod archive), else by name."""

    def __init__(self, stage_dir: Path, flat_dir: Path | None = None):
        self.aem_dir, self.flat_dir = stage_dir, flat_dir

    @property
    def dir(self) -> Path:
        """Where the stage PDFs are read from: [ui] stage_dir, else stage-pdf/ when it holds PDFs (stage PDFs
        saved by hand, named by their Document Title), else the AEM downloads in aem-map-pdfs/."""
        want = (engine.load_config().get("ui") or {}).get("stage_dir") or ""
        if want:
            p = Path(want)
            return p if p.is_absolute() else PROJECT / p
        if self.flat_dir is not None and self.flat_dir.is_dir() and any(self.flat_dir.glob("*.pdf")):
            return self.flat_dir
        return self.aem_dir

    def stage_files(self) -> list[dict]:
        out = []
        if self.dir.is_dir() and not any(p.is_dir() and p.name not in ("other-outputs",) for p in self.dir.iterdir()):
            # a flat folder of stage PDFs named by title (“Monitor GW90P Series user manual.pdf”)
            for p in sorted(self.dir.glob("*.pdf")):
                product = re.sub(r"\s*\(\d+\)$", "", p.stem)
                out.append({"path": str(p.resolve()), "name": p.name, "lang": "en", "title": product,
                            "product": re.sub(r"\s+user\s+manual$", "", product, flags=re.I),
                            "folder": self.dir.name, "size": p.stat().st_size})
            return out
        for p in sorted(self.dir.rglob("*.pdf")) if self.dir.is_dir() else []:
            rel = p.relative_to(self.dir)
            if "other-outputs" in rel.parts or len(rel.parts) < 3:
                continue
            out.append({"path": str(p.resolve()), "name": p.name, "lang": rel.parts[0], "product": rel.parts[-2],
                        "folder": "/".join(rel.parts[1:-1]), "size": p.stat().st_size})
        return out

    def suggest(self) -> dict:
        from .. import metadata
        prod = [f for f in LIBRARY.status()["files"]]
        main: dict[str, dict] = {}
        for f in prod:  # the library lists each product's manual first; a "…_marked" review copy is not the manual
            if f["product"] not in main or ("marked" in main[f["product"]]["name"].lower() and "marked" not in f["name"].lower()
                                            and not f["sub"].lower().count("images")):
                main[f["product"]] = f
        norm = {k: metadata._norm(k) for k in main}
        try:
            rows = metadata.load_sheet(metadata.default_sheet())
        except Exception:
            rows = []
        stage = self.stage_files()
        by_title = {metadata._norm(r.get("exp_doc", "")): r for r in rows if r.get("exp_doc")}
        for s in stage:
            # a file named by its Document Title is that sheet row; else the map / folder name
            row = (by_title.get(metadata._norm(s.get("title", ""))) if s.get("title") else None) or \
                (metadata.match(s["product"], s["product"], rows) if rows else None)
            keys = [k for k in ((row or {}).get("file"), (row or {}).get("model"), s["product"]) if k]
            hit, via = None, ""
            for n_k, k in enumerate(keys):
                nk = metadata._norm(k)
                hit = next((p for p, n in norm.items() if n and n == nk), None) or \
                    next((p for p, n in norm.items() if nk and len(nk) >= 3 and (n.startswith(nk) or nk.startswith(n) and len(n) >= 4)), None)
                if hit:
                    via = "Excel" if row and n_k < 2 else "name"
                    break
            s["prod"], s["via"] = (main[hit]["path"] if hit else ""), via
            s["model"] = (row or {}).get("model", "")
        return {"stage": stage, "prod": prod, "stage_dir": str(self.dir)}


    def prod_for_url(self, url: str) -> dict:
        """The prod PDF of a stage web guide, from its URL - the same pairing as a stage PDF's: the guide's
        product (…/projector/w2720i/en/page.html -> w2720i, en) looked up in the migration Excel (the map's model
        row -> its prod archive), else by name in the prod library. {} fields empty when nothing matches."""
        from urllib.parse import urlsplit, unquote
        from .. import metadata
        segs = [unquote(x) for x in urlsplit(url).path.split("/") if x]
        if segs and "." in segs[-1]:
            segs[-1] = segs[-1].rsplit(".", 1)[0]
        is_lang = lambda x: bool(re.fullmatch(r"[a-z]{2}([-_][a-zA-Z]{2,4})?", x))
        k = next((n for n, x in enumerate(segs) if n > 0 and is_lang(x)), None)
        lang = segs[k] if k is not None else ""
        # the product is the folder the language sits in; else every folder of the path, deepest first
        skip = {"content", "guide", "guides", "dam", "en", "html"}
        names = ([segs[k - 1]] if k else []) + [x for x in reversed(segs[:-1] if k is None else segs[:k - 1])
                                                if x.lower() not in skip and not is_lang(x)]
        prod = LIBRARY.status()["files"]
        main: dict[str, dict] = {}
        for f in prod:
            if f["product"] not in main or ("marked" in main[f["product"]]["name"].lower() and "marked" not in f["name"].lower()
                                            and not f["sub"].lower().count("images")):
                main[f["product"]] = f
        norm = {key: metadata._norm(key) for key in main}
        try:
            rows = metadata.load_sheet(metadata.default_sheet())
        except Exception:
            rows = []
        want = re.compile(rf"(^|[_\-\s]){re.escape(lang[:2])}([_\-\s]|$)", re.I) if lang else None
        for name in names:
            row = metadata.match(name, name, rows) if rows else None
            keys = [x for x in ((row or {}).get("file"), (row or {}).get("model"), name) if x]
            for n_k, key in enumerate(keys):
                nk = metadata._norm(key)
                exact = [p for p, n in norm.items() if n and n == nk]
                loose = [p for p, n in norm.items() if p not in exact and nk and len(nk) >= 3
                         and (n.startswith(nk) or nk.startswith(n) and len(n) >= 4)]
                hits = exact + loose
                if hits:
                    # the guide's language first (W2720i_FR_… for …/w2720i/fr/…), among every folder of the product;
                    # else the exact name, else the nearest
                    # (an exact name always before a near one: "pd2732u-timing" is PD2732U_timing_V0, not the
                    # manual PD2732U_EN_V0 that merely starts the same and happens to carry the language)
                    # A library name that only starts the guide's name (pd2732u for pd2732utiming) is a more general
                    # product: taken only when nothing else matches. One that goes on after it (w2720ifr for
                    # w2720i) is a variant of the same product - a language - and competes with the exact name.
                    variants = exact + [p for p in loose if norm[p].startswith(nk)]
                    tier = variants or loose
                    hit = next((p for p in tier if want and want.search(p)), tier[0])
                    f = main[hit]
                    return {"prod": f["path"], "name": f["name"], "size": f["size"], "library_product": hit,
                            "via": "Excel" if row and n_k < 2 else "name", "product": name, "lang": lang,
                            "model": (row or {}).get("model", "")}
        return {"prod": "", "product": names[0] if names else "", "lang": lang}

    def prod_for_urls(self, urls: list[str]) -> list[dict]:
        """prod_for_url, for many stage web-guide URLs at once: pasted or uploaded in bulk, each matched to its
        prod PDF the same way, so the reviewer can tick which pairs to validate."""
        seen, out = set(), []
        for u in urls:
            u = u.strip()
            if not u or u in seen:
                continue
            seen.add(u)
            try:
                r = self.prod_for_url(u)
            except Exception as e:
                r = {"prod": "", "product": "", "lang": "", "error": str(e)}
            out.append({"url": u, **r})
        return out


PAIRS = Pairs(PROJECT / "aem-map-pdfs", PROJECT / "stage-pdf")


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
            last = json.loads((self.dir / "metadata.json").read_text(encoding="utf-8"))
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
