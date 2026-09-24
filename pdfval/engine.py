"""Orchestrates: extract -> sections -> match -> per-section checks -> result dict."""
from __future__ import annotations

import copy
import datetime as dt
import re
import tomllib
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable

from . import extract, sections, toc as tocmod
from .checks import PIPELINE, Aligner, Unit, locs
from .model import SEVERITY_RANK, Anchor, Doc, Finding, Loc

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "default.toml"


def load_config(path: str | None = None) -> dict:
    with open(DEFAULT_CONFIG, "rb") as f:
        cfg = tomllib.load(f)
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


def compare(baseline: str, candidate: str, cfg: dict | None = None, *, only: str | None = None,
            progress: Callable[[float, str], None] | None = None, candidate_doc: Doc | None = None,
            candidate_meta: dict | None = None) -> dict:
    """progress(fraction 0..1, message) is called as the run advances.
    candidate_doc: an already built candidate (e.g. a web page from html_source.capture);
    `candidate` is then the path of its image PDF (screenshots, viewer)."""
    report = progress or (lambda f, m: None)
    cfg = copy.deepcopy(cfg or load_config())
    report(0.0, "Extracting baseline (prod)")
    A = extract.load(baseline, "baseline", cfg)
    report(0.2, "Extracting candidate")
    B = candidate_doc or extract.load(candidate, "candidate", cfg)
    report(0.4, "Matching sections")
    # the printed table of contents is compared on its own (levels, entries, page numbers);
    # its text is taken out of the content diff (page numbers shift with every layout change)
    toc_a, toc_b = tocmod.detect(A, cfg), tocmod.detect(B, cfg)
    for d, t in ((A, toc_a), (B, toc_b)):
        for li in t.lines:
            for w in d.words[d.lines[li].first_word:]:
                if w.line != li:
                    break
                w.norm = ""
    anA, anB = sections.build_anchors(A, cfg), sections.build_anchors(B, cfg)
    tocmod.resolve_pages(toc_a, anA)
    tocmod.resolve_pages(toc_b, anB)
    toc_info, toc_findings = tocmod.compare(toc_a, toc_b, cfg)
    if toc_a.heading and toc_b.heading and normalize_heading(toc_a.heading) != normalize_heading(toc_b.heading):
        toc_findings.insert(0, Finding("toc", "warning", f"TOC heading differs: “{toc_a.heading}” → “{toc_b.heading}”",
                                       detail={"kind": "heading"}, types=["heading differs"]))
    _assign_roles(A, anA)
    _assign_roles(B, anB)
    pairs = sections.match_anchors(anA, anB, cfg)

    # --- build units: [front matter] + one per matched heading, each extends to the next matched heading
    bounds: list[tuple[str, Anchor | None, Anchor | None, int, int]] = []
    if cfg["sections"].get("front_matter", True):
        bounds.append(("Front matter", None, None, 0, 0))
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
        eb = nxt(starts_b, sb, len(B.words))
        slug = _slug(title)
        seen[slug] += 1
        uid = f"{k:03d}-{slug}" + (f"-{seen[slug]}" if seen[slug] > 1 else "")
        units.append(Unit(uid, title, A, B, (sa, max(sa, ea)), (sb, max(sb, eb)), aa, ab, cfg))

    # --- structural findings (unmatched / relevelled headings) attach to the unit containing them
    struct: dict[str, list[Finding]] = defaultdict(list)
    matched_a, matched_b = {i for i, _, _ in pairs}, {j for _, j, _ in pairs}

    def unit_for(side: str, idx: int) -> Unit:
        for u in units:
            r = u.a_range if side == "a" else u.b_range
            if r[0] <= idx < r[1]:
                return u
        return units[0]

    for i, an in enumerate(anA):
        if i not in matched_a:
            struct[unit_for("a", an.word).id].append(Finding(
                "structure", cfg["sections"].get("missing_severity", "error"),
                f"Section “{an.title}” (baseline p.{an.page + 1}) not found in candidate",
                [locs(A, [an.word])[0]] if an.located else [Loc(an.page, (0, an.y, A.pages[an.page].width, an.y + 20))],
                detail={"heading": an.title, "anchor_side": "baseline", "anchor_word": an.word if an.located else None},
                critical=True))
    for j, an in enumerate(anB):
        if j not in matched_b:
            struct[unit_for("b", an.word).id].append(Finding(
                "structure", cfg["sections"].get("extra_severity", "warning"),
                f"Extra section “{an.title}” in candidate (p.{an.page + 1})",
                [], [locs(B, [an.word])[0]] if an.located else [],
                detail={"heading": an.title, "anchor_side": "candidate", "anchor_word": an.word if an.located else None}))
    in_order = _increasing(pairs)
    for i, j, s in pairs:
        u = unit_for("a", anA[i].word)
        if (i, j) not in in_order:
            struct[u.id].append(Finding(
                "structure", cfg["sections"].get("order_severity", "error"),
                f"Section order differs: “{anA[i].title}” is section #{i + 1} in prod but #{j + 1} in the candidate",
                [locs(A, [anA[i].word])[0]], [locs(B, [anB[j].word])[0]],
                {"kind": "order differs"}, types=["order differs"]))
        if anA[i].level != anB[j].level:
            struct[u.id].append(Finding(
                "structure", cfg["sections"].get("level_severity", "warning"),
                f"Outline level: H{anA[i].level} → H{anB[j].level}",
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

    # --- run checks
    only_re = re.compile(only, re.I) if only else None
    max_f = cfg["report"]["max_findings_per_check"]
    ccfg = cfg["content"]
    out_sections, style_map = [], defaultdict(lambda: {"words": 0, "sections": 0})
    sync_points: list[tuple] = []
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
            # critical first, then by severity, so the per-check cap never hides a breaking issue
            res.sort(key=lambda f: (not f.critical, -SEVERITY_RANK[f.severity]))
            kept = [f for f in res if f.critical] + [f for f in res if not f.critical][:max_f]
            for f in res[len(kept):] if len(res) > len(kept) else []:
                truncated[f.check] += 1
            findings.extend(kept)
        _resolve_one_sided(u, findings, cfg)
        sync_points += _sync_points(u)
        for f in findings:
            f.types = _types(f)
        per_check = {c: _check_summary([f for f in findings if f.check == c]) for c in CHECKS}
        for c, n in truncated.items():
            per_check[c]["truncated"] = n
        for f in findings:
            if f.check == "style":
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

    status = Counter(s["status"] for s in out_sections)
    cstatus = Counter(s["content"]["status"] for s in out_sections)
    base_words = sum(s["content"].get("baseline_words", 0) for s in out_sections)
    good_words = sum(s["content"].get("matched_words", 0) - s["content"].get("spacing_issues", 0) for s in out_sections)
    crit_kinds = Counter(_critical_kind(f) for s in out_sections for f in s["findings"] if f["critical"])
    return {
        "meta": {
            "generated": dt.datetime.now().isoformat(timespec="seconds"),
            "baseline": _doc_meta(A, anA),
            "candidate": {**_doc_meta(B, anB), **(candidate_meta or {})},
            "mode": (candidate_meta or {}).get("mode", "pdf"),
            "matched_sections": len(pairs),
        },
        "summary": {
            "sections": len(out_sections),
            "pass": status["pass"], "warn": status["warn"], "fail": status["fail"],
            "content": {
                "match_pct": round(100.0 * good_words / base_words, 2) if base_words else 100.0,
                "baseline_words": base_words,
                "missing_words": sum(s["content"].get("missing_words", 0) for s in out_sections),
                "extra_words": sum(s["content"].get("extra_words", 0) for s in out_sections),
                "spacing_issues": sum(s["content"].get("spacing_issues", 0) for s in out_sections),
                "pass": cstatus["pass"], "warn": cstatus["warn"], "fail": cstatus["fail"],
                "pass_pct": ccfg.get("pass_pct", 98.0), "warn_pct": ccfg.get("warn_pct", 90.0),
            },
            "critical": {"total": sum(crit_kinds.values()), "by_kind": dict(crit_kinds.most_common())},
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
        "sync": _monotonic(sync_points, A, B),
        "toc": toc_info,
        "style_map": sorted(
            [{"role": r, "baseline": a, "candidate": b, **v} for (r, a, b), v in style_map.items()],
            key=lambda x: -x["words"]),
        "sections": out_sections,
    }


CHECKS = ("toc", "structure", "content", "tables", "assets", "integrity", "style", "layout")

# check -> category shown to users (filters, reports)
CATEGORY = {"content": "content", "assets": "images", "tables": "tables", "structure": "structure",
            "integrity": "links", "style": "css", "layout": "css", "toc": "toc"}
CATEGORIES = ("content", "images", "tables", "toc", "structure", "links", "css")
_KIND_TYPE = {  # detail.kind -> type, for checks that tag findings with a kind
    "missing": "missing image", "extra": "extra image", "changed": "image changed", "raster-vs-vector": "raster vs vector",
    "glyph": "broken glyph", "offpage": "text off page", "broken-link": "broken link", "missing-link": "missing link",
    "file-missing": "missing file", "file-extra": "extra file", "outline-only": "bookmark only",
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
            "delete": "content block missing", "replace": "content block missing"}.get(
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
                f.candidate = [locs(u.b, [b])[0]]
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


def normalize_heading(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip().lower()


def _sync_points(u: Unit) -> list[tuple]:
    """(prod page, y, stage page, y) for every line start whose word matched on both
    sides, plus the section start. Used by the viewer to scroll both PDFs together."""
    pts = []
    if u.a_range[0] < len(u.a.words) and u.b_range[0] < len(u.b.words):
        wa, wb = u.a.words[u.a_range[0]], u.b.words[u.b_range[0]]
        pts.append((wa.page, wa.bbox[1], wb.page, wb.bbox[1]))
    for i, j in u.pairs:
        wa, wb = u.a.words[i], u.b.words[j]
        if wa.line_start and wb.line_start:
            pts.append((wa.page, wa.bbox[1], wb.page, wb.bbox[1]))
    return pts


def _monotonic(pts: list[tuple], A: Doc, B: Doc) -> list[list[float]]:
    """Keep the longest subsequence increasing on BOTH sides (moved text would make the
    other pane jump back and forth), plus both documents' first and last positions."""
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
    chain = [(0, 0.0, 0, 0.0)] + [c for c in chain if (c[0], c[1]) > (0, 0.0) and (c[2], c[3]) > (0, 0.0)] + \
            [(last_a, A.pages[last_a].height, last_b, B.pages[last_b].height)]
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
    doc, info = html_source.capture(
        url, out_dir, root=h.get("root", ""), exclude=h.get("exclude") or html_source.DEFAULT_EXCLUDE,
        width=int(h.get("width") or 1280), wait_ms=int(h.get("wait_ms") or 1500),
        progress=lambda f, m: report(0.3 * f, m))
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
    return result
