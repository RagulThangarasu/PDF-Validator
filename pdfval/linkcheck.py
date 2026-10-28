"""Link check: crawl a guide through its left navigation and report, for every topic, the topic's
GUID and the links in its body that do not work.

This is not a comparison. There is no prod PDF and no baseline: the pages are judged on their own.
A link fails when it does not open (HTTP 4xx / 5xx, no response at all, a dead #anchor), or when it
still carries a DITA GUID - in the address, in the text the reader sees, or because a readable
address redirects to a GUID page. Only the links in the topic body are checked: the left navigation
is what the crawl walks, and it repeats unchanged on every page of the guide.

Several guides are checked in one run: the caller passes a list of start URLs, one per guide.
"""
from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

from .html_source import _filter_request, _login_form, _settle, _sign_in, split_login
from .site_nav import _GUID, key, read_chrome

# a GUID in the page's own address: GUID-….html, or …/GUID-…/… - the topic's own id, not a link's
_GUID_IN_URL = re.compile(r"(GUID-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})", re.I)

# where a published AEM Guides page keeps its topic id, when the address does not carry it
_GUID_JS = r"""() => {
  const pick = s => {
    const m = /GUID-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/.exec(s || '');
    return m ? m[0] : '';
  };
  // a meta tag or a data- attribute on the page's own container, never an attribute of a link:
  // a link's GUID belongs to the page it points at, not to this one
  for (const m of document.querySelectorAll('meta[name], meta[property]')) {
    const g = pick(m.getAttribute('content'));
    if (g) return { guid: g, where: 'meta ' + (m.getAttribute('name') || m.getAttribute('property')) };
  }
  for (const e of document.querySelectorAll('[data-guid],[data-topic-id],[data-id],[id],[class]')) {
    if (e.closest('a[href]')) continue;
    for (const a of ['data-guid', 'data-topic-id', 'data-id', 'id', 'class']) {
      const g = pick(e.getAttribute(a));
      if (g) return { guid: g, where: e.tagName.toLowerCase() + '[' + a + ']' };
    }
  }
  return { guid: '', where: '' };
}"""


def topic_guid(page) -> tuple[str, str]:
    """(GUID, where it was read) of the topic the page shows, from its own address or its HTML.
    ('', '') when the page does not publish one - never guessed from a link on the page."""
    m = _GUID_IN_URL.search(page.url or "")
    if m:
        return m.group(1), "page address"
    try:
        got = page.evaluate(_GUID_JS)
    except Exception:
        return "", ""
    return got.get("guid", ""), got.get("where", "")


def _probe(urls: list[str], cookie_header: str, workers: int = 8, progress=None) -> dict:
    """{key(url): {status, url, title, h1, ids, error}} - the address is asked for its status only, several
    at a time. The link's page is never opened in the browser and never crawled: the check asks the
    address what it answers (HEAD), and reads the page itself only when the link points at a #fragment
    that has to exist. Replays the browser's own cookies, so a guide behind a login is asked as the
    signed-in user."""
    def one(u: str) -> dict:
        import urllib.error
        from urllib.request import Request, urlopen
        # a link to a #fragment is the one case the page's own HTML is needed for (does the anchor
        # exist?); every other link is only asked for its status line. The question is asked per page,
        # not per link: key() drops the fragment, so one page's entry serves “…/p.html” and
        # “…/p.html#sec” alike - fetched as HTML as soon as any link to it names an anchor
        want_html = key(u) in with_anchor
        head = {"Cookie": cookie_header} if cookie_header else {}
        try:
            req = Request(u, headers=head, method="GET" if want_html else "HEAD")
            with urlopen(req, timeout=20) as resp:
                html = (resp.read(1_000_000).decode("utf-8", "replace")
                        if want_html and "html" in (resp.headers.get("content-type") or "") else "")
                status, final = resp.status, resp.geturl()
        except urllib.error.HTTPError as e:
            # a server that refuses HEAD (405 / 501) is asked again the way a reader would ask
            if not want_html and e.code in (403, 405, 501):
                try:
                    with urlopen(Request(u, headers=head), timeout=20) as resp:
                        status, final, html = resp.status, resp.geturl(), ""
                except urllib.error.HTTPError as e2:
                    status, final, html = e2.code, e2.url, ""
                except Exception as e2:
                    return {"status": 0, "url": u, "error": f"{type(e2).__name__}: {e2}"}
            else:
                html = e.read(1_000_000).decode("utf-8", "replace") if want_html and "html" in (e.headers.get("content-type") or "") else ""
                status, final = e.code, e.url
        except Exception as e:
            return {"status": 0, "url": u, "error": f"{type(e).__name__}: {e}"}
        t = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        h = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S | re.I)
        strip = lambda s: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()
        return {"status": status, "url": final, "title": strip(t.group(1)) if t else "",
                "h1": strip(h.group(1)) if h else "",
                "ids": re.findall(r"""\s(?:id|name)=["']([^"']+)["']""", html)[:5000]}

    web = [u for u in urls if urlsplit(u).scheme in ("http", "https")]
    with_anchor = {key(u) for u in web if "#" in u}
    out, todo = {}, list({key(u): u for u in web}.values())  # one request per page, not per link
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for n, (u, got) in enumerate(zip(todo, ex.map(one, todo)), 1):
            if progress and n % 10 == 0:
                progress(f"Checking link {n}/{len(todo)}")
            out[key(u)] = got
    return out


