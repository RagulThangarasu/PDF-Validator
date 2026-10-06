"""Web page (URL) -> Doc, so a PDF can be validated against its HTML version.

The page is rendered in headless Chromium and read from the DOM (not OCR):
every word with its rendered box and computed style, the heading outline
(h1-h6), real tables (table/tr/td), images and links. The full-page screenshot
is cut into "pages" (slices) and saved as an image-only PDF with the links
added as annotations, so the rest of the pipeline (screenshots, viewer, sync
scrolling, report) treats the web page like any other document. 1 CSS px = 1 pt.
"""
from __future__ import annotations

import io
import math
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

import pymupdf
from PIL import Image as PILImage

from . import normalize, site_nav
from .extract import _measure
from .model import Doc, Image, Line, PageInfo, Style, Word

DEFAULT_EXCLUDE = "nav, header, footer, aside, script, style, noscript, template, [aria-hidden='true'], [role='navigation']"

# Runs in the page. Returns words (DOM order) with boxes and styles, headings, tables, images, links.
_EXTRACT_JS = r"""
([rootSel, exclude]) => {
  const root = (rootSel && document.querySelector(rootSel)) || document.querySelector('main') ||
               document.querySelector('article') || document.body;
  const sx = window.scrollX, sy = window.scrollY;
  // site chrome is excluded - except a <header> inside the content that holds the page title
  // (AEM topic pages: <main> ... <header class="topic-renderer__header"><h1>Port overview</h1>)
  const titleHeader = x => x.tagName === 'HEADER' && x !== root && root.contains(x) && !!x.querySelector('h1');
  const excluded = el => {
    let x = el && exclude ? el.closest(exclude) : null;
    while (x && titleHeader(x)) x = x.parentElement ? x.parentElement.closest(exclude) : null;
    return !!x;
  };
  const visible = el => el && (el.checkVisibility ? el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true }) : el.offsetParent !== null);
  const BLOCK = new Set(['block', 'list-item', 'table-cell', 'table-caption', 'flex', 'grid', 'flow-root']);
  const blockIds = new Map();
  const blockOf = el => {
    let b = el;
    while (b && b !== root && !(/^H[1-6]$/.test(b.tagName) || BLOCK.has(getComputedStyle(b).display))) b = b.parentElement;
    b = b || root;
    if (!blockIds.has(b)) blockIds.set(b, blockIds.size);
    return blockIds.get(b);
  };
  const hex = c => { const m = c.match(/\d+(\.\d+)?/g) || [0, 0, 0]; return '#' + m.slice(0, 3).map(v => (+v | 0).toString(16).padStart(2, '0')).join(''); };
  const styleCache = new Map();
  const styleOf = el => {
    if (styleCache.has(el)) return styleCache.get(el);
    const cs = getComputedStyle(el);
    const s = { family: cs.fontFamily.split(',')[0].replace(/["']/g, '').trim(), weight: +cs.fontWeight || 400,
                italic: cs.fontStyle !== 'normal', size: parseFloat(cs.fontSize), color: hex(cs.color),
                pre: /pre/.test(cs.whiteSpace) };
    styleCache.set(el, s); return s;
  };
  const words = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const range = document.createRange();
  let node;
  while ((node = walker.nextNode())) {
    const el = node.parentElement;
    if (!el || excluded(el) || !visible(el) || !node.data.trim()) continue;
    const st = styleOf(el), block = blockOf(el);
    const h = el.closest('h1,h2,h3,h4,h5,h6');
    const a = el.closest('a[href]');
    // a character of a script written without spaces (Chinese, Japanese, Thai, ...) is its own word
    const re = /[__NOSPACE__]\p{Mn}*|[^\s__NOSPACE__]+/gu; let m;
    while ((m = re.exec(node.data))) {
      range.setStart(node, m.index); range.setEnd(node, m.index + m[0].length);
      const rs = range.getClientRects(); if (!rs.length) continue;
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (const r of rs) { x0 = Math.min(x0, r.left); y0 = Math.min(y0, r.top); x1 = Math.max(x1, r.right); y1 = Math.max(y1, r.bottom); }
      if (x1 - x0 < 0.5) continue;
      const after = node.data.slice(m.index + m[0].length).match(/^\s*/)[0].length;
      words.push([m[0], x0 + sx, y0 + sy, x1 + sx, y1 + sy, st.family, st.weight, st.italic, st.size, st.color, block,
                  h ? +h.tagName[1] : 0, a ? a.href : '', st.pre ? after : Math.min(after, 1)]);
    }
  }
  const rect = el => { const r = el.getBoundingClientRect(); return [r.left + sx, r.top + sy, r.right + sx, r.bottom + sy]; };
  const headings = [...root.querySelectorAll('h1,h2,h3,h4,h5,h6')].filter(h => visible(h) && !excluded(h) && h.innerText.trim())
    .map(h => [+h.tagName[1], h.innerText.replace(/\s+/g, ' ').trim(), ...rect(h)]);
  const images = [...root.querySelectorAll('img, svg, [role="img"]')].filter(i => visible(i) && !excluded(i) && !i.closest('svg svg'))
    .map(i => [...rect(i), i.tagName === 'IMG' && i.complete && !i.naturalWidth ? 1 : 0])  // 1 = failed to load
    .filter(r => r[2] - r[0] >= 4 && r[3] - r[1] >= 4);
  const tables = [...root.querySelectorAll('table')].filter(t => visible(t) && !excluded(t)).map(t => {
    const rows = [...t.rows].filter(visible);
    const gridRow = rows.reduce((b, r) => (r.cells.length > (b ? b.cells.length : 0) ? r : b), null);
    return { box: rect(t), rows: rows.map(r => [rect(r), r.cells.length ? rect(r.cells[0]) : null]),
             grid: gridRow ? [...gridRow.cells].map(c => { const q = rect(c); return [q[0], q[2]]; }) : [] };
  });
  const links = [...root.querySelectorAll('a[href]')].filter(a => visible(a) && !excluded(a) && a.innerText.trim())
    .map(a => [a.href, ...rect(a)]);
  // blocks a slice must not cut through: boxed blocks (callouts such as NOTE / WARNING, cards: a
  // background or a border), figures, list items, paragraphs, code; a heading stays with what follows
  const clear = c => !c || c === 'transparent' || /rgba\(.*,\s*0\)$/.test(c);
  const keep = [];
  for (const e of root.querySelectorAll('*')) {
    if (excluded(e) || !/^(block|list-item|flex|grid|flow-root|table)$/.test(getComputedStyle(e).display)) continue;
    const cs = getComputedStyle(e);
    const boxed = !clear(cs.backgroundColor) || ['Top', 'Left'].some(k => parseFloat(cs['border' + k + 'Width']) > 0 && cs['border' + k + 'Style'] !== 'none');
    if (!(boxed || /^(FIGURE|BLOCKQUOTE|PRE|LI|P|DL|DT|DD|H[1-6])$/.test(e.tagName) || e.getAttribute('role') === 'note') || !visible(e)) continue;
    const r = rect(e);
    if (r[3] - r[1] > 4) keep.push([r[1], /^H[1-6]$/.test(e.tagName) ? r[3] + 40 : r[3]]);
  }
  const de = document.documentElement;
  return { title: document.title, width: Math.max(de.clientWidth, 320), height: Math.max(de.scrollHeight, document.body.scrollHeight),
           words, headings, images, tables, links, keep };
}
"""
_EXTRACT_JS = _EXTRACT_JS.replace("__NOSPACE__", normalize.NOSPACE)



