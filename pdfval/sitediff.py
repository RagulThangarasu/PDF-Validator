"""Validate one AEM guide tree against another - page by page, both sides live web pages.

Two trees of the same guides published under different roots, e.g.

    A  /content/guide/rs-joshi/en/<product>/<topic>.html
    B  /content/guide/rs-joshi/post-script/en/<product>/<topic>.html

are compared publication by publication: for each product that both roots hold, one topic inside it
is opened (with ?wcmmode=disabled, so the page renders as a reader sees it, without the author
chrome), its left navigation is crawled to the bottom, and every topic is compared with the topic at
the same place in the other tree.

Pairing is by the path BELOW the publication, not by position: the two trees mirror each other, so
".../gw2291/safety-precautions.html" on A belongs with ".../gw2291/safety-precautions.html" on B
whatever order the navigation happens to list them in. A topic only one side has is reported as such
rather than silently lined up against an unrelated page.

Both sides are web pages of the same width, so unlike a PDF against a page everything is comparable -
the words, the tables, the pictures AND the measures in points (indent, alignment, line height). That
is what makes a difference a reader would see in the rendering show up here.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from . import engine
from .html_source import _filter_request
from .linkcheck import SignInError, crawl_guide, open_page
from .site_nav import key

WCM = "wcmmode=disabled"

# engine.compare() starts with engine._reset_caches(), which clears module-level caches shared by
# every check (ocr._BOXES, inline_icons._ICONS, assets._DOCS, ...) - fine for one comparison at a time,
# but several publications' topics are compared concurrently here (ThreadPoolExecutor in run()/_share()).
# One thread's "start fresh" reset can empty a cache entry another thread is mid-read on, surfacing as a
# stray KeyError / "document closed" on an otherwise fine page - this topic then goes unreported rather
# than compared. Serializing the compare() call itself (the crawl/screenshot work around it still runs
# concurrently) removes the race without giving up the concurrency that matters (I/O-bound browser work).
_COMPARE_LOCK = threading.Lock()


def with_wcm(url: str) -> str:
    """The page as a reader sees it: author chrome off."""
    if WCM in url:
        return url
    return url + ("&" if "?" in url else "?") + WCM


LAST_ERROR: dict[str, str] = {}   # url -> why the last read of it failed, for the message the caller shows


def _json(url: str, user: str, password: str, tries: int = 3) -> dict | list | None:
    """One Sling JSON read, with basic auth. None when it cannot be read.

    Retried: an author instance under load drops a request now and then, and a single failure used to
    look exactly like a wrong password - the run stopped with "check the user name" on a listing that
    had worked a minute earlier. The reason for the last failure is kept in LAST_ERROR so the caller
    can say what actually happened instead of guessing.
    """
    import base64
    import time as _t
    req = Request(url)
    if user:
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode())
    why = ""
    for n in range(tries):
        try:
            with urlopen(req, timeout=30) as r:
                LAST_ERROR.pop(url, None)
                return json.loads(r.read(4_000_000).decode("utf-8", "replace"))
        except Exception as e:
            why = f"{type(e).__name__}: {e}"
            if n + 1 < tries:
                _t.sleep(1.5 * (n + 1))
    LAST_ERROR[url] = why
    return None


def publications(root: str, user: str = "", password: str = "") -> list[str]:
    """The publication (product) names directly under a content root, read from AEM's own JSON. A root
    that answers with nothing readable gives [], and the caller is told rather than guessing."""
    data = _json(root.rstrip("/") + ".1.json", user, password)
    if not isinstance(data, dict):
        return []
    out = []
    for name, node in data.items():
        if name.startswith("jcr:") or name.startswith("rep:") or not isinstance(node, dict):
            continue
        if node.get("jcr:primaryType") in ("cq:Page", "sling:Folder", "nt:folder"):
            out.append(name)
    return sorted(out)


def first_topic(root: str, product: str, user: str = "", password: str = "") -> str:
    """A page inside the publication to start the crawl from: its first child page. The crawl then
    reaches the rest through the left navigation, so which one it is does not matter - only that it is
    a real topic page and not the folder."""
    base = f"{root.rstrip('/')}/{product}"
    data = _json(base + ".1.json", user, password)
    if isinstance(data, dict):
        for name, node in data.items():
            if name.startswith(("jcr:", "rep:")) or not isinstance(node, dict):
                continue
            if node.get("jcr:primaryType") == "cq:Page":
                return with_wcm(f"{base}/{name}.html")
    return with_wcm(base + ".html")


def below(url: str, root: str, product: str) -> str:
    """The part of the address below the publication - what a topic is paired by. Query and fragment
    are dropped, so ?wcmmode=disabled does not make two copies of the same topic look different."""
    path = urlsplit(url).path
    marker = f"/{product}/"
    return path.split(marker, 1)[1] if marker in path else path.rsplit("/", 1)[-1]


def crawl(page, start: str, root: str, product: str, scfg: dict, user: str, password: str,
          max_topics: int = 0, say=None) -> dict:
    """Every topic of one publication, through its left navigation, keyed by the path below it."""
    guide = {"start": start, "title": "", "pages": [], "error": ""}
    visit = lambda u: open_page(page, with_wcm(u), user, password, 300, say)
    visit(start)
    crawl_guide(page, guide, start, scfg, visit, max_topics, say)
    return {below(p["url"], root, product): p for p in guide["pages"] if not p.get("error")}


def compare_topic(page, a_url: str, b_url: str, out: Path, cfg: dict, user: str, password: str,
                  width: int = 1280, wait_ms: int = 1500) -> dict | None:
    """One topic on each side, rendered in the caller's own tab and compared.

    The tab is reused: capturing through html_source.capture would start a Chromium per page, two per
    topic pair - thousands of launches over a guide tree, and far more time than the page loads. Both
    Docs go into the comparison, so neither side is treated as "the PDF". None when a side could not be
    read, which is reported for that topic instead of failing the publication.
    """
    from . import html_source

    out.mkdir(parents=True, exist_ok=True)
    docs = {}
    for side, url in (("a", a_url), ("b", b_url)):
        doc, pdf = html_source.capture_in_tab(page, with_wcm(url), out / side, wait_ms=wait_ms, site=None)
        if doc is None:
            return None
        docs[side] = (doc, pdf)
    (doc_a, pdf_a), (doc_b, pdf_b) = docs["a"], docs["b"]
    with _COMPARE_LOCK:
        res = engine.compare(pdf_a, pdf_b, cfg, baseline_doc=doc_a, candidate_doc=doc_b)
        # a crop of A and a crop of B for every finding, the same screenshots a normal run produces: a
        # difference in the rendering is read from the two pictures, not from the sentence describing it
        try:
            from .report import shots
            res.setdefault("meta", {}).setdefault("baseline", {})["path"] = pdf_a
            res["meta"].setdefault("candidate", {})["path"] = pdf_b
            shots.render(res, out, "all")
        except Exception:
            pass   # a run without pictures still reports its differences
    return res


# What a reader sees that is not the words: the type, the colour, and where things sit. These are in
# [ignore] types for a prod-PDF-vs-stage run, and rightly so - a printed page and a web page are
# different widths and fonts, so comparing them there is noise. A against B is the SAME guide rendered
# the same way at the same width, so a difference in any of them is real and is exactly what "visual
# difference" means here. Turned back on for this comparison only; every other run is untouched.
VISUAL = {"font-family", "font-size", "font-weight", "color", "line-height", "text-align",
          "space-above", "indent", "line-wrap", "emphasis"}

# Where a link points is not a visual difference, and the two trees are published under different roots,
# so every cross-reference in B carries B's path: comparing targets reports the roots, over and over,
# not anything a reader sees. Left out of this comparison; a dead link is still the link check's job.
LINKS = {"link target differs", "link lands elsewhere", "link page number", "link to wrong section",
         "broken link", "missing link", "extra link", "link quotes", "page zero"}


def site_config(cfg: dict) -> dict:
    """The comparison settings for page against page.

    compare_url turns the point-based layout measures off because a PDF page and a web page are
    different widths. Here both sides are the same guide rendered the same way, so indent, alignment,
    line height, type and colour ARE comparable - and they carry the differences a reader sees. The
    design-spec typography check stays off: it judges against the PDF's spec, which is not the question
    when A is compared with B. The section scope is off as well: each call compares one topic, already
    paired.
    """
    import copy
    c = copy.deepcopy(cfg)
    c["sections"]["page_scope"] = False
    c["layout"]["enabled"] = True
    c["layout"]["compare_with_prod"] = True
    c.setdefault("typography", {})["enabled"] = False
    c["ignore"]["types"] = sorted((set(c["ignore"].get("types", [])) - VISUAL) | LINKS)
    c["genuine"]["types"] = sorted((set(c["genuine"].get("types", [])) | VISUAL) - LINKS)
    c.setdefault("style", {})["enabled"] = True
    return c


def all_pages(root: str, product: str, user: str = "", password: str = "", depth: int = 5) -> dict[str, str]:
    """Every page AEM holds under the publication: {path below it: URL}, read from AEM's own JSON.

    The left navigation is what a reader can reach, and it is not always everything that is published:
    gw2291 has 40 pages of which two (disclaimer, typographics) no navigation entry links to. Crawling
    the navigation alone would validate 38 of them and call the publication done. This is the other
    half - what exists - so nothing in the publication is skipped.

    One request returns `depth` levels. A branch that is still deeper than that comes back truncated,
    and a fixed depth would drop its pages without a word, so each page reached at the bottom of a
    response is asked for again and its own subtree walked. Nothing is left out however deep the tree
    goes, and a shallow guide still costs the one request.
    """
    base = f"{root.rstrip('/')}/{product}"
    out: dict[str, str] = {}
    seen: set[str] = set()

    def fetch(url: str, prefix: str, level: int = 0) -> None:
        if url in seen:
            return
        seen.add(url)
        data = _json(f"{url}.{depth}.json", user, password)
        if not isinstance(data, dict):
            return
        walk(data, prefix, url, 0)

    def walk(node: dict, prefix: str, node_url: str, level: int) -> None:
        for name, child in (node or {}).items():
            if name.startswith(("jcr:", "rep:", "cq:", "sling:")) or not isinstance(child, dict):
                continue
            if child.get("jcr:primaryType") != "cq:Page":
                continue
            path = f"{prefix}{name}"
            out[f"{path}.html"] = with_wcm(f"{base}/{path}.html")
            if level + 1 >= depth - 1:
                # as deep as this response goes: ask for this page's own subtree rather than stop here
                fetch(f"{base}/{path}", f"{path}/")
            else:
                walk(child, f"{path}/", f"{base}/{path}", level + 1)

    fetch(base, "")
    return out


def root_of(start: str, product: str) -> str:
    """The tree a start URL belongs to: everything before /<product>/. A publication is paired by its
    product name, so this is what turns the picked start URL back into the root its topics hang off."""
    marker = f"/{product}/"
    return start.split(marker, 1)[0] if marker in start else start.rsplit("/", 1)[0]


def _share(products: list[str], *, a_root: str, b_root: str, ccfg: dict, scfg: dict, user: str,
           password: str, max_topics: int, out_dir: Path, creds, session_state: dict, on_pub, say,
           starts: dict[str, tuple[str, str]] | None = None, own: str = "") -> list[dict]:
    """One worker's share of the publications: its own Playwright and browser (sync Playwright is not
    safe to share across threads), seeded with the session of the browser that signed in, so no worker
    fills the login form again. Each publication is crawled on both sides and its topics compared."""
    from playwright.sync_api import sync_playwright

    out = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            ctx = browser.new_context(viewport={"width": 1280, "height": 900},
                                      http_credentials=creds, storage_state=session_state or None)
            page = ctx.new_page()
            # Without this every page pulls the trackers and AEM's own ContextHub script, which keep the
            # DOM mutating so the "settle" wait burns its whole 4s cap on each of the two pages of every
            # pair. The guide's own files, stylesheets, fonts and images still load - the comparison
            # needs them for type, colour and layout.
            page.route("**/*", lambda route: _filter_request(route, own))
            for product in products:
                rec = {"product": product, "topics": [], "only_a": [], "only_b": [], "pairs": [], "error": ""}
                out.append(rec)
                try:
                    # the pair picked in the UI, or - with no pair given - the first topic under each root
                    a_start, b_start = (starts or {}).get(product, (None, None))
                    a_start = a_start or first_topic(a_root, product, user, password)
                    b_start = b_start or first_topic(b_root, product, user, password)
                    a_base, b_base = root_of(a_start, product), root_of(b_start, product)
                    # Which pages the publication has is asked of AEM - ONE request per side - instead of
                    # walking the navigation page by page. The walk cost 40 page loads per side before a
                    # single comparison could start, half the work of the whole publication, and it found
                    # strictly FEWER pages: the navigation reaches what a reader can click, AEM holds what
                    # is published (gw2291: 38 against 40). The navigation is still crawled when AEM
                    # answers with nothing, so a tree that cannot be listed is not simply skipped.
                    a_topics = {k: {"url": v, "nav_text": "", "title": "", "links": [], "error": ""}
                                for k, v in all_pages(a_base, product, user, password).items()}
                    b_topics = {k: {"url": v, "nav_text": "", "title": "", "links": [], "error": ""}
                                for k, v in all_pages(b_base, product, user, password).items()}
                    if not a_topics:
                        a_topics = crawl(page, a_start, a_base, product, scfg, user, password, max_topics, say)
                    if not b_topics:
                        b_topics = crawl(page, b_start, b_base, product, scfg, user, password, max_topics, say)
                    if max_topics:
                        a_topics = dict(sorted(a_topics.items())[:max_topics])
                        b_topics = dict(sorted(b_topics.items())[:max_topics])
                except SignInError:
                    raise
                except Exception as e:
                    rec["error"] = f"{type(e).__name__}: {e}"
                    on_pub(rec)
                    continue
                rec["a_pages"], rec["b_pages"] = len(a_topics), len(b_topics)
                rec["only_a"] = sorted(set(a_topics) - set(b_topics))
                rec["only_b"] = sorted(set(b_topics) - set(a_topics))
                rec["pairs"] = sorted(set(a_topics) & set(b_topics))
                for k, name in enumerate(rec["pairs"], 1):
                    say(f"{product}: topic {k}/{len(rec['pairs'])} {name}")
                    topic = {"name": name, "a": a_topics[name]["url"], "b": b_topics[name]["url"],
                             "findings": [], "error": ""}
                    rec["topics"].append(topic)
                    try:
                        res = compare_topic(page, topic["a"], topic["b"],
                                            out_dir / product / re.sub(r"[^\w.-]+", "-", name),
                                            ccfg, user, password)
                        if res is None:
                            topic["error"] = "The topic could not be read on one of the two sides"
                            continue
                    except Exception as e:
                        topic["error"] = f"{type(e).__name__}: {e}"
                        continue
                    tdir = out_dir / product / re.sub(r"[^\w.-]+", "-", name)
                    topic["dir"] = str(tdir)
                    # one page of this publication, as each side renders it: the report shows A and B
                    # side by side once per publication rather than once per finding
                    rec.setdefault("sample", {"topic": name,
                                              "a": str(tdir / "a" / "candidate_source.pdf"),
                                              "b": str(tdir / "b" / "candidate_source.pdf")})
                    topic["findings"] = [f for sec in res.get("sections", []) for f in sec.get("findings", [])
                                         if f.get("genuine")]
                    topic["match_pct"] = (res.get("summary") or {}).get("match_pct")
                on_pub(rec)
        finally:
            browser.close()
    return out


def run(a_root: str, b_root: str, cfg: dict, *, user: str = "", password: str = "",
        products: list[str] | None = None, max_topics: int = 0, out_dir: str | Path = "runs/_sitediff",
        concurrency: int = 4, progress=None, starts: dict[str, tuple[str, str]] | None = None) -> dict:
    """Compare every publication both roots hold, topic by topic.

    The publications are independent, so `concurrency` of them run at a time, each in its own browser
    started from the session of the one that signed in. Inside a publication the crawl stays in order:
    the navigation only reveals a branch's children once their parent topic is open.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from playwright.sync_api import sync_playwright

    say = progress or (lambda m: None)
    scfg = (cfg or {}).get("site") or {}
    ccfg = site_config(cfg)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    a_pubs = products or publications(a_root, user, password)
    b_pubs = products or publications(b_root, user, password)
    if not a_pubs or not b_pubs:
        bad = a_root if not a_pubs else b_root
        why = LAST_ERROR.get(bad.rstrip("/") + ".1.json", "")
        raise RuntimeError(
            f"Could not list the publications under {bad}."
            + (f" The server said: {why}." if why else "")
            + " Check the AEM user name and password, and that the root path is right.")
    both = [p for p in a_pubs if p in b_pubs]
    result = {"a_root": a_root, "b_root": b_root, "publications": [],
              "only_a": [p for p in a_pubs if p not in b_pubs],
              "only_b": [p for p in b_pubs if p not in a_pubs]}
    creds = {"username": user, "password": password} if user else None
    own = urlsplit(a_root).netloc
    # The settle wait is a cap, not a pause: a page that goes quiet returns at once. 4s is the right
    # worst case for a capture run of one guide; here it is paid twice per topic pair over thousands of
    # pages, and with the trackers filtered out these pages settle in a fraction of it.
    import pdfval.html_source as _hs
    _hs._IDLE_MS = min(_hs._IDLE_MS, 1200)

    # sign in once and hand the session to every worker, instead of each filling the login form
    say(f"Signing in as {user}" if user else "Opening the first publication")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            ctx = browser.new_context(viewport={"width": 1280, "height": 900}, http_credentials=creds)
            page = ctx.new_page()
            page.route("**/*", lambda route: _filter_request(route, own))
            open_page(page, first_topic(a_root, both[0], user, password), user, password, 300, say)
            session_state = ctx.storage_state()
        finally:
            browser.close()

    n = max(1, min(int(concurrency or 1), len(both)))
    shares = [both[i::n] for i in range(n)]
    done, lock = [0], threading.Lock()

    def on_pub(rec):
        with lock:
            done[0] += 1
            say(f"{done[0]}/{len(both)} publications - {rec['product']}: "
                f"{len(rec['topics'])} topic(s), {sum(len(t['findings']) for t in rec['topics'])} difference(s)")

    say(f"Comparing {len(both)} publication(s), {n} at a time")
    got: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=n) as ex:
        futures = [ex.submit(_share, share, a_root=a_root, b_root=b_root, ccfg=ccfg, scfg=scfg,
                             user=user, password=password, max_topics=max_topics, out_dir=out_dir,
                             creds=creds, session_state=session_state, on_pub=on_pub, say=lambda m: None,
                             starts=starts, own=own)
                   for share in shares if share]
        for f in futures:
            for rec in f.result():
                got[rec["product"]] = rec
    result["publications"] = [got[p] for p in both if p in got]
    return result


