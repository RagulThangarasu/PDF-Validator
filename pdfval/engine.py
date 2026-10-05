"""Orchestrates: extract -> sections -> match -> per-section checks -> result dict."""
from __future__ import annotations

import copy
import datetime as dt
import re
import tomllib
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from . import aem, extract, genuine, normalize, sections, toc as tocmod
from .checks import PIPELINE, Aligner, Unit, insertion_loc, locs, snippet
from .checks import typography as checks_typography
from .checks import footer as checks_footer
from .model import SEVERITY_RANK, Anchor, Doc, Finding, Loc
from .report.pdf_report import is_image_issue

# the content match % counts only these: content missing/extra, image missing/broken, note style -
# everything else (table merges, css, layout, toc, structure ...) still shows in the report, just
# does not move the percentage
CONTENT_PCT_TYPES = {"missing text", "extra text", "missing image", "broken image", "callout style"}

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "default.toml"


def load_config(path: str | None = None) -> dict:
    with open(DEFAULT_CONFIG, "rb") as f:
        cfg = tomllib.load(f)
    typo = Path(DEFAULT_CONFIG).with_name("typography.toml")  # the design spec (Figma), its own file
    if typo.exists():
        with open(typo, "rb") as f:
            _merge(cfg, tomllib.load(f))
    if path:
        with open(path, "rb") as f:
            _merge(cfg, tomllib.load(f))
    return cfg


def _merge(base: dict, over: dict) -> None:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v


def _assign_roles(doc: Doc, anchors: list[Anchor]) -> None:
    for w in doc.words:
        w.role = "body" if abs(w.style.size - doc.body_size) < 0.6 else f"text-{w.style.size:g}pt"
    for a in anchors:
        if a.located:
            line = doc.words[a.word].line
            for w in doc.words[a.word:]:
                if w.line != line:
                    break
                w.role = f"h{a.level}"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48] or "section"


def _pos(doc: Doc, idx: int) -> dict:
    if idx >= len(doc.words):
        p = len(doc.pages) - 1
        return {"page": p, "y": doc.pages[p].height}
    w = doc.words[idx]
    return {"page": w.page, "y": round(w.bbox[1], 1)}


def _reset_caches() -> None:
    """The checks cache open PDFs and per-page results by file path. A new run may use the same
    path for a different file (a stage PDF re-exported under the same name), so start clean."""
    from .checks import assets, integrity, placement, tables
    for mod in (assets, integrity, tables):
        for doc in mod._DOCS.values():
            doc.close()
    for cache in (assets._DOCS, assets._VIS, assets._BLANK, integrity._DOCS, integrity._LINKS,
                  integrity._OFFPAGE, integrity._NAMES, tables._DOCS, tables._RAW, tables._RULES,
                  tables._DIAGRAMS, tables._EDGES_Y):
        cache.clear()
    placement._shapes.cache_clear()
    from .checks import layout
    layout._BOXES.clear()
    from . import ocr
    ocr.reset()
    from . import genuine as _gen
    _gen._LINE_ART.clear()


