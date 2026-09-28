"""AEM Guides source topics: which DITA topic (GUID) each stage issue comes from.

A PDF published by AEM Guides has a named destination for every topic and for the
elements inside it, named after the topic's GUID:

    GUID-17f6ffdc-ff83-4a35-9a6e-5d5bc655db60-en              the topic (one .dita file)
    GUID-17f6ffdc-ff83-4a35-9a6e-5d5bc655db60-en-section_3    a section / table / list / note in it

Each destination is a position in the PDF, so a stage issue belongs to the last
destination at or above it. The finding gets `aem = {guid, lang, topic, element, url}`
and the result gets a per-topic list, so a reviewer can open the topic in AEM
(new tab) and fix it there. The URL comes from `[aem] link` in the config with
the product's DAM folder from `[aem.products]` (keyed by the ditamap name).
"""
from __future__ import annotations

import re
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from urllib.parse import quote

import pymupdf

_DEST = re.compile(r"^(GUID-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
                   r"(?:-([A-Za-z]{2}(?:[-_][A-Za-z]{2})?))?(?:-(.+))?$")


@dataclass
class Anchor:
    page: int
    y: float  # top-down, like word boxes
    guid: str
    lang: str
    element: str  # "" = the topic itself
    title: str


def anchors(path: str) -> tuple[list[Anchor], dict]:
    """GUID destinations of an AEM Guides PDF in reading order, and {map, creator}.
    Empty when the PDF has none (not published by AEM Guides, or a web capture)."""
    try:
        doc = pymupdf.open(path)
        names = doc.resolve_names()
    except Exception:
        return [], {}
    out = []
    for name, dest in names.items():
        m = _DEST.match(name)
        page = dest.get("page", -1)
        if not m or not 0 <= page < doc.page_count or not dest.get("to"):
            continue
        y = max(0.0, doc[page].rect.height - dest["to"][1])  # PDF user space is bottom-up
        out.append(Anchor(page, round(y, 1), m.group(1), (m.group(2) or "").lower(), m.group(3) or "",
                          _title_below(doc[page], y)))
    out.sort(key=lambda a: (a.page, a.y, a.element != ""))
    info = {"map": (doc.metadata or {}).get("title", ""), "creator": (doc.metadata or {}).get("creator", "")}
    return out, info


def _title_below(page: pymupdf.Page, y: float) -> str:
    """Text of the first line at or below the anchor (the topic / section heading)."""
    lines = {}
    for w in page.get_text("words"):
        if y - 3 <= w[1] <= y + 40:
            lines.setdefault((w[5], w[6]), []).append(w)
    if not lines:
        return ""
    first = min(lines.values(), key=lambda ws: (min(w[1] for w in ws), min(w[0] for w in ws)))
    text = " ".join(w[4] for w in sorted(first, key=lambda w: w[0]))
    text = re.sub(r"^\W+\s*", "", text)  # a list anchor's line starts with its bullet
    return text[:80] if re.search(r"\w", text) else ""


class Locator:
    def __init__(self, items: list[Anchor]):
        self.items = items
        self.keys = [(a.page, a.y) for a in items]
        self.topics = {a.guid: a for a in items if not a.element}

    def at(self, page: int, y: float) -> tuple[Anchor, Anchor] | None:
        """(topic, nearest element) for a position; the element is the topic itself when none is closer."""
        i = bisect_right(self.keys, (page, y + 4)) - 1  # a word's box starts a little below its anchor
        if i < 0:
            return None
        near = self.items[i]
        topic = self.topics.get(near.guid, near)
        return topic, near


def product_of(map_name: str, cfg: dict) -> tuple[str, str]:
    """(product key, DAM folder) for a ditamap name like 'sl04_and_sh04.ditamap'."""
    key = re.sub(r"\.ditamap$", "", map_name or "", flags=re.I)
    products = cfg.get("products") or {}
    for pat, folder in products.items():
        if pat.lower() == key.lower() or re.fullmatch(pat, key, re.I):
            return key, folder
    return key, ""


