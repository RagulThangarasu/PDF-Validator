"""Consolidated report of a batch: one row per product (publication) with its content match %, the
section results, critical / genuine / CSS issue counts and how long the run took. PDF + CSV."""
from __future__ import annotations

import csv
import json
from datetime import datetime
from html import escape
from pathlib import Path

import pymupdf

GREEN, AMBER, RED, GREY = "#16a34a", "#b45309", "#d92d20", "#64748b"

CSS = """
* { font-family: sans-serif; font-size: 8.5px; color: #1d2330; }
h1 { font-size: 18px; margin: 0 0 2px 0; } h2 { font-size: 12px; margin: 14px 0 4px 0; }
table { border-collapse: collapse; width: 100%; }
th, td { border-bottom: 0.5px solid #d0d4da; padding: 3px 4px; text-align: left; vertical-align: top; }
th { background-color: #f1f3f6; font-weight: bold; }
.muted { color: #6a7282; } .n { text-align: right; }
"""


def _finished(job: dict, run_dir: Path) -> datetime | None:
    if job.get("finished"):
        return datetime.fromisoformat(job["finished"])
    try:
        return datetime.fromtimestamp((run_dir / "job.json").stat().st_mtime)
    except OSError:
        return None


def _images(job: dict, run_dir: Path) -> dict | None:
    """The run's image verdict {"issues", "result"}: from its summary, else (a run made before the verdict was
    kept) counted from its results."""
    img = (job.get("summary") or {}).get("images")
    if img:
        return img
    try:
        from .writer import image_summary
        return image_summary(json.loads((run_dir / "results.json").read_text(encoding="utf-8")))
    except Exception:
        return None


def rows(jobs: list[dict], runs_dir: Path) -> list[dict]:
    """One row per run of the batch, worst content match first; runs that did not finish last."""
    out = []
    for j in jobs:
        sm = j.get("summary") or {}
        c = sm.get("content") or {}
        pct = c.get("match_pct")
        pass_pct, warn_pct = c.get("pass_pct", 98.0), c.get("warn_pct", 90.0)
        band = ("" if pct is None else "pass" if pct >= pass_pct else "warn" if pct >= warn_pct else "fail")
        img = _images(j, runs_dir / j["id"]) if j["status"] == "done" else None
        end = _finished(j, runs_dir / j["id"]) if j["status"] == "done" else None
        secs = (end - datetime.fromisoformat(j["created"])).total_seconds() if end else None
        out.append({
            "product": j["name"], "status": j["status"], "run": j["id"], "match_pct": pct, "band": band,
            "sections": sm.get("sections"), "sec_fail": sm.get("fail"), "sec_warn": sm.get("warn"), "sec_pass": sm.get("pass"),
            "critical": (sm.get("critical") or {}).get("total"), "genuine": (sm.get("genuine") or {}).get("total"),
            "css": (sm.get("css") or {}).get("issues"), "missing_words": c.get("missing_words"),
            "extra_words": c.get("extra_words"), "words": c.get("baseline_words"),
            "seconds": round(secs) if secs is not None else None,
            "image_issues": img["issues"] if img else None, "image_result": img["result"] if img else "",
            "message": "" if j["status"] == "done" else j.get("message", ""),
        })
    out.sort(key=lambda r: (r["match_pct"] is None, r["match_pct"] if r["match_pct"] is not None else 0, r["product"].lower()))
    return out


