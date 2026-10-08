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


def map_path(a: dict, cfg: dict) -> str:
    """DAM path of the product's map: the one found in AEM, else <product folder>/<Maps>/<map file>
    (the map file is the stage PDF's title, e.g. sl04_and_sh04.ditamap)."""
    if a.get("map_path"):
        return a["map_path"]
    folder, name = (a.get("folder") or "").rstrip("/"), a.get("map") or ""
    if not folder or not name.lower().endswith(".ditamap"):
        return ""
    return f"{folder}/{cfg.get('maps_folder', 'Maps')}/{name}"


def url_for(a: dict, cfg: dict, open_in: str | None = None) -> str:
    """The link a GUID opens in AEM. open_in = "topic" (default, `[aem] open_in`): the topic file AEM
    reported for the GUID (resolve()), else the product's map in the editor; "map": always the map,
    with every topic of the map. Without a known file / map the link opens the editor's Explorer
    (`fallback_link`), never a guessed file that does not exist."""
    author = (cfg.get("author") or "").rstrip("/")
    mode = open_in or cfg.get("open_in", "topic")
    if (mode == "map" or not a.get("path")) and author and (mp := map_path(a, cfg)):
        tmpl = cfg.get("map_link") or "{author}/libs/fmdita/clientlibs/xmleditor/page.html?src={path}&leftPanel=repository_panel&appMode=author"
        return tmpl.format(author=author, path=quote(mp, safe="/"), guid=a["guid"], map=a.get("map", ""))
    tmpl = cfg.get("link", "")
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
    wide = cfg.get("wide_search_root") or "/content/dam"  # when the product folder has no match
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
            # the file named after the GUID is that topic. Else every topic file that mentions the GUID is a
            # candidate - but maps and other topics mention it too (topicrefs, xrefs, conrefs), so a
            # candidate counts only when its own root element carries the GUID as its id
            named = query({"nodename": f"{guid}*"})
            if not named and wide and wide != root:
                named = query({"nodename": f"{guid}*", "path": wide})
            named = [h for h in named if re.search(r"\.(dita|xml)$", h, re.I)]
            cands = []
            if not named:
                for params in ({"fulltext": guid, "nodename": "*.dita"}, {"fulltext": guid, "nodename": "*.xml"},
                               {"fulltext": guid, "path": wide}):
                    cands += [h for h in query(params) if re.search(r"\.(dita|xml)$", h, re.I) and h not in cands]
                cands = [h for h in cands if _topic_id(author, auth, h) == guid]
        except HTTPError as e:
            return out, ("AEM refused the login (user name or password wrong)" if e.code in (401, 403)
                         else f"AEM search failed: HTTP {e.code}")
        except (URLError, OSError, ValueError) as e:
            return out, f"AEM not reachable: {e}"
        hits = named or cands
        if hits:
            hits.sort(key=lambda h: (f"/{lang}/" not in h if lang else False, len(h)))
            out[guid] = _PATHS[(author, guid)] = hits[0]
        else:
            err = err or f"{guid} not found in AEM under {root}"
    return out, err


def _topic_id(author: str, auth: str, path: str) -> str:
    """The id of a DITA file's root element (<topic id="GUID-…">, <concept …>, <task …>), read from the
    start of the file in AEM; "" when it cannot be read."""
    from urllib import request as _rq
    from urllib.parse import quote as _quote
    try:
        req = _rq.Request(f"{author}{_quote(path, safe='/')}/jcr:content/renditions/original",
                          headers={"Authorization": auth, "Range": "bytes=0-8191"})
        with _rq.urlopen(req, timeout=15) as r:
            head = r.read(8192).decode("utf-8", "replace")
    except Exception:
        return ""
    m = re.search(r"<(?![?!])[\w:-]+\b[^>]*?\sid\s*=\s*[\"']([^\"']+)[\"']", head)
    return m.group(1) if m else ""