def classify(text: str, href: str, st: dict | None) -> dict:
    """What the report says about one link: status (fail / pass / skip), a kind and a sentence.
    Only the two things this check is for - the link does not open, or it carries a GUID."""
    frag = href.split("#", 1)[1] if "#" in href else ""
    # the GUID is in the address (or in the label) the page itself carries: it is read off what was
    # captured, so a link with a GUID is reported as such whether or not the address answers at all
    in_href, in_text = _GUID.search(href), _GUID.search(text or "")
    if in_href or in_text:
        where = "address and label" if in_href and in_text else "address" if in_href else "label"
        return {"status": "fail", "kind": "GUID in the link",
                "note": f"The link's {where} still carries the topic GUID {(in_href or in_text).group(0)}"}
    if urlsplit(href).scheme not in ("http", "https"):
        return {"status": "skip", "kind": "not a web link", "note": "Not an http(s) link - not checked"}
    if not st:
        return {"status": "skip", "kind": "not checked", "note": "The link was not checked"}
    if st.get("error"):
        return {"status": "fail", "kind": "broken", "note": f"The link does not open: {st['error']}"}
    code = st.get("status", 0)
    if not 0 < code < 400:
        return {"status": "fail", "kind": "broken", "note": f"The link does not open: HTTP {code}"}
    if frag and frag not in (st.get("ids") or []):
        return {"status": "fail", "kind": "dead anchor",
                "note": f"The page opens but it has no #{frag} to jump to"}
    redirected = _GUID.search(st.get("url") or "")
    if redirected:
        return {"status": "fail", "kind": "redirects to a GUID address",
                "note": f"The link opens, but it lands on the GUID address {st['url'][:160]}"}
    return {"status": "pass", "kind": "opens", "note": f"HTTP {code} - {st.get('h1') or st.get('title') or 'opens'}"}


def _topics(chrome: dict, start: str) -> list[tuple[str, str]]:
    """(text, href) of every topic in the guide's left navigation, in navigation order, each once.
    A guide with no navigation is just its own page."""
    seen, out = set(), []
    for it in ((chrome.get("nav") or {}).get("items") or []):
        href = it.get("href") or ""
        if urlsplit(href).scheme not in ("http", "https"):
            continue
        k = key(href)
        if k not in seen:
            seen.add(k)
            out.append((it.get("text") or "", href))
    if key(start) not in seen:
        out.insert(0, ("", start))
    return out


