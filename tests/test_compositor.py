"""Tests for the figure compositor.

The compositor's whole value proposition is that the PDF it writes looks like
the sheet the user arranged.  So the tests here are mostly *differential*:
the composed output is rendered and compared against an independently computed
expectation, rather than against a recorded number.

The sharpest test in the file is
:func:`test_bake_reproduces_what_the_viewer_sees`, which renders the source
page and the baked figure and demands they agree -- for every page rotation.
A coordinate mistake in that path produces a figure that is subtly, and then
not so subtly, the wrong way round.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pymupdf as fitz
import pytest

from pdf_auto_crop import compositor as comp
from pdf_auto_crop.config import CropConfig

try:  # tkinter may be missing on a headless box
    from pdf_auto_crop import compositor_gui
except ImportError:  # pragma: no cover
    compositor_gui = None

needs_tk = pytest.mark.skipif(
    compositor_gui is None, reason="tkinter is not available")

MM = 72.0 / 25.4


def _assert_rect(actual, expected, *, abs_tol: float = 1e-6) -> None:
    """Compare two Rects component-wise.

    ``pytest.approx`` fails on a :class:`fitz.Rect` even against an
    identical one, because the type's own ``__eq__`` interferes with the
    numeric comparison.
    """
    got, want = tuple(actual), tuple(expected)
    for i, (a, b) in enumerate(zip(got, want)):
        assert a == pytest.approx(b, abs=abs_tol), f"coord {i}: {got} != {want}"


def _build_source(
    tmp_path: Path,
    rotation: int = 0,
    *,
    width: float = 400,
    height: float = 400,
    origin: tuple[float, float] = (0.0, 0.0),
    red: fitz.Rect | None = None,
    marker: bool = False,
) -> tuple[Path, fitz.Rect]:
    """A page with a green field and a red square, in a known region.

    The red square is drawn in page space (top-down).  The returned region is
    in the package's user space: x from the visible rect's left edge, y
    upward from its bottom edge.

    ``marker`` adds a small blue square at the red square's top-left corner.
    It is what makes a rotation mistake visible -- a red square on its own
    looks the same upside down.
    """
    ox, oy = origin
    red = fitz.Rect(red) if red is not None else fitz.Rect(100, 100, 300, 300)
    doc = fitz.open()
    page = doc.new_page(width=width, height=height)
    if origin != (0.0, 0.0):
        # Move the page's origin.  PyMuPDF's page-box setter takes x
        # absolute and y counted down from the MediaBox's top edge.
        page.set_mediabox(fitz.Rect(ox, oy, ox + width, oy + height))
        page.set_cropbox(fitz.Rect(ox, 0, ox + width, height))

    # Drawing happens in the page's normalised frame, whose origin is the
    # visible rectangle's top-left corner.
    page.draw_rect(fitz.Rect(60, 60, width - 60, height - 60),
                   color=(0, 0.6, 0), fill=(0, 0.6, 0), width=0)
    page.draw_rect(red, color=(0.85, 0.1, 0.1),
                   fill=(0.85, 0.1, 0.1), width=0)
    if marker:
        page.draw_rect(fitz.Rect(red.x0 + 10, red.y0 + 10,
                                 red.x0 + 50, red.y0 + 50),
                       color=(0.1, 0.1, 0.85), fill=(0.1, 0.1, 0.85), width=0)
    page.set_rotation(rotation)
    path = tmp_path / f"src{rotation}.pdf"
    doc.save(str(path))
    doc.close()
    region = fitz.Rect(red.x0, height - red.y1, red.x1, height - red.y0)
    return path, region


def _render(path: Path, clip: fitz.Rect | None = None, dpi: int = 100) -> np.ndarray:
    doc = fitz.open(str(path))
    try:
        pix = doc[0].get_pixmap(dpi=dpi, clip=clip)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n)
        if pix.n == 1:
            arr = np.repeat(arr, 3, axis=2)
        return arr[:, :, :3].astype(int)
    finally:
        doc.close()


def _red_bbox(arr: np.ndarray):
    """Where the red square is, in pixels."""
    red = (arr[:, :, 0] > 150) & (arr[:, :, 1] < 120)
    ys, xs = np.where(red)
    if len(xs) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def _marker_corner(arr: np.ndarray) -> str:
    """Which corner of the red square the blue marker sits in."""
    blue = (arr[:, :, 2] > 120) & (arr[:, :, 0] < 120)
    ys, xs = np.where(blue)
    if len(xs) == 0:
        return "none"
    cx, cy = xs.mean() / arr.shape[1], ys.mean() / arr.shape[0]
    return f"{'left' if cx < 0.5 else 'right'}-{'top' if cy < 0.5 else 'bottom'}"


# ---------------------------------------------------------------------------
# Coordinate conversion
# ---------------------------------------------------------------------------


def test_user_space_and_mupdf_space_are_mirror_images() -> None:
    r = fitz.Rect(10, 20, 30, 40)
    top_down = comp.to_mupdf_rect(r, 100.0)
    assert (top_down.x0, top_down.x1) == (10, 30)
    assert (top_down.y0, top_down.y1) == (60, 80)
    _assert_rect(comp.from_mupdf_rect(top_down, 100.0), r)


def test_to_mupdf_rect_round_trips() -> None:
    r = fitz.Rect(12.5, 33.25, 140.75, 260.5)
    back = comp.from_mupdf_rect(comp.to_mupdf_rect(r, 300.0), 300.0)
    _assert_rect(back, r, abs_tol=1e-9)


# ---------------------------------------------------------------------------
# Baking
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_bake_reproduces_what_the_viewer_sees(rotation, tmp_path) -> None:
    """The baked figure must match the source page as a viewer displays it.

    The oracle is the source page's own rendering: render the whole page,
    find the coloured square in the *pixels*, and that bounding box is where
    the region lands on screen.  No coordinate arithmetic is involved, so a
    mistake in the bake cannot satisfy both sides.

    The page is deliberately not square, the region is deliberately not
    centred, and the region carries a marker in one corner.  A square page
    with a centred, unmarked figure is symmetric under every rotation and
    therefore hides exactly the mistakes this test is for.
    """
    src, region = _build_source(
        tmp_path, rotation, width=400, height=600,
        red=fitz.Rect(60, 90, 300, 230), marker=True)
    fig = comp.Figure(path=src, page_index=0, region=region)

    full = _render(src, dpi=72)
    box = _red_bbox(full)
    assert box is not None, "the fixture square did not render"
    x0, y0, x1, y1 = box
    expected = full[y0:y1, x0:x1]
    expected_corner = _marker_corner(expected)

    baked = comp.bake_figure(fig, tmp_path / "cache")
    actual = _render(baked, dpi=72)

    h = min(expected.shape[0], actual.shape[0])
    w = min(expected.shape[1], actual.shape[1])
    assert h > 10 and w > 10
    diff = np.abs(expected[:h, :w] - actual[:h, :w])
    changed = (diff.max(axis=2) > 24).sum()
    assert changed / (h * w) < 0.01, (
        f"rotation {rotation}: {changed}/{h*w} pixels differ "
        f"(max {diff.max()}) -- the bake does not match the viewer's view")
    # The marker pins the orientation down: a figure that came out upside
    # down would still be mostly red, but the marker would be in the wrong
    # corner.
    assert _marker_corner(actual) == expected_corner, (
        f"rotation {rotation}: the figure is turned the wrong way "
        f"({_marker_corner(actual)} vs {expected_corner})")


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_bake_is_right_on_a_page_that_does_not_start_at_the_origin(
        rotation, tmp_path) -> None:
    """A MediaBox that does not start at (0, 0) must not shift the figure."""
    src, region = _build_source(
        tmp_path, rotation, width=400, height=600, origin=(20, 50),
        red=fitz.Rect(60, 90, 300, 230), marker=True)
    fig = comp.Figure(path=src, page_index=0, region=region)

    full = _render(src, dpi=72)
    box = _red_bbox(full)
    assert box is not None
    x0, y0, x1, y1 = box
    expected = full[y0:y1, x0:x1]

    baked = comp.bake_figure(fig, tmp_path / "cache")
    actual = _render(baked, dpi=72)

    h = min(expected.shape[0], actual.shape[0])
    w = min(expected.shape[1], actual.shape[1])
    diff = np.abs(expected[:h, :w] - actual[:h, :w])
    changed = (diff.max(axis=2) > 24).sum()
    assert changed / (h * w) < 0.01, (
        f"rotation {rotation}: {changed}/{h*w} pixels differ -- the origin "
        f"of the page shifted the figure")


def test_embed_rotation_compensates_the_page_rotation() -> None:
    """``/Rotate`` turns the page clockwise; ``show_pdf_page`` turns it back.

    Feeding the page's own rotation straight through would show every
    rotated figure upside down, so the two are compensated here.
    """
    assert comp.embed_rotation(0) == 0
    assert comp.embed_rotation(90) == 270
    assert comp.embed_rotation(180) == 180
    assert comp.embed_rotation(270) == 90
    assert comp.embed_rotation(450) == 270


def test_baked_figure_has_no_content_outside_it(tmp_path) -> None:
    src, region = _build_source(tmp_path)
    fig = comp.Figure(path=src, page_index=0, region=region)
    baked = comp.bake_figure(fig, tmp_path / "cache")
    doc = fitz.open(str(baked))
    try:
        page = doc[0]
        # The visible rect must be exactly the region.
        assert page.cropbox.width == pytest.approx(region.width)
        assert page.cropbox.height == pytest.approx(region.height)
        # And nothing of the surrounding green field may come along: the
        # whole visible area is the red square.
        arr = _render(baked, dpi=72)
        assert (arr[:, :, 0] > 150).all(), "the figure is not the red square"
        assert (arr[:, :, 1] < 120).all(), "green bled into the figure"
    finally:
        doc.close()


def test_baked_page_mediabox_is_the_figure_itself(tmp_path) -> None:
    """The baked file must not carry the rest of the source page."""
    src, region = _build_source(tmp_path)
    fig = comp.Figure(path=src, page_index=0, region=region)
    baked = comp.bake_figure(fig, tmp_path / "cache")
    doc = fitz.open(str(baked))
    try:
        page = doc[0]
        assert page.mediabox.width == pytest.approx(region.width)
        assert page.mediabox.height == pytest.approx(region.height)
    finally:
        doc.close()


def test_bake_is_cached_and_stable(tmp_path) -> None:
    src, region = _build_source(tmp_path)
    fig = comp.Figure(path=src, page_index=0, region=region)
    cache = tmp_path / "cache"
    a = comp.bake_figure(fig, cache)
    b = comp.bake_figure(fig, cache)
    assert a == b
    assert len(list(cache.glob("fig_*.pdf"))) == 1


def test_figure_key_changes_with_the_region(tmp_path) -> None:
    src, region = _build_source(tmp_path)
    a = comp.Figure(path=src, page_index=0, region=region)
    b = comp.Figure(path=src, page_index=0, region=fitz.Rect(110, 100, 300, 300))
    assert a.key != b.key


def test_displayed_size_accounts_for_rotation(tmp_path) -> None:
    """A 90-degree turn swaps the displayed width and height."""
    src, region = _build_source(tmp_path, 90)
    fig = comp.Figure(path=src, page_index=0, region=region)
    w, h = comp.figure_display_size(fig)
    assert w == pytest.approx(200)
    assert h == pytest.approx(200)
    # The source region is square, so use a non-square one to see the swap.
    doc = fitz.open(str(src))
    try:
        vis = comp.source_visible_rect(doc, 0)
        assert vis.width == 400 and vis.height == 400
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------


def test_fit_rect_preserves_aspect_and_centres() -> None:
    target = fitz.Rect(0, 0, 400, 200)
    out = comp.fit_rect(1.0, target)  # square
    assert out.width == pytest.approx(200)
    assert out.height == pytest.approx(200)
    assert out.x0 == pytest.approx(100)
    assert out.y0 == pytest.approx(0)


def test_fit_rect_wide_figure_uses_the_full_width() -> None:
    out = comp.fit_rect(2.0, fitz.Rect(0, 0, 400, 400))
    assert out.width == pytest.approx(400)
    assert out.height == pytest.approx(200)
    assert out.y0 == pytest.approx(100)


def test_auto_label_letters_and_numbers() -> None:
    assert comp.auto_label(1) == "(a)"
    assert comp.auto_label(2) == "(b)"
    assert comp.auto_label(26) == "(z)"
    assert comp.auto_label(27) == "(aa)"
    assert comp.auto_label(3, "number") == "3"


def test_label_rect_sits_below_the_figure() -> None:
    fig = comp.Figure(path=Path("x.pdf"), page_index=0, region=fitz.Rect(0, 0, 10, 10))
    p = comp.Placement(fig, fitz.Rect(100, 200, 300, 400), label="(a)")
    lr = comp.label_rect(p)
    assert lr.y1 <= 200, "the label must not overlap the figure"
    assert lr.x0 == pytest.approx(100)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _two_figure_composition(src: Path, region: fitz.Rect) -> comp.Composition:
    c = comp.Composition(width=400, height=300)
    c.placements.append(comp.Placement(
        comp.Figure(path=src, page_index=0, region=region),
        fitz.Rect(20, 160, 220, 280), label="(a)"))
    c.placements.append(comp.Placement(
        comp.Figure(path=src, page_index=0, region=region),
        fitz.Rect(220, 160, 420, 280), label="(b)"))
    return c


def test_export_writes_vector_content_and_real_text(tmp_path) -> None:
    src, region = _build_source(tmp_path)
    c = _two_figure_composition(src, region)
    out = comp.export_composition(c, tmp_path / "out.pdf")
    doc = fitz.open(str(out))
    try:
        page = doc[0]
        assert len(page.get_drawings()) > 0, "the figures must be vector paths"
        assert len(page.get_images()) == 0, "no raster may be introduced"
        text = page.get_text()
        assert "(a)" in text and "(b)" in text
    finally:
        doc.close()


def test_export_fits_the_content_onto_the_page(tmp_path) -> None:
    """Fitting the page to the content must move the content with it.

    The content is laid out for a 400x300 sheet; the fitted page is smaller.
    If the content were not translated, the figures would end up outside the
    page -- which is exactly what happened before this was fixed.
    """
    src, region = _build_source(tmp_path)
    c = _two_figure_composition(src, region)
    out = comp.export_composition(c, tmp_path / "out.pdf")
    doc = fitz.open(str(out))
    try:
        page = doc[0]
        assert page.rect.height < 300
        assert page.rect.width <= 400 + 1
        # The labels must be on the page, not off the bottom.
        words = page.get_text("words")
        assert words, "no text landed on the page"
        for w in words:
            assert 0 <= w[1] <= page.rect.height
            assert 0 <= w[3] <= page.rect.height
    finally:
        doc.close()


def test_export_with_a_fixed_page_keeps_the_layout(tmp_path) -> None:
    src, region = _build_source(tmp_path)
    c = _two_figure_composition(src, region)
    out = comp.export_composition(
        c, tmp_path / "out.pdf", fit_to_content=False, page_size=(400, 300))
    doc = fitz.open(str(out))
    try:
        assert doc[0].rect.width == pytest.approx(400)
        assert doc[0].rect.height == pytest.approx(300)
    finally:
        doc.close()


def test_export_places_the_figure_where_it_was_dragged(tmp_path) -> None:
    """The composed figure must land on the requested part of the page."""
    src, region = _build_source(tmp_path)
    c = comp.Composition(width=400, height=400)
    c.placements.append(comp.Placement(
        comp.Figure(path=src, page_index=0, region=region),
        fitz.Rect(40, 40, 240, 240), label="(a)"))
    out = comp.export_composition(
        c, tmp_path / "out.pdf", fit_to_content=False, page_size=(400, 400))
    doc = fitz.open(str(out))
    try:
        pix = doc[0].get_pixmap(dpi=72)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n)
        red = (arr[:, :, 0] > 150) & (arr[:, :, 1] < 120)
        ys, xs = np.where(red)
        assert len(xs) > 0
        # The figure *is* the red square, so it has to fill the box it was
        # dropped into: x 40..240, and -- the page's y axis pointing up --
        # y 40..240 from the bottom, i.e. 160..360 from the top.
        assert 38 <= xs.min() <= 42, xs.min()
        assert 237 <= xs.max() <= 241, xs.max()
        assert 158 <= ys.min() <= 162, ys.min()
        assert 357 <= ys.max() <= 361, ys.max()
    finally:
        doc.close()


def test_export_of_a_cjk_label_uses_a_cjk_font(tmp_path) -> None:
    src, region = _build_source(tmp_path)
    c = comp.Composition(width=400, height=400)
    c.placements.append(comp.Placement(
        comp.Figure(path=src, page_index=0, region=region),
        fitz.Rect(40, 40, 240, 240), label="图一"))
    out = comp.export_composition(c, tmp_path / "out.pdf")
    doc = fitz.open(str(out))
    try:
        text = doc[0].get_text()
        assert "图一" in text
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# Labels: font, size, colour
# ---------------------------------------------------------------------------


def test_label_font_falls_back_to_chinese_when_needed() -> None:
    """An ASCII face cannot draw Chinese, so the Chinese face is used."""
    name, fontfile = comp.resolve_label_font("(a)", "Helvetica")
    assert name == "helv"
    assert fontfile == ""

    name, fontfile = comp.resolve_label_font("图一", "Helvetica")
    assert name == comp.FONT_CJK

    # A face that *can* draw it is left alone.
    name, _ = comp.resolve_label_font("图一", "宋体 (中文)")
    assert name == comp.FONT_CJK


def test_an_unknown_font_name_falls_back_to_the_default() -> None:
    name, _ = comp.resolve_label_font("(a)", "No Such Face")
    assert name == comp.LABEL_FONTS[comp.LABEL_FONT_DEFAULT]


def test_text_width_grows_with_the_size() -> None:
    small = comp.text_width("(a)", "tiro", 9)
    large = comp.text_width("(a)", "tiro", 18)
    assert large == pytest.approx(small * 2, rel=0.01)


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


def _shape_composition() -> comp.Composition:
    c = comp.Composition(width=400, height=400)
    c.shapes.append(comp.Shape(comp.SHAPE_ARROW, fitz.Point(100, 100),
                                fitz.Point(300, 300),
                                color=(1.0, 0.0, 0.0), width=2.0))
    c.shapes.append(comp.Shape(comp.SHAPE_RECT, fitz.Point(50, 200),
                                fitz.Point(150, 300),
                                color=(0.0, 0.0, 1.0), width=1.0, fill=True))
    return c


def test_shape_survives_a_json_round_trip(tmp_path) -> None:
    c = _shape_composition()
    path = comp.save_composition(c, tmp_path / "layout.json")
    back = comp.load_composition(path)
    assert len(back.shapes) == 2
    arrow = back.shapes[0]
    assert arrow.kind == comp.SHAPE_ARROW
    assert arrow.start == fitz.Point(100, 100)
    assert arrow.end == fitz.Point(300, 300)
    assert arrow.color == pytest.approx((1.0, 0.0, 0.0))
    assert arrow.width == pytest.approx(2.0)
    assert back.shapes[1].fill is True


def test_placement_keeps_its_font_and_colour_through_json(tmp_path) -> None:
    src, region = _build_source(tmp_path)
    c = comp.Composition(width=400, height=400)
    c.placements.append(comp.Placement(
        comp.Figure(path=src, page_index=0, region=region),
        fitz.Rect(20, 20, 220, 220), label="(a)",
        label_font="Helvetica-Bold", label_size=13.0,
        label_color=(0.8, 0.1, 0.1)))
    path = comp.save_composition(c, tmp_path / "layout.json")
    back = comp.load_composition(path)
    p = back.placements[0]
    assert p.label_font == "Helvetica-Bold"
    assert p.label_size == pytest.approx(13.0)
    assert p.label_color == pytest.approx((0.8, 0.1, 0.1))


def test_shapes_are_part_of_the_content_bounds() -> None:
    c = _shape_composition()
    bounds = c.content_bounds()
    assert bounds is not None
    assert bounds.contains(fitz.Rect(50, 100, 300, 300))


def test_export_draws_the_shapes_as_vectors(tmp_path) -> None:
    c = _shape_composition()
    out = comp.export_composition(
        c, tmp_path / "out.pdf", fit_to_content=False, page_size=(400, 400))
    doc = fitz.open(str(out))
    try:
        page = doc[0]
        assert len(page.get_images()) == 0, "a shape must not become a bitmap"
        assert len(page.get_drawings()) >= 4, "the shapes are missing"
    finally:
        doc.close()


def test_the_shape_lands_where_it_was_drawn(tmp_path) -> None:
    """The red arrow must be on the page, where it was put."""
    c = comp.Composition(width=400, height=400)
    c.shapes.append(comp.Shape(comp.SHAPE_ARROW, fitz.Point(100, 100),
                                fitz.Point(300, 300), color=(1.0, 0.0, 0.0)))
    out = comp.export_composition(
        c, tmp_path / "out.pdf", fit_to_content=False, page_size=(400, 400))
    arr = _render(out, dpi=72)
    red = (arr[:, :, 0] > 150) & (arr[:, :, 1] < 120) & (arr[:, :, 2] < 120)
    ys, xs = np.where(red)
    assert len(xs) > 0
    # The arrow runs from (100, 100) to (300, 300) in user space, i.e. from
    # the top-left to the bottom-right of the rendered page.
    assert 95 <= xs.min() <= 115, xs.min()
    assert 285 <= xs.max() <= 305, xs.max()
    assert 95 <= ys.min() <= 115, ys.min()
    assert 285 <= ys.max() <= 305, ys.max()


def test_fitting_the_page_moves_the_shapes_with_it(tmp_path) -> None:
    """The page is resized to the content, so the shapes move with it."""
    c = comp.Composition(width=800, height=800)
    c.shapes.append(comp.Shape(comp.SHAPE_ARROW, fitz.Point(400, 400),
                                fitz.Point(500, 500), color=(1.0, 0.0, 0.0)))
    out = comp.export_composition(c, tmp_path / "out.pdf")
    doc = fitz.open(str(out))
    try:
        assert doc[0].rect.width == pytest.approx(100, abs=2)
        assert doc[0].rect.height == pytest.approx(100, abs=2)
    finally:
        doc.close()


def test_the_label_is_written_in_the_chosen_font_and_colour(tmp_path) -> None:
    src, region = _build_source(tmp_path)
    c = comp.Composition(width=400, height=400)
    c.placements.append(comp.Placement(
        comp.Figure(path=src, page_index=0, region=region),
        fitz.Rect(20, 20, 220, 220), label="(a)",
        label_font="Helvetica-Bold", label_size=13.0,
        label_color=(0.8, 0.1, 0.1)))
    out = comp.export_composition(
        c, tmp_path / "out.pdf", fit_to_content=False, page_size=(400, 400))
    doc = fitz.open(str(out))
    try:
        spans = [s for b in doc[0].get_text("dict")["blocks"]
                 for line in b.get("lines", []) for s in line["spans"]]
        assert len(spans) == 1
        span = spans[0]
        assert span["text"] == "(a)"
        assert "Helvetica" in span["font"]
        assert span["size"] == pytest.approx(13.0, abs=0.2)
        assert span["color"] == 0xCC1A1A, hex(span["color"])
    finally:
        doc.close()


def test_a_font_file_with_a_space_in_its_name_is_usable(tmp_path) -> None:
    """Windows font files are called "Times New Roman.ttf" and the like.

    The name goes into the PDF as a resource name, where a space is illegal,
    so the export used to fail with "bad fontname chars".
    """
    src, region = _build_source(tmp_path)
    font = tmp_path / "Times New Roman.ttf"
    if not Path("C:/Windows/Fonts/times.ttf").exists():
        pytest.skip("no system font to copy")
    shutil.copy("C:/Windows/Fonts/times.ttf", font)

    name, fontfile = comp.resolve_label_font("(a)", "", str(font))
    assert " " not in name
    assert name == "Times_New_Roman"

    c = comp.Composition(width=400, height=400)
    c.placements.append(comp.Placement(
        comp.Figure(path=src, page_index=0, region=region),
        fitz.Rect(20, 20, 220, 220), label="(a)",
        label_font="Times New Roman.ttf", label_fontfile=str(font)))
    out = comp.export_composition(
        c, tmp_path / "out.pdf", fit_to_content=False, page_size=(400, 400))
    doc = fitz.open(str(out))
    try:
        assert "(a)" in doc[0].get_text()
        assert doc.get_page_fonts(0), "the font was not embedded"
    finally:
        doc.close()


def test_the_canvas_font_follows_the_chosen_face() -> None:
    """Tk does not know a base-14 name, so the preview needs a real family."""
    assert compositor_gui.tk_font("Times-Roman", 12) == (
        "Times New Roman", 12, "normal")
    assert compositor_gui.tk_font("Helvetica-Bold", 12) == (
        "Arial", 12, "bold")
    assert compositor_gui.tk_font("Times-Bold Italic", 12) == (
        "Times New Roman", 12, "bold italic")
    assert compositor_gui.tk_font("Courier", 12) == ("Courier New", 12, "normal")
    # A size below Tk's minimum is bumped up rather than refused.
    assert compositor_gui.tk_font("Times-Roman", 2)[1] == 8


# ---------------------------------------------------------------------------
# Alignment
# ---------------------------------------------------------------------------


def test_alignment_lines_a_group_up_against_each_other() -> None:
    left = fitz.Rect(10, 10, 60, 40)
    right = fitz.Rect(100, 80, 150, 110)
    rects = [left, right]

    # The reference is the group's own bounding box.
    target = comp.align_target(rects, comp.ALIGN_LEFT)
    assert target == 10
    assert comp.aligned_rect(left, comp.ALIGN_LEFT, target).x0 == 10
    assert comp.aligned_rect(right, comp.ALIGN_LEFT, target).x0 == 10
    # The right edges meet on the rightmost one.
    target = comp.align_target(rects, comp.ALIGN_RIGHT)
    assert target == 150
    assert comp.aligned_rect(left, comp.ALIGN_RIGHT, target).x1 == 150
    # The centres meet in the middle of the group.
    target = comp.align_target(rects, comp.ALIGN_HCENTER)
    assert target == 80
    assert comp.aligned_rect(left, comp.ALIGN_HCENTER, target).x0 == 55
    # Vertically, "top" is the larger y -- the sheet counts upwards.
    target = comp.align_target(rects, comp.ALIGN_TOP)
    assert target == 110
    assert comp.aligned_rect(left, comp.ALIGN_TOP, target).y1 == 110
    target = comp.align_target(rects, comp.ALIGN_BOTTOM)
    assert target == 10
    target = comp.align_target(rects, comp.ALIGN_VCENTER)
    assert target == 60


def test_alignment_keeps_the_size_of_the_rectangle() -> None:
    r = fitz.Rect(10, 20, 60, 70)
    moved = comp.aligned_rect(r, comp.ALIGN_LEFT, 0)
    assert moved.width == r.width
    assert moved.height == r.height
    assert moved.x0 == 0
    assert moved.y0 == 20


def test_a_lone_figure_is_aligned_against_the_sheet() -> None:
    """With one object there is nobody to line up with, so the sheet is it."""
    sheet = fitz.Rect(0, 0, 842, 595)
    r = fitz.Rect(300, 200, 400, 260)
    assert comp.align_target([r], comp.ALIGN_LEFT, sheet) == 0
    assert comp.align_target([r], comp.ALIGN_RIGHT, sheet) == 842
    assert comp.align_target([r], comp.ALIGN_TOP, sheet) == 595
    assert comp.align_target([r], comp.ALIGN_BOTTOM, sheet) == 0
    assert comp.align_target([r], comp.ALIGN_HCENTER, sheet) == 421


def test_an_unknown_alignment_mode_is_refused() -> None:
    with pytest.raises(ValueError):
        comp.align_target([fitz.Rect(0, 0, 1, 1)], "sideways")
    with pytest.raises(ValueError):
        comp.aligned_rect(fitz.Rect(0, 0, 1, 1), "sideways", 0.0)


# ---------------------------------------------------------------------------
# Distribution
# ---------------------------------------------------------------------------


def test_distributing_spreads_the_gaps_evenly() -> None:
    """The outer two stay put; the gap between them is shared out."""
    rects = [fitz.Rect(0, 0, 10, 10), fitz.Rect(20, 0, 30, 10),
             fitz.Rect(50, 0, 60, 10)]
    out = comp.distribute_rects(rects, comp.DISTRIBUTE_HORIZONTAL)
    # The first and the last did not move.
    assert out[0].x0 == 0
    assert out[2].x0 == 50
    # The gaps are now 15 and 15.
    assert out[1].x0 == 25
    assert out[1].x0 - out[0].x1 == 15
    assert out[2].x0 - out[1].x1 == 15


def test_distributing_keeps_the_sizes() -> None:
    rects = [fitz.Rect(0, 0, 10, 10), fitz.Rect(20, 0, 35, 10),
             fitz.Rect(50, 0, 60, 10)]
    out = comp.distribute_rects(rects, comp.DISTRIBUTE_HORIZONTAL)
    assert out[1].width == 15
    assert out[1].height == 10


def test_distributing_vertically_counts_upwards() -> None:
    """The sheet's y points up, so the lowest object comes first."""
    rects = [fitz.Rect(0, 0, 10, 10), fitz.Rect(0, 20, 10, 30),
             fitz.Rect(0, 50, 10, 60)]
    out = comp.distribute_rects(rects, comp.DISTRIBUTE_VERTICAL)
    assert out[0].y0 == 0
    assert out[2].y0 == 50
    assert out[1].y0 == 25
    assert out[1].y0 - out[0].y1 == 15
    assert out[2].y0 - out[1].y1 == 15


