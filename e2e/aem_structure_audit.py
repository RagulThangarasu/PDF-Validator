"""AEM Guides structure audit: the Sites output under /content/guide against the DITA source in DAM.

Finds structure problems in the published Sites tree, explains why pages get names like
"-", "-0", "-1", screenshots every issue in the AEM author UI and writes one PDF report.

  PDFVAL_AEM_PASSWORD=... python e2e/aem_structure_audit.py
  python e2e/aem_structure_audit.py --author http://host:4502 --user ragul --out reports/aem-structure
  python e2e/aem_structure_audit.py --no-shots          # data + PDF only, no screenshots

The password comes from PDFVAL_AEM_PASSWORD (never from the command line). The user comes from
--user, PDFVAL_AEM_USER, or runs/aem-settings.json. Outputs <out>/<timestamp>/:
issues.json, shots/*.png, report.html and aem-structure-audit.pdf.
Exit code 1 when any issue of severity "high" is found.
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITES = "/content/guide"
DAM_ROOTS = ["/content/dam/benq-aem-guides", "/content/dam/hashout/benq-aem-guides-hashout",
             "/content/dam/hashout/benq-aem-guides"]
BRANDS = {"benq", "business", "consumer", "education", "zowie", "inftylab"}
SYSTEM = {"errors", "search"}
LANG = re.compile(r"^[a-z]{2}(-[a-z]{2})?$")
LANG_ANY = re.compile(r"^[a-z]{2}(-[a-z]{2})?$", re.I)
# a page name AEM had to invent: empty after dropping non-Latin letters ("-"), numbered clashes
# ("-0", "-12"), or a name that is only punctuation around a few Latin words ("-ncc-", "taiwan-")
GENERATED = re.compile(r"^-\d*$")
CLASH = re.compile(r"-\d+$")
PROPS = ["jcr:path", "jcr:content/jcr:title", "jcr:content/jcr:created", "jcr:content/cq:template",
         "jcr:content/basePath", "jcr:content/sourcePath", "jcr:content/indexPath",
         "jcr:content/siteTitle", "jcr:content/sitePath", "jcr:content/uuid", "jcr:content/preset",
         "jcr:content/jcr:createdBy", "jcr:content/cq:lastReplicationAction"]

SEV_ORDER = {"high": 0, "medium": 1, "low": 2}


# ---------------------------------------------------------------- AEM access
class Aem:
    def __init__(self, author: str, user: str, password: str):
        self.author = author.rstrip("/")
        self.user, self.password = user, password
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    def get(self, path: str, params: dict | None = None, timeout: int = 180):
        url = self.author + path + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, headers={"Authorization": self.auth})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)

    def query(self, **params) -> list[dict]:
        params = {"p.limit": "-1", "p.hits": "selective", **params}
        return self.get("/bin/querybuilder.json", params)["hits"]


def script_of(text: str) -> str:
    """The writing system most letters of a title use."""
    c = Counter()
    for ch in text or "":
        o = ord(ch)
        if ch.isascii():
            if ch.isalpha():
                c["Latin"] += 1
        elif 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF:
            c["CJK"] += 1
        elif 0x3040 <= o <= 0x30FF:
            c["Japanese kana"] += 1
        elif 0xAC00 <= o <= 0xD7AF:
            c["Korean"] += 1
        elif 0x0400 <= o <= 0x04FF:
            c["Cyrillic"] += 1
        elif 0x0600 <= o <= 0x06FF:
            c["Arabic"] += 1
        elif 0x0E00 <= o <= 0x0E7F:
            c["Thai"] += 1
        elif ch.isalpha():
            c["Latin"] += 1          # é, ç, ñ … are Latin with accents
    if not c:
        return "none"
    if c["Japanese kana"]:
        return "Japanese kana"
    return c.most_common(1)[0][0]


LATIN_LANGS = {"en", "fr", "de", "es", "it", "nl", "pl", "pt", "ro", "sv", "cs", "tr", "id", "vi", "lt"}
SCRIPT_LANGS = {"CJK": {"zh", "ja"}, "Japanese kana": {"ja"}, "Korean": {"ko"},
                "Cyrillic": {"ru", "uk"}, "Arabic": {"ar"}, "Thai": {"th"}, "Latin": LATIN_LANGS}


def lang_of_dam(path: str) -> str:
    for seg in (path or "").split("/")[3:]:
        if LANG_ANY.match(seg):
            return seg.lower()
    return ""


# ---------------------------------------------------------------- analysis
def collect(aem: Aem) -> dict:
    pages = aem.query(path=SITES, type="cq:Page", **{"p.properties": " ".join(PROPS)})
    maps = []
    for root in DAM_ROOTS:
        try:
            maps += [h["jcr:path"] for h in aem.query(path=root, type="dam:Asset", nodename="*.ditamap",
                                                      **{"p.properties": "jcr:path"})]
        except Exception as e:  # a root the user can't read, or too large to query
            print(f"  (skipped DAM root {root}: {e})", file=sys.stderr)
    return {"pages": pages, "maps": sorted(set(maps))}


def analyse(data: dict) -> list[dict]:
    pages = {}
    for h in data["pages"]:
        jc = h.get("jcr:content", {}) or {}
        p = h["jcr:path"]
        pages[p] = {"path": p, "name": p.rsplit("/", 1)[1], "parent": p.rsplit("/", 1)[0],
                    "depth": p.count("/"), "title": (jc.get("jcr:title") or "").strip(),
                    "created": jc.get("jcr:created", ""), "template": (jc.get("cq:template") or "").rsplit("/", 1)[-1],
                    "map": jc.get("basePath", ""), "source": jc.get("sourcePath", ""),
                    "index": jc.get("indexPath", ""), "preset": jc.get("preset", ""),
                    "by": jc.get("jcr:createdBy", ""), "live": jc.get("cq:lastReplicationAction") == "Activate"}
    kids = defaultdict(list)
    for p in pages.values():
        kids[p["parent"]].append(p)
    topics = [p for p in pages.values() if p["template"] == "topic-page"]
    issues: list[dict] = []

    def add(**kw):
        kw.setdefault("paths", [])
        kw.setdefault("rows", [])
        issues.append(kw)

    # 1. "-", "-0", "-1" … names made from non-Latin titles
    gen = defaultdict(list)
    for t in topics:
        if GENERATED.match(t["name"]):
            gen[t["parent"]].append(t)
    for parent, ts in sorted(gen.items(), key=lambda kv: -len(kv[1])):
        ts.sort(key=lambda t: (len(t["name"]), t["name"]))
        scripts = Counter(script_of(t["title"]) for t in ts)
        lang = parent.rsplit("/", 1)[1]
        add(category="Generated page names (-, -0, -1 …)", severity="high", key="names",
            title=f"{len(ts)} of {len(kids[parent])} pages in {parent} have no real name",
            where=parent, paths=[t["path"] for t in ts],
            detail=(f"Language node “{lang}”. Titles are written in {', '.join(f'{s} ({n})' for s, n in scripts.most_common())}. "
                    "Every one of these titles became an empty name “-”; the first page kept “-”, "
                    "every later one got the next free number (-0, -1, -2 …)."),
            rows=[[t["name"], t["title"], t["source"].rsplit("/", 1)[-1]] for t in ts],
            row_head=["Page name", "Topic title (source of the name)", "Topic file (a usable name)"])

    # 2. names cut to 50 characters, then numbered; names that are mostly stripped punctuation
    trunc, junk = [], []
    for t in topics:
        n = t["name"]
        if GENERATED.match(n):
            continue
        if len(n) >= 45 and CLASH.search(n):
            trunc.append(t)
        elif (n.startswith("-") or n.endswith("-") or "--" in n) and script_of(t["title"]) != "none":
            junk.append(t)
    if trunc:
        add(category="Truncated / clashing page names", severity="medium", key="trunc",
            title=f"{len(trunc)} page names cut at 50 characters and then numbered “-0”",
            where=trunc[0]["parent"], paths=[t["path"] for t in trunc],
            detail=("Long titles are cut to the 50-character name limit. Two titles that only differ after "
                    "character 50 (“…(for models with stand)” / “…(for models without stand)”) produce the same "
                    "name, so the second becomes “…-mode-0”. The URL no longer says which variant the page is."),
            rows=[[t["parent"].replace(SITES, "…"), t["name"], t["title"]] for t in trunc],
            row_head=["Language node", "Page name", "Title"])
    if junk:
        add(category="Page names with stripped characters", severity="low", key="junk",
            title=f"{len(junk)} page names lost characters (®, :, 警語, 废弃 …) and end or start with “-”",
            where=junk[0]["parent"], paths=[t["path"] for t in junk],
            detail=("Characters outside a–z 0–9 are replaced by “-”: “ClassroomCare®” → “classroomcare-”, "
                    "“NCC 警語” → “ncc-”, “Avertissements et précautions de sécurité” → "
                    "“avertissements-et-pr-cautions-de-s-curit-”. Not broken, but ugly and unstable URLs."),
            rows=[[t["parent"].replace(SITES, "…"), t["name"], t["title"]] for t in junk],
            row_head=["Language node", "Page name", "Title"])

    # 3. content language ≠ language node
    wrong = defaultdict(list)
    for t in topics:
        node = (t["index"] or t["parent"]).rsplit("/", 1)[1].lower()
        if not LANG.match(node):
            continue            # no language level at all: reported as a structure issue below
        base = node.split("-")[0].rstrip("0123456789")
        sc = script_of(t["title"])
        src_lang = lang_of_dam(t["source"])
        bad_script = sc in SCRIPT_LANGS and base and base not in SCRIPT_LANGS[sc] and sc != "Latin"
        bad_src = src_lang and src_lang.split("-")[0] != base
        if bad_script or bad_src:
            wrong[t["index"] or t["parent"]].append((t, sc, src_lang))
    for parent, ts in sorted(wrong.items(), key=lambda kv: -len(kv[1])):
        node = parent.rsplit("/", 1)[1]
        langs = Counter(s for _, s, _ in ts)
        srcs = Counter(src for _, _, src in ts if src)
        other_src = [l for l in srcs if l.split("-")[0] != node.split("-")[0]]
        cause = (f"Generated from the “{other_src[0]}” map: the “{node}” output was made without a translated map"
                 if other_src else
                 f"The “{srcs.most_common(1)[0][0] if srcs else node}” source topics themselves are written in "
                 f"{', '.join(langs)} — the source map mixes several languages")
        regulatory = bool(re.search(r"(^|[_-])rs([_-]|$)|regulatory|safety", parent.rsplit("/", 2)[-2], re.I))
        if regulatory and not other_src:
            cause = ("Regulatory / safety statement: country-specific warnings (Russian, French, Chinese …) are part "
                     "of the document by design — not a language error. Only their page names are broken (see “-0/-1”).")
        add(category="Content in the wrong language node", severity="low" if regulatory and not other_src else "high",
            key="lang", cause=cause,
            title=f"{len(ts)} pages in {parent.replace(SITES, '…')} are not “{node}” content",
            where=parent, paths=[t["path"] for t, _, _ in ts],
            detail=(f"The language node is “{node}”, but these topics are written in "
                    f"{', '.join(f'{s} ({n})' for s, n in langs.most_common())}"
                    " or come from another language's DAM folder. The source map references translated topics "
                    "directly (or several language copies are in one map), so the published “"
                    f"{node}” manual mixes languages."),
            rows=[[t["name"], t["title"][:70], src or "—", t["source"].rsplit("/", 1)[-1]] for t, _, src in ts],
            row_head=["Page name", "Title", "Source language", "Topic file"])

    # 4. language / product levels that don't follow brand/category/product/lang/topic
    for p in sorted(pages.values(), key=lambda p: p["path"]):
        segs = p["path"].split("/")[3:]
        if not segs or segs[0] not in BRANDS or (len(segs) > 1 and segs[-1] == "search"):
            continue            # orphan roots are one issue each (below); their depths don't apply
        if p["depth"] == 6 and p["template"] != "topic-page" and not LANG.match(p["name"]):
            add(category="Non-standard language node", severity="medium", key="langnode",
                title=f"Language node “{p['name']}” in {p['parent']}", where=p["parent"], paths=[p["path"]],
                detail=("Language nodes must be a lowercase locale code (en, zh-cn, fr-fr). "
                        + ("Upper case “En” makes a second English URL. " if p["name"].lower() in {"en"} else "")
                        + ("“en0” is a numbered clash: “en” already existed when this output was generated, "
                           "so AEM created “en0” instead of overwriting. " if re.match(r"^[a-z]{2}\d+$", p["name"]) else "")
                        + "Language switchers and hreflang links will not find this page."))
        if p["depth"] >= 7 and p["template"] != "topic-page" and LANG.match(p["parent"].rsplit("/", 1)[1]):
            add(category="Extra page inside a language node", severity="medium", key="extra",
                title=f"“{p['name']}” ({len(kids[p['path']])} topic pages) inside {p['parent']}",
                where=p["parent"], paths=[p["path"]],
                detail=("A second map or a test output was generated into an existing manual's language node, so "
                        "its topics appear inside that manual's navigation."),
                rows=[[c["name"], c["title"], c["source"].rsplit("/", 1)[-1]] for c in kids[p["path"]]],
                row_head=["Page", "Title", "Topic file"])
        if p["depth"] == 5 and p["template"] != "topic-page":
            ch = kids[p["path"]]
            if ch and all(c["template"] == "topic-page" for c in ch):
                add(category="Missing language level", severity="medium", key="nolang",
                    title=f"{p['path']} has topics directly under the product, no language node",
                    where=p["path"], paths=[p["path"]],
                    detail=f"{len(ch)} topic pages sit at /product/<topic> instead of /product/<lang>/<topic>.")
            elif ch and LANG.match(p["name"]):
                add(category="Language and product swapped", severity="medium", key="swap",
                    title=f"{p['path']}: product under a language node",
                    where=p["path"], paths=[p["path"]] + [c["path"] for c in ch],
                    detail="The path is …/<lang>/<product> instead of …/<product>/<lang>.")
            if re.match(r"^\d+-", p["name"]):
                add(category="Product named after a map file", severity="medium", key="mapname",
                    title=f"Product page “{p['name']}” in {p['parent']}", where=p["parent"], paths=[p["path"]],
                    detail=("The product node is named after the map file (1-en.ditamap), not the product. "
                            "The map file name should be the product (cr21_screenbar-pro)."))

    # 5. sites outside a brand/category tree
    for p in sorted(pages.values(), key=lambda p: p["path"]):
        if p["depth"] == 3 and p["name"] not in BRANDS | SYSTEM:
            sub = [q for q in pages if q.startswith(p["path"] + "/")]
            add(category="Output outside the brand/category tree", severity="high", key="orphan",
                title=f"{p['path']} is a separate root ({len(sub)} pages)", where=p["path"], paths=[p["path"]],
                detail=("Published with an output path of /content/guide/<name> instead of "
                        "/content/guide/<brand>/<category>/<product>. These pages are outside every brand site, "
                        "so they are missing from the brand navigation and search, and duplicate the copy "
                        "published in the brand tree."),
                rows=[[q.replace(p["path"], "") or "/"] for q in sorted(sub)[:12]], row_head=["Pages below"])

    # 6. one map published to several places
    by_map = defaultdict(set)
    for t in topics:
        if t["map"] and t["index"]:
            by_map[t["map"]].add(t["index"])
    dups = {m: sorted(s) for m, s in by_map.items() if len(s) > 1}
    if dups:
        add(category="Same map published to several paths", severity="high", key="dup",
            title=f"{len(dups)} maps are published to more than one Sites path",
            where=sorted(dups.values(), key=len)[-1][0].rsplit("/", 1)[0], paths=[p for s in dups.values() for p in s],
            detail=("Each map should have one output. The copies are old outputs left behind after the output "
                    "path, preset or site changed, or the map was published once to a brand site and once to "
                    "another. Readers and search see two versions that drift apart."),
            rows=[[m.split("/")[-1], "\n".join(s)] for m, s in sorted(dups.items())],
            row_head=["Map", "Published to"])

    # 7. version / file name in the product node
    ver = [p for p in pages.values() if p["depth"] == 5 and p["template"] != "topic-page"
           and (re.search(r"_?(en|um)_?v\d|env\d|umv\d|_v\d+-\d+|\.ditamap$", p["name"] + " " + p["title"], re.I))]
    if ver:
        twins = []
        for p in sorted(ver, key=lambda p: p["path"]):
            base = re.split(r"[_-](?:um[_-]?)?(?:en[_-]?)?v\d|env\d|umv\d|_um_|_en_", p["name"], flags=re.I)[0]
            sib = [q["name"] for q in kids[p["parent"]] if q["name"] != p["name"] and q["name"].lower().startswith(base.lower())]
            twins.append([p["parent"].replace(SITES, "…"), p["name"], p["title"], ", ".join(sib) or "—"])
        add(category="Version / file names in product URLs", severity="medium", key="ver",
            title=f"{len(ver)} product nodes carry a document version or file name",
            where=ver[0]["parent"], paths=[p["path"] for p in ver],
            detail=("Product nodes such as “w5850_en_v1-01” or titled “color-shuttle_en_v1-00.ditamap” come from the "
                    "PDF/map file name. Each new version creates a new URL next to the old one "
                    "(w5850 and w5850_en_v1-01), links to the old version stay live, and “.” is turned into “-”."),
            rows=twins, row_head=["Category", "Product node", "Title", "Other node for the same product"])

    # 8. Sites category ≠ DAM category for the same map
    moved = []
    for m, idx in by_map.items():
        segs = [s.lower() for s in m.split("/")]
        try:
            i = next(k for k, s in enumerate(segs) if LANG_ANY.match(s) and k > 3)
        except StopIteration:
            continue
        dam_brand, dam_cat = (segs[i + 1:i + 3] + ["", ""])[:2]
        for ix in idx:
            s = ix.split("/")
            if len(s) < 7:
                continue
            site_brand, site_cat = s[3].lower(), s[4].lower()
            norm = lambda x: re.sub(r"[^a-z]", "", x).replace("projactor", "projector").replace("benqconsumer", "benq")
            if norm(dam_cat) != norm(site_cat) or (norm(dam_brand) != norm(site_brand)
                                                    and not (dam_brand == "consumer" and site_brand == "benq")):
                moved.append([m.split("/benq-aem-guides")[-1], ix.replace(SITES, "…")])
    if moved:
        add(category="Sites category differs from DAM folder", severity="low", key="moved",
            title=f"{len(moved)} outputs sit in a different brand/category than their source map",
            where=SITES, paths=[r[1] for r in moved],
            detail=("The DAM folder says one brand/category, the Sites path another (DAM Consumer/Monitor-arm/bsh → "
                    "Sites benq/monitor/bsh). Keep one taxonomy so the output path can be derived from the DAM path."),
            rows=sorted(moved), row_head=["Source map (DAM)", "Published to (Sites)"])

    # 9. DAM hygiene and coverage
    maps = data["maps"]
    published = set(by_map)
    src = [m for m in maps if m.startswith("/content/dam/benq-aem-guides/")]
    unpub = [m for m in src if m not in published]
    if unpub:
        add(category="DAM maps with no Sites output", severity="low", key="unpub",
            title=f"{len(unpub)} of {len(src)} maps in /content/dam/benq-aem-guides have no Sites output",
            where="/content/dam/benq-aem-guides/en", paths=unpub, dam=True,
            detail=("No topic page in /content/guide points back to these maps (basePath). Either they are not "
                    "published yet, or their Sites output was generated from a copy in another DAM root "
                    "(/content/dam/hashout/…), so DAM and Sites are out of step."),
            rows=[[m.replace("/content/dam/benq-aem-guides", "…")] for m in unpub], row_head=["Map"])
    roots = Counter(next((r for r in sorted(DAM_ROOTS, key=len, reverse=True) if m.startswith(r + "/")), "?")
                    for m in published)
    if len(roots) > 1:
        add(category="Sites built from several DAM roots", severity="medium", key="roots",
            title=f"Published maps come from {len(roots)} DAM roots", where="/content/dam", dam=True,
            detail=("The same products exist in more than one DAM tree and outputs were generated from each. "
                    "Pick one source of truth (/content/dam/benq-aem-guides) and move translations into its "
                    "language folders."),
            rows=[[r, str(n)] for r, n in roots.most_common()], row_head=["DAM root", "Maps published"])
    hyg = []
    for m in src:
        segs = m.split("/")
        folder, name = segs[-3], segs[-1][:-len(".ditamap")]
        if "projactor" in m.lower():
            hyg.append([m, "Typo in folder name “Projactor-Accessory”"])
        if re.search(r"/test", m, re.I):
            hyg.append([m, "Test folder inside the production DITA tree"])
        if re.sub(r"[^a-z0-9]", "", folder.lower()) not in re.sub(r"[^a-z0-9]", "", name.lower()) and \
           re.sub(r"[^a-z0-9]", "", name.lower()) not in re.sub(r"[^a-z0-9]", "", folder.lower()):
            hyg.append([m, f"Map “{name}” does not match its product folder “{folder}”"])
        if name.endswith("-") or name[:1].isdigit():
            hyg.append([m, f"Map file name “{name}” is not a product name"])
    cats = defaultdict(set)
    for m in src:
        segs = m.split("/")
        if len(segs) > 6:
            cats[segs[6].lower()].add(segs[6])
    for low, vs in cats.items():
        if len(vs) > 1:
            hyg.append([", ".join(sorted(vs)), "Same category folder spelled with different case"])
    for folder in sorted({f"/content/dam/benq-aem-guides/{l}" for l in ("ar-me",)}):
        if any(m.startswith(folder + "/BenQ-consumer") for m in src) or any(m.startswith(folder + "/BenQ/") for m in src):
            hyg.append([folder, "ar-me uses brand folders (BenQ, BenQ-consumer) that en does not have"])
    if hyg:
        add(category="DAM folder and map naming", severity="low", key="hyg",
            title=f"{len(hyg)} naming problems in the DITA source tree", where="/content/dam/benq-aem-guides/en", dam=True,
            paths=[r[0] for r in hyg],
            detail="Folder and map names feed the output path and the product node name, so they need to be clean.",
            rows=[[r[0].replace("/content/dam/benq-aem-guides", "…"), r[1]] for r in hyg], row_head=["Path", "Problem"])

    presets = Counter(t["preset"] for t in topics if t["preset"])
    if len(presets) > 1:
        by_preset = defaultdict(set)
        for t in topics:
            if t["preset"] and t["index"]:
                by_preset[t["preset"]].add(t["index"].split("/")[3])
        add(category="Several output presets write into the same site", severity="high", key="presets",
            title=f"{len(presets)} different output presets generated /content/guide", where=SITES,
            detail="Every preset has its own output path and naming settings, so the same kind of map lands in different places.",
            rows=[[p, str(n), ", ".join(sorted(by_preset[p]))] for p, n in presets.most_common()],
            row_head=["Output preset", "Topic pages", "Top-level folders it wrote to"])
    unlive = [t for t in topics if not t["live"]]
    for i in issues:                       # who / which preset / when produced each finding
        pre, by, when = Counter(), Counter(), []
        for p in i["paths"]:
            for t in topics:
                if t["path"] == p or t["path"].startswith(p + "/") or t["index"] == p:
                    pre[t["preset"]] += 1
                    by[t["by"]] += 1
                    when.append(t["created"][4:15])
        i["origin"] = (", ".join(k for k in pre if k) + (f" · by {', '.join(k for k in by if k)}" if by else "")
                       + (f" · {min(when)}" if when else "")) if pre else ""
    issues.append({"key": "_stats", "unlive": len(unlive), "topics": len(topics), "presets": dict(presets),
                   "products": len({t["index"].rsplit("/", 1)[0] for t in topics if t["index"]}),
                   "multilang": sum(1 for v in _langs_per_product(topics).values() if len(v) > 1),
                   "langnodes": dict(Counter(t["index"].rsplit("/", 1)[1] for t in topics if t["index"]).most_common()),
                   "category": "", "severity": "low", "paths": []})
    return issues


def _langs_per_product(topics: list[dict]) -> dict:
    out = defaultdict(set)
    for t in topics:
        if t["index"]:
            out[t["index"].rsplit("/", 1)[0]].add(t["index"].rsplit("/", 1)[1])
    return out


def merge_small(issues: list[dict], keys=("names", "lang"), keep: int = 3) -> list[dict]:
    """Groups of fewer than `keep` pages are folded into one issue per type, each row naming its node."""
    out = []
    for key in keys:
        small = [i for i in issues if i["key"] == key and len(i["paths"]) < keep]
        if len(small) < 2:
            continue
        for i in small:
            issues.remove(i)
        first = small[0]
        out.append({**first, "merged": True, "title": f"{sum(len(i['paths']) for i in small)} more pages in {len(small)} other language nodes",
                    "paths": [p for i in small for p in i["paths"]],
                    "rows": [[i["where"].replace(SITES, "…")] + r for i in small for r in i["rows"]],
                    "row_head": ["Language node"] + first["row_head"],
                    "detail": first["detail"].split(". ", 1)[-1] if key == "names" else
                              "Same cause as above, in smaller numbers: topics whose script or source folder is "
                              "not the language of the node they are published under."})
    return issues + out


def summary(data: dict, issues: list[dict]) -> dict:
    pages = data["pages"]
    tp = [h for h in pages if (h.get("jcr:content") or {}).get("cq:template", "").endswith("topic-page")]
    gen = [h for h in tp if GENERATED.match(h["jcr:path"].rsplit("/", 1)[1])]
    langs = Counter(((h.get("jcr:content") or {}).get("indexPath") or "").rsplit("/", 1)[-1] for h in tp)
    return {"pages": len(pages), "topics": len(tp), "generated": len(gen), "maps": len(data["maps"]),
            "languages": dict(langs.most_common()), "issues": len(issues),
            "by_severity": dict(Counter(i["severity"] for i in issues))}


# ---------------------------------------------------------------- screenshots
def screenshots(aem: Aem, issues: list[dict], out: Path) -> None:
    from playwright.sync_api import sync_playwright
    shots = out / "shots"
    shots.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000}, device_scale_factor=1)
        page = ctx.new_page()
        page.goto(aem.author + "/libs/granite/core/content/login.html")
        page.fill("#username", aem.user)
        page.fill("#password", aem.password)
        page.click("#submit-button")
        page.wait_for_load_state("networkidle")

        def shot(url: str, name: str, select: str | None = None, banner: str | None = None) -> str | None:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(3500)
                if select:           # highlight the offending item in the console list
                    page.evaluate("""n => { for (const el of document.querySelectorAll('[data-foundation-collection-item-id]'))
                        if (el.getAttribute('data-foundation-collection-item-id').endsWith('/' + n)) {
                          el.style.outline = '3px solid #d7263d'; el.scrollIntoView({block: 'center'}); } }""", select)
                    page.wait_for_timeout(400)
                if banner:           # the reader's URL, where the generated name shows
                    page.evaluate("""t => { const d = document.createElement('div'); d.textContent = t;
                        d.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;background:#d7263d;' +
                          'color:#fff;font:600 15px/1.2 Menlo,monospace;padding:8px 14px';
                        document.body.appendChild(d); }""", banner)
                f = shots / f"{name}.png"
                page.screenshot(path=str(f))
                return f"shots/{f.name}"
            except Exception as e:
                print(f"  screenshot failed {url}: {e}", file=sys.stderr)
                return None

        for i in issues:
            i["shots"] = []
            if i.get("shot_level", 0) == 0:
                continue
            console = "/assets.html" if i.get("dam") else "/sites.html"
            where = i["where"]
            if i.get("dam") and i["paths"] and i["paths"][0].startswith("/content/dam/"):
                where = i["paths"][0].split("/Maps/")[0].rsplit("/", 1)[0]
            first = i["paths"][0] if i["paths"] else None
            # the console list around the problem, with the item outlined
            if i["key"] in {"orphan", "langnode", "mapname", "ver", "dup", "moved", "nolang", "swap"} and first:
                s = shot(f"{aem.author}{console}{first.rsplit('/', 1)[0]}", f"{i['id']}-console", first.rsplit("/", 1)[1])
            else:
                pick = first.split("/Maps/")[0].rsplit("/", 1)[1] if first and i.get("dam") else (first.rsplit("/", 1)[1] if first else None)
                s = shot(f"{aem.author}{console}{where}", f"{i['id']}-console", pick)
            if s:
                i["shots"].append({"src": s, "caption": f"AEM {'Assets' if i.get('dam') else 'Sites'} console: {where if i['key'] not in {'orphan'} else SITES}"})
            # the page itself, as a reader sees it (disabled edit mode)
            if i.get("shot_level") == 2 and not i.get("dam") and first and i["key"] in {"names", "lang", "trunc", "dup", "orphan"}:
                target = i["paths"][1] if i["key"] == "dup" and len(i["paths"]) > 1 else first
                s = shot(f"{aem.author}{target}.html?wcmmode=disabled", f"{i['id']}-page", banner=f"URL: {target}.html")
                if s:
                    i["shots"].append({"src": s, "caption": f"Published page {target}.html"})
        browser.close()


# ---------------------------------------------------------------- report
# One section per issue type. For each: what was found, why it happens (with the evidence read from
# AEM), what it breaks, how to fix it, and the reviewer's comment. Instances are rows, never sections.
KB = {
    "presets": dict(
        why="The Sites output under /content/guide was not produced by one output preset but by several, some of "
            "them clearly test presets (testsites, testaemsiteho, bq). Every preset carries its own output path, "
            "page-naming rule and language setting. Whoever generated a map picked a preset, and the output landed "
            "wherever that preset pointed: sometimes under brand/category, sometimes straight under /content/guide, "
            "sometimes into a folder that already held another version.",
        impact="This is the root cause behind most other findings: stray roots, duplicate outputs, version names in "
               "URLs and inconsistent language folders all trace back to which preset was used.",
        fix="Keep one production Sites preset per brand (or one global preset with the brand/category in the output "
            "path), lock it in the map template, and delete the test presets and everything they generated.",
        comment="Fix this first. Cleaning pages without consolidating presets will reproduce the same problems on the "
                "next generation."),
    "names": dict(
        why="AEM Guides names each Sites page after the topic <b>title</b>. The title passes through AEM's page-name "
            "filter, which keeps only a–z, 0–9, “_” and “-”, lower-cases, and turns everything else into “-”. A title "
            "written in Chinese, Japanese, Russian or Arabic has no Latin letter left, so the name is empty and becomes "
            "“-”. The first topic of the map gets “-”; every next one clashes with an existing name and AEM appends the "
            "next free number: “-0”, “-1”, “-2” … That is why it happens <b>only in those languages</b> — English, French "
            "or Dutch titles keep most of their letters and rarely clash.",
        impact="URLs carry no meaning; they depend on topic order, so inserting or moving one topic renumbers every "
               "later page and breaks bookmarks, search-engine links and analytics. The language switcher cannot "
               "match /zh-cn/-3 to its English twin.",
        fix="Switch the preset's page naming from the title to the topic file name (the file names are already clean "
            "ASCII, e.g. i800_i800ST_UM_V1.02_ZH-CN-2.dita), ideally language-neutral file names "
            "(product-support.dita) so every language gets the same slug. Then delete and regenerate the affected "
            "language nodes — renaming the preset does not rename existing pages.",
        comment="Confirmed from the page properties: the name is derived from jcr:title, and sourcePath shows the "
                "ASCII file name that should have been used."),
    "trunc": dict(
        why="The same filter also cuts names at 50 characters. Two long titles that differ only after character 50 "
            "(“…(for models with stand)” / “…(for models without stand)”) produce the same name, so the second gets "
            "“-0” appended to its cut name.",
        impact="The URL no longer tells the variants apart, and which variant owns the “-0” depends on topic order.",
        fix="Solved by the same file-name setting; otherwise shorten the titles or set a navtitle / short title.",
        comment="Same mechanism as “-0/-1”, in English."),
    "junk": dict(
        why="Characters the filter does not keep (®, :, accents, CJK words inside mixed titles) become “-”, leaving "
            "names such as “classroomcare-”, “ncc-” or “avertissements-et-pr-cautions-de-s-curit-”.",
        impact="Not broken, but the URLs look damaged and change whenever a title is edited.",
        fix="Solved by naming pages after the file name.", comment="Low priority; fixed for free by the naming change."),
    "lang": dict(
        why="Two different causes were found, shown per row in the “Cause” column: (1) the output was generated into a "
            "language node (ru-ru, ar-me, zh-tw) from the <b>English</b> map — the preset's language was set, but no "
            "translated map existed, so English topics were published as Russian/Arabic; (2) the English source map "
            "itself contains topics in other languages (the source was imported from a multilingual PDF, so zh-tw, "
            "zh-cn and ja chapters are topics inside the “en” map). Regulatory / safety statement documents (RS) are the "
            "exception: their country-specific warnings are multilingual on purpose — those rows are marked low.",
        impact="Readers of the Russian or Arabic site get English, and English readers get Chinese and Japanese "
               "chapters in the navigation. Search and language switching mix languages.",
        fix="A language node must be generated only from the map of that language. Split multilingual source maps into "
            "one map per language (each in its language folder in DAM), use AEM translation (language copies) for "
            "ru-ru / ar-me, and delete the outputs listed here.",
        comment="The non-Latin topics in /en are also what produce the “-”, “-0” names there."),
    "orphan": dict(
        why="These outputs were generated with a preset whose output path is /content/guide itself (preset "
            "“benqaemsites”), so the map name became a top-level folder instead of "
            "/content/guide/&lt;brand&gt;/&lt;category&gt;/&lt;product&gt;. Most of these products also have an output in "
            "the brand tree.",
        impact="Duplicate content outside the brand navigation and search; none of these pages are published, so "
               "they are leftovers.",
        fix="Delete these roots after confirming the brand-tree copy is the one to keep.",
        comment="All six were created with the benqaemsites preset (see the “Origin” column)."),
    "dup": dict(
        why="The same map was generated more than once with different presets or output paths (e.g. w5850 and "
            "w5850_en_v1-01, created a month apart by different users). The second generation does not replace the "
            "first because it writes to another path.",
        impact="Two live copies drift apart; readers and search find the stale one.",
        fix="Keep one output per map (the brand-tree path without version), delete the others, add redirects for any "
            "that were published.",
        comment="Several pairs are both published (Activate), which is the risky case."),
    "ver": dict(
        why="The product node was named after the PDF / map file name (“W5850_EN_V1.01” → “w5850_en_v1-01”, the dot "
            "becomes “-”). Each new document version then creates a new product folder next to the old one.",
        impact="Version numbers in URLs, old versions stay reachable, duplicates grow with every release.",
        fix="Name the product node after the product (w5850) in the output path; keep the version in metadata.",
        comment="Most of these have a twin without the version, listed in the last column."),
    "langnode": dict(
        why="The language node name comes from the preset/site language. “En” was typed by hand; “en0” is AEM's "
            "clash numbering again — a second map (regulatory.ditamap) was generated into the same product folder, "
            "“en” already existed, so AEM created “en0”; “pd05u” is a product name used where a language code "
            "belongs.",
        impact="Language switcher, hreflang and dispatcher rules keyed on locale codes miss these pages.",
        fix="Use lowercase locale codes only; publish a second map of the same product into its own product folder.",
        comment=""),
    "nolang": dict(
        why="The preset's output path ended at the product, without a language level, so topics sit directly under "
            "the product.",
        impact="No place for another language; inconsistent with the other 150+ products.", fix="Regenerate with the standard path.",
        comment=""),
    "swap": dict(
        why="Output path was entered as …/monitor/en/&lt;product&gt; instead of …/monitor/&lt;product&gt;/en.",
        impact="The product appears as a language, and an “en” category shows up in the monitor navigation.",
        fix="Regenerate with the standard path and delete this node.", comment=""),
    "extra": dict(
        why="A second map (named “test”) was generated into an existing manual's language node.",
        impact="Its topics show up inside the pdp_rs_classa manual's navigation.", fix="Delete the node.", comment=""),
    "mapname": dict(
        why="The product folder is named after the map file “1-en.ditamap”, not the product.",
        impact="Meaningless URL; two different products share the name “1-en”.",
        fix="Rename the map to the product (cr21_screenbar-pro) and regenerate.", comment=""),
    "moved": dict(
        why="The DAM folder and the Sites path use different brand/category names for the same map (DAM "
            "Consumer/Monitor-arm/bsh → Sites benq/monitor/bsh), because the output path is typed per preset "
            "instead of being derived from the DAM path.",
        impact="Nobody can predict where a map's output lives; audits and cleanups miss pages.",
        fix="Align the two taxonomies and derive the output path from the DAM folder.", comment=""),
    "roots": dict(
        why="Maps were published from three DAM trees: /content/dam/benq-aem-guides, and two copies under "
            "/content/dam/hashout. Most live pages come from the hashout copy.",
        impact="Edits in the “official” tree do not reach the site; the three copies disagree.",
        fix="Pick one DAM root as the source of truth and archive the others.", comment=""),
    "unpub": dict(
        why="No Sites page points back to these maps (basePath). Their pages were generated from the hashout copy "
            "instead, or were never generated.",
        impact="The DAM tree the team edits is not what the site shows.", fix="Resolved by choosing one DAM root.",
        comment=""),
    "hyg": dict(
        why="Folder and map names were created by hand without a convention.",
        impact="They feed the product node names and output paths.",
        fix="One spelling per folder (Projactor → Projector, one case), no test folders in production, map named after "
            "the product.", comment=""),
}


def consolidate(issues: list[dict]) -> list[dict]:
    groups: dict[str, dict] = {}
    for i in issues:
        g = groups.setdefault(i["key"], {"key": i["key"], "category": i["category"], "severity": i["severity"],
                                         "items": [], "dam": i.get("dam", False)})
        if SEV_ORDER[i["severity"]] < SEV_ORDER[g["severity"]]:
            g["severity"] = i["severity"]
        g["items"].append(i)
    out = sorted(groups.values(), key=lambda g: (g["key"] != "presets", SEV_ORDER[g["severity"]],
                                                 -sum(len(i["paths"]) or 1 for i in g["items"])))
    for n, g in enumerate(out, 1):
        g["id"] = f"T{n:02d}"
        g["items"].sort(key=lambda i: -len(i["paths"]))
        for k, i in enumerate(g["items"]):
            i["id"] = f"{g['id']}-{k + 1}"
            i["shot_level"] = 2 if k == 0 else (1 if k == 1 else 0)   # 2 examples per type, never more
        g["pages"] = sum(len(i["paths"]) for i in g["items"])
    return out


def _examples(i: dict, n: int = 4) -> str:
    rows = i.get("rows") or []
    if i["key"] in {"names", "trunc", "junk"}:
        return "; ".join(f"{r[1][:28]} → {r[0]}" for r in rows[:n])
    if i["key"] == "lang":
        return "; ".join(f"{r[1][:30]} ({r[0]})" for r in rows[:n])
    return "; ".join(" · ".join(str(c) for c in r)[:90] for r in rows[:n])


def language_section(stats: dict) -> str:
    ln = stats["langnodes"]
    real = {k: v for k, v in ln.items() if LANG.match(k)}
    return f"""