def compare(baseline: str, candidate: str, cfg: dict | None = None, *, only: str | None = None,
            progress: Callable[[float, str], None] | None = None, candidate_doc: Doc | None = None,
            candidate_meta: dict | None = None) -> dict:
    """progress(fraction 0..1, message) is called as the run advances.
    candidate_doc: an already built candidate (e.g. a web page from html_source.capture);
    `candidate` is then the path of its image PDF (screenshots, viewer)."""
    report = progress or (lambda f, m: None)
    cfg = copy.deepcopy(cfg or load_config())
    _reset_caches()
    report(0.0, "Extracting baseline (prod)")
    # each side is the other's reference for glyphs drawn without a Unicode map (glyphs.py)
    A = extract.load(baseline, "baseline", cfg, reference=None if candidate_doc else candidate)
    report(0.2, "Extracting candidate")
    B = candidate_doc or extract.load(candidate, "candidate", cfg, reference=baseline)
    if cfg["content"].get("skip_picture_text", True):  # the words of a picture are its artwork, not content
        from . import genuine as _g
        _g.skip_picture_text(A, cfg)
        _g.skip_picture_text(B, cfg)
    labels = cfg["content"].get("label_words") or []
    if labels:  # "Tips" -> "TIPS:", "Note" -> "NOTE:" is house style, not a content change
        normalize.fold_labels(A, labels)
        normalize.fold_labels(B, labels)
    if cfg["content"].get("ignore_xref_page_numbers", True):  # `"Title" on page 12` vs `"Title"`: template
        normalize.fold_xref_pages(A)
        normalize.fold_xref_pages(B)
    report(0.4, "Matching sections")
    # the printed table of contents is compared on its own (levels, entries, page numbers);
    # its text is taken out of the content diff (page numbers shift with every layout change)
    toc_a, toc_b = tocmod.detect(A, cfg), tocmod.detect(B, cfg)
    nav_toc = ((candidate_meta or {}).get("capture") or {}).get("nav_toc")
    if nav_toc:  # a web guide's table of contents is its left navigation, not the pages' heading outline
        toc_b = tocmod.Toc("navigation", entries=[
            tocmod.TocEntry(e["title"], normalize.title(e["title"]), e["level"], None, e["toc_page"],
                            tuple(e["bbox"]), e["bbox"][0]) for e in nav_toc])
        toc_b.pages = sorted({e.toc_page for e in toc_b.entries if e.toc_page >= 0})
        toc_b.page_sizes = {p: (B.pages[p].width, B.pages[p].height) for p in toc_b.pages}
    for d, t in ((A, toc_a), (B, toc_b)):
        for li in t.lines:
            for w in d.words[d.lines[li].first_word:]:
                if w.line != li:
                    break
                w.norm = ""
        # every line of a printed TOC's pages, from its first entry on: a wrapped entry ("Safety notice for
        # remote control (applicable if a | remote control is provided) ..... 9") leaves lines no entry pattern sees
        if t.source == "printed" and t.lines:
            start = min(t.lines)
            for w in d.words:
                if w.page in t.pages and w.line >= start - 1:
                    w.norm = ""
    anA, anB = sections.build_anchors(A, cfg), sections.build_anchors(B, cfg)
    # the printed TOC is compared on its own (TOC check): its title and entry lines are not sections,
    # whatever found them (bookmarks, or font sizes when the PDF has no bookmarks)
    def off_toc(anchors, doc, toc):
        lines = set(toc.lines)
        return [a for a in anchors if not (lines and a.located and a.word < len(doc.words) and doc.words[a.word].line in lines)
                and not (toc.pages and not a.located and a.page in toc.pages)]
    anA, anB = off_toc(anA, A, toc_a), off_toc(anB, B, toc_b)
    tocmod.resolve_pages(toc_a, anA)
    tocmod.resolve_pages(toc_b, anB)
    _assign_roles(A, anA)
    _assign_roles(B, anB)
    pairs = sections.match_anchors(anA, anB, cfg, A, B)
    # a web page is one part of the PDF: validate only the chapter(s) it covers
    scope = None
    if (candidate_meta or {}).get("mode") == "html" and cfg["sections"].get("page_scope", True):
        scope = _page_scope(A, anA, anB, pairs, (candidate_meta or {}).get("page_title", ""), cfg)
    if scope:
        pairs = [p for p in pairs if scope.contains(anA[p[0]].word)]
        toc_a.entries = [e for e in toc_a.entries if e.norm in scope.norms and e.norm not in scope.title_norms]
        for e in toc_b.entries:  # the page's own heading levels, shifted onto the PDF's
            e.level += scope.level_offset
    max_level = cfg.get("toc", {}).get("max_level", 0)
    if max_level:  # validate the top level(s) only; an entry that is top-level on either side stays in
        top_a = {e.norm for e in toc_a.entries if e.level <= max_level}
        top_b = {e.norm for e in toc_b.entries if e.level <= max_level}
        toc_a.entries = [e for e in toc_a.entries if e.level <= max_level or e.norm in top_b]
        toc_b.entries = [e for e in toc_b.entries if e.level <= max_level or e.norm in top_a]
    skip_sec = _ignored_sections(cfg)
    if skip_sec:  # sections the validation leaves out (Q&A index): not compared in the TOC either
        toc_a.entries = [e for e in toc_a.entries if not skip_sec(e.norm)]
        toc_b.entries = [e for e in toc_b.entries if not skip_sec(e.norm)]
    toc_info, toc_findings = tocmod.compare(toc_a, toc_b, cfg)
    if toc_a.heading and toc_b.heading and normalize_heading(toc_a.heading) != normalize_heading(toc_b.heading):
        toc_findings.insert(0, Finding("toc", "warning", f"TOC heading differs: “{toc_a.heading}” → “{toc_b.heading}”",
                                       detail={"kind": "heading"}, types=["heading differs"]))

    # --- build units: [front matter] + one per matched heading, each extends to the next matched heading
    bounds: list[tuple[str, Anchor | None, Anchor | None, int, int]] = []
    if cfg["sections"].get("front_matter", True):
        bounds.append(("Front matter", None, None, 0, 0))
    if scope:  # chapter heading matched by the page title: its intro text vs the page text before the first heading
        for c in scope.title_chapters:
            if c not in {i for i, _, _ in pairs}:
                bounds.append((anA[c].title, anA[c], None, anA[c].word, 0))
    for i, j, _ in pairs:
        bounds.append((anA[i].title, anA[i], anB[j], anA[i].word, anB[j].word))
    # a heading in one column, its section's text in the other column read first: the PDF stream has that text
    # before the heading (prod: a remote-control key table to the right of the "Remote control" heading), so it
    # would fall into the previous section - reported missing there and changed here. The words right before a
    # heading on its page that do not lie above it start its section too.
    def pull_back(D, w: int, floor: int, anchor) -> int:
        if anchor is None or not getattr(anchor, "located", True) or not 0 < w < len(D.words):
            return w
        hw = D.words[w]
        top = hw.bbox[1] - 0.5 * (hw.bbox[3] - hw.bbox[1])
        k = w
        while k - 1 > floor and D.words[k - 1].page == hw.page and D.words[k - 1].bbox[1] >= top:
            k -= 1
        return k

    for side, pos in (("a", 3), ("b", 4)):
        D, anc = (A, 1) if side == "a" else (B, 2)
        starts = sorted({b[pos] for b in bounds})
        for n, b in enumerate(bounds):
            floor = max((s for s in starts if s < b[pos]), default=-1)
            k = pull_back(D, b[pos], floor, b[anc])
            if k != b[pos]:
                bounds[n] = b[:pos] + (k,) + b[pos + 1:]
    units: list[Unit] = []
    seen = Counter()
    # each section runs to the next paired heading in ITS OWN document, so a section that
    # moved (same title, different position) is still compared with its own counterpart
    starts_a, starts_b = sorted({b[3] for b in bounds}), sorted({b[4] for b in bounds})
    nxt = lambda starts, x, total: next((y for y in starts if y > x), total)
    for k, (title, aa, ab, sa, sb) in enumerate(bounds):
        ea = nxt(starts_a, sa, len(A.words))
        if scope:  # never run past the end of the chapter the page covers
            ea = min(ea, scope.end_of(sa))
        eb = nxt(starts_b, sb, len(B.words))
        slug = _slug(title)
        seen[slug] += 1
        uid = f"{k:03d}-{slug}" + (f"-{seen[slug]}" if seen[slug] > 1 else "")
        units.append(Unit(uid, title, A, B, (sa, max(sa, ea)), (sb, max(sb, eb)), aa, ab, cfg))

    if scope:  # chapter matched by the page title: the text between its heading and the first
        # sub-heading vs the page text before its first heading (the heading line is the page title)
        first_b = min((anB[j].word for _, j, _ in pairs), default=len(B.words))
        for u in [u for u in units if u.b_anchor is None and u.a_anchor is not None]:
            head = A.words[u.a_anchor.word].line if u.a_anchor.located else -1
            a0 = next((k for k in range(*u.a_range) if A.words[k].line != head), u.a_range[1])
            u.a_range, u.b_range = (a0, u.a_range[1]), (0, first_b)
            if not any(A.words[k].norm for k in range(*u.a_range)) and not any(B.words[k].norm for k in range(*u.b_range)):
                units.remove(u)  # no intro text on either side

    if not units:  # no heading matched on both sides: compare the documents as one whole section
        units.append(Unit("000-whole-document", "Whole document", A, B, (0, len(A.words)), (0, len(B.words)),
                          None, None, cfg))

    # --- structural findings (unmatched / relevelled headings) attach to the unit containing them
    struct: dict[str, list[Finding]] = defaultdict(list)
    matched_a, matched_b = {i for i, _, _ in pairs}, {j for _, j, _ in pairs}

    def unit_for(side: str, idx: int) -> Unit:
        for u in units:
            r = u.a_range if side == "a" else u.b_range
            if r[0] <= idx < r[1]:
                return u
        return units[0]

    # a section missing in stage belongs where the next section present on both sides starts in stage
    # (or at the end of the one before): not where look-alike text (repeated notes, table rows) aligns
    missing_spans: list[tuple[tuple, tuple, Loc | None, str]] = []  # prod (page, y) start, end, stage spot, title
    # web capture: guide pages that failed to load (HTTP 5xx, login form), by their file name slug.
    # A section of such a page is missing because its page is broken, not because the text was left out.
    # (matched by the page's left-navigation title, else by its file name)
    failed_pages = {}
    for s in ((candidate_meta or {}).get("capture") or {}).get("skipped", []):
        for name in (s.get("title", ""), urlsplit(s["url"]).path.rsplit("/", 1)[-1].rsplit(".", 1)[0]):
            if name:
                failed_pages.setdefault(_slug(name), s)
    top_level = min((a.level for a in anA), default=1)

    def failed_page_of(i: int) -> dict | None:
        """The failed stage page holding prod section i: its own, or its chapter's."""
        for k in range(i, -1, -1):
            hit = failed_pages.get(_slug(anA[k].title))
            if hit or anA[k].level <= top_level:
                return hit
        return None

    by_a = sorted((anA[i].word, anB[j].word) for i, j, _ in pairs)
    pos_a = lambda k: (A.words[k].page, A.words[k].bbox[1]) if k < len(A.words) else (len(A.pages), 0.0)
    # web guide: the PDF's printed-only pages - cover, the TOC page, back cover - have no web page.
    # A heading the PDF's own TOC does not list, before its first chapter or after its last one's page.
    html = (candidate_meta or {}).get("mode") == "html"
    in_toc = {e.norm for e in toc_a.entries}
    toc_pages = [an.page for an in anA if an.norm in in_toc]
    # cover / back cover (web guide and PDF alike): a heading the TOC does not list, before its first
    # chapter or after its last chapter's page, is print matter - not a section that can go missing
    print_only = lambda an: (toc_pages and an.norm not in in_toc
                             and (an.page < min(toc_pages) or an.page > max(toc_pages)))
    for i, an in enumerate(anA):
        if scope and (not scope.contains(an.word) or i in scope.title_chapters):
            continue  # outside the part of the PDF this web page covers / matched by the page title
        if i not in matched_a and print_only(an):
            continue
        if i not in matched_a:
            nxt = next(((aw, bw) for aw, bw in by_a if aw > an.word), None)
            prv = next(((aw, bw) for aw, bw in reversed(by_a) if aw < an.word), None)
            if nxt:  # at the next section's heading, even when that heading starts a new page
                w = B.words[nxt[1]]
                spot = Loc(w.page, w.bbox)
            elif prv:
                spot = insertion_loc(B, unit_for("b", prv[1]).b_range[1] - 1, None)
            else:
                spot = None
            end = min((x.word for x in anA if x.word > an.word), default=len(A.words))
            missing_spans.append((pos_a(an.word), pos_a(end), spot, an.title))
            broken = failed_page_of(i)
            # the whole lost section: its word count, its text and every line of it highlighted (heading first)
            body = [k for k in range(an.word, end) if A.words[k].norm] if an.located else []
            if an.located and not body and A.outline:
                # a bookmarked section is a section whatever it holds: a page of dimension drawings, all of its
                # text picture text (left out of the comparison), still goes missing as a section
                body = [k for k in range(an.word, end) if A.words[k].text.strip()]
            if an.located and not body:
                # nothing of it is compared text: its "heading" is a picture's label (a dimension note "Unit: mm"
                # set large on a drawing, in a PDF whose headings are guessed from font sizes) - not a section
                missing_spans.pop()
                continue
            last_page = A.words[end - 1].page + 1 if body else an.page + 1
            pages = f"p.{an.page + 1}" + (f"–{last_page}" if last_page > an.page + 1 else "")
            struct[unit_for("a", an.word).id].append(Finding(
                "structure", cfg["sections"].get("missing_severity", "error"),
                f"Section “{an.title}” (baseline {pages}, {len(body)} words) not found in candidate"
                + (f" - its stage page was not captured ({broken['reason']})" if broken else "")
                + (f": “{snippet(A, body, 24)}”" if len(body) > 1 else ""),
                (locs(A, body, cfg["report"]["max_locs"]) if body else [])
                or ([locs(A, [an.word])[0]] if an.located else [Loc(an.page, (0, an.y, A.pages[an.page].width, an.y + 20))]),
                detail={"heading": an.title, "anchor_side": "baseline", "anchor_word": an.word if an.located else None,
                        "words": len(body), "baseline_text": snippet(A, body, 200) if body else "",
                        **({"stage_page_error": {"url": broken["url"], "reason": broken["reason"]}} if broken else {})},
                candidate_at=spot, critical=True))
    count_a = Counter(an.norm for an in anA)
    count_b = Counter(an.norm for an in anB)
    pos_b = lambda k: (B.words[k].page, B.words[k].bbox[1]) if k < len(B.words) else (len(B.pages), 0.0)
    dup_spans: list[tuple[tuple, tuple, Finding]] = []  # stage (page, y) start, end of a duplicate copy
    for j, an in enumerate(anB):
        # a duplicate copies a prod section: a heading prod does not have (e.g. "Structure" repeated
        # under every menu chapter in stage, plain text in prod) is an extra section, not a duplicate
        if j not in matched_b and count_b[an.norm] > count_a[an.norm] >= 1 and count_b[an.norm] >= 2:
            first = next(x for x in anB if x.norm == an.norm)
            struct[unit_for("b", an.word).id].append(Finding(
                "structure", cfg["sections"].get("duplicate_severity", "error"),
                f"Duplicate section in stage: “{an.title}” appears {count_b[an.norm]} times in stage "
                f"but {count_a[an.norm]} time(s) in prod (again on stage p.{an.page + 1}, first on p.{first.page + 1})",
                [], [locs(B, [an.word])[0]] if an.located else [Loc(an.page, (0, an.y, B.pages[an.page].width, an.y + 20))],
                detail={"kind": "duplicate-section", "heading": an.title, "anchor_side": "candidate",
                        "anchor_word": an.word if an.located else None}, critical=True, types=["duplicate section"]))
            end = min((x.word for x in anB if x.word > an.word), default=len(B.words))
            dup_spans.append((pos_b(an.word), pos_b(end), struct[unit_for("b", an.word).id][-1]))
            continue
        if j not in matched_b:
            struct[unit_for("b", an.word).id].append(Finding(
                "structure", cfg["sections"].get("extra_severity", "warning"),
                f"Extra section “{an.title}” in candidate (p.{an.page + 1})",
                [], [locs(B, [an.word])[0]] if an.located else [],
                detail={"heading": an.title, "anchor_side": "candidate", "anchor_word": an.word if an.located else None}))
    in_order = _increasing(pairs)
    guessed_a, guessed_b = not A.outline, not B.outline  # no bookmarks: levels guessed from font sizes
    if (html or guessed_a or guessed_b) and not scope:
        # compare heading depth, not the H number. A PDF without bookmarks has levels from font sizes
        # (H4/H5/H6), a web guide from h1/h2 per page: a heading listed in the side's own printed TOC
        # takes the TOC's level; else each side's levels are ranked among the matched headings
        ranks_a = sorted({anA[i].level for i, _, _ in pairs})
        ranks_b = sorted({anB[j].level for _, j, _ in pairs})
        def toc_depths(anchors, toc):
            """Depth of each heading from the side's own TOC; a heading the TOC does not list sits one
            level below the listed heading before it (the same level when its type is as large)."""
            listed = {e.norm: e.level for e in toc.entries}
            out, last = {}, None  # last = (toc level, font level) of the previous listed heading
            for k, an in enumerate(anchors):
                if an.norm in listed:
                    out[k] = listed[an.norm]
                    last = (listed[an.norm], an.level)
                elif last:
                    out[k] = last[0] + (1 if an.level > last[1] else 0)
            return out
        lvl_a = toc_depths(anA, toc_a) if guessed_a and toc_a.entries else {}
        lvl_b = toc_depths(anB, toc_b) if guessed_b and toc_b.entries else {}
        depth_a = lambda i: lvl_a.get(i) or ranks_a.index(anA[i].level) + 1
        depth_b = lambda j: lvl_b.get(j) or ranks_b.index(anB[j].level) + 1
    else:
        depth_a = lambda i: anA[i].level
        depth_b = lambda j: anB[j].level + (scope.level_offset if scope else 0)
    for i, j, s in pairs:
        u = unit_for("a", anA[i].word)
        if (i, j) not in in_order:
            struct[u.id].append(Finding(
                "structure", cfg["sections"].get("order_severity", "error"),
                f"Section order differs: “{anA[i].title}” is section #{i + 1} in prod but #{j + 1} in the candidate",
                [locs(A, [anA[i].word])[0]], [locs(B, [anB[j].word])[0]],
                {"kind": "order differs"}, types=["order differs"]))
        if depth_a(i) != depth_b(j):
            struct[u.id].append(Finding(
                "structure", cfg["sections"].get("level_severity", "warning"),
                f"Outline level: H{anA[i].level} → H{anB[j].level}"
                + (f" (heading depth {depth_a(i)} in prod, {depth_b(j)} in stage)" if (html or guessed_a or guessed_b) and not scope else ""),
                [locs(A, [anA[i].word])[0]], [locs(B, [anB[j].word])[0]]))
        if s < 1.0:
            struct[u.id].append(Finding(
                "structure", "warning", f"Heading text: “{anA[i].title}” → “{anB[j].title}”",
                [locs(A, [anA[i].word])[0]], [locs(B, [anB[j].word])[0]]))
        for side, an, d in (("baseline", anA[i], A), ("candidate", anB[j], B)):
            if not an.located:
                struct[u.id].append(Finding(
                    "structure", "info", f"Heading “{an.title}” not found as text on {side} p.{an.page + 1}; "
                                         f"section starts at top of page"))

    doc_findings = _document_findings(A, B, cfg)
    from .checks import notes as checks_notes
    # the same kind of note drawn another way (bar vs filled box): shown in every section that has such notes
    note_findings = checks_notes.place(checks_notes.compare(A, B, cfg), A, units)
    # a picture's callout numbers, labels and leader lines, prod against stage (for the image report)
    from .checks import picture_labels as checks_picture_labels
    picture_findings = checks_picture_labels.check(A, B, units, cfg)
    doc_findings += checks_typography.document(B, cfg)  # stage vs the design spec (config/typography.toml)
    genuine_types = set(cfg.get("genuine", {}).get("types", []))

    # --- run checks
    only_re = re.compile(only, re.I) if only else None
    max_f = cfg["report"].get("max_findings_per_check", 0) or None  # 0 = every finding (no cap)
    ccfg = cfg["content"]
    out_sections, style_map = [], defaultdict(lambda: {"words": 0, "sections": 0})
    sync_points: list[tuple] = []
    ran: list[tuple[Unit, list[Finding], Counter]] = []
    for n, u in enumerate(units):
        if only_re and not only_re.search(u.title):
            continue
        report(0.45 + 0.55 * n / len(units), f"Validating “{u.title}”")
        findings = list(struct.get(u.id, []))
        if n == 0:
            findings += doc_findings + toc_findings
        findings += note_findings.get(u.id, [])
        findings += picture_findings.get(u.id, [])
        truncated: Counter = Counter()
        for fn in PIPELINE:
            res = fn(u)
            for f in res:
                f.types = _types(f)
            # the per-check cap only thins presentation findings (CSS, layout, ...): a critical or
            # genuine issue is never dropped, however many a section has
            keep = lambda f: f.critical or bool(set(f.types) & genuine_types)
            res.sort(key=lambda f: (not keep(f), -SEVERITY_RANK[f.severity]))
            must = [f for f in res if keep(f)]
            rest = [f for f in res if not keep(f)]
            for f in (rest[max_f:] if max_f else []):
                truncated[f.check] += 1
            findings.extend(must + rest[:max_f])
        _resolve_one_sided(u, findings, cfg)
        # stage-only findings inside a duplicate copy of a section are that duplicate, not news of their own
        for start, end, dup in [x for x in dup_spans if x[2].detail.get("kind") == "duplicate-section"]:
            inside = [f for f in findings if f is not dup and f.candidate and not f.baseline
                      and start <= (f.candidate[0].page, f.candidate[0].bbox[1]) < end]
            if inside:
                findings[:] = [f for f in findings if not any(f is x for x in inside)]
                dup.detail = {**dup.detail, "folded": dup.detail.get("folded", 0) + len(inside)}
        # prod-only findings inside a missing section (its text, images, rows) are that section, not
        # issues of their own: folded into the "Section missing" finding, counted there
        for start, end, spot, title in missing_spans:
            head = next((f for f in findings if f.check == "structure" and f.detail.get("heading") == title
                         and "not found in candidate" in f.message), None)
            inside = [f for f in findings if f is not head and f.baseline and not f.candidate
                      and start <= (f.baseline[0].page, f.baseline[0].bbox[1]) < end]
            if head is not None and inside:
                findings[:] = [f for f in findings if not any(f is x for x in inside)]
                head.detail = {**head.detail, "folded": head.detail.get("folded", 0) + len(inside)}
            else:
                for f in inside:  # the section finding sits in another unit: keep them, marked where it belongs
                    if spot:
                        f.candidate_at = spot
                        f.detail = {**f.detail, "in_missing_section": title}
        sync_points += _sync_points(u)
        for f in findings:
            f.types = _types(f)
        ran.append((u, findings, truncated))

    # --- running headers / footers, prod vs stage (taken out of the text comparison, compared on their own)
    for unit, f in checks_footer.compare(A, B, [x for x, _, _ in ran], cfg):
        f.types = f.types or _types(f)
        next(fs for x, fs, _ in ran if x is unit).append(f)

    # --- issues that span sections: image/content in the wrong section, links to the wrong section
    report(0.97, "Relating sections")
    genuine.cross_section([(u, fs) for u, fs, _ in ran], A, B, cfg, (candidate_meta or {}).get("mode", "pdf"),
                          progress=lambda m: report(0.97, f"Relating sections - {m}"))
    # reordered text in a table: the data compared cell by cell (dropped when every cell is the same)
    from .checks import table_cells as checks_table_cells
    checks_table_cells.validate_reordered(ran, A, B, cfg)
    _apply_ignore(ran, A, B, cfg)
    if cfg.get("ignore", {}).get("cover_pages", True):
        _drop_cover_pages(ran, A, B, anA, anB)
    keep_pics = cfg.get("assets", {}).get("report_types")
    if keep_pics is not None:  # [assets] report_types: the only picture issues reported
        keep_pics = set(keep_pics)
        for _, fs, _ in ran:
            fs[:] = [f for f in fs if f.check != "assets" or set(f.types or []) & keep_pics]
    # bold / italic on the same words is a content difference (reported with the text, not as CSS)
    for _, fs, _ in ran:
        for f in fs:
            if f.check == "style" and "emphasis" in (f.types or []):
                f.check = "content"
    _spec_prod_spots(ran, cfg)
    data_min = cfg.get("genuine", {}).get("data_missing_words", 3)
    genuine_skip = set(cfg.get("genuine", {}).get("exclude_checks", []))
    if cfg["report"].get("merge_nearby", True):  # one issue per place: the same kind a few lines apart
        for _, findings, _ in ran:
            # image-report-only findings (one per picture) are never merged into another issue
            apart = [f for f in findings if f.detail.get("image_report_only")]
            findings[:] = _merge_nearby([f for f in findings if not f.detail.get("image_report_only")],
                                        cfg["report"].get("merge_distance", 45)) + apart
    if cfg["report"].get("split_by_page", True):  # every place visible: one issue per stage page
        for _, findings, _ in ran:
            findings[:] = [g for f in findings for g in _split_by_page(f)]
    # the predefined check/category lists, extended with whatever check names actually turned up this run:
    # a new check module is counted and reported even if CHECKS/CATEGORY was never updated for it
    seen_checks = {f.check for _, fs, _ in ran for f in fs}
    all_checks = tuple(dict.fromkeys((*CHECKS, *sorted(seen_checks - set(CHECKS)))))
    all_categories = tuple(dict.fromkeys((*CATEGORIES, *sorted({_category(c) for c in seen_checks} - set(CATEGORIES)))))
    for u, findings, truncated in ran:
        # issues for the image report only (a picture's label missing in stage): kept apart - they do not count
        # in the section's verdict, the other reports or the viewer
        image_only = [f for f in findings if f.detail.get("image_report_only")]
        findings[:] = [f for f in findings if not f.detail.get("image_report_only")]
        per_check = {c: _check_summary([f for f in findings if f.check == c]) for c in all_checks}
        for c, n in truncated.items():
            per_check[c]["truncated"] = n
        for f in findings:
            if f.check == "style" and f.detail.get("kind") != "spec" and "role" in f.detail:  # prod -> stage styles only
                key = (f.detail["role"], f.detail["baseline_style"], f.detail["candidate_style"])
                style_map[key]["words"] += f.detail["words"]
                style_map[key]["sections"] += 1

        # the findings as reported, tagged genuine or not (the PDF report = genuine issues + image issues)
        found = [{"id": f"{len(out_sections):03d}-{k:03d}", "category": _category(f.check), **f.to_json()}
                 for k, f in enumerate(findings)]
        for f in found:
            genuine.tag(f, genuine_types, data_min, genuine_skip, cfg.get("genuine", {}).get("everything", False))
        reported = sum(bool(f["genuine"]) or is_image_issue(f) for f in found)
        # the % itself counts only the content/image/note-style kinds above, never a table merge or
        # any other genuine finding - those still appear in the section's issue list and the report
        counted_for_pct = sum(f["genuine"] and bool(set(f.get("types") or []) & CONTENT_PCT_TYPES) for f in found)

        # --- verdict: the score is 100 % less issue_weight_pct (0.1 %) for each issue in the PDF report;
        # CSS separately, critical always fails
        pct = _issue_score(counted_for_pct, ccfg)
        content_status = ("pass" if pct >= ccfg.get("pass_pct", 98.0)
                          else "warn" if pct >= ccfg.get("warn_pct", 90.0) else "fail")
        # text genuinely absent from stage (not merely moved/reordered), extra text, a missing/broken image
        # or a note-style issue is never cheap enough to pass: at 0.1 % per issue a section would need 20
        # such findings before the flat per-issue score even reaches WARN, hiding a real content loss behind
        # "PASS" - and, worse, the document's overall score (below) drops a passing section's issues entirely,
        # so a real content-percentage issue must never leave a section merely "passing"
        if content_status == "pass" and any(f["genuine"] and set(f["types"]) & CONTENT_PCT_TYPES for f in found):
            content_status = "warn"
        # a section that passes is OK: its issues take nothing off the document's overall score (they are
        # still listed in the report) - but the section's own displayed % is always the real computed score,
        # not rounded up to 100, so a passing section with issues does not look like a clean 100 % match
        counted = 0 if content_status == "pass" else counted_for_pct
        critical = [f for f in findings if f.critical]
        css = [f for f in findings if f.check in ("style", "layout")]
        other = max((SEVERITY_RANK[f.severity] for f in findings
                     if f.check not in ("content", "style", "layout")), default=-1)
        status = ("fail" if critical or content_status == "fail" or other == 2
                  else "warn" if content_status == "warn" or css or other == 1 else "pass")
        out_sections.append({
            "id": u.id,
            "title": u.title,
            "level": u.a_anchor.level if u.a_anchor else 0,
            "candidate_title": u.b_anchor.title if u.b_anchor else u.title,
            "status": status,
            # match_pct: the issue score; word_match_pct: prod words present in stage (the former score)
            "content": {**u.content, "word_match_pct": u.content.get("match_pct", 100.0), "match_pct": pct,
                        "issues": reported, "counted_issues": counted, "status": content_status},
            "css": {"issues": len(css), "status": "warn" if css else "pass",
                    "style": sum(f.check == "style" for f in css), "layout": sum(f.check == "layout" for f in css)},
            "critical": len(critical),
            "similarity": round(u.similarity, 4),
            "baseline": {"start": _pos(A, u.a_range[0]), "end": _pos(A, u.a_range[1]), "words": u.a_range[1] - u.a_range[0]},
            "candidate": {"start": _pos(B, u.b_range[0]), "end": _pos(B, u.b_range[1]), "words": u.b_range[1] - u.b_range[0]},
            "checks": per_check,
            "categories": {c: sum(_category(f.check) == c for f in findings) for c in all_categories},
            "findings": found,
            "image_findings": [{"id": f"{len(out_sections):03d}-i{k:02d}", "category": _category(f.check), **f.to_json()}
                               for k, f in enumerate(image_only)],
        })
        for f in out_sections[-1]["image_findings"]:
            genuine.tag(f, genuine_types, data_min, genuine_skip, cfg.get("genuine", {}).get("everything", False))
        out_sections[-1]["genuine"] = sum(f["genuine"] for f in out_sections[-1]["findings"])

    status = Counter(s["status"] for s in out_sections)
    cstatus = Counter(s["content"]["status"] for s in out_sections)
    base_words = sum(s["content"].get("baseline_words", 0) + s["content"].get("extra_words", 0) for s in out_sections)
    good_words = sum(s["content"].get("matched_words", 0) - s["content"].get("spacing_issues", 0)
                     - s["content"].get("script_issues", 0) for s in out_sections)
    crit_kinds = Counter(_critical_kind(f) for s in out_sections for f in s["findings"] if f["critical"])
    result = {
        "meta": {
            "generated": dt.datetime.now().isoformat(timespec="seconds"),
            "baseline": {**_doc_meta(A, anA), **({"scope": _scope_meta(A, scope)} if scope else {})},
            "candidate": {**_doc_meta(B, anB), **(candidate_meta or {})},
            "mode": (candidate_meta or {}).get("mode", "pdf"),
            "matched_sections": len(pairs),
            "typography": checks_typography.reference(B, cfg),  # the Figma type scale stage is checked against
        },
        "summary": {
            "sections": len(out_sections),
            "pass": status["pass"], "warn": status["warn"], "fail": status["fail"],
            "content": {
                "match_pct": _issue_score(sum(s["content"]["counted_issues"] for s in out_sections), ccfg),
                "issues": sum(s["content"]["issues"] for s in out_sections),
                "counted_issues": sum(s["content"]["counted_issues"] for s in out_sections),
                "issue_weight_pct": ccfg.get("issue_weight_pct", 0.1),
                "word_match_pct": round(100.0 * good_words / base_words, 2) if base_words else 100.0,
                "baseline_words": sum(s["content"].get("baseline_words", 0) for s in out_sections),
                "missing_words": sum(s["content"].get("missing_words", 0) for s in out_sections),
                "extra_words": sum(s["content"].get("extra_words", 0) for s in out_sections),
                "spacing_issues": sum(s["content"].get("spacing_issues", 0) for s in out_sections),
                "pass": cstatus["pass"], "warn": cstatus["warn"], "fail": cstatus["fail"],
                "pass_pct": ccfg.get("pass_pct", 98.0), "warn_pct": ccfg.get("warn_pct", 90.0),
            },
            "critical": {"total": sum(crit_kinds.values()), "by_kind": dict(crit_kinds.most_common())},
            "genuine": {"total": sum(s["genuine"] for s in out_sections),
                        "by_issue": dict(Counter(f["issue"] for s in out_sections for f in s["findings"]
                                                 if f["genuine"]).most_common())},
            "css": {"issues": sum(s["css"]["issues"] for s in out_sections),
                    "style": sum(s["css"]["style"] for s in out_sections),
                    "layout": sum(s["css"]["layout"] for s in out_sections)},
            "by_check": {c: sum(s["checks"].get(c, {}).get("total", 0) for s in out_sections) for c in all_checks},
            "by_category": {c: {"total": sum(1 for s in out_sections for f in s["findings"] if f["category"] == c),
                                "types": dict(Counter(t for s in out_sections for f in s["findings"]
                                                      if f["category"] == c for t in f["types"]).most_common())}
                            for c in all_categories},
            "result": "fail" if status["fail"] else "pass",
        },
        "sync": _monotonic(sync_points, A, B, _scope_end(A, scope)),
        "toc": toc_info,
        "style_map": sorted(
            [{"role": r, "baseline": a, "candidate": b, **v} for (r, a, b), v in style_map.items()],
            key=lambda x: -x["words"]),
        "sections": out_sections,
    }
    aem.annotate(result, cfg)  # source topic (GUID) of every stage issue, for AEM Guides PDFs
    return result