def test_distributing_needs_three_objects() -> None:
    rects = [fitz.Rect(0, 0, 10, 10), fitz.Rect(20, 0, 30, 10)]
    out = comp.distribute_rects(rects, comp.DISTRIBUTE_HORIZONTAL)
    assert [r.x0 for r in out] == [0, 20]
    with pytest.raises(ValueError):
        comp.distribute_rects(rects, "sideways")


# ---------------------------------------------------------------------------
# Free labels and free text
# ---------------------------------------------------------------------------


def test_a_label_can_be_put_where_the_user_wants(tmp_path) -> None:
    src, region = _build_source(tmp_path)
    c = comp.Composition(width=400, height=400)
    c.placements.append(comp.Placement(
        comp.Figure(path=src, page_index=0, region=region),
        fitz.Rect(100, 100, 300, 250), label="(a)",
        label_mode=comp.LABEL_FREE, label_offset=(20.0, 40.0)))
    # The label's anchor follows the figure's corner plus the offset.
    anchor = c.placements[0].label_anchor()
    assert anchor.x == pytest.approx(120.0)
    assert anchor.y == pytest.approx(140.0)
    # Moving the figure carries the label along.
    c.placements[0].rect = fitz.Rect(150, 150, 350, 300)
    anchor = c.placements[0].label_anchor()
    assert anchor.x == pytest.approx(170.0)
    assert anchor.y == pytest.approx(190.0)


