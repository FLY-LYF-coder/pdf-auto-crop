"""Preview image generation.

Produces a PNG with detection rectangles overlaid on the page raster so users
can sanity-check the algorithm's choices before committing to crops.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import cv2
import numpy as np
import pymupdf as fitz

from .geometry import raster_bbox_to_pdf_rect


# BGR colours used for drawing.
_COLOR_RED = (0, 0, 255)
_COLOR_GREEN = (0, 255, 0)
_COLOR_BLUE = (255, 0, 0)
_LINE_THICKNESS = 3
_TEXT_SCALE = 1.0
_TEXT_THICKNESS = 2


def draw_preview(
    rgb: np.ndarray,
    rects: List[fitz.Rect],
    dpi: int,
    labels: Optional[List[str]] = None,
) -> np.ndarray:
    """Draw detection rectangles onto a copy of ``rgb``.

    Args:
        rgb: HxWx3 uint8 RGB raster of the page.
        rects: PDF-space rectangles returned by :func:`detect_figures`.
        dpi: Render DPI used for the raster (needed to convert back to pixels).
        labels: Optional list of strings the same length as ``rects``.

    Returns:
        A new HxWx3 uint8 RGB array with boxes drawn on top.
    """
    if rects is None:
        raise ValueError("rects must not be None")
    if labels is not None and len(labels) != len(rects):
        raise ValueError("labels length must match rects length")

    page_h_pt = float(rgb.shape[0]) * 72.0 / dpi
    out = rgb.copy()
    bgr = cv2.cvtColor(out, cv2.COLOR_RGB2BGR)

    for idx, rect in enumerate(rects):
        bbox = _pdf_rect_to_pixel_bbox(rect, dpi, page_h_pt, rgb.shape)
        x0, y0, x1, y1 = bbox
        cv2.rectangle(bgr, (x0, y0), (x1, y1), _COLOR_RED, _LINE_THICKNESS)
        if labels is not None:
            text = labels[idx]
        else:
            text = f"FIG {idx + 1}"
        # Place text just above the box, falling back to inside if no room.
        ty = max(y0 - 6, 20)
        cv2.putText(
            bgr,
            text,
            (x0, ty),
            cv2.FONT_HERSHEY_SIMPLEX,
            _TEXT_SCALE,
            _COLOR_GREEN,
            _TEXT_THICKNESS,
            cv2.LINE_AA,
        )

    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)


def save_preview(rgb: np.ndarray, out_path: Path) -> None:
    """Save a preview array as a PNG on disk.

    Args:
        rgb: HxWx3 uint8 RGB image.
        out_path: Destination ``.png`` path.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    cv2.imwrite(str(out_path), bgr)


def _pdf_rect_to_pixel_bbox(
    rect: fitz.Rect,
    dpi: int,
    page_h_pt: float,
    image_shape: tuple,
) -> tuple:
    """Inverse of :func:`raster_bbox_to_pdf_rect` for preview drawing."""
    scale = dpi / 72.0
    h, w = image_shape[:2]

    x0 = int(round(rect.x0 * scale))
    x1 = int(round(rect.x1 * scale))
    # y in PDF space is bottom-up; flip to top-down pixel coordinates.
    y_top = int(round((page_h_pt - rect.y1) * scale))
    y_bot = int(round((page_h_pt - rect.y0) * scale))

    # Clamp.
    x0 = max(0, min(w, x0))
    x1 = max(0, min(w, x1))
    y_top = max(0, min(h, y_top))
    y_bot = max(0, min(h, y_bot))
    return (x0, y_top, x1, y_bot)


__all__ = ["draw_preview", "save_preview"]