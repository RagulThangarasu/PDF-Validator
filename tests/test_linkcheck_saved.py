"""A link check kept aside as the “before” report: runs/_linkcheck/before/.

The next check overwrites nothing there, so the state the guides were in before the fixes is still on
disk tomorrow, and a later run can be read against it.
"""
import json

import pytest

from pdfval.app.server import LinkChecker


class FakeJobs:
    def __init__(self, root):
        self.dir = root


def _run_folder(root, name, failed=7):
    d = root / "_linkcheck" / name
    d.mkdir(parents=True)
    for f in ("link-check.pdf", "link-check-failures.pdf", "link-check.csv"):
        (d / f).write_bytes(b"%PDF-1.4 report")
    (d / "link-check.json").write_text(json.dumps({"guides": [], "totals": {"failed": failed, "links": 20}}))
    return d


@pytest.fixture
def lc(tmp_path):
    return LinkChecker(FakeJobs(tmp_path))


def test_a_run_is_copied_into_the_before_folder(lc, tmp_path):
    _run_folder(tmp_path, "20261014-112142")
    got = lc.save("before")
    dest = tmp_path / "_linkcheck" / "before"
    assert sorted(f.name for f in dest.glob("link-check*")) == [
        "link-check-failures.pdf", "link-check.csv", "link-check.json", "link-check.pdf"]
    assert got["run"] == "20261014-112142"
    assert got["totals"]["failed"] == 7


def test_the_original_run_folder_is_left_where_it_is(lc, tmp_path):
    src = _run_folder(tmp_path, "20261014-112142")
    lc.save("before")
    assert (src / "link-check.pdf").exists()


def test_a_named_run_can_be_saved_instead_of_the_newest(lc, tmp_path):
    _run_folder(tmp_path, "20261014-090000", failed=3)
    _run_folder(tmp_path, "20261014-120000", failed=9)
    assert lc.save("before", "20261014-090000")["totals"]["failed"] == 3


def test_the_newest_run_is_the_one_saved_by_default(lc, tmp_path):
    _run_folder(tmp_path, "20261014-090000", failed=3)
    newer = _run_folder(tmp_path, "20261014-120000", failed=9)
    import os, time
    os.utime(newer, (time.time() + 10, time.time() + 10))
    assert lc.save("before")["totals"]["failed"] == 9


def test_saving_again_replaces_the_folder_instead_of_mixing_two_runs(lc, tmp_path):
    _run_folder(tmp_path, "20261014-090000", failed=3)
    lc.save("before")
    (tmp_path / "_linkcheck" / "before" / "link-check-old-extra.pdf").write_bytes(b"stale")
    _run_folder(tmp_path, "20261014-120000", failed=9)
    lc.save("before", "20261014-120000")
    dest = tmp_path / "_linkcheck" / "before"
    assert not (dest / "link-check-old-extra.pdf").exists()
    assert json.loads((dest / "meta.json").read_text())["run"] == "20261014-120000"


def test_before_and_after_are_kept_apart(lc, tmp_path):
    _run_folder(tmp_path, "20261014-090000", failed=12)
    lc.save("before")
    _run_folder(tmp_path, "20261014-150000", failed=2)
    lc.save("after", "20261014-150000")
    saved = lc.status()["saved"]
    assert saved["before"]["totals"]["failed"] == 12
    assert saved["after"]["totals"]["failed"] == 2


def test_an_unknown_folder_name_is_refused(lc, tmp_path):
    _run_folder(tmp_path, "20261014-090000")
    with pytest.raises(ValueError, match="Unknown report folder"):
        lc.save("../../etc")


def test_saving_with_no_run_at_all_says_so(lc, tmp_path):
    (tmp_path / "_linkcheck").mkdir(parents=True)
    with pytest.raises(ValueError, match="no link check to save"):
        lc.save("before")


def test_nothing_is_reported_as_saved_before_anything_is(lc, tmp_path):
    assert lc.status()["saved"] == {}
