"""Site navigation of a web guide: is the chrome around the content right?

The content checks read only the page's main content (nav, header and footer are
excluded). This module reads that chrome on every captured page and validates it:

  left navigation   every level-1 entry of the PDF's TOC is in the left navigation, at the top
                    level and in the same order; every navigation link opens the right page
                    (HTTP status, page title/heading = link text, #fragment exists); the
                    highlighted entry is the page itself; the navigation is the same on every page
  download PDF      the button is there on every page, the file downloads, is a PDF, and is the
                    same document as prod (page count, text)
  next / previous   each page's next / previous topic is the next / previous page of the
                    navigation, its label names that page, the first page has no previous
                    and the last no next
  on this page      the right-hand list names every sub-heading of the page, in order, and each
                    entry jumps to its heading
  product subtitle  the product title in the header: which element it is read from, the AEM
                    property it comes from (author instance), the same on every page, and whether
                    it names the product of the prod PDF (cover / title); the version in the footer

Everything is found by heuristics (labels such as "Table of contents", "On this page",
"Next topic", "Download PDF"); `[site]` in the config takes CSS selectors when a site
needs them.
"""
from __future__ import annotations

import csv
import re
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import normalize

# Runs in the page: the chrome around the content.
CHROME_JS = r"""
([sel, rootSel]) => {
  const root = (rootSel && document.querySelector(rootSel)) || document.querySelector('main') ||
               document.querySelector('article') || document.body;
  const W = document.documentElement.clientWidth || 1280;
  const txt = el => (el ? (el.innerText || el.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const visible = el => !!el && (el.checkVisibility ? el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true }) : el.offsetParent !== null);
  const box = el => { const r = el.getBoundingClientRect(); return [Math.round(r.left + scrollX), Math.round(r.top + scrollY), Math.round(r.right + scrollX), Math.round(r.bottom + scrollY)]; };
  const path = el => {  // short CSS path, so the report can say where a value is read from
    const out = [];
    for (let e = el; e && e.nodeType === 1 && out.length < 5; e = e.parentElement) {
      if (e.id) { out.unshift(e.tagName.toLowerCase() + '#' + CSS.escape(e.id)); break; }
      const cls = [...e.classList].filter(c => !/^(is-|has-|active|current|selected|open)/.test(c)).slice(0, 2);
      out.unshift(e.tagName.toLowerCase() + cls.map(c => '.' + CSS.escape(c)).join(''));
      if (e === document.body) break;
    }
    return out.join(' > ');
  };
  const q = s => { try { return s ? document.querySelector(s) : null; } catch (e) { return null; } };
  const qa = s => { try { return s ? [...document.querySelectorAll(s)] : []; } catch (e) { return []; } };
  // the smallest element around a label that holds at least `min` links
  const panelOf = (label, min) => { for (let e = label; e && e !== document.body; e = e.parentElement) if (e.querySelectorAll('a[href]').length >= min) return e; return null; };
  const labelled = re => qa('h1,h2,h3,h4,h5,h6,p,span,div,strong,b,button,summary,label,[aria-label]').find(e =>
    (re.test(txt(e)) && txt(e).length < 40 && !e.querySelector('a[href]') && e.children.length <= 2) || re.test(e.getAttribute('aria-label') || ''));
  const ACTIVE = /(^|[-_\s])(active|current|selected|is-active|is-current)($|[-_\s])/i;
  const items = (panel, skipSel) => {
    if (!panel) return [];
    const links = [...panel.querySelectorAll('a[href]')].filter(a => !(skipSel && a.closest(skipSel)) && txt(a));
    const lists = links.map(a => { let n = 0; for (let e = a.parentElement; e && e !== panel; e = e.parentElement) if (/^(UL|OL)$/.test(e.tagName)) n++; return n; });
    const minList = Math.min(...lists, 99);
    const xs = links.map(a => box(a)[0]), minX = Math.min(...xs, 1e9);
    return links.map((a, k) => {
      const li = a.closest('li');
      const active = a.getAttribute('aria-current') && a.getAttribute('aria-current') !== 'false' ||
                     ACTIVE.test(a.className || '') || (li && ACTIVE.test(li.className || '') && li.querySelector('a[href]') === a);
      const depth = minList < 99 && lists.some(n => n !== minList) ? lists[k] - minList : Math.round((xs[k] - minX) / 12) > 0 ? 1 : 0;
      return { text: txt(a), href: a.href, raw: a.getAttribute('href'), depth, active: !!active, visible: visible(a), box: box(a) };
    });
  };
  // left navigation (table of contents)
  let nav = q(sel.nav), navLabel = '';
  if (!nav) {
    const l = labelled(/^(table of )?contents$|^in this (guide|manual)$/i);
    if (l) { nav = panelOf(l, 2); navLabel = txt(l); }
  }
  if (!nav) {
    const cands = qa('nav, aside, [role="navigation"]').filter(e => { const b = box(e); return (b[0] + b[2]) / 2 < W * 0.35 && e.querySelectorAll('a[href]').length >= 2; });
    nav = cands.sort((a, b) => b.querySelectorAll('a[href]').length - a.querySelectorAll('a[href]').length)[0] || null;
  }
  // on this page (right side)
  let otp = q(sel.on_this_page), otpLabel = '';
  if (!otp) {
    const l = labelled(/^(on this page|in this (topic|article|page|section)|page contents|contents on this page)$/i);
    if (l) { otp = panelOf(l, 1); otpLabel = txt(l); }
  }
  if (!otp) {
    const here = location.href.split('#')[0];
    const cands = qa('nav, aside, [role="navigation"], div').filter(e => { const b = box(e); const a = [...e.querySelectorAll('a[href]')];
      return (b[0] + b[2]) / 2 > W * 0.65 && a.length >= 1 && a.every(x => x.href.split('#')[0] === here && x.href.includes('#')); });
    otp = cands.sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length)[0] || null;
  }
  if (nav && otp && (nav.contains(otp) || otp.contains(nav))) otp = null;
  const panels = [nav, otp].filter(Boolean);
  const inPanels = el => panels.some(p => p.contains(el));
  // next / previous topic
  const pager = (s, re) => {
    let a = q(s);
    if (!a) {
      const c = qa('a[href], button, [role="link"]').filter(e => !inPanels(e) && (re.test(txt(e)) || re.test(e.getAttribute('aria-label') || '') ||
                re.test(e.getAttribute('rel') || '') || re.test(e.getAttribute('title') || '')));
      a = c[c.length - 1] || null;  // the pager at the end of the topic, not a carousel arrow on top
    }
    if (!a) return null;
    const link = a.closest('a[href]') || a.querySelector('a[href]') || a;
    const words = t => /[\p{L}\p{N}]/u.test(t) ? t : '';
    let label = words(txt(link).replace(re, '').replace(/topic|page|[←→‹›«»<>]/gi, '').trim());
    if (!label) {  // "NEXT TOPIC →" with the target's title printed next to it; stop before the other pager
      for (let e = link.parentElement; e && e !== document.body && !label; e = e.parentElement) {
        if ([...e.querySelectorAll('a[href]')].some(x => x.href !== link.href)) break;
        label = words(txt(e).replace(txt(link), '').replace(re, '').replace(/topic|[←→‹›«»<>]/gi, '').trim());
      }
    }
    return { text: txt(link), label: label.slice(0, 200), href: link.href || '', where: path(link), visible: visible(link) };
  };
  const next = pager(sel.next, /\bnext\b/i), prev = pager(sel.prev, /\bprev(ious)?\b/i);
  // download PDF
  const dl = (sel.download ? qa(sel.download) : qa('a[href], button, [role="button"]').filter(e =>
      /download/i.test(txt(e) + ' ' + (e.getAttribute('aria-label') || '') + ' ' + (e.getAttribute('title') || '')) ||
      /\.pdf(\?|#|$)/i.test(e.getAttribute('href') || '') || e.hasAttribute('download')))
    .filter(e => !inPanels(e) || /pdf/i.test(txt(e)))
    .map(e => ({ text: txt(e) || e.getAttribute('aria-label') || '', href: e.href || e.getAttribute('data-href') || e.getAttribute('data-url') || '',
                 tag: e.tagName.toLowerCase(), where: path(e), visible: visible(e) }));
  // header: product title (subtitle)
  const header = q(sel.header) || qa('header, [role="banner"]').filter(visible)[0] || null;
  const headTexts = [];
  const scope = header ? [...header.querySelectorAll('*')] : qa('body *').filter(e => { const b = box(e); return b[3] < 160 && !root.contains(e); });
  for (const e of scope) {
    if (!visible(e) || e.closest('button, select, input, [role="button"], [role="search"], form, [role="listbox"]') || inPanels(e)) continue;
    const own = [...e.childNodes].filter(n => n.nodeType === 3).map(n => n.data).join(' ').replace(/\s+/g, ' ').trim();
    if (!own || own.length > 120 || /download|search|english|language/i.test(own)) continue;
    const cs = getComputedStyle(e), b = box(e);
    const logo = !!(e.closest('a') && e.closest('a').querySelector('img, svg'));
    const hint = /product|subtitle|sub-title|guide-?title|doc-?title|book-?title|manual|series|title/i.test((e.className || '') + ' ' + (e.id || '') + ' ' + (e.parentElement ? e.parentElement.className : ''));
    headTexts.push({ text: own, where: path(e), size: parseFloat(cs.fontSize), weight: +cs.fontWeight || 400, box: b, logo, hint });
  }
  let sub = q(sel.subtitle);
  let subtitle = null;
  if (sub) subtitle = { text: txt(sub), where: path(sub), how: 'selector' };
  else if (headTexts.length) {
    const top = Math.min(...headTexts.map(h => h.box[1]));
    const score = h => (h.hint ? 3 : 0) + (h.weight >= 600 ? 1 : 0) + (h.logo ? -3 : 0) + (h.box[1] > top + 10 ? 1 : 0) + h.size / 100;
    const best = [...headTexts].sort((a, b) => score(b) - score(a))[0];
    // parts on the same row next to it ("Monitor arm BSH Series | Monitor")
    const row = headTexts.filter(h => h !== best && Math.abs(h.box[1] - best.box[1]) < 12 && h.box[0] > best.box[0] && h.box[0] - best.box[2] < 120 && !h.logo);
    subtitle = { text: best.text, where: best.where, how: 'header heuristic', also: row.map(h => h.text) };
  }
  const footer = q(sel.footer) || qa('footer, [role="contentinfo"]').filter(visible).pop() || null;
  const crumbs = q(sel.breadcrumb) || qa('[aria-label*="breadcrumb" i], .breadcrumb, .breadcrumbs, [class*="breadcrumb"]')[0] || null;
  const heads = [...root.querySelectorAll('h1,h2,h3,h4,h5,h6')].filter(h => visible(h) && txt(h) && !inPanels(h))
    .map(h => ({ level: +h.tagName[1], text: txt(h), id: h.id || (h.closest('[id]') && h.closest('[id]') !== root ? h.closest('[id]').id : '') ||
                 (h.querySelector('[id], a[name]') ? (h.querySelector('[id]')?.id || h.querySelector('a[name]').getAttribute('name')) : '') }));
  // what each #fragment of this page lands on
  const targets = {};
  for (const it of items(otp)) {
    const f = decodeURIComponent((it.href.split('#')[1] || ''));
    if (!f || f in targets) continue;
    const t = document.getElementById(f) || document.getElementsByName(f)[0];
    targets[f] = t ? (t.matches('h1,h2,h3,h4,h5,h6') ? txt(t) : txt(t.querySelector('h1,h2,h3,h4,h5,h6')) || txt(t).slice(0, 120)) : null;
  }
  return {
    url: location.href, title: document.title, h1: txt(root.querySelector('h1')),
    nav: nav ? { label: navLabel, where: path(nav), items: items(nav) } : null,
    otp: otp ? { label: otpLabel, where: path(otp), items: items(otp), targets } : null,
    next, prev, download: dl, subtitle, header: headTexts.map(({ text, where, size, weight }) => ({ text, where, size, weight })),
    footer: footer ? txt(footer).slice(0, 400) : '', breadcrumb: crumbs ? txt(crumbs) : '', headings: heads,
    ids: [...document.querySelectorAll('[id]')].map(e => e.id).slice(0, 5000),
  };
}
"""

