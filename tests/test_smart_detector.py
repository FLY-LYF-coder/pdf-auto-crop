"""Unit tests for the layout-adaptive detector.

Covers the three gap-analysis primitives in isolation, the structural helpers,
and the end-to-end behaviour of :func:`smart_detect_figures` on the
hand-crafted diagnostic fixtures in ``tests/fixtures_diag/``.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pymupdf as fitz
import pytest

from pdf_auto_crop.config import CropConfig
from pdf_auto_crop.image_processor import compute_adaptive_params
from pdf_auto_crop.pdf_processor import render_page_to_array
from pdf_auto_crop.smart_detector import (
    KERNEL_LADDER_MM,
    MIN_GAP_SAMPLES,
    absorb_contained,
    bimodality_score,
    choose_merge_threshold,
    compute_all_inter_comps_gaps,
    find_optimal_gap_threshold,
    kernel_ladder_px,
    smart_detect_figures,
)

ROOT = Path(__file__).resolve().parent
FIXTURES_DIAG = ROOT / "fixtures_diag"

#: Pixels per millimetre at the default render DPI.
PX_PER_MM = 250 / 25.4


# ---------------------------------------------------------------------------
# compute_all_inter_comps_gaps
# ---------------------------------------------------------------------------


def test_gaps_horizontal_alignment() -> None:
    """Two side-by-side boxes yield exactly their horizontal separation."""
    boxes = [(0, 0, 100, 100), (150, 0, 100, 100)]
    assert compute_all_inter_comps_gaps(boxes) == [50]


def test_gaps_vertical_alignment() -> None:
    """Two stacked boxes yield exactly their vertical separation."""
    boxes = [(0, 0, 100, 100), (0, 150, 100, 100)]
    assert compute_all_inter_comps_gaps(boxes) == [50]


def test_gaps_ignore_diagonal_pairs() -> None:
    """Boxes overlapping on neither axis are diagonal -- not a gap."""
    assert compute_all_inter_comps_gaps([(0, 0, 50, 50), (100, 100, 50, 50)]) == []


def test_gaps_ignore_overlapping_pairs() -> None:
    """Nested / overlapping boxes must never report a gap."""
    assert compute_all_inter_comps_gaps([(0, 0, 100, 100), (50, 50, 100, 100)]) == []


def test_gaps_too_few_components() -> None:
    assert compute_all_inter_comps_gaps([]) == []
    assert compute_all_inter_comps_gaps([(0, 0, 10, 10)]) == []


def test_gaps_min_overlap_ratio_rejects_slivers() -> None:
    """A one-pixel overlap is a coincidence, not an alignment."""
    boxes = [(0, 0, 100, 100), (150, 99, 100, 100)]
    assert compute_all_inter_comps_gaps(boxes, min_overlap_ratio=0.0) == [50]
    assert compute_all_inter_comps_gaps(boxes, min_overlap_ratio=0.25) == []


def test_gaps_counts_every_pair() -> None:
    """Three boxes in a row produce all three pairwise gaps."""
    boxes = [(0, 0, 100, 100), (150, 0, 100, 100), (300, 0, 100, 100)]
    # (0,150)=50, (150,300)=50, (0,300)=200
    assert sorted(compute_all_inter_comps_gaps(boxes)) == [50, 50, 200]


# ---------------------------------------------------------------------------
# bimodality_score
# ---------------------------------------------------------------------------


def test_bimodality_too_few_samples() -> None:
    """Below the sample floor the score is undefined, not zero."""
    assert bimodality_score([10, 200, 30]) == -1.0
    assert bimodality_score([]) == -1.0


def test_bimodality_flat_distribution() -> None:
    """No variation at all means there is nothing to separate."""
    assert bimodality_score([40] * 12, PX_PER_MM) == 0.0


def test_bimodality_unimodal_spread() -> None:
    """A continuous spread has no deep valley, so it scores zero."""
    assert bimodality_score(list(range(100, 115)), PX_PER_MM) == 0.0


def test_bimodality_two_modes() -> None:
    """Two tight clusters separated by empty space score highly."""
    gaps = [10] * 10 + [200] * 10
    assert bimodality_score(gaps, PX_PER_MM) > 0.0


def test_bimodality_detects_peak_on_histogram_edge() -> None:
    """A mode sitting in the last bin must still count as a peak.

    A naive ``range(1, len(hist) - 1)`` scan silently drops the final bin and
    would report 0.0 here, which is how gap data clustered near its upper
    bound used to escape detection.
    """
    gaps = [10] * 10 + [190] * 10
    assert bimodality_score(gaps, PX_PER_MM) > 0.0


def test_bimodality_requires_a_deep_valley() -> None:
    """A shallow dip between two humps is not a usable separation.

    Two peaks of 10 with a valley of 8 in between fails the depth test, so the
    distribution is reported as not bimodal rather than trusted.
    """
    gaps = [0] * 10 + [2] * 8 + [4] * 10
    assert bimodality_score(gaps, PX_PER_MM) == 0.0


# ---------------------------------------------------------------------------
# find_optimal_gap_threshold
# ---------------------------------------------------------------------------


def test_threshold_empty_gaps() -> None:
    assert find_optimal_gap_threshold([], PX_PER_MM) == 0


def test_threshold_between_the_two_modes() -> None:
    """For bimodal data the valley must land between the modes."""
    gaps = [10] * 10 + [200] * 10
    t = find_optimal_gap_threshold(gaps, PX_PER_MM)
    assert 10 < t < 200


def test_threshold_unimodal_fallback_is_positive() -> None:
    """Unimodal data has no valley, so fall back to half the median."""
    t = find_optimal_gap_threshold([50, 51, 52, 53, 54], PX_PER_MM)
    assert t >= 1


def test_threshold_single_distinct_value() -> None:
    """All gaps identical: no range to bin, fall back to the median."""
    assert find_optimal_gap_threshold([30] * 8, PX_PER_MM) >= 1


# ---------------------------------------------------------------------------
# absorb_contained
# ---------------------------------------------------------------------------


def test_absorb_contained_folds_inner_box() -> None:
    """A box inside another is part of it, whatever the gap."""
    result = absorb_contained([(0, 0, 100, 100), (20, 20, 10, 10)], tolerance_px=2)
    assert result == [(0, 0, 100, 100)]


def test_absorb_contained_keeps_siblings() -> None:
    """Two side-by-side figures share no containment and must survive.

    Inputs are ``(x, y, w, h)``; outputs are ``(x0, y0, x1, y1)`` extents.
    """
    result = absorb_contained([(0, 0, 100, 100), (200, 0, 100, 100)], tolerance_px=2)
    assert result == [(0, 0, 100, 100), (200, 0, 300, 100)]


def test_absorb_contained_respects_tolerance() -> None:
    """A box poking outside is only absorbed when tolerance covers the excess.

    The group is then the hull of both boxes, so the result grows by at most
    ``tolerance_px``; the pipeline re-snaps to real ink afterwards, which makes
    that slack invisible in the final crop.
    """
    outer = (0, 0, 100, 100)
    poking_out = (90, 90, 20, 20)  # extends 10 px past the right/bottom edge
    assert len(absorb_contained([outer, poking_out], tolerance_px=3)) == 2
    assert absorb_contained([outer, poking_out], tolerance_px=12) == [(0, 0, 110, 110)]


def test_absorb_contained_single_box() -> None:
    assert absorb_contained([(5, 6, 10, 20)], tolerance_px=2) == [(5, 6, 15, 26)]


# ---------------------------------------------------------------------------
# kernel ladder / threshold selection
# ---------------------------------------------------------------------------


def test_kernel_ladder_at_default_dpi() -> None:
    assert kernel_ladder_px(250) == [1, 3, 5, 7, 9, 11, 15]


def test_kernel_ladder_scales_with_dpi() -> None:
    """Doubling the DPI must double every kernel -- no hard-coded pixel values."""
    base = kernel_ladder_px(250)
    doubled = kernel_ladder_px(500)
    assert len(base) == len(doubled) == len(KERNEL_LADDER_MM)
    assert all(h > l for l, h in zip(base, doubled))
    assert base[0] == 1 and doubled[0] == 3  # the identity rung saturates low


def test_kernel_ladder_rejects_bad_dpi() -> None:
    with pytest.raises(ValueError):
        kernel_ladder_px(0)


def test_choose_threshold_without_gaps_uses_config() -> None:
    params = compute_adaptive_params(250, CropConfig())
    assert choose_merge_threshold([], params, PX_PER_MM) == params["merge_gap_px"]


def test_choose_threshold_is_just_below_smallest_gap() -> None:
    """The safe rule: merge touching things, never fuse distinct figures."""
    params = compute_adaptive_params(250, CropConfig())
    # Three boxes, nearest neighbours 30 px apart.
    t = choose_merge_threshold([30, 30, 400, 400, 900], params, PX_PER_MM)
    assert t == 29


def test_choose_threshold_never_below_floor() -> None:
    """A zero gap must still merge, which needs a threshold of at least 2."""
    params = compute_adaptive_params(250, CropConfig())
    assert choose_merge_threshold([0, 0, 0, 0, 0], params, PX_PER_MM) >= 2


# ---------------------------------------------------------------------------
# smart_detect_figures -- argument validation
# ---------------------------------------------------------------------------


def test_smart_detect_rejects_bad_image_shape() -> None:
    with pytest.raises(ValueError):
        smart_detect_figures(np.zeros((10, 10), dtype=np.uint8), (595, 842), CropConfig())


def test_smart_detect_rejects_bad_page_size() -> None:
    rgb = np.full((20, 20, 3), 255, dtype=np.uint8)
    with pytest.raises(ValueError):
        smart_detect_figures(rgb, (595,), CropConfig())
    with pytest.raises(ValueError):
        smart_detect_figures(rgb, (0, 842), CropConfig())


def test_smart_detect_on_blank_page() -> None:
    """A page with no ink yields no figures rather than raising."""
    rgb = np.full((200, 200, 3), 255, dtype=np.uint8)
    assert smart_detect_figures(rgb, (595, 842), CropConfig()) == []


# ---------------------------------------------------------------------------
# smart_detect_figures -- diagnostic fixtures
# ---------------------------------------------------------------------------


def _render(path: Path, cfg: CropConfig):
    doc = fitz.open(str(path))
    try:
        return render_page_to_array(doc, 0, cfg.dpi)
    finally:
        doc.close()


def _count(path: Path, cfg: CropConfig | None = None) -> int:
    cfg = cfg or CropConfig()
    rgb, size = _render(path, cfg)
    return len(smart_detect_figures(rgb, size, cfg))


@pytest.mark.parametrize(
    "name, expected",
    [
        ("D1_horizontal_3.pdf", 3),
        ("D2_3plus1.pdf", 4),
        ("D3_big_left_4right.pdf", 5),
        ("D4_fem_blank_interior.pdf", 1),
        ("D5_topology.pdf", 1),
        ("D6_tiny_gap.pdf", 2),
        ("D7_mixed_gaps.pdf", 3),
        ("D8_mixed_widths.pdf", 3),
    ],
)
def test_diagnostic_fixture_counts(name: str, expected: int) -> None:
    """Every diagnostic layout resolves to the expected number of figures."""
    assert _count(FIXTURES_DIAG / name) == expected


def test_d7_splits_the_one_millimetre_gap() -> None:
    """D7 is the case the legacy detector could not solve.

    Three figures are stacked with a 20 mm gap above and a 1 mm gap below.  A
    single global threshold cannot serve both: the 1 mm inter-figure gap is
    *smaller* than the ~10 mm gaps between a figure's own interior lines, so
    any threshold tight enough to keep the figures apart would also tear each
    figure apart.  Dropping the interior lines as sub-threshold components
    before measuring gaps is what makes this solvable.
    """
    cfg = CropConfig()
    rgb, size = _render(FIXTURES_DIAG / "D7_mixed_gaps.pdf", cfg)
    rects = smart_detect_figures(rgb, size, cfg)
    assert len(rects) == 3
    # Three 80x60 mm figures; the split must respect the 1 mm lower gap.
    widths = [r.width * 25.4 / 72 for r in rects]
    heights = [r.height * 25.4 / 72 for r in rects]
    assert all(abs(w - 80) < 1.0 for w in widths)
    assert all(abs(h - 60) < 1.0 for h in heights)


def test_d2_keeps_the_20mm_row_gap_separate() -> None:
    """A histogram-valley threshold would wrongly fuse the top row here.

    The top row has a 15 mm column gap and the big bottom figure sits 30 mm
    away.  Splitting at the valley between those two modes lands near 22 mm,
    which is above the 15 mm column gap and merges two distinct figures.
    """
    cfg = CropConfig()
    rgb, size = _render(FIXTURES_DIAG / "D2_3plus1.pdf", cfg)
    rects = smart_detect_figures(rgb, size, cfg)
    assert len(rects) == 4
    tops = [r.y0 for r in rects[:3]]
    assert max(tops) - min(tops) < 1.0, "the three top-row figures must align"


def test_dense_mesh_is_not_fragmented() -> None:
    """A dense mesh with a blank interior stays a single figure."""
    cfg = CropConfig()
    rgb, size = _render(FIXTURES_DIAG / "D4_fem_blank_interior.pdf", cfg)
    rects = smart_detect_figures(rgb, size, cfg)
    assert len(rects) == 1
    assert rects[0].width > 100 and rects[0].height > 100


# ---------------------------------------------------------------------------
# Config integration
# ---------------------------------------------------------------------------


def test_config_defaults_to_smart() -> None:
    assert CropConfig().algorithm == "smart"


def test_config_round_trips_algorithm() -> None:
    cfg = CropConfig(algorithm="legacy")
    assert CropConfig.from_dict(cfg.to_dict()).algorithm == "legacy"


def test_config_rejects_unknown_algorithm() -> None:
    with pytest.raises(ValueError):
        CropConfig(algorithm="magic").validate()
