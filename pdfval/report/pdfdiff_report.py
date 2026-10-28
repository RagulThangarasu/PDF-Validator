"""The PDF A ⇄ B report: one or many prod/stage PDF pairs, reported the same way as the Site A ⇄ Site
B report (sitediff_report) - same kept types (numbering, bullets, image alignment/size, icon wrap,
table header padding), same noise cuts, same clean per-finding screenshots. Built by wrapping each pair
as a single-topic "publication" and handing it to sitediff_report, so every PDF-vs-PDF report (one run
or a whole batch) shares one visual language and one set of noise-reduction rules instead of drifting
apart."""
from __future__ import annotations

import json
from pathlib import Path

from . import sitediff_report


def _pub(result: dict, out_dir: Path, *, name: str | None = None, error: str | None = None) -> dict:
    """One "publication" entry (sitediff_report's unit): a single PDF-vs-PDF run, its findings
    flattened out of their sections. `error`: the run never produced a result at all (crashed, stopped)
    - still listed, so a batch report accounts for every run, not just the ones that finished."""
    meta = (result or {}).get("meta") or {}
    base, cand = meta.get("baseline") or {}, meta.get("candidate") or {}
    pub_name = name or meta.get("name") or "PDF A ⇄ B"
    findings = [f for s in (result or {}).get("sections", []) for f in s.get("findings", [])]
    return {
        "product": pub_name, "a_pages": base.get("pages", 0), "b_pages": cand.get("pages", 0),
        "error": error, "only_a": [], "only_b": [],
        "sample": {"a": str(out_dir / "baseline.pdf"), "b": str(out_dir / "candidate.pdf")},
        "topics": [] if error else [{"name": pub_name, "error": None, "findings": findings,
                                      "dir": str(out_dir), "a": "", "b": ""}],
    }


def _wrap(result: dict, out_dir: Path) -> dict:
    meta = result.get("meta") or {}
    base, cand = meta.get("baseline") or {}, meta.get("candidate") or {}
    return {"a_root": base.get("path", ""), "b_root": cand.get("path", ""), "only_a": [], "only_b": [],
            "publications": [_pub(result, out_dir)]}


def _wrap_batch(pubs: list[dict]) -> dict:
    return {"a_root": "", "b_root": "", "only_a": [], "only_b": [], "publications": pubs}


_INTRO = ("A (the reference) and B (the one to check), compared page against page. Words, tables, "
          "pictures, type, colour and layout. Hyperlink targets are not compared.")


def build(result: dict, out_dir: str | Path, stem: str = "pdf-a-vs-b") -> dict:
    out = Path(out_dir)
    return sitediff_report.build(_wrap(result, out), out, stem=stem, title="PDF A ⇄ B", intro=_INTRO, unit="document")


def build_batch(jobs: list[dict], runs_dir: str | Path, out_dir: str | Path, stem: str = "pdf-a-vs-b") -> dict:
    """The same report as `build()`, over every PDF pair of a batch (the /pdfdiff page's "Two folders at
    once"): one row per publication, then every publication's differences with their own screenshots -
    instead of the batch's generic content-match-% table (batch_report), which knows nothing of this
    report's formatting-only scope. A run that is not done yet, or that errored, is still listed."""
    runs_dir = Path(runs_dir)
    pubs = []
    for j in sorted(jobs, key=lambda j: j["name"].lower()):
        run_dir = runs_dir / j["id"]
        results_path = run_dir / "results.json"
        if j["status"] != "done" or not results_path.is_file():
            pubs.append(_pub({}, run_dir, name=j["name"],
                             error=j.get("message") or f"Run {j['status']}: no result"))
            continue
        result = json.loads(results_path.read_text(encoding="utf-8"))
        pubs.append(_pub(result, run_dir, name=j["name"]))
    out = Path(out_dir)
    return sitediff_report.build(_wrap_batch(pubs), out, stem=stem, title="PDF A ⇄ B — batch", intro=_INTRO,
                                 unit="document")
