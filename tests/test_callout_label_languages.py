"""A callout label is recognised in every language the manuals are published in.

InDesign draws a note with its icon alone; AEM Guides prints the label as a word in front of it. The
icon says which label it stands for, so a label only stage has is not content stage added - as long
as the label is recognised. It was English-only, so "注記:" on a Japanese page read as extra content
beside a prod note carrying the very same label as its pencil icon.

Two things are needed for that: the label words of each language ([content] label_words,
callout_icons), and joining a label the extractor hands over one character at a time - CJK and Thai
are written without spaces, so "注記:" arrives as "注", "記", ":".
"""
import pymupdf
import pytest

from pdfval import engine, extract, normalize

BODY = "ダウンロードできない場合は、ウイルス対策ソフトウェアを無効にしてからやり直してください。"
HEAD = "Display QuicKit のダウンロードと起動"


@pytest.fixture
def cfg():
    c = engine.load_config()
    c["typography"]["enabled"] = False
    return c


def _ja(path, *, label: str | None) -> str:
    """A Japanese page whose note is drawn with a pencil icon, with or without its label in words."""
    doc = pymupdf.open()
    pg = doc.new_page(width=595, height=842)
    pg.insert_text((72, 70), HEAD, fontsize=16, fontname="japan")
    pg.insert_text((72, 120), "作業を始める前に、コンピュータとモニタの省エネ機能を無効にしてください。",
                   fontsize=10, fontname="japan")
    y = 200
    pg.draw_circle((78, y + 5), 7, color=None, fill=(0.27, 0.11, 0.53))
    pg.draw_polyline([(74, y + 9), (82, y + 1), (84, y + 3), (76, y + 11), (74, y + 9)], color=None, fill=(1, 1, 1))
    pg.draw_polyline([(74, y + 9), (73, y + 12), (76, y + 11)], color=None, fill=(1, 1, 1))
    if label:
        pg.insert_text((95, y + 8), label, fontsize=10, fontname="japan")
        pg.insert_text((95, y + 22), BODY, fontsize=10, fontname="japan")
    else:
        pg.insert_text((95, y + 8), BODY, fontsize=10, fontname="japan")
    doc.set_toc([[1, HEAD, 1]])
    doc.save(path)
    doc.close()
    return str(path)


# --- the label words -------------------------------------------------------------------------------

@pytest.mark.parametrize("word", ["注記", "警告", "重要", "提示", "참고", "примечание", "ملاحظة",
                                  "hinweis", "remarque", "nota", "uwaga", "หมายเหตุ", "catatan"])
def test_the_label_is_known_in_each_language(word, cfg):
    assert word in {w.lower() for w in cfg["content"]["label_words"]}, word


@pytest.mark.parametrize("icon,word", [("pencil", "注記"), ("pencil", "참고"), ("pencil", "ملاحظة"),
                                       ("triangle", "警告"), ("exclamation", "重要"), ("lightbulb", "提示")])
def test_each_icon_stands_for_its_label_in_each_language(icon, word, cfg):
    assert word in {l.lower() for l in cfg["content"]["callout_icons"][icon]}


# --- joining a label written without spaces ----------------------------------------------------------

def test_a_cjk_label_arrives_one_character_at_a_time(tmp_path, cfg):
    """What the folder has to cope with - and that it does."""
    d = extract.load(_ja(tmp_path / "s.pdf", label="注記:"), "stage", cfg)
    assert [w.text for w in d.words if 195 < w.bbox[1] < 215][:3] == ["注", "記", ":"]
    normalize.fold_labels(d, cfg["content"]["label_words"])
    folded = [w.norm for w in d.words if w.norm.startswith("<label:")]
    assert folded == ["<label:注記>"], folded


def test_the_colon_after_it_goes_with_it(tmp_path, cfg):
    d = extract.load(_ja(tmp_path / "s.pdf", label="注記:"), "stage", cfg)
    normalize.fold_labels(d, cfg["content"]["label_words"])
    assert not [w for w in d.words if w.norm == ":" and 195 < w.bbox[1] < 215]


def test_a_word_that_merely_starts_with_a_label_is_left_alone(cfg):
    """注意 is a label; 注意事項 is a heading that begins with it."""
    import types
    ws = [types.SimpleNamespace(text=ch, norm=ch, line=0) for ch in "注意事項"]
    doc = types.SimpleNamespace(words=ws)
    normalize.fold_labels(doc, cfg["content"]["label_words"])
    assert [w.norm for w in ws] == ["<label:注意>", "", "事", "項"]


# --- end to end ---------------------------------------------------------------------------------------

def test_a_japanese_label_matching_prods_icon_is_not_extra_content(tmp_path, cfg):
    """The report from the manual: prod draws the pencil, stage writes 注記: in front of the same
    sentence. The content is there and the icon says NOTE, so there is nothing to report."""
    r = engine.compare(_ja(tmp_path / "a.pdf", label=None), _ja(tmp_path / "b.pdf", label="注記:"), cfg)
    content = [f for s in r["sections"] for f in s["findings"] if f["check"] == "content"]
    assert not content, [f["message"] for f in content]


def test_the_same_page_on_both_sides_is_still_clean(tmp_path, cfg):
    r = engine.compare(_ja(tmp_path / "a.pdf", label="注記:"), _ja(tmp_path / "b.pdf", label="注記:"), cfg)
    assert not [f for s in r["sections"] for f in s["findings"] if f["check"] == "content"]


def test_a_label_the_icon_does_not_stand_for_is_still_reported(tmp_path, cfg):
    """Only a label the icon carries is silent: 警告 (WARNING) against a pencil is not."""
    r = engine.compare(_ja(tmp_path / "a.pdf", label=None), _ja(tmp_path / "b.pdf", label="警告:"), cfg)
    got = [f for s in r["sections"] for f in s["findings"] if f["check"] == "content"]
    assert got, "a mismatched label must not be dropped"
