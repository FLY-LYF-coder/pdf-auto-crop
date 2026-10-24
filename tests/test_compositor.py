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
    assert compositor_gui.resolve_pick(2, 1, "body") == (None, 2, "shape-body")
    # The element on its own.
    assert compositor_gui.resolve_pick(0, None, None) == (None, 0, "shape-body")
    # No element: the figure is picked, and the drag mode is the figure's.
    assert compositor_gui.resolve_pick(None, 1, "body") == (1, None, "body")
    assert compositor_gui.resolve_pick(None, 1, "handle2") == (1, None, "handle2")
    # Nothing at all.
    assert compositor_gui.resolve_pick(None, None, None) == (None, None, None)


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