SELECTOR_KEYS = ("nav", "on_this_page", "next", "prev", "download", "header", "subtitle", "footer", "breadcrumb")


def read_chrome(page, cfg: dict, root: str = "") -> dict | None:
    """The chrome of the loaded page (CHROME_JS); None when it cannot be read."""
    sel = {k: (cfg or {}).get(k, "") for k in SELECTOR_KEYS}
    try:
        return page.evaluate(CHROME_JS, [sel, root])
    except Exception as e:  # never fail the content run because of the chrome
        return {"url": page.url, "error": f"{type(e).__name__}: {e}"}


def key(u: str) -> str:
    """scheme://host/path: the page a link opens (query and #fragment aside)."""
    s = urlsplit(u or "")
    return f"{s.scheme}://{s.netloc}{s.path}"


def probe(page, pages: list[dict], visited: dict, out: Path, report=None) -> dict:
    """With the browser still open: open what the chrome points to.
    - every navigation / next / previous target that the crawl did not open (HTTP status, title)
    - the Download PDF file (saved as site-download.pdf)
    - the AEM property the header's product title is read from (author instance only)"""
    say = report or (lambda m: None)
    targets = {}
    for p in pages:
        for it in ((p.get("nav") or {}).get("items") or []):
            targets.setdefault(key(it["href"]), it["href"])
        for pg in (p.get("next"), p.get("prev")):
            if pg and pg.get("href"):
                targets.setdefault(key(pg["href"]), pg["href"])
    status = dict(visited)
    todo = [(k, u) for k, u in targets.items() if k not in status and urlsplit(u).scheme in ("http", "https")]
    for n, (k, u) in enumerate(todo[:200]):
        say(f"Checking navigation link {n + 1}/{len(todo)}")
        try:
            r = page.request.get(u, timeout=30_000, max_redirects=10)
            body = r.text() if "html" in (r.headers.get("content-type") or "") else ""
            m = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
            h = re.search(r"<h1[^>]*>(.*?)</h1>", body, re.S | re.I)
            strip = lambda s: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()
            ids = re.findall(r"""\s(?:id|name)=["']([^"']+)["']""", body)
            status[k] = {"status": r.status, "url": r.url, "title": strip(m.group(1)) if m else "",
                         "h1": strip(h.group(1)) if h else "", "ids": ids[:5000], "crawled": False}
        except Exception as e:
            status[k] = {"status": 0, "url": u, "error": f"{type(e).__name__}: {e}", "crawled": False}
    return {"links": status, "download": _download(page, pages, out, say), "subtitle_source": _subtitle_source(page, pages)}


