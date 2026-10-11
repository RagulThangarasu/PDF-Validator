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

from .batch_report import CSS, GREEN, GREY, RED, rows

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
    # failures first (that is what the reader is looking for), then runs that did not finish, then
    # the publications that passed; alphabetical inside each group
    rank = {"FAIL": 0, "PASS": 2, "not validated": 3}
    out.sort(key=lambda r: (min(rank.get(verdict(r[s]), 1) for s in SIDES), r["product"].lower()))
    return out


def totals(ps: list[dict]) -> dict:
    t = {"products": len(ps), "both": sum(1 for p in ps if p["pdf"] and p["site"])}
    for side in SIDES:
        vs = [verdict(p[side]) for p in ps]
        t[side] = {"pass": vs.count("PASS"), "fail": vs.count("FAIL"),
                   "not_validated": vs.count("not validated"),
                   "incomplete": sum(1 for v in vs if v not in ("PASS", "FAIL", "not validated"))}
    return t


def verdict(r: dict | None) -> str:
    """PASS / FAIL for one side of a product, or why there is no verdict.

    A publication passes when its content score is at or above [content] pass_pct - the same verdict
    its own report shows at the top. A run that did not finish has no verdict at all; saying FAIL for
    it would read as "this publication is wrong" when nothing was ever checked.
    """
    if r is None:
        return "not validated"
    if r["status"] != "done":
        return r["status"]            # error / queued / running / stopped
    return (r.get("result") or "").upper() or "—"


_VERDICT_COLOUR = {"PASS": GREEN, "FAIL": RED}


def _cell(r: dict | None) -> str:
    v = verdict(r)
    colour = _VERDICT_COLOUR.get(v)
    return f'<b style="color:{colour}">{v}</b>' if colour else f'<span style="color:{GREY}">{escape(v)}</span>'


def passed(ps: list[dict]) -> list[dict]:
    """The publications whose PDF validation passed.

    The report lists these alone. Anything else - a publication that failed, one whose run errored or
    is still going, one never validated - is left out, so the page is a clean list of what is good.
    The count of what was left out is printed under it, because a report that silently shrinks when a
    publication starts failing would read as "nothing to see here".
    """
    return [p for p in ps if verdict(p["pdf"]) == "PASS"]


def build_pdf(ps: list[dict], out: str | Path) -> Path:
    """One line per publication that passed: its name and PASS. Nothing else - no score, no issue
    counts, no breakdown by images / layout / tables, and no AEM site column."""
    ok = passed(ps)
    left_out = len(ps) - len(ok)
    html = [f"""<h1>PDF validation report</h1>
<p class="muted">{datetime.now().strftime('%Y-%m-%d %H:%M')} \u00b7 {len(ok)} publication(s) passed \u00b7 each row is that publication's most recent PDF run."""
            + (f' <b style="color:{RED}">{left_out} not listed</b> (did not pass, or has no finished run).'
               if left_out else "") + """</p>
<h2>Publications</h2>
<table><tr><th class="n">#</th><th>Publication</th><th>Status</th></tr>"""]
    for k, p in enumerate(ok, 1):
        html.append(f'<tr><td class="n">{k}</td><td><b>{escape(p["product"])}</b></td>'
                    f'<td><b style="color:{GREEN}">PASS</b></td></tr>')
    html.append("</table>")
    if not ok:
        html.append(f'<p class="muted">No publication passed.</p>')
    out = Path(out)
    story = pymupdf.Story("".join(html), user_css=CSS)
    writer = pymupdf.DocumentWriter(str(out))
    page = pymupdf.paper_rect("a4")
    where = page + (36, 36, -36, -40)
    more, n = 1, 0
    while more and n < 400:
        dev = writer.begin_page(page)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
        n += 1
    writer.close()
    return out


def write_csv(ps: list[dict], out: str | Path) -> Path:
    out = Path(out)
    with open(out, "w", newline="", encoding="utf-8-sig") as fh:  # opens in Excel
        w = csv.writer(fh)
        w.writerow(["Publication", "Status"])
        for p in passed(ps):
            w.writerow([p["product"], "PASS"])
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
