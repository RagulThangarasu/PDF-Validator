"""The A ⇄ B site report: two guide trees compared page by page, with both renderings side by side."""
from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from html import escape
from pathlib import Path

from .batch_report import CSS, GREEN, GREY, RED
import pymupdf
from PIL import Image as PILImage

# These live here rather than being imported from another report module: this report owns how its
# screenshots are sized and drawn, and an import tied it to a module that was rewritten underneath it.
# An A4 landscape page is 842 x 595 pt, its text frame 786 x 531: two pictures side by side may be 380
# wide, 470 tall, leaving room for the two lines above them. Stored at twice that and drawn at half, so
# a page of small print survives a zoom; a picture that fits in neither dimension is never placed at
# all, and pymupdf's Story then asks for page after page without ever advancing.
SHOT_W, SHOT_H, SHOT_SCALE, SHOT_QUALITY = 380, 470, 2, 88
SAMPLE_H = 330   # the per-publication A/B pair: leaves room for the heading and its two lines of text

# The same noise cut as the PDF-vs-PDF formatting-only report: "indent"/"list level"/"text highlight"/
# "single column alignment"/plain "changed text" and a few others are left out entirely - on two LIVE
# pages (different roots, a reflow-prone medium) these are mostly layout noise, not a defect a reader
# would notice. Only the types below are ever shown.
_NUMBERING_TYPES = {"numbering style", "numbering format", "numbering sequence"}

# The site validation report shows SIX things and nothing else:
#   1. a picture breaking            - gone, not loading, swapped, or one B has that A does not
#   2. alignment differing           - a picture, icon, list, row or table cell set differently
#   3. a, b, c differences           - list numbering and bullet markers
#   4. upper / lower case differing  - the same words cased differently
#   5. a table breaking its layout   - split, joined, or its right border not drawn
#   6. a 2 x 2 (or n-column) picture grid laid out differently - pictures side by side in A that
#      stack in B, or the other way round
_IMAGE_BREAKING = {"missing image", "broken image", "different image", "extra image"}
_ALIGNMENT = {"image alignment", "image vertical alignment", "icon alignment", "list alignment",
              "row alignment", "vertical alignment", "table cell alignment", "table header alignment",
              # an icon that sits beside its word in A but is pushed onto the NEXT line in B:
              # an alignment difference a reader sees immediately, shown with its screenshots
              "icon wraps to next line"}
_CASE = {"case"}
_TABLE_LAYOUT = {"table split", "tables merged", "table border"}
_GRID = {"picture row", "picture text row", "caption row"}

_KEPT_TYPES = (_NUMBERING_TYPES | {"bullet marker"} | _IMAGE_BREAKING | _ALIGNMENT
               | _CASE | _TABLE_LAYOUT | _GRID)
# "spec image alignment" and "table header padding" are judged on B ALONE against the design spec, not
# against A. They dominated the report - 707 and 95 of 849 findings on the last batch, 83% of it - and
# a reader scanning for "what changed between A and B" cannot use them: a picture off-centre in B is
# equally off-centre in A when both come from the same source. They also have no A side to show, which
# is why those entries had one screenshot instead of two. A against B is what this report answers;
# judging one document against the spec is the design-spec report's job.
# not in the global [genuine] list, but asked for by name in this report
_ALWAYS = {"table border", "different image", "picture text row", "vertical alignment",
           "table header alignment"}
_ONE_SIDED = {"spec image alignment", "table header padding", "table font size",
              "single column alignment", "table cell alignment"}
# "missing image" (assets.py): a prod picture with nothing at all on the stage side - as visual a
# difference as exists; the only genuinely one-sided type here (no candidate screenshot to show, the
# shot pipeline instead marks the aligned stage position where it should have been).
# "spec image alignment" (components.py): a picture - icon included, no size floor there - not centred in
# its content box against the fixed design spec, one-sided (stage only, no prod comparison needed). Kept
# alongside "image alignment" (assets.py's prod-vs-stage relative check) since both describe the exact
# same reader-visible defect ("this picture/icon is not centred"), just measured two different ways.
# "wrap under number" is deliberately NOT kept here: confirmed by eye (same prod/stage screenshot,
# pixel for pixel) that it fires whenever BOTH sides share the same absolute formatting quirk - real
# for one document on its own, but not an A-vs-B DIFFERENCE, which is the one thing this report is for.
_SIZE_DIFF_OVER = 60  # a picture smaller by up to 60% is normal reflow, not a real size regression
_ALIGN_RATIO_MAJOR = 0.5  # "spec image alignment": smaller gap / larger gap must be under this to count -
                          # near 1.0 is a few pt of reflow slack (still "not centred" by the check's own
                          # tiny tolerance), only a picture pushed clearly toward one edge is reported