def _download(page, pages: list[dict], out: Path, say) -> dict:
    """Fetch the Download PDF file of the first page that has one."""
    first = next(((p, d) for p in pages for d in (p.get("download") or []) if d.get("visible")), None) or \
        next(((p, d) for p in pages for d in (p.get("download") or [])), None)
    if not first:
        return {"found": False}
    p, d = first
    res = {"found": True, "text": d["text"], "href": d["href"], "where": d["where"], "page": p["url"]}
    path = out / "site-download.pdf"
    say("Downloading the site's PDF")
    try:
        if d["href"] and urlsplit(d["href"]).scheme in ("http", "https"):
            r = page.request.get(d["href"], timeout=90_000, max_redirects=10)
            res.update(status=r.status, content_type=r.headers.get("content-type", ""), final_url=r.url)
            data = r.body()
        else:  # a button that starts the download from script
            if key(page.url) != key(p["url"]):
                page.goto(p["url"], wait_until="load", timeout=90_000)
            with page.expect_download(timeout=60_000) as info:
                page.locator(d["where"] or "body").first.click()
            dl = info.value
            dl.save_as(path)
            data = path.read_bytes()
            res.update(status=200, content_type="(browser download)", final_url=dl.url, filename=dl.suggested_filename)
        res["bytes"] = len(data)
        res["is_pdf"] = data[:5] == b"%PDF-"
        if res["is_pdf"]:
            path.write_bytes(data)
            res["file"] = path.name
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"
    return res


