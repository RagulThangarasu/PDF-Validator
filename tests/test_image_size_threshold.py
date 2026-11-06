"""A picture's size is a finding when it is noticeably different, and the two directions have their
own threshold: the stage picture MORE than 10 % under the prod picture's width
([assets] smaller_ratio_tolerance = 0.10), or 30 % or more over it (bigger_ratio_tolerance = 0.30).

A picture drawn too small to read is a defect long before one drawn a little large is, which is why
the thresholds are not the same number.

width_tolerance is OR-ed with those ratios, so it has to stay out of the way (1.0) or a 10 %-of-content
shift would fire first and the ratios would never decide anything.
"""
import pytest

from pdfval import engine
from pdfval.checks.assets import size_reported


@pytest.fixture(scope="module")
def acfg():
    return engine.load_config()["assets"]


@pytest.mark.parametrize("stage,reported", [
    (0.784, False),  # -2 %: a rendering-level difference, not a real size change
    (0.752, False),  # -6 %: under the 10 % the smaller side now asks for
    (0.720, False),  # -10 % exactly: the rule is MORE than 10 % smaller
    (0.712, True),   # -11 %: past it
    (0.40, True),    # -50 %
    (0.20, True),    # -75 %
    (0.824, False),  # +3 %
    (0.856, False),  # +7 %: a growth this small is not reported
    (1.00, False),   # +25 %
    (1.04, True),    # +30 % exactly: the bigger side reports at the threshold
    (1.20, True),    # +50 %
    (1.60, True),    # +100 %: half again and more
])
def test_each_direction_uses_its_own_threshold(acfg, stage, reported):
    assert size_reported(0.80, stage, acfg) is reported


def test_the_two_directions_are_deliberately_not_symmetric(acfg):
    """-20 % is reported, +20 % is not: shrinking hurts the reader sooner than growing."""
    assert size_reported(0.50, 0.40, acfg)
    assert not size_reported(0.50, 0.60, acfg)


def test_the_configured_thresholds_are_the_ones_that_decide(acfg):
    assert acfg["smaller_ratio_tolerance"] == 0.10
    assert acfg["bigger_ratio_tolerance"] == 0.30
    assert acfg["width_tolerance"] >= 1.0, "width_tolerance must not fire before the ratios do"


def test_an_unset_direction_falls_back_to_the_symmetric_setting():
    """An old config carrying only size_ratio_tolerance keeps behaving as it always did."""
    old = {"size_ratio_tolerance": 0.05, "width_tolerance": 1.0}
    assert size_reported(0.80, 0.752, old) and size_reported(0.80, 0.856, old)


def test_a_missing_prod_width_is_not_a_size_finding(acfg):
    assert size_reported(0.0, 0.5, acfg) is False