def _same_guide(href: str, start: str) -> bool:
    """Is this navigation entry a topic of the guide that was started from? The guide is the folder its
    start page lives in (…/content/guide/consumer/monitor/gr10/en/…), so a link to another product's
    guide, or to the site's home, is not crawled as one of this guide's topics."""
    a, b = urlsplit(href), urlsplit(start)
    if a.scheme not in ("http", "https") or a.netloc != b.netloc:
        return False
    root = b.path.rsplit("/", 1)[0] + "/"
    return a.path.startswith(root)


def crawl_guide(page, guide: dict, start: str, scfg: dict, visit, max_pages: int = 0, say=None) -> dict:
    """Walk one guide: every topic of its left navigation, to the bottom of the tree, reading each
    topic's GUID and capturing the links in its body. Fills `guide` in place and returns it.

    The queue grows as the crawl goes. A navigation that renders a branch's children only while that
    branch is open shows them once their parent topic is the open one, and they are queued then - so
    the whole guide is covered, not only the level the start page happened to show. `max_pages` (the
    UI's "max topics per guide", 0 = all) cuts the crawl short; what is left is counted, so a
    part-crawled guide never reads like a guide whose every topic came back clean."""
    talk = say or (lambda m: None)
    chrome = read_chrome(page, scfg) or {}
    guide["title"] = chrome.get("title") or ""
    # the navigation can also link out of the guide (another product's manual, the site's home): those
    # are not this guide's topics and are not crawled as if they were - they are still checked as links
    # wherever a topic body points at them
    queue = [t for t in _topics(chrome, start) if _same_guide(t[1], start) or key(t[1]) == key(start)]
    done: set[str] = set()
    discovered = {key(h) for _, h in queue}  # every topic the navigation has shown so far
    n = 0
    while queue and not (max_pages and n >= max_pages):
        nav_text, href = queue.pop(0)
        if key(href) in done:
            continue
        done.add(key(href))
        n += 1
        talk(f"topic {n}{f'/{max_pages}' if max_pages else ''} of {len(queue) + n} - {nav_text or href}")
        rec = {"url": href, "nav_text": nav_text, "title": "", "guid": "", "guid_from": "",
               "links": [], "error": ""}
        guide["pages"].append(rec)
        try:
            visit(href)
        except Exception as e:
            rec["error"] = f"The topic did not open: {type(e).__name__}: {e}"
            continue
        c = read_chrome(page, scfg) or {}
        rec["url"] = page.url
        rec["title"] = c.get("h1") or c.get("title") or nav_text
        rec["guid"], rec["guid_from"] = topic_guid(page)
        rec["links"] = [{"text": (it.get("text") or "").strip(), "href": it.get("href") or "",
                         "where": it.get("where") or ""}
                        for it in (c.get("content_links") or []) if it.get("href")]
        # whatever this page's navigation shows that has not been seen yet: the deeper levels of the
        # tree, revealed now that their parent is the open topic
        for found in _topics(c, rec["url"]):
            if key(found[1]) not in discovered and _same_guide(found[1], start):
                discovered.add(key(found[1]))
                queue.append(found)
    guide["nav_items"] = len(discovered)          # topics the left navigation has in total
    guide["not_crawled"] = len(discovered) - len(done)  # left out by "max topics per guide"
    return guide


class SignInError(RuntimeError):
    """The crawl reached a sign-in page it could not get past: without this the whole run reads the
    login page instead of the guide - every topic comes back as "AEM Sign In" with the login page's own
    links, which looks like a result but says nothing about the guide."""