def totals(rs: list[dict]) -> dict:
    done = [r for r in rs if r["match_pct"] is not None]
    pcts = sorted(r["match_pct"] for r in done)
    words = sum(r["words"] or 0 for r in done)
    return {"products": len(rs), "done": len(done), "error": sum(r["status"] == "error" for r in rs),
            "pending": sum(r["status"] in ("queued", "running") for r in rs),
            "average": round(sum(pcts) / len(pcts), 2) if pcts else None,
            "median": pcts[len(pcts) // 2] if pcts else None,
            # all words of all products together: big manuals count for more than a one-page sheet
            "weighted": round(sum((r["match_pct"] or 0) * (r["words"] or 0) for r in done) / words, 2) if words else None,
            "pass": sum(r["band"] == "pass" for r in rs), "warn": sum(r["band"] == "warn" for r in rs),
            "fail": sum(r["band"] == "fail" for r in rs),
            "critical": sum(r["critical"] or 0 for r in rs), "genuine": sum(r["genuine"] or 0 for r in rs),
            "seconds": sum(r["seconds"] or 0 for r in rs)}


def _dur(s) -> str:
    return "" if s is None else f"{s // 60:.0f} min {s % 60:02.0f} s" if s >= 60 else f"{s:.0f} s"


def _pct(r: dict) -> str:
    if r["match_pct"] is None:
        return f'<span style="color:{GREY}">—</span>'
    col = {"pass": GREEN, "warn": AMBER, "fail": RED}[r["band"]]
    return f'<b style="color:{col}">{r["match_pct"]:.2f} %</b>'


def _img(r: dict) -> str:
    if r["image_result"] == "pass":
        return f'<b style="color:{GREEN}">PASS</b>'
    if r["image_result"] == "fail":
        return f'<b style="color:{RED}">FAIL</b> ({r["image_issues"]})'
    return ""


def build_pdf(batch: str, rs: list[dict], out: str | Path) -> Path:
    t = totals(rs)
    fmt = lambda v: "—" if v is None else f"{v:.2f} %"
    html = [f"""<h1>Batch comparison — consolidated report</h1>
<p class="muted">{escape(batch)} · {datetime.now().strftime('%Y-%m-%d %H:%M')} · prod vs stage, one row per product, lowest content match first.
Content match = share of the prod words found unchanged in stage (missing, changed and extra words count against it).</p>
<table><tr><th>Products</th><th>Finished</th><th>Failed to run</th><th>Still running</th><th>Average match</th><th>Median match</th>
<th>Run time (sum)</th></tr>
<tr><td>{t['products']}</td><td>{t['done']}</td><td style="color:{RED if t['error'] else '#1d2330'}">{t['error']}</td><td>{t['pending']}</td>
<td><b>{fmt(t['average'])}</b></td><td>{fmt(t['median'])}</td><td>{_dur(t['seconds'])}</td></tr></table>
<h2>Products</h2>
<table><tr><th class="n">#</th><th>Product</th><th class="n">Content match</th><th class="n">CSS issues</th>
<th>Image issues</th><th class="n">Run time</th><th>Run</th></tr>"""]
    for k, r in enumerate(rs, 1):
        note = f'<br/><span style="color:{RED}">{escape(r["status"])}: {escape(r["message"][:160])}</span>' if r["message"] else ""
        html.append(f'<tr><td class="n">{k}</td><td>{escape(r["product"])}{note}</td><td class="n">{_pct(r)}</td>'
                    f'<td class="n">{r["css"] if r["css"] is not None else ""}</td><td>{_img(r)}</td><td class="n">{_dur(r["seconds"])}</td>'
                    f'<td class="muted">{escape(r["run"])}</td></tr>')
    html.append("</table>")
    out = Path(out)
    story = pymupdf.Story("".join(html), user_css=CSS)
    writer = pymupdf.DocumentWriter(str(out))
    page = pymupdf.paper_rect("a4-l")
    where = page + (28, 28, -28, -36)
    more, n = 1, 0
    while more and n < 400:
        dev = writer.begin_page(page)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
        n += 1
    writer.close()
    doc = pymupdf.open(out)
    for k, pg in enumerate(doc):
        pg.insert_text((page.width - 90, page.height - 16), f"Page {k + 1} / {len(doc)}", fontsize=7, color=(0.42, 0.45, 0.51))
    doc.saveIncr()
    return out


def _story_pdf(html: str, out: Path, landscape: bool = True) -> Path:
    story = pymupdf.Story(html, user_css=CSS)
    writer = pymupdf.DocumentWriter(str(out))
    page = pymupdf.paper_rect("a4-l" if landscape else "a4")
    where = page + (28, 28, -28, -36)
    more, n = 1, 0
    while more and n < 400:
        dev = writer.begin_page(page)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
        n += 1
    writer.close()
    doc = pymupdf.open(out)
    for k, pg in enumerate(doc):
        pg.insert_text((page.width - 90, page.height - 16), f"Page {k + 1} / {len(doc)}", fontsize=7, color=(0.42, 0.45, 0.51))
    doc.saveIncr()
    return out


def image_rows(rs: list[dict]) -> list[dict]:
    """The publications by image result: FAIL first (most image issues first), then PASS, then not finished."""
    order = {"fail": 0, "pass": 1, "": 2}
    return sorted(rs, key=lambda r: (order.get(r["image_result"], 2), -(r["image_issues"] or 0), r["product"].lower()))


def build_image_pdf(batch: str, rs: list[dict], out: str | Path) -> Path:
    """Consolidated image report: one row per publication - PASS (no image issue; no image report) or FAIL
    (the number of image issues; its image report holds them)."""
    rs = image_rows(rs)
    n_fail = sum(r["image_result"] == "fail" for r in rs)
    n_pass = sum(r["image_result"] == "pass" for r in rs)
    html = [f"""<h1>Batch comparison — consolidated image report</h1>
<p class="muted">{escape(batch)} · {datetime.now().strftime('%Y-%m-%d %H:%M')} · prod vs stage, one row per publication.
PASS = no image issue (no image report for that publication). FAIL = image issues; each is shown in the publication's image report.</p>
<table><tr><th>Publications</th><th>Image PASS</th><th>Image FAIL</th><th>Not finished</th><th>Image issues (total)</th></tr>
<tr><td>{len(rs)}</td><td style="color:{GREEN}"><b>{n_pass}</b></td><td style="color:{RED if n_fail else '#1d2330'}"><b>{n_fail}</b></td>
<td>{len(rs) - n_pass - n_fail}</td><td>{sum(r['image_issues'] or 0 for r in rs)}</td></tr></table>
<h2>Publications</h2>
<table><tr><th class="n">#</th><th>Publication</th><th>Content match %</th><th>Image issues</th><th class="n">Number of image issues</th>
<th>Image report</th><th>Run</th></tr>"""]
    for k, r in enumerate(rs, 1):
        res = r["image_result"]
        verdict = (f'<b style="color:{GREEN}">PASS</b>' if res == "pass" else f'<b style="color:{RED}">FAIL</b>' if res == "fail"
                   else f'<span style="color:{GREY}">{escape(r["status"])}</span>')
        report = "" if res != "fail" else "image report"
        pct = "" if r["match_pct"] is None else f'{r["match_pct"]:.2f}%'
        html.append(f'<tr><td class="n">{k}</td><td>{escape(r["product"])}</td>'
                    f'<td class="n">{pct}</td><td>{verdict}</td>'
                    f'<td class="n">{"" if r["image_issues"] is None else r["image_issues"]}</td>'
                    f'<td class="muted">{report if res == "fail" else ("not needed" if res == "pass" else "")}</td>'
                    f'<td class="muted">{escape(r["run"])}</td></tr>')
    html.append("</table>")
    return _story_pdf("".join(html), Path(out))


def write_image_csv(rs: list[dict], out: str | Path) -> Path:
    out = Path(out)
    with open(out, "w", newline="", encoding="utf-8-sig") as fh:  # opens in Excel
        w = csv.writer(fh)
        w.writerow(["Publication", "Content match %", "Image issues (PASS/FAIL)", "Number of image issues", "Image report", "Status", "Run"])
        for r in image_rows(rs):
            res = r["image_result"]
            w.writerow([r["product"], "" if r["match_pct"] is None else r["match_pct"], res.upper(), "" if r["image_issues"] is None else r["image_issues"],
                        "yes" if res == "fail" else "not needed" if res == "pass" else "", r["status"], r["run"]])
    return out


def build_images(batch: str, jobs: list[dict], runs_dir: str | Path, out_dir: str | Path) -> tuple[Path, Path]:
    rs = rows(jobs, Path(runs_dir))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    return (build_image_pdf(batch, rs, out_dir / f"{batch}-image-consolidated.pdf"),
            write_image_csv(rs, out_dir / f"{batch}-image-consolidated.csv"))


def write_csv(rs: list[dict], out: str | Path) -> Path:
    out = Path(out)
    with open(out, "w", newline="", encoding="utf-8-sig") as fh:  # opens in Excel
        w = csv.writer(fh)
        w.writerow(["Product", "Status", "Content match %", "Content result", "CSS issues", "Image issues (PASS/FAIL)",
                    "Number of image issues", "Run time (s)", "Run", "Message"])
        for r in rs:
            w.writerow([r["product"], r["status"], "" if r["match_pct"] is None else r["match_pct"], r["band"],
                        r["css"], r["image_result"].upper(), "" if r["image_issues"] is None else r["image_issues"],
                        r["seconds"], r["run"], r["message"]])
    return out


def build(batch: str, jobs: list[dict], runs_dir: str | Path, out_dir: str | Path) -> tuple[Path, Path]:
    rs = rows(jobs, Path(runs_dir))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf = build_pdf(batch, rs, out_dir / f"{batch}-consolidated.pdf")
    csv_ = write_csv(rs, out_dir / f"{batch}-consolidated.csv")
    (out_dir / f"{batch}-consolidated.json").write_text(json.dumps({"totals": totals(rs), "rows": rs}, indent=1), encoding="utf-8")
    return pdf, csv_