def find_map(name: str, cfg: dict) -> str:
    """DAM path of the map file <name> (e.g. sl04_and_sh04.ditamap), asked from AEM; "" without a login."""
    import base64
    import json
    from urllib import request as _rq
    from urllib.parse import urlencode

    author = (cfg.get("author") or "").rstrip("/")
    if not name or not author or not cfg.get("user") or not cfg.get("password"):
        return ""
    if (author, name) in _PATHS:
        return _PATHS[(author, name)]
    auth = "Basic " + base64.b64encode(f"{cfg['user']}:{cfg['password']}".encode()).decode()
    q = {"path": cfg.get("search_root") or "/content/dam", "type": "dam:Asset", "nodename": name,
         "p.limit": "10", "p.hits": "selective", "p.properties": "jcr:path"}
    try:
        with _rq.urlopen(_rq.Request(f"{author}/bin/querybuilder.json?{urlencode(q)}",
                                     headers={"Authorization": auth}), timeout=15) as r:
            hits = [h["jcr:path"] for h in json.load(r).get("hits", [])]
    except Exception:
        return ""
    hits.sort(key=lambda h: ("/en/" not in h, len(h)))
    if hits:
        _PATHS[(author, name)] = hits[0]
    return hits[0] if hits else ""


_LANG_SUFFIX = re.compile(r"[-_ ](?:[a-z]{2}(?:[-_][a-z]{2})?)$", re.I)


def map_key(name: str) -> str:
    """A map name compared loosely: no .ditamap, no language suffix (-en, _EN, -zh-cn), case and
    punctuation ignored - “EW270Q-en.ditamap” and “ew270q.ditamap” are the same map."""
    stem = re.sub(r"\.ditamap$", "", (name or "").rsplit("/", 1)[-1], flags=re.I)
    stem = _LANG_SUFFIX.sub("", stem)
    return re.sub(r"[^a-z0-9]", "", stem.lower())


_ALL_MAPS: dict = {}


def all_maps(cfg: dict) -> list[str]:
    """Every .ditamap AEM has: under the search root first, then the whole DAM."""
    import base64
    import json
    from urllib import request as _rq
    from urllib.parse import urlencode
    author = (cfg.get("author") or "").rstrip("/")
    if not author or not cfg.get("user") or not cfg.get("password"):
        return []
    if author in _ALL_MAPS:
        return _ALL_MAPS[author]
    auth = "Basic " + base64.b64encode(f"{cfg['user']}:{cfg['password']}".encode()).decode()
    out: list[str] = []
    for root in dict.fromkeys([cfg.get("search_root") or "/content/dam", "/content/dam"]):
        q = {"path": root, "type": "dam:Asset", "nodename": "*.ditamap", "p.limit": "-1",
             "p.hits": "selective", "p.properties": "jcr:path"}
        try:
            with _rq.urlopen(_rq.Request(f"{author}/bin/querybuilder.json?{urlencode(q)}",
                                         headers={"Authorization": auth}), timeout=60) as r:
                out += [h["jcr:path"] for h in json.load(r).get("hits", [])]
        except Exception:
            continue
    _ALL_MAPS[author] = list(dict.fromkeys(out))
    return _ALL_MAPS[author]


def best_map(name: str, maps: list[str], prefer: str = "") -> str:
    """The map that is `name` by map_key; else one whose key contains it (or the other way round).
    Ties: under the search root, an English folder, then the shortest path."""
    want = map_key(name)
    if not want:
        return ""
    rank = lambda p: (not p.startswith(prefer) if prefer else False, "/en/" not in p.lower(), len(p))
    exact = sorted((p for p in maps if map_key(p) == want), key=rank)
    if exact:
        return exact[0]
    near = sorted((p for p in maps if len(want) >= 4 and (want in map_key(p) or (len(map_key(p)) >= 4 and map_key(p) in want))),
                  key=lambda p: (abs(len(map_key(p)) - len(want)),) + rank(p))
    return near[0] if near else ""


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
    found = find_map(a.get("map", ""), acfg)  # with a login: the map's real place in AEM
    maps = acfg.get("maps_folder", "Maps")
    if found and not a["folder"]:  # .../<product>/Maps/<map>.ditamap -> the product folder
        a["folder"] = re.sub(rf"/{re.escape(maps)}/[^/]+$", "", found) if f"/{maps}/" in found else found.rsplit("/", 1)[0]
    a["map_path"] = found or map_path({"map": a.get("map", ""), "folder": a["folder"]}, acfg)
    # topics: inside the product folder first, then anywhere under the configured search root
    scfg = {**acfg, "search_root": a["folder"], "wide_search_root": acfg.get("search_root") or "/content/dam"} \
        if a["folder"] else acfg
    paths, a["resolve_error"] = resolve([(t["guid"], t["lang"]) for t in a["topics"]], scfg)
    base = {"map": a.get("map", ""), "product": a["product"], "folder": a["folder"], "map_path": a["map_path"]}
    urls = {}
    for t in a["topics"]:
        # a clean lookup decides (an earlier, wrong file is dropped); without a login keep what the run had
        searched = bool(acfg.get("password")) and not a["resolve_error"]
        t["path"] = paths.get(t["guid"], "" if searched else t.get("path", ""))
        t["url"] = urls[t["guid"]] = url_for({**base, **t}, acfg)
        t["topic_url"] = url_for({**base, **t}, acfg, "topic") if t["path"] else ""
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