def _subtitle_source(page, pages: list[dict]) -> dict:
    """AEM: the page (or a parent page) property whose value is the product title shown in the header.
    Reads <page>/jcr:content.json up the tree; works on author / where the Sling JSON is open."""
    sub = next((p["subtitle"]["text"] for p in pages if p.get("subtitle")), "")
    if not sub or not pages:
        return {}
    s = urlsplit(pages[0]["url"])
    if "/content/" not in s.path:
        return {}
    parts = re.sub(r"\.html$", "", s.path).split("/")
    want = normalize.title(sub)
    tried = []
    for n in range(len(parts), 2, -1):
        base = "/".join(parts[:n])
        url = f"{s.scheme}://{s.netloc}{base}/jcr:content.json"
        tried.append(base)
        try:
            r = page.request.get(url, timeout=15_000)
            if r.status != 200:
                continue
            props = r.json()
        except Exception:
            continue
        hits = [k for k, v in props.items() if isinstance(v, str) and normalize.title(v) == want]
        near = [k for k, v in props.items() if isinstance(v, str) and 3 < len(v) < 200 and want and want in normalize.title(v)]
        if hits or near:
            k = (hits or near)[0]
            return {"page": base, "property": k, "value": props[k], "exact": bool(hits)}
    return {"not_found_in": tried}


# ---------------------------------------------------------------- validation against the PDF

def _norm(s: str) -> str:
    return normalize.title(re.sub(r"^\s*\d+(\.\d+)*\.?\s+", "", s or ""))


def _same(a: str, b: str, ratio: float = 0.9) -> bool:
    a, b = _norm(a), _norm(b)
    return bool(a and b) and (a == b or SequenceMatcher(None, a, b).ratio() >= ratio)


def _row(group: str, status: str, item: str, expected: str = "", actual: str = "", page: str = "", note: str = "",
         where: str = "") -> dict:
    return {"group": group, "status": status, "item": item, "expected": expected, "actual": actual,
            "page": page, "note": note, "where": where}


GROUPS = [("nav", "Left navigation — L1 TOC"), ("links", "Navigation links"), ("download", "Download PDF"),
          ("pager", "Next / previous topic"), ("otp", "On this page"), ("subtitle", "Product subtitle")]


