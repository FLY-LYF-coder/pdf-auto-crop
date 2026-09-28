"""Tests for the manual crop-window editor.

:mod:`pdf_auto_crop.gui_edit` deliberately holds no tkinter import, so every
rule that decides *what gets cropped* is testable without a display.  The
canvas plumbing around it (press/drag/release) is thin enough to be covered by
the screenshot smoke test in ``scripts/gui_screenshot.py``; what is asserted
here is the part that, if wrong, would silently produce the wrong crops.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf as fitz
import pytest

from pdf_auto_crop import gui_edit
from pdf_auto_crop.config import CropConfig
from pdf_auto_crop.geometry import add_margin_pt, mm_to_pt
from pdf_auto_crop.pdf_processor import render_page_to_array
from pdf_auto_crop.pipeline import get_detector

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"
FIXTURES_DIAG = ROOT / "fixtures_diag"

A4 = (595.3, 841.9)
PAGE = fitz.Rect(0, 0, *A4)


def _r(x0: float, y0: float, x1: float, y1: float) -> fitz.Rect:
    return fitz.Rect(x0, y0, x1, y1)


def _assert_rect(actual, expected, *, abs_tol: float = 1e-6) -> None:
    """Compare two Rects component-wise.

    ``pytest.approx`` cannot be used here: it fails on a :class:`fitz.Rect`
    even against a byte-identical one, because the type's own ``__eq__``
    interferes with the numeric comparison.  Comparing the four floats
    separately is both correct and produces a readable failure message.
    """
    got, want = tuple(actual), tuple(expected)
    for i, (a, b) in enumerate(zip(got, want)):
        assert a == pytest.approx(b, abs=abs_tol), f"coordinate {i}: {got} != {want}"


# ---------------------------------------------------------------------------
# Coordinate round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dpi", [150, 250, 300, 600])
@pytest.mark.parametrize("zoom", [0.3, 0.5, 1.0, 1.7])
@pytest.mark.parametrize("origin", [(0.0, 0.0), (37.5, -12.25), (120.0, 44.0)])
def test_round_trip_recovers_the_rect(dpi: int, zoom: float, origin) -> None:
    """canvas -> PDF -> canvas must be the identity, or a drawn box drifts.

    The two conversions flip the vertical axis and apply zoom plus a centring
    offset, so they are easy to get subtly wrong in a way that only shows up as
    boxes sitting a few millimetres off the artwork.  Asserting the round trip
    is the cheapest way to pin that down.
    """
    rect = _r(50.0, 120.0, 480.0, 700.0)
    canvas = gui_edit.pdf_rect_to_canvas(
        rect, dpi=dpi, page_height_pt=A4[1], zoom=zoom, origin=origin
    )
    back = gui_edit.canvas_rect_to_pdf(
        *canvas, dpi=dpi, page_height_pt=A4[1], zoom=zoom, origin=origin
    )
    _assert_rect(back, rect, abs_tol=1e-9)


def test_canvas_rect_normalises_a_drag_that_runs_up_and_left() -> None:
    """Tk reports press then release; the user may drag in any direction."""
    kwargs = dict(dpi=250, page_height_pt=A4[1], zoom=1.0, origin=(0.0, 0.0))
    up_left = gui_edit.canvas_rect_to_pdf(300, 400, 100, 200, **kwargs)
    down_right = gui_edit.canvas_rect_to_pdf(100, 200, 300, 400, **kwargs)
    _assert_rect(up_left, down_right, abs_tol=1e-9)
    assert up_left.x0 < up_left.x1 and up_left.y0 < up_left.y1


def test_conversion_rejects_a_degenerate_zoom_or_dpi() -> None:
    kwargs = dict(page_height_pt=A4[1], zoom=1.0, origin=(0.0, 0.0))
    with pytest.raises(ValueError):
        gui_edit.pdf_rect_to_canvas(PAGE, dpi=0, **kwargs)
    with pytest.raises(ValueError):
        gui_edit.pdf_rect_to_canvas(PAGE, dpi=250, page_height_pt=A4[1], zoom=0, origin=(0, 0))


# ---------------------------------------------------------------------------
# Rect utilities
# ---------------------------------------------------------------------------


def test_normalise_sorts_corners() -> None:
    assert gui_edit.normalise(10, 20, 5, 8) == (5, 8, 10, 20)


def test_too_small_boxes_are_rejected() -> None:
    assert gui_edit.is_too_small(_r(0, 0, 1.9, 100))
    assert gui_edit.is_too_small(_r(0, 0, 100, 1.9))
    assert not gui_edit.is_too_small(_r(0, 0, 2.0, 2.0))
    # A hairline figure is legitimate content; only a stray click is degenerate.
    assert not gui_edit.is_too_small(_r(0, 0, 0.6, 500), min_side_pt=0.5)


def test_clamp_pulls_a_box_back_instead_of_raising() -> None:
    """Unlike ``geometry.clamp_rect`` this must survive a fully-outside box."""
    clamped = gui_edit.clamp_to_page(_r(-500, -500, 5000, 5000), PAGE)
    assert clamped == PAGE
    partial = gui_edit.clamp_to_page(_r(-10, 100, 50, 200), PAGE)
    assert (partial.x0, partial.x1) == (0, 50)


def test_move_keeps_the_box_rigid_at_the_page_edge() -> None:
    """Dragging into the margin pins the box; it must not squash it."""
    rect = _r(10, 10, 110, 60)
    moved = gui_edit.move_rect(rect, -999, -999, PAGE)
    assert moved == _r(0, 0, 100, 50)
    assert moved.width == rect.width and moved.height == rect.height


def test_move_applies_a_legal_delta_verbatim() -> None:
    rect = _r(100, 100, 200, 200)
    assert gui_edit.move_rect(rect, 5, -7, PAGE) == _r(105, 93, 205, 193)


def test_resize_moves_only_the_requested_edges() -> None:
    """A corner drag must not drag the opposite edge along with it.

    ``top``/``bottom`` are named for the page, not the coordinate axis: in PDF
    space ``top`` is ``y1`` and ``bottom`` is ``y0``.
    """
    rect = _r(100, 100, 200, 300)
    assert gui_edit.resize_rect(rect, top=150) == _r(100, 100, 200, 150)
    assert gui_edit.resize_rect(rect, bottom=150) == _r(100, 150, 200, 300)
    out = gui_edit.resize_rect(rect, left=50, top=150)
    assert out == _r(50, 100, 200, 150)


def test_resize_clamps_to_the_page_when_asked() -> None:
    rect = _r(100, 100, 200, 300)
    out = gui_edit.resize_rect(rect, left=-40, page=PAGE)
    assert out.x0 == 0


def test_resize_cannot_invert_a_box() -> None:
    """Dragging a corner past the far edge flips the box rather than inverting it."""
    rect = _r(100, 100, 200, 300)
    out = gui_edit.resize_rect(rect, left=400)
    assert out.x0 <= out.x1


def test_handles_sit_on_the_corners() -> None:
    rect = _r(100, 100, 200, 300)
    pts = gui_edit.handle_points(rect, size_pt=8.0)
    # PDF y grows upward, so handle 0 (bottom-left) has the *lowest* y.
    assert (pts[0][0], pts[0][1]) == (104.0, 104.0)
    assert pts[2] == (196.0, 296.0)


def test_edges_for_handle_maps_corners_to_edges() -> None:
    # Handle 0 is the *bottom*-left corner in PDF space, so it drags left+bottom.
    assert gui_edit.edges_for_handle(0) == ("left", "bottom")
    assert gui_edit.edges_for_handle(1) == ("right", "bottom")
    assert gui_edit.edges_for_handle(2) == ("right", "top")
    assert gui_edit.edges_for_handle(3) == ("left", "top")
    with pytest.raises(IndexError):
        gui_edit.edges_for_handle(4)


def test_nearest_handle_picks_the_corner_under_the_cursor() -> None:
    rect = _r(100, 100, 200, 300)
    assert gui_edit.nearest_handle(100, 300, rect, grab_pt=3) == 3
    assert gui_edit.nearest_handle(200, 300, rect, grab_pt=3) == 2
    assert gui_edit.nearest_handle(150, 200, rect, grab_pt=3) is None


def test_box_at_prefers_a_later_overlapping_box() -> None:
    """The box drawn last paints on top, so it is the one the user means."""
    rects = [_r(0, 0, 100, 100), _r(50, 50, 150, 150)]
    assert gui_edit.box_at(75, 75, rects, grab_pt=1) == 1


def test_box_at_grabs_an_edge_before_the_box_underneath() -> None:
    """Clicking a border must resize, not start a move of whatever is behind."""
    rects = [_r(0, 0, 200, 200), _r(100, 100, 300, 300)]
    # 2 pt from the first box's right edge, which lies inside the second box.
    assert gui_edit.box_at(198, 150, rects, grab_pt=3) == 0


def test_box_at_misses_empty_page() -> None:
    assert gui_edit.box_at(300, 400, [_r(0, 0, 100, 100)], grab_pt=2) is None
    assert gui_edit.box_at(300, 400, [], grab_pt=2) is None


def test_overlap_ignores_a_shared_edge() -> None:
    a = _r(0, 0, 100, 100)
    assert not gui_edit.rects_overlap(a, _r(100, 0, 200, 100))
    assert gui_edit.rects_overlap(a, _r(99, 0, 200, 100), tolerance_pt=0.5)


# ---------------------------------------------------------------------------
# ManualBoxStore
# ---------------------------------------------------------------------------


def test_store_returns_copies_not_live_lists() -> None:
    """A caller mutating the result must not reach into the store."""
    store = gui_edit.ManualBoxStore()
    store.set(0, [_r(0, 0, 10, 10)])
    boxes = store.get(0)
    boxes.append(_r(0, 0, 1, 1))
    assert store.count(0) == 1


def test_boxes_are_unmarked_until_the_user_takes_ownership() -> None:
    """Seeding a page from the detector must not silently detach it."""
    store = gui_edit.ManualBoxStore()
    store.set(0, [_r(0, 0, 10, 10)])
    assert not store.is_manual(0)
    store.mark(0)
    assert store.is_manual(0)


def test_deleting_every_box_keeps_the_page_manual() -> None:
    """A page the user deliberately emptied must stay empty, not revert."""
    store = gui_edit.ManualBoxStore()
    store.mark(0)
    store.set(0, [_r(0, 0, 10, 10)])
    store.remove(0, 0)
    assert store.is_manual(0)
    assert store.count(0) == 0


def test_reset_page_hands_the_page_back_to_the_detector() -> None:
    store = gui_edit.ManualBoxStore()
    store.mark(2)
    store.set(2, [_r(0, 0, 10, 10)])
    store.reset_page(2)
    assert not store.is_manual(2)
    assert store.get(2) == []


def test_manual_pages_are_reported_in_page_order() -> None:
    store = gui_edit.ManualBoxStore()
    for p in (4, 0, 2):
        store.mark(p)
    assert store.manual_pages == [0, 2, 4]
    assert not store.is_empty


def test_clear_wipes_every_page() -> None:
    store = gui_edit.ManualBoxStore()
    store.mark(0)
    store.set(0, [_r(0, 0, 1, 1)])
    store.mark(1)
    store.clear()
    assert store.is_empty and store.manual_pages == []


def test_add_and_update_track_indices() -> None:
    store = gui_edit.ManualBoxStore()
    i0 = store.add(0, _r(0, 0, 10, 10))
    i1 = store.add(0, _r(20, 20, 30, 30))
    assert (i0, i1) == (0, 1)
    store.update(0, 0, _r(5, 5, 15, 15))
    assert store.get(0)[0] == _r(5, 5, 15, 15)


# ---------------------------------------------------------------------------
# Hybrid resolution
# ---------------------------------------------------------------------------


def test_manual_page_exports_exactly_what_the_user_drew() -> None:
    store = gui_edit.ManualBoxStore()
    drawn = _r(120, 200, 300, 480)
    store.mark(0)
    store.set(0, [drawn])
    out = store.resolve(0, [_r(0, 0, 400, 600)], margin_pt=mm_to_pt(3.0), page=PAGE)
    assert out == [drawn]


def test_manual_boxes_are_not_reinflated_by_the_margin() -> None:
    """Re-applying margin would undo the whitespace the user just trimmed."""
    store = gui_edit.ManualBoxStore()
    store.mark(0)
    store.set(0, [_r(120, 200, 300, 480)])
    out = store.resolve(0, [], margin_pt=mm_to_pt(3.0), page=PAGE)
    assert out[0].width == pytest.approx(180)
    assert out[0].height == pytest.approx(280)


def test_untouched_pages_still_get_the_detector_boxes_with_margin() -> None:
    store = gui_edit.ManualBoxStore()
    store.mark(0)
    store.set(0, [_r(0, 0, 10, 10)])
    detected = [_r(100, 100, 200, 200)]
    out = store.resolve(1, detected, margin_pt=mm_to_pt(2.0), page=PAGE)
    assert len(out) == 1
    assert out[0].width == pytest.approx(100 + 2 * mm_to_pt(2.0))


def test_summary_reads_naturally_for_none_one_and_many() -> None:
    store = gui_edit.ManualBoxStore()
    assert "自动检测" in store.summary()
    store.mark(2)
    assert "第 3 页" in store.summary()
    store.mark(0)
    assert "2 页" in store.summary()


# ---------------------------------------------------------------------------
# Export integration
# ---------------------------------------------------------------------------


def _cropbox(path: Path) -> fitz.Rect:
    """The CropBox exactly as PyMuPDF reports it (top-down origin)."""
    doc = fitz.open(str(path))
    try:
        return doc[0].cropbox
    finally:
        doc.close()


def _cropbox_user_space(path: Path) -> fitz.Rect:
    """The CropBox converted back to PDF user space (bottom-up origin).

    ``Page.get_cropbox`` reports MuPDF's top-down convention, while every rect
    in this package -- detector output, expected crops, ``rect`` arguments --
    is bottom-up.  Comparing one against the other without this flip is the
    mistake that let a vertically mirrored crop pass every coordinate assertion.
    """
    cb = _cropbox(path)
    page_h = _page_height(path)
    return fitz.Rect(cb.x0, page_h - cb.y1, cb.x1, page_h - cb.y0)


def _page_height(path: Path) -> float:
    """Page height, read from the MediaBox (unaffected by any CropBox)."""
    doc = fitz.open(str(path))
    try:
        return float(doc[0].mediabox.height)
    finally:
        doc.close()



def test_export_single_honours_manual_boxes(tmp_path: Path) -> None:
    """One hand-drawn box must replace the three the detector found."""
    from pdf_auto_crop.gui import _export_single

    src = FIXTURES_DIAG / "D7_mixed_gaps.pdf"
    out = tmp_path / "manual"
    drawn = _r(100, 200, 300, 500)
    _, count = _export_single(src, out, CropConfig(), {0: [drawn]})
    assert count == 1
    _assert_rect(_cropbox_user_space(out / "figure_p01_f01.pdf"), drawn)


def test_export_single_mixes_manual_and_detected_pages(tmp_path: Path) -> None:
    """The hybrid: page 0 by hand, the rest by the detector."""
    from pdf_auto_crop.config import CropConfig as Cfg
    from pdf_auto_crop.gui import _export_single

    src = FIXTURES / "case6_multipage.pdf"
    doc = fitz.open(str(src))
    detected_counts = []
    from pdf_auto_crop.pipeline import get_detector
    from pdf_auto_crop.pdf_processor import render_page_to_array

    cfg = Cfg()
    for i in range(len(doc)):
        rgb, size = render_page_to_array(doc, i, cfg.dpi)
        detected_counts.append(len(get_detector(cfg)(rgb, size, cfg)))
    doc.close()

    out = tmp_path / "hybrid"
    _, count = _export_single(src, out, cfg, {0: [_r(50, 50, 200, 200)]})
    assert count == 1 + sum(detected_counts[1:])

    # Page index 1 was never touched, so its figures come from the detector.
    p2 = sorted(out.glob("figure_p02_*"))
    assert len(p2) == detected_counts[1]
    first = _cropbox_user_space(p2[0])
    assert first.width > 0 and first.height > 0
    # ...while the page the user drew is the only one with a hand-sized crop.
    _assert_rect(_cropbox_user_space(out / "figure_p01_f01.pdf"), _r(50, 50, 200, 200))


def test_export_preserves_vector_content_with_manual_boxes(tmp_path: Path) -> None:
    """Manual cropping must not sneak in a rasterisation."""
    from pdf_auto_crop.gui import _export_single

    src = FIXTURES / "case7_vector_only.pdf"
    out = tmp_path / "vec"
    _export_single(src, out, CropConfig(), {0: [_r(40, 60, 520, 760)]})
    doc = fitz.open(str(out / "figure_p01_f01.pdf"))
    try:
        assert len(doc[0].get_images()) == 0
        assert len(doc[0].get_drawings()) > 0
    finally:
        doc.close()


def test_pipeline_without_overrides_is_unchanged(tmp_path: Path) -> None:
    """The default path must still be exactly the detector's result."""
    from pdf_auto_crop.gui import _export_single

    src = FIXTURES_DIAG / "D7_mixed_gaps.pdf"
    auto, auto_n = _export_single(src, tmp_path / "auto", CropConfig())
    manual, manual_n = _export_single(src, tmp_path / "man", CropConfig(), None)
    assert auto_n == manual_n == 3
    _assert_rect(
        _cropbox(auto / "figure_p01_f02.pdf"),
        _cropbox(manual / "figure_p01_f02.pdf"),
    )