# ---------------------------------------------------------------- generate the stage PDF in AEM Guides

def preset_for(map_path: str, cfg: dict) -> str:
    """The Native PDF output preset for a map, from where the map lives: [aem.generate] rules =
    [[path part, preset], ...] (first match, case-insensitive), else the default preset."""
    g = cfg.get("generate") or {}
    for part, preset in g.get("rules", []):
        if part.lower() in (map_path or "").lower():
            return preset
    return g.get("default_preset", "BenQ with images")


def _key(title: str) -> str:
    """Preset titles compared loosely: case, spaces and a plural “s” do not matter (“BenQ with images” = “BenQ With Image”)."""
    k = re.sub(r"[^a-z0-9]", "", (title or "").lower())
    return k[:-1] if k.endswith("s") else k


def preset_id(title: str, cfg: dict) -> str:
    """The node name AEM Guides knows an output preset by (often a UUID), from its title as the map
    console shows it. The presets are read from the folder profiles under /var/dxml/folderprofiles."""
    import json
    from urllib import request as _rq
    from urllib.parse import urlencode
    author, auth = _auth(cfg)
    q = {"path": "/var/dxml/folderprofiles", "property": "fmdita-outputTitle", "property.operation": "exists",
         "p.limit": "-1", "p.hits": "selective", "p.properties": "jcr:path fmdita-outputTitle fmdita-outputType"}
    with _rq.urlopen(_rq.Request(f"{author}/bin/querybuilder.json?{urlencode(q)}", headers={"Authorization": auth}),
                     timeout=30) as r:
        hits = json.load(r).get("hits", [])
    for h in hits:
        if _key(h.get("fmdita-outputTitle", "")) == _key(title):
            return h["jcr:path"].rsplit("/", 1)[-1]
    pdf = sorted({h.get("fmdita-outputTitle", "") for h in hits if (h.get("fmdita-outputType") or "").lower() == "pdf"})
    raise RuntimeError(f"AEM has no output preset “{title}”. PDF presets in AEM: {', '.join(pdf) or 'none'} "
                       "- set the name in [aem.generate] of config/default.toml")


def _auth(cfg: dict) -> tuple[str, str]:
    import base64
    author = (cfg.get("author") or "").rstrip("/")
    if not author or not cfg.get("user") or not cfg.get("password"):
        raise RuntimeError("Log in to AEM first (author URL, user and password in the AEM login card)")
    return author, "Basic " + base64.b64encode(f"{cfg['user']}:{cfg['password']}".encode()).decode()


def resolve_map(name_or_path: str, cfg: dict) -> str:
    """A map's DAM path from its path or its file name (w2720i / w2720i.ditamap)."""
    s = (name_or_path or "").strip()
    if s.startswith("/content/"):
        return s
    name = s if s.lower().endswith(".ditamap") else f"{s}.ditamap"
    path = find_map(name, cfg) or best_map(name, all_maps(cfg), cfg.get("search_root") or "")
    if not path:
        raise RuntimeError(f"Map “{name}” not found in AEM: no map in any DAM folder is named like it "
                           f"(compared as “{map_key(name)}”)")
    return path