CHECKS = ("toc", "structure", "content", "tables", "assets", "integrity", "style", "layout")

# check -> category shown to users (filters, reports)
CATEGORY = {"content": "content", "assets": "images", "tables": "tables", "structure": "structure",
            "integrity": "links", "style": "css", "layout": "css", "toc": "toc"}
CATEGORIES = ("content", "images", "tables", "toc", "structure", "links", "css")


def _category(check: str) -> str:
    """Category shown to users for a check. A check not in CATEGORY (a new check module whose
    author forgot to register it there) falls back to its own name instead of crashing the whole
    run with a KeyError or silently losing its findings from the category/check summaries."""
    return CATEGORY.get(check, check)
_KIND_TYPE = {  # detail.kind -> type, for checks that tag findings with a kind
    "missing": "missing image", "extra": "extra image", "changed": "image changed", "raster-vs-vector": "raster vs vector",
    "glyph": "broken glyph", "offpage": "text off page", "broken-link": "broken link", "missing-link": "missing link", "extra-link": "extra link",
    "file-missing": "missing file", "file-extra": "extra file", "outline-only": "bookmark only",
    "broken": "broken image",
    "blackout": "image blacked out",
}


_NO_MERGE = {"structure", "toc"}  # one finding per heading / TOC entry


def _merge_nearby(findings: list[Finding], dist: float) -> list[Finding]:
    """Findings of the same kind that one pair of screenshots shows (both sides on the same page,
    within `dist` pt of each other) become one finding: every box marked, every message listed.
    “Switching” and “input” of one link, two word gaps on one line - one issue, not two."""
    def spot(locs_, at):
        if locs_:
            return locs_[0].page, min(l.bbox[1] for l in locs_), max(l.bbox[3] for l in locs_)
        return (at.page, at.bbox[1], at.bbox[3]) if at else None

    def near(p, q):
        if p is None or q is None:
            return p is None and q is None
        return p[0] == q[0] and q[1] - p[2] <= dist and p[1] - q[2] <= dist

    out: list[Finding] = []
    parts: dict[int, list[str]] = {}
    for f in findings:
        g = next((g for g in reversed(out)
                  if f.check not in _NO_MERGE and g.check == f.check and g.types == f.types
                  and g.detail.get("kind") == f.detail.get("kind") and g.severity == f.severity
                  and near(spot(g.baseline, g.baseline_at), spot(f.baseline, f.baseline_at))
                  and near(spot(g.candidate, g.candidate_at), spot(f.candidate, f.candidate_at))), None)
        if g is None:
            out.append(f)
            continue
        n = g.detail.get("merged", 1) + 1
        parts.setdefault(id(g), [g.message]).append(f.message)
        g.baseline, g.candidate = g.baseline + f.baseline, g.candidate + f.candidate
        g.links = (g.links or []) + (f.links or [])
        g.critical = g.critical or f.critical
        g.detail = {**g.detail, "merged": n}
    for g in out:  # "Link missing in stage (2 places): “Switching” …; “input” …"
        msgs = parts.get(id(g))
        if msgs:
            heads = {m.split(":", 1)[0] for m in msgs}
            if any("\n" in m for m in msgs):  # "Figma: … / Stage: …" messages: one block each
                g.message = "\n\n".join(dict.fromkeys(msgs))
            elif len(heads) == 1 and all(":" in m for m in msgs):
                g.message = f"{heads.pop()} ({len(msgs)} places): " + "; ".join(m.split(":", 1)[1].strip() for m in msgs)
            else:
                g.message = "  ·  ".join(msgs)
    return _merge_kinds(out, spot, near)