def open_page(page, url: str, user: str = "", password: str = "", wait_ms: int = 300, say=None) -> bool:
    """Open `url` and wait until it is ready to read; returns whether a sign-in was needed for it.

    Waiting matters: the left navigation is rendered after DOMContentLoaded, and without the wait
    read_chrome finds no navigation and the guide is crawled as the single page it was started from.

    AEM author answers a request it has no session for with its own sign-in page - HTTP 200, not 401,
    so basic auth alone never gets past it. The form is filled in (html_source._sign_in, the same
    sign-in a web capture does) and the page that was wanted is asked for again, because the sign-in
    lands wherever the site sends it. Raises SignInError rather than let the run read the login page as
    if it were the guide."""
    talk = say or (lambda m: None)
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    _settle(page, wait_ms)
    if not _login_form(page):
        return False
    if not user or not password:
        raise SignInError(
            f"{url} answers with a sign-in page. Enter the AEM user name and password above (the same "
            f"login the comparison runs use) and check the links again."
            + (" A user name was given without a password." if user else ""))
    talk(f"Signing in as {user}")
    _sign_in(page, user, password, 60000, wait_ms)
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    _settle(page, wait_ms)
    if _login_form(page):
        raise SignInError(
            f"Sign-in failed for user \u201c{user}\u201d: {url} still shows its sign-in form. Check the "
            f"user name and password, and that this user may open the guide in AEM.")
    return True


def _crawl_share(starts: list[str], *, creds, session_state: dict, own: str, scfg: dict,
                 user: str, password: str, max_pages: int, wait_ms: int, on_guide, say) -> list[dict]:
    """One worker's share of the guides: its own Playwright and browser (sync Playwright is not safe to
    share across threads), each guide crawled in its own tab, one after another. `session_state` carries
    the cookies of the browser that signed in, so this fresh browser is already signed in and no worker
    has to fill the login form again. Returns one guide dict per start URL, in the order given."""
    from playwright.sync_api import sync_playwright

    out: list[dict] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                      http_credentials=creds, storage_state=session_state or None)
            page = ctx.new_page()
            page.route("**/*", lambda route: _filter_request(route, own))
            visit = lambda url: open_page(page, url, user, password, wait_ms, say)
            for start in starts:
                guide = {"start": start, "title": "", "pages": [], "error": ""}
                out.append(guide)
                try:
                    visit(start)
                    crawl_guide(page, guide, start, scfg, visit, max_pages,
                                lambda m: say(f"{_short_guide(start)}: {m}"))
                except SignInError:
                    raise
                except Exception as e:
                    guide["error"] = f"The start page did not open: {type(e).__name__}: {e}"
                on_guide(guide)
        finally:
            browser.close()
    return out


def _short_guide(url: str) -> str:
    """…/content/guide/consumer/monitor/gr10/en/x.html -> consumer/monitor/gr10 - short enough for a
    progress line that several guides write to at once."""
    path = urlsplit(url).path
    part = path.split("/content/guide/", 1)[-1] if "/content/guide/" in path else path
    return "/".join(part.strip("/").split("/")[:3]) or url