def test_a_free_label_is_written_where_it_sits(tmp_path) -> None:
    src, region = _build_source(tmp_path)
    c = comp.Composition(width=400, height=400)
    c.placements.append(comp.Placement(
        comp.Figure(path=src, page_index=0, region=region),
        fitz.Rect(100, 100, 300, 250), label="(a)",
        label_mode=comp.LABEL_FREE, label_offset=(20.0, 40.0)))
    out = comp.export_composition(
        c, tmp_path / "out.pdf", fit_to_content=False, page_size=(400, 400))
    doc = fitz.open(str(out))
    try:
        spans = [s for b in doc[0].get_text("dict")["blocks"]
                 for line in b.get("lines", []) for s in line["spans"]]
        assert spans
        # The page counts downwards, so the label's baseline is at
        # 400 - 140 = 260.
        assert spans[0]["origin"][1] == pytest.approx(260, abs=1)
    finally:
        doc.close()


def test_a_free_text_is_written_as_real_text(tmp_path) -> None:
    c = comp.Composition(width=400, height=400)
    c.texts.append(comp.TextItem(
        text="Hello", position=fitz.Point(50, 300), size=12.0,
        color=(0.1, 0.2, 0.3)))
    out = comp.export_composition(
        c, tmp_path / "out.pdf", fit_to_content=False, page_size=(400, 400))
    doc = fitz.open(str(out))
    try:
        assert "Hello" in doc[0].get_text()
        spans = [s for b in doc[0].get_text("dict")["blocks"]
                 for line in b.get("lines", []) for s in line["spans"]]
        assert spans[0]["color"] == 0x1A334D
    finally:
        doc.close()


