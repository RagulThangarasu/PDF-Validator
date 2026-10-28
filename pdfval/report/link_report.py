"""The link-check report: one row per link, grouped by guide and by topic, with the topic's GUID.
PDF + CSV + JSON. No comparison and no baseline - the pages are judged on their own."""
from __future__ import annotations

import csv
import json
from datetime import datetime
from html import escape
from pathlib import Path

import pymupdf

from .batch_report import CSS, GREEN, GREY, RED

AMBER = "#b45309"
HEAD = ["Guide", "Topics in the nav", "Topics crawled", "Topic", "Topic URL", "Topic GUID", "Link text",
        "Link address", "Status", "What is wrong", "Note"]


def write_csv(result: dict, path: str | Path) -> Path:
    from ..linkcheck import rows
    path = Path(path)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(HEAD)
        # the guide's own coverage travels with every row: a reader filtering the sheet by guide sees
        # at once whether the whole left navigation was crawled or the run was cut short
        cover = {g["start"]: (g.get("nav_items") or len(g["pages"]), len(g["pages"])) for g in result["guides"]}
        for r in rows(result["guides"]):
            in_nav, crawled = cover.get(r["guide"], ("", ""))
            w.writerow([r["guide"], in_nav, crawled, r["topic"], r["topic_url"], r["guid"], r["link_text"],
                        r["href"], r["status"], r["kind"], r["note"]])
    return path


def write_json(result: dict, path: str | Path) -> Path:
    path = Path(path)
    path.write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    return path


def _story_pdf(html: str, out: Path) -> Path:
    story = pymupdf.Story(html, user_css=CSS)
    writer = pymupdf.DocumentWriter(str(out))
    page = pymupdf.paper_rect("a4-l")
    where = page + (28, 28, -28, -36)
    more, n = 1, 0
    while more and n < 2000:
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


def _collapse(links: list[dict]) -> list[tuple[dict, int]]:
    """The same link printed several times in one topic ("Product support" four times over, the same
    address and the same fault) is one row with how often it occurs - four identical rows say nothing
    the first does not. Order is kept: the first occurrence is where the row stands."""
    out: list[tuple[dict, int]] = []
    seen: dict[tuple, int] = {}
    for l in links:
        k = (l.get("text", ""), l.get("href", ""), l.get("kind", ""), l.get("note", ""))
        if k in seen:
            i = seen[k]
            out[i] = (out[i][0], out[i][1] + 1)
            continue
        seen[k] = len(out)
        out.append((l, 1))
    return out


