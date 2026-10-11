"""A picture's size is a finding whenever it is noticeably different, not only when it is grossly so:
the stage picture at 95 % of the prod picture's width or less, or at 105 % or more
([assets] size_ratio_tolerance = 0.05).

width_tolerance is OR-ed with that ratio, so it has to stay out of the way (1.0) or a 10 %-of-content
shift would fire first and the ratio would never decide anything.
"""
import pytest

from pdfval import engine
from pdfval.checks.assets import size_reported


@pytest.fixture(scope="module")
def acfg():
    return engine.load_config()["assets"]


@pytest.mark.parametrize("stage,reported", [
    (0.784, False),  # -2 %: a rendering-level difference, not a real size change
    (0.824, False),  # +3 %
    (0.752, True),   # -6 %: past the 5 % tolerance
    (0.856, True),   # +7 %
    (0.40, True),    # -50 %
    (0.20, True),    # -75 %
    (1.20, True),    # +50 %
    (1.60, True),    # +100 %: half again and more
])
def test_any_size_difference_past_5_percent_is_reported(acfg, stage, reported):
    assert size_reported(0.80, stage, acfg) is reported


def test_both_directions_use_the_same_threshold(acfg):
    assert size_reported(0.50, 0.20, acfg) and size_reported(0.20, 0.50, acfg)


def test_the_configured_threshold_is_the_one_that_decides(acfg):
    assert acfg["size_ratio_tolerance"] == 0.05
    assert acfg["width_tolerance"] >= 1.0, "width_tolerance must not fire before the ratio does"


def test_a_missing_prod_width_is_not_a_size_finding(acfg):
    assert size_reported(0.0, 0.5, acfg) is False