def generate_pdf(map_path: str, preset: str, dest: str, cfg: dict, progress=None) -> str:
    """Start the preset's Native PDF generation for the map (as “Generate” in the map console's Output
    tab), wait for the new PDF to appear in AEM, download it to dest. Returns dest.
    The request is [aem.generate] endpoint + params ({map}, {preset} filled in), so it can be adapted to
    the AEM Guides version without code changes."""
    import json
    import time
    from datetime import datetime, timezone
    from urllib import request as _rq
    from urllib.parse import urlencode

    say = progress or (lambda m: None)
    g = cfg.get("generate") or {}
    author, auth = _auth(cfg)
    started = datetime.now(timezone.utc)
    pid = preset_id(preset, cfg)
    params = {k: str(v).format(map=map_path, preset=pid, preset_title=preset) for k, v in
              (g.get("params") or {"operation": "GENERATEOUTPUT", "source": "{map}", "outputName": "{preset}"}).items()}
    # the parameters go in the query string: AEM Guides' publishlistener answers a form body with
    # “400 Request Data has already been read”
    req = _rq.Request(f"{author}{g.get('endpoint', '/bin/publishlistener')}?{urlencode(params)}", data=b"",
                      headers={"Authorization": auth}, method="POST")
    say(f"Generating “{preset}” for {map_path.rsplit('/', 1)[-1]} in AEM")
    from urllib.error import HTTPError
    try:
        with _rq.urlopen(req, timeout=60) as r:
            if r.status >= 400:
                raise RuntimeError(f"AEM refused the generation: HTTP {r.status}")
    except HTTPError as e:  # say what AEM said, not just "400 Bad Request"
        body = re.sub(r"<[^>]+>|\s+", " ", e.read().decode("utf-8", "replace")).strip()[:300]
        raise RuntimeError(f"AEM refused the generation of “{preset}” for {map_path.rsplit('/', 1)[-1]}: "
                           f"HTTP {e.code} {body}") from None
    # wait for a PDF of this map written after the start: the preset's output lands in the DAM
    stem = map_path.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
    root = g.get("output_root") or cfg.get("search_root") or "/content/dam"
    deadline = time.time() + float(g.get("timeout_s", 1200))
    while time.time() < deadline:
        time.sleep(float(g.get("poll_s", 10)))
        q = {"path": root, "type": "dam:Asset", "nodename": "*.pdf", "p.limit": "20", "p.hits": "full", "p.nodedepth": "2",
             "daterange.property": "jcr:content/jcr:lastModified",
             "daterange.lowerBound": started.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
             "orderby": "@jcr:content/jcr:lastModified", "orderby.sort": "desc"}
        with _rq.urlopen(_rq.Request(f"{author}/bin/querybuilder.json?{urlencode(q)}", headers={"Authorization": auth}),
                         timeout=30) as r:
            hits = [h.get("jcr:path", "") for h in json.load(r).get("hits", [])]
        # this map's PDF: named after the map or its preset, else the newest one written since the start
        mine = [h for h in hits if stem in h.lower()] or [h for h in hits if preset.lower().replace(" ", "") in h.lower().replace(" ", "")]
        if mine:
            with _rq.urlopen(_rq.Request(author + quote(mine[0], safe="/"), headers={"Authorization": auth}), timeout=120) as r, \
                    open(dest, "wb") as out:
                out.write(r.read())
            say(f"Downloaded {mine[0]}")
            return dest
        say(f"Waiting for AEM to finish “{preset}” ({int(deadline - time.time())} s left)")
    raise RuntimeError(f"AEM did not produce the PDF within {g.get('timeout_s', 1200)} s - check the map console's Output tab")


def map_candidates(filename: str) -> list[str]:
    """Map names a prod PDF's file name points to: “aeedd66f_W2720i_V1.03_EN.pdf” -> w2720i, …;
    “SL04&SH04_UM_V1.2_EN.pdf” -> sl04_and_sh04, sl04, sh04. Upload prefixes, versions, language and
    document-type words are dropped."""
    stem = re.sub(r"\.pdf$", "", filename.rsplit("/", 1)[-1], flags=re.I)
    stem = re.sub(r"^[0-9a-f]{8}_", "", stem)  # upload prefix
    words = [w for w in re.split(r"[_\s\-]+", stem) if w]
    noise = re.compile(r"^(v?\d+(\.\d+)*|en|eng|um|ug|qsg|user|manual|guide|final|draft|\(\d+\))$", re.I)
    keep = [w for w in words if not noise.match(w)]
    out = []
    if keep:
        out.append("_".join(keep).replace("&", "_and_").lower())
        out += [w.lower() for w in keep]
        for w in keep:
            out += [p.lower() for p in w.split("&") if p]
    return list(dict.fromkeys(x for x in out if len(x) >= 3))