def _merge_kinds(findings: list[Finding], spot, near) -> list[Finding]:
    """Different content differences at one spot (a colon added AND the bullet dropped on the same
    list items) are one issue: one pair of screenshots marks all of them, the message lists each."""
    rank = {"error": 2, "warning": 1, "info": 0}
    out: list[Finding] = []
    for f in findings:
        g = next((g for g in out if f.check == "content" and g.check == "content"
                  and f.severity != "info" and g.severity != "info" and g.types != f.types
                  and near(spot(g.baseline, g.baseline_at), spot(f.baseline, f.baseline_at))
                  and near(spot(g.candidate, g.candidate_at), spot(f.candidate, f.candidate_at))), None)
        if g is None:
            out.append(f)
            continue
        kinds = g.detail.get("merged_types") or [(g.types or ["content"])[0]]
        # each merged difference keeps its own types, message and details
        parts = g.detail.get("parts") or [{"types": list(g.types), "message": g.message, "detail": dict(g.detail)}]
        parts.append({"types": list(f.types), "message": f.message, "detail": dict(f.detail)})
        g.detail = {**g.detail, "merged_types": kinds + [(f.types or ["content"])[0]], "parts": parts}
        g.message = f"{g.message}\n\n{f.message}" if "\n" in g.message + f.message else f"{g.message}  ·  {f.message}"
        g.types = list(dict.fromkeys(g.types + f.types))
        g.baseline, g.candidate = g.baseline + f.baseline, g.candidate + f.candidate
        g.links = (g.links or []) + (f.links or [])
        g.critical = g.critical or f.critical
        if rank.get(f.severity, 0) > rank.get(g.severity, 0):
            g.severity = f.severity
    return out