def evaluate(site: dict, baseline: str, cfg: dict | None = None) -> dict:
    """Validate the captured chrome against the prod PDF. Returns {summary, groups, rows, pages}."""
    import pymupdf

    cfg = cfg or {}
    pages = [p for p in site.get("pages") or [] if not p.get("error")]
    links = site.get("links") or {}
    rows: list[dict] = []
    prod = pymupdf.open(baseline)
    l1 = [t for lvl, t, _ in prod.get_toc() if lvl == 1]
    l1_source = "PDF bookmarks (level 1)"
    if not l1 and site.get("toc_l1"):
        l1, l1_source = site["toc_l1"], "printed TOC (level 1)"

    # ---- left navigation: all L1 TOC entries
    navs = [p for p in pages if p.get("nav") and p["nav"]["items"]]
    if not pages:
        rows.append(_row("nav", "fail", "Chrome", note="No page could be read"))
    elif not navs:
        rows.append(_row("nav", "fail", "Left navigation", "a table of contents on every page", "not found",
                         note="Set [site] nav = \"<css selector>\" if the site has one"))
    else:
        ref = navs[0]
        items = ref["nav"]["items"]
        top = [it for it in items if it["depth"] == 0]
        used, order = set(), []
        for t in l1:
            k = next((i for i, it in enumerate(top) if i not in used and _same(t, it["text"])), None)
            if k is not None:
                used.add(k)
                order.append(k)
                rows.append(_row("nav", "pass", t, t, top[k]["text"], ref["url"], "top level", ref["nav"]["where"]))
                continue
            deeper = next((it for it in items if it["depth"] > 0 and _same(t, it["text"])), None)
            if deeper:
                rows.append(_row("nav", "warn", t, "top level", f"{deeper['text']} (level {deeper['depth'] + 1})", ref["url"],
                                 "In the navigation, but not as a top-level entry", ref["nav"]["where"]))
            else:
                rows.append(_row("nav", "fail", t, t, "missing", ref["url"], "L1 TOC entry not in the left navigation",
                                 ref["nav"]["where"]))
        if order != sorted(order):
            rows.append(_row("nav", "fail", "Order", " → ".join(top[k]["text"] for k in sorted(order)),
                             " → ".join(top[k]["text"] for k in order), ref["url"], "L1 entries are in a different order than the PDF TOC"))
        for i, it in enumerate(top):
            if i not in used:
                rows.append(_row("nav", "info", it["text"], "", it["text"], ref["url"], "Top-level navigation entry that is not an L1 TOC entry"))
        sig = lambda p: [(it["text"], key(it["href"])) for it in p["nav"]["items"]]
        for p in navs[1:]:
            if sig(p) != sig(ref):
                a, b = {x[0] for x in sig(ref)}, {x[0] for x in sig(p)}
                diff = ", ".join(f"-{x}" for x in sorted(a - b)) + " " + ", ".join(f"+{x}" for x in sorted(b - a))
                rows.append(_row("nav", "warn", "Same navigation on every page", f"{len(sig(ref))} entries as on the first page",
                                 f"{len(sig(p))} entries {diff.strip()}", p["url"], "Navigation differs from page to page"))
        for p in pages:
            if not p.get("nav"):
                rows.append(_row("nav", "fail", "Left navigation", "present", "not found", p["url"]))
        l1_note = f"{len(l1)} L1 entries from the {l1_source}; {len(top)} top-level navigation entries"
        rows.insert(0, _row("nav", "pass" if l1 else "warn", "L1 entries compared", str(len(l1)), str(len(top)), ref["url"], l1_note,
                            ref["nav"]["where"]))

    # ---- navigation links: open, land on the right page, active entry
    seen = set()
    for p in navs:
        for it in p["nav"]["items"]:
            k = key(it["href"])
            if (k, it["text"]) in seen:
                continue
            seen.add((k, it["text"]))
            rows.append(_link_row("links", it["text"], it["href"], links, p["url"]))
    for p in navs:
        act = [it for it in p["nav"]["items"] if it["active"]]
        own = [it for it in p["nav"]["items"] if key(it["href"]) == key(p["url"]) and "#" not in (it["raw"] or "")]
        if not own:
            rows.append(_row("links", "warn", "Page in the navigation", _title(p), "not listed", p["url"],
                             "The page is not an entry of the left navigation"))
        elif not act:
            rows.append(_row("links", "warn", "Highlighted entry", own[0]["text"], "nothing highlighted", p["url"],
                             "The current page is not marked in the navigation"))
        elif not any(key(it["href"]) == key(p["url"]) for it in act):
            rows.append(_row("links", "fail", "Highlighted entry", own[0]["text"], ", ".join(a["text"] for a in act), p["url"],
                             "The navigation highlights another page"))
        else:
            rows.append(_row("links", "pass", "Highlighted entry", own[0]["text"], act[0]["text"], p["url"]))

    # ---- download PDF
    dl = site.get("download") or {}
    if not dl.get("found"):
        rows.append(_row("download", "fail", "Download PDF button", "present", "not found", pages[0]["url"] if pages else "",
                         "Set [site] download = \"<css selector>\" if the site has one"))
    else:
        rows.append(_row("download", "pass", "Download PDF button", "present", dl["text"], dl["page"], where=dl["where"]))
        missing = [p["url"] for p in pages if not p.get("download")]
        for u in missing:
            rows.append(_row("download", "fail", "Download PDF button", "present", "not found", u))
        hrefs = {d["href"] for p in pages for d in (p.get("download") or []) if d["href"]}
        if len(hrefs) > 1:
            rows.append(_row("download", "warn", "Same file on every page", "one file", f"{len(hrefs)} different links",
                             note=" · ".join(sorted(hrefs))[:500]))
        if dl.get("error"):
            rows.append(_row("download", "fail", "Download", "a PDF file", dl["error"], dl["page"], dl.get("href", "")))
        elif dl.get("status", 0) >= 400:
            rows.append(_row("download", "fail", "Download", "HTTP 200", f"HTTP {dl['status']}", dl["page"], dl.get("href", "")))
        elif not dl.get("is_pdf"):
            rows.append(_row("download", "fail", "Download", "a PDF file",
                             f"{dl.get('content_type') or 'unknown type'}, {dl.get('bytes', 0)} bytes", dl["page"], dl.get("href", "")))
        else:
            rows.append(_row("download", "pass", "Download", "a PDF file", f"PDF, {dl['bytes'] // 1024} KB", dl["page"],
                             dl.get("final_url", "")))
            rows += _same_document(prod, Path(site["dir"]) / dl["file"], dl["page"])

    # ---- next / previous: the navigation's reading order
    order = []
    for it in (navs[0]["nav"]["items"] if navs else []):
        k = key(it["href"])
        if "#" not in (it["raw"] or "") and k not in order:
            order.append(k)
    titles = {key(p["url"]): _title(p) for p in pages}
    for p in pages:
        k = key(p["url"])
        i = order.index(k) if k in order else None
        for name, pg, step in (("Next topic", p.get("next"), 1), ("Previous topic", p.get("prev"), -1)):
            exp = order[i + step] if i is not None and 0 <= i + step < len(order) else None
            exp_title = (titles.get(exp) or _nav_text(navs, exp)) if exp else ""
            if i is None:
                if pg:
                    rows.append(_link_row("pager", f"{name}: {pg['label'] or pg['text']}", pg["href"], links, p["url"]))
                continue
            if not exp:
                rows.append(_row("pager", "fail" if pg and pg.get("visible") else "pass", name,
                                 "none (first page)" if step < 0 else "none (last page)",
                                 f"{pg['label'] or pg['text']} → {pg['href']}" if pg and pg.get("visible") else "none", p["url"],
                                 "" if not (pg and pg.get("visible")) else "The first/last page should not link further"))
                continue
            if not pg or not pg.get("href"):
                rows.append(_row("pager", "fail", name, exp_title, "missing", p["url"], "No link to the next page of the navigation"))
                continue
            got = key(pg["href"])
            if got != exp:
                rows.append(_row("pager", "fail", name, f"{exp_title} ({_short(exp)})", f"{pg['label'] or '—'} ({_short(got)})",
                                 p["url"], "Goes to another page than the navigation order", pg["where"]))
            elif pg["label"] and exp_title and not _same(pg["label"], exp_title, 0.85):
                rows.append(_row("pager", "warn", name, exp_title, pg["label"], p["url"],
                                 "Right page, but the label names another one", pg["where"]))
            else:
                st = links.get(got, {}).get("status", 200)
                rows.append(_row("pager", "pass" if 0 < st < 400 else "fail", name, exp_title, pg["label"] or pg["text"], p["url"],
                                 "" if 0 < st < 400 else f"HTTP {st}", pg["where"]))

    # ---- on this page: every sub-heading, each entry jumps to its heading
    for p in pages:
        heads = [h for h in p.get("headings") or [] if h["level"] >= 2]
        if not p.get("otp"):
            if len(heads) >= 1:
                rows.append(_row("otp", "fail", "On this page", f"{len(heads)} heading(s)", "not found", p["url"],
                                 "The page has sub-headings but no “On this page” list"))
            continue
        its = [it for it in p["otp"]["items"] if "#" in it["href"]]
        top = min((h["level"] for h in heads), default=2)
        want = [h for h in heads if h["level"] <= top + max(it["depth"] for it in its or [{"depth": 0}])]
        used = set()
        for h in want:
            k = next((i for i, it in enumerate(its) if i not in used and _same(h["text"], it["text"], 0.85)), None)
            if k is None:
                rows.append(_row("otp", "fail", h["text"], h["text"], "missing", p["url"], f"h{h['level']} not listed", p["otp"]["where"]))
                continue
            used.add(k)
            it = its[k]
            frag = unquote(it["href"].split("#", 1)[1])
            landed = (p["otp"].get("targets") or {}).get(frag)
            if landed is None:
                rows.append(_row("otp", "fail", it["text"], f"#{frag} on the page", "anchor not found", p["url"],
                                 "The entry does not jump anywhere", p["otp"]["where"]))
            elif landed and not _same(landed, h["text"], 0.85) and not _same(landed, it["text"], 0.85):
                rows.append(_row("otp", "fail", it["text"], h["text"], f"jumps to “{landed[:80]}”", p["url"],
                                 "Jumps to the wrong heading", p["otp"]["where"]))
            else:
                rows.append(_row("otp", "pass", it["text"], h["text"], it["text"], p["url"], where=p["otp"]["where"]))
        for i, it in enumerate(its):
            if i not in used:
                rows.append(_row("otp", "warn", it["text"], "", it["text"], p["url"], "Entry without a matching heading on the page",
                                 p["otp"]["where"]))

    # ---- product subtitle
    rows += _subtitle_rows(pages, prod, site.get("subtitle_source") or {}, baseline)
    prod.close()

    counts = {g: {"pass": 0, "warn": 0, "fail": 0, "info": 0} for g, _ in GROUPS}
    for r in rows:
        counts[r["group"]][r["status"]] += 1
    summary = {"pages": len(pages), "fail": sum(c["fail"] for c in counts.values()),
               "warn": sum(c["warn"] for c in counts.values()), "pass": sum(c["pass"] for c in counts.values()),
               "groups": counts, "status": "fail" if any(c["fail"] for c in counts.values()) else
               "warn" if any(c["warn"] for c in counts.values()) else "pass", "l1": l1}
    page_rows = [{"url": p["url"], "title": _title(p), "nav": (p.get("nav") or {}).get("where", ""),
                  "on_this_page": (p.get("otp") or {}).get("where", ""), "subtitle": (p.get("subtitle") or {}).get("text", ""),
                  "next": (p.get("next") or {}).get("href", ""), "prev": (p.get("prev") or {}).get("href", ""),
                  "footer": p.get("footer", ""), "breadcrumb": p.get("breadcrumb", "")} for p in pages]
    return {"summary": summary, "groups": [{"id": g, "title": t} for g, t in GROUPS], "rows": rows, "pages": page_rows,
            "download": {k: v for k, v in dl.items() if k != "error"} | ({"error": dl["error"]} if dl.get("error") else {}),
            "subtitle_source": site.get("subtitle_source") or {}}