# ---------------------------------------------------------------------------
# The white border must be visible, not invisible
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("margin_mm", [0.0, 1.0, 3.0, 8.0])
@pytest.mark.parametrize("padding_mm", [0.0, 2.0])
def test_shown_boxes_equal_the_exported_cropbox(margin_mm, padding_mm, tmp_path) -> None:
    """The frame drawn on screen must be the frame written to disk.

    Regression test for the bug a user actually hit: the pipeline inflates every
    *detected* rectangle by ``margin_mm + padding_mm``, but the preview drew the
    raw detection.  Every automatic crop therefore came out 3 mm larger on each
    side than the frame drawn around it, and there was no way to tell from the
    UI.  This asserts the two agree to within a hundredth of a millimetre for a
    range of borders.
    """
    from pdf_auto_crop.gui import _export_single

    src = FIXTURES_DIAG / "D7_mixed_gaps.pdf"
    cfg = CropConfig(margin_mm=margin_mm, padding_mm=padding_mm)
    out = tmp_path / f"m{margin_mm}_p{padding_mm}"
    _export_single(src, out, cfg)

    # Reproduce exactly what the canvas would draw: the detection expanded by
    # the same border the pipeline applies.
    doc = fitz.open(str(src))
    try:
        rgb, size_pt = render_page_to_array(doc, 0, cfg.dpi)
        detected = get_detector(cfg)(rgb, size_pt, cfg)
    finally:
        doc.close()
    page = fitz.Rect(0, 0, *size_pt)
    shown = [add_margin_pt(r, mm_to_pt(margin_mm + padding_mm), page) for r in detected]

    assert len(shown) == len(detected)
    for i, rect in enumerate(shown, start=1):
        _assert_rect(
            _cropbox_user_space(out / f"figure_p01_f{i:02d}.pdf"), rect, abs_tol=0.01
        )


