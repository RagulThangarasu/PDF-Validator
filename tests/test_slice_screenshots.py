"""Slice screenshots of a web page, when the page is not the height it said it was.

The cuts are measured from the DOM; Playwright clips against the image it has just rendered. A page
that shrinks in between (lazy content unloaded, a banner collapsed, an animation settling) leaves a
cut past the end of the image, and the clip fails with "Clipped area is either empty or outside the
resulting image" - which used to lose the whole page over its last few hundred pixels.
"""
import io

import pytest
from PIL import Image as PILImage

from pdfval.html_source import _shoot_slices


def _png(w: int, h: int) -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", (w, h), (255, 255, 255)).save(buf, "PNG")
    return buf.getvalue()


class _Page:
    """A page whose real image is `real_h` tall, however tall the DOM claimed to be."""

    def __init__(self, real_h: int, width: int = 800):
        self.real_h, self.width, self.clipped, self.full = real_h, width, 0, 0

    def screenshot(self, clip=None, full_page=True, timeout=None):
        if clip is None:
            self.full += 1
            return _png(self.width, self.real_h)
        self.clipped += 1
        if clip["y"] + clip["height"] > self.real_h or clip["height"] <= 0:
            raise RuntimeError("Page.screenshot: Clipped area is either empty or outside "
                               "the resulting image")
        return _png(int(clip["width"]), int(clip["height"]))


def _sizes(slices):
    return [PILImage.open(io.BytesIO(s)).size for s in slices]


def test_slices_are_clipped_one_by_one_when_the_page_is_the_height_it_said(_=None):
    page = _Page(real_h=3000)
    cuts, slices = _shoot_slices(page, 800, 3000, [0, 1000, 2000, 3000])
    assert cuts == [0, 1000, 2000, 3000]
    assert _sizes(slices) == [(800, 1000), (800, 1000), (800, 1000)]
    assert page.clipped == 3 and page.full == 0


def test_a_page_shorter_than_it_claimed_still_yields_every_slice():
    """The whole page used to be lost here. Now one full-page shot is cut up instead."""
    page = _Page(real_h=2400)                       # the DOM said 3000
    cuts, slices = _shoot_slices(page, 800, 3000, [0, 1000, 2000, 3000])
    assert slices, "the page must still be captured"
    assert len(slices) == len(cuts) - 1, "the caller pairs cuts with slices: they must stay aligned"
    assert all(PILImage.open(io.BytesIO(s)).height > 0 for s in slices), "no empty slice"
    assert page.full == 1, "one full-page shot, not another round of failing clips"


def test_the_fallback_covers_the_whole_image_without_overlapping():
    page = _Page(real_h=2400)
    cuts, slices = _shoot_slices(page, 800, 3000, [0, 1000, 2000, 3000])
    heights = [PILImage.open(io.BytesIO(s)).height for s in slices]
    assert sum(heights) == 2400, (heights, cuts)


def test_a_page_with_no_height_is_shot_whole_rather_than_clipped():
    page = _Page(real_h=600)
    cuts, slices = _shoot_slices(page, 800, 0, [0, 0])
    assert len(slices) == 1 and page.clipped == 0
    assert PILImage.open(io.BytesIO(slices[0])).height == 600


def test_an_error_that_is_not_the_clip_is_not_swallowed():
    class _Broken(_Page):
        def screenshot(self, clip=None, full_page=True, timeout=None):
            raise RuntimeError("Timeout 30000ms exceeded")

    with pytest.raises(RuntimeError, match="Timeout"):
        _shoot_slices(_Broken(3000), 800, 3000, [0, 1000, 3000])
