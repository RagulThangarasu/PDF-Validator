"""One report covering both kinds of validation: the PDF comparison (prod PDF vs stage PDF) and the
AEM site validation (prod PDF vs the live guide), side by side, one row per product.

The two run as separate jobs - often in separate batches, or outside a batch altogether - so this
report does not take a batch. It walks every run in `runs/`, groups them by the product name the run
was given, and keeps the **newest run of each kind** per product: that is the current state of that
product. A newer run that failed is shown as failed, not quietly replaced by an older success.

Each side gets its content match %, its issue counts and the breakdown by category (content, image,
table, hyperlink, layout ...), so a product whose PDF is fine but whose site drops pictures is
obvious without opening either run. PDF + CSV + JSON, like the batch report.
"""
from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from html import escape
from pathlib import Path

import pymupdf

from .batch_report import CSS, GREEN, GREY, RED, _cats, _dur, _img, _pct, rows

SIDES = ("pdf", "site")
SIDE_LABEL = {"pdf": "PDF validation", "site": "AEM site validation"}


def _jobs(runs_dir: Path) -> list[dict]:
    out = []
    for f in sorted(runs_dir.glob("*/job.json")):
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # a run still being written, or a half-deleted one
        if j.get("id") and j.get("name"):
            out.append(j)
    return out


# "SW272_EN vs bda93743_Monitor SW272 User Manual (4)" (an ad-hoc PDF run, named after the two files)
# and "sw272 (en)" (a site run, named after the guide) are the same product. Both names are reduced to
# a key: what comes before " vs ", without the language marker, the version and the punctuation.
_VS = re.compile(r"\s+vs\.?\s+", re.I)
_LANG = re.compile(r"[\s_-]*\((?:[a-z]{2}(?:[-_][a-z]{2})?)\)\s*$", re.I)        # "... (en)", "... (zh-tw)"
_SUFFIX = re.compile(r"(?:[\s_-](?:[a-z]{2}(?:[-_][a-z]{2})?|v\d+(?:\.\d+)*))+$", re.I)  # "_EN", "-zh_TW", "_V1.03"


def product_key(name: str) -> str:
    """The product a run's name refers to, so a PDF run and a site run of the same product meet.

    Conservative on purpose: it only drops what is known to differ between the two naming styles. Two
    products whose names really are different keep different keys ("pd30" and "pd30-timing" do not merge).
    """
    base = _VS.split(name or "", 1)[0].strip()
    base = _LANG.sub("", base)
    base = _SUFFIX.sub("", base)
    return re.sub(r"[^a-z0-9]+", "", base.lower()) or (name or "").strip().lower()


def _side(job: dict) -> str:
    """Which validation this run is: the site crawl, or the PDF comparison."""
    return "site" if (job.get("mode") or job.get("options", {}).get("mode")) == "html" else "pdf"


def _newest(jobs: list[dict]) -> dict:
    """The run started last (its id carries the timestamp; `created` is the readable form)."""
    return max(jobs, key=lambda j: (j.get("created") or "", j.get("id") or ""))


def pick(jobs: list[dict]) -> dict[str, dict[str, dict]]:
    """product -> side -> the newest run of that side."""
    by: dict[str, dict[str, list]] = {}
    for j in jobs:
        by.setdefault(product_key(j["name"]), {}).setdefault(_side(j), []).append(j)
    return {key: {side: _newest(js) for side, js in sides.items()} for key, sides in by.items()}


def pairs(runs_dir: str | Path) -> list[dict]:
    """One row per product: {"product", "pdf": row|None, "site": row|None}, worst first.

    A product validated only one way keeps the other side empty - that is a real finding in itself
    (the site was never checked, or the PDF never was), not something to hide.
    """
    runs_dir = Path(runs_dir)
    picked = pick(_jobs(runs_dir))
    # batch_report.rows() does the per-run arithmetic (match %, band, image verdict, run time); feed it
    # every chosen run at once so a row here is exactly a row of the batch report
    chosen = [(name, side, job) for name, sides in picked.items() for side, job in sides.items()]
    built = rows([job for _, _, job in chosen], runs_dir)
    by_run = {r["run"]: r for r in built}
    out = []
    for key, sides in picked.items():
        # the site run's name is the guide's own ("sw272 (en)"); a PDF run's is "<prod> vs <stage>"
        shown = (sides.get("site") or sides.get("pdf"))["name"]
        row = {"product": shown, "key": key,
               **{s: by_run.get(sides[s]["id"]) if s in sides else None for s in SIDES}}
        pcts = [row[s]["match_pct"] for s in SIDES if row[s] and row[s]["match_pct"] is not None]
        row["worst"] = min(pcts) if pcts else None
        out.append(row)
    out.sort(key=lambda r: (r["worst"] is None, r["worst"] if r["worst"] is not None else 0, r["product"].lower()))
    return out


