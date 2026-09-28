"""PDF-specific helpers: rasterisation (for analysis) and cropping (for output).

**Critical design point**: cropping is implemented with PyMuPDF's
``Page.set_cropbox`` rather than ``get_pixmap + insert_image``.  The latter
would destroy vector content; the former preserves it perfectly because the
output PDF still references the original page's drawing operators.
"""

from __future__ import annotations

from pathlib import Path
from typing import Tuple

import numpy as np
import pymupdf as fitz


def render_page_to_array(
    doc: fitz.Document,
    page_index: int,
    dpi: int,
) -> Tuple[np.ndarray, Tuple[float, float]]:
    """Rasterise a single page at the requested DPI.

    Args:
        doc: An open :class:`fitz.Document`.
        page_index: Zero-based page index.
        dpi: Render resolution.

    Returns:
        Tuple ``(rgb_array, (page_width_pt, page_height_pt))``.
        ``rgb_array`` is an HxWx3 uint8 array in **RGB** order (the
        convention used by the rest of the pipeline).

    Raises:
        IndexError: If ``page_index`` is out of range.
    """
    if page_index < 0 or page_index >= len(doc):
        raise IndexError(
            f"page_index={page_index} out of range (document has {len(doc)} pages)"
        )
    if dpi <= 0:
        raise ValueError("dpi must be positive")

    page = doc[page_index]
    page_w_pt = float(page.rect.width)
    page_h_pt = float(page.rect.height)

    # ``colorspace=fitz.csRGB`` ensures 3-channel output we can rely on.
    pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csRGB, alpha=False)
    rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    # PyMuPDF returns RGB already; copy so callers may mutate freely.
    rgb = rgb.copy()
    return rgb, (page_w_pt, page_h_pt)


def crop_and_save_pdf(
    src_path: Path,
    page_index: int,
    rect: fitz.Rect,
    out_path: Path,
) -> None:
    """Copy one page of ``src_path`` to ``out_path`` with a tight CropBox.

    The page is copied (not extracted) so that fonts, color profiles, and
    other resources remain intact.  Only the CropBox changes, which means
    all drawing operators -- text, paths, images -- are preserved as
    vector data.  This is the central reason the tool is useful for
    scientific-paper workflows.

    Args:
        src_path: Path to the source PDF.
        page_index: Zero-based page index to copy.
        rect: PDF-space rect (in points) that becomes the new CropBox, in
            **PDF user space** -- origin at the bottom-left of the page, y
            increasing upward.  This is the convention used everywhere else in
            this package (:mod:`pdf_auto_crop.geometry` in particular), so a
            rect coming straight out of either detector needs no conversion.
        out_path: Destination file path.  Any existing file is overwritten.

    Raises:
        ValueError: If ``rect`` lies entirely outside the page.

    Note:
        PyMuPDF's own coordinate system has its origin at the **top-left**,
        so ``Page.set_cropbox`` reads its argument in top-down terms.  The
        conversion happens here, at the single boundary where bottom-up user
        space meets top-down MuPDF space; without it every crop is silently
        mirrored about the page's horizontal centre line, which looks correct
        for a roughly centred figure and lands on entirely the wrong artwork
        for a multi-figure page.
    """
    src = fitz.open(src_path)
    try:
        if page_index < 0 or page_index >= len(src):
            raise IndexError(
                f"page_index={page_index} out of range "
                f"(document has {len(src)} pages)"
            )
        out = fitz.open()
        try:
            out.insert_pdf(src, from_page=page_index, to_page=page_index)
            page = out[0]
            page_h = float(page.rect.height)
            # bottom-up user space -> top-down PyMuPDF space
            mupdf_rect = fitz.Rect(
                rect.x0, page_h - rect.y1, rect.x1, page_h - rect.y0
            )
            page.set_cropbox(mupdf_rect)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out.save(str(out_path), garbage=4, deflate=True)
        finally:
            out.close()
    finally:
        src.close()


__all__ = ["render_page_to_array", "crop_and_save_pdf"]