def split_login(url: str) -> tuple[str, str, str]:
    """http://user:pass@host/x -> ("http://host/x", "user", "pass"); no login -> (url, "", "")."""
    parts = urlsplit(url)
    if not parts.username:
        return url, "", ""
    clean = urlunsplit(parts._replace(netloc=parts.hostname + (f":{parts.port}" if parts.port else "")))
    return clean, unquote(parts.username), unquote(parts.password or "")


def _login_form(page) -> bool:
    return page.locator("input[type=password]:visible").count() > 0


def _sign_in(page, user: str, password: str, timeout: int, settle_ms: int) -> None:
    """Fill the sign-in form the site shows: the visible password field and the user
    field before it in the same form (text / email / no type), then submit it.
    Sites submit either as a normal form post or in the background (AEM), so wait for
    the network to settle and give script redirects `settle_ms` (the run's "wait after
    load") instead of expecting a navigation."""
    pw = page.locator("input[type=password]:visible").first
    user_field = pw.evaluate_handle("""p => {
        const scope = p.form || document;
        const fields = [...scope.querySelectorAll('input')].filter(i =>
            ['', 'text', 'email'].includes((i.getAttribute('type') || '').toLowerCase()) && i.offsetParent !== null);
        const before = fields.filter(i => i.compareDocumentPosition(p) & Node.DOCUMENT_POSITION_FOLLOWING);
        return before[before.length - 1] || fields[0] || null;
    }""").as_element()
    if user_field is None:
        raise RuntimeError("Found a password field but no user name field on the sign-in page")
    user_field.fill(user)
    pw.fill(password)
    start = page.url
    pw.press("Enter")
    # done when the sign-in form is gone or the site navigated away; polled, not "network idle":
    # AEM author pages keep background connections open, so the network is never idle
    waited = 0
    while waited < _SIGN_IN_MS:
        page.wait_for_timeout(500)
        waited += 500
        try:
            if page.url != start or not _login_form(page):
                break
        except Exception:  # the page is navigating
            continue
    _settle(page, settle_ms)