def _types(f: Finding) -> list[str]:
    if f.types:
        return f.types
    d = f.detail
    if f.check == "style":
        return [p["property"] for p in d.get("props", [])]
    if f.check == "layout":
        return [d.get("property", "layout")]
    if f.check == "structure":
        if d.get("kind") == "outline-only":
            return ["bookmark only"]
        m = f.message
        return (["missing section"] if "not found in candidate" in m else ["extra section"] if m.startswith("Extra section")
                else ["outline level"] if m.startswith("Outline level") else ["heading text"] if m.startswith("Heading text")
                else ["heading not found"])
    if f.check == "assets":
        return [_KIND_TYPE.get(d.get("kind"), "size / aspect")]
    return [_KIND_TYPE.get(d.get("kind"), f.check)]


def _critical_kind(f: dict) -> str:
    k = f["detail"].get("kind") or f["detail"].get("op") or ""
    return {"missing-row": "table row missing", "missing": "image missing", "glyph": "broken glyph",
            "offpage": "text off page", "broken-link": "broken link", "file-missing": "embedded file missing",
            "delete": "content block missing", "replace": "content block missing", "broken": "image broken",
            "wrong-section": "placed in wrong section", "duplicate-section": "section duplicated",
            "wrong-link-target": "link to wrong section", "missing header": "table header missing"}.get(
        k, "section missing" if f["check"] == "structure" else k or f["check"])


def _document_findings(A: Doc, B: Doc, cfg: dict) -> list[Finding]:
    """Whole-document checks: embedded files (attachments) present in prod must exist in stage."""
    import pymupdf
    out = []
    try:
        fa, fb = set(pymupdf.open(A.path).embfile_names()), set(pymupdf.open(B.path).embfile_names())
    except Exception:
        return out
    for name in sorted(fa - fb):
        out.append(Finding("integrity", "error", f"Embedded file missing in stage: “{name}”",
                           detail={"kind": "file-missing", "file": name}, critical=True))
    for name in sorted(fb - fa):
        out.append(Finding("integrity", "warning", f"Extra embedded file in stage: “{name}”",
                           detail={"kind": "file-extra", "file": name}))
    return out


