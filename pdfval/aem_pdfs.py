"""Download the PDF of every product map in AEM, one folder per product.

  python -m pdfval aem-pdfs [--out aem-map-pdfs] [--lang en] [--all-versions]

Each map (<search_root>/<lang>/<Brand>/<Category>/<product>/Maps/<map>.ditamap) records its last generated
PDF in jcr:content/pdfPath; every PDF in /content/dam/fmdita-outputs records its map in sourcePath. The PDF is
saved to <out>/<lang>/<Brand>/<Category>/<product>/<file>.pdf. A map without pdfPath gets the newest PDF
generated from it; --all-versions also saves every other PDF generated from the map (test / older presets).
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib import request as _rq

from .metadata import Aem

OUTPUTS = "/content/dam/fmdita-outputs"


def _outputs(aem: Aem) -> dict[str, list[tuple[str, str]]]:
    """{map DAM path: [(pdf path, modified)]} for every generated PDF."""
    hits = aem.get("/bin/querybuilder.json", path=OUTPUTS, type="dam:Asset", nodename="*.pdf",
                   **{"p.limit": "-1", "p.hits": "selective",
                      "p.properties": "jcr:path jcr:content/sourcePath jcr:content/jcr:lastModified"})["hits"]
    out: dict[str, list[tuple[str, str]]] = {}
    for h in hits:
        c = h.get("jcr:content") or {}
        src = c.get("sourcePath") or ""
        if src:
            out.setdefault(src, []).append((h["jcr:path"], c.get("jcr:lastModified", "")))
    return out


def _download(aem: Aem, src: str, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = _rq.Request(aem.author + quote(src), headers={"Authorization": aem.auth})
    tmp = dest.with_suffix(".part")
    with _rq.urlopen(req, timeout=300) as r, open(tmp, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    tmp.replace(dest)
    return dest.stat().st_size


def run(cfg: dict, out: str | Path, lang: str | None = "en", all_versions: bool = False, progress=print) -> list[dict]:
    aem = Aem(cfg)
    out = Path(out)
    root = aem.root.rstrip("/")
    maps = []
    for lg in ([lang] if lang else _langs(aem, root)):
        maps += aem.maps(lg)
    generated = _outputs(aem)
    # a PDF's sourcePath can be the map's old project path: match on the map file name as well
    by_name: dict[str, list[tuple[str, str]]] = {}
    for src, pdfs in generated.items():
        by_name.setdefault(src.rsplit("/", 1)[-1].lower(), []).extend(pdfs)

    def one(mp: str) -> dict:
        rel = mp[len(root) + 1:]                      # en/Consumer/Monitor/gw2291/Maps/gw2291.ditamap
        folder = out / re.sub(r"/Maps/[^/]+$", "", rel)
        rec = {"map": mp, "folder": str(folder), "files": [], "error": ""}
        try:
            latest = aem.get(f"{mp}/jcr:content.json").get("pdfPath") or ""
        except HTTPError as e:
            latest = ""
            rec["error"] = f"map: HTTP {e.code}"
        others = sorted(generated.get(mp, []) or by_name.get(mp.rsplit("/", 1)[-1].lower(), []), key=lambda x: x[1], reverse=True)
        picks = [latest] if latest else [p for p, _ in others[:1]]
        if all_versions:
            picks += [p for p, _ in others if p not in picks]
        if not picks:
            rec["error"] = rec["error"] or "no PDF generated for this map"
        if latest:  # the recorded PDF may have been deleted since: then the newest one generated from the map
            picks += [p for p, _ in others[:1] if p not in picks]
        for src in picks:
            name = src.rsplit("/", 1)[-1]
            main = not any(f["latest"] for f in rec["files"])
            if not main and not all_versions:
                break
            dest = folder / (name if main else f"other-outputs/{name}")
            try:
                size = dest.stat().st_size if dest.exists() else _download(aem, src, dest)
                rec["files"].append({"source": src, "path": str(dest), "size": size, "latest": main})
                rec["error"] = ""
            except Exception as e:
                rec["error"] = f"{name}: {e}"
        progress(f"{'OK ' if rec['files'] and not rec['error'] else 'ERR'} {rel}  {rec['error']}")
        return rec

    with ThreadPoolExecutor(6) as ex:
        recs = list(ex.map(one, maps))
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.json").write_text(json.dumps(recs, indent=1, ensure_ascii=False), encoding="utf-8")
    return recs


# ---------------------------------------------------------------- generate new PDFs

PROFILE_PRESETS = "/var/dxml/folderprofiles"
# brand folder (after the language) -> PDF preset; Arabic maps (ar-*) use "<preset> Arabic"
PRESET_BY_BRAND = [("education", "BenQ EDU With Image"), ("zowie", "Zowie"), ("inftylab", "INFTY"),
                   ("", "BenQ With Image")]  # Consumer, Business, BenQ, BenQ-consumer ...
RUNNING = {"queued", "in progress", "inprogress", "running", "waiting", "processing", "started"}


def preset_for(rel: str) -> str:
    """Preset title for a map path relative to the search root (en/Consumer/Monitor/gw2291/Maps/x.ditamap)."""
    lang, brand = (rel.split("/") + ["", ""])[:2]
    title = next(t for key, t in PRESET_BY_BRAND if key in brand.lower())
    return f"{title} Arabic" if lang.lower().startswith("ar") else title


def _preset_ids(aem: Aem) -> dict[str, str]:
    """{preset title: preset id} of every folder-profile output preset."""
    out = {}
    for prof, v in aem.get(f"{PROFILE_PRESETS}.1.json").items():
        if not isinstance(v, dict) or prof.startswith("jcr:"):
            continue
        try:
            presets = aem.get(f"{PROFILE_PRESETS}/{prof}/presets.1.json")
        except HTTPError:
            continue
        for pid, p in presets.items():
            if isinstance(p, dict) and p.get("fmdita-outputTitle"):
                out.setdefault(p["fmdita-outputTitle"], pid)
    return out


def _outputs_of(aem: Aem, mp: str) -> list[dict]:
    return aem.get("/bin/publishlistener", operation="PUBLISHBEACON", source=mp).get("outputs", [])


def _start(aem: Aem, mp: str, preset_id: str) -> None:
    """Ask AEM Guides to generate the map with the preset - the same GET the map console's Generate sends."""
    r = aem.get("/bin/publishlistener", operation="GENERATEOUTPUT", source=mp, outputName=preset_id)
    res = r.get(preset_id) or {}
    if not res.get("success"):
        raise RuntimeError(f"AEM refused the generation: {res or r}")