def test_a_free_text_survives_a_json_round_trip(tmp_path) -> None:
    item = comp.TextItem(
        text="x = 1", position=fitz.Point(10, 20), size=9.5,
        font="Helvetica-Bold", color=(0.2, 0.4, 0.6), bold=True,
        italic=True, formula=True, box=True, box_color=(0.1, 0.1, 0.1),
        box_width=1.5)
    back = comp.TextItem.from_dict(item.to_dict())
    assert back.text == "x = 1"
    assert back.position.x == pytest.approx(10.0)
    assert back.size == pytest.approx(9.5)
    assert back.font == "Helvetica-Bold"
    assert back.color == pytest.approx((0.2, 0.4, 0.6))
    assert back.bold is True
    assert back.italic is True
    assert back.formula is True
    assert back.box is True
    assert back.box_color == pytest.approx((0.1, 0.1, 0.1))
    assert back.box_width == pytest.approx(1.5)


def test_the_composition_carries_the_texts_through_json(tmp_path) -> None:
    c = comp.Composition(width=400, height=400)
    c.texts.append(comp.TextItem(text="note", position=fitz.Point(5, 6)))
    back = comp.Composition.from_dict(c.to_dict())
    assert len(back.texts) == 1
    assert back.texts[0].text == "note"


def test_bold_and_italic_pick_the_right_variant() -> None:
    assert comp.apply_style("tiro") == "tiro"
    assert comp.apply_style("tiro", bold=True) == "tibo"
    assert comp.apply_style("tiro", italic=True) == "tiit"
    assert comp.apply_style("tiro", bold=True, italic=True) == "tibiz"
    assert comp.apply_style("helv", bold=True) == "hebo"
    assert comp.apply_style("cour", italic=True) == "coit"
    # A face without a variant is left alone.
    assert comp.apply_style("symb", bold=True) == "symb"


