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
from .checks import PIPELINE, Aligner, Unit, insertion_loc, locs
from .checks import typography as checks_typography
from .model import SEVERITY_RANK, Anchor, Doc, Finding, Loc

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
                  integrity._OFFPAGE, integrity._NAMES, tables._DOCS, tables._RAW, tables._RULES):
        cache.clear()
    placement._shapes.cache_clear()
    from . import ocr
    ocr.reset()


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
    A = extract.load(baseline, "baseline", cfg)
    report(0.2, "Extracting candidate")
    B = candidate_doc or extract.load(candidate, "candidate", cfg)
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
    anA, anB = sections.build_anchors(A, cfg), sections.build_anchors(B, cfg)
    tocmod.resolve_pages(toc_a, anA)
    tocmod.resolve_pages(toc_b, anB)
    _assign_roles(A, anA)
    _assign_roles(B, anB)
    pairs = sections.match_anchors(anA, anB, cfg)
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
    print_only = lambda an: (html and toc_pages and an.norm not in in_toc
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
            struct[unit_for("a", an.word).id].append(Finding(
                "structure", cfg["sections"].get("missing_severity", "error"),
                f"Section “{an.title}” (baseline p.{an.page + 1}) not found in candidate"
                + (f" - its stage page was not captured ({broken['reason']})" if broken else ""),
                [locs(A, [an.word])[0]] if an.located else [Loc(an.page, (0, an.y, A.pages[an.page].width, an.y + 20))],
                detail={"heading": an.title, "anchor_side": "baseline", "anchor_word": an.word if an.located else None,
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
                detail={"kind": "duplicate-section", "heading": an.title}, critical=True, types=["duplicate section"]))
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
    if html and not scope:
        # a whole guide vs the PDF: compare heading depth, not the H number. The PDF's levels come
        # from the cover title (H1) and font sizes (H2, H3, H5), the site's from h1/h2 per page:
        # rank each side's levels among the matched headings (H2/H3/H5 -> 1/2/3, h1/h2 -> 1/2)
        ranks_a = sorted({anA[i].level for i, _, _ in pairs})
        ranks_b = sorted({anB[j].level for _, j, _ in pairs})
        depth_a = lambda i: ranks_a.index(anA[i].level) + 1
        depth_b = lambda j: ranks_b.index(anB[j].level) + 1
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
                + (f" (heading depth {depth_a(i)} in prod, {depth_b(j)} in stage)" if html and not scope else ""),
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
        for start, end, dup in dup_spans:
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

    # --- issues that span sections: image/content in the wrong section, links to the wrong section
    report(0.97, "Relating sections")
    genuine.cross_section([(u, fs) for u, fs, _ in ran], A, B, cfg, (candidate_meta or {}).get("mode", "pdf"),
                          progress=lambda m: report(0.97, f"Relating sections - {m}"))
    data_min = cfg.get("genuine", {}).get("data_missing_words", 3)
    genuine_skip = set(cfg.get("genuine", {}).get("exclude_checks", []))
    for u, findings, truncated in ran:
        per_check = {c: _check_summary([f for f in findings if f.check == c]) for c in CHECKS}
        for c, n in truncated.items():
            per_check[c]["truncated"] = n
        for f in findings:
            if f.check == "style" and f.detail.get("kind") != "spec":  # prod -> stage styles only
                key = (f.detail["role"], f.detail["baseline_style"], f.detail["candidate_style"])
                style_map[key]["words"] += f.detail["words"]
                style_map[key]["sections"] += 1

        # --- verdict: content by percentage, CSS separately, critical always fails
        pct = u.content.get("match_pct", 100.0)
        content_status = ("pass" if pct >= ccfg.get("pass_pct", 98.0)
                          else "warn" if pct >= ccfg.get("warn_pct", 90.0) else "fail")
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
            "content": {**u.content, "status": content_status},
            "css": {"issues": len(css), "status": "warn" if css else "pass",
                    "style": sum(f.check == "style" for f in css), "layout": sum(f.check == "layout" for f in css)},
            "critical": len(critical),
            "similarity": round(u.similarity, 4),
            "baseline": {"start": _pos(A, u.a_range[0]), "end": _pos(A, u.a_range[1]), "words": u.a_range[1] - u.a_range[0]},
            "candidate": {"start": _pos(B, u.b_range[0]), "end": _pos(B, u.b_range[1]), "words": u.b_range[1] - u.b_range[0]},
            "checks": per_check,
            "categories": {c: sum(CATEGORY[f.check] == c for f in findings) for c in CATEGORIES},
            "findings": [{"id": f"{len(out_sections):03d}-{k:03d}", "category": CATEGORY[f.check], **f.to_json()}
                         for k, f in enumerate(findings)],
        })
        for f in out_sections[-1]["findings"]:
            genuine.tag(f, genuine_types, data_min, genuine_skip)
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
        },
        "summary": {
            "sections": len(out_sections),
            "pass": status["pass"], "warn": status["warn"], "fail": status["fail"],
            "content": {
                "match_pct": round(100.0 * good_words / base_words, 2) if base_words else 100.0,
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
            "by_check": {c: sum(s["checks"].get(c, {}).get("total", 0) for s in out_sections) for c in CHECKS},
            "by_category": {c: {"total": sum(1 for s in out_sections for f in s["findings"] if f["category"] == c),
                                "types": dict(Counter(t for s in out_sections for f in s["findings"]
                                                      if f["category"] == c for t in f["types"]).most_common())}
                            for c in CATEGORIES},
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
_KIND_TYPE = {  # detail.kind -> type, for checks that tag findings with a kind
    "missing": "missing image", "extra": "extra image", "changed": "image changed", "raster-vs-vector": "raster vs vector",
    "glyph": "broken glyph", "offpage": "text off page", "broken-link": "broken link", "missing-link": "missing link",
    "file-missing": "missing file", "file-extra": "extra file", "outline-only": "bookmark only",
    "broken": "broken image",
    "blackout": "image blacked out",
}


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
    for f in findings:
        d = f.detail
        if f.check == "structure" and d.get("anchor_word") is not None:
            w = d["anchor_word"]
            if d["anchor_side"] == "candidate" and w in b2a:
                a = b2a[w]
                f.baseline = [locs(u.a, [a])[0]]
                f.severity, d["kind"], f.critical = sev, "outline-only", False
                f.message = (f"Bookmark only in candidate: “{d['heading']}” is an outline entry in stage, "
                             f"but in prod it is plain text (prod p.{u.a.words[a].page + 1})")
                continue
            if d["anchor_side"] == "baseline" and w in a2b:
                b = a2b[w]
                f.candidate, f.candidate_at = [locs(u.b, [b])[0]], None
                f.severity, d["kind"], f.critical = sev, "outline-only", False
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