def url_for(a: dict, cfg: dict) -> str:
    """The link that opens the topic in AEM: the editor with the topic file AEM reported for the
    GUID (resolve()). The file name is not derived from the GUID, so without a looked-up path the
    link opens the editor's Explorer (`fallback_link`), never a guessed file that does not exist."""
    tmpl, author = cfg.get("link", ""), (cfg.get("author") or "").rstrip("/")
    if not tmpl or ("{author}" in tmpl and not author):
        return ""
    folder = (a.get("folder") or cfg.get("dam_root", "")).rstrip("/")
    path = a.get("path")
    if not path:
        tmpl = cfg.get("fallback_link") or "{author}/libs/fmdita/clientlibs/xmleditor/page.html?leftPanel=repository_panel&appMode=author"
        path = ""
    return tmpl.format(author=author, guid=a["guid"], lang=a["lang"], element=a.get("element", ""),
                       map=a.get("map", ""), product=a.get("product", ""), folder=folder,
                       path=quote(path, safe="/"), q=quote(a["guid"]))


_PATHS: dict[tuple[str, str], str] = {}  # (author, guid) -> DAM path, for the life of the process


def check_login(cfg: dict) -> tuple[bool, str]:
    """Does AEM accept this user/password? (AEM's current-user endpoint answers "anonymous" otherwise.)"""
    import base64
    import json
    from urllib import request as _rq
    from urllib.error import HTTPError, URLError

    author = (cfg.get("author") or "").rstrip("/")
    if not author:
        return False, "Set the AEM author URL first"
    if not cfg.get("user") or not cfg.get("password"):
        return False, "Enter the AEM user and password"
    auth = "Basic " + base64.b64encode(f"{cfg['user']}:{cfg['password']}".encode()).decode()
    req = _rq.Request(f"{author}/libs/granite/security/currentuser.json", headers={"Authorization": auth})
    try:
        with _rq.urlopen(req, timeout=15) as r:
            who = json.load(r).get("authorizableId", "")
    except HTTPError as e:
        return False, "AEM refused the login (user name or password wrong)" if e.code in (401, 403) else f"AEM answered HTTP {e.code}"
    except (URLError, OSError, ValueError) as e:
        return False, f"AEM not reachable: {e}"
    if not who or who == "anonymous":
        return False, "AEM refused the login (user name or password wrong)"
    return True, f"Logged in to AEM as {who}"


def resolve(guids: list[tuple[str, str]], cfg: dict) -> tuple[dict[str, str], str]:
    """Ask AEM (QueryBuilder) where each topic file is: ({guid: DAM path}, error message).
    guids: [(guid, lang)]. Needs cfg['user'] and cfg['password']; without them nothing is looked up.
    The file is found by name (GUID-….dita) or, failing that, by the GUID in its content; a hit in
    the topic's language folder (/en/) wins."""
    import base64
    import json
    from urllib.error import HTTPError, URLError
    from urllib.parse import urlencode
    from urllib.request import Request
    from urllib import request as _rq

    author = (cfg.get("author") or "").rstrip("/")
    if not author or not cfg.get("user") or not cfg.get("password"):
        return {}, ""
    auth = "Basic " + base64.b64encode(f"{cfg['user']}:{cfg['password']}".encode()).decode()
    root = cfg.get("search_root") or "/content/dam"
    out, err = {}, ""

    def query(params: dict) -> list[str]:
        q = {"path": root, "type": "dam:Asset", "p.limit": "20", "p.hits": "selective", "p.properties": "jcr:path", **params}
        req = Request(f"{author}/bin/querybuilder.json?{urlencode(q)}", headers={"Authorization": auth})
        with _rq.urlopen(req, timeout=15) as r:
            return [h["jcr:path"] for h in json.load(r).get("hits", [])]

    for guid, lang in guids:
        if (author, guid) in _PATHS:
            out[guid] = _PATHS[(author, guid)]
            continue
        try:
            hits = query({"nodename": f"{guid}*"}) or query({"fulltext": guid, "nodename": "*.dita"})
        except HTTPError as e:
            return out, ("AEM refused the login (user name or password wrong)" if e.code in (401, 403)
                         else f"AEM search failed: HTTP {e.code}")
        except (URLError, OSError, ValueError) as e:
            return out, f"AEM not reachable: {e}"
        hits = [h for h in hits if re.search(r"\.(dita|xml)$", h, re.I)] or hits
        if hits:
            hits.sort(key=lambda h: (f"/{lang}/" not in h if lang else False, len(h)))
            out[guid] = _PATHS[(author, guid)] = hits[0]
        else:
            err = err or f"{guid} not found in AEM under {root}"
    return out, err