def test_a_bold_label_reaches_the_pdf(tmp_path) -> None:
    src, region = _build_source(tmp_path)
    c = comp.Composition(width=400, height=400)
    c.placements.append(comp.Placement(
        comp.Figure(path=src, page_index=0, region=region),
        fitz.Rect(20, 20, 220, 220), label="(a)", label_bold=True))
    out = comp.export_composition(
        c, tmp_path / "out.pdf", fit_to_content=False, page_size=(400, 400))
    doc = fitz.open(str(out))
    try:
        spans = [s for b in doc[0].get_text("dict")["blocks"]
                 for line in b.get("lines", []) for s in line["spans"]]
        assert spans
        assert "Bold" in spans[0]["font"] or "bold" in spans[0]["font"].lower()
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# Turning, and the PowerPoint-like shapes
# ---------------------------------------------------------------------------


def test_a_point_is_turned_about_a_centre() -> None:
    centre = fitz.Point(100, 100)
    turned = comp.rotate_point(fitz.Point(110, 100), centre, 90)
    # A quarter turn in the page's frame (which counts downwards) puts the
    # point below the centre.
    assert turned.x == pytest.approx(100, abs=1e-6)
    assert turned.y == pytest.approx(110, abs=1e-6)


def test_a_turned_rectangle_takes_up_a_taller_box() -> None:
    rect = fitz.Rect(0, 0, 100, 50)
    turned = comp.rotated_bbox(rect, 90)
    assert turned.width == pytest.approx(50, abs=1e-6)
    assert turned.height == pytest.approx(100, abs=1e-6)
    # The middle does not move.
    assert turned.x0 + turned.width / 2 == pytest.approx(50, abs=1e-6)
    assert turned.y0 + turned.height / 2 == pytest.approx(25, abs=1e-6)
    # No turn means no change.
    same = comp.rotated_bbox(rect, 0)
    assert same.x0 == 0 and same.y0 == 0


def test_a_turned_shape_is_drawn_turned(tmp_path) -> None:
    """The triangle's apex has to move to the right."""
    c = comp.Composition(width=200, height=200)
    c.shapes.append(comp.Shape(
        comp.SHAPE_TRIANGLE, fitz.Point(50, 150), fitz.Point(100, 180),
        color=(0.0, 0.0, 1.0), width=1, rotation=90))
    out = comp.export_composition(
        c, tmp_path / "out.pdf", fit_to_content=False, page_size=(200, 200))
    doc = fitz.open(str(out))
    try:
        pix = doc[0].get_pixmap(dpi=72)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n)[:, :, :3].astype(int)
    finally:
        doc.close()
    blue = (arr[:, :, 2] > 100) & (arr[:, :, 0] < 100)
    ys, xs = np.where(blue)
    assert len(xs)
    # The apex is now on the right, so the ink is taller than it is wide.
    assert xs.max() - xs.min() < ys.max() - ys.min()


