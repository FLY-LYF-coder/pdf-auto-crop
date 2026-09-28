"""Pure geometry and state for the GUI's manual crop-window editor.

The automatic detector is right most of the time; the manual editor is right
*exactly* right.  This module holds everything both need that is not Tk: the
coordinate round-trip between canvas pixels and PDF points, the per-page store
of user-drawn rectangles, and the merge policy that decides which rectangles an
export actually uses.

Design decisions
----------------

**Per-page hybrid, not per-document.**  A page the user never touched keeps the
detector's rectangles; a page the user drew on keeps the user's rectangles.
This is the whole point of offering both ways -- on a 40-page paper the user
fixes the two pages the detector got wrong instead of redrawing all forty.

**Explicit intent beats implicit.**  A page is only "manual" once the user has
actually drawn on it.  Merely opening the editor, or drawing a rectangle and
deleting it again, must not silently detach that page from the detector, so
:attr:`ManualBoxStore.manual_pages` is driven by :meth:`ManualBoxStore.mark`
and cleared by :meth:`ManualBoxStore.reset_page`.

**The canvas is a view, never the source of truth.**  Window resize, DPI change
and zoom all move canvas pixels around.  Every rectangle is stored in PDF
points the instant it is drawn and only projected back to pixels for painting,
so a resize cannot corrupt the crop.

Coordinate spaces
-----------------

Three spaces, two of them flipped:

* **PDF points** -- bottom-left origin, the space CropBox lives in.
* **Raster pixels** -- top-left origin at ``dpi/72`` scale, what the detector
  works in and what :func:`pdf_auto_crop.geometry.raster_bbox_to_pdf_rect`
  converts from.
* **Canvas pixels** -- top-left origin, additionally scaled by the zoom factor
  and shifted by the centring offset.

The vertical axis flips twice on the way out (PDF -> raster) and twice on the
way back, so a naive copy/paste of the detector's transform is wrong by a
mirror.  :func:`pdf_rect_to_canvas` and :func:`canvas_rect_to_pdf` are the only
two places that do this, and :func:`test_round_trip_recovers_the_rect` exists
specifically to stop them from drifting apart.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import pymupdf as fitz

from .geometry import add_margin_pt

#: Boxes thinner than this many PDF points are treated as an accidental click
#: rather than a deliberate crop.  2 pt is far below any real figure (a 2 pt
#: line) but comfortably above a double-click's coordinate jitter.
MIN_BOX_PT: float = 2.0

#: How close a click must be to a box edge, in canvas pixels, before it grabs
#: that edge instead of selecting the whole box.  Scaled-independent because it
#: is measured on screen, where the user's hand operates.
EDGE_GRAB_PX: float = 6.0

#: Handles drawn at the four corners of the selected box.  4-point tuples in
#: canvas space, filled in by :func:`handle_points`.
_HANDLE_SIZE_PX: float = 4.0


def page_rect(size_pt: Tuple[float, float]) -> fitz.Rect:
    """Build the page's own rectangle from a ``(width_pt, height_pt)`` pair."""
    return fitz.Rect(0.0, 0.0, float(size_pt[0]), float(size_pt[1]))


# ---------------------------------------------------------------------------
# Coordinate conversion
# ---------------------------------------------------------------------------


def pdf_rect_to_canvas(
    rect: fitz.Rect,
    *,
    dpi: int,
    page_height_pt: float,
    zoom: float,
    origin: Tuple[float, float],
) -> Tuple[float, float, float, float]:
    """Project a PDF rect onto the canvas.

    Args:
        rect: Rectangle in PDF points (bottom-left origin).
        dpi: Analysis DPI -- fixes the PDF->pixel scale.
        page_height_pt: Page height, needed to flip the vertical axis.
        zoom: Canvas scale factor.
        origin: ``(dx, dy)`` centring offset in canvas pixels.

    Returns:
        ``(x0, y0, x1, y1)`` in canvas pixels, top-left origin, already
        normalised so ``x0 <= x1`` and ``y0 <= y1``.
    """
    if dpi <= 0:
        raise ValueError("dpi must be positive")
    if zoom <= 0:
        raise ValueError("zoom must be positive")
    scale = dpi / 72.0
    h = float(page_height_pt)
    # PDF y grows upward, raster/canvas y grows downward.
    x0 = rect.x0 * scale
    x1 = rect.x1 * scale
    y0 = (h - rect.y1) * scale
    y1 = (h - rect.y0) * scale
    ox, oy = origin
    cx0 = x0 * zoom + ox
    cy0 = y0 * zoom + oy
    cx1 = x1 * zoom + ox
    cy1 = y1 * zoom + oy
    return (
        min(cx0, cx1),
        min(cy0, cy1),
        max(cx0, cx1),
        max(cy0, cy1),
    )