def product_of(pub: dict) -> str:
    """The product a publication listing stands for: the last segment of its index path
    (/content/guide/rs-joshi/post-script/en/gw2291 -> gw2291). Two trees publish the same manual under
    different roots, so this - not the position in the list, and not the page title, which the two
    sides may word differently - is what pairs them."""
    site = (pub.get("site") or "").rstrip("/")
    return site.rsplit("/", 1)[-1] if site else ""


def match(a_pubs: list[dict], b_pubs: list[dict]) -> dict:
    """Pair two publication listings by product. Returns
    {"pairs": [{product, a, b, a_pages, b_pages}], "only_a": [...], "only_b": [...]} - a product only
    one side has is named, never paired with something else."""
    a = {product_of(p): p for p in a_pubs if product_of(p)}
    b = {product_of(p): p for p in b_pubs if product_of(p)}
    pairs = [{"product": k, "a": a[k].get("start") or a[k].get("url"), "b": b[k].get("start") or b[k].get("url"),
              "a_pages": a[k].get("pages", 0), "b_pages": b[k].get("pages", 0)}
             for k in sorted(set(a) & set(b))]
    return {"pairs": pairs,
            "only_a": sorted(set(a) - set(b)),
            "only_b": sorted(set(b) - set(a))}


def run_pairs(pairs: list[dict], cfg: dict, **kw) -> dict:
    """Compare the publications already paired by product (match(), or the Link check page's own
    table): each pair carries the start URL to crawl from on each side, so nothing has to be guessed
    from the roots again."""
    starts = {p["product"]: (p["a"], p["b"]) for p in pairs if p.get("a") and p.get("b")}
    if not starts:
        raise RuntimeError("No publication is in both trees - nothing to compare.")
    first = pairs[0]
    kw.setdefault("products", sorted(starts))
    return run(root_of(first["a"], first["product"]), root_of(first["b"], first["product"]),
               cfg, starts=starts, **kw)