def totals(ps: list[dict]) -> dict:
    t = {"products": len(ps), "both": sum(1 for p in ps if p["pdf"] and p["site"])}
    for s in SIDES:
        rs = [p[s] for p in ps if p[s]]
        done = [r for r in rs if r["match_pct"] is not None]
        pcts = sorted(r["match_pct"] for r in done)
        t[s] = {"runs": len(rs), "done": len(done), "error": sum(r["status"] == "error" for r in rs),
                "pending": sum(r["status"] in ("queued", "running") for r in rs),
                "missing": len(ps) - len(rs),  # products never validated this way
                "average": round(sum(pcts) / len(pcts), 2) if pcts else None,
                "median": pcts[len(pcts) // 2] if pcts else None,
                "pass": sum(r["band"] == "pass" for r in rs), "warn": sum(r["band"] == "warn" for r in rs),
                "fail": sum(r["band"] == "fail" for r in rs),
                "genuine": sum(r["genuine"] or 0 for r in rs)}
    return t


def _cell(r: dict | None) -> str:
    """One side of a product's row: match %, issue breakdown, image verdict - or why there is nothing."""
    if r is None:
        return f'<span style="color:{GREY}">not validated</span>'
    if r["status"] != "done":
        return (f'<span style="color:{RED}">{escape(r["status"])}</span>'
                f'<br/><span class="muted">{escape((r["message"] or "")[:140])}</span>')
    return f'{_pct(r)}<br/><span class="muted">{_cats(r)}</span>'


def build_pdf(ps: list[dict], out: str | Path) -> Path:
    t = totals(ps)
    fmt = lambda v: "—" if v is None else f"{v:.2f} %"
    html = [f"""<h1>PDF and AEM site validation — combined report</h1>
<p class="muted">{datetime.now().strftime('%Y-%m-%d %H:%M')} · one row per product, lowest content match first.
Each side is that product's most recent run of its kind. Content match = share of the prod words found
unchanged (missing, changed and extra words count against it).</p>
<table><tr><th>Validation</th><th class="n">Products</th><th class="n">Finished</th><th class="n">Failed</th>
<th class="n">Still running</th><th class="n">Never run</th><th class="n">Pass</th><th class="n">Warn</th>
<th class="n">Fail</th><th class="n">Average</th><th class="n">Median</th></tr>"""]
    for s in SIDES:
        d = t[s]
        html.append(f'<tr><td><b>{SIDE_LABEL[s]}</b></td><td class="n">{d["runs"]}</td><td class="n">{d["done"]}</td>'
                    f'<td class="n" style="color:{RED if d["error"] else "#1d2330"}">{d["error"]}</td>'
                    f'<td class="n">{d["pending"]}</td><td class="n">{d["missing"]}</td>'
                    f'<td class="n" style="color:{GREEN}">{d["pass"]}</td><td class="n">{d["warn"]}</td>'
                    f'<td class="n" style="color:{RED if d["fail"] else "#1d2330"}">{d["fail"]}</td>'
                    f'<td class="n"><b>{fmt(d["average"])}</b></td><td class="n">{fmt(d["median"])}</td></tr>')
    html.append(f"""</table>
<p class="muted">{t['both']} of {t['products']} product(s) validated both ways.</p>
<h2>Products</h2>
<table><tr><th class="n">#</th><th>Product</th><th>{SIDE_LABEL['pdf']}</th><th>Images</th>
<th>{SIDE_LABEL['site']}</th><th>Images</th></tr>""")
    for k, p in enumerate(ps, 1):
        html.append(f'<tr><td class="n">{k}</td><td><b>{escape(p["product"])}</b></td>'
                    f'<td>{_cell(p["pdf"])}</td><td>{_img(p["pdf"]) if p["pdf"] else ""}</td>'
                    f'<td>{_cell(p["site"])}</td><td>{_img(p["site"]) if p["site"] else ""}</td></tr>')
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
    return out


_CSV_HEAD = ["Status", "Content match %", "Content result", "Issues (genuine)", "Issues by category",
             "Image issues (PASS/FAIL)", "Number of image issues", "Run", "Message"]


def _csv_cells(r: dict | None) -> list:
    if r is None:
        return ["not validated", "", "", "", "", "", "", "", ""]
    cats = " · ".join(f"{c} {v.get('total')}" for c, v in (r["by_category"] or {}).items() if v.get("total"))
    return [r["status"], "" if r["match_pct"] is None else r["match_pct"], r["band"], r["genuine"], cats,
            r["image_result"].upper(), "" if r["image_issues"] is None else r["image_issues"], r["run"], r["message"]]


def write_csv(ps: list[dict], out: str | Path) -> Path:
    out = Path(out)
    with open(out, "w", newline="", encoding="utf-8-sig") as fh:  # opens in Excel
        w = csv.writer(fh)
        w.writerow(["Product"] + [f"{SIDE_LABEL[s]} — {h}" for s in SIDES for h in _CSV_HEAD])
        for p in ps:
            w.writerow([p["product"]] + [c for s in SIDES for c in _csv_cells(p[s])])
    return out


def build(runs_dir: str | Path, out_dir: str | Path) -> tuple[Path, Path]:
    ps = pairs(runs_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf = build_pdf(ps, out_dir / "combined-validation-report.pdf")
    csv_ = write_csv(ps, out_dir / "combined-validation-report.csv")
    (out_dir / "combined-validation-report.json").write_text(
        json.dumps({"totals": totals(ps), "products": ps}, indent=1), encoding="utf-8")
    return pdf, csv_