<h2>Language folders inside each product — is it the right approach?</h2>
<p>Today every map output is its own small site: <code>/content/guide/&lt;brand&gt;/&lt;category&gt;/&lt;product&gt;/<b>&lt;lang&gt;</b>/&lt;topic&gt;</code>.
Of <b>{stats['products']}</b> product outputs, <b>{stats['multilang']}</b> have more than one language; language nodes in use:
{', '.join(f'{k} ({v})' for k, v in real.items())}.</p>
<table class="t"><tr><th></th><th>Language under the product (current)</th><th>Language root at the top (AEM standard)</th></tr>
<tr><td>Path</td><td>/guide/benq/projector/i800/<b>zh-cn</b>/support</td><td>/guide/<b>zh-cn</b>/benq/projector/i800/support</td></tr>
<tr><td>Fits AEM Guides</td><td>Yes — each map's Sites output is self-contained; this is what the preset produces by default.</td><td>Yes — set the preset output path per language.</td></tr>
<tr><td>Brand, category, search pages</td><td>Exist once, in one language only (benq/projector, benq/search).</td><td>Exist per language, so the whole site can be localised.</td></tr>
<tr><td>AEM translation / language copies, MSM</td><td>Not usable — AEM expects language roots.</td><td>Works out of the box.</td></tr>
<tr><td>Language switcher / hreflang</td><td>Needs custom code per product; breaks when names differ (“-3” vs “product-support”).</td><td>Standard: same relative path under each language root.</td></tr>
<tr><td>Dispatcher / CDN / analytics rules per language</td><td>One rule per product.</td><td>One rule per language prefix.</td></tr>
</table>
<div class="comment"><b>Comment.</b> Keeping the language inside the product folder is workable for a documentation portal that is
almost entirely English (today only {stats['multilang']} products have a second language), and it is how the current
site and its language switcher are built. It is <b>not</b> the right long-term approach once more languages are added:
category and search pages cannot be translated, AEM translation projects cannot be used, and a language switcher only
works if page names are identical across languages — which the title-based naming breaks today. Recommendation:
(1) immediately, switch page names to file names and keep one locale code per product; (2) before rolling out more
languages, move to language roots (<code>/content/guide/&lt;lang&gt;/&lt;brand&gt;/…</code>) through the preset output path.</div>"""


def build_html(groups: list[dict], stats: dict, summ: dict, author: str, stamp: str) -> str:
    e = html.escape
    sev_col = {"high": "#c62828", "medium": "#ef6c00", "low": "#546e7a"}
    high = sum(g["severity"] == "high" for g in groups)
    parts = [f"""<!doctype html><html><head><meta charset="utf-8"><title>AEM Guides structure audit</title>
