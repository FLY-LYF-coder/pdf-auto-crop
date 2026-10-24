"""Compose figures from several PDFs into one page, with text labels.

The idea
--------
``pdf-auto-crop`` splits one PDF page into its figures.  This module does the
other half of the job: take figures from *several* PDFs, arrange them on one
page, number them, and write the result out as a single vector PDF.

Mechanism
---------
PyMuPDF's :meth:`Page.show_pdf_page` embeds a source page into a target rect
as a Form XObject.  That is the same mechanism LaTeX's ``\\includegraphics``
uses for PDF input, and it keeps the placed content **vector** -- paths, text
and fills stay as drawing operators rather than being flattened to a bitmap.
:meth:`Page.insert_text` adds real, selectable text on top.

Two measured facts shape the design (see ``scripts/probe_bake_fix2.py``):

* ``show_pdf_page`` places the source page's **visible** rect (its CropBox),
  not its MediaBox, and it **ignores the source page's own ``/Rotate``** --
  the orientation comes solely from its ``rotate`` argument.
* It scales to *fit* the target rect, preserving aspect ratio, rather than
  stretching.  Exact placement therefore needs the target rect computed here,
  not left to MuPDF's fitting.

A third fact, measured in ``scripts/probe_bake_fix2.py``, decides how a
figure is lifted out of its page:

* ``show_pdf_page`` accepts a ``clip`` rectangle, given in the source page's
  own coordinates.  Clipping is strictly better than embedding the whole
  page and trimming it with a CropBox: the baked figure then carries no
  off-figure content at all.
* The page's ``/Rotate`` turns the page **clockwise**, while
  ``show_pdf_page(rotate=)`` turns the content the **other way**.  The two
  must be compensated, or every rotated figure comes out upside down.
* For a page that carries a ``/Rotate``, the clip is read in the *rotated*
  frame, and the frame it uses depends on the page's MediaBox and CropBox.
  Rather than encode a rule that only holds for some combinations of boxes,
  the figure is lifted out of an unrotated copy of the page and turned
  afterwards.

Baking
------
Rather than place a sub-rectangle of a page, each figure is first *baked* into
its own single-page PDF whose visible area is exactly that figure, with any
page rotation already applied.  Placement then becomes trivial and exact:
every figure the compositor handles is a rotation-free page whose visible rect
is the figure, so the same code path serves rotated and unrotated sources
alike.

Coordinate convention
---------------------
Like the rest of this package, rectangles here are in **PDF user space**:
origin bottom-left, y increasing upward.  Two details are worth spelling
out, because both were wrong at some point and neither shows up on a square
page with a centred figure:

* A figure's region is measured **relative to the source page's visible
  rect** -- x from its left edge, y upward from its bottom edge -- which is
  what the detectors produce.  :func:`region_to_page_rect` is the one place
  that turns such a region into the source page's own top-down coordinates.
* PyMuPDF's pages use a top-left origin, so every conversion into MuPDF goes
  through :func:`to_mupdf_rect` -- the single place the two conventions
  meet, which is exactly where the vertically-mirrored crop bug of the past
  was born.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

import pymupdf as fitz

from .config import CropConfig
from .geometry import mm_to_pt

#: Label placement modes.
LABEL_BELOW = "below"      # centred under the figure -- the journal convention
LABEL_OVERLAY = "overlay"  # bottom-left corner, on top of the figure

#: Base-14 fonts that need no embedding.  ``tiro`` is Times-Roman, which
#: matches the body font of most papers.
FONT_ASCII = "tiro"
FONT_CJK = "china-s"  # PyMuPDF's built-in Simplified Chinese face

#: Gap between a figure and its label, in points.
LABEL_GAP_PT = 4.0


# ---------------------------------------------------------------------------
# Coordinate helpers
# ---------------------------------------------------------------------------


def to_mupdf_rect(rect: fitz.Rect, page_height: float) -> fitz.Rect:
    """Convert a user-space (bottom-up) rect to PyMuPDF's top-down space."""
    return fitz.Rect(
        rect.x0, page_height - rect.y1, rect.x1, page_height - rect.y0
    )