def find_map_for(filename: str, cfg: dict) -> str:
    """The DAM path of the map a prod PDF belongs to, from the PDF's file name (see map_candidates):
    an exact map file name first, then a map whose name contains the product code."""
    import json
    from urllib import request as _rq
    from urllib.parse import urlencode
    author, auth = _auth(cfg)
    for name in map_candidates(filename):
        hit = find_map(f"{name}.ditamap", cfg) or best_map(name, all_maps(cfg), cfg.get("search_root") or "")
        if hit:
            return hit
    for name in map_candidates(filename):
        q = {"path": cfg.get("search_root") or "/content/dam", "type": "dam:Asset", "nodename": f"*{name}*.ditamap",
             "p.limit": "10", "p.hits": "selective", "p.properties": "jcr:path"}
        with _rq.urlopen(_rq.Request(f"{author}/bin/querybuilder.json?{urlencode(q)}", headers={"Authorization": auth}),
                         timeout=30) as r:
            hits = sorted((h["jcr:path"] for h in json.load(r).get("hits", [])), key=lambda h: ("/en/" not in h, len(h)))
        if hits:
            return hits[0]
    raise RuntimeError(f"No map found in AEM for “{filename}” (tried {', '.join(map_candidates(filename)) or 'nothing'})")


# ---------------------------------------------------------------- the map's AEM Sites output

def site_for_map(map_path: str, cfg: dict, lang: str = "") -> str:
    """The AEM Sites page published from a map: the language page every topic page of the map points
    back to (jcr:content/basePath = the map, indexPath = its site root). Several outputs of one map
    (a copy outside the brand tree, an old version folder) - the one under a brand site, in the map's
    language, with the most topic pages. Returns the author URL of that page (…/<lang>.html), or ""."""
    import json
    from collections import Counter
    from urllib import request as _rq
    from urllib.parse import urlencode
    author, auth = _auth(cfg)
    q = {"path": cfg.get("sites_root") or "/content/guide", "type": "cq:PageContent", "property": "basePath",
         "property.value": map_path, "p.limit": "-1", "p.hits": "selective", "p.properties": "indexPath"}

    def ask(q: dict) -> Counter:
        with _rq.urlopen(_rq.Request(f"{author}/bin/querybuilder.json?{urlencode(q)}", headers={"Authorization": auth}),
                         timeout=60) as r:
            return Counter(h.get("indexPath", "") for h in json.load(r).get("hits", []) if h.get("indexPath"))
    roots = ask(q)
    if not roots:  # published from a copy of the map in another DAM tree (/content/dam/hashout/…): same file name
        roots = ask({**q, "property.value": "%/" + map_path.rsplit("/", 1)[-1], "property.operation": "like"})
    if not roots:
        return ""
    lang = (lang or next((s for s in map_path.lower().split("/") if re.fullmatch(r"[a-z]{2}(-[a-z]{2})?", s)), "en")).lower()
    brands = set((cfg.get("site_brands") or "benq business consumer education zowie inftylab").split())

    def rank(path: str):
        seg = path.split("/")
        return (len(seg) > 3 and seg[3] in brands, seg[-1].lower() == lang, roots[path], -len(path))
    best = max(roots, key=rank)
    return f"{author}{best}.html"


_SITE_NOISE = re.compile(r"(?i)(^|[_\-\s.])(um|em|qsg|user\s*manual|instructions?|manual|marked|final|v\d+(\.\d+)*|en|eng)(?=$|[_\-\s.])")


def is_site_root(url: str) -> bool:
    """The URL names a folder of AEM Sites, not a guide page: the Sites console (…/sites.html/content/guide) or the
    folder itself (…/content/guide, …/content/guide/consumer)."""
    from urllib.parse import urlsplit
    path = urlsplit(url).path.rstrip("/")
    if "/sites.html" in path:
        return True
    last = path.rsplit("/", 1)[-1]
    return path.startswith("/content/") and "." not in last and not re.fullmatch(r"[a-z]{2}([-_][a-zA-Z]{2,4})?", last)


