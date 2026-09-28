"""Layout-adaptive ("smart") figure detection.

The legacy detector in :mod:`pdf_auto_crop.detector` picks **one** close-kernel
size and **one** merge gap for every page.  That works for uniform layouts but
breaks as soon as a page mixes a dense finite-element mesh with sparse line
art, or mixes a 1 mm inter-figure gap with a 20 mm one -- the single parameter
cannot be right for both.

This module replaces the guesswork with measurement:

1. **Multi-scale search** -- the page is closed with a ladder of kernel sizes
   and every scale is scored by how cleanly the resulting inter-component gaps
   split into two populations (see :func:`bimodality_score`).
2. **Containment absorption** -- a component whose bounding box sits inside
   another component's bounding box is part of that component, whatever the
   gap.  This is what keeps a plot frame together with its axes, legend and
   star-shaped line art without needing an aggressive closing kernel.
3. **Adaptive merge threshold** -- derived from the *measured* gap distribution
   of the surviving anchors rather than from a user constant.

Design note -- why the threshold is ``min_gap - 1`` rather than the histogram
valley
------------------------------------------------------------------------
The valley of a bimodal histogram is the textbook choice, but it is only
correct when the *low* population must be merged.  Consider a 2x2 grid with a
20 mm column gap and a 30 mm row gap: the two modes are 20 mm and 30 mm, the
valley sits at ~25 mm, and a 25 mm threshold happily merges the 20 mm column
neighbours that must stay separate.  The low mode there is an *inter*-figure
gap, not an *intra*-figure one, and no histogram can tell the two apart.

``min_gap - 1`` is the only rule that is safe in both directions: it merges
everything that touches and nothing that does not, so it can never fuse two
distinct figures.  The valley is still computed and is applied only when it
lands *below* the smallest measured gap, where it can tighten the result but
never loosen it.  Bimodality therefore drives *kernel selection*, which is
where it genuinely changes the outcome.

All pixel-level magnitudes are derived from the render DPI, so behaviour is
identical at 150 DPI and 600 DPI.
"""

from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

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

#: An OpenCV-style component box ``(x, y, w, h)``.
Box = Tuple[int, int, int, int]


#: Close-kernel ladder in **millimetres** so it scales with the render DPI.
#: At the default 250 DPI these resolve to 1, 3, 5, 7, 9, 11 and 15 pixels.
#: The 0.10 mm rung is effectively an identity closing: it keeps the page
#: undistorted, which matters when two figures sit less than a millimetre
#: apart.  A 0.6 pt frame stroke is ~2 px wide at 250 DPI, so a 0.5 mm white
#: gap is only ~3 px of real whitespace -- every kernel from 3 px upwards
#: bridges it, and only the identity rung can tell the figures apart.
KERNEL_LADDER_MM: Tuple[float, ...] = (0.10, 0.30, 0.50, 0.70, 0.90, 1.10, 1.50)

#: Minimum number of gaps before a histogram is considered trustworthy.
MIN_GAP_SAMPLES: int = 5

#: Histogram bin width floor, in millimetres.  0.2 mm == 2 px at 250 DPI.
BIN_WIDTH_MM: float = 0.2

#: Number of bins the gap range is divided into (resolution, not a distance).
TARGET_BIN_COUNT: int = 15

#: A valley is only "deep" when it is below half of the smaller peak.
VALLEY_DEPTH_RATIO: float = 0.5

#: Two components only form a usable gap when they share at least this
#: fraction of the smaller box along the shared axis.  Without it, pairs that
#: merely clip by a single pixel inject noise gaps of ~0 px into the minimum.
MIN_OVERLAP_RATIO: float = 0.25

#: Tolerance for containment tests, in millimetres.  Absorbs the 1-2 px of
#: dilation slack left by the closing step.
CONTAINMENT_TOLERANCE_MM: float = 0.25

#: Smallest merge threshold handed to :func:`merge_close_boxes`.  The helper
#: dilates by ``gap_px // 2`` per side, so a threshold below 2 px can never
#: join even perfectly touching components.
MIN_MERGE_GAP_PX: int = 2

