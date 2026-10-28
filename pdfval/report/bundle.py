"""One PDF for a whole batch: the validation report of every publication, back to back, behind a
contents page.

The per-run report (`<name>-validation-report.pdf`) already merges one publication's issues. A batch
leaves one of those per publication in a folder, so a reviewer who wants to go through the batch has
to open thirty files. This puts them in a single PDF: a contents page listing every publication with
its result, its score and its issue count, then the reports themselves in that order, each behind a
level-1 bookmark with its own outline kept underneath it.

Nothing is re-validated and no report is rebuilt - the pages are the ones already written.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from html import escape
from pathlib import Path

import pymupdf

from .batch_report import CSS, GREEN, GREY, RED

OUT_NAME = "All publications - issues.pdf"
AMBER = "#b45309"


def _cover(doc: "pymupdf.Document") -> dict:
    """What the report's own cover page says about the publication: its product name, its result
    (PASS / FAIL / WARN), its score and how many issues this report holds. A report whose cover does
    not say (an older one, or one built another way) simply leaves the field empty."""
    text = doc[0].get_text() if doc.page_count else ""
    def after(label: str, pattern: str = r"(.+)") -> str:
        m = re.search(rf"^{re.escape(label)}\s*\n{pattern}", text, re.M)
        return m.group(1).strip() if m else ""
    issues = re.search(r"^(\d+) issue\(s\)", text, re.M)
    return {"product": after("Product"),
            # the verdict on the title line, whatever separates it from the title (the dash is an
            # em dash in the real report, another glyph in a PDF written with a base-14 font)
            "result": (re.search(r"PDF Report[^\n]*?\b(PASS|FAIL|WARN)\b", text) or [None, ""])[1],
            "score": (re.search(r"Score\s*\n\s*([\d.]+\s*%)", text) or [None, ""])[1],
            "issues": int(issues.group(1)) if issues else None,
            "critical": _critical(after("Critical / breaking"))}


def _critical(text: str) -> str:
    """The cover prints the critical count and its breakdown on one line ("3 3 × image missing", or
    "0 — no missing sections, rows, images, files, links or glyphs"): the count is already the first
    number of the breakdown, so only the breakdown is kept, and "none" is left empty."""
    text = re.sub(r"^\d+\s+(?=\d)", "", text.strip())
    return "" if text.startswith(("0 ", "0\u00a0", "—")) or not text else text


def reports(folder: str | Path, pattern: str = "*-validation-report.pdf", batch: str = "") -> list[Path]:
    """The publication reports to bundle, by name - never the bundle this module writes.

    Either a folder holding the reports themselves (reports/batch1/<name>-validation-report.pdf), or a
    runs folder, where each run keeps its own report in its own directory (runs/<run id>/<name>-…pdf):
    a batch does not collect them anywhere, so looking one level down is what lets a batch be bundled
    straight from runs/ without copying thirty files by hand first. `batch` keeps only the runs of that
    batch, read from each run's job.json."""
    folder = Path(folder)
    here = sorted(p for p in folder.glob(pattern) if p.name != OUT_NAME)
    if here and not batch:
        return here
    deep = []
    for run in sorted(folder.iterdir() if folder.is_dir() else []):
        if not run.is_dir():
            continue
        if batch:
            try:
                if json.loads((run / "job.json").read_text(encoding="utf-8")).get("batch") != batch:
                    continue
            except (OSError, ValueError):
                continue
        deep += [p for p in sorted(run.glob(pattern)) if p.name != OUT_NAME]
    return here + deep


def _contents_pdf(rows: list[dict], title: str, out: Path) -> Path:
    colour = lambda r: RED if r["result"] == "FAIL" else AMBER if r["result"] == "WARN" else GREEN if r["result"] else GREY
    html = [f"""<h1>{escape(title)} — all publications</h1>
<p class="muted">{datetime.now().strftime('%Y-%m-%d %H:%M')} · {len(rows)} publication(s) · every publication's
validation report in one file, in this order. Each one starts at the page named here and is bookmarked by its name.</p>
<table><tr><th class="n">#</th><th>Publication</th><th>Product</th><th>Result</th><th class="n">Score</th>
<th class="n">Issues</th><th>Critical</th><th class="n">Pages</th><th class="n">Starts on page</th></tr>"""]
    for k, r in enumerate(rows, 1):
        html.append(f'<tr><td class="n">{k}</td><td>{escape(r["name"])}</td><td>{escape(r["product"])}</td>'
                    f'<td style="color:{colour(r)}"><b>{escape(r["result"] or "—")}</b></td>'
                    f'<td class="n">{escape(r["score"] or "—")}</td>'
                    f'<td class="n">{"—" if r["issues"] is None else r["issues"]}</td>'
                    f'<td>{escape(r["critical"] or "—")}</td>'
                    f'<td class="n">{r["pages"]}</td><td class="n">{r["starts"] or "—"}</td></tr>')
    html.append("</table>")
    story = pymupdf.Story("".join(html), user_css=CSS)
    writer = pymupdf.DocumentWriter(str(out))
    page = pymupdf.paper_rect("a4-l")
    where = page + (28, 28, -28, -36)
    more, n = 1, 0
    while more and n < 200:
        dev = writer.begin_page(page)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
        n += 1
    writer.close()
    return out


def build(folder: str | Path, out: str | Path | None = None, title: str | None = None,
          batch: str = "") -> Path:
    """Bundle every publication report of `folder` into one PDF. Returns the path written."""
    folder = Path(folder)
    srcs = reports(folder, batch=batch)
    if not srcs:
        raise FileNotFoundError(f"no *-validation-report.pdf in {folder}"
                                + (f" for batch {batch}" if batch else "")
                                + " (nor in the folders one level under it)")
    out = Path(out) if out else folder / OUT_NAME
    out.parent.mkdir(parents=True, exist_ok=True)
    title = title or batch or folder.name

    # read each report's cover first: the contents page needs the page each one starts on, which is only
    # known once every report's length is known, and the contents page itself comes before all of them
    rows = []
    for src in srcs:
        with pymupdf.open(src) as d:
            rows.append({"path": src, "name": src.name[:-len("-validation-report.pdf")] if src.name.endswith("-validation-report.pdf") else src.stem,
                         "pages": d.page_count, "starts": 0, **_cover(d)})

    tmp = out.with_suffix(".contents.pdf")
    try:
        # the contents page's own length changes where the reports start, so it is laid out, measured,
        # then laid out again with the real page numbers
        for _ in range(2):
            _contents_pdf(rows, title, tmp)
            with pymupdf.open(tmp) as c:
                front = c.page_count
            at = front + 1
            for r in rows:
                r["starts"] = at
                at += r["pages"]

        merged, toc = pymupdf.open(), []
        try:
            with pymupdf.open(tmp) as c:
                merged.insert_pdf(c)
            toc.append([1, f"{title} — contents", 1])
            for r in rows:
                start = merged.page_count + 1
                with pymupdf.open(r["path"]) as src:
                    label = f'{r["name"]}' + (f' — {r["result"]}' if r["result"] else "")
                    toc.append([1, label, start])
                    # the report's own bookmarks stay, one level deeper and moved to where it now sits
                    for lvl, name, pno, *_ in src.get_toc():
                        if 1 <= pno <= src.page_count:
                            toc.append([lvl + 1, name, start + pno - 1])
                    merged.insert_pdf(src)
            merged.set_toc(toc)
            out.unlink(missing_ok=True)
            merged.save(str(out), garbage=3, deflate=True)
        finally:
            merged.close()
    finally:
        tmp.unlink(missing_ok=True)
    return out