CONTENT_ONLY = False   # set by build()/build_pdf(): report the words alone, nothing else


def _is_content(f: dict) -> bool:
    """A finding about the WORDS: text that differs, is missing or was added.

    The site report normally reports formatting only (see keep below). A content-only run
    inverts that: the check the finding came from decides, so every text difference is kept
    and every formatting, CSS, layout, table-structure and picture finding is dropped.
    """
    if (f.get("check") or "") != "content":
        return False
    types = set(f.get("types") or [])
    return not (types & _KEPT_TYPES)      # formatting sub-types never count as content


def keep(f: dict) -> bool:
    """The six things this report shows, and nothing else (see _KEPT_TYPES).

    The earlier version of this filter carried exceptions from the PDF-vs-PDF report - a, b, c -> 1,
    2, 3 treated as expected, only a CENTRED picture counting as an alignment defect, a bullet B adds
    being ignored. Those are exactly the differences asked for here, so they are no longer excluded.
    """
    types = set(f.get("types") or [])
    if CONTENT_ONLY:
        return _is_content(f)
    if not (types & _KEPT_TYPES):
        return False
    return bool(f.get("genuine") or types & _ALWAYS)


def _kept(finds: list[dict]) -> list[dict]:
    """The topic's findings this report shows, with one extra cut: a list renumbered end to end shows
    the SAME underlying issue once per item ("1." → "6.", "1." → "7.", ... one finding per step) - real,
    but one finding says it; the rest are the same thing restated. Only the first "numbering sequence"
    finding of a topic is kept, with a note for how many more there were."""
    out, seq_seen, seq_idx = [], 0, -1
    for f in finds:
        if not keep(f):
            continue
        if "numbering sequence" in (f.get("types") or []):
            seq_seen += 1
            if seq_seen > 1:
                continue
            seq_idx = len(out)
        out.append(f)
    if seq_seen > 1:
        extra = seq_seen - 1
        out[seq_idx] = {**out[seq_idx], "message": (out[seq_idx].get("message") or "")
                        + f"  ·  (+{extra} more item(s) in this list renumbered the same way)"}
    return out


def _shot_html(it: dict, labels: tuple[str, str] = ("A", "B")) -> str:
    """The finding's two crops side by side, each under its label. Nothing is drawn when a crop is
    missing - the finding still reads, it just has no picture."""
    sh = it.get("shots") or {}
    cells = []
    for side, label, colour in (("baseline", labels[0], "#2563eb"), ("candidate", labels[1], "#0f766e")):
        if sh.get(side):
            src, _, width = sh[side].partition("|")
            cells.append(f'<td style="width:50%"><p class="cap" style="color:{colour}">{label}</p>'
                         f'<img src="{escape(src)}" width="{width or SHOT_W}"/></td>')
    return f'<table style="width:100%"><tr>{"".join(cells)}</tr></table>' if cells else ""


def _story_pdf_with_shots(html: str, out: Path, roots: list[str]) -> Path:
    """The report PDF, with an archive so <img src="_shots/….jpg"> resolves."""
    archive = pymupdf.Archive()
    for root in dict.fromkeys(roots):
        try:
            archive.add(root)
        except Exception:
            pass
    story = pymupdf.Story(html, user_css=CSS + "\n.cap{font-size:7.5px;font-weight:bold;margin:2px 0;}",
                          archive=archive)
    writer = pymupdf.DocumentWriter(str(out))
    page = pymupdf.paper_rect("a4-l")
    where = page + (28, 28, -28, -36)
    more, n = 1, 0
    while more and n < 4000:
        dev = writer.begin_page(page)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
        n += 1
    writer.close()
    doc = pymupdf.open(out)
    for k, pg in enumerate(doc):
        pg.insert_text((page.width - 90, page.height - 16), f"Page {k + 1} / {len(doc)}",
                       fontsize=7, color=(0.42, 0.45, 0.51))
    doc.saveIncr()
    return out

AMBER = "#b45309"


