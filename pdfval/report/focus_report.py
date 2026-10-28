"""One report over every publication, carrying only the checks asked for - nothing else.

The full reports answer "what is different in this publication"; this one answers "where does this
particular defect occur, across the whole set". It reads the finished runs under runs/ (the newest run
of each publication) and keeps only the findings whose type is in CATEGORIES below, grouped by defect
first and publication second, so a reviewer works through one kind of problem at a time.

The categories are the list that was asked for:

    numbering     list numbering that differs - the style (a,b,c vs 1,2,3), the format (1. vs 1) vs
                  (1)), and a sequence that does not run 1,2,3,4
    bullets       a bullet point missing (or added) in a section
    side-by-side  a layout that was side by side in prod and is stacked in stage; pictures combined
                  into one, or coming in another order
    alignment     a picture aligned differently from prod (prod centred, stage left), and a picture
                  not centred in its content box against the design spec
    icons         an icon that is not on the same line as its text - it sits above it, or is gone
    tables        a table breaking up: split in two, merged, rows split or merged, text running across
                  a cell border, a cell border not drawn

Everything else a run found - text differences, fonts, sizes, TOC, images smaller/bigger, sections
missing - is deliberately absent. The run's own reports still carry all of it.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime
from html import escape
from pathlib import Path

import pymupdf

from .batch_report import CSS, GREEN, GREY, RED

AMBER = "#b45309"

# (id, heading, the finding types it collects). Order is the order of the report.
CATEGORIES: list[tuple[str, str, set[str]]] = [
    ("numbering", "List numbering differs (1, 2, 3, 4 …)",
     {"numbering style", "numbering format", "numbering sequence", "list level", "marker above"}),
    ("bullets", "Bullet point missing or added",
     {"bullet marker", "list marker"}),
    ("side-by-side", "Side-by-side layout lost (picture and text, picture grid)",
     {"text beside picture", "image combined", "image order", "placement", "image in wrong section"}),
    ("alignment", "Picture aligned differently (prod centred, stage left)",
     {"image alignment", "spec image alignment", "image vertical alignment", "spec text-align"}),
    ("icons", "Icon not on the same line as its text",
     {"icon above text", "icon missing inline"}),
    ("tables", "Table layout breaking",
     {"table split", "tables merged", "row split", "rows merged", "text outside table border",
      "cell border", "caption row", "row alignment", "row order", "missing row"}),
]
TITLES = {cid: title for cid, title, _ in CATEGORIES}
OF_TYPE = {t: cid for cid, _, types in CATEGORIES for t in types}
HEAD = ["Category", "Publication", "Section", "Severity", "Prod pages", "Stage pages", "Issue", "Description"]


def _pages(f: dict, side: str) -> str:
    """The pages a finding sits on, as "p.3, 4" - "" when that side has none (stage-only checks)."""
    ps = sorted({(l.get("page", 0) + 1) for l in (f.get(side) or []) if isinstance(l, dict)})
    return "p." + ", ".join(str(p) for p in ps[:6]) + ("…" if len(ps) > 6 else "") if ps else ""


def collect(runs_dir: str | Path) -> list[dict]:
    """One row per finding of the listed categories, over the newest run of every publication."""
    from .combined_report import _jobs, pick

    runs_dir = Path(runs_dir)
    rows = []
    for _, sides in pick(_jobs(runs_dir)).items():
        job = sides.get("pdf") or sides.get("site")
        if not job or job.get("status") != "done":
            continue
        data = runs_dir / job["id"] / "results.json"
        if not data.exists():
            continue
        try:
            result = json.loads(data.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        name = (result.get("meta") or {}).get("name") or job.get("name") or job["id"]
        for sec in result.get("sections") or []:
            for f in sec.get("findings") or []:
                cid = next((OF_TYPE[t] for t in (f.get("types") or []) if t in OF_TYPE), "")
                if not cid:
                    continue
                rows.append({
                    "category": cid, "publication": name, "run": job["id"],
                    "section": sec.get("title") or "", "severity": f.get("severity", ""),
                    "prod": _pages(f, "baseline"), "stage": _pages(f, "candidate"),
                    "issue": f.get("issue") or f.get("check", ""),
                    "message": " ".join((f.get("message") or "").split()),
                    "types": f.get("types") or [], "bug": f.get("bug", ""),
                })
    rows.sort(key=lambda r: ([c for c, _, _ in CATEGORIES].index(r["category"]),
                             r["publication"].lower(), r["section"].lower()))
    return rows


def write_csv(rows: list[dict], path: str | Path) -> Path:
    path = Path(path)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(HEAD)
        for r in rows:
            w.writerow([TITLES.get(r["category"], r["category"]), r["publication"], r["section"],
                        r["severity"], r["prod"], r["stage"], r["issue"], r["message"]])
    return path


def _story_pdf(html: str, out: Path) -> Path:
    story = pymupdf.Story(html, user_css=CSS)
    writer = pymupdf.DocumentWriter(str(out))
    page = pymupdf.paper_rect("a4-l")
    where = page + (28, 28, -28, -36)
    more, n = 1, 0
    while more and n < 3000:
        dev = writer.begin_page(page)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
        n += 1
    writer.close()
    doc = pymupdf.open(out)
    for k, pg in enumerate(doc):
        pg.insert_text((page.width - 92, page.height - 16), f"Page {k + 1} / {len(doc)}",
                       fontsize=7, color=(0.42, 0.45, 0.51))
    doc.saveIncr()
    return out


def build_pdf(rows: list[dict], out: str | Path, publications: int = 0) -> Path:
    """The report: what was looked for, how much of it there is, then one section per category with
    every occurrence, publication by publication."""
    out = Path(out)
    by_cat: dict[str, list[dict]] = {cid: [r for r in rows if r["category"] == cid] for cid, _, _ in CATEGORIES}
    affected = {cid: len({r["publication"] for r in rs}) for cid, rs in by_cat.items()}
    html = [f"""<h1>Publication issues — the checks asked for</h1>