def canvas_rect_to_pdf(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    *,
    dpi: int,
    page_height_pt: float,
    zoom: float,
    origin: Tuple[float, float],
) -> fitz.Rect:
    """Inverse of :func:`pdf_rect_to_canvas`.

    A drag is reported by Tk as press-position then release-position, and the
    user may drag up-left as easily as down-right, so the incoming corners are
    normalised before the flip rather than after.
    """
    if dpi <= 0:
        raise ValueError("dpi must be positive")
    if zoom <= 0:
        raise ValueError("zoom must be positive")
    ox, oy = origin
    left, right = sorted((float(x0), float(x1)))
    top, bottom = sorted((float(y0), float(y1)))
    scale = dpi / 72.0
    h = float(page_height_pt)
    px0 = (left - ox) / zoom
    px1 = (right - ox) / zoom
    py0 = (top - oy) / zoom
    py1 = (bottom - oy) / zoom
    # Undo the DPI scale, then flip y back into PDF space.
    return fitz.Rect(
        px0 / scale,
        h - py1 / scale,
        px1 / scale,
        h - py0 / scale,
    )


# ---------------------------------------------------------------------------
# Rect utilities
# ---------------------------------------------------------------------------


def normalise(
    x0: float, y0: float, x1: float, y1: float
) -> Tuple[float, float, float, float]:
    """Return corners sorted so ``x0 <= x1`` and ``y0 <= y1``."""
    return (
        min(x0, x1),
        min(y0, y1),
        max(x0, x1),
        max(y0, y1),
    )


def is_too_small(rect: fitz.Rect, min_side_pt: float = MIN_BOX_PT) -> bool:
    """True when either side is below ``min_side_pt`` -- treat as a stray click.

    Degenerate boxes are the one input an export cannot recover from: PyMuPDF
    refuses to set a CropBox with zero width or height, so an accidental click
    would otherwise turn into a hard failure at export time rather than a
    no-op at draw time.
    """
    return rect.width < min_side_pt or rect.height < min_side_pt


def clamp_to_page(rect: fitz.Rect, page: fitz.Rect) -> fitz.Rect:
    """Clamp ``rect`` into ``page`` without raising on an empty result.

    Unlike :func:`pdf_auto_crop.geometry.clamp_rect` this never raises: a drag
    that ends outside the page is simply pulled back to the nearest legal
    position, which is the behaviour a drag handle wants.
    """
    x0 = min(max(rect.x0, page.x0), page.x1)
    y0 = min(max(rect.y0, page.y0), page.y1)
    x1 = min(max(rect.x1, page.x0), page.x1)
    y1 = min(max(rect.y1, page.y0), page.y1)
    return fitz.Rect(*normalise(x0, y0, x1, y1))


def move_rect(
    rect: fitz.Rect,
    dx_pt: float,
    dy_pt: float,
    page: fitz.Rect,
) -> fitz.Rect:
    """Translate ``rect`` by a PDF-space delta, staying inside ``page``.

    The whole box is kept rigid: when the delta would push an edge past the
    page border it is truncated rather than applied, so dragging into the
    margin pins the box against the edge instead of squashing it.
    """
    dx = min(max(dx_pt, page.x0 - rect.x0), page.x1 - rect.x1)
    dy = min(max(dy_pt, page.y0 - rect.y0), page.y1 - rect.y1)
    return fitz.Rect(rect.x0 + dx, rect.y0 + dy, rect.x1 + dx, rect.y1 + dy)


def resize_rect(
    rect: fitz.Rect,
    *,
    left: Optional[float] = None,
    right: Optional[float] = None,
    top: Optional[float] = None,
    bottom: Optional[float] = None,
    page: Optional[fitz.Rect] = None,
) -> fitz.Rect:
    """Move individual edges, clamping to ``page`` when one is supplied.

    Only the edges passed in move; the others keep their position.  This is what
    makes a corner handle feel right -- dragging the top-left corner should not
    also drag the bottom edge along with it.

    ``top`` and ``bottom`` are named for how they look on the page, so in PDF
    space ``top`` is ``y1`` (the larger y) and ``bottom`` is ``y0``.
    """
    x0, y0, x1, y1 = rect.x0, rect.y0, rect.x1, rect.y1
    if left is not None:
        x0 = left
    if right is not None:
        x1 = right
    if top is not None:
        y1 = top
    if bottom is not None:
        y0 = bottom
    out = fitz.Rect(*normalise(x0, y0, x1, y1))
    return clamp_to_page(out, page) if page is not None else out



