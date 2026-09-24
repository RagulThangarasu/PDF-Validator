# pdfval — section-by-section PDF parity (prod baseline vs stage)

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

## Issue categories and types (filters in the UI, viewer and PDF report)

Every issue has one **category** and one or more **types**. In the UI you pick a category and see only its issues, then narrow them with the type chips.

| Category | Types |
|---|---|
| **Content** | missing text · extra text · changed text · case (uppercase/lowercase) · punctuation · case + punctuation · spacing (word gap: double, missing or extra spaces, "details,see" vs "details, see") · paragraph break (gap between paragraphs added or removed) · reordered |
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

## Verdict model: content %, critical, CSS kept separate

| Dimension | What it covers | How it is judged |
|---|---|---|
| **Content %** | The text itself: words, case, **punctuation** (`. , : ;`, quotes, dashes) and **spacing** between words on the same line (double or missing spaces). | `match % = prod words present in stage (in order, moved, or reordered in a table) − spacing errors ÷ prod words`. Pass ≥ `content.pass_pct` (98), warn ≥ `content.warn_pct` (90), below that fail. Font weight, colour and size never affect it. |
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
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/playwright install chromium

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