#: Render DPI assumed by the standalone gap-analysis helpers when the caller
#: does not supply one, so their defaults match the project default config.
DEFAULT_DPI: int = 250


def _default_px_per_mm() -> float:
    return DEFAULT_DPI / 25.4


# ---------------------------------------------------------------------------
# Gap measurement
# ---------------------------------------------------------------------------


def compute_all_inter_comps_gaps(
    components: Sequence[Box],
    *,
    min_overlap_ratio: float = 0.0,
) -> List[int]:
    """Return every gap between two axis-aligned, substantially aligned boxes.

    Two components are considered *aligned* when they overlap on one axis; the
    gap is then measured along the other.  Overlapping or diagonally offset
    pairs produce no gap at all (they are handled by containment or merging).

    Args:
        components: List of ``(x, y, w, h)`` boxes (OpenCV convention).
        min_overlap_ratio: How much of the smaller box the shared-axis overlap
            must cover for the pair to count.  ``0.0`` accepts any overlap and
            reproduces the plain pairwise sweep; ``0.25`` (the value the
            detector uses) ignores pairs that merely clip by a pixel or two.

    Returns:
        Gaps in pixels, non-negative, in arbitrary order.
    """
    gaps: List[int] = []
    n = len(components)
    if n < 2:
        return gaps

    for i in range(n):
        xi, yi, wi, hi = components[i]
        if wi <= 0 or hi <= 0:
            continue
        for j in range(i + 1, n):
            xj, yj, wj, hj = components[j]
            if wj <= 0 or hj <= 0:
                continue

            # Vertical gap needs a real horizontal overlap ...
            x_overlap = min(xi + wi, xj + wj) - max(xi, xj)
            if x_overlap > min_overlap_ratio * min(wi, wj):
                y_bottom, y_top = yi + hi, yj + hj
                if y_bottom <= yj:
                    gaps.append(yj - y_bottom)
                elif y_top <= yi:
                    gaps.append(yi - y_top)

            # ... and a horizontal gap needs a real vertical overlap.
            y_overlap = min(yi + hi, yj + hj) - max(yi, yj)
            if y_overlap > min_overlap_ratio * min(hi, hj):
                x_right, x_left = xi + wi, xj
                if x_right <= xj:
                    gaps.append(xj - x_right)
                elif x_left <= xi:
                    gaps.append(xi - x_left)

    return gaps


def _min_bin_px(px_per_mm: float) -> int:
    """Smallest histogram bin width for ``px_per_mm`` (floored at 2 px)."""
    return max(2, int(round(BIN_WIDTH_MM * px_per_mm)))


