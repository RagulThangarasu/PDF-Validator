# pdfval — section-by-section PDF parity (prod baseline vs stage)

## Setup and run (start here)

**1. Get the latest code and set up (once, and again after every `git pull`):**

```bash
git clone https://github.com/RagulThangarasu/PDF-Validator.git   # first time only
cd PDF-Validator
git pull                     # later: get the latest fixes
./setup.sh                   # creates .venv, installs everything, checks it works
```

`setup.sh` needs **Python 3.11 or newer**. It uses one that is installed. If there is none, it downloads Python 3.12 into your home folder with `uv` (no admin rights needed). The Python that comes with macOS is 3.9 and does not work (`No module named 'tomllib'`), and the VS Code Python extension does not install Python. Never copy `.venv` from another computer: `setup.sh` replaces a copied or too-old one.

Optional: `brew install tesseract` for OCR of text drawn inside pictures.

**2. Start the UI (every time):**

```bash
./start.sh                   # opens http://localhost:8700 - keep the terminal open
```

To restart after updating the code: press **Ctrl+C** in that terminal, then run `./start.sh` again.

**3. Run a comparison.** In the browser:
1. Pick the **prod PDF**, then the **stage PDF** or a stage **URL**. For a URL, tick **Crawl** to follow every page of the left navigation, and add the site login if needed.
2. Enter your **AEM user and password** in the **AEM login** card. The AEM author URL is pre-filled (`[aem] author` in `config/default.toml`), and you can change it there.
3. Click **Run comparison**, then open the result or download **⬇ Genuine issues** (PDF + CSV). Runs are saved in `runs/<run-id>/`.

In VS Code: **Cmd+Shift+P → Python: Select Interpreter → `./.venv/bin/python`**.

Validates a **candidate** PDF (stage) against a **baseline** PDF (prod) section by section for:

| Check | What is compared | Example finding |
|---|---|---|
| **structure** | outline/headings: missing, extra, renamed, re-levelled sections | `Section “VESA specifications” not found in candidate` |
| **content** | word-level text diff including punctuation and spacing; gives the content % | `Changed text: “Important” → “IMPORTANT:”`, `Spacing: “over  the” (2 spaces) → (1 space)` |
| **tables** | every table row present, judged by its words so reading order doesn't matter | `Table row missing in stage: “Storage 64 GB …”` (critical) |
| **integrity** | broken links, prod links missing in stage, broken glyphs, text off the page, embedded files | `Link missing in stage: “Package contents.”` |
| **style** ("CSS") | font-family, weight, italic, size and colour of the *same words* | `[h3] font-weight: 400 → 700, font-size: 16pt → 13.5pt` |
| **layout** (alignment) | indent vs content box, text-align, line-height, space above headings, and **graphic placement**: inline in a sentence or on its own line, and which text it follows | `[body] text-align: center → left (4 lines)`, `Inline graphic dropped out of its line: in prod it sits in the text after “Select”; in stage it is on its own line below “Select”` |
| **assets** | image count, aspect ratio, and width relative to the content box | `Image count: 3 → 2` |

The PDFs don't need the same page size, page count or engine. The sample pair is InDesign at 668×915 pt versus AEM Guides on A4. The engine aligns the two by **section**, not by page.

## Genuine issues (the report to act on)

Most findings are differences you can live with (CSS, wrapping, case/punctuation, spacing). The **genuine issues** are the real problems, listed in `[genuine] types` in the config:

| Issue | Example |
|---|---|
| Section missing / duplicated / in the wrong place | `Duplicate section in stage: “Setup” appears 2 times in stage but 1 time(s) in prod` |
| Image missing / broken / in the wrong section | `Image placed in the wrong section: in prod it is in “Installation” (p.2), in stage it is in “Maintenance” (p.4)` |
| Image blacked out / a different image at the same spot | `Image blacked out in stage: 50% of the picture is black where prod shows content` · `Different image in stage: the picture at this spot is not the prod picture` |
| Image duplicated into another section | `Image duplicated: the picture from “Installation” (prod p.2) appears again in “Maintenance” in stage` |
| Image label / caption missing | `Image label / caption missing in stage: Missing text: “Low Brightness Low Contrast High Contrast”` |
| Content in the wrong section, data missing | `Content placed in the wrong section: “Keep the ventilation …” is in “Overview” in prod but in “Maintenance” in stage` |
| Content duplicated into another section | `Content duplicated in stage: “Keep the ventilation …” is prod text from “Overview”, repeated in “Maintenance”` |
| Table missing / header missing / rows missing / tables or rows merged / table split | `Table header missing in stage: prod table (p.3) starts with the header row “Setting Value” …` |
| Link broken / not a link in stage / pointing to the wrong section / to a different address | `Link points to the wrong section: “For details, see …” goes to “Setup” in prod but to “Maintenance” in stage` |
| Attachment missing, broken characters, text cut off | |