def _title(p: dict) -> str:
    return p.get("h1") or re.split(r"\s+[|–-]\s+", p.get("title") or "")[0]


def _nav_text(navs: list, k: str | None) -> str:
    for p in navs[:1]:
        for it in p["nav"]["items"]:
            if key(it["href"]) == k:
                return it["text"]
    return ""


def _short(u: str) -> str:
    return urlsplit(u or "").path.rsplit("/", 1)[-1] or u


def _link_row(group: str, text: str, href: str, links: dict, page: str) -> dict:
    k = key(href)
    st = links.get(k)
    frag = href.split("#", 1)[1] if "#" in href else ""
    if urlsplit(href).scheme not in ("http", "https"):
        return _row(group, "warn", text, "a page link", href, page, "Not an http(s) link")
    if not st:
        return _row(group, "warn", text, "opens", "not checked", page, href)
    if st.get("error") or not 0 < st.get("status", 0) < 400:
        return _row(group, "fail", text, "HTTP 200", st.get("error") or f"HTTP {st['status']}", page, href)
    landed = st.get("h1") or re.split(r"\s+[|–-]\s+", st.get("title") or "")[0]
    if frag and frag not in (st.get("ids") or []):
        return _row(group, "fail", text, f"#{frag} on {_short(href)}", "anchor not found", page, href)
    if landed and not _same(landed, text, 0.8) and not _same(st.get("title", ""), text, 0.8) and not frag:
        return _row(group, "fail", text, text, f"opens “{landed}”", page, f"{href} → {st.get('url', '')}")
    return _row(group, "pass", text, text, landed or f"HTTP {st['status']}", page, href)


