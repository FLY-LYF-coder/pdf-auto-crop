"""Geometry helpers bridging raster pixel coordinates and PDF point space.

All functions in this module are **pure**: given the same inputs they produce
the same outputs without mutating any external state.  This makes them easy to
unit test in isolation.
"""

from __future__ import annotations

from typing import Tuple

import pymupdf as fitz


# Conversion factor: 1 inch = 25.4 mm = 72 pt  =>  1 mm = 72/25.4 pt.
MM_TO_PT: float = 72.0 / 25.4


def mm_to_pt(mm: float) -> float:
    """Convert millimetres to PDF points (1 pt = 1/72 inch).

    Args:
        mm: Length in millimetres.  May be negative (used for relative deltas).

    Returns:
        Equivalent length in PDF points.
    """
    return mm * MM_TO_PT


def raster_bbox_to_pdf_rect(
    bbox: Tuple[int, int, int, int],
    dpi: int,
    page_height_pt: float,
) -> fitz.Rect:
    """Convert a pixel bounding box (top-left origin) into a PDF rect.

    The PyMuPDF rasteriser returns pixels with ``(0,0)`` at the **top-left**.
    PDF itself uses a bottom-left origin.  This helper does the necessary flip
    and unit conversion so that downstream code can place a CropBox.

    Args:
        bbox: ``(x0, y0, x1, y1)`` in pixel coordinates where ``y0`` is the
            top edge and ``y1`` is the bottom edge.
        dpi: Render DPI used to produce the pixels.
        page_height_pt: PDF page height in points.

    Returns:
        A :class:`fitz.Rect` expressed in PDF point space with origin at the
        bottom-left.
    """
    if page_height_pt <= 0:
        raise ValueError("page_height_pt must be positive")
    if dpi <= 0:
        raise ValueError("dpi must be positive")
    if bbox[2] < bbox[0] or bbox[3] < bbox[1]:
        raise ValueError(f"Invalid bbox: x1<x0 or y1<y0 in {bbox!r}")

    scale = dpi / 72.0
    x0_px, y0_px, x1_px, y1_px = bbox

    pdf_x0 = x0_px / scale
    pdf_x1 = x1_px / scale
    pdf_y0 = page_height_pt - y1_px / scale
    pdf_y1 = page_height_pt - y0_px / scale

    return fitz.Rect(pdf_x0, pdf_y0, pdf_x1, pdf_y1)


def add_margin_pt(
    rect: fitz.Rect,
    margin_pt: float,
    page_rect: fitz.Rect,
) -> fitz.Rect:
    """Inflate ``rect`` by ``margin_pt`` on every side, clamping to the page.

    Useful for adding visual whitespace around a detected figure so that any
    tick marks or labels that extend slightly beyond the strict bounding box
    are not clipped.

    Args:
        rect: Original PDF rect to expand.
        margin_pt: Padding in points (positive expands, negative contracts).
        page_rect: Page bounds used for clamping.

    Returns:
        A new :class:`fitz.Rect` that lies within ``page_rect``.
    """
    expanded = fitz.Rect(
        rect.x0 - margin_pt,
        rect.y0 - margin_pt,
        rect.x1 + margin_pt,
        rect.y1 + margin_pt,
    )
    return clamp_rect(expanded, page_rect)


def clamp_rect(rect: fitz.Rect, page_rect: fitz.Rect) -> fitz.Rect:
    """Intersect ``rect`` with ``page_rect`` and return a fresh copy.

    Empty intersections raise :class:`ValueError` because an empty CropBox
    produces a malformed PDF page.

    Args:
        rect: Any :class:`fitz.Rect`.
        page_rect: Page bounds.

    Returns:
        A new :class:`fitz.Rect` guaranteed to be a non-empty subset of the
        page rect.
    """
    x0 = max(rect.x0, page_rect.x0)
    y0 = max(rect.y0, page_rect.y0)
    x1 = min(rect.x1, page_rect.x1)
    y1 = min(rect.y1, page_rect.y1)
    if x1 <= x0 or y1 <= y0:
        raise ValueError(
            f"Rect {rect} does not intersect page rect {page_rect} after clamping"
        )
    return fitz.Rect(x0, y0, x1, y1)


__all__ = [
    "MM_TO_PT",
    "mm_to_pt",
    "raster_bbox_to_pdf_rect",
    "add_margin_pt",
    "clamp_rect",
]