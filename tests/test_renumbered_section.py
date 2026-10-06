"""A section under another number in stage (“Appendix 3: … for X-Sign” -> “Appendix 4: … for X-Sign”, the
appendices in another order) is the same section with a different heading - not a section missing."""
from pdfval import normalize
from pdfval.sections import _unnumbered


def _u(title):
    return _unnumbered(normalize.title(title))


def test_the_same_title_under_another_number_is_one_section():
    assert _u("Appendix 3: Basic Troubleshooting Checklists for X-Sign") == _u("Appendix 4: Basic Troubleshooting Checklists for X-Sign")


def test_another_title_under_the_same_number_is_another_section():
    assert _u("Appendix 4: Basic Troubleshooting Checklists for X-Sign") != _u("Appendix 4: Basic Troubleshooting Checklists for InstaShare 2")


def test_a_title_without_a_number_or_too_short_is_not_paired_this_way():
    assert _u("A quick start guide") is None
    assert _u("Appendix 2: Notes") is None