def test_the_polygon_shapes_are_inscribed_in_their_box() -> None:
    box = fitz.Rect(0, 0, 100, 50)
    triangle = comp.shape_outline(comp.SHAPE_TRIANGLE, box)
    assert len(triangle) == 3
    assert min(p.y for p in triangle) == box.y0
    assert max(p.y for p in triangle) == box.y1
    diamond = comp.shape_outline(comp.SHAPE_DIAMOND, box)
    assert len(diamond) == 4
    assert min(p.x for p in diamond) == box.x0
    assert max(p.x for p in diamond) == box.x1
    # The right arrow points to the right.
    arrow = comp.shape_outline(comp.SHAPE_RIGHT_ARROW, box)
    assert max(p.x for p in arrow) == box.x1
    left = comp.shape_outline(comp.SHAPE_LEFT_ARROW, box)
    assert min(p.x for p in left) == box.x0
    up = comp.shape_outline(comp.SHAPE_UP_ARROW, box)
    assert min(p.y for p in up) == box.y0
    down = comp.shape_outline(comp.SHAPE_DOWN_ARROW, box)
    assert max(p.y for p in down) == box.y1


def test_every_shape_kind_is_drawn(tmp_path) -> None:
    """No shape may silently draw nothing."""
    for kind in comp.SHAPE_KINDS:
        c = comp.Composition(width=200, height=200)
        c.shapes.append(comp.Shape(
            kind, fitz.Point(40, 120), fitz.Point(160, 180),
            color=(0.0, 0.0, 1.0), width=2))
        out = comp.export_composition(
            c, tmp_path / f"{kind}.pdf", fit_to_content=False,
            page_size=(200, 200))
        doc = fitz.open(str(out))
        try:
            assert doc[0].get_drawings(), f"{kind} drew nothing"
        finally:
            doc.close()


def test_a_shape_can_carry_a_caption(tmp_path) -> None:
    c = comp.Composition(width=200, height=200)
    c.shapes.append(comp.Shape(
        comp.SHAPE_RECT, fitz.Point(40, 120), fitz.Point(160, 180),
        color=(0.0, 0.0, 1.0), width=2, text="Note"))
    out = comp.export_composition(
        c, tmp_path / "out.pdf", fit_to_content=False, page_size=(200, 200))
    doc = fitz.open(str(out))
    try:
        assert "Note" in doc[0].get_text()
    finally:
        doc.close()


def test_a_turned_figure_keeps_its_size(tmp_path) -> None:
    """A quarter turn swaps the figure's width and height."""
    src, region = _build_source(tmp_path, red=fitz.Rect(50, 60, 250, 160))
    assert region.width == pytest.approx(200)
    assert region.height == pytest.approx(100)
    c = comp.Composition(width=400, height=400)
    c.placements.append(comp.Placement(
        comp.Figure(path=src, page_index=0, region=region),
        fitz.Rect(50, 150, 250, 250), label="", rotation=90))
    out = comp.export_composition(
        c, tmp_path / "out.pdf", fit_to_content=False, page_size=(400, 400))
    doc = fitz.open(str(out))
    try:
        pix = doc[0].get_pixmap(dpi=72)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n)[:, :, :3].astype(int)
    finally:
        doc.close()
    ink = arr.sum(axis=2) < 720
    ys, xs = np.where(ink)
    assert len(xs)
    # The figure was 200 wide and 100 tall; turned, it is the other way.
    assert xs.max() - xs.min() == pytest.approx(100, abs=3)
    assert ys.max() - ys.min() == pytest.approx(200, abs=3)


def test_the_rotation_survives_a_json_round_trip(tmp_path) -> None:
    src, region = _build_source(tmp_path)
    c = comp.Composition(width=400, height=400)
    c.placements.append(comp.Placement(
        comp.Figure(path=src, page_index=0, region=region),
        fitz.Rect(20, 20, 220, 220), label="(a)", rotation=45))
    c.shapes.append(comp.Shape(
        comp.SHAPE_TRIANGLE, fitz.Point(10, 10), fitz.Point(50, 40),
        rotation=30, text="x", text_size=11.0))
    back = comp.Composition.from_dict(c.to_dict())
    assert back.placements[0].rotation == pytest.approx(45.0)
    assert back.shapes[0].rotation == pytest.approx(30.0)
    assert back.shapes[0].text == "x"
    assert back.shapes[0].text_size == pytest.approx(11.0)


def test_a_turned_shape_keeps_its_caption(tmp_path) -> None:
    c = comp.Composition(width=200, height=200)
    c.shapes.append(comp.Shape(
        comp.SHAPE_RECT, fitz.Point(40, 120), fitz.Point(160, 180),
        color=(0.0, 0.0, 1.0), width=2, text="Note", rotation=45))
    out = comp.export_composition(
        c, tmp_path / "out.pdf", fit_to_content=False, page_size=(200, 200))
    doc = fitz.open(str(out))
    try:
        assert "Note" in doc[0].get_text()
    finally:
        doc.close()


def test_a_turned_text_stays_real_text(tmp_path) -> None:
    """Turning a text must not turn it into a picture."""
    c = comp.Composition(width=300, height=200)
    c.texts.append(comp.TextItem(
        text="Turned", position=fitz.Point(100, 100), size=12.0,
        rotation=30))
    out = comp.export_composition(
        c, tmp_path / "out.pdf", fit_to_content=False, page_size=(300, 200))
    doc = fitz.open(str(out))
    try:
        assert "Turned" in doc[0].get_text()
        assert doc[0].get_images() == [], "the text was rasterised"
    finally:
        doc.close()


def test_a_turned_text_stays_where_it_was(tmp_path) -> None:
    """Turning a caption must not make it jump away."""
    c = comp.Composition(width=300, height=200)
    c.texts.append(comp.TextItem(
        text="Here", position=fitz.Point(100, 100), size=12.0,
        rotation=90))
    out = comp.export_composition(
        c, tmp_path / "out.pdf", fit_to_content=False, page_size=(300, 200))
    doc = fitz.open(str(out))
    try:
        pix = doc[0].get_pixmap(dpi=72)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n)[:, :, :3].astype(int)
    finally:
        doc.close()
    ink = arr.sum(axis=2) < 720
    ys, xs = np.where(ink)
    assert len(xs)
    # The text starts at (100, 100) in user space, which is (100, 100) in
    # page space here; the ink has to be around there.
    assert 60 < xs.min() < 140, (xs.min(), xs.max())
    assert 60 < ys.min() < 140, (ys.min(), ys.max())


def test_the_text_rotation_survives_a_json_round_trip() -> None:
    item = comp.TextItem(text="x", position=fitz.Point(1, 2), rotation=45)
    back = comp.TextItem.from_dict(item.to_dict())
    assert back.rotation == pytest.approx(45.0)


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------


def test_a_group_survives_a_json_round_trip() -> None:
    c = comp.Composition(width=400, height=400)
    c.figs = []
    group = comp.Group(figs=[0, 2], shapes=[1])
    c.groups.append(group)
    back = comp.Composition.from_dict(c.to_dict())
    assert len(back.groups) == 1
    assert back.groups[0].figs == [0, 2]
    assert back.groups[0].shapes == [1]
    assert back.groups[0].texts == []


def test_the_group_of_an_object_is_found() -> None:
    c = comp.Composition(width=400, height=400)
    c.groups.append(comp.Group(figs=[1, 2]))
    assert c.group_of("figure", 2) is not None
    assert c.group_of("figure", 3) is None
    assert c.group_of("shape", 1) is None


def test_the_groups_follow_the_objects_when_one_is_removed() -> None:
    """Removing an object shifts the ones behind it, and the groups with
    them -- otherwise a group would hold the wrong objects."""
    c = comp.Composition(width=400, height=400)
    c.groups.append(comp.Group(figs=[1, 2, 3]))
    c.regroup_after_removal("figure", 1)
    assert c.groups[0].figs == [1, 2]
    # And when the last member goes, the empty group is dropped.
    c.regroup_after_removal("figure", 1)
    c.regroup_after_removal("figure", 1)
    assert c.groups == []


