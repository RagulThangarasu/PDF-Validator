"""A table header repeated on the continuation page ends where the header ends. The first row of the
table's part on the earlier page may start with the same word as this page's first row (“OFF | HDR
content” there, “OFF (grayed out) | Non-HDR content” here): that word is this page's data, not header."""
from types import SimpleNamespace

from pdfval.checks import content


def _doc(rows):
    """rows: (page, y, [(x, cell text)])"""
    words = []
    for page, y, cells in rows:
        for x, text in cells:
            for t in text.split():
                words.append(SimpleNamespace(page=page, text=t, norm=t.lower(), bbox=(x, y, x + 5 * len(t), y + 10),
                                             style=SimpleNamespace(size=9)))
                x += 5 * len(t) + 3
    return SimpleNamespace(words=words)


HEAD = [(72, "HDR Demo"), (190, "Video input"), (290, "Availability of HDR function"), (480, "OSD")]
HEAD2 = [(72, "option"), (480, "message")]


def _table(first_cell_p2):
    return _doc([
        (0, 700, HEAD), (0, 712, HEAD2),
        (0, 735, [(72, "OFF"), (190, "HDR content"), (290, "Switches to HDR mode automatically."), (480, "HDR: On")]),
        (1, 57, HEAD), (1, 69, HEAD2),
        (1, 98, [(72, first_cell_p2), (190, "Non-HDR content"), (290, "Emulated HDR function"), (480, "HDR: Emulated")]),
    ])


def _run(d):
    idx = [i for i, w in enumerate(d.words) if w.page == 1]
    return [d.words[i].text for i in content._repeated_header_rows(d, idx)]


def test_the_first_data_cell_is_not_part_of_the_repeated_header():
    run = _run(_table("OFF (grayed out)"))
    assert run and run[-1] == "message" and "OFF" not in run


def test_the_header_alone_is_still_found():
    run = _run(_table("ON"))
    assert run and run[0] == "HDR" and run[-1] == "message"