_SIGN_IN_MS = 30_000  # a sign-in that has not left the login form by then has failed
_IDLE_MS = 8_000  # at most this long for background requests to calm down after a page has loaded


# Resolves when the page is ready to read: every image has loaded (or failed), the fonts are in and the DOM
# has not changed for `quiet` ms - or after `cap` ms, whichever comes first. Returns the ms it took.
_READY_JS = r"""
([quiet, cap]) => new Promise(resolve => {
  const t0 = performance.now();
  let last = t0;
  const mo = new MutationObserver(() => { last = performance.now(); });
  mo.observe(document.documentElement, {subtree: true, childList: true, attributes: true, characterData: true});
  const imgs = () => [...document.images].every(i => i.complete);
  const tick = () => {
    const now = performance.now();
    const fonts = !document.fonts || document.fonts.status === 'loaded';
    if ((imgs() && fonts && now - last >= quiet) || now - t0 >= cap) { mo.disconnect(); resolve(Math.round(now - t0)); }
    else setTimeout(tick, 50);
  };
  tick();
})
"""


def _ready(page, quiet_ms: int = 300, cap_ms: int = _IDLE_MS) -> None:
    """Wait until the page is ready to read (_READY_JS), at most cap_ms: a page that is done in half a second is
    read after half a second - not after a fixed pause, nor after a "network idle" that a polling page never
    reaches."""
    try:
        page.evaluate(_READY_JS, [quiet_ms, cap_ms])
    except Exception:  # the page navigated while waiting
        pass


def _settle(page, extra_ms: int = 0) -> None:
    """The page's HTML is in: wait for it to be ready to read - its images and fonts loaded, its DOM quiet -
    for a bounded time. Not for the "load" event or a fully idle network: neither ever comes on pages that
    poll (AEM author, analytics, chat), and each cost its whole timeout on every page."""
    _ready(page)
    if extra_ms:
        page.wait_for_timeout(min(extra_ms, 5_000))


def _goto(page, url: str, timeout: int):
    """Open a page: wait for its HTML, then a bounded time for the rest (_settle). Waiting for the "load"
    event can last forever on an AEM author page (a request that never finishes)."""
    resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout)
    _settle(page)
    return resp


def _launch_hint(e: Exception) -> str:
    """Why the browser for the web capture did not start, in one line, with what to do about it."""
    msg = str(e)
    if "bootstrap_check_in" in msg or "MachPortRendezvous" in msg:
        # macOS: the process cannot register with its login session any more - the UI server outlived the
        # shell / session that started it (or runs sandboxed), and every browser it starts inherits that
        return ("The browser for the web capture could not start: this UI server has lost its macOS login session "
                "(it outlived the terminal that started it, or runs in a sandbox). Restart the UI server from a "
                "Terminal window (./start.sh) and run again.")
    if "Executable doesn't exist" in msg or "playwright install" in msg:
        return ("The browser for the web capture is not installed: run `.venv/bin/python -m playwright install "
                "chromium` in the pdf_validator folder, then run again.")
    first = next((ln.strip() for ln in msg.splitlines() if ln.strip()), type(e).__name__)
    return f"The browser for the web capture could not start: {first[:300]}"