def rows(result: dict) -> list[dict]:
    out = []
    for pub in result["publications"]:
        for name in pub.get("only_a", []):
            out.append({"publication": pub["product"], "topic": name, "kind": "only in A",
                        "issue": "Topic only in A", "detail": "B has no page at this path", "a": "", "b": ""})
        for name in pub.get("only_b", []):
            out.append({"publication": pub["product"], "topic": name, "kind": "only in B",
                        "issue": "Topic only in B", "detail": "A has no page at this path", "a": "", "b": ""})
        for t in pub["topics"]:
            if t["error"]:
                out.append({"publication": pub["product"], "topic": t["name"], "kind": "error",
                            "issue": "Topic could not be compared", "detail": t["error"],
                            "a": t["a"], "b": t["b"]})
            for f in _kept(t["findings"]):
                out.append({"publication": pub["product"], "topic": t["name"], "kind": "difference",
                            "issue": f.get("issue") or ", ".join(f.get("types") or []),
                            "detail": (f.get("description") or f.get("message") or "").split("\n")[0],
                            "a": t["a"], "b": t["b"]})
    return out


def write_csv(result: dict, path: str | Path) -> Path:
    path = Path(path)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["Publication", "Topic", "Kind", "Issue", "Detail", "A URL", "B URL"])
        for r in rows(result):
            w.writerow([r["publication"], r["topic"], r["kind"], r["issue"], r["detail"], r["a"], r["b"]])
    return path


def _sample_shots(result: dict, out_dir: Path) -> None:
    """One picture of each side per publication: the first page of the capture each side produced for a
    sample topic, rendered to JPEG beside the report. Per publication, not per finding - thirty pairs to
    glance at rather than hundreds, which is what makes the report readable in one pass."""
    shots_dir = out_dir / "_shots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    for n, pub in enumerate(result["publications"]):
        sample = pub.get("sample") or {}
        keep = {}
        for side in ("a", "b"):
            src = sample.get(side)
            if not src or not Path(src).exists():
                continue
            try:
                with pymupdf.open(src) as doc:
                    if not doc.page_count:
                        continue
                    pix = doc[0].get_pixmap(dpi=72)
                    name = f"pub{n:03d}{side}.jpg"
                    img = PILImage.frombytes("RGB", (pix.width, pix.height), pix.samples)
                    # shorter than a finding's crop on purpose: this pair sits under the publication
                    # heading and two lines of text, and at the full SHOT_H the row no longer fits the
                    # page - pymupdf's Story then places one cell and drops the other, which is why
                    # these entries showed A but no B
                    img.thumbnail((SHOT_W * SHOT_SCALE, SAMPLE_H * SHOT_SCALE))
                    img.save(shots_dir / name, "JPEG", quality=SHOT_QUALITY)
                    keep[side] = f"_shots/{name}|{max(1, round(img.width / SHOT_SCALE))}"
            except Exception:
                continue
        pub["sample_shots"] = keep


def _pub_shot_html(pub: dict) -> str:
    sh = pub.get("sample_shots") or {}
    cells = []
    for side, label, colour in (("a", "A", "#2563eb"), ("b", "B", "#0f766e")):
        if sh.get(side):
            src, _, width = sh[side].partition("|")
            cells.append(f'<td style="width:50%"><p class="cap" style="color:{colour}">{label}</p>'
                         f'<img src="{escape(src)}" width="{width or SHOT_W}"/></td>')
    return f'<table style="width:100%"><tr>{"".join(cells)}</tr></table>' if cells else ""


def _finding_shots(topic_dir: str, f: dict, out_dir: Path, uid: str) -> dict:
    """shots.render() already drew a marked crop of this exact finding on each side (the same webp
    crops the PDF-vs-PDF report uses) - converted to JPEG next to the report (pymupdf's Story cannot
    read webp, it silently drops the <img> with no error) so _shot_html can show the finding itself,
    not just a generic sample page of the publication."""
    sh = f.get("shots") or {}
    shots_dir = out_dir / "_shots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    keep = {}
    for side, key in (("baseline", "baseline"), ("candidate", "candidate")):
        rel = sh.get(key)
        if not rel:
            continue
        src = Path(topic_dir) / rel
        if not src.is_file():
            continue
        try:
            img = PILImage.open(src).convert("RGB")
            img.thumbnail((SHOT_W * SHOT_SCALE, SHOT_H * SHOT_SCALE))
            name = f"f{uid}{side[0]}.jpg"
            img.save(shots_dir / name, "JPEG", quality=SHOT_QUALITY)
            keep[side] = f"_shots/{name}|{max(1, round(img.width / SHOT_SCALE))}"
        except Exception:
            continue
    return keep