def generate(cfg: dict, out: str | Path, lang: str | None = None, parallel: int = 3, timeout: int = 1800,
             progress=print) -> list[dict]:
    """Generate a new PDF of every map with its brand's preset, wait for it and download it into the map's folder
    (the PDFs downloaded there before are replaced)."""
    import time
    aem = Aem(cfg)
    out = Path(out)
    root = aem.root.rstrip("/")
    ids = _preset_ids(aem)
    maps = [m for lg in ([lang] if lang else _langs(aem, root)) for m in aem.maps(lg)]

    def one(mp: str) -> dict:
        rel = mp[len(root) + 1:]
        folder = out / re.sub(r"/Maps/[^/]+$", "", rel)
        title = preset_for(rel)
        rec = {"map": mp, "folder": str(folder), "preset": title, "files": [], "error": ""}
        if title not in ids:
            rec["error"] = f"preset {title!r} not found in AEM"
            progress(f"ERR {rel}  {rec['error']}")
            return rec
        try:
            before = {o.get("jobId") for o in _outputs_of(aem, mp)}
            t0 = time.time()
            _start(aem, mp, ids[title])
            job = None
            while time.time() - t0 < timeout:
                time.sleep(10)
                new = [o for o in _outputs_of(aem, mp) if o.get("jobId") not in before and o.get("outputSetting") == title]
                if new:
                    job = new[0]
                    if job.get("outputStatus", "").lower() not in RUNNING:
                        break
            if not job:
                raise TimeoutError("AEM did not start the generation")
            status = job.get("outputStatus", "")
            if status.lower() in RUNNING:
                raise TimeoutError(f"still {status} after {timeout // 60} min")
            if status.lower() != "finished" or not job.get("outputPath"):
                raise RuntimeError(f"generation {status or 'failed'}" + (" (DITA-OT failure)" if job.get("ditaotFaliure") else ""))
            src = job["outputPath"]
            for old in folder.glob("*.pdf"):  # the PDFs downloaded before: replaced by the new one
                old.unlink()
            dest = folder / src.rsplit("/", 1)[-1]
            rec["files"].append({"source": src, "path": str(dest), "size": _download(aem, src, dest), "latest": True,
                                 "job": job.get("jobId"), "errors_in_log": bool(job.get("errorsExist")),
                                 "log": job.get("ditaotLogFile", "")})
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {e}"
        progress(f"{'OK ' if rec['files'] else 'ERR'} [{title}] {rel}  {rec['error']}")
        return rec

    with ThreadPoolExecutor(parallel) as ex:
        recs = list(ex.map(one, maps))
    out.mkdir(parents=True, exist_ok=True)
    (out / "generated.json").write_text(json.dumps(recs, indent=1, ensure_ascii=False), encoding="utf-8")
    return recs


def _langs(aem: Aem, root: str) -> list[str]:
    tree = aem.get(f"{root}.1.json")
    return sorted(k for k, v in tree.items() if isinstance(v, dict) and not k.startswith(("jcr:", "rep:")))