def run(urls: list[str], cfg: dict, *, user: str = "", password: str = "", max_pages: int = 0,
        wait_ms: int = 300, concurrency: int = 4, link_workers: int = 16, progress=None) -> dict:
    """Crawl every guide in `urls` through its left navigation and check the links in each topic.
    Returns {"guides": [...], "totals": {...}} - no file is written here.

    AEM author answers an unauthenticated request with its own sign-in page (HTTP 200, not 401), so
    basic auth alone is not enough: one browser signs in through the form first (the same sign-in as a
    web capture, html_source._sign_in) and the run stops with a clear error when it cannot get past it.

    Speed: the guides are independent, so `concurrency` of them are crawled at a time, each by its own
    browser started from the signed-in session - no worker signs in again. Inside a guide the crawl
    stays in order, because the navigation reveals its deeper levels only as their parent topic opens.
    The links themselves are opened `link_workers` at a time as plain HTTP, once, however many topics
    point at them."""
    from playwright.sync_api import sync_playwright

    say = progress or (lambda m: None)
    scfg = (cfg or {}).get("site") or {}
    starts = []
    for u in urls:
        if not (u or "").strip():
            continue
        # a login carried in the address itself (http://user:pass@host/...) is the login to use: Chromium
        # would otherwise strip it and the crawl would land on the sign-in page
        clean, u_user, u_password = split_login(u.strip())
        starts.append(clean)
        user, password = user or u_user, password or u_password
    if not starts:
        return {"guides": [], "totals": totals([])}
    creds = {"username": user, "password": password} if user else None
    own = urlsplit(starts[0]).netloc

    # sign in once, on one browser, and hand its session to every worker: filling the login form on each
    # of them instead would be one extra page load per worker, and 30 guides' worth of sign-ins
    say("Signing in" if user else "Opening the first guide")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            ctx = browser.new_context(viewport={"width": 1440, "height": 900}, http_credentials=creds)
            page = ctx.new_page()
            page.route("**/*", lambda route: _filter_request(route, own))
            open_page(page, starts[0], user, password, wait_ms, say)   # raises SignInError, clearly
            session_state = ctx.storage_state()
            cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in ctx.cookies())
        finally:
            browser.close()

    n = max(1, min(int(concurrency or 1), len(starts)))
    shares: list[list[str]] = [starts[i::n] for i in range(n)]   # round robin: the long guides spread out
    done = [0]
    lock = threading.Lock()

    def on_guide(guide: dict) -> None:
        with lock:
            done[0] += 1
            say(f"Crawled {done[0]}/{len(starts)} guide(s) - {len(guide['pages'])} topic(s) in "
                f"{_short_guide(guide['start'])}")

    say(f"Crawling {len(starts)} guide(s), {n} at a time")
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=n) as ex:
        futures = [ex.submit(_crawl_share, share, creds=creds, session_state=session_state, own=own,
                             scfg=scfg, user=user, password=password, max_pages=max_pages,
                             wait_ms=wait_ms, on_guide=on_guide, say=lambda m: None)
                   for share in shares if share]
        for f in futures:
            for guide in f.result():          # a SignInError from any worker stops the whole run
                results[guide["start"]] = guide
    guides = [results[s] for s in starts if s in results]

    wanted = {l["href"] for g in guides for p in g["pages"] for l in p["links"]}
    say(f"Checking {len(wanted)} link(s)")
    status = _probe(sorted(wanted), cookie_header, workers=link_workers, progress=say)
    for g in guides:
        for p in g["pages"]:
            for l in p["links"]:
                l.update(classify(l["text"], l["href"], status.get(key(l["href"]))))
    return {"guides": guides, "totals": totals(guides)}


def totals(guides: list[dict]) -> dict:
    links = [l for g in guides for p in g["pages"] for l in p["links"]]
    pages = [p for g in guides for p in g["pages"]]
    return {"guides": len(guides), "topics": len(pages),
            # what the left navigation has, against what was crawled: a guide cut short by "max topics
            # per guide" must not read like a guide whose every topic came back clean
            "nav_topics": sum(g.get("nav_items") or len(g["pages"]) for g in guides),
            "not_crawled": sum(g.get("not_crawled") or 0 for g in guides),
            "topics_without_guid": sum(1 for p in pages if not p["guid"] and not p["error"]),
            "links": len(links),
            "failed": sum(1 for l in links if l.get("status") == "fail"),
            "broken": sum(1 for l in links if l.get("kind") in ("broken", "dead anchor")),
            "guid_links": sum(1 for l in links if "GUID" in (l.get("kind") or "")),
            "skipped": sum(1 for l in links if l.get("status") == "skip"),
            "page_errors": sum(1 for p in pages if p["error"]) + sum(1 for g in guides if g["error"])}


def rows(guides: list[dict]) -> list[dict]:
    """One row per link, for the CSV and the report table."""
    out = []
    for g in guides:
        for p in g["pages"]:
            if p["error"] and not p["links"]:
                out.append({"guide": g["start"], "topic": p["nav_text"] or p["url"], "topic_url": p["url"],
                            "guid": "", "link_text": "", "href": "", "status": "fail",
                            "kind": "topic did not open", "note": p["error"]})
                continue
            for l in p["links"]:
                out.append({"guide": g["start"], "topic": p["title"] or p["nav_text"], "topic_url": p["url"],
                            "guid": p["guid"], "link_text": l["text"], "href": l["href"],
                            "status": l.get("status", ""), "kind": l.get("kind", ""), "note": l.get("note", "")})
    return out
