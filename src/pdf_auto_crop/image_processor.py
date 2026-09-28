"""Image processing primitives for figure detection.

These helpers wrap OpenCV calls behind small, intention-revealing functions.
Each function returns new arrays -- the inputs are never mutated in place so
callers can reason about data flow locally.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

import cv2
import numpy as np

from .config import CropConfig


def binarize(rgb: np.ndarray, threshold: int) -> np.ndarray:
    """Convert an RGB image into a foreground mask (white = background).

    The convention used throughout the pipeline is:

    * ``0``  -- background (white in the original page).
    * ``255`` -- foreground (anything with ink: text, lines, fills).

    Args:
        rgb: HxWx3 uint8 RGB image (or BGR; only luminance matters).
        threshold: Pixels whose grayscale value exceeds this are treated as
            background.  ``245`` matches the bright-white typical of vector
            figures exported from PowerPoint.

    Returns:
        A HxW uint8 mask with values ``0`` or ``255``.
    """
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError(f"binarize expects an HxWx3 image, got shape {rgb.shape}")
    if not 0 <= threshold <= 255:
        raise ValueError(f"threshold must be 0..255, got {threshold}")

    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    # ``THRESH_BINARY_INV`` because we want non-white (= dark ink) to be 255.
    _, mask = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY_INV)
    return mask


def morph_close(mask: np.ndarray, kernel_size: int) -> np.ndarray:
    """Apply a binary closing with a square kernel.

    Closing (dilate then erode) bridges small gaps inside figure outlines so
    that a single complex figure (e.g. a finite-element mesh with text inside
    its outlines) does not get fragmented.

    Args:
        mask: Binary uint8 mask (0 or 255).
        kernel_size: Side length of the square kernel.  Will be clamped to
            ``>= 1`` and made odd if even (OpenCV's ``getStructuringElement``
            tolerates either, but odd is conventional).

    Returns:
        A new uint8 mask of identical shape.
    """
    if kernel_size < 1:
        raise ValueError("kernel_size must be >= 1")
    side = int(kernel_size)
    if side % 2 == 0:
        side += 1
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (side, side))
    return cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)


def find_components(mask: np.ndarray) -> List[Tuple[int, int, int, int]]:
    """Return connected components as ``(x, y, w, h)`` tuples.

    The background component (label ``0``) is discarded.  Coordinates are
    inclusive of the pixel grid and use the standard OpenCV origin
    (top-left).

    Args:
        mask: Binary uint8 mask.

    Returns:
        A list of ``(x, y, w, h)`` bounding boxes, possibly empty.
    """
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    out: List[Tuple[int, int, int, int]] = []
    for label in range(1, num_labels):
        x, y, w, h, area = stats[label]
        if area <= 0:
            continue
        out.append((int(x), int(y), int(w), int(h)))
    return out


def refine_bbox(
    raw_mask: np.ndarray,
    initial_bbox: Tuple[int, int, int, int],
) -> Tuple[int, int, int, int]:
    """Re-snap a bounding box onto the actual ink within ``raw_mask``.

    The morphological close step grows bounding boxes outward, which is
    undesirable for tight final crops.  This function takes an initial
    (possibly inflated) rectangle and re-fits it to the **non-zero** pixels
    of the **raw** (un-closed) mask that fall inside it.  This keeps tightly
    drawn vector figures from being padded by surrounding whitespace that
    crept in via dilation.

    The input bbox uses the **OpenCV convention** -- ``(x, y, w, h)`` where
    ``w`` and ``h`` are non-negative widths -- so that it can be fed
    directly from :func:`find_components`.  The returned bbox uses
    ``(x0, y0, x1, y1)`` with ``x1 = x0 + w`` and ``y1 = y0 + h`` (exclusive
    upper bound) so callers can pass it straight to
    :func:`raster_bbox_to_pdf_rect`.

    Args:
        raw_mask: HxW uint8 mask (typically the output of :func:`binarize`).
        initial_bbox: ``(x, y, w, h)`` in pixel coordinates (top-left
            origin, ``w``/``h`` are widths).

    Returns:
        A new ``(x0, y0, x1, y1)`` bounding box that tightly encloses the ink
        inside ``initial_bbox``.  If no ink remains, the original bbox is
        returned unchanged (this can happen for purely-empty regions of the
        page; the caller is expected to filter those out earlier).
    """
    x, y, w, h = initial_bbox
    if w <= 0 or h <= 0:
        return (x, y, x + w, y + h)

    img_h, img_w = raw_mask.shape[:2]

    # Convert to exclusive-extent rect.
    x0, y0, x1, y1 = x, y, x + w, y + h

    # Clamp to image bounds (defensive: external callers may pass slightly
    # out-of-bounds boxes from dilation).
    x0c = max(0, min(img_w, x0))
    y0c = max(0, min(img_h, y0))
    x1c = max(0, min(img_w, x1))
    y1c = max(0, min(img_h, y1))

    if x1c <= x0c or y1c <= y0c:
        return (x0, y0, x1, y1)

    sub = raw_mask[y0c:y1c, x0c:x1c]
    ys, xs = np.where(sub > 0)
    if xs.size == 0:
        return (x0, y0, x1, y1)

    new_x0 = int(xs.min()) + x0c
    new_y0 = int(ys.min()) + y0c
    new_x1 = int(xs.max()) + 1 + x0c  # +1 -> exclusive upper bound
    new_y1 = int(ys.max()) + 1 + y0c
    return (new_x0, new_y0, new_x1, new_y1)


def compute_adaptive_params(dpi: int, config: CropConfig) -> Dict[str, int]:
    """Translate millimetre-level config into pixel-level numbers.

    Args:
        dpi: Render DPI the page will be rasterised at.
        config: User-facing configuration.

    Returns:
        Dict with integer keys ``close_kernel``, ``merge_gap_px``,
        ``min_area_px``, ``min_width_px``, ``min_height_px``.
    """
    if dpi <= 0:
        raise ValueError("dpi must be positive")
    px_per_mm = dpi / 25.4

    close_kernel = max(3, round(dpi / 25.0 * config.morph_kernel_multiplier))
    # Ensure odd so the kernel has a clear centre pixel.
    if close_kernel % 2 == 0:
        close_kernel += 1

    merge_gap_px = max(2, round(config.merge_gap_mm * px_per_mm))
    min_area_px = max(50, round(config.min_area_mm2 * (px_per_mm ** 2)))
    min_width_px = max(5, round(config.min_width_mm * px_per_mm))
    min_height_px = max(5, round(config.min_height_mm * px_per_mm))

    return {
        "close_kernel": int(close_kernel),
        "merge_gap_px": int(merge_gap_px),
        "min_area_px": int(min_area_px),
        "min_width_px": int(min_width_px),
        "min_height_px": int(min_height_px),
    }


def filter_components(
    components: List[Tuple[int, int, int, int]],
    params: Dict[str, int],
) -> List[Tuple[int, int, int, int]]:
    """Drop components that are too small to be real figures.

    Args:
        components: List of ``(x, y, w, h)`` boxes.
        params: Dict from :func:`compute_adaptive_params`.

    Returns:
        Filtered list preserving original order.
    """
    min_area = params["min_area_px"]
    min_w = params["min_width_px"]
    min_h = params["min_height_px"]

    out: List[Tuple[int, int, int, int]] = []
    for (x, y, w, h) in components:
        if w * h < min_area:
            continue
        if w < min_w or h < min_h:
            continue
        out.append((x, y, w, h))
    return out


def merge_close_boxes(
    boxes: List[Tuple[int, int, int, int]],
    gap_px: int,
) -> List[Tuple[int, int, int, int]]:
    """Merge boxes whose closest edges are within ``gap_px``.

    Each box is inflated by ``gap_px // 2`` on every side so that two boxes
    whose gap on a single axis is less than ``gap_px`` end up with
    intersecting inflated rectangles.  Nested / overlapping boxes always
    merge.  Uses simple union-find (a O(N^2 alpha(N)) operation) which is
    fine for the small ``N`` (typically < 50) produced by a single page.

    Args:
        boxes: List of ``(x, y, w, h)``.
        gap_px: Maximum gap in pixels between two components for them to be
            merged into a single figure.

    Returns:
        List of merged ``(x, y, w, h)`` boxes.  Order follows the merged
        group's smallest y position (top-left-most component first).
    """
    if not boxes:
        return []

    # Dilation per side: half of gap_px so the effective merge distance
    # matches the user-facing ``gap_px`` (not 2 * gap_px).
    half = max(1, gap_px // 2)

    n = len(boxes)
    parent = list(range(n))

    def find(i: int) -> int:
        root = i
        while parent[root] != root:
            root = parent[root]
        # path compression
        while parent[i] != root:
            parent[i], i = root, parent[i]
        return root

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    inflated = [
        (x - half, y - half, x + w + half, y + h + half) for (x, y, w, h) in boxes
    ]

    for i in range(n):
        for j in range(i + 1, n):
            if _rects_intersect(inflated[i], inflated[j]):
                union(i, j)

    groups: Dict[int, List[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    merged: List[Tuple[int, int, int, int]] = []
    for members in groups.values():
        xs0 = [boxes[m][0] for m in members]
        ys0 = [boxes[m][1] for m in members]
        xs1 = [boxes[m][0] + boxes[m][2] for m in members]
        ys1 = [boxes[m][1] + boxes[m][3] for m in members]
        x0 = min(xs0)
        y0 = min(ys0)
        x1 = max(xs1)
        y1 = max(ys1)
        merged.append((x0, y0, x1 - x0, y1 - y0))

    merged.sort(key=lambda b: (b[1], b[0]))
    return merged


def _rects_intersect(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> bool:
    """True iff the two rectangles (inclusive-exclusive corners) overlap."""
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


__all__ = [
    "binarize",
    "morph_close",
    "find_components",
    "refine_bbox",
    "compute_adaptive_params",
    "filter_components",
    "merge_close_boxes",
]