**Nothing is dropped.** Every finding is reported (`report.max_findings_per_check = 0`, no cap); if you set a cap it only thins presentation findings (CSS, layout). The PDF report lists every issue of every severity in four groups, each starting on a new page: **1. Content** (text), **2. Links** (links, attachments, broken characters, text off the page), **3. Formatting** (sections and headings, tables, images, TOC, layout, bullets and lists) and **4. CSS** last (fonts, sizes, weights, colours); in each group the document's section order. Critical and genuine issues are always kept, however many a section has. Presentation findings stay out of the genuine report: fonts, colours, indent, alignment, line height, line-wrap, graphic placement, same artwork drawn as vector vs image, bookmark-only differences and reordered-only text. TOC findings stay out too (they come from the ditamap and the PDF template; they are in the full report and the TOC tab, `[genuine] exclude_checks`). Template differences are info findings, not genuine issues: a callout label on one side only (stage prints “NOTE:” where prod shows only the note icon), a “(continued)” header repeated at a page break, a cross-reference page number (`see "Title" on page 12.` vs `see "Title".`, `content.ignore_xref_page_numbers`), and a prod picture that stage shows as part of one larger combined picture. Arrow keys drawn with the Wingdings 3 font (the letters p q t u) are compared as ▲ ▼ ◄ ►. Pictures laid out in another order (two columns, items flowing onto the next page) are paired by their pixels, wherever they are in the section. Everything else is in: extra sections and heading levels; extra, distorted, different, blacked-out and duplicated images and missing image labels; and every table-structure change (extra table or row, rows split, cells merged or split, table turned into text and back). An image counts as *distorted* when stage draws it at least 25 % out of its own pixel proportions and prod does not.

Every run writes `genuine-issues.pdf` (overview table with a description and why it matters, then each issue with prod/stage screenshots) and `genuine-issues.csv` (opens in Excel). In the UI: **⬇ Genuine issues (N)**, or *PDF report… → Genuine issues only*. CLI: `--fail-on genuine` exits 1 when there is any genuine issue.

Images get a second opinion from their pixels, not just from the visual hash: a correlation of the two pictures (pictures ≥ 15 % of the content width, not small logos) and the share that turned black, measured on the whole image box. On the sample manuals, genuinely identical pictures correlate ≥ 0.8 and swapped ones ≤ 0.72. Thresholds: `assets.same_picture_similarity`, `different_picture_similarity`, `picture_min_width`, `blackout_fraction`. Duplicates count only across sections, because text or pictures repeated within one section are usually table reading order.

"Data missing" counts only text whose words are really absent from stage (≥ `genuine.data_missing_words`), not text that moved or changed case.

## Proof: every kind of content defect is caught

`tests/test_real_defects.py` plants one known defect into a copy of a real manual (the SL04 prod PDF), compares the original with the copy, and requires the defect to be reported as a genuine issue:

| Planted in stage | Reported as |
|---|---|
| a word removed / text added | Data missing / Extra content |
| a space removed (“meets the” → “meetsthe”) / a full stop removed | Space after a word differs / Punctuation differs |
| a link removed / pointed at another section | Link not working / Link to wrong section |
| an image removed / replaced by another picture | Image missing (or broken) / Different image |
| a table row removed / a whole table removed / two tables merged into one | Table row missing / Table missing / Tables merged |
| a section removed / moved / duplicated | Section missing / Section in the wrong place / Section duplicated |

A missing or duplicated section is one finding: its own text, images and rows are folded into it, and the stage marker sits where the section belongs. Comparing each sample manual with itself gives no issue.

**Internal links are checked by where they land.** InDesign and AEM Guides link inside a document through *named destinations*, not page numbers. Each one is resolved to its page and position, including destination names in other scripts (“錨點”), so a link that jumps to another section, or to a destination that does not exist, is reported. It is not reported when the link's own text names stage's target and not prod's, for example “Front panel.” going to *Front panel* in stage but to its parent *Components* in prod.