def rects_overlap(a: fitz.Rect, b: fitz.Rect, tolerance_pt: float = 0.5) -> bool:
    """True when two boxes share area, ignoring slivers thinner than ``tolerance_pt``.

    A hair of overlap between two adjacent figures is normal (their strokes may
    touch); deciding that means "these are the same box" needs a real overlap.
    """
    ix = min(a.x1, b.x1) - max(a.x0, b.x0)
    iy = min(a.y1, b.y1) - max(a.y0, b.y0)
    if ix <= 0 or iy <= 0:
        return False
    # Shrink both by the tolerance on every side: overlap of at least
    # ``tolerance_pt`` in *both* axes is required, so a shared edge alone does
    # not count.
    return ix >= tolerance_pt and iy >= tolerance_pt


def point_in_rect(x: float, y: float, rect: fitz.Rect) -> bool:
    """Inclusive point-in-rect test used for click hit-testing."""
    return rect.x0 <= x <= rect.x1 and rect.y0 <= y <= rect.y1


def handle_points(
    rect: fitz.Rect, size_pt: float = _HANDLE_SIZE_PX
) -> List[Tuple[float, float]]:
    """Return the four corner handle centres, in PDF space.

    Handles are sized in PDF points rather than canvas pixels so that they
    shrink and grow with the box: a 4 pt handle inside a 20 pt box is a visible
    target, whereas 4 pt inside a 400 pt box would be nearly impossible to hit.
    """
    half = size_pt / 2.0
    return [
        (rect.x0 + half, rect.y0 + half),
        (rect.x1 - half, rect.y0 + half),
        (rect.x1 - half, rect.y1 - half),
        (rect.x0 + half, rect.y1 - half),
    ]


#: Which edges each of the four corner handles moves, in the order
#: :func:`handle_points` returns them.
#:
#: PDF y grows *upward*, so the first handle ``(x0, y0)`` sits at the
#: bottom-left corner and must drag the ``left`` and ``bottom`` edges.  Naming
#: these by their PDF coordinates rather than by screen intuition is what stops
#: a bottom-left drag from teleporting the top edge too.
_HANDLE_EDGES: Tuple[Tuple[str, ...], ...] = (
    ("left", "bottom"),
    ("right", "bottom"),
    ("right", "top"),
    ("left", "top"),
)


def edges_for_handle(index: int) -> Tuple[str, ...]:
    """Edges moved by corner handle ``index`` (0-3), counting from bottom-left."""
    if not 0 <= index < len(_HANDLE_EDGES):
        raise IndexError(f"handle index {index} out of range (0-3)")
    return _HANDLE_EDGES[index]


def nearest_handle(
    x_pdf: float,
    y_pdf: float,
    rect: fitz.Rect,
    grab_pt: float,
) -> Optional[int]:
    """Index of the corner handle within ``grab_pt`` of the point, else ``None``."""
    best: Optional[int] = None
    best_d = grab_pt
    for i, (hx, hy) in enumerate(handle_points(rect)):
        d = ((x_pdf - hx) ** 2 + (y_pdf - hy) ** 2) ** 0.5
        if d <= best_d:
            best_d = d
            best = i
    return best


def box_at(
    x_pdf: float,
    y_pdf: float,
    rects: Sequence[fitz.Rect],
    grab_pt: float,
) -> Optional[int]:
    """Topmost box index whose edge or interior lies under the point.

    Later entries paint on top, so they win ties -- that is the box the user
    last drew and therefore the one they are most likely aiming at.  Edges are
    checked across all boxes *before* interior containment so that a click near
    a border grabs that border, rather than selecting whichever box happens to
    lie underneath and starting an unwanted move.

    Args:
        x_pdf: Click position in PDF points.
        y_pdf: Click position in PDF points.
        rects: Candidate boxes, in draw order.
        grab_pt: Snap distance in PDF points.  The caller converts its on-screen
            tolerance to PDF points, because the threshold has to mean the same
            thing at every zoom level.
    """
    for i in range(len(rects) - 1, -1, -1):
        r = rects[i]
        near_edge = (
            abs(x_pdf - r.x0) <= grab_pt
            or abs(x_pdf - r.x1) <= grab_pt
            or abs(y_pdf - r.y0) <= grab_pt
            or abs(y_pdf - r.y1) <= grab_pt
        )
        if near_edge:
            return i
    for i in range(len(rects) - 1, -1, -1):
        if point_in_rect(x_pdf, y_pdf, rects[i]):
            return i
    return None