def test_a_group_keeps_its_members_when_one_is_removed() -> None:
    c = comp.Composition(width=400, height=400)
    c.groups.append(comp.Group(shapes=[0, 2]))
    c.regroup_after_removal("shape", 0)
    assert c.groups[0].shapes == [1]


# ---------------------------------------------------------------------------
# Editing the text inside an imported figure
# ---------------------------------------------------------------------------


def _figure_with_text(tmp_path) -> tuple[Path, comp.Figure]:
    src = tmp_path / "figure.pdf"
    doc = fitz.open()
    page = doc.new_page(width=300, height=220)
    page.draw_rect(fitz.Rect(50, 40, 250, 170), color=(0, 0, 0), width=1)
    page.draw_line(fitz.Point(60, 150), fitz.Point(240, 60),
                   color=(0, 0, 1), width=2)
    page.insert_text(fitz.Point(120, 190), "time t (s)", fontsize=9)
    page.insert_text(fitz.Point(15, 100), "amplitude", fontsize=9)
    doc.save(str(src))
    doc.close()
    figure = comp.Figure(path=src, page_index=0,
                         region=fitz.Rect(10, 30, 260, 200))
    return src, figure


def test_the_text_inside_a_figure_is_recognised(tmp_path) -> None:
    _src, figure = _figure_with_text(tmp_path)
    blocks = comp.figure_text_blocks(figure)
    assert [b.text for b in blocks] == ["time t (s)", "amplitude"]
    # The numbering is stable: the edits are stored by it.
    assert [b.index for b in blocks] == [0, 1]
    assert blocks[0].size == pytest.approx(9.0)


def test_the_text_inside_a_figure_can_be_rewritten(tmp_path) -> None:
    _src, figure = _figure_with_text(tmp_path)
    edited = comp.bake_with_edits(figure, {0: "time t (ms)"})
    doc = fitz.open(str(edited))
    try:
        text = doc[0].get_text()
        assert "time t (ms)" in text
        assert "time t (s)" not in text
        # The artwork is untouched, and nothing was rasterised.
        assert len(doc[0].get_images()) == 0
        assert len(doc[0].get_drawings()) >= 2
    finally:
        doc.close()


def test_a_line_of_text_can_be_taken_away(tmp_path) -> None:
    _src, figure = _figure_with_text(tmp_path)
    edited = comp.bake_with_edits(figure, {1: ""})
    doc = fitz.open(str(edited))
    try:
        text = doc[0].get_text()
        assert "amplitude" not in text
        assert "time t (s)" in text
    finally:
        doc.close()


def test_the_rewritten_text_reaches_the_composition(tmp_path) -> None:
    src, figure = _figure_with_text(tmp_path)
    c = comp.Composition(width=400, height=300)
    c.placements.append(comp.Placement(
        figure=figure, rect=fitz.Rect(20, 20, 320, 220), label="(a)",
        text_edits={0: "time t (ms)", 1: ""}))
    out = comp.export_composition(c, tmp_path / "out.pdf", fit_to_content=False,
                                  page_size=(400, 300))
    doc = fitz.open(str(out))
    try:
        text = doc[0].get_text()
        assert "time t (ms)" in text
        assert "amplitude" not in text
        assert "(a)" in text
        assert doc[0].get_images() == []
    finally:
        doc.close()


def test_the_text_edits_survive_a_json_round_trip(tmp_path) -> None:
    src, region = _build_source(tmp_path)
    c = comp.Placement(comp.Figure(path=src, page_index=0, region=region),
                        fitz.Rect(0, 0, 100, 100), text_edits={0: "x", 2: ""})
    back = comp.Placement.from_dict(c.to_dict())
    assert back.text_edits == {0: "x", 2: ""}


def test_a_rewritten_line_keeps_the_look_of_the_original(tmp_path) -> None:
    """A line written in Times stays in Times."""
    _src, figure = _figure_with_text(tmp_path)
    assert comp._replacement_font("Times-Roman") == "tiro"
    assert comp._replacement_font("Times-Bold") == "tibo"
    assert comp._replacement_font("Helvetica") == "helv"
    assert comp._replacement_font("Courier-Oblique") == "coit"
    assert comp._replacement_font("Symbol") == "symb"


# ---------------------------------------------------------------------------
# Graphic element styling
# ---------------------------------------------------------------------------


def test_a_shape_survives_a_json_round_trip_with_its_style(tmp_path) -> None:
    shape = comp.Shape(
        comp.SHAPE_RECT, fitz.Point(10, 10), fitz.Point(50, 40),
        color=(0.1, 0.2, 0.3), width=2.5, fill=True,
        fill_color=(1.0, 0.9, 0.2), fill_opacity=0.4,
        stroke_opacity=0.6, dash=comp.DASH_DASHED)
    back = comp.Shape.from_dict(shape.to_dict())
    assert back.kind == comp.SHAPE_RECT
    assert back.color == pytest.approx((0.1, 0.2, 0.3))
    assert back.width == pytest.approx(2.5)
    assert back.fill is True
    assert back.fill_color == pytest.approx((1.0, 0.9, 0.2))
    assert back.fill_opacity == pytest.approx(0.4)
    assert back.stroke_opacity == pytest.approx(0.6)
    assert back.dash == comp.DASH_DASHED


def test_a_shape_falls_back_to_the_border_colour_for_its_fill() -> None:
    shape = comp.Shape(comp.SHAPE_RECT, fitz.Point(0, 0), fitz.Point(1, 1),
                       color=(0.2, 0.4, 0.6))
    assert shape.fill_rgb == pytest.approx((0.2, 0.4, 0.6))
    shape.fill_color = (1.0, 0.0, 0.0)
    assert shape.fill_rgb == pytest.approx((1.0, 0.0, 0.0))


def test_a_shape_clamps_its_own_style() -> None:
    shape = comp.Shape(comp.SHAPE_RECT, fitz.Point(0, 0), fitz.Point(1, 1),
                       width=-3, fill_opacity=5.0, stroke_opacity=-1.0,
                       dash="squiggle")
    assert shape.width == pytest.approx(0.1)
    assert shape.fill_opacity == pytest.approx(1.0)
    assert shape.stroke_opacity == pytest.approx(0.0)
    assert shape.dash == comp.DASH_SOLID


def test_the_border_style_reaches_the_pdf(tmp_path) -> None:
    """The dash pattern and the opacities are written into the page."""
    c = comp.Composition(width=200, height=200)
    c.shapes.append(comp.Shape(
        comp.SHAPE_RECT, fitz.Point(20, 20), fitz.Point(180, 180),
        color=(1.0, 0.0, 0.0), width=3.0, fill=True,
        fill_color=(0.0, 0.0, 1.0), fill_opacity=0.5,
        stroke_opacity=0.8, dash=comp.DASH_DASHED))
    out = comp.export_composition(
        c, tmp_path / "out.pdf", fit_to_content=False, page_size=(200, 200))
    doc = fitz.open(str(out))
    try:
        drawings = doc[0].get_drawings()
        assert drawings, "nothing was drawn"
        d = drawings[0]
        assert d["dashes"] and d["dashes"] != "[] 0", d.get("dashes")
        assert d["color"] == (1.0, 0.0, 0.0)
        assert d["fill"] == (0.0, 0.0, 1.0)
        assert d["fill_opacity"] == pytest.approx(0.5, abs=0.02)
        assert d["stroke_opacity"] == pytest.approx(0.8, abs=0.02)
        assert d["width"] == pytest.approx(3.0, abs=0.1)
    finally:
        doc.close()