## AEM topic GUIDs: open the source topic and fix it

A stage PDF published by AEM Guides has a named destination for every topic (`GUID-…-en`) and for each section, table, list, note and image in it (`GUID-…-en-section_3`). The validator maps every stage issue to the topic it comes from, using the issue's own position in stage, and to the nearest element in that topic ([pdfval/aem.py](pdfval/aem.py)).

- **UI**: every issue shows a GUID chip, and the chip opens that topic in AEM in a new tab. The expanded issue shows *Source topic*, the full GUID, **Copy GUID**, and the nearest element (e.g. `near section_2 “Panel”`). The sidebar filters issues by topic. The **AEM topics** tab lists each topic with its GUID link and its genuine, critical and total issue counts, plus **Show issues** (the filter is kept in the URL, so you can share it).
- **Reports**: `genuine-issues.pdf` starts with *AEM topics to fix*, and every issue in it has a clickable GUID. `genuine-issues.csv` has *AEM topic*, *GUID* (an Excel `HYPERLINK`), *Element* and *Open in AEM* columns. `summary.md` and the side-by-side viewer show the links too.
- TOC issues have no GUID. They come from the ditamap and the PDF template, not from a topic.

**What a GUID opens** (`[aem] open_in`): by default the **topic file** the GUID belongs to. The file is looked up in AEM, so this needs the AEM login; without it (or when AEM does not find the file) the link opens the product's **map** in the author editor, from `<product folder>/Maps/<map file>`. The map file is named in the stage PDF (e.g. `sl04_and_sh04.ditamap`), and the product folder comes from the product settings, e.g. `sl04_and_sh04` = `/content/dam/benq-aem-guides/en/Education/Signage/SL04-and-SH04`. With the AEM login, the map is looked up in AEM too (so the product folder is found automatically) and topic files are searched inside that product folder. `open_in = "map"` always opens the map instead.

**Link settings** (AEM topics tab → *AEM link settings*, saved in `runs/aem-settings.json`, or `[aem]` in the config):

| Setting | Example |
|---|---|
| AEM author URL | `https://author-p12345-e67890.adobeaemcloud.com` |
| DAM folder per product, keyed by the ditamap name (`sl04_and_sh04.ditamap` → `sl04_and_sh04`) | `/content/dam/benq-aem-guides/en/…` |
| Link template, with placeholders `{author} {folder} {guid} {lang} {map} {product} {element} {q}` | `{author}/libs/fmdita/clientlibs/xmleditor/page.html?src={folder}/{guid}.dita` |

When you save, the current run's links, CSV, genuine-issues PDF and viewer are rebuilt. Runs made before this feature get their GUIDs the first time you open them.

## Site navigation (PDF ⇄ Web page runs)

The content checks read only the page's main content. A web run also checks the guide's chrome around it, on every crawled page ([pdfval/site_nav.py](pdfval/site_nav.py)):

| Check | What is validated | Example finding |
|---|---|---|
| **Left navigation: L1 TOC** | every level-1 entry of the prod PDF's TOC (bookmarks) is a top-level entry of the left navigation, in the same order; the navigation is the same on every page | `Supported media: missing (L1 TOC entry not in the left navigation)` |
| **Navigation links** | every navigation link opens (HTTP status), lands on the page it names (title / h1 = link text) and on its `#anchor`; the highlighted entry is the page itself | `Troubleshooting: opens “Settings”` |
| **Download PDF** | the button is on every page, the file downloads and is a PDF, and it is the prod document (page count, shared vocabulary, cover) | `Download: text/html, 5120 bytes` |
| **Next / previous topic** | each page's next / previous goes to the next / previous page of the navigation, its label names that page; none before the first or after the last page | `Next topic: Basic operations → Settings (goes to another page than the navigation order)` |
| **On this page** | every sub-heading is listed, and each entry jumps to its heading | `Mounting: #nope, anchor not found` |
| **Product subtitle** | where the header's product title is read from (element, and the AEM `jcr:content` property it comes from on an author instance); the same on every page; it names the product on the prod PDF's cover, title or file name; the version in the footer matches the PDF | `Monitor arm BSH Series: none of BSH is on the PDF cover — wrong product?` |