def capture(url: str, out_dir: str | Path, *, root: str = "", exclude: str = DEFAULT_EXCLUDE,
            width: int = 1280, wait_ms: int = 1500, slice_h: int = 1800, progress=None,
            user: str = "", password: str = "", crawl: bool = False, max_pages: int = 0,
            site: dict | None = None, toc_l1: list[str] | None = None) -> tuple[Doc, dict]:
    """Render `url`, extract its structure and write <out_dir>/candidate_source.pdf. Returns (Doc, info).
    user/password (or a login in the URL): sent as HTTP basic auth and, when the site
    shows a sign-in form instead of the page, typed into that form.
    crawl: also every page of the same guide the pages link to (same site, under the
    start page's folder), in reading order, joined into one document: each page's title
    becomes a level-1 heading with the page's own headings below it (max_pages: 0 = no limit).
    toc_l1: the PDF's level-1 TOC titles. With crawl, they drive it: each is looked up in the
    site's left navigation (found by site_nav, no site-specific selectors) and its page opened
    in TOC order, followed by every page nested under it in the navigation (all levels). An L1
    entry the navigation lacks is reported in info["skipped"]. Without a navigation (or no L1
    entry in it), the crawl follows the guide's links under the start page's folder.
    site: the `[site]` config: also read each page's navigation chrome (site_nav) and open
    what it points to; info["site"] holds it. None: skip."""
    from playwright.sync_api import sync_playwright

    report = progress or (lambda f, m: None)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    url, url_user, url_password = split_login(url)
    user, password = user or url_user, password or url_password
    # "always": servers like AEM redirect to a login page instead of answering 401
    creds = {"username": user, "password": password, "send": "always"} if user else None
    timeout = 90_000
    import time as _time
    t_start = _time.monotonic()
    timing = {"open_s": 0.0, "load_s": 0.0, "read_s": 0.0, "site_s": 0.0, "build_s": 0.0}
    captured, skipped = [], []
    chromes, visited, site_raw = [], {}, None
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:
            raise RuntimeError(_launch_hint(e)) from None
        try:
            page = browser.new_page(viewport={"width": width, "height": 1000}, device_scale_factor=1, http_credentials=creds)
            report(0.02, f"Opening {url}")
            t0 = _time.monotonic()
            _open(page, url, user, password, timeout, wait_ms, report)
            timing["open_s"] = _time.monotonic() - t0
            queue, seen = [url], {_page_key(url)}
            titles: dict[str, str] = {}  # page key -> its left-navigation entry
            nav_led, ordered = False, not crawl
            not_in_nav: list[str] = []
            nav_toc: list[dict] = []  # the left navigation of the first captured page: the stage TOC
            k = 0
            while k < len(queue) and (not max_pages or len(captured) < max_pages):
                u = queue[k]
                frac = 0.05 + 0.8 * k / max(len(queue), 1)
                if _page_key(page.url) != _page_key(u):
                    report(frac, f"Page {k + 1}/{len(queue)}: {titles.get(_page_key(u)) or u}")
                    t0 = _time.monotonic()
                    resp = _goto(page, u, timeout)
                    timing["load_s"] += _time.monotonic() - t0
                    if resp is not None and resp.status >= 500:  # gateway hiccups are often transient: once more
                        page.wait_for_timeout(2_000)
                        resp = _goto(page, u, timeout)
                    if (resp is not None and resp.status >= 400) or _login_form(page):
                        bad = f"HTTP {resp.status}" if resp is not None and resp.status >= 400 else "login form"
                        skipped.append({"url": u, "title": titles.get(_page_key(u), ""), "reason": bad})
                        visited[site_nav.key(u)] = {"status": resp.status if resp is not None else 0, "url": page.url,
                                                    "error": "" if resp is not None and resp.status >= 400 else bad, "crawled": True}
                        k += 1
                        continue
                if crawl and not ordered:  # first page: decide which pages make up the guide, in which order
                    ordered = True
                    plan, not_in_nav = _nav_plan(page, site or {}, root, toc_l1 or [])
                    if plan:  # the PDF's L1 TOC, looked up in the left navigation
                        nav_led = True
                        queue = [l for l, _ in plan]
                    else:  # no navigation to go by: the guide's links, in their order
                        not_in_nav = []
                        links = _guide_links(page, url)
                        if _page_key(url) in {_page_key(l) for l in links}:
                            queue = links
                    seen = {_page_key(l) for l in queue}
                    titles = {_page_key(l): t for l, t in plan}
                    skipped += [{"url": "", "title": t, "reason": "not in the left navigation"} for t in not_in_nav]
                    if _page_key(queue[0]) != _page_key(page.url):
                        continue  # start with the first page of the guide
                if crawl:
                    found = _nav_children(page, site or {}, root) if nav_led else [(l, "") for l in _guide_links(page, url)]
                    new = [(l, t) for l, t in found if _page_key(l) not in seen]
                    seen.update(_page_key(l) for l, _ in new)
                    titles.update({_page_key(l): t for l, t in new if t})
                    queue[k + 1:k + 1] = [l for l, _ in new]  # a page's sub-pages follow it (reading order)
                t0 = _time.monotonic()
                data, slices = _read_page(page, root, exclude, wait_ms, slice_h, report, frac)
                timing["read_s"] += _time.monotonic() - t0
                if site is not None:
                    ch = site_nav.read_chrome(page, site, root)
                    chromes.append(ch)
                    visited[site_nav.key(u)] = visited[site_nav.key(page.url)] = {
                        "status": 200, "url": page.url, "title": ch.get("title", ""), "h1": ch.get("h1", ""),
                        "ids": ch.get("ids") or [], "crawled": True}
                if data["words"]:
                    captured.append((page.url, data, slices))
                    if crawl and len(captured) == 1:  # it sits at the top of the joined document
                        nav_toc = [{"title": it["text"], "level": it["depth"] + 1, "url": it["href"], "box": it.get("box")}
                                   for it in _nav_items(page, site or {}, root) if it["visible"]]
                else:
                    skipped.append({"url": u, "title": titles.get(_page_key(u), ""), "reason": "no text"})
                k += 1
            if site is not None and chromes:
                report(0.86, "Checking the site navigation")
                t0 = _time.monotonic()
                site_raw = {"pages": chromes, **site_nav.probe(page, chromes, visited, out, lambda m: report(0.87, m)),
                            "dir": str(out)}
                timing["site_s"] = _time.monotonic() - t0
        finally:
            browser.close()
    if not captured:
        raise RuntimeError("No text found on the web page" + (f" inside “{root}”" if root else "")
                           + " - check the URL, the content root selector and that the page loads without a login")

    report(0.9, "Building the document")
    t_build = _time.monotonic()
    data, cuts, slices = _stack(captured, titled=crawl)
    W = int(data["width"])
    pdf = pymupdf.open()
    for (y0, y1), png in zip(zip(cuts, cuts[1:]), slices):
        pg = pdf.new_page(width=W, height=y1 - y0)
        pg.insert_image(pg.rect, stream=png)
    for href, x0, y0, x1, y1 in data["links"]:
        k = _slice_of(cuts, y0)
        if k is not None:
            pdf[k].insert_link({"kind": pymupdf.LINK_URI, "uri": href,
                                "from": pymupdf.Rect(x0, y0 - cuts[k], x1, y1 - cuts[k])})
    path = out / "candidate_source.pdf"
    pdf.save(path, garbage=3, deflate=True)
    doc = _to_doc(data, cuts, str(path))
    for e in nav_toc:  # where each entry shows on the stage PDF (the first page's slices)
        k = _slice_of(cuts, e["box"][1]) if e.get("box") else None
        e["toc_page"] = k if k is not None else -1
        e["bbox"] = [e["box"][0], e["box"][1] - cuts[k], e["box"][2], e["box"][3] - cuts[k]] if k is not None else [0, 0, 0, 0]
    first = captured[0][1]
    info = {"url": url, "title": first["title"], "width": W, "height": int(cuts[-1]), "pages": len(slices),
            "words": len(doc.words), "headings": len(data["headings"]), "tables": len(data["tables"]),
            "images": len(data["images"]), "links": len(data["links"]), "root": root or "auto (main / article / body)",
            "crawl": crawl, "web_pages": [{"url": u, "title": d["title"], "words": len(d["words"])} for u, d, _ in captured],
            "nav_toc": nav_toc,
            "crawl_by": "left navigation (PDF L1 TOC)" if nav_led else "guide links" if crawl else "",
            "skipped": skipped}
    # where the capture's time went (seconds): signing in / first page, opening the other pages, reading them
    # (scroll, structure, screenshots), the site-navigation link checks, building the document
    timing["build_s"] = _time.monotonic() - t_build
    info["timing"] = {**{k: round(v, 1) for k, v in timing.items()}, "total_s": round(_time.monotonic() - t_start, 1),
                      "web_pages": len(captured)}
    if site_raw:
        for ch in site_raw["pages"]:
            ch.pop("ids", None)  # only needed for the link checks, large
        info["site"] = site_raw
    return doc, info