def test_manual_boxes_are_never_grown_by_the_border(tmp_path: Path) -> None:
    """The other half of the contract: what you draw is what you get."""
    from pdf_auto_crop.gui import _export_single

    src = FIXTURES_DIAG / "D7_mixed_gaps.pdf"
    drawn = _r(100, 200, 300, 500)
    out = tmp_path / "m"
    _export_single(src, out, CropConfig(margin_mm=3.0, padding_mm=2.0), {0: [drawn]})
    _assert_rect(_cropbox_user_space(out / "figure_p01_f01.pdf"), drawn, abs_tol=0.01)


def test_border_reaches_the_page_edge_but_never_past_it() -> None:
    """A figure touching the page edge gets a one-sided border, not a shift."""
    from pdf_auto_crop.geometry import add_margin_pt

    page = fitz.Rect(0, 0, 595, 842)
    at_edge = _r(0, 0, 100, 100)
    out = add_margin_pt(at_edge, mm_to_pt(3.0), page)
    assert out.x0 == 0 and out.y0 == 0
    assert out.x1 == pytest.approx(100 + mm_to_pt(3.0))


# ---------------------------------------------------------------------------
# Coordinate-convention contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "corner,colour",
    [
        ("top-left", (255, 0, 0)),
        ("top-right", (0, 200, 0)),
        ("bottom-left", (0, 0, 255)),
        ("bottom-right", (255, 0, 255)),
    ],
)
def test_crop_lands_on_the_requested_region(corner, colour, tmp_path) -> None:
    """The cropped page must contain the artwork the rect pointed at.

    Every other test here compares *coordinates*, which is convention-blind: a
    rect read back with the same convention it was written with always matches,
    even when the crop is mirrored.  This one is deliberately empirical.  A
    marker of a unique colour is drawn in each of the four corners, the crop is
    taken around one of them, and the result is rasterised to ask *which colour
    came back*.

    A vertical flip swaps top with bottom and a 180-degree rotation swaps left
    with right as well; because the four markers are different colours, either
    is caught immediately.  Colours are content, not bookkeeping, so no
    coordinate convention can hide from them.
    """
    import numpy as np

    from pdf_auto_crop.pdf_processor import crop_and_save_pdf

    size = 400
    # Markers in PyMuPDF's top-down drawing space, keyed by the corner name.
    top = {
        "top-left": (20, 20, 140, 140),
        "top-right": (size - 140, 20, size - 20, 140),
        "bottom-left": (20, size - 140, 140, size - 20),
        "bottom-right": (size - 140, size - 140, size - 20, size - 20),
    }
    src = tmp_path / "quadrants.pdf"
    doc = fitz.open()
    page = doc.new_page(width=size, height=size)
    for name, (x0, y0, x1, y1) in top.items():
        r, g, b = (255, 0, 0) if name == "top-left" else (
            (0, 200, 0) if name == "top-right" else
            (0, 0, 255) if name == "bottom-left" else (255, 0, 255)
        )
        page.draw_rect(fitz.Rect(x0, y0, x1, y1), color=(r / 255, g / 255, b / 255),
                       fill=(r / 255, g / 255, b / 255), width=0)
    doc.save(str(src))
    doc.close()

    # The crop rect is given in PDF user space (bottom-up), as every caller
    # supplies it -- so convert the top-down marker to that convention here.
    x0, y0, x1, y1 = top[corner]
    user_rect = _r(x0, size - y1, x1, size - y0)

    out = tmp_path / f"crop_{corner}.pdf"
    crop_and_save_pdf(src, 0, user_rect, out)

    d = fitz.open(str(out))
    try:
        pix = d[0].get_pixmap(dpi=72)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n
        )
    finally:
        d.close()
    centre = arr[
        pix.height // 4 : 3 * pix.height // 4, pix.width // 4 : 3 * pix.width // 4
    ]
    mean = centre[:, :, :3].reshape(-1, 3).mean(axis=0)
    got = tuple(int(v) for v in mean)
    want = colour
    assert max(abs(a - b) for a, b in zip(got, want)) < 40, (
        f"cropping the {corner} marker returned {got}, expected ~{want} -- "
        "the crop landed on the wrong region"
    )