def build_pdf(result: dict, path: str | Path, *, title: str = "Site A ⇄ Site B", intro: str | None = None,
              unit: str = "publication", content_only: bool = False) -> Path:
    """One row per publication: how many pages each side holds, how many were validated, how many
    differ, and whether it passed - then the two renderings side by side, then every difference of
    every page with its own marked-up A/B screenshot (the same crop the PDF-vs-PDF report uses).

    title/intro/unit let a caller reuse this exact report - same kept types, same layout, same
    screenshots - for something that isn't a live-site crawl (pdfdiff_report: one PDF pair), without
    the text claiming it crawled a site."""
    global CONTENT_ONLY
    CONTENT_ONLY = bool(content_only)
    out_dir_for_shots = Path(path).parent
    pubs = result["publications"]
    n_diff = sum(len(_kept(t["findings"])) for p in pubs for t in p["topics"])
    n_errored = sum(1 for p in pubs for t in p["topics"] if t.get("error"))
    failed = sum(1 for p in pubs if p["error"] or any(_kept(t["findings"]) for t in p["topics"]))
    if intro is None and content_only:
        intro = (f"every {unit} both roots hold, every page of it at every level, opened with "
                 f"<b>?wcmmode=disabled</b> and compared page against page. <b>Content only</b>: whether the "
                 f"words match. Text that differs, is missing or was added. Formatting, CSS, layout, tables, "
                 f"pictures and hyperlink targets are not reported.")
    if intro is None:
        intro = (f"every {unit} both roots hold, every page of it at every level (AEM's own page list, so a page "
                 f"the navigation does not link to is validated too), opened with <b>?wcmmode=disabled</b> and "
                 f"compared page against page. Words, tables, pictures, type, colour and layout. Hyperlink targets "
                 f"are not compared: the two trees sit under different roots, so every cross-reference would "
                 f"differ by its root alone.")
    U = escape(unit.capitalize())
    scope = "" if content_only else """<p class="muted">Scope, six things and nothing else:
a picture breaking (gone, not loading, swapped, or one B has that A does not); alignment differing
(a picture, icon, list, row or table cell set differently, and an icon pushed onto the next line
instead of staying beside its word); a, b, c differences (list numbering and
bullet markers); upper / lower case differing; a table breaking its layout (split, joined, or its
right-hand border not drawn); and a picture grid (2 x 2 or n columns) laid out differently - pictures
side by side in A that stack in B, or the other way round. Everything else is left out.</p>"""
    html = [f"""<h1>{escape(title)}</h1>
<p class="muted">{datetime.now().strftime('%Y-%m-%d %H:%M')} · {intro}</p>
{scope}
<table><tr><th>A</th><td class="muted">{escape(result['a_root'])}</td></tr>
<tr><th>B</th><td class="muted">{escape(result['b_root'])}</td></tr></table>
<table><tr><th>{U}s</th><th class="n">Pages in A</th><th class="n">Pages in B</th>
<th class="n">Pages validated</th><th class="n">Could not be compared</th><th class="n">Differences</th>
<th class="n">{U}s with differences</th></tr>
<tr><td>{len(pubs)}</td><td class="n">{sum(p.get('a_pages', 0) for p in pubs)}</td>
<td class="n">{sum(p.get('b_pages', 0) for p in pubs)}</td>
<td class="n">{sum(len(p['topics']) for p in pubs)}</td>
<td class="n" style="color:{AMBER if n_errored else GREEN}"><b>{n_errored or '—'}</b></td>
<td class="n" style="color:{RED if n_diff else GREEN}"><b>{n_diff}</b></td>
<td class="n">{failed} of {len(pubs)}</td></tr></table>"""]
    if result.get("only_a") or result.get("only_b"):
        html.append(f'<p class="muted">{U}s only under A: ' + (escape(", ".join(result["only_a"])) or "none")
                    + f'<br/>{U}s only under B: ' + (escape(", ".join(result["only_b"])) or "none") + "</p>")

    html.append(f'<h2>By {escape(unit)}</h2><table><tr><th>{U}</th><th class="n">Pages in A</th>'
                '<th class="n">Pages in B</th><th class="n">Validated</th><th class="n">Differences</th>'
                '<th class="n">Only A</th><th class="n">Only B</th><th>Status</th></tr>')
    for p in pubs:
        d = sum(len(_kept(t["findings"])) for t in p["topics"])
        errored = [t["name"] for t in p["topics"] if t.get("error")]
        status, colour = ("ERROR", RED) if p["error"] else (
            ("DIFFERENCES", RED) if d else (("INCOMPLETE", AMBER) if errored else ("PASS", GREEN)))
        html.append(f'<tr><td>{escape(p["product"])}</td>'
                    f'<td class="n">{p.get("a_pages", 0)}</td><td class="n">{p.get("b_pages", 0)}</td>'
                    f'<td class="n">{len(p["topics"])}</td>'
                    f'<td class="n" style="color:{RED if d else GREY}">{d or "—"}</td>'
                    f'<td class="n">{len(p.get("only_a") or []) or "—"}</td>'
                    f'<td class="n">{len(p.get("only_b") or []) or "—"}</td>'
                    f'<td style="color:{colour}"><b>{status}</b></td></tr>')
    html.append("</table>")

    for p in pubs:
        d = sum(len(_kept(t["findings"])) for t in p["topics"])
        errored = [(t["name"], t["error"]) for t in p["topics"] if t.get("error")]
        # Only what differs gets a section. A publication that passed is counted in the table above and
        # nothing more: a page saying "PASS" with a sample of each side showed nothing to act on. One
        # that could not be compared is still listed - "not checked" must not read as "no difference".
        if not d and not p["error"] and not errored:
            continue
        status, colour = ("ERROR", RED) if p["error"] else (
            ("DIFFERENCES", RED) if d else ("INCOMPLETE", AMBER))
        html.append(f'<h2>{escape(p["product"])} — <span style="color:{colour}">{status}</span></h2>')
        html.append(f'<p class="muted">Pages in A: {p.get("a_pages", 0)} · Pages in B: {p.get("b_pages", 0)} · '
                    f'validated: {len(p["topics"])} · differences: {d}'
                    + (f' · only in A: {len(p.get("only_a") or [])}' if p.get("only_a") else "")
                    + (f' · only in B: {len(p.get("only_b") or [])}' if p.get("only_b") else "")
                    + (f'<br/><span style="color:{RED}">{escape(p["error"][:160])}</span>' if p["error"] else "")
                    + "</p>")
        if errored:
            html.append(f'<p style="color:{AMBER}">{len(errored)} page(s) could not be compared at all - '
                        f'not validated, not just "no difference found":</p><ul>'
                        + "".join(f'<li>{escape(name)} <span class="muted">— {escape(err[:100])}</span></li>'
                                  for name, err in errored) + "</ul>")
        # no per-publication sample pair here: the screenshots that matter are the ones under each
        # difference below, and the sample pair was tall enough that the page dropped one of its sides
        bad = [(t, _kept(t["findings"])) for t in p["topics"] if _kept(t["findings"])]
        for k, (t, finds) in enumerate(bad):
            kinds = ", ".join(sorted({x for f in finds for x in (f.get("types") or [])}))
            html.append(f'<h3 style="margin-top:10px">{escape(t["name"])} — '
                        f'<span style="color:{RED}">{len(finds)} difference(s)</span>'
                        f'<span class="muted" style="font-weight:normal"> · {escape(kinds[:160])}</span></h3>')
            for j, f in enumerate(finds):
                uid = re.sub(r"[^\w-]+", "-", f"{p['product']}{k}-{j}")
                fshots = _finding_shots(t.get("dir", ""), f, Path(out_dir_for_shots), uid)
                text = (f.get("description") or f.get("message") or "").split("\n")[0]
                html.append(f'<p style="margin:6px 0 2px"><b>{escape(f.get("issue") or "Difference")}</b> '
                            f'<span class="muted">{escape(text[:220])}</span></p>')
                shot = _shot_html({"shots": fshots}, ("A", "B"))
                if shot:
                    html.append(shot)
    return _story_pdf_with_shots("".join(html), Path(path), [str(Path(path).parent)])


def build(result: dict, out_dir: str | Path, stem: str = "site-a-vs-b", *, title: str = "Site A ⇄ Site B",
          intro: str | None = None, unit: str = "publication", content_only: bool = False) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{stem}.json").write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    return {"pdf": str(build_pdf(result, out_dir / f"{stem}.pdf", title=title, intro=intro, unit=unit,
                                content_only=content_only)),
            "csv": str(write_csv(result, out_dir / f"{stem}.csv")),
            "json": str(out_dir / f"{stem}.json")}