def _open(page, url: str, user: str, password: str, timeout: int, wait_ms: int, report) -> None:
    """Load the first page, signing in if the site asks for it; raise a clear error if it cannot be read."""
    resp = _goto(page, url, timeout)
    if _login_form(page) and user:
        report(0.04, f"Signing in as {user} (up to {_SIGN_IN_MS // 1000} s)")
        _sign_in(page, user, password, timeout, wait_ms)
        if _login_form(page):
            raise RuntimeError(f"Sign-in failed for user “{user}”: the site still shows its login form "
                               "- check the user name and password")
        report(0.05, "Signed in - opening the page")
        resp = _goto(page, url, timeout)  # back to the page itself
    if resp is not None and resp.status >= 400:
        hint = (" - the page needs a login: enter the user name and password under “Login” in the run form"
                if resp.status in (401, 403) else "")
        raise RuntimeError(f"Web page returned HTTP {resp.status} {resp.status_text}{hint}")
    if _login_form(page):  # redirected to a sign-in form
        raise RuntimeError(f"The web page needs a login (it shows a sign-in form: {page.url}). "
                           "Enter the user name and password under “Login” in the run form")


def _nav_items(page, cfg: dict, root: str) -> list[dict]:
    ch = site_nav.read_chrome(page, cfg, root) or {}
    return [it for it in (ch.get("nav") or {}).get("items") or [] if it.get("href", "").startswith("http")]