def _resolve_one_sided(u: Unit, findings: list[Finding], cfg: dict) -> None:
    """Give every one-sided finding a correct counterpart on the other side.

    The content diff pairs identical words across both PDFs, so those pairs are
    the ground truth for "where is this spot in the other document".

    * An unmatched heading whose own text is paired is not a missing/extra
      section: the heading exists on both sides and only the PDF bookmarks
      (outline) differ. It is re-classified and gets boxes on both sides.
    * Anything else that exists on one side only (extra heading, image, text
      without an insertion point) gets `<side>_at`: the position of the nearest
      following paired word on the other side, i.e. where it would appear.
    """
    al = Aligner(u)
    a2b, b2a = al.a2b, al.b2a

    sev = cfg["sections"].get("outline_only_severity", "warning")
    import re as _re
    key = lambda t: _re.sub(r"[^\w]+", "", (t or "").lower())

    def find_title(doc, rng, title):
        """First word of `title` as consecutive words of doc within rng (case and punctuation ignored:
        "Blurred image." heads a stage section, prod has the line "Blurred image")."""
        words = [k for k in (key(t) for t in (title or "").split()) if k]
        if not words:
            return None
        ws = doc.words
        lo, hi = max(0, rng[0]), min(len(ws), rng[1])
        for k in range(lo, hi - len(words) + 1):  # the same words, anywhere
            if all(key(ws[k + n].text) == words[n] for n in range(len(words))):
                return k
        # the same letters split into other words: prod prints a step "6.Turn-on the power." (one word
        # "6.Turn-on"), the stage bookmark reads "6. Turn-on the power." - accepted only as whole lines, as a heading
        # is set (not the same names in a sentence, a list of models or a wrapped table cell "SL5504/ | SH5504")
        want = "".join(words)
        for k in range(lo, hi):
            if doc.lines[ws[k].line].first_word != k or not key(ws[k].text):
                continue
            got, n = "", k
            head = doc.lines[ws[k].line]
            # one line - or a heading wrapped onto the next lines, in the same size above body text ("9.Position your
            # keyboard" / "properly.", 15 pt); not body-size text wrapped in a table cell
            same = lambda m: ws[m].line == ws[k].line or (head.size >= (doc.body_size or 0) + 1
                                                          and abs(doc.lines[ws[m].line].size - head.size) < 0.3)
            while n < hi and len(got) < len(want) and same(n):
                got += key(ws[n].text)
                if not want.startswith(got):
                    break
                n += 1
            if got == want and (n >= len(ws) or ws[n].line != ws[n - 1].line):
                return k
        return None

    span = cfg["sections"].get("heading_search_words", 400)  # how far around the section to look for the text
    for f in findings:
        d = f.detail
        if f.check == "structure" and d.get("anchor_word") is not None:
            w = d["anchor_word"]
            if d["anchor_side"] == "candidate" and w not in b2a and d.get("heading") \
                    and d.get("kind") != "duplicate-section":
                a = find_title(u.a, (u.a_range[0] - span, u.a_range[1] + span), d["heading"])
                if a is not None:
                    b2a = {**b2a, w: a}
            if d["anchor_side"] == "baseline" and w not in a2b and d.get("heading") \
                    and d.get("kind") != "duplicate-section":
                b = find_title(u.b, (u.b_range[0] - span, u.b_range[1] + span), d["heading"])
                if b is not None:
                    a2b = {**a2b, w: b}
            if d["anchor_side"] == "candidate" and w in b2a:
                # the heading's text is in prod right here (a sub-heading prod does not bookmark): not an
                # extra section, nor a second copy of a same-titled section - only the bookmarks differ
                a = b2a[w]
                f.baseline = [locs(u.a, [a])[0]]
                f.severity, d["kind"], f.critical = sev, "outline-only", False
                f.types = ["bookmark only"]
                f.message = (f"Bookmark only in candidate: “{d['heading']}” is an outline entry in stage, "
                             f"but in prod it is plain text (prod p.{u.a.words[a].page + 1})")
                continue
            if d["anchor_side"] == "baseline" and w in a2b:
                b = a2b[w]
                f.candidate, f.candidate_at = [locs(u.b, [b])[0]], None
                f.severity, d["kind"], f.critical = sev, "outline-only", False
                f.types = ["bookmark only"]
                f.message = (f"Bookmark only in baseline: “{d['heading']}” is an outline entry in prod, "
                             f"but in stage it is plain text (stage p.{u.b.words[b].page + 1})")
                continue
        if not f.baseline and f.candidate and f.baseline_at is None:
            c = f.candidate[0]
            j = al.word_at(u.b, u.b_range, c.page, c.bbox[1])
            if j is not None:
                f.baseline_at = al.loc_in_a(j)
        if not f.candidate and f.baseline and f.candidate_at is None:
            c = f.baseline[0]
            i = al.word_at(u.a, u.a_range, c.page, c.bbox[1])
            if i is not None:
                f.candidate_at = al.loc_in_b(i)


def _increasing(pairs: list[tuple]) -> set[tuple]:
    """(i, j) pairs on the longest run increasing in both documents; the rest moved."""
    import bisect as _b
    seq = sorted(pairs)
    tails, idx, prev = [], [], [-1] * len(seq)
    for n, (_, j, _) in enumerate(seq):
        k = _b.bisect_left(tails, j)
        prev[n] = idx[k - 1] if k else -1
        if k == len(tails):
            tails.append(j); idx.append(n)
        else:
            tails[k], idx[k] = j, n
    keep, n = set(), idx[-1] if idx else -1
    while n != -1:
        keep.add((seq[n][0], seq[n][1]))
        n = prev[n]
    return keep


@dataclass
class _Scope:
    ranges: list[tuple[int, int]]  # PDF word ranges of the chapters the page covers
    norms: set  # normalised titles of all headings inside them
    title_chapters: set  # anchor indices of chapters matched by the page title
    title_norms: set
    level_offset: int  # PDF level - page level (page h1 = a PDF level-2 heading -> 1)

    def contains(self, word: int) -> bool:
        return any(a <= word < b for a, b in self.ranges)

    def end_of(self, word: int) -> int:
        return next((b for a, b in self.ranges if a <= word < b), word)


def _page_scope(A: Doc, anA: list[Anchor], anB: list[Anchor], pairs, page_title: str, cfg: dict) -> _Scope | None:
    """The top-level chapter(s) of the PDF a web page covers: the chapters holding the
    page's matched headings, plus the chapter whose title is the page's title (a page
    usually shows the chapter name only as its title / navigation, not as a heading).
    None when nothing ties the page to a chapter (then the whole PDF is compared)."""
    if not anA:
        return None
    top = min(a.level for a in anA)
    chapters = [k for k, a in enumerate(anA) if a.level == top]
    chapter_of = lambda i: max((c for c in chapters if c <= i), default=None)
    end = lambda c: next((anA[k].word for k in chapters if k > c), len(A.words))
    title_chapters = set()
    if page_title.strip():
        page = Anchor(page_title, normalize.title(page_title), top, 0, 0.0, 0)
        title_chapters = {chapters[i] for i, _, _ in sections.match_anchors([anA[c] for c in chapters], [page], cfg)}
    picked = {chapter_of(i) for i, _, _ in pairs} | title_chapters
    picked.discard(None)
    if not picked:
        return None
    ranges = sorted((anA[c].word, end(c)) for c in picked)
    inside = lambda w: any(a <= w < b for a, b in ranges)
    offsets = Counter(anA[i].level - anB[j].level for i, j, _ in pairs if inside(anA[i].word))
    return _Scope(ranges, {a.norm for a in anA if inside(a.word)}, title_chapters,
                  {anA[c].norm for c in title_chapters}, offsets.most_common(1)[0][0] if offsets else 0)