def test_the_fill_opacity_reaches_the_pixels(tmp_path) -> None:
    """A half-transparent fill really is half-transparent on the page."""
    c = comp.Composition(width=100, height=100)
    c.shapes.append(comp.Shape(
        comp.SHAPE_RECT, fitz.Point(10, 10), fitz.Point(90, 90),
        color=(0.0, 0.0, 0.0), width=1.0, fill=True,
        fill_color=(1.0, 0.0, 0.0), fill_opacity=0.5))
    out = comp.export_composition(
        c, tmp_path / "out.pdf", fit_to_content=False, page_size=(100, 100))
    doc = fitz.open(str(out))
    try:
        pix = doc[0].get_pixmap(dpi=72)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n)[:, :, :3].astype(int)
    finally:
        doc.close()
    # The middle of the box is the fill over white: red at half strength.
    r, g, b = arr[50, 50]
    assert 250 <= r <= 255, (r, g, b)
    assert 120 <= g <= 135, (r, g, b)
    assert 120 <= b <= 135, (r, g, b)


# ---------------------------------------------------------------------------
# Canvas geometry
# ---------------------------------------------------------------------------


@needs_tk
def test_canvas_box_puts_the_top_edge_on_top() -> None:
    """The canvas counts downwards, the sheet counts upwards.

    Getting this backwards puts the picture outside its own frame and makes
    the hit test miss every click -- both of which happened.
    """
    rect = fitz.Rect(100, 200, 300, 500)   # user space, bottom-up
    left, top, right, bottom = compositor_gui.sheet_to_canvas_box(
        rect, origin=(0.0, 0.0), zoom=1.0, sheet_h=600)
    assert left == pytest.approx(100)
    assert right == pytest.approx(300)
    assert top == pytest.approx(100)      # 600 - 500
    assert bottom == pytest.approx(400)   # 600 - 200
    assert top < bottom


@needs_tk
def test_a_handle_drags_the_corner_it_is_drawn_in() -> None:
    rect = fitz.Rect(100, 200, 300, 500)
    left, top, right, bottom = compositor_gui.sheet_to_canvas_box(
        rect, origin=(0.0, 0.0), zoom=1.0, sheet_h=600)
    handles = compositor_gui.HANDLE_POSITIONS(left, top, right, bottom)
    assert handles[0] == (left, top)       # top-left on the canvas
    assert handles[1] == (right, top)      # top-right
    assert handles[2] == (right, bottom)   # bottom-right
    assert handles[3] == (left, bottom)    # bottom-left
    # The top-left handle on the canvas is the *top* left corner of the
    # sheet, which is the highest y in user space.
    assert compositor_gui.USER_CORNER(rect, 0) == (rect.x0, rect.y1)
    assert compositor_gui.USER_CORNER(rect, 1) == (rect.x1, rect.y1)
    assert compositor_gui.USER_CORNER(rect, 2) == (rect.x1, rect.y0)
    assert compositor_gui.USER_CORNER(rect, 3) == (rect.x0, rect.y0)


@needs_tk
def test_the_colour_helpers_round_trip() -> None:
    assert compositor_gui.hex_to_rgb("#ff8000") == pytest.approx(
        (1.0, 0.50196, 0.0), abs=1e-4)
    assert compositor_gui.rgb_to_hex((1.0, 0.50196, 0.0)) == "#ff8000"
    # Nonsense in, sensible out -- the colour picker is not the only source.
    assert compositor_gui.hex_to_rgb("nonsense") == (0.0, 0.0, 0.0)
    assert compositor_gui.rgb_to_hex((2.0, -1.0, 0.5)) == "#ff0080"


@needs_tk
def test_distance_to_segment() -> None:
    assert compositor_gui.distance_to_segment(5, 5, 0, 0, 10, 0) == \
        pytest.approx(5.0)
    # Beyond the end, the distance is to the end point.
    assert compositor_gui.distance_to_segment(20, 0, 0, 0, 10, 0) == \
        pytest.approx(10.0)
    # A degenerate segment is a point.
    assert compositor_gui.distance_to_segment(3, 4, 0, 0, 0, 0) == \
        pytest.approx(5.0)


@needs_tk
def test_a_press_on_a_graphic_element_starts_a_drag() -> None:
    """An element under the cursor must be grabbable.

    Picking the element and leaving the drag mode empty is what makes an
    element sit still while the cursor drags it.
    """
    # The element is on top of the figure: the element wins.
    assert compositor_gui.resolve_pick(2, 1, "body") == (None, 2, None)
    # The element on its own.
    assert compositor_gui.resolve_pick(0, None, None) == (None, 0, None)
    # No element: the figure is picked, and the handle is kept.
    assert compositor_gui.resolve_pick(None, 1, "body") == (1, None, "body")
    assert compositor_gui.resolve_pick(None, 1, "handle2") == (1, None, "handle2")
    # Nothing at all.
    assert compositor_gui.resolve_pick(None, None, None) == (None, None, None)


@needs_tk
def test_the_selection_grows_and_shrinks_with_ctrl() -> None:
    """Ctrl+click adds to the selection, so a group can be lined up."""
    u = compositor_gui.update_selection
    # A plain click replaces the selection.
    assert u([], [], [], "figure", 1, False) == ([1], [], [], "body")
    # Ctrl+click adds.
    assert u([1], [], [], "figure", 3, True) == ([1, 3], [], [], "body")
    # Ctrl+click again takes it back out.
    assert u([1, 3], [], [], "figure", 3, True) == ([1], [], [], None)
    # An element under the cursor wins over a figure underneath it.
    assert u([1], [], [], "shape", 0, False) == ([], [0], [], "shape-body")
    # The three kinds are never selected together.
    assert u([1], [], [], "shape", 0, True) == ([], [0], [], "shape-body")
    assert u([], [2], [], "figure", 1, True) == ([1], [], [], "body")
    assert u([], [], [4], "text", 5, True) == ([], [], [4, 5], "text-body")
    # Clicking the empty sheet clears the selection -- unless Ctrl is held.
    assert u([1], [], [], None, None, False) == ([], [], [], None)
    assert u([1], [], [], None, None, True) == ([1], [], [], None)


# ---------------------------------------------------------------------------
# Detection and persistence
# ---------------------------------------------------------------------------


def test_detect_figures_finds_the_fixtures(tmp_path) -> None:
    src = Path(__file__).resolve().parent / "fixtures_diag" / "D2_3plus1.pdf"
    if not src.exists():
        pytest.skip("diagnostic fixtures not generated")
    found = comp.detect_figures(src, CropConfig())
    assert len(found) == 4
    for f in found:
        assert f.page_index == 0
        assert f.region.width > 0 and f.region.height > 0


def test_composition_survives_a_json_round_trip(tmp_path) -> None:
    src, region = _build_source(tmp_path)
    c = _two_figure_composition(src, region)
    path = comp.save_composition(c, tmp_path / "layout.json")
    back = comp.load_composition(path)
    assert len(back.placements) == 2
    assert back.placements[0].label == "(a)"
    assert back.placements[1].figure.path == src
    _assert_rect(back.placements[1].rect, c.placements[1].rect)


def test_content_bounds_includes_the_labels(tmp_path) -> None:
    src, region = _build_source(tmp_path)
    c = _two_figure_composition(src, region)
    bounds = c.content_bounds()
    assert bounds is not None
    # The label hangs below the lowest figure, so the bounds must reach lower.
    assert bounds.y0 < min(p.rect.y0 for p in c.placements)