def test_export_count_ignores_files_left_by_an_earlier_run(tmp_path: Path) -> None:
    """The status bar must report what this run wrote, not what is on disk.

    Globbing the output directory counts stale figures from a previous,
    differently-parameterised run, so "wrote 7" would show up next to five
    files the user can actually see.  The pipeline now hands back its own tally.
    """
    from pdf_auto_crop.gui import _export_single

    src = FIXTURES_DIAG / "D7_mixed_gaps.pdf"
    out = tmp_path / "stale"
    out.mkdir()
    # A previous run that produced more figures leaves extra files behind.
    for n in range(1, 8):
        (out / f"figure_p01_f{n:02d}.pdf").write_bytes(b"%PDF-1.4 leftover")

    _, count = _export_single(src, out, CropConfig())
    assert count == 3, "the reported count must be this run's tally, not the listing"
    # The leftovers are still on disk -- this function does not delete anything.
    assert len(list(out.glob("*.pdf"))) == 7


def test_detect_then_crop_keeps_the_figures_in_their_own_places(tmp_path: Path) -> None:
    """Full detector -> export -> rasterise, on a page that punishes a mirror.

    The strongest form of the convention test, and the closest to what a user
    actually does.  Two well-separated panels of *different colours* are laid
    out asymmetrically, the real detector splits them, the pipeline crops them,
    and each output is rasterised and checked for its own colour.

    Every fixture in this repository is dense enough that a vertically mirrored
    crop still lands on *some* ink, which is why count-based tests never
    noticed.  A page with one figure above another and whitespace between them
    is the case that exposes it.
    """
    import numpy as np

    from pdf_auto_crop.gui import _export_single

    src = tmp_path / "two_panels.pdf"
    doc = fitz.open()
    page = doc.new_page(width=400, height=600)
    # Red panel high on the page, blue panel low, with a wide empty band
    # between them.  Drawn in PyMuPDF's top-down space.
    page.draw_rect(fitz.Rect(80, 60, 320, 200), color=(1, 0, 0), fill=(1, 0, 0), width=0)
    page.draw_rect(fitz.Rect(80, 400, 320, 540), color=(0, 0, 1), fill=(0, 0, 1), width=0)
    doc.save(str(src))
    doc.close()

    out = tmp_path / "panels_out"
    _, count = _export_single(src, out, CropConfig(margin_mm=0.0))
    assert count == 2, f"expected two figures, detected {count}"

    colours = set()
    for pdf in sorted(out.glob("*.pdf")):
        d = fitz.open(str(pdf))
        try:
            pix = d[0].get_pixmap(dpi=72)
            arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                pix.height, pix.width, pix.n
            )
        finally:
            d.close()
        centre = arr[
            pix.height // 4 : 3 * pix.height // 4, pix.width // 4 : 3 * pix.width // 4
        ][:,:, :3].reshape(-1, 3).mean(axis=0)
        r, g, b = (int(v) for v in centre)
        if r > 120 and b < 120:
            colours.add("red")
        elif b > 120 and r < 120:
            colours.add("blue")
        else:
            pytest.fail(f"{pdf.name} rendered as a blend (rgb={r},{g},{b})")
    assert colours == {"red", "blue"}, (
        f"crops came back as {colours} -- a mirrored crop gives two of one colour"
    )