def build_pdf(result: dict, out: str | Path, failures_only: bool = False) -> Path:
    t = result["totals"]
    out = Path(out)
    nav_topics = t.get("nav_topics", t["topics"])
    left = t.get("not_crawled", 0)
    html = [f"""<h1>Link check{' — failures only' if failures_only else ''}</h1>
<p class="muted">{datetime.now().strftime('%Y-%m-%d %H:%M')} · every topic of each guide's left navigation, to the
bottom of the tree, and every link in each topic's body. The links themselves are captured from the page - their text
and their address - and the address is only asked what it answers; a link's own page is never opened or crawled.
No baseline and no PDF comparison: a link fails when its address or label carries a DITA GUID, when it lands on a
GUID address, or when it does not answer (HTTP 4xx / 5xx, no response, a dead #anchor).</p>"""
            + ('<p class="muted">This report lists the failing links and nothing else: a link that opens, a topic whose '
               'every link opens, and a guide with no failure at all are left out. The counts below are of the whole '
               'run, so they also say how much passed.</p>' if failures_only else "")
            + f"""
<p class="muted"><b>Coverage:</b> {t['topics']} of {nav_topics} topic(s) in the left navigation were crawled"""
            + (f', <span style="color:{AMBER}"><b>{left} not crawled</b> (the "max topics per guide" limit)</span>'
               if left else " — the whole navigation")
            + f""".</p>
<table><tr><th>Guides</th><th>Topics in the nav</th><th>Topics crawled</th><th>Links checked</th><th>Failed</th><th>Broken</th>
<th>GUID links</th><th>Not checked</th><th>Topics with no GUID</th><th>Pages that did not open</th></tr>
<tr><td>{t['guides']}</td><td>{nav_topics}</td>
<td style="color:{AMBER if left else '#1d2330'}">{t['topics']}</td><td>{t['links']}</td>
<td style="color:{RED if t['failed'] else GREEN}"><b>{t['failed']}</b></td>
<td>{t['broken']}</td><td>{t['guid_links']}</td><td>{t['skipped']}</td>
<td>{t['topics_without_guid']}</td>
<td style="color:{RED if t['page_errors'] else '#1d2330'}">{t['page_errors']}</td></tr></table>"""]

    for g in result["guides"]:
        # the failures report carries failures and nothing else: a guide whose every link opens is not
        # in it at all - not even as a heading saying so
        if failures_only and not g["error"] and not any(
                p["error"] or any(l.get("status") == "fail" for l in p["links"]) for p in g["pages"]):
            continue
        html.append(f'<h2>{escape(g["title"] or g["start"])}</h2>')
        in_nav, missed = g.get("nav_items") or len(g["pages"]), g.get("not_crawled") or 0
        links_here = sum(len(p["links"]) for p in g["pages"])
        fails_here = sum(1 for p in g["pages"] for l in p["links"] if l.get("status") == "fail")
        errs_here = sum(1 for p in g["pages"] if p["error"])
        state = (f'<span style="color:{RED}">{fails_here} failing</span>' if fails_here else
                 f'<span style="color:{RED}">{errs_here} topic(s) did not open</span>' if errs_here else 'all fine')
        html.append(f'<p class="muted">{escape(g["start"])}<br/>'
                    f'{len(g["pages"])} of {in_nav} topic(s) in the left navigation crawled'
                    + (f' · <span style="color:{AMBER}">{missed} not crawled</span>' if missed else '')
                    + f' · {links_here} link(s) in their bodies, ' + state
                    + (f' · <span style="color:{RED}">{errs_here} topic(s) did not open</span>'
                       if errs_here and fails_here else '')
                    + '</p>')
        if g["error"]:
            html.append(f'<p style="color:{RED}"><b>{escape(g["error"])}</b></p>')
        for p in g["pages"]:
            bad = [l for l in p["links"] if l.get("status") == "fail"]
            shown = bad if failures_only else p["links"]
            if failures_only and not bad and not p["error"]:
                continue
            guid = escape(p["guid"]) if p["guid"] else '<span style="color:%s">no GUID on the page</span>' % AMBER
            html.append(f'<h3>{escape(p["title"] or p["nav_text"] or p["url"])}</h3>'
                        f'<p class="muted">{escape(p["url"])}<br/>Topic GUID: {guid}'
                        + (f' <span class="muted">(from the {escape(p["guid_from"])})</span>' if p["guid_from"] else "")
                        + f' · {len(p["links"])} link(s), {len(bad)} failing</p>')
            if p["error"]:
                html.append(f'<p style="color:{RED}"><b>{escape(p["error"])}</b></p>')
            if not shown:
                # in a failures report there is nothing to say about a topic with no failure - only a
                # topic that did not open at all gets here, and its error is printed above
                if not failures_only:
                    html.append('<p class="muted">No links in this topic.</p>' if not p["links"]
                                else '<p class="muted">Every link in this topic opens.</p>')
                continue
            html.append('<table><tr><th>Link text</th><th>Address</th><th>Status</th><th>What is wrong</th></tr>')
            for l, times in _collapse(shown):
                colour = RED if l.get("status") == "fail" else GREY if l.get("status") == "skip" else GREEN
                html.append(f'<tr><td>{escape(l["text"][:80] or "(no text)")}'
                            + (f' <span class="muted">\u00d7 {times}</span>' if times > 1 else "")
                            + f'</td><td class="muted">{escape(l["href"][:120])}</td>'
                            f'<td style="color:{colour}"><b>{escape(l.get("kind", ""))}</b></td>'
                            f'<td>{escape(l.get("note", ""))}</td></tr>')
            html.append("</table>")
    return _story_pdf("".join(html), out)


def build(result: dict, out_dir: str | Path, stem: str = "link-check") -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    return {"pdf": str(build_pdf(result, out_dir / f"{stem}.pdf")),
            "failures_pdf": str(build_pdf(result, out_dir / f"{stem}-failures.pdf", failures_only=True)),
            "csv": str(write_csv(result, out_dir / f"{stem}.csv")),
            "json": str(write_json(result, out_dir / f"{stem}.json"))}
