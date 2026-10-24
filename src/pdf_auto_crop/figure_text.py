"""Folding a figure's own labels into the box that was detected.

The detector works on a raster of the page.  What it finds is the **ink** of
the plot: the frame, the curves, the legend.  The axis ticks, the axis titles
and the figure's title are separate ink components a millimetre or two away,
so they are not part of the detected box -- and a crop taken from that box
loses them.  That is how a curve plot ends up as a rectangle with no axes.

The page's own text says where those labels are.  This module uses it: the
text blocks that sit next to a detected region *belong to* that region and
are folded into it.

Two things must not be folded in:

* the **caption** ("Fig. 3: ...") -- it belongs to the figure but is not part
  of it, and it is usually the widest text on the page;
* the **body text** of the paper, which is far away but sometimes wide.

Both are told apart from an axis label by their width and by the way they
start: a caption names the figure.

Everything here works in the page's **top-down** frame -- the frame MuPDF
renders and the frame ``Page.get_text`` reports in.  The detectors speak
bottom-up PDF user space, so the conversion happens at the call site.
"""

from __future__ import annotations

import re
from typing import List, Optional, Sequence, Tuple

import pymupdf as fitz

#: A caption names its figure.  Both the western and the Chinese forms are
#: recognised, because a Chinese thesis uses the latter.
CAPTION_PATTERN = re.compile(
    r"^\s*(fig(?:ure)?[\s.]*\d|table[\s.]*\d|图\s*\d|表\s*\d|附图\s*\d)",
    re.IGNORECASE,
)

#: How close a text block has to be to the region, at least.
MIN_MARGIN_PT = 3.0 * 72.0 / 25.4      # 3 mm

#: ... and at most, however big the figure is.
MAX_MARGIN_PT = 12.0 * 72.0 / 25.4     # 12 mm

#: The margin also scales with the figure, so that a wide figure can have a
#: wide axis title -- but never by more than :data:`MAX_MARGIN_PT`.
MARGIN_RATIO = 0.15

#: A text block this much wider than the figure is a caption or body text,
#: not an axis label.
WIDE_RATIO = 1.6


def is_caption(text: str) -> bool:
    """Whether a text block looks like a figure or table caption."""
    return bool(CAPTION_PATTERN.match(text or ""))


def text_blocks(page: fitz.Page) -> List[Tuple[fitz.Rect, str]]:
    """The page's text blocks, in the page's top-down frame."""
    out: List[Tuple[fitz.Rect, str]] = []
    for block in page.get_text("blocks"):
        if len(block) < 5:
            continue
        out.append((fitz.Rect(block[:4]), block[4]))
    return out


def _gap(a: fitz.Rect, b: fitz.Rect) -> float:
    """How far apart two rectangles are; 0 when they touch or overlap.

    The distance is measured along the axis on which they are apart: a label
    that sits to the left of the figure is 3 mm away even when it is far
    above or below it.
    """
    dx = max(0.0, max(a.x0 - b.x1, b.x0 - a.x1))
    dy = max(0.0, max(a.y0 - b.y1, b.y0 - a.y1))
    return max(dx, dy)


def _is_wide_line(rect: fitz.Rect, region: fitz.Rect) -> bool:
    """Whether a text block is a full-width line rather than a label.

    A caption runs the width of the column and sits below the figure; the
    axis labels are short.  The block has to be *outside* the figure for the
    rule to apply -- a wide block that overlaps the figure is part of it.
    """
    if rect.intersects(region):
        return False
    return rect.width > region.width * WIDE_RATIO


def _belongs_to_other(rect: fitz.Rect, others: Sequence[fitz.Rect]) -> bool:
    """Whether a text block already sits inside another detected figure."""
    for other in others:
        if other.contains(rect) or other.intersects(rect):
            return True
    return False


def expand_region_with_text(
    region: fitz.Rect,
    blocks: Sequence[Tuple[fitz.Rect, str]],
    *,
    others: Sequence[fitz.Rect] = (),
    margin_pt: Optional[float] = None,
) -> fitz.Rect:
    """Grow a detected region to include the text that belongs to it.

    Args:
        region: The detected region, in the page's top-down frame.
        blocks: The page's text blocks, as returned by :func:`text_blocks`.
        others: The other regions on the page; text inside them belongs to
            them, not to this one.
        margin_pt: How far a label may sit from the region.  By default it
            scales with the figure, between :data:`MIN_MARGIN_PT` and
            :data:`MAX_MARGIN_PT`.

    Returns:
        The grown region.  The original region is returned unchanged when
        nothing belongs to it.
    """
    out = fitz.Rect(region)
    margin = margin_pt
    if margin is None:
        margin = min(max(MIN_MARGIN_PT, MARGIN_RATIO * out.width),
                     MAX_MARGIN_PT)

    changed = True
    while changed:
        changed = False
        for rect, text in blocks:
            if rect.is_empty or out.contains(rect):
                continue
            if _gap(out, rect) > margin:
                continue
            if is_caption(text):
                continue
            if _is_wide_line(rect, out):
                continue
            if _belongs_to_other(rect, others):
                continue
            out |= rect
            changed = True
    return out


def expand_regions_with_text(
    regions: Sequence[fitz.Rect],
    blocks: Sequence[Tuple[fitz.Rect, str]],
    *,
    margin_pt: Optional[float] = None,
) -> List[fitz.Rect]:
    """Grow every detected region to include the text that belongs to it.

    Args:
        regions: The detected regions, in the page's top-down frame.
        blocks: The page's text blocks.
        margin_pt: See :func:`expand_region_with_text`.

    Returns:
        One region per input region, in the same order.
    """
    out: List[fitz.Rect] = []
    for i, region in enumerate(regions):
        others = [fitz.Rect(r) for j, r in enumerate(regions) if j != i]
        out.append(expand_region_with_text(
            fitz.Rect(region), blocks, others=others, margin_pt=margin_pt))
    return out


__all__ = [
    "CAPTION_PATTERN",
    "expand_region_with_text",
    "expand_regions_with_text",
    "is_caption",
    "text_blocks",
]
