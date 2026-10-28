"""Data missing: words stage drops are reported as missing, wherever they sit.

Two things this pins. A replacement that only takes words away ("start the calibration sequence." ->
"start.") is data missing, not a wording change - it used to be filed as "changed text" unless it was
big enough to count as critical, so a sentence quietly losing its last words slipped past a reviewer
reading the report for missing content. And content that runs over a page break is compared across
it: the same text reflowed onto another page is no issue, while text stage really drops at the break
is reported with the page it belongs to.

[content] shrunk_is_missing = false restores the old classification.
"""
import pymupdf
import pytest

from pdfval import engine

HEAD = "Calibrating the monitor"
LINES = ["Connect the colorimeter to the monitor before you start the calibration sequence.",
         "Keep the ventilation openings free so warm air can leave the housing easily.",
         "Press the menu key to open the settings menu and select the item you want.",
         "Set to extend your screen from the operating system of your computer."]


@pytest.fixture
def cfg():
    c = engine.load_config()
    c["typography"]["enabled"] = False
    return c


def _pdf(path, lines, *, split_at: int | None = None) -> str:
    """One section; with `split_at` the lines after it open a second page."""
    doc = pymupdf.open()
    pg = doc.new_page(width=595, height=842)
    pg.insert_text((72, 70), HEAD, fontsize=16, fontname="hebo")
    y = 120
    for k, t in enumerate(lines):
        if split_at is not None and k == split_at:
            pg, y = doc.new_page(width=595, height=842), 80
        pg.insert_text((72, y), t, fontsize=10, fontname="helv")
        y += 18
    doc.set_toc([[1, HEAD, 1]])
    doc.save(path)
    doc.close()
    return str(path)


def _content(r):
    return [f for s in r["sections"] for f in s["findings"] if f["check"] == "content"]


# --- a replacement that only removes words ----------------------------------------------------------

def test_dropped_words_are_data_missing_not_a_wording_change(tmp_path, cfg):
    short = ["Connect the colorimeter to the monitor before you start."] + LINES[1:]
    r = engine.compare(_pdf(tmp_path / "a.pdf", LINES), _pdf(tmp_path / "b.pdf", short), cfg)
    got = _content(r)
    assert len(got) == 1, [f["message"] for f in got]
    assert got[0]["types"] == ["missing text"] and got[0]["issue"] == "Data missing"
    assert got[0]["message"].startswith("Missing text:")          # not "Changed text:"
    assert "calibration sequence" in got[0]["message"]
    assert got[0]["genuine"]


def test_one_dropped_word_still_counts(tmp_path, cfg):
    short = ["Connect the colorimeter to the monitor before you start the calibration."] + LINES[1:]
    r = engine.compare(_pdf(tmp_path / "a.pdf", LINES), _pdf(tmp_path / "b.pdf", short), cfg)
    assert [f["types"] for f in _content(r)] == [["missing text"]]


def test_the_score_counts_the_words_that_went(tmp_path, cfg):
    short = ["Connect the colorimeter to the monitor before you start."] + LINES[1:]
    r = engine.compare(_pdf(tmp_path / "a.pdf", LINES), _pdf(tmp_path / "b.pdf", short), cfg)
    assert r["sections"][0]["content"]["match_pct"] < 100.0


def test_a_real_rewording_is_still_a_wording_change(tmp_path, cfg):
    """Words stage brings of its own: the sentence was rewritten, not cut."""
    other = ["Attach the measuring device to the panel prior to launching the colour routine."] + LINES[1:]
    r = engine.compare(_pdf(tmp_path / "a.pdf", LINES), _pdf(tmp_path / "b.pdf", other), cfg)
    assert all("changed text" in f["types"] for f in _content(r)), [f["types"] for f in _content(r)]


def test_the_old_classification_can_be_restored(tmp_path, cfg):
    cfg["content"]["shrunk_is_missing"] = False
    short = ["Connect the colorimeter to the monitor before you start."] + LINES[1:]
    r = engine.compare(_pdf(tmp_path / "a.pdf", LINES), _pdf(tmp_path / "b.pdf", short), cfg)
    assert [f["types"] for f in _content(r)] == [["changed text"]]


# --- across a page break ----------------------------------------------------------------------------

def test_text_reflowed_onto_the_next_page_is_no_issue(tmp_path, cfg):
    """Prod breaks after two lines, stage after three: the same content, laid out differently."""
    r = engine.compare(_pdf(tmp_path / "a.pdf", LINES, split_at=2),
                       _pdf(tmp_path / "b.pdf", LINES, split_at=3), cfg)
    assert not _content(r), [f["message"] for f in _content(r)]


def test_a_line_stage_drops_at_the_page_break_is_reported(tmp_path, cfg):
    """The line that opened prod's second page is gone from stage."""
    r = engine.compare(_pdf(tmp_path / "a.pdf", LINES, split_at=2),
                       _pdf(tmp_path / "b.pdf", LINES[:2] + LINES[3:], split_at=2), cfg)
    got = _content(r)
    assert got and "missing text" in got[0]["types"]
    assert "Press the menu key" in got[0]["message"]
    assert [l["page"] for l in got[0]["baseline"]] == [1]          # prod's second page


def test_a_sentence_split_over_the_break_is_matched_whole(tmp_path, cfg):
    """Prod ends page 1 mid-sentence and finishes it on page 2; stage keeps it on one page."""
    half = ["The receiver supports resolutions up to 3840 x 2160 at 60 Hz over the",
            "DisplayPort input and up to 1920 x 1080 at 120 Hz over the HDMI input."]
    r = engine.compare(_pdf(tmp_path / "a.pdf", half, split_at=1), _pdf(tmp_path / "b.pdf", half), cfg)
    assert not _content(r), [f["message"] for f in _content(r)]


def test_the_half_sentence_stage_drops_at_the_break_is_reported(tmp_path, cfg):
    half = ["The receiver supports resolutions up to 3840 x 2160 at 60 Hz over the",
            "DisplayPort input and up to 1920 x 1080 at 120 Hz over the HDMI input."]
    r = engine.compare(_pdf(tmp_path / "a.pdf", half, split_at=1), _pdf(tmp_path / "b.pdf", half[:1]), cfg)
    got = _content(r)
    assert got and "missing text" in got[0]["types"] and "DisplayPort input" in got[0]["message"]


# --- a cross-reference page RANGE ---------------------------------------------------------------

@pytest.mark.parametrize("ref", ["see page 24-25.", "see page 24 - 25.", "see pages 24 to 26.",
                                 "see page 24–25.", "(see page 24-25)"])
def test_a_page_range_is_folded_whole(ref):
    """Folding only the first number left the tail ("- 25.") in the word stream, and the comparison
    reported that scrap against whatever the other template writes in its place."""
    import pymupdf
    from pdfval import extract, normalize

    def words(text):
        doc = pymupdf.open()
        pg = doc.new_page(width=595, height=842)
        pg.insert_text((72, 70), "Connections", fontsize=16, fontname="hebo")
        pg.insert_text((72, 120), f"For detailed connection methods, {text}", fontsize=10, fontname="helv")
        doc.set_toc([[1, "Connections", 1]])
        import tempfile, pathlib
        p = pathlib.Path(tempfile.mkdtemp()) / "x.pdf"
        doc.save(str(p))
        doc.close()
        d = extract.load(str(p), "prod", engine.load_config())
        normalize.fold_xref_pages(d)
        return [w.norm for w in d.words if w.norm]

    left = words(ref)
    assert not [w for w in left if any(c.isdigit() for c in w)], left
    assert not [w for w in left if w.strip() in ("-", "–", "—")], left
