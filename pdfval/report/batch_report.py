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


def rows(jobs: list[dict], runs_dir: Path) -> list[dict]:
    """One row per run of the batch, worst content match first; runs that did not finish last."""
    out = []
    for j in jobs:
        sm = j.get("summary") or {}
        c = sm.get("content") or {}
        pct = c.get("match_pct")
        pass_pct, warn_pct = c.get("pass_pct", 98.0), c.get("warn_pct", 90.0)
        band = ("" if pct is None else "pass" if pct >= pass_pct else "warn" if pct >= warn_pct else "fail")
        end = _finished(j, runs_dir / j["id"]) if j["status"] == "done" else None
        secs = (end - datetime.fromisoformat(j["created"])).total_seconds() if end else None
        out.append({
            "product": j["name"], "status": j["status"], "run": j["id"], "match_pct": pct, "band": band,
            "sections": sm.get("sections"), "sec_fail": sm.get("fail"), "sec_warn": sm.get("warn"), "sec_pass": sm.get("pass"),
            "critical": (sm.get("critical") or {}).get("total"), "genuine": (sm.get("genuine") or {}).get("total"),
            "css": (sm.get("css") or {}).get("issues"), "missing_words": c.get("missing_words"),
            "extra_words": c.get("extra_words"), "words": c.get("baseline_words"),
            "seconds": round(secs) if secs is not None else None,
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
<th class="n">Run time</th><th>Run</th></tr>"""]
    for k, r in enumerate(rs, 1):
        note = f'<br/><span style="color:{RED}">{escape(r["status"])}: {escape(r["message"][:160])}</span>' if r["message"] else ""
        html.append(f'<tr><td class="n">{k}</td><td>{escape(r["product"])}{note}</td><td class="n">{_pct(r)}</td>'
                    f'<td class="n">{r["css"] if r["css"] is not None else ""}</td><td class="n">{_dur(r["seconds"])}</td>'
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


def write_csv(rs: list[dict], out: str | Path) -> Path:
    out = Path(out)
    with open(out, "w", newline="", encoding="utf-8-sig") as fh:  # opens in Excel
        w = csv.writer(fh)
        w.writerow(["Product", "Status", "Content match %", "Content result", "CSS issues", "Run time (s)", "Run", "Message"])
        for r in rs:
            w.writerow([r["product"], r["status"], "" if r["match_pct"] is None else r["match_pct"], r["band"],
                        r["css"], r["seconds"], r["run"], r["message"]])
    return out


def build(batch: str, jobs: list[dict], runs_dir: str | Path, out_dir: str | Path) -> tuple[Path, Path]:
    rs = rows(jobs, Path(runs_dir))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf = build_pdf(batch, rs, out_dir / f"{batch}-consolidated.pdf")
    csv_ = write_csv(rs, out_dir / f"{batch}-consolidated.csv")
    (out_dir / f"{batch}-consolidated.json").write_text(json.dumps({"totals": totals(rs), "rows": rs}, indent=1))
    return pdf, csv_