def merge_settings(acfg: dict, saved: dict) -> dict:
    """Config [aem] with the settings saved from the UI on top (products merged)."""
    out = {**acfg, **{k: v for k, v in saved.items() if k != "products" and v not in (None, "")}}
    out["products"] = {**(acfg.get("products") or {}), **(saved.get("products") or {})}
    return out


def relink(result: dict, acfg: dict) -> bool:
    """(Re)build every AEM link of a result from the settings (author, template, product folder),
    looking up each topic's DAM path in AEM when a login is configured."""
    a = result.get("aem")
    if not a:
        return False
    a["product"], a["folder"] = product_of(a.get("map", ""), acfg)
    a["author"] = acfg.get("author", "")
    paths, a["resolve_error"] = resolve([(t["guid"], t["lang"]) for t in a["topics"]], acfg)
    base = {"map": a.get("map", ""), "product": a["product"], "folder": a["folder"]}
    urls = {}
    for t in a["topics"]:
        t["path"] = paths.get(t["guid"], t.get("path", ""))
        t["url"] = urls[t["guid"]] = url_for({**base, **t}, acfg)
    a["resolved"] = sum(bool(t["path"]) for t in a["topics"])
    by_guid = {t["guid"]: t for t in a["topics"]}
    for s in result["sections"]:
        for x in s.get("aem", []):
            x["url"] = urls.get(x["guid"], "")
        for f in s["findings"]:
            if f.get("aem"):
                f["aem"]["path"] = by_guid.get(f["aem"]["guid"], {}).get("path", "")
                f["aem"]["url"] = url_for({**base, **f["aem"]}, acfg)
    return True


def annotate(result: dict, cfg: dict) -> None:
    """Add `aem` to every finding and section of a PDF ⇄ PDF result, and `result['aem']`."""
    acfg = cfg.get("aem", {})
    if not acfg.get("enabled", True) or result["meta"].get("mode") == "html":
        return
    items, info = anchors(result["meta"]["candidate"]["path"])
    if not items:
        return
    loc = Locator(items)
    product, folder = product_of(info.get("map", ""), acfg)
    base = {"map": info.get("map", ""), "product": product, "folder": folder}
    topics: dict[str, dict] = {}

    def ref(page: int, y: float) -> dict | None:
        hit = loc.at(page, y)
        if not hit:
            return None
        topic, near = hit
        r = {"guid": topic.guid, "lang": topic.lang, "topic": topic.title,
             "element": near.element, "element_title": near.title if near.element else ""}
        r["url"] = ""  # set by relink() below
        return r

    for s in result["sections"]:
        st = s["candidate"]["start"]
        s_ref = ref(st["page"], st["y"])
        seen = Counter()
        for f in s["findings"]:
            at = (f.get("candidate") or [None])[0] or f.get("candidate_at")
            r = ref(at["page"], at["bbox"][1]) if at else s_ref
            if not r:
                continue
            f["aem"] = {**r, "exact": bool(at)}  # exact = located by the issue's own position in stage
            seen[r["guid"]] += 1
            t = topics.setdefault(r["guid"], {"guid": r["guid"], "lang": r["lang"], "topic": r["topic"], "url": r["url"],
                                              "page": loc.topics[r["guid"]].page + 1 if r["guid"] in loc.topics else None,
                                              "issues": 0, "genuine": 0, "critical": 0, "sections": []})
            t["issues"] += 1
            t["genuine"] += bool(f.get("genuine"))
            t["critical"] += bool(f.get("critical"))
            if s["id"] not in t["sections"]:
                t["sections"].append(s["id"])
        s["aem"] = [{"guid": g, "topic": topics[g]["topic"], "url": topics[g]["url"], "issues": n}
                    for g, n in seen.most_common()]
    result["aem"] = {**base, "author": acfg.get("author", ""), "topics_in_pdf": len(loc.topics),
                     "topics": sorted(topics.values(), key=lambda t: (-t["genuine"], -t["critical"], -t["issues"]))}
    relink(result, acfg)