<p class="muted">{datetime.now().strftime('%Y-%m-%d %H:%M')} · {len(rows)} issue(s) across {publications}
publication(s). This report carries these checks and nothing else: list numbering, bullet points,
side-by-side layout, picture alignment, icons on the line of their text, and tables breaking up.
Text differences, fonts, sizes, the TOC and picture sizes are left out — each publication's own report
still carries all of them.</p>
<table><tr><th>Check</th><th>Issues</th><th>Publications affected</th></tr>"""]
    for cid, title, _ in CATEGORIES:
        n = len(by_cat[cid])
        html.append(f'<tr><td>{escape(title)}</td>'
                    f'<td style="color:{RED if n else GREEN}"><b>{n}</b></td>'
                    f'<td>{affected[cid]} of {publications}</td></tr>')
    html.append("</table>")

    for cid, title, _ in CATEGORIES:
        rs = by_cat[cid]
        html.append(f'<h2>{escape(title)} — {len(rs)} issue(s)</h2>')
        if not rs:
            html.append(f'<p class="muted" style="color:{GREEN}">Nothing found in any publication.</p>')
            continue
        last = None
        for r in rs:
            if r["publication"] != last:
                last = r["publication"]
                n_here = sum(1 for x in rs if x["publication"] == last)
                html.append(f'<h3>{escape(last)} — {n_here} issue(s)</h3>'
                            '<table><tr><th>Section</th><th>Prod</th><th>Stage</th><th>What is wrong</th></tr>')
            colour = RED if r["severity"] == "error" else AMBER if r["severity"] == "warning" else GREY
            html.append(f'<tr><td>{escape(r["section"][:60])}</td>'
                        f'<td class="muted">{escape(r["prod"])}</td>'
                        f'<td class="muted">{escape(r["stage"])}</td>'
                        f'<td><b style="color:{colour}">{escape(r["issue"])}</b><br/>{escape(r["message"][:400])}</td></tr>')
            nxt = rs[rs.index(r) + 1] if rs.index(r) + 1 < len(rs) else None
            if nxt is None or nxt["publication"] != last:
                html.append("</table>")
    return _story_pdf("".join(html), out)


def build(runs_dir: str | Path, out_dir: str | Path | None = None, stem: str = "publication-issues") -> dict:
    runs_dir = Path(runs_dir)
    out_dir = Path(out_dir) if out_dir else runs_dir / "_combined"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = collect(runs_dir)
    pubs = len({r["publication"] for r in rows}) or len({r["run"] for r in rows})
    return {"pdf": str(build_pdf(rows, out_dir / f"{stem}.pdf", publications=pubs)),
            "csv": str(write_csv(rows, out_dir / f"{stem}.csv")),
            "rows": len(rows), "publications": pubs}