def from_mupdf_rect(rect: fitz.Rect, page_height: float) -> fitz.Rect:
    """Inverse of :func:`to_mupdf_rect`."""
    return fitz.Rect(
        rect.x0, page_height - rect.y1, rect.x1, page_height - rect.y0
    )


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass
class Figure:
    """A rectangular region of a PDF page, ready to be placed.

    ``region`` is in the source page's **unrotated** user space, exactly as
    the detectors produce it.  Rotation is handled at bake time.
    """

    path: Path
    page_index: int
    region: fitz.Rect
    name: str = ""

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.region = fitz.Rect(self.region)
        if not self.name:
            self.name = f"{self.path.stem} p{self.page_index + 1}"

    @property
    def key(self) -> str:
        """Stable identity, used for caching the baked file."""
        raw = (
            f"{self.path.resolve()}|{self.page_index}|"
            f"{self.region.x0:.4f},{self.region.y0:.4f},"
            f"{self.region.x1:.4f},{self.region.y1:.4f}"
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "page_index": self.page_index,
            "region": [self.region.x0, self.region.y0, self.region.x1, self.region.y1],
            "name": self.name,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Figure":
        return cls(
            path=Path(data["path"]),
            page_index=int(data["page_index"]),
            region=fitz.Rect(*data["region"]),
            name=data.get("name", ""),
        )


@dataclass
class Placement:
    """A figure positioned on the composition page.

    ``rect`` is in composition **user space** and is the figure's bounding
    box *including* whatever space its label needs?  No -- the rect is the
    figure alone; the label is placed relative to it.  Keeping the two
    separate means moving the figure never drags its label into the artwork.
    """

    figure: Figure
    rect: fitz.Rect
    label: str = ""
    label_mode: str = LABEL_BELOW
    label_size: float = 9.0

    def __post_init__(self) -> None:
        self.rect = fitz.Rect(self.rect)

    @property
    def aspect(self) -> float:
        """Width / height of the figure as the viewer sees it."""
        w, h = figure_display_size(self.figure)
        return w / h if h else 1.0

    def to_dict(self) -> dict:
        return {
            "figure": self.figure.to_dict(),
            "rect": [self.rect.x0, self.rect.y0, self.rect.x1, self.rect.y1],
            "label": self.label,
            "label_mode": self.label_mode,
            "label_size": self.label_size,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Placement":
        return cls(
            figure=Figure.from_dict(data["figure"]),
            rect=fitz.Rect(*data["rect"]),
            label=data.get("label", ""),
            label_mode=data.get("label_mode", LABEL_BELOW),
            label_size=float(data.get("label_size", 9.0)),
        )


@dataclass
class Composition:
    """A page being assembled."""

    width: float
    height: float
    placements: List[Placement] = field(default_factory=list)
    margin_pt: float = 0.0

    def content_bounds(self) -> Optional[fitz.Rect]:
        """Bounding box of everything placed, labels included."""
        if not self.placements:
            return None
        boxes = [fitz.Rect(p.rect) for p in self.placements]
        for p in self.placements:
            if p.label and p.label_mode == LABEL_BELOW:
                boxes.append(label_rect(p))
        out = boxes[0]
        for b in boxes[1:]:
            out |= b
        return out

    def fitted_size(self) -> Tuple[float, float]:
        """Page size that exactly contains the content plus the margin."""
        bounds = self.content_bounds()
        if bounds is None:
            return (self.width, self.height)
        m = self.margin_pt
        return (bounds.width + 2 * m, bounds.height + 2 * m)

    def to_dict(self) -> dict:
        return {
            "width": self.width,
            "height": self.height,
            "margin_pt": self.margin_pt,
            "placements": [p.to_dict() for p in self.placements],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Composition":
        return cls(
            width=float(data["width"]),
            height=float(data["height"]),
            margin_pt=float(data.get("margin_pt", 0.0)),
            placements=[Placement.from_dict(p) for p in data.get("placements", [])],
        )


# ---------------------------------------------------------------------------
# Baking: figure -> standalone single-page PDF
# ---------------------------------------------------------------------------


def source_visible_rect(doc: fitz.Document, page_index: int) -> fitz.Rect:
    """The frame a viewer shows, in the page's *normalised* coordinates.

    PyMuPDF reports a page's visible rectangle with its origin at ``(0, 0)``
    -- the same frame ``get_pixmap`` renders, and the same frame
    ``draw_rect`` and ``show_pdf_page(clip=...)`` work in.  A region in this
    package is measured against that frame's bottom-left corner, so the two
    meet here.

    The size is the **unrotated** one: ``page.rect`` swaps width and height
    for a rotated page, but a region is always measured on the page as it
    was written, before the rotation is applied.

    Using the normalised frame rather than the raw MediaBox is what keeps a
    page whose MediaBox does not start at the origin working: the offset
    lives in the MediaBox, not in the frame the content is addressed in.
    """
    cropbox = fitz.Rect(doc[page_index].cropbox)
    return fitz.Rect(0, 0, cropbox.width, cropbox.height)


def region_to_page_rect(region: fitz.Rect, visible: fitz.Rect) -> fitz.Rect:
    """Convert a figure region into the page's normalised top-down frame.

    A :class:`Figure`'s region is in the package's **user space**: x measured
    from the visible frame's left edge, y measured upward from its bottom
    edge -- the same convention the detectors and the cropper use.  MuPDF
    wants the rectangle top-down instead, and the two are mirror images.
    """
    h = visible.height
    return fitz.Rect(region.x0, h - region.y1, region.x1, h - region.y0)


def embed_rotation(page_rotation: int) -> int:
    """The ``rotate=`` argument that reproduces what a viewer displays.

    A page's ``/Rotate`` turns the page **clockwise**, but
    :meth:`Page.show_pdf_page` turns the content the **other way**.  Feeding
    the page's own rotation straight through therefore shows the figure
    upside down; the two are compensated here.

    Measured in ``scripts/probe_bake_fix2.py``: with the correction the baked
    figure matches the viewer's view for all four rotations, on square and
    non-square pages alike.
    """
    return (360 - (page_rotation % 360)) % 360


def displayed_size(width: float, height: float, rotation: int
                   ) -> Tuple[float, float]:
    """The size of a rectangle as a viewer sees it."""
    if rotation in (90, 270):
        return (height, width)
    return (width, height)


def bake_figure(figure: Figure, cache_dir: Optional[Path] = None) -> Path:
    """Write a single-page PDF whose visible area is exactly ``figure``.

    The result has no rotation and no content outside the figure, so placing
    it is exact: the target rect on the composition page *is* the figure's
    position, with no fitting to reason about.

    The bake runs in two steps, because ``show_pdf_page`` reads its ``clip``
    in a frame that depends on the source page's own boxes -- and, for a page
    that carries a ``/Rotate``, in the *rotated* frame.  The rule relating
    the two was measured (``scripts/probe_clip_rule3.py``) but it does not
    hold for every combination of MediaBox and CropBox.  So instead of
    guessing, the figure is lifted out of an **unrotated copy** of the
    source page -- where the clip is read in the plain, normalised frame --
    and the turn is applied afterwards, about an origin that is known.

    The pivot of the turn does not matter: what is wanted is the region's
    own shape, and a turn is a turn.

    The file is cached by content key, so dragging a figure around does not
    re-bake it.
    """
    if cache_dir is None:
        cache_dir = Path(tempfile.gettempdir()) / "pdf_auto_crop_bake"
    cache_dir.mkdir(parents=True, exist_ok=True)
    out_path = cache_dir / f"fig_{figure.key}.pdf"
    if out_path.exists():
        return out_path

    src = fitz.open(str(figure.path))
    try:
        page = src[figure.page_index]
        rotation = page.rotation % 360
        visible = source_visible_rect(src, figure.page_index)

        # A copy of the page with the rotation taken off.
        scratch = fitz.open()
        try:
            scratch.insert_pdf(
                src, from_page=figure.page_index, to_page=figure.page_index)
            scratch[0].set_rotation(0)

            clip = region_to_page_rect(figure.region, visible)
            rw = max(figure.region.width, 1.0)
            rh = max(figure.region.height, 1.0)

            # Step one: lift the region out of the unrotated copy.
            stage1 = fitz.open()
            try:
                p1 = stage1.new_page(width=rw, height=rh)
                p1.show_pdf_page(
                    fitz.Rect(0, 0, rw, rh), scratch, 0, rotate=0, clip=clip)

                # Step two: turn it the way the viewer sees it.  The page's
                # own /Rotate turns the page clockwise, while show_pdf_page
                # turns the content the other way, so the two cancel.
                w, h = displayed_size(rw, rh, rotation)
                out = fitz.open()
                try:
                    p2 = out.new_page(width=w, height=h)
                    p2.show_pdf_page(
                        fitz.Rect(0, 0, w, h), stage1, 0,
                        rotate=embed_rotation(rotation),
                        clip=fitz.Rect(0, 0, rw, rh),
                    )
                    out.save(str(out_path), garbage=4, deflate=True)
                finally:
                    out.close()
            finally:
                stage1.close()
        finally:
            scratch.close()
    finally:
        src.close()
    return out_path


def figure_display_size(figure: Figure) -> Tuple[float, float]:
    """Size of the figure in points, as a viewer sees it."""
    baked = fitz.open(str(bake_figure(figure)))
    try:
        r = baked[0].rect
        return (r.width, r.height)
    finally:
        baked.close()


# ---------------------------------------------------------------------------
# Detecting candidate figures in a source PDF
# ---------------------------------------------------------------------------


def detect_figures(
    pdf_path: Path, cfg: Optional[CropConfig] = None
) -> List[Figure]:
    """Find candidate figures in every page of ``pdf_path``.

    Reuses the same detector the cropping pipeline uses, so "compose" and
    "crop" agree about what a figure is.
    """
    from .pdf_processor import render_page_to_array
    from .pipeline import get_detector

    cfg = cfg or CropConfig()
    detect = get_detector(cfg)
    out: List[Figure] = []
    doc = fitz.open(str(pdf_path))
    try:
        for pno in range(len(doc)):
            rgb, size_pt = render_page_to_array(doc, pno, cfg.dpi)
            for i, rect in enumerate(detect(rgb, size_pt, cfg), start=1):
                out.append(
                    Figure(
                        path=Path(pdf_path),
                        page_index=pno,
                        region=fitz.Rect(rect),
                        name=f"{Path(pdf_path).stem} p{pno + 1} #{i}",
                    )
                )
    finally:
        doc.close()
    return out


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------


def fit_rect(aspect: float, target: fitz.Rect) -> fitz.Rect:
    """Largest rect with the given aspect that fits inside ``target``.

    Centred.  The compositor uses this so that MuPDF's own aspect-preserving
    fit has nothing left to do, and the placement is exactly where the user
    dragged it.
    """
    if aspect <= 0:
        return fitz.Rect(target)
    tw, th = target.width, target.height
    if tw / th > aspect:
        h = th
        w = th * aspect
    else:
        w = tw
        h = tw / aspect
    cx = (target.x0 + target.x1) / 2
    cy = (target.y0 + target.y1) / 2
    return fitz.Rect(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def label_rect(placement: Placement) -> fitz.Rect:
    """The box a label occupies, in composition user space."""
    if placement.label_mode == LABEL_OVERLAY:
        size = placement.label_size
        return fitz.Rect(
            placement.rect.x0 + 2,
            placement.rect.y0 + 2,
            placement.rect.x0 + 2 + size * 3,
            placement.rect.y0 + 2 + size * 1.2,
        )
    size = placement.label_size
    return fitz.Rect(
        placement.rect.x0,
        placement.rect.y0 - LABEL_GAP_PT - size * 1.2,
        placement.rect.x1,
        placement.rect.y0 - LABEL_GAP_PT,
    )


def auto_label(index: int, style: str = "letter") -> str:
    """Default label for the ``index``-th figure (1-based).

    ``letter`` gives (a), (b), (c); ``number`` gives 1, 2, 3.
    """
    if style == "number":
        return str(index)
    if index < 1:
        return ""
    letters = ""
    n = index
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(ord("a") + rem) + letters
    return f"({letters})"


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _font_for(text: str) -> str:
    """Pick a base-14 font that can render ``text``."""
    if all(ord(c) < 128 for c in text):
        return FONT_ASCII
    return FONT_CJK


def export_composition(
    comp: Composition,
    out_path: Path,
    *,
    fit_to_content: bool = True,
    page_size: Optional[Tuple[float, float]] = None,
) -> Path:
    """Write the composition to a PDF.

    Args:
        comp: The composition to render.
        out_path: Destination file.
        fit_to_content: When true the page is sized to the content plus the
            margin, which is what you want for a figure destined for a
            manuscript.  When false, ``page_size`` is used instead.
        page_size: ``(width_pt, height_pt)`` for a fixed-size page.

    Returns:
        ``out_path``, for chaining.
    """
    if fit_to_content:
        bounds = comp.content_bounds()
        if bounds is None:
            w, h = comp.width, comp.height
            dx = dy = 0.0
        else:
            m = comp.margin_pt
            w, h = bounds.width + 2 * m, bounds.height + 2 * m
            # The page is resized to the content, so the content has to move
            # with it -- otherwise the figures (and their labels) end up
            # outside the page that was just made for them.
            dx = -bounds.x0 + m
            dy = -bounds.y0 + m
    elif page_size:
        w, h = page_size
        dx = dy = 0.0
    else:
        w, h = comp.width, comp.height
        dx = dy = 0.0
    w, h = max(w, 1.0), max(h, 1.0)

    out = fitz.open()
    try:
        page = out.new_page(width=w, height=h)
        for placement in comp.placements:
            rect = fitz.Rect(
                placement.rect.x0 + dx, placement.rect.y0 + dy,
                placement.rect.x1 + dx, placement.rect.y1 + dy,
            )
            baked = fitz.open(str(bake_figure(placement.figure)))
            try:
                # The figure's rect is in user space; MuPDF wants top-down.
                target = to_mupdf_rect(rect, h)
                page.show_pdf_page(target, baked, 0, rotate=0)
            finally:
                baked.close()

            if not placement.label:
                continue
            size = placement.label_size
            font = _font_for(placement.label)
            if placement.label_mode == LABEL_OVERLAY:
                baseline = fitz.Point(
                    rect.x0 + 3,
                    h - (rect.y0 + 3) - size * 0.85,
                )
            else:
                # insert_text has no alignment parameter, so the centred
                # position is computed from the measured text width.
                width = fitz.get_text_length(
                    placement.label, fontname=font, fontsize=size
                )
                baseline = fitz.Point(
                    (rect.x0 + rect.x1) / 2 - width / 2,
                    h - (rect.y0 - LABEL_GAP_PT) + size * 0.2,
                )
            page.insert_text(
                baseline, placement.label, fontsize=size,
                fontname=font, color=(0, 0, 0),
            )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out.save(str(out_path), garbage=4, deflate=True)
    finally:
        out.close()
    return out_path


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def save_composition(comp: Composition, path: Path) -> Path:
    """Write the composition's layout to JSON, so it can be re-opened."""
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(comp.to_dict(), indent=2), encoding="utf-8")
    return path


def load_composition(path: Path) -> Composition:
    """Read a composition back from JSON."""
    import json

    return Composition.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def clear_bake_cache(cache_dir: Optional[Path] = None) -> int:
    """Delete baked figures; returns how many were removed."""
    if cache_dir is None:
        cache_dir = Path(tempfile.gettempdir()) / "pdf_auto_crop_bake"
    if not cache_dir.exists():
        return 0
    n = 0
    for f in cache_dir.glob("fig_*.pdf"):
        f.unlink()
        n += 1
    return n


__all__ = [
    "Composition",
    "Figure",
    "LABEL_BELOW",
    "LABEL_OVERLAY",
    "Placement",
    "auto_label",
    "bake_figure",
    "clear_bake_cache",
    "detect_figures",
    "displayed_size",
    "embed_rotation",
    "export_composition",
    "figure_display_size",
    "fit_rect",
    "from_mupdf_rect",
    "label_rect",
    "load_composition",
    "region_to_page_rect",
    "save_composition",
    "source_visible_rect",
    "to_mupdf_rect",
]