# ---------------------------------------------------------------------------
# Per-page box store
# ---------------------------------------------------------------------------


class ManualBoxStore:
    """The rectangles the user drew, keyed by page index.

    A plain dict would do, but the two invariants that make the hybrid mode
    correct are easy to break by accident and cheap to enforce here:

    * **PDF space only.**  Rects are stored as drawn, never as canvas pixels, so
      resizing the window cannot move a crop.
    * **Pages are manual only once marked.**  ``set`` records rectangles;
      :meth:`mark` records the *intent* to override the detector.  A page whose
      boxes were all deleted falls back to the detector unless it is still
      marked, which is what stops a mis-click from blanking a page.
    """

    def __init__(self) -> None:
        self._boxes: Dict[int, List[fitz.Rect]] = {}
        self._manual: set[int] = set()

    # -- reads ------------------------------------------------------------

    def get(self, page_index: int) -> List[fitz.Rect]:
        """Rectangles for a page; empty when the detector should be used."""
        return list(self._boxes.get(page_index, ()))

    def count(self, page_index: int) -> int:
        return len(self._boxes.get(page_index, ()))

    def is_manual(self, page_index: int) -> bool:
        """True when this page's export must use the user's rectangles."""
        return page_index in self._manual

    @property
    def manual_pages(self) -> List[int]:
        """Marked pages, ascending -- the order the UI lists them in."""
        return sorted(self._manual)

    @property
    def is_empty(self) -> bool:
        return not self._manual

    def total_boxes(self) -> int:
        return sum(len(v) for v in self._boxes.values())

    # -- writes -----------------------------------------------------------

    def set(self, page_index: int, rects: Iterable[fitz.Rect]) -> None:
        """Replace a page's rectangles."""
        self._boxes[page_index] = [fitz.Rect(r) for r in rects]

    def mark(self, page_index: int) -> None:
        """Declare that the user owns this page from now on."""
        self._manual.add(page_index)

    def add(self, page_index: int, rect: fitz.Rect) -> int:
        """Append a rectangle and return its index."""
        box = self._boxes.setdefault(page_index, [])
        box.append(fitz.Rect(rect))
        return len(box) - 1

    def update(self, page_index: int, index: int, rect: fitz.Rect) -> None:
        """Overwrite one rectangle in place."""
        self._boxes[page_index][index] = fitz.Rect(rect)

    def remove(self, page_index: int, index: int) -> None:
        """Delete one rectangle, leaving the page marked."""
        del self._boxes[page_index][index]

    def reset_page(self, page_index: int) -> None:
        """Forget a page entirely, handing it back to the detector."""
        self._boxes.pop(page_index, None)
        self._manual.discard(page_index)

    def clear(self) -> None:
        """Forget every manual edit -- the "start over" button."""
        self._boxes.clear()
        self._manual.clear()

    # -- export -----------------------------------------------------------

    def resolve(
        self,
        page_index: int,
        detected: Sequence[fitz.Rect],
        margin_pt: float = 0.0,
        page: Optional[fitz.Rect] = None,
    ) -> List[fitz.Rect]:
        """Rectangles to actually export for one page.

        Args:
            page_index: Zero-based page.
            detected: What the detector found, used when the page is not manual.
            margin_pt: Expansion applied to *detected* boxes only.  User boxes
                are already the final answer, so the global margin is not
                re-applied on top -- otherwise a carefully drawn crop would
                silently grow back into the whitespace the user just removed.
            page: Page bounds used to clamp.

        Returns:
            The rectangles to crop, detector boxes already expanded.
        """
        if self.is_manual(page_index):
            return self.get(page_index)
        if page is None:
            return [fitz.Rect(r) for r in detected]
        return [add_margin_pt(r, margin_pt, page) for r in detected]

    def summary(self) -> str:
        """One-line description of the manual state, for the status bar."""
        if not self._manual:
            return "未手动修改（全部页面使用自动检测）"
        pages = self.manual_pages
        if len(pages) == 1:
            return f"已手动修改第 {pages[0] + 1} 页"
        return f"已手动修改 {len(pages)} 页：{', '.join(str(p + 1) for p in pages)}"


__all__ = [
    "MIN_BOX_PT",
    "EDGE_GRAB_PX",
    "ManualBoxStore",
    "box_at",
    "canvas_rect_to_pdf",
    "clamp_to_page",
    "edges_for_handle",
    "handle_points",
    "is_too_small",
    "move_rect",
    "nearest_handle",
    "normalise",
    "page_rect",
    "pdf_rect_to_canvas",
    "point_in_rect",
    "rects_overlap",
    "resize_rect",
]