def _nav_plan(page, cfg: dict, root: str, toc_l1: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """Each L1 TOC title's left-navigation page, in TOC order: ([(url, nav text)], [titles not in the nav]).
    ([], []) when the page has no navigation or none of the titles is in it."""
    items = _nav_items(page, cfg, root)
    plan, missing, used = [], [], set()
    for t in toc_l1:
        # a top-level entry first; the same title deeper down only when the top level lacks it
        it = next((it for d in (True, False) for it in items
                   if (it["depth"] == 0) == d and site_nav._same(t, it["text"]) and _page_key(it["href"]) not in used), None)
        if it is None:
            missing.append(t)
            continue
        used.add(_page_key(it["href"]))
        plan.append((it["href"].split("#")[0], it["text"]))
    return (plan, missing) if plan else ([], [])


def _nav_children(page, cfg: dict, root: str) -> list[tuple[str, str]]:
    """The pages nested under the open page in the left navigation, all levels, in order. Read on
    every page: a navigation often expands only the branch of the page that is open."""
    items = _nav_items(page, cfg, root)
    here = _page_key(page.url)
    at = [k for k, it in enumerate(items) if _page_key(it["href"]) == here]
    if not at:
        return []
    k = next((k for k in at if items[k]["active"]), at[0])
    out, seen = [], {here}
    for it in items[k + 1:]:
        if it["depth"] <= items[k]["depth"]:
            break
        if _page_key(it["href"]) not in seen:  # #fragment links into a page already listed are that page
            seen.add(_page_key(it["href"]))
            out.append((it["href"].split("#")[0], it["text"]))
    return out


# Scrolls the window and every scrollable panel to its end, following content that loads while
# scrolling (lazy images, infinite sections), and opens collapsed <details>; then back to the top.
_SCROLL_JS = r"""
async (step) => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  document.querySelectorAll('details:not([open])').forEach(d => { d.open = true; });
  const panels = [...document.querySelectorAll('body *')].filter(e =>
    /(auto|scroll)/.test(getComputedStyle(e).overflowY) && e.scrollHeight > e.clientHeight + 20);
  for (const el of [document.scrollingElement || document.documentElement, ...panels]) {
    for (let y = 0, n = 0; y < el.scrollHeight + step && n < 500; y += step, n++) {  // scrollHeight grows as content loads
      el.scrollTop = y;
      await sleep(60);
    }
    el.scrollTop = 0;
  }
}
"""


def _read_page(page, root: str, exclude: str, wait_ms: int, slice_h: int, report, frac: float):
    """Structure + slice screenshots of the loaded page."""
    report(frac, f"Scrolling through “{page.title()}”")
    page.evaluate(_SCROLL_JS, 800)
    _ready(page, 250, max(wait_ms, 500))  # what scrolling loaded (lazy images): until it is in, at most wait_ms
    report(frac, f"Reading “{page.title()}”")
    data = page.evaluate(_EXTRACT_JS, [root, exclude])
    W, H = int(data["width"]), int(math.ceil(data["height"]))
    cuts = _cuts(data, H, slice_h)
    slices = [page.screenshot(clip={"x": 0, "y": y0, "width": W, "height": y1 - y0}, full_page=True)
              for y0, y1 in zip(cuts, cuts[1:])]
    data["cuts"] = cuts
    return data, slices


_NON_PAGE = re.compile(r"\.(pdf|zip|png|jpe?g|gif|svg|webp|mp4|docx?|xlsx?|pptx?|txt|xml|json|js|css)$", re.I)


def _page_key(u: str) -> str:
    """Identity of a page: scheme://host/path (query and #fragment do not make another page)."""
    s = urlsplit(u)
    return f"{s.scheme}://{s.netloc}{s.path}"


def _guide_links(page, start: str) -> list[str]:
    """Links on the page to other pages of the same guide: same site, under the start
    page's folder, not a file download, in document order (the navigation comes first).
    They get the start page's query (e.g. ?wcmmode=disabled) when they have none."""
    s = urlsplit(start)
    folder = s.path.rsplit("/", 1)[0] + "/"
    out, seen = [], set()
    for href in page.evaluate("[...document.querySelectorAll('a[href]')].map(a => a.href)"):
        h = urlsplit(href)
        path = h.path
        # AEM navigation often links the repository path (/content/guide/<guide folder>/page.html),
        # which the site redirects to the public one (/<guide folder>/page.html): the same page
        if not path.startswith(folder) and folder in path:
            path = path[path.index(folder):]
        if h.scheme not in ("http", "https") or h.netloc != s.netloc or not path.startswith(folder) or _NON_PAGE.search(path):
            continue
        u = urlunsplit((h.scheme, h.netloc, path, h.query or s.query, ""))
        key = _page_key(u)
        if key in seen:
            continue
        seen.add(key)
        out.append(u)
    return out


def _stack(captured: list, titled: bool) -> tuple[dict, list[int], list[bytes]]:
    """Join the captured pages top to bottom into one document (structure, slice
    boundaries, slice images). titled: each web page's title becomes a level-1 heading
    at its top unless the page already starts with that heading; the page's own
    headings are ranked below it (per page, so h2/h3 on one page and h1/h2 on another
    both become levels 2/3)."""
    out = {"title": captured[0][1]["title"], "width": max(d["width"] for _, d, _ in captured),
           "words": [], "headings": [], "images": [], "tables": [], "links": []}
    cuts, slices, off = [0], [], 0
    shift = lambda r: [r[0], r[1] + off, r[2], r[3] + off]
    for n, (_, d, sl) in enumerate(captured):
        for w in d["words"]:
            w = list(w)
            w[2] += off; w[4] += off
            w[10] = n * 1_000_000 + w[10]  # block ids stay unique per page
            out["words"].append(w)
        used = sorted({h[0] for h in d["headings"]})
        title = (d["title"] or "").strip()
        add_title = titled and title and not (d["headings"] and normalize.title(d["headings"][0][1]) == normalize.title(title))
        heads = [[used.index(h[0]) + 1 + (1 if add_title else 0), h[1], *shift(h[2:])] for h in d["headings"]]
        if add_title:  # the page title is the level-1 heading, the page's headings sit below it
            top = min((w[2] for w in d["words"]), default=0) + off
            out["headings"].append([1, title, 0, top, 0, top])
        out["headings"] += heads
        out["images"] += [shift(r) + list(r[4:]) for r in d["images"]]
        out["tables"] += [{"box": shift(t["box"]), "rows": [[shift(r), shift(c) if c else None] for r, c in t["rows"]],
                           "grid": t["grid"]} for t in d["tables"]]
        out["links"] += [[l[0], *shift(l[1:])] for l in d["links"]]
        cuts += [c + off for c in d["cuts"][1:]]
        slices += sl
        off = cuts[-1]
    return out, cuts, slices


def _cuts(data: dict, H: int, slice_h: int) -> list[int]:
    """Slice boundaries every ~slice_h px, moved up so they don't cut through a table, an
    image, a boxed block (NOTE / WARNING callout, list item, paragraph, figure), a line of
    text, or between a heading and what follows (a cut there splits one element over two
    pages). Blocks taller than a slice may be cut, but never through a line of text."""
    spans = [(t["box"][1], t["box"][3]) for t in data["tables"]]
    spans += [(r[1], r[3]) for r in data["images"]]
    spans += [(k[0], k[1]) for k in data.get("keep", [])]
    spans = [(a, b) for a, b in spans if b - a < slice_h * 0.9]
    spans += [(w[2], w[4]) for w in data["words"]]
    cuts, y = [0], 0
    while y + slice_h < H:
        c = y + slice_h
        moved = True
        while moved:  # moving the cut up can land it inside another block: repeat until it is clear
            moved = False
            for a, b in spans:
                if a < c < b and a > y + slice_h * 0.3 and int(a) - 2 < c:
                    c, moved = int(a) - 2, True
        cuts.append(int(c))
        y = int(c)
    cuts.append(H)
    return cuts


def _slice_of(cuts: list[int], y: float) -> int | None:
    for k, (a, b) in enumerate(zip(cuts, cuts[1:])):
        if a <= y < b:
            return k
    return None


def _to_doc(data: dict, cuts: list[int], path: str) -> Doc:
    W = int(data["width"])
    pages = [PageInfo(W, b - a) for a, b in zip(cuts, cuts[1:])]
    words: list[Word] = []
    lines: list[Line] = []
    prev = None
    for k, (t, x0, y0, x1, y1, fam, wt, it, px, col, block, hlevel, href, after) in enumerate(data["words"]):
        p = _slice_of(cuts, y0)
        if p is None:
            continue
        top = y0 - cuts[p]
        size = round(px * 0.75, 1)  # CSS px -> pt, comparable with the PDF's font sizes
        new_line = (prev is None or prev[0] != p or prev[1] != block
                    or abs(top - prev[2]) > 0.5 * max(y1 - y0, 1))
        if new_line:
            if words:
                words[-1].space_after = None  # line end
            lines.append(Line(p, (x0, top, x1, y1 - cuts[p]), t, size, len(words), (p, block)))
        else:
            ln = lines[-1]
            ln.bbox = (min(ln.bbox[0], x0), min(ln.bbox[1], top), max(ln.bbox[2], x1), max(ln.bbox[3], y1 - cuts[p]))
            ln.text += " " + t
            ln.size = max(ln.size, size)
        words.append(Word(t, normalize.token(t, case_sensitive=True, ignore={"•", "●", "■", "▪", "◦"}),
                          p, (x0, top, x1, y1 - cuts[p]), Style(fam, int(wt), bool(it), size, col),
                          len(lines) - 1, line_start=new_line, space_after=int(after)))
        prev = (p, block, top)
    if words:
        words[-1].space_after = None
    images = []
    for x0, y0, x1, y1, *flag in data["images"]:
        p = _slice_of(cuts, y0)
        if p is not None:
            images.append(Image(p, (x0, y0 - cuts[p], x1, min(y1, cuts[p + 1]) - cuts[p]), broken=bool(flag and flag[0])))
    # outline from the heading hierarchy: levels ranked (h2/h3/h4 used -> 1/2/3)
    used = sorted({h[0] for h in data["headings"]})
    outline = []
    for lvl, text, x0, y0, x1, y1 in data["headings"]:
        p = _slice_of(cuts, y0)
        if p is not None:
            outline.append((used.index(lvl) + 1, text, p + 1))
    doc = Doc(path, "candidate", pages, words, lines, images, outline)
    # real table structure from the DOM, used by the tables check instead of detection
    raw: dict[int, list] = {}
    for t, tb in enumerate(data["tables"]):
        p = _slice_of(cuts, tb["box"][1])
        if p is None:
            continue
        off = cuts[p]
        sh = lambda r: (r[0], r[1] - off, r[2], r[3] - off)
        raw.setdefault(p, []).append((t, sh(tb["box"]), [(sh(r), sh(c) if c else None) for r, c in tb["rows"]],
                                      [tuple(g) for g in tb["grid"]]))
    doc.raw_tables = raw
    _measure(doc)
    return doc