def normalize_heading(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip().lower()


def _sync_points(u: Unit) -> list[tuple]:
    """(prod page, y, stage page, y) positions that show the same thing on both sides:
    the section start and end, every matched word that starts a line on either side
    (a web page wraps lines differently from print), and the top and bottom of every
    matched image. Used by the viewer to scroll both documents together."""
    A, B = u.a, u.b
    pts = []
    if u.a_range[0] < len(A.words) and u.b_range[0] < len(B.words):
        wa, wb = A.words[u.a_range[0]], B.words[u.b_range[0]]
        pts.append((wa.page, wa.bbox[1], wb.page, wb.bbox[1]))
    if u.a_range[1] > u.a_range[0] and u.b_range[1] > u.b_range[0]:
        wa, wb = A.words[u.a_range[1] - 1], B.words[u.b_range[1] - 1]
        pts.append((wa.page, wa.bbox[3], wb.page, wb.bbox[3]))
    for i, j in u.pairs:
        wa, wb = A.words[i], B.words[j]
        if wa.line_start or wb.line_start:
            pts.append((wa.page, wa.bbox[1], wb.page, wb.bbox[1]))
    for ia, ib in u.image_pairs:
        pts.append((ia.page, ia.bbox[1], ib.page, ib.bbox[1]))
        pts.append((ia.page, ia.bbox[3], ib.page, ib.bbox[3]))
    return pts


def _scope_end(A: Doc, scope) -> tuple[int, float] | None:
    """Where the part of the PDF a web page covers ends (page, y)."""
    if not scope:
        return None
    end = max(b for _, b in scope.ranges)
    if end >= len(A.words):
        return len(A.pages) - 1, A.pages[-1].height
    w = A.words[end]
    return w.page, w.bbox[1]


def _scope_meta(A: Doc, scope) -> dict:
    """The part of the PDF a web page covers, for the viewer: first/last page (0-based)
    and where it starts and ends (page, y)."""
    w = A.words[min(a for a, _ in scope.ranges)]
    end = _scope_end(A, scope)
    return {"pages": [w.page, end[0]], "start": {"page": w.page, "y": round(w.bbox[1], 1)},
            "end": {"page": end[0], "y": round(end[1], 1)}}


def _monotonic(pts: list[tuple], A: Doc, B: Doc, a_end: tuple[int, float] | None = None) -> list[list[float]]:
    """Keep the longest subsequence increasing on BOTH sides (moved text would make the
    other pane jump back and forth), plus both documents' first and last positions.
    a_end: where the compared part of the baseline ends (a web page covers one chapter)."""
    import bisect as _b
    pts = sorted(set(pts))
    keys = [(p[2], p[3]) for p in pts]
    tails, tails_idx, prev = [], [], [-1] * len(pts)
    for n, k in enumerate(keys):
        pos = _b.bisect_right(tails, k)
        if pos and tails[pos - 1] == k:
            continue
        prev[n] = tails_idx[pos - 1] if pos else -1
        if pos == len(tails):
            tails.append(k)
            tails_idx.append(n)
        else:
            tails[pos], tails_idx[pos] = k, n
    chain, n = [], tails_idx[-1] if tails_idx else -1
    while n != -1:
        chain.append(pts[n])
        n = prev[n]
    chain.reverse()
    last_a, last_b = len(A.pages) - 1, len(B.pages) - 1
    end_a = a_end or (last_a, A.pages[last_a].height)
    chain = [c for c in chain if (c[0], c[1]) <= end_a]
    start = (chain[0][0], 0.0, 0, 0.0) if a_end and chain else (0, 0.0, 0, 0.0)
    chain = [start] + [c for c in chain if (c[0], c[1]) > start[:2] and (c[2], c[3]) > (0, 0.0)] + \
            [(*end_a, last_b, B.pages[last_b].height)]
    return [[a, round(ya, 1), b, round(yb, 1)] for a, ya, b, yb in chain]


def _check_summary(findings: list[Finding]) -> dict:
    c = Counter(f.severity for f in findings)
    return {"total": len(findings), "error": c["error"], "warning": c["warning"], "info": c["info"]}


def _doc_meta(d: Doc, anchors: list[Anchor]) -> dict:
    return {
        "path": str(Path(d.path).resolve()), "pages": len(d.pages),
        "page_size": [round(d.pages[0].width, 1), round(d.pages[0].height, 1)],
        "words": len(d.words), "body_size": d.body_size, "sections": len(anchors),
        "margins": {"odd": [d.pages[0].left, round(d.pages[0].right, 1)],
                    "even": [d.pages[min(1, len(d.pages) - 1)].left, round(d.pages[min(1, len(d.pages) - 1)].right, 1)]},
        "removed_header_footer_lines": d.removed_lines,
        # glyphs of fonts without a Unicode map, read back from the page (OCR + shape)
        **({"decoded_glyphs": d.decoded} if getattr(d, "decoded", None) else {}),
    }


def _toc_l1(baseline: str, cfg: dict) -> list[str]:
    """The PDF's level-1 TOC titles: its bookmarks, else its printed table of contents."""
    import pymupdf
    l1 = [t.strip() for lvl, t, _ in pymupdf.open(baseline).get_toc() if lvl == 1]
    if l1:
        return l1
    entries = tocmod.detect(extract.load(baseline, "baseline", cfg), cfg).entries
    top = min((e.level for e in entries), default=1)
    return [e.title for e in entries if e.level == top]


def compare_url(baseline: str, url: str, out_dir: str, cfg: dict | None = None, *, html: dict | None = None,
                progress: Callable[[float, str], None] | None = None) -> dict:
    """Validate a PDF (baseline) against a web page (candidate), driven by the TOC.

    The page is rendered and read from its DOM (html_source.capture): words with
    boxes and styles, the h1-h6 outline, real tables, images and links. Sections
    come from the PDF's TOC/bookmarks matched to the page's headings; each section's
    text, tables, images and links are validated like PDF vs PDF. Layout checks
    (indent/alignment) are switched off – web and print layout are not comparable."""
    from . import html_source

    report = progress or (lambda f, m: None)
    cfg = copy.deepcopy(cfg or load_config())
    h = html or {}
    hcfg = cfg.get("html", {})
    crawl = h.get("crawl", hcfg.get("crawl", True))
    max_pages = int(h.get("max_pages") or hcfg.get("max_pages", 0) or 0)
    toc_l1 = _toc_l1(baseline, cfg) if crawl else []
    doc, info = html_source.capture(
        url, out_dir, root=h.get("root", ""), exclude=h.get("exclude") or html_source.DEFAULT_EXCLUDE,
        width=int(h.get("width") or 1280), wait_ms=int(h.get("wait_ms") or 1500),
        user=h.get("user", ""), password=h.get("password", ""), crawl=crawl, max_pages=max_pages,
        site=cfg.get("site", {}) if cfg.get("site", {}).get("enabled", True) else None, toc_l1=toc_l1,
        progress=lambda f, m: report(0.3 * f, m))
    site = info.pop("site", None)
    if site is not None:
        site.setdefault("toc_l1", toc_l1)
    if crawl:  # the whole guide against the whole PDF, not one chapter
        cfg["sections"]["page_scope"] = False
    cfg["layout"]["enabled"] = False
    cfg["layout"]["check_placement"] = False
    cfg["sections"]["front_matter"] = False  # a web page has no cover / printed front matter
    cfg["content"]["spacing_mode"] = "presence"  # browsers collapse repeated spaces: only gap vs no gap is visible
    for k, v in cfg.get("html", {}).items():  # [html] overrides in the config file
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            _merge(cfg[k], v)
    result = compare(baseline, doc.path, cfg, candidate_doc=doc,
                     candidate_meta={"mode": "html", "url": url, "page_title": info["title"], "capture": info},
                     progress=lambda f, m: report(0.3 + 0.7 * f, m))
    if site:  # left navigation, download PDF, next/previous, on this page, product subtitle
        from . import site_nav
        try:
            result["site"] = site_nav.evaluate(site, baseline, cfg.get("site", {}))
            result["summary"]["site"] = {k: result["site"]["summary"][k] for k in ("status", "fail", "warn", "pass")}
        except Exception as e:  # the content result stands on its own
            result["site"] = {"error": f"{type(e).__name__}: {e}"}
    return result


_CALLOUT_WORD = re.compile(r"^\W*(note|notes|warning|important|caution|notice|tip|danger|attention)\b", re.I)


def _ignored_sections(cfg: dict):
    pats = [re.compile(p, re.I) for p in cfg.get("ignore", {}).get("sections", [])]
    return (lambda norm: any(p.search(norm or "") for p in pats)) if pats else None


def _apply_ignore(ran, A: Doc, B: Doc, cfg: dict) -> None:
    """[ignore]: what the validation leaves out entirely (not in any report): finding types, line
    breaks / wrapping inside table cells, indentation of Note / Warning / Important callouts, and
    whole sections by title. A finding with several types keeps the ones not ignored."""
    icfg = cfg.get("ignore", {})
    drop = set(icfg.get("types", []))
    in_table, in_callout = set(icfg.get("in_table_cells", [])), set(icfg.get("in_callouts", []))
    skip_sec = _ignored_sections(cfg)
    if not (drop or in_table or in_callout or skip_sec):
        return
    from .checks import tables as _tables, components as _components
    tables_of: dict = {}
    callouts_of: dict = {}

    def inside(r, box, pad=3):
        return r[0] >= box[0] - pad and r[1] >= box[1] - pad and r[2] <= box[2] + pad and r[3] <= box[3] + pad

    def in_tbl(doc, loc) -> bool:
        key = (id(doc), loc.page)
        if key not in tables_of:
            try:
                tables_of[key] = [t[1] for t in _tables._raw(doc, loc.page)]
            except Exception:
                tables_of[key] = []
        return any(inside(loc.bbox, b) for b in tables_of[key])

    def in_co(doc, loc) -> bool:
        if id(doc) not in callouts_of:
            try:
                callouts_of[id(doc)] = [c for c in _components.callouts(doc, cfg) if c.get("box")]
            except Exception:
                callouts_of[id(doc)] = []
        if any(c["page"] == loc.page and inside(loc.bbox, c["box"]) for c in callouts_of[id(doc)]):
            return True
        # a callout without a box: its paragraph starts with the callout word ("Note: ...")
        ln = next((l for l in doc.lines if l.page == loc.page and l.bbox[1] <= loc.bbox[3] and l.bbox[3] >= loc.bbox[1]), None)
        return bool(ln and _CALLOUT_WORD.match(ln.text))

    def all_in(test, f) -> bool:
        pts = [(A, l) for l in f.baseline] + [(B, l) for l in f.candidate]
        return bool(pts) and all(test(d, l) for d, l in pts)

    title_re = re.compile(r"[“\"]([^”\"]+)[”\"]")
    for u, findings, _ in ran:
        if skip_sec and (skip_sec(normalize.title(u.title or "")) or
                         (u.b_anchor is not None and skip_sec(u.b_anchor.norm))):
            findings.clear()
            continue
        keep = []
        for f in findings:
            types = list(f.types or [])
            if skip_sec and f.check in ("structure", "toc") and (m := title_re.search(f.message)) \
                    and skip_sec(normalize.title(m.group(1))):
                continue
            rm = set(t for t in types if t in drop)
            if set(types) & in_table and all_in(in_tbl, f):
                rm |= set(types) & in_table
            if set(types) & in_callout and all_in(in_co, f):
                rm |= set(types) & in_callout
            if rm:
                rest = [t for t in types if t not in rm]
                if not rest:
                    genuine.credit_dropped(u, f)  # its words do not count against the content % either
                    continue
                f.types = rest
            keep.append(f)
        findings[:] = keep


def _drop_cover_pages(ran, A: Doc, B: Doc, anA: list, anB: list) -> None:
    """[ignore] cover_pages: an issue whose every spot is on the first or last page of its PDF (cover,
    back cover: "BenQ.com © 2025 BenQ Corporation ...") is not reported. Text missing / added there is
    not counted against the section's content match either. A first / last page that holds a section
    heading is content (stage ends on the "Working with a tripod" page: no back cover), and a short
    PDF (a page or two) has no cover."""
    def edge(doc: Doc, anchors: list) -> set:
        if len(doc.pages) < 4 or doc.raw_tables is not None:  # (a web guide has no cover / back cover)
            return set()
        return {0, len(doc.pages) - 1} - {an.page for an in anchors}
    ea, eb = edge(A, anA), edge(B, anB)

    def on_cover(f: Finding) -> bool:  # (the TOC is compared on its own: its spots are the TOC page)
        return f.check != "toc" and bool(f.baseline or f.candidate) and all(l.page in ea for l in f.baseline) \
            and all(l.page in eb for l in f.candidate)

    for u, findings, _ in ran:
        cover = [f for f in findings if on_cover(f)]
        if not cover:
            continue
        findings[:] = [f for f in findings if not any(f is x for x in cover)]
        c = u.content
        if not c:
            continue
        for f in cover:
            if f.check != "content" or f.detail.get("op") not in ("delete", "insert"):
                continue
            n = f.detail.get("words", 0)
            if f.detail["op"] == "delete":
                c["baseline_words"] = max(0, c.get("baseline_words", 0) - n)
                c["missing_words"] = max(0, c.get("missing_words", 0) - f.detail.get("absent_words", n))
            else:
                c["extra_words"] = max(0, c.get("extra_words", 0) - n)
        denom = c.get("baseline_words", 0) + c.get("extra_words", 0)
        good = c.get("matched_words", 0) - c.get("spacing_issues", 0) - c.get("script_issues", 0)
        c["match_pct"] = round(100.0 * max(good, 0) / denom, 2) if denom else 100.0


def _issue_score(issues: int, ccfg: dict) -> float:
    """100 % less [content] issue_weight_pct (default 0.1 %) for each issue in the PDF report, never below 0."""
    return round(max(0.0, 100.0 - ccfg.get("issue_weight_pct", 0.1) * issues), 2)


def _spec_prod_spots(ran, cfg: dict) -> None:
    """A design-spec finding is measured on stage only, but the reader compares with prod: each stage spot
    gets the prod spot holding the same words (the content diff's word pairs, of whichever section holds
    the spot: a document-wide finding lists callouts of every section), so the prod screenshot shows the
    same callout / heading / text as prod prints it."""
    units = [u for u, _, _ in ran if u.pairs]
    if not units:
        return
    A, B = units[0].a, units[0].b
    b2a = {j: i for u in units for i, j in u.pairs}
    by_page: dict[int, list[int]] = defaultdict(list)
    for j, w in enumerate(B.words):
        by_page[w.page].append(j)
    als: dict[int, Aligner] = {}

    def prod_spot(loc):
        js = [j for j in by_page.get(loc.page, []) if (w := B.words[j]).bbox[1] < loc.bbox[3] and w.bbox[3] > loc.bbox[1]
              and w.bbox[0] < loc.bbox[2] and w.bbox[2] > loc.bbox[0]]
        ia = [b2a[j] for j in js if j in b2a]
        if ia:
            ws = [A.words[i] for i in ia if A.words[i].page == A.words[ia[0]].page]
            return Loc(ws[0].page, (min(w.bbox[0] for w in ws), min(w.bbox[1] for w in ws),
                                    max(w.bbox[2] for w in ws), max(w.bbox[3] for w in ws)))
        u = next((u for u in units if js and u.b_range[0] <= js[0] < u.b_range[1]), None)
        if u is not None:
            al = als.setdefault(id(u), Aligner(u))
            return al.loc_in_a(js[0])
        return None

    boxes = None

    def whole_callout(loc):
        """A callout finding: the prod callout around the spot, whole (its title / icon / text)."""
        nonlocal boxes
        if boxes is None:
            from .checks import components as _components
            try:
                boxes = [c for c in _components.callouts(A, cfg) if c.get("box")]
            except Exception:
                boxes = []
        cx, cy = (loc.bbox[0] + loc.bbox[2]) / 2, (loc.bbox[1] + loc.bbox[3]) / 2
        c = next((c for c in boxes if c["page"] == loc.page and c["box"][0] <= cx <= c["box"][2]
                  and c["box"][1] <= cy <= c["box"][3]), None)
        if c:
            return Loc(loc.page, tuple(c["box"]))
        # no coloured box (a callout set between rules): the text block at the spot, its lines one under
        # the other with no paragraph gap, plus an icon at its left
        lines = [l for l in A.lines if l.page == loc.page]
        k = next((n for n, l in enumerate(lines) if l.bbox[1] <= cy <= l.bbox[3]), None)
        if k is None:
            return loc
        lo = hi = k
        gap = lambda a, b: (0 <= b.bbox[1] - a.bbox[3] <= 0.6 * (a.bbox[3] - a.bbox[1])  # the next line, no paragraph gap
                            and abs(a.size - b.size) <= 0.5)
        while lo > 0 and gap(lines[lo - 1], lines[lo]):
            lo -= 1
        while hi + 1 < len(lines) and gap(lines[hi], lines[hi + 1]):
            hi += 1
        blk = lines[lo:hi + 1]
        x0, y0 = min(l.bbox[0] for l in blk), min(l.bbox[1] for l in blk)
        x1, y1 = max(l.bbox[2] for l in blk), max(l.bbox[3] for l in blk)
        icon = [im.bbox for im in A.images if im.page == loc.page and im.bbox[2] <= x0 + 2
                and x0 - im.bbox[2] <= 60 and im.bbox[1] < y1 and im.bbox[3] > y0]
        for b in icon:
            x0, y0, y1 = min(x0, b[0]), min(y0, b[1]), max(y1, b[3])
        return Loc(loc.page, (x0, y0, x1, y1))

    for _, findings, _ in ran:
        for f in findings:
            if f.detail.get("kind") == "spec" and not f.baseline and f.candidate:
                spots = [x for x in (prod_spot(l) for l in f.candidate) if x is not None]
                if any(t.startswith("spec callout") for t in f.types or []):
                    spots = [whole_callout(x) for x in spots]
                f.baseline = spots


def _split_by_page(f: Finding) -> list[Finding]:
    """A finding whose places are on several stage pages (a bullet indent on p.39 and p.40, a link not clickable
    in 3 places) becomes one finding per stage page, so each screenshot shows - and highlights - every place
    of its page. The prod side of each part: the prod boxes paired with its stage boxes (links), else the
    prod boxes on the matching prod page. Spec / TOC / document-wide findings stay whole."""
    pages = sorted({l.page for l in f.candidate})
    if len(pages) < 2 or f.check in ("style", "toc", "structure") or f.detail.get("kind") == "spec":
        return [f]
    import copy
    pb_pages = [l.page for l in f.baseline]
    out = []
    for n, p in enumerate(pages):
        cand = [l for l in f.candidate if l.page == p]
        links = [(a, b) for a, b in f.links if b.page == p]
        base = [a for a, _ in links]
        if not base and f.baseline:
            # no pairs: the prod page in the same position among the prod pages
            pa = sorted(set(pb_pages))
            q = pa[min(n * len(pa) // len(pages), len(pa) - 1)]
            base = [l for l in f.baseline if l.page == q]
        g = copy.copy(f)
        g.candidate, g.baseline, g.links = cand, base, links
        g.detail = {**f.detail, "part": f"{n + 1} of {len(pages)}", "stage_page": p + 1}
        g.message = f"{f.message} [stage p.{p + 1}: part {n + 1} of {len(pages)}]"
        out.append(g)
    return out