def _words(doc, limit: int = 0) -> set[str]:
    out = set()
    for k, pg in enumerate(doc):
        if limit and k >= limit:
            break
        out.update(w for w in re.findall(r"\w+", pg.get_text().lower()) if len(w) > 2)
    return out


def _same_document(prod, path: Path, page: str) -> list[dict]:
    import pymupdf
    try:
        d = pymupdf.open(path)
    except Exception as e:
        return [_row("download", "fail", "Opens as a PDF", "readable", f"{type(e).__name__}: {e}", page)]
    rows = []
    a, b = _words(prod), _words(d)
    sim = len(a & b) / max(len(a | b), 1)
    rows.append(_row("download", "pass" if sim >= 0.8 else "warn" if sim >= 0.5 else "fail", "Same document as prod",
                     f"prod: {len(prod)} pages, “{prod.metadata.get('title') or '—'}”",
                     f"download: {len(d)} pages, “{d.metadata.get('title') or '—'}”", page,
                     f"{sim:.0%} of the vocabulary shared" + ("" if sim >= 0.5 else " — a different manual?"), path.name))
    cov_a, cov_b = _cover(prod), _cover(d)
    if cov_a and cov_b:
        s = SequenceMatcher(None, normalize.title(cov_a), normalize.title(cov_b)).ratio()
        rows.append(_row("download", "pass" if s >= 0.8 else "warn", "Cover", cov_a, cov_b, page,
                         "" if s >= 0.8 else "The cover texts differ"))
    d.close()
    return rows