<style>
@page {{ size: A4; }}
body {{ font: 9.5pt/1.45 -apple-system, "Helvetica Neue", Arial, "PingFang SC", "Hiragino Sans", sans-serif; color:#1d2330; }}
h1 {{ font-size: 22pt; margin: 0 0 4pt; color:#0b3d91 }} h2 {{ font-size: 13.5pt; color:#0b3d91; border-bottom:2px solid #0b3d91; padding-bottom:3pt; margin-top:16pt }}
h3 {{ font-size: 13pt; margin: 0 0 6pt; color:#0b3d91 }} h4 {{ font-size:10pt; margin:9pt 0 2pt; color:#344054 }}
code {{ background:#eef1f6; padding:0 3px; border-radius:3px; font-size:8.5pt }} p {{ margin:3pt 0 }}
.muted {{ color:#667085 }} .kpis {{ display:flex; gap:8pt; margin:12pt 0 }}
.kpi {{ flex:1; border:1px solid #d0d7e2; border-radius:6px; padding:7pt }} .kpi b {{ display:block; font-size:17pt; color:#0b3d91 }}
table.t {{ border-collapse: collapse; width:100%; margin:5pt 0; font-size:8pt; }}
table.t th, table.t td {{ border:1px solid #d0d7e2; padding:3pt 5pt; text-align:left; vertical-align:top; word-break: break-word }}
table.t th {{ background:#eef1f6 }}
.type {{ page-break-before: always }} .sev {{ color:#fff; padding:1pt 6pt; border-radius:8px; font-size:7.5pt; text-transform:uppercase; vertical-align:middle }}
figure {{ margin:6pt 0; page-break-inside: avoid }} figure img {{ width:100%; border:1px solid #c9d1dd }}
figcaption {{ font-size:7.5pt; color:#667085 }}
.grid {{ display:grid; grid-template-columns: 1fr 1fr; gap:6pt 12pt }}
.box {{ background:#f6f8fb; border-left:4px solid #0b3d91; padding:5pt 8pt }}
.why {{ border-left-color:#c62828 }} .fix {{ border-left-color:#2e7d32 }} .imp {{ border-left-color:#ef6c00 }}
.comment {{ background:#fff8e1; border:1px solid #f0d58a; border-radius:5px; padding:6pt 9pt; margin:6pt 0 }}
</style></head><body>
<h1>AEM Guides — Sites structure audit</h1>
<div class="muted">Author {e(author)} · Sites <code>/content/guide</code> compared with the DITA source in DAM · {e(stamp)}</div>
<div class="kpis">
<div class="kpi"><b>{summ['pages']}</b>Sites pages</div><div class="kpi"><b>{summ['topics']}</b>topic pages ({stats['unlive']} never published)</div>
<div class="kpi"><b>{summ['generated']}</b>pages named “-”, “-0”, “-1” …</div><div class="kpi"><b>{len(stats['presets'])}</b>output presets used</div>
<div class="kpi"><b>{len(groups)}</b>issue types ({high} high)</div></div>
<h2>Key conclusions</h2><ol>
<li><b>“-0”, “-1” names</b> happen because page names are built from the topic <i>title</i>; non-Latin titles (Chinese, Japanese, Russian, Arabic) filter down to nothing, so AEM numbers the clashes. Name pages after the topic file instead.</li>
<li><b>{len(stats['presets'])} different output presets</b> wrote into /content/guide. That is the root cause of the stray top-level folders, duplicate outputs and version numbers in URLs.</li>
<li><b>Languages are mixed</b>: some language nodes were generated from the English map, and some English maps contain Chinese/Japanese topics.</li>
<li><b>Language-inside-product</b> works for today's mostly-English site but blocks AEM translation and localised navigation; plan language roots before adding languages.</li></ol>
<h2>Issue types</h2><table class="t"><tr><th>ID</th><th>Severity</th><th>Issue type</th><th>Occurrences</th><th>Pages / items</th></tr>"""]
    for g in groups:
        parts.append(f"<tr><td>{g['id']}</td><td style='color:{sev_col[g['severity']]}'><b>{g['severity']}</b></td>"
                     f"<td>{e(g['category'])}</td><td>{len(g['items'])}</td><td>{g['pages'] or '—'}</td></tr>")
    parts.append("</table>")
    parts.append("<div style='page-break-before:always'></div>" + language_section(stats))
    for g in groups:
        kb = KB.get(g["key"], {})
        parts.append(f"<div class='type'><h3>{g['id']} · {e(g['category'])} "
                     f"<span class='sev' style='background:{sev_col[g['severity']]}'>{g['severity']}</span></h3>"
                     f"<p><b>Found:</b> {len(g['items'])} occurrence(s), {g['pages']} page(s)/item(s). "
                     f"{e(g['items'][0]['detail']) if len(g['items']) == 1 else ''}</p>"
                     f"<div class='grid'><div class='box why'><b>Why it happens</b><br>{kb.get('why', '')}</div>"
                     f"<div class='box imp'><b>Impact</b><br>{kb.get('impact', '')}</div></div>"
                     f"<div class='box fix' style='margin-top:6pt'><b>Fix</b><br>{kb.get('fix', '')}</div>")
        if kb.get("comment"):
            parts.append(f"<div class='comment'><b>Comment.</b> {kb['comment']}</div>")
        shots = [s for i in g["items"] for s in i.get("shots", [])][:3]
        for s in shots:
            parts.append(f"<figure><img src='{e(s['src'])}'><figcaption>{e(s['caption'])}</figcaption></figure>")
        # all occurrences, one row each
        if len(g["items"]) > 1 or g["key"] in {"lang", "names", "orphan"}:
            head = ["#", "Where", "Finding", "Cause" if g["key"] == "lang" else "Examples", "Origin (preset · by · created)"]
            parts.append("<h4>All occurrences</h4><table class='t'><tr>" + "".join(f"<th>{h}</th>" for h in head) + "</tr>")
            for i in g["items"]:
                c3 = i.get("cause") if g["key"] == "lang" else _examples(i)
                parts.append(f"<tr><td>{i['id']}</td><td><code>{e(i['where'].replace(SITES, '…'))}</code></td>"
                             f"<td>{e(i['title'])}</td><td>{e(c3 or '')}</td><td>{e(i.get('origin', ''))}</td></tr>")
            parts.append("</table>")
        # item detail only when the type is a single occurrence with its own list
        if len(g["items"]) == 1 and g["items"][0].get("rows"):
            i = g["items"][0]
            parts.append("<h4>Details</h4><table class='t'><tr>" + "".join(f"<th>{e(h)}</th>" for h in i.get("row_head", []))
                         + "</tr>" + "".join("<tr>" + "".join(f"<td>{e(str(c))}</td>" for c in r) + "</tr>" for r in i["rows"][:80])
                         + "</table>")
            if len(i["rows"]) > 80:
                parts.append(f"<div class='muted'>… {len(i['rows']) - 80} more in issues.json</div>")
        parts.append("</div>")
    parts.append("</body></html>")
    return "".join(parts)


def to_pdf(html_file: Path, pdf: Path) -> None:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.goto(html_file.resolve().as_uri(), wait_until="load")
        pg.pdf(path=str(pdf), format="A4", print_background=True,
               display_header_footer=True, header_template="<span></span>",
               footer_template="<div style='font-size:7pt;width:100%;text-align:center;color:#888'>"
                               "AEM Guides structure audit · <span class='pageNumber'></span>/<span class='totalPages'></span></div>",
               margin={"top": "14mm", "bottom": "16mm", "left": "12mm", "right": "12mm"})
        b.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    settings = {}
    try:
        settings = json.loads((ROOT / "runs" / "aem-settings.json").read_text())
    except (OSError, ValueError):
        pass
    ap.add_argument("--author", default=settings.get("author", "http://139.59.13.139:4502"))
    ap.add_argument("--user", default=os.environ.get("PDFVAL_AEM_USER") or settings.get("user", ""))
    ap.add_argument("--out", default=str(ROOT / "reports" / "aem-structure"))
    ap.add_argument("--no-shots", action="store_true", help="skip the AEM UI screenshots")
    a = ap.parse_args(argv)
    password = os.environ.get("PDFVAL_AEM_PASSWORD", "")
    if not a.user or not password:
        print("Set the AEM user (--user / PDFVAL_AEM_USER) and PDFVAL_AEM_PASSWORD.", file=sys.stderr)
        return 2
    aem = Aem(a.author, a.user, password)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = Path(a.out) / stamp
    out.mkdir(parents=True, exist_ok=True)

    print("Reading /content/guide and the DAM maps …")
    data = collect(aem)
    issues = analyse(data)
    stats = next(i for i in issues if i["key"] == "_stats")
    issues.remove(stats)
    groups = consolidate(issues)
    summ = summary(data, issues)
    print(f"  {summ['pages']} pages, {summ['maps']} maps, {len(groups)} issue types, {len(issues)} findings")
    if not a.no_shots:
        print("Taking screenshots …")
        screenshots(aem, issues, out)
    (out / "issues.json").write_text(json.dumps({"summary": summ, "stats": stats, "groups": groups},
                                                ensure_ascii=False, indent=1))
    h = out / "report.html"
    h.write_text(build_html(groups, stats, summ, a.author, stamp), encoding="utf-8")
    pdf = out / "aem-structure-audit.pdf"
    to_pdf(h, pdf)
    latest = Path(a.out) / "latest"
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    latest.symlink_to(out.name)
    print(f"Report: {pdf}")
    for g in groups:
        print(f"  {g['id']} [{g['severity']}] {g['category']} ({len(g['items'])})")
    return 1 if any(g["severity"] == "high" for g in groups) else 0


if __name__ == "__main__":
    sys.exit(main())