def site_for_pdf(pdf_path: str, root_url: str, cfg: dict, lang: str = "en") -> dict:
    """The guide of a prod PDF's product in AEM Sites, searched under the folder `root_url` names: every topic
    page there records the map it was published from (basePath) and its guide's language page (indexPath). The
    product is taken from the PDF's name and its folders (BL2291_UM-en.pdf in …/BL2291-EM-V0/ -> bl2291); the
    guide is the one whose map or site folder carries that name, in `lang`. Read-only (a query).
    The page is opened with ?wcmmode=disabled: the published view, without the author's editing frame (the crawl
    gives the same query to every page of the guide).
    Returns {"url": the language page (…/en.html?wcmmode=disabled) or "", "site", "map", "pages", "product", "candidates": [...]}."""
    import json
    from collections import Counter, defaultdict
    from pathlib import Path
    from urllib import request as _rq
    from urllib.parse import urlencode, urlsplit
    author, auth = _auth(cfg)
    root = urlsplit(root_url).path.rstrip("/")
    root = root.split("/sites.html", 1)[1] if "/sites.html" in root else root
    root = root or cfg.get("sites_root") or "/content/guide"
    q = {"path": root, "type": "cq:PageContent", "property": "basePath", "property.operation": "exists",
         "p.limit": "-1", "p.hits": "selective", "p.properties": "basePath indexPath"}
    with _rq.urlopen(_rq.Request(f"{author}/bin/querybuilder.json?{urlencode(q)}", headers={"Authorization": auth}), timeout=120) as r:
        hits = json.load(r).get("hits", [])
    guides: dict = defaultdict(Counter)  # language page -> maps it was published from (topic pages each)
    for h in hits:
        if h.get("basePath") and h.get("indexPath"):
            guides[h["indexPath"]][h["basePath"]] += 1
    norm = lambda t: re.sub(r"[^a-z0-9]", "", t.lower())
    clean = lambda t: norm(_SITE_NOISE.sub(" ", _SITE_NOISE.sub(" ", t)))
    pp = Path(pdf_path)
    # the PDF's own name first, then the folders it sits in (the product's archive folder)
    keys = list(dict.fromkeys(k for k in [clean(pp.stem)] + [clean(x) for x in list(pp.parts[:-1])[::-1][:3]] if len(k) >= 3))
    lang = (lang or "en").lower()
    scored = []
    for index, maps in guides.items():
        seg = index.strip("/").split("/")
        if seg[-1].lower() != lang:
            continue
        names = {norm(seg[-2]) if len(seg) > 1 else ""} | {norm(m.rsplit("/", 1)[-1].replace(".ditamap", "")) for m in maps}
        names.discard("")
        best = 0
        for rank, k in enumerate(keys):
            for n in names:
                sc = 100 if n == k else 80 if n.startswith(k) or k.startswith(n) and len(n) >= 4 else \
                    60 if (k in n or n in k) and min(len(k), len(n)) >= 4 else 0
                best = max(best, sc - 5 * rank)
        if best > 0:
            scored.append((best, sum(maps.values()), index, maps.most_common(1)[0][0]))
    scored.sort(key=lambda x: (-x[0], -x[1], len(x[2])))
    cands = [{"url": f"{author}{i}.html?wcmmode=disabled", "site": i, "map": m, "pages": n, "score": sc} for sc, n, i, m in scored[:5]]
    top = cands[0] if cands else {}
    if top:
        # the language page itself is an empty landing page (its title, no navigation): the guide starts at its
        # first topic page, which carries the left navigation the crawl follows to every other page
        try:
            with _rq.urlopen(_rq.Request(f"{author}{top['site']}.1.json", headers={"Authorization": auth}), timeout=60) as r:
                kids = [k for k, v in json.load(r).items() if isinstance(v, dict) and v.get("jcr:primaryType") == "cq:Page"]
            if kids:
                top["url"] = f"{author}{top['site']}/{kids[0]}.html?wcmmode=disabled"
                top["first_page"] = kids[0]
        except Exception:
            pass
    return {"url": top.get("url", ""), "site": top.get("site", ""), "map": top.get("map", ""), "pages": top.get("pages", 0),
            "product": keys[0] if keys else "", "root": root, "guides": len(guides), "candidates": cands}