def _cover(doc) -> str:
    """The big text of the first page: the product and the kind of manual."""
    if not len(doc):
        return ""
    spans = [s for b in doc[0].get_text("dict")["blocks"] for l in b.get("lines", []) for s in l["spans"] if s["text"].strip()]
    if not spans:
        return ""
    big = [s["text"].strip() for s in spans if s["size"] >= 12] or [s["text"].strip() for s in spans]  # not slug / print marks
    return " · ".join(dict.fromkeys(big))[:300]


_VERSION = re.compile(r"\bv(?:ersion)?\s*\.?\s*(\d+(?:\.\d+)+)", re.I)


def _subtitle_rows(pages: list[dict], prod, source: dict, baseline: str) -> list[dict]:
    rows = []
    subs = [(p["url"], p["subtitle"]) for p in pages if p.get("subtitle")]
    if not subs:
        return [_row("subtitle", "fail", "Product subtitle", "a product title in the header", "not found",
                     pages[0]["url"] if pages else "", "Set [site] subtitle = \"<css selector>\" if the site has one")]
    url, s = subs[0]
    shown = s["text"] + (" | " + " | ".join(s["also"]) if s.get("also") else "")
    rows.append(_row("subtitle", "info", "Read from", "", shown, url, f"{s['how']}", s["where"]))
    if source.get("property"):
        rows.append(_row("subtitle", "pass" if source.get("exact") else "warn", "AEM source", "",
                         f"{source['property']} = “{source['value']}”", url, f"page property of {source['page']}"))
    elif source.get("not_found_in"):
        rows.append(_row("subtitle", "info", "AEM source", "", "not found", url,
                         "No jcr:content property of the page or its parents has this value (or the JSON is not readable)"))
    values: dict[str, list] = {}
    for u, x in subs:
        values.setdefault(x["text"], []).append((u, x))
    common = max(values, key=lambda v: len(values[v]))
    if len(values) > 1:
        for v, us in values.items():
            if v != common:
                for u, x in us:
                    rows.append(_row("subtitle", "fail", "Same on every page", common, v, u,
                                     f"{len(values[common])} page(s) show “{common}”", x["where"]))
    else:
        rows.append(_row("subtitle", "pass", "Same on every page", common, common, url, f"{len(subs)} page(s)"))
    for p in pages:
        if not p.get("subtitle"):
            rows.append(_row("subtitle", "fail", "Product subtitle", common, "not found", p["url"]))
    # does it name the product of the prod PDF?
    cover = _cover(prod)
    ref = " ".join([cover, prod.metadata.get("title") or "", prod.metadata.get("subject") or "", Path(baseline).stem])
    ref_tokens = {t.lower() for t in _tokens(ref)}
    for v, us in values.items():
        tokens = _tokens(v)
        models = [t for t in tokens if re.search(r"\d", t) or t.isupper() and len(t) > 1]
        key_tokens = models or [t for t in tokens if len(t) > 2]
        hit = [t for t in key_tokens if t.lower() in ref_tokens]
        ok = bool(key_tokens) and len(hit) >= max(1, len(key_tokens) // 2)
        rows.append(_row("subtitle", "pass" if ok else "fail", "Matches the prod PDF", cover or Path(baseline).stem, v, us[0][0],
                         (f"names {', '.join(hit)}" if ok else
                          f"None of {', '.join(key_tokens) or 'its words'} is on the PDF cover, title or file name — wrong product?")
                         + (f" · on {len(us)} page(s)" if len(values) > 1 else ""), us[0][1]["where"]))
    for extra in s.get("also") or []:
        e_tokens = [t for t in _tokens(extra) if len(t) > 2]
        e_hit = [t for t in e_tokens if t.lower() in ref_tokens]
        rows.append(_row("subtitle", "pass" if e_hit else "warn", "Category", cover, extra, url,
                         "" if e_hit else "Not on the PDF cover"))
    # version in the footer / title vs the PDF
    fv = next((m.group(1) for p in pages for m in [_VERSION.search((p.get("footer") or "") + " " + (p.get("subtitle") or {}).get("text", ""))] if m), "")
    pv = next((m.group(1) for m in [_VERSION.search(" ".join([Path(baseline).stem.replace("_", " "), cover,
                                                           " ".join(prod[i].get_text() for i in range(min(3, len(prod))))]))] if m), "")
    if fv or pv:
        rows.append(_row("subtitle", "pass" if fv and fv == pv else "warn", "Version", pv or "—", fv or "—", url,
                         "" if fv == pv else "The site's version (footer) and the PDF's version differ"))
    return rows


def _tokens(s: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9]+", s or "")


def write_csv(site: dict, path: Path) -> Path:
    titles = dict((g, t) for g, t in GROUPS)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["Check", "Status", "Item", "Expected", "Actual", "Page", "Note", "Element"])
        for r in site["rows"]:
            w.writerow([titles.get(r["group"], r["group"]), r["status"], r["item"], r["expected"], r["actual"],
                        r["page"], r["note"], r["where"]])
    return path
