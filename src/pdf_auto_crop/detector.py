"""High-level figure detection orchestrator.

The :func:`detect_figures` function ties together the image-processing and
geometry helpers into a single pipeline that takes a rasterised page and
returns a list of PDF-space rectangles.
"""

from __future__ import annotations

from typing import List, Tuple

import numpy as np
import pymupdf as fitz

from .config import CropConfig
from .geometry import raster_bbox_to_pdf_rect
from .image_processor import (
    binarize,
    compute_adaptive_params,
    filter_components,
    find_components,
    merge_close_boxes,
    morph_close,
    refine_bbox,
)


def detect_figures(
    rgb: np.ndarray,
    page_size_pt: Tuple[float, float],
    config: CropConfig,
) -> List[fitz.Rect]:
    """Detect figure bounding boxes on a rasterised PDF page.

    The pipeline is:

    1. Binarise the RGB raster so non-white pixels become foreground.
    2. Morphological close to bridge small gaps inside figures.
    3. Connected-component labelling to find candidate blobs.
    4. Merge blobs whose dilated rectangles overlap (handles "tight" gaps).
    5. Filter by area / width / height.
    6. Re-snap each remaining blob onto the **raw** ink inside it so the
       tightest possible crop is taken (this compensates for the dilation
       applied during closing).
    7. Convert to PDF point space and sort top-left to bottom-right.

    Args:
        rgb: HxWx3 uint8 raster of a single PDF page.
        page_size_pt: ``(width_pt, height_pt)`` of the PDF page.
        config: Active :class:`CropConfig`.

    Returns:
        A list of :class:`fitz.Rect` (in PDF point space, sorted top-down).
    """
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError(f"rgb must be HxWx3, got shape {rgb.shape}")
    if len(page_size_pt) != 2:
        raise ValueError("page_size_pt must be a 2-tuple")
    page_w_pt, page_h_pt = page_size_pt
    if page_w_pt <= 0 or page_h_pt <= 0:
        raise ValueError("page size must be positive")

    raw_mask = binarize(rgb, config.threshold)
    params = compute_adaptive_params(config.dpi, config)

    closed_mask = morph_close(raw_mask, params["close_kernel"])
    components = find_components(closed_mask)
    merged = merge_close_boxes(components, params["merge_gap_px"])
    survivors = filter_components(merged, params)

    rects: List[fitz.Rect] = []
    for bbox in survivors:
        tight = refine_bbox(raw_mask, bbox)
        pdf_rect = raster_bbox_to_pdf_rect(tight, config.dpi, page_h_pt)
        rects.append(pdf_rect)

    rects.sort(key=lambda r: (round(r.y0, 1), round(r.x0, 1)))
    return rects


__all__ = ["detect_figures"]