In the UI: the **Site navigation** card and tab (*Problems only* / *All checks*, filter by check). Each row shows the element the value was read from. Downloads: `site-navigation.csv`, the `summary.md` section, and `site-download.pdf` (the file the site's button delivers). The elements are found by their labels ("Table of contents", "On this page", "Next topic", "Download PDF"). If a site labels them differently, set CSS selectors under `[site]` in the config.

## Source download: what prod and stage really contain

Stage PDFs come from AEM Guides DITA that was converted from the prod PDF, and content and structure can get lost on the way. **⬇ Source (prod + stage)** on a run, the ⋯ menu for one side only, or `pdfval source <pdf> --out file.zip`, gives each PDF as:

| Folder | Contents |
|---|---|
| `raw/` | Everything read straight from the PDF, unfiltered: `document.json` (every text line with its spans: font, size, colour, bold, italic, superscript, position; outline; named destinations; images; links; tables cell by cell; metadata), `text.txt`, `tables/*.csv`, `images/` (the original embedded files), `attachments/`, `fonts.csv`, `links.csv`, `metadata.xmp` |
| `dita/` | The document rebuilt as **DITA 1.3 source that AEM Guides can import**: a `.ditamap` with one topic per heading, nested like the outline; paragraphs, bulleted/numbered lists, note/tip/warning callouts (including bordered note boxes), CALS tables, figures with the extracted images, inline bold/italic/superscript/subscript and `<xref>` links. The language comes from the PDF's tag, unless the text is in another script. Running headers/footers and the printed TOC are left out (the map generates them) but are in `raw/`. |
| `aem/` | Stage only, with an AEM login: the **real topic files downloaded from AEM** for every topic GUID in the PDF, and `manifest.json` (GUID → DAM path). |

The zip is built on first download (tens of seconds for a 70-page manual) and kept in the run folder.

## Issue categories and types (filters in the UI, viewer and PDF report)

Every issue has one **category** and one or more **types**. In the UI you pick a category and see only its issues, then narrow them with the type chips.

| Category | Types |
|---|---|
| **Content** | missing text · extra text · changed text · case (uppercase/lowercase) · punctuation · case + punctuation · spacing (word gap: double, missing or extra spaces, "details,see" vs "details, see") · paragraph break (off by default: `content.check_paragraphs`) · reordered |
| **Images** | missing image · extra image · image changed (same place, different picture) · size / aspect · placement (inline in a sentence vs on its own line) · raster vs vector |
| **Tables** | missing table · extra table · tables merged · table split · missing row · extra row · rows merged · row split · cells merged · cells split · table to text / text to table |
| **Structure** | missing section · extra section · bookmark only · outline level · heading text |
| **Links & rendering** | broken link · missing link · broken glyph · text off page · missing file · extra file |
| **CSS / layout** | font-family · font-size · font-weight · font-style · color · indent · text-align · line-height · space-above |

**How tables are compared (no rules specific to one document):**
- Row boxes come from table detection; row text comes from the extracted words. Each row is stretched to the full table width, and each word belongs to only one row.
- Cells are counted against the table's column grid, taken from the row where the detector sees the most cells. A spanning (merged) cell occupies fewer columns.
- A *data* table needs at least 2 rows and at least 2 columns with text, so a bordered Note/Tip box doesn't count.
- Rows are mapped by their words in any order, with the first-cell label required. When identical rows repeat, such as column headers, the one at the aligned position wins, and repeated header rows are ignored when relating tables.
- A table only counts as "turned into plain text" if text from different rows runs together on one line on the other side. Otherwise it is still a table, just one the detector couldn't see.

**Cover page and TOC.** The cover (everything before the first section) is not validated (`sections.front_matter = false`). The printed TOC is compared for its **level-1 entries only** (`toc.max_level = 1`): title, order and page number of each chapter. A level-1 entry that is deeper on the other side is reported as a level difference, and the TOC heading ("Table of contents") is still compared. `max_level = 0` compares every level.

**Other languages.** Every character is compared, in any script:
- Chinese, Japanese, Thai, Lao, Khmer and Myanmar have no spaces between words, so each character is compared on its own, and a finding names exactly the characters that differ. A short difference is shown in its line, e.g. `Changed text: “废” → “州” in “有关 China WEEE 州弃电器电子产品回收处理”`.
- Right-to-left text (Arabic, Hebrew) is compared in reading order. Arabic "presentation form" characters and ligatures (ﬁ) are folded to plain letters.
- Full-width and CJK punctuation (“，” “（）” “。”) is kept, so “，” → “,” is reported.
- Tested on Arabic, Chinese, Japanese, Russian and English prod/stage pairs: an identical copy gives no issue, and missing, extra and changed text, a missing sentence, empty table cells and table overflow are all caught.

**Tables are read row by row, cell by cell** (right to left in Arabic/Hebrew tables), top to bottom inside a cell (`extract.table_reading_order`). Plain top-to-bottom reading scrambles a row whose label wraps ("液晶面 / 板"), so the two sides no longer line up. A row whose label changed ("液晶面板" → "液晶螢幕") is paired by its position between matched neighbours and the same values. It is reported once, as changed text with both sides boxed, not as an extra row or a row split. A line break inside a cell joins Chinese/Japanese characters without a space ("面板").

**Superscript / subscript.** Every character records whether it is raised or lowered: at least 15 % smaller than the line's main text and above or below its baseline. The PDF's own superscript flag is a guess and is not used. The same text raised or lowered on one side only is a genuine issue, shown in Unicode: `Superscript / subscript differs: “(Cr+6)” → “(Cr⁺⁶)” (“+6” is superscript in stage)`.

**Text outside the table border.** Stage text in a table that runs across a cell border (into the next column, past the table edge, or down over a row line) is a genuine issue when the same text sits inside its cell in prod. The borders are the lines the page actually draws. Row lines are tested against the core height of the text (centre ± 0.35 × font size), because the glyph boxes of tall scripts such as Arabic reach past letters that sit inside the cell. The screenshots box the overflowing line in stage and the same words in prod.

**Callout labels are house style, not content.** "Tips" → "TIPS:", "Note" → "NOTE:", "Warning" → "WARNING:", "Important" → "IMPORTANT:" (case, colon, plural) are not reported. A *different* label ("Tip" → "NOTE:") still is. The words are listed in `content.label_words`; an empty list compares them strictly.

**Text that only continues on the next line is not an issue.** When the same text wraps to the next line at a different word, is hyphenated over a line break, starts a new line/paragraph on one side only, or sits in another cell order because a table cell wrapped, nothing is reported as content, CSS/layout or table layout. The switches `content.ignore_relocated`, `content.check_paragraphs` and `layout.check_wrap` turn those reports back on.

## Verdict model: content %, critical, CSS kept separate

| Dimension | What it covers | How it is judged |
|---|---|---|
| **Content %** | The text itself: words, case, **punctuation** (`. , : ;`, quotes, dashes) and **spacing** between words on the same line (double or missing spaces). | `match % = (prod words present in stage − spacing errors − superscript/subscript errors) ÷ (prod words + words stage adds)`. **100 % means the text is identical**: every missing, changed or added word, space and punctuation mark lowers it. Text that is only stored in another order in the PDF (a wrapped line written as its own run, a table label centred in its row on one side and top-aligned on the other, a repeated column header) is not reported as reordered.. Pass ≥ `content.pass_pct` (98), warn ≥ `content.warn_pct` (90), below that fail. Font weight, colour and size never affect it. |
| **Critical / breaking** | A missing section, **missing table row**, missing figure image, missing embedded **file**, a missing content block (≥ 8 prod words absent from stage in any order), a broken link target, broken glyphs (U+FFFD or private-use characters), or text outside the page. | Always flagged. A section with any critical issue **fails** whatever its content %. |
| **CSS / layout** | Font family, weight, size, colour, indent, alignment, line height. | Counted separately, with the global style map. A section with CSS issues can be at most **warn**. |
| Other | Links present in prod but plain text in stage, extra images or rows, image geometry, bookmark differences. | Uses its own severity: an error fails the section, a warning makes it warn. |

**Section status:** fail if it has a critical issue, content % below the warn threshold, or another error. Warn if content % is below the pass threshold, or it has CSS issues or warnings. Otherwise pass.

**CI** (JUnit and `tests/test_pdf_parity.py`) fails a section only on a critical issue or a content % below the warn threshold. CSS issues are listed in the output but never fail CI.

**Table rows** are judged by their words, not their order. The two PDFs rarely extract table cells in the same order, so a row counts as found when its first-cell label and most of its words exist on the other side. Case and punctuation are ignored for that presence test; the content check still reports wording changes such as "Note" → "NOTE:".

## How it works

```
 prod.pdf ─┐                                      ┌─ results.json / junit.xml / summary.md
           ├─ extract ─ sections ─ match ─ checks ─┤
 stage.pdf ┘  (PyMuPDF)  (outline   (ordered   │   └─ index.html  (PDF.js side-by-side viewer)
              words+style  or heading fuzzy    │                      │
              +bbox)       fallback)  LCS)     │                      ▼
                                               │         e2e/walk_sections.py (Playwright):
                           content → pairs of equal words → style + layout compare those pairs
                                                            scroll to each section → assert visible → screenshot
```

1. **Extract** ([pdfval/extract.py](pdfval/extract.py)): PyMuPDF `rawdict` gives every word with its font, weight, size, colour and bbox. Text outside the trim box (printer slugs, crop marks) is dropped. Running headers, footers and page numbers are removed automatically, and the config adds ignore regexes. The extractor also finds each document's content box (body margins), so alignment can be compared even when page sizes differ.
2. **Sections** ([pdfval/sections.py](pdfval/sections.py)): anchors come from PDF bookmarks, located on the page by fuzzy-matching the heading text. When a PDF has no outline, large-font lines are used instead. Baseline and candidate anchors are paired by order-preserving fuzzy alignment. Each matched pair defines one **unit** that runs until the next matched heading. A heading that exists on only one side becomes a structure finding, and its text is still diffed inside the parent unit.
3. **Checks** ([pdfval/checks/](pdfval/checks/)): `content` runs first and records which words are equal on both sides. `style` and `layout` compare only those pairs, so every CSS or alignment finding refers to the *same text* in both PDFs.
4. **Report** ([pdfval/report/](pdfval/report/)):
   - A prod crop and a stage crop for every issue ([shots.py](pdfval/report/shots.py)).
   - `report.pdf` with the issues and their screenshots ([pdf_report.py](pdfval/report/pdf_report.py)).
   - JSON, JUnit (one testcase per section), Markdown, and the interactive viewer.

### Correspondence strategy: showing the same place in prod and stage
Every issue's prod and stage screenshots come from the **content alignment**. The words that are identical in both PDFs are matched by the word-level diff, and those matches are the anchors that link any spot in one PDF to the same spot in the other.

| Situation | How the other side is located |
|---|---|
| Same text differs in style, alignment or wording | The matched words themselves: boxes on both sides |
| Text exists only on one side | The insertion point between the matched words around the gap. It is marked on the same page, never pushed to the next page. |
| A heading is a bookmark on one side only | If the heading text is matched on the other side, it is **not** a missing section. It becomes "Bookmark only in stage/prod" and both sides are boxed. Example: "Physical" is a PDF bookmark in stage but only table text in prod. |
| Images | Each image is placed by the text next to it, mapped to the other PDF, then **matched by appearance**: a 16×16 visual fingerprint after trimming borders. A different picture in the same spot means "image content differs". Aspect ratio and width are measured on the visible picture, not the padded box. |
| Image on one side, same artwork drawn as vectors or text on the other | The other page is searched visually (correlation at several scales plus the fingerprint). If found, it is reported as "same artwork, image vs vector" (info), not "extra image". |
| Small icons (note, tip, LED) | Paired like images. Icons left unpaired are ignored by default (`assets.icons`), because they are often bitmaps in one PDF and vector drawings in the other. |
| TOC page numbers | Ignored by default (`content.ignore_toc_page_numbers`), because they shift whenever the page size or layout changes |

## Web UI (recommended)

```bash
.venv/bin/python -m pdfval ui            # opens http://127.0.0.1:8700
```

1. **New comparison**: drop or browse the prod and stage PDFs, or pick them from the PDFs found on disk (`--root` sets which folder is searched; the default is the parent of this project). You can also set tolerances, severities and sections to skip.
2. **Run**: a live progress bar shows each step (extracting, validating each section, screenshots, PDF report). Runs are saved in `runs/<id>/`, and the **Runs** page lists every past run.
3. **Run page**:
   - **Issues**: every finding, filterable by severity, check, section and text. Each issue shows the **prod and stage screenshots side by side**, with the problem boxed. Click a screenshot for a full-size prod ⇆ stage view, and use ← → to browse.
   - **Sections**: pass, warn or fail and the issue counts for each section. Click a section to filter its issues.
   - **Style map**: prod → stage CSS mismatches.
   - **Download PDF report**: summary, sections table, style map, then every issue with prod | stage screenshots.
   - **Open side-by-side viewer**: both PDFs scroll to the chosen section.

## Quick start (CLI)

```bash
cd pdf_validator
# one-time setup: see "Setup and run" at the top (needs Python 3.11+)

# 1. compare  (exit code 1 when any section has an error; --fail-on warning|never;
#    --screenshots all|warnings|errors|none controls which issues get screenshots in report.pdf)
.venv/bin/python -m pdfval compare \
  --baseline "../prod/SL04&SH04_UM_V1.2_EN.pdf" \
  --candidate "../stage/SH04 & SL04 User Manual (12).pdf" \
  --out reports/latest

# 2. open the viewer: click a section and both PDFs scroll to it, findings are boxed
.venv/bin/python -m pdfval serve reports/latest

# 3. end-to-end walk: scroll to every section, verify, screenshot
.venv/bin/python e2e/walk_sections.py reports/latest [--only "Mount"] [--headed --slow 800] [--focus-findings 3]

# or do all three with one command
./run.sh <baseline.pdf> <candidate.pdf> [out-dir]
```

### Viewer
- Left: the sections list with pass/warn/fail status, text similarity and finding count. You can filter it and step through it with `j`/`k`.
- Centre: prod and stage rendered with PDF.js. Selecting a section scrolls **both** panes to its start (solid line) and marks the end (dashed line).
- Findings are boxed in colour: content red, style purple, layout orange, assets teal, structure blue. Click a finding, or press `n`, to scroll both PDFs to that exact spot.
- The **Global style map** tab lists every baseline → candidate style mismatch across the document. Each row is effectively one CSS rule to fix in the stage stylesheet.
- It needs HTTP (`pdfval serve`) because browsers block PDF.js from reading local `file://` PDFs.

### CI
```bash
.venv/bin/python -m pytest tests/test_pdf_parity.py --baseline prod.pdf --candidate stage.pdf -q   # one test per section
.venv/bin/python -m pytest tests/test_engine_synthetic.py -q    # self-tests: injected regressions are caught
```
`reports/<run>/junit.xml` can also be published directly by Jenkins, GitLab or GitHub.

## Configuration
[config/default.toml](config/default.toml) holds every tolerance and severity. To override some of them, pass `--config my.toml`; it is deep-merged over the defaults. [config/example-override.toml](config/example-override.toml) shows an example.

- Tolerances: `style.size_tolerance` (pt), `style.color_tolerance` (RGB distance), `layout.indent_tolerance` (pt), `layout.line_height_tolerance_em`, and `assets.aspect_tolerance`.
- Severity for each CSS or layout property, e.g. make `color` an error or `indent` just info.
- `sections.aliases` handles renamed headings and `sections.skip` excludes sections. `extract.ignore_patterns` removes lines such as build stamps or dates.
- `content.case_sensitive`, `content.ignore_tokens` (bullets and glyphs), and `reorder_severity`.

## Extending
Add a module to `pdfval/checks/` with `check(unit) -> list[Finding]` and append it to `PIPELINE` in [pdfval/checks/__init__.py](pdfval/checks/__init__.py). A `Unit` gives you both `Doc`s, the word ranges of the section and the equal-word `pairs`. Some ideas:
- **tables**: column count and header text, using `page.find_tables()` from PyMuPDF.
- **visual**: pixel/SSIM diff of section crops when the page geometry matches, using Pillow and numpy.
- **links**: internal and external link targets, using `page.get_links()`.

## Result on the sample pair
83 units: front matter plus all 82 prod sections, and every prod section was found in stage. Every section fails at least one check. Most of the findings come from systematic styling differences:
- Body text is Roboto 12pt `#231f20` in prod but 11pt `#000000` in stage.
- H3 headings are Poppins Regular 16pt in prod but Bold 13.5pt in stage.
- Notes are 10pt in prod but 12pt in stage.
- Links are `#0000ff` in prod but `#00626b` in stage.
- The stage outline is flat: prod's H3 sections are H2 in stage.
- Stage has extra headings, such as "VESA specifications", "Changing the wallpaper" and "Panel".
- Labels differ in content: "Note" became "NOTE:" and "Important" became "IMPORTANT:".