def _gap_histogram(
    gaps: Sequence[int], px_per_mm: float
) -> Tuple[np.ndarray, np.ndarray]:
    """Bin ``gaps`` into a histogram of roughly :data:`TARGET_BIN_COUNT` bins.

    Returns:
        ``(bin_edges, counts)`` where ``counts[i]`` is the number of gaps in
        ``[bin_edges[i], bin_edges[i + 1])``.
    """
    arr = np.sort(np.asarray(gaps, dtype=np.float64))
    g_min, g_max = int(arr.min()), int(arr.max())
    if g_max <= g_min:
        return np.array([g_min, g_min + 1], dtype=np.float64), np.array(
            [len(arr)], dtype=np.int64
        )

    min_bin = _min_bin_px(px_per_mm)
    bin_width = max(min_bin, (g_max - g_min) // TARGET_BIN_COUNT)
    bins = np.arange(g_min, g_max + bin_width, bin_width)
    if bins.size < 2:  # pragma: no cover -- defensive
        bins = np.array([g_min, g_min + bin_width], dtype=np.float64)
    counts, _ = np.histogram(arr, bins=bins)
    return bins, counts


def _find_peaks(counts: np.ndarray) -> List[int]:
    """Indices of local maxima, **including** the first and last bin.

    Padding the series with ``-1`` means a mode sitting on the histogram edge
    still registers as a peak.  Ignoring edge bins (as a naive
    ``range(1, len - 1)`` scan does) silently loses the maximum of the gap
    distribution, which is exactly the mode that matters when gaps are
    clustered near their upper bound.
    """
    padded = np.concatenate(([-1], counts.astype(np.int64), [-1]))
    # ``i`` indexes the padded series, so shift back to the original bins.
    return [
        i - 1
        for i in range(1, padded.size - 1)
        if padded[i] >= padded[i - 1] and padded[i] > padded[i + 1]
    ]


def _two_strongest_peaks(counts: np.ndarray, peaks: Sequence[int]) -> Tuple[int, int]:
    """Return the two highest-count peak indices, left one first."""
    ordered = sorted(peaks, key=lambda i: (int(counts[i]), -i), reverse=True)
    a, b = ordered[0], ordered[1]
    return (a, b) if a <= b else (b, a)


def bimodality_score(gaps: Sequence[int], px_per_mm: float = 0.0) -> float:
    """Score how cleanly ``gaps`` splits into two well-separated populations.

    A page whose figures need *different* merge gaps (a dense mesh beside
    sparse line art, a 1 mm gap beside a 20 mm one) produces a bimodal gap
    distribution: one mode of small intra-figure gaps and one of large
    inter-figure gaps.  A page that needs a single uniform gap produces a
    single mode, or a flat spread with no deep valley.

    The score is ``(smaller_peak - valley) / smaller_peak`` plus a bonus
    proportional to how far apart the two peaks sit, so a wide, deep valley
    scores highest.

    Args:
        gaps: Gaps in pixels, e.g. from :func:`compute_all_inter_comps_gaps`.
        px_per_mm: Pixels per millimetre for the current render.  Defaults to
            the project default DPI so the standalone call matches the
            detector's default configuration.

    Returns:
        ``-1.0`` when there are too few gaps to judge, ``0.0`` when the
        distribution is not usefully bimodal, otherwise a positive score.
    """
    scale = px_per_mm if px_per_mm > 0 else _default_px_per_mm()

    if len(gaps) < MIN_GAP_SAMPLES:
        return -1.0

    arr = np.asarray(gaps, dtype=np.float64)
    if float(arr.std()) < _min_bin_px(scale):
        return 0.0

    _, counts = _gap_histogram(gaps, scale)
    if counts.size < 3:
        return 0.0

    peaks = _find_peaks(counts)
    if len(peaks) < 2:
        return 0.0

    p1, p2 = _two_strongest_peaks(counts, peaks)
    valley = counts[p1 + 1 : p2]
    valley_count = int(valley.min()) if valley.size else 0
    peak_min = min(int(counts[p1]), int(counts[p2]))
    if peak_min < 2:
        return 0.0
    if valley_count > peak_min * VALLEY_DEPTH_RATIO:
        return 0.0

    score = (peak_min - valley_count) / peak_min
    separation_bonus = (p2 - p1) / counts.size
    return float(score + separation_bonus)


def find_optimal_gap_threshold(
    gaps: Sequence[int], px_per_mm: float = 0.0
) -> int:
    """Locate the valley between the two strongest modes of ``gaps``.

    Only meaningful for a genuinely bimodal distribution -- see the module
    docstring for why the detector treats the result as a *tightening* of the
    safe ``min_gap - 1`` rule rather than as the threshold itself.

    Args:
        gaps: Gaps in pixels.
        px_per_mm: Pixels per millimetre for the current render.

    Returns:
        A merge threshold in pixels.  Falls back to half the median when the
        gaps are unimodal, and to ``0`` for an empty input.
    """
    if not gaps:
        return 0

    scale = px_per_mm if px_per_mm > 0 else _default_px_per_mm()
    arr = np.sort(np.asarray(gaps, dtype=np.float64))
    g_min, g_max = int(arr.min()), int(arr.max())
    if g_max <= g_min:
        return max(1, int(np.median(arr) * 0.5))

    bins, counts = _gap_histogram(gaps, scale)
    if counts.size < 3:
        return max(1, int(np.median(arr) * 0.5))

    peaks = _find_peaks(counts)
    if len(peaks) >= 2:
        p1, p2 = _two_strongest_peaks(counts, peaks)
        if p2 <= p1 + 1:
            return max(1, int(np.median(arr) * 0.5))
        valley_idx = p1 + 1 + int(np.argmin(counts[p1 + 1 : p2]))
        min_bin = _min_bin_px(scale)
        return max(1, int(bins[valley_idx]) - min_bin // 2)

    return max(1, int(np.median(arr) * 0.5))


# ---------------------------------------------------------------------------
# Structural helpers
# ---------------------------------------------------------------------------


def absorb_contained(components: Sequence[Box], tolerance_px: int = 0) -> List[Box]:
    """Fold every box that lies inside another box into its container.

    A plot frame, its axes, its legend and its line art are separate connected
    components, yet they are one figure.  Rather than growing the closing
    kernel until the morphology joins them -- which would also start bridging
    neighbouring figures -- this resolves containment geometrically, which is
    exact and costs one pairwise pass.

    Args:
        components: ``(x, y, w, h)`` boxes.
        tolerance_px: Slop allowed on each side, absorbing dilation from the
            closing step.

    Returns:
        The surviving boxes as ``(x0, y0, x1, y1)`` extents, sorted top-left to
        bottom-right.
    """
    boxes = [tuple(int(v) for v in c) for c in components]
    n = len(boxes)
    if n < 2:
        return [(b[0], b[1], b[0] + b[2], b[1] + b[3]) for b in boxes]

    extents = [(b[0], b[1], b[0] + b[2], b[1] + b[3]) for b in boxes]
    parent = list(range(n))

    def find(i: int) -> int:
        root = i
        while parent[root] != root:
            root = parent[root]
        while parent[i] != root:
            parent[i], i = root, parent[i]
        return root

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    tol = max(0, int(tolerance_px))
    for i in range(n):
        ax0, ay0, ax1, ay1 = extents[i]
        area_i = (ax1 - ax0) * (ay1 - ay0)
        for j in range(n):
            if i == j:
                continue
            bx0, by0, bx1, by1 = extents[j]
            if (bx0 - ax0) >= -tol and (by0 - ay0) >= -tol \
                    and (bx1 - ax1) <= tol and (by1 - ay1) <= tol:
                # Only let a strictly larger box adopt a strictly smaller one,
                # otherwise two identical boxes would never resolve.
                if area_i >= (bx1 - bx0) * (by1 - by0):
                    union(i, j)

    groups: Dict[int, List[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    out: List[Tuple[int, int, int, int]] = []
    for members in groups.values():
        # A contained box never grows the union, so the hull equals the
        # container itself; the hull is used anyway for safety.
        out.append(
            (
                min(extents[m][0] for m in members),
                min(extents[m][1] for m in members),
                max(extents[m][2] for m in members),
                max(extents[m][3] for m in members),
            )
        )

    out.sort(key=lambda r: (r[1], r[0]))
    return out


def kernel_ladder_px(dpi: int) -> List[int]:
    """Resolve :data:`KERNEL_LADDER_MM` to odd pixel kernels for ``dpi``.

    A side of 1 is kept: a 1x1 structuring element makes the closing an
    identity operation, giving the ladder a "no distortion" rung.
    """
    if dpi <= 0:
        raise ValueError("dpi must be positive")
    px_per_mm = dpi / 25.4
    kernels: List[int] = []
    for mm in KERNEL_LADDER_MM:
        side = max(1, int(round(mm * px_per_mm)))
        if side % 2 == 0:
            side += 1
        kernels.append(side)
    return kernels


def choose_merge_threshold(
    gaps: Sequence[int], params: Dict[str, int], px_per_mm: float = 0.0
) -> int:
    """Pick the merge threshold in pixels from the measured gap distribution.

    The safe rule is ``min_gap - 1``: it merges components that touch and
    separates every pair that does not, so it can never fuse two figures.
    When the gaps are strongly bimodal the histogram valley is used, but only
    if it lands *below* the smallest measured gap -- there it can pull the
    threshold down to stitch fragments, never up to fuse distinct figures.

    Args:
        gaps: Measured anchor-to-anchor gaps in pixels.
        params: Output of :func:`compute_adaptive_params`; supplies the
            user-configured fallback when there is nothing to measure.
        px_per_mm: Pixels per millimetre for the current render.

    Returns:
        A merge threshold in pixels, always ``>= MIN_MERGE_GAP_PX``.
    """
    user_gap = max(MIN_MERGE_GAP_PX, int(params["merge_gap_px"]))
    if not gaps:
        return user_gap

    smallest = int(min(gaps))
    threshold = max(MIN_MERGE_GAP_PX, smallest - 1)

    if bimodality_score(gaps, px_per_mm) > 0.0:
        valley = find_optimal_gap_threshold(gaps, px_per_mm)
        if 0 < valley < threshold:
            threshold = valley

    return threshold


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def smart_detect_figures(
    rgb: np.ndarray,
    page_size_pt: Tuple[float, float],
    config: CropConfig,
) -> List[fitz.Rect]:
    """Detect figure rectangles on a rasterised page, adapting to the layout.

    Pipeline:

    1. Binarise the page into an ink mask.
    2. Sweep a DPI-scaled ladder of closing kernels.  For each scale, keep only
       components large enough to be a real figure, absorb the ones contained
       in another, and score how bimodal the remaining gaps are.
    3. Keep the scale with the best bimodality (ties go to the smallest kernel,
       which preserves genuinely tight inter-figure gaps).
    4. Derive the merge threshold from that scale's measured gaps.
    5. Merge, re-filter, re-snap each box onto the raw ink, and convert to PDF
       point space.

    Args:
        rgb: HxWx3 uint8 raster of a single page.
        page_size_pt: ``(width_pt, height_pt)`` of the page.
        config: Active :class:`CropConfig`.

    Returns:
        A list of :class:`fitz.Rect` in PDF point space, sorted top-down.
    """
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError(f"rgb must be HxWx3, got {rgb.shape}")
    if len(page_size_pt) != 2:
        raise ValueError("page_size_pt must be a 2-tuple")
    page_w_pt, page_h_pt = page_size_pt
    if page_w_pt <= 0 or page_h_pt <= 0:
        raise ValueError("page size must be positive")

    raw_mask = binarize(rgb, config.threshold)
    params = compute_adaptive_params(config.dpi, config)
    px_per_mm = config.dpi / 25.4
    tolerance_px = max(1, int(round(CONTAINMENT_TOLERANCE_MM * px_per_mm)))

    best_score = -2.0
    best_kernel = 0
    best_anchors: List[Tuple[int, int, int, int]] = []
    best_gaps: List[int] = []

    for kernel in kernel_ladder_px(config.dpi):
        closed = morph_close(raw_mask, kernel)
        components = find_components(closed)
        anchors = filter_components(components, params)
        if not anchors:
            continue

        anchors = absorb_contained(anchors, tolerance_px)
        extents = [(b[0], b[1], b[2] - b[0], b[3] - b[1]) for b in anchors]
        gaps = compute_all_inter_comps_gaps(
            extents, min_overlap_ratio=MIN_OVERLAP_RATIO
        )
        score = bimodality_score(gaps, px_per_mm)

        # Highest score wins; ``-kernel`` breaks ties towards the smallest
        # kernel, which is the one least likely to bridge real inter-figure
        # gaps such as D6's 0.5 mm.
        if (score, -kernel) > (best_score, -best_kernel):
            best_score = score
            best_kernel = kernel
            best_anchors = anchors
            best_gaps = gaps

    if not best_anchors:
        return []

    threshold_px = choose_merge_threshold(best_gaps, params, px_per_mm)
    merged = merge_close_boxes(
        [(b[0], b[1], b[2] - b[0], b[3] - b[1]) for b in best_anchors], threshold_px
    )
    survivors = filter_components(merged, params)

    rects: List[fitz.Rect] = []
    for bbox in survivors:
        tight = refine_bbox(raw_mask, bbox)
        rects.append(raster_bbox_to_pdf_rect(tight, config.dpi, page_h_pt))

    rects.sort(key=lambda r: (round(r.y0, 1), round(r.x0, 1)))
    return rects


__all__ = [
    "compute_all_inter_comps_gaps",
    "bimodality_score",
    "find_optimal_gap_threshold",
    "absorb_contained",
    "choose_merge_threshold",
    "kernel_ladder_px",
    "smart_detect_figures",
    "KERNEL_LADDER_MM",
]
