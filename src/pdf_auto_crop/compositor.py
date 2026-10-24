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

import dataclasses
import hashlib
import math
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import pymupdf as fitz

from .config import CropConfig
from .geometry import mm_to_pt

#: Label placement modes.
LABEL_BELOW = "below"      # centred under the figure -- the journal convention
LABEL_OVERLAY = "overlay"  # bottom-left corner, on top of the figure

#: Base-14 fonts that need no embedding.  ``tiro`` is Times-Roman, which
#: matches the body font of most papers.
FONT_ASCII = "tiro"

#: The Simplified-Chinese face PyMuPDF carries itself.
FONT_CJK = "china-s"

#: The label fonts the compositor offers, in the order the GUI lists them.
#: Every one of them is a base-14 face or a face PyMuPDF ships, so a label
#: never drags a font file into the output.
LABEL_FONTS: Dict[str, str] = {
    "Times-Roman": "tiro",
    "Times-Bold": "tibo",
    "Times-Italic": "tiit",
    "Times-Bold Italic": "tibiz",
    "Helvetica": "helv",
    "Helvetica-Bold": "hebo",
    "Helvetica-Oblique": "hebi",
    "Courier": "cour",
    "Courier-Bold": "cobo",
    "Symbol": "symb",
    "ZapfDingbats": "zadb",
    "宋体 (中文)": FONT_CJK,
}

#: The default label font.
LABEL_FONT_DEFAULT = "Times-Roman"

#: The label colour used until the user picks another one.
LABEL_COLOR_DEFAULT: Tuple[float, float, float] = (0.0, 0.0, 0.0)

#: Gap between a figure and its label, in points.
LABEL_GAP_PT = 4.0

#: The graphic elements that can be drawn on top of the figures.
SHAPE_ARROW = "arrow"
SHAPE_RECT = "rect"
SHAPE_ELLIPSE = "ellipse"
SHAPE_LINE = "line"

SHAPE_KINDS: Tuple[str, ...] = (SHAPE_ARROW, SHAPE_RECT, SHAPE_ELLIPSE,
                                 SHAPE_LINE)

#: The colour a new shape is drawn in.
SHAPE_COLOR_DEFAULT: Tuple[float, float, float] = (0.85, 0.1, 0.1)

#: The line width a new shape is drawn with, in points.
SHAPE_WIDTH_DEFAULT = 1.5

#: How opaque the fill of a new shape is.
SHAPE_FILL_OPACITY_DEFAULT = 0.25

#: The stroke line styles.  Each is ``(the PDF dash pattern, the line cap)``:
#: MuPDF draws the dashes itself, and a round cap turns a zero-length dash
#: into a dot.
DASH_SOLID = "solid"
DASH_DASHED = "dashed"
DASH_DOTTED = "dotted"

DASH_STYLES: Dict[str, Tuple[Optional[str], int]] = {
    DASH_SOLID: (None, 0),
    DASH_DASHED: ("[3 2] 0", 0),
    DASH_DOTTED: ("[0.1 2] 0", 1),
}

#: The same styles as Tk wants them, in pixels: ``(on, off)``.
DASH_PATTERNS_TK: Dict[str, Tuple[int, ...]] = {
    DASH_SOLID: (),
    DASH_DASHED: (6, 4),
    DASH_DOTTED: (1, 3),
}


def dash_pattern(dash: str) -> Tuple[Optional[str], int]:
    """The PDF dash pattern and line cap for a line style."""
    return DASH_STYLES.get(dash, DASH_STYLES[DASH_SOLID])


def _font_has_text(fontname: str, text: str) -> bool:
    """Whether a font can render every character of ``text``."""
    try:
        font = fitz.Font(fontname=fontname)
    except Exception:  # noqa: BLE001 -- an unknown face simply has no glyphs
        return False
    return all(font.has_glyph(ord(ch)) for ch in text)


def _safe_font_name(name: str) -> str:
    """A PDF-safe font name.

    PyMuPDF hands the name straight to the PDF as a resource name, and a name
    with a space in it is rejected outright -- which is exactly what
    "Times New Roman.ttf" produces on Windows.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    return cleaned or "font"


def resolve_label_font(text: str, font: str = "", fontfile: str = ""
                       ) -> Tuple[str, str]:
    """Which font to write a label with.

    Returns ``(fontname, fontfile)``.  A font file is embedded in the output;
    a base-14 name is not.  When the chosen face cannot render the text -- an
    ASCII face asked for Chinese, say -- the Chinese face is used instead,
    because a label that shows nothing is worse than a label in another
    typeface.
    """
    if fontfile:
        path = Path(fontfile)
        if path.is_file():
            return _safe_font_name(path.stem), str(path)
    name = LABEL_FONTS.get(font) or LABEL_FONTS[LABEL_FONT_DEFAULT]
    if _font_has_text(name, text):
        return name, ""
    if _font_has_text(FONT_CJK, text):
        return FONT_CJK, ""
    return name, ""


def text_width(text: str, fontname: str, fontsize: float,
               fontfile: str = "") -> float:
    """How wide a label will be, in points."""
    if fontfile:
        try:
            return fitz.Font(fontfile=fontfile).text_length(
                text, fontsize=fontsize)
        except Exception:  # noqa: BLE001
            pass
    try:
        return fitz.get_text_length(text, fontname=fontname, fontsize=fontsize)
    except Exception:  # noqa: BLE001
        return len(text) * fontsize * 0.5


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
    box alone -- the label is placed relative to it.  Keeping the two
    separate means moving the figure never drags its label into the artwork.
    """

    figure: Figure
    rect: fitz.Rect
    label: str = ""
    label_mode: str = LABEL_BELOW
    label_size: float = 9.0
    #: The label's typeface: a key of :data:`LABEL_FONTS`, or empty for the
    #: default.
    label_font: str = ""
    #: An optional font file to embed instead of a built-in face.
    label_fontfile: str = ""
    #: The label's colour, as (r, g, b) in 0..1.
    label_color: Tuple[float, float, float] = LABEL_COLOR_DEFAULT

    def __post_init__(self) -> None:
        self.rect = fitz.Rect(self.rect)
        self.label_color = tuple(float(c) for c in self.label_color)[:3]

    @property
    def aspect(self) -> float:
        """Width / height of the figure as the viewer sees it."""
        w, h = figure_display_size(self.figure)
        return w / h if h else 1.0

    def label_font_info(self) -> Tuple[str, str]:
        """The ``(fontname, fontfile)`` the label is written with."""
        return resolve_label_font(
            self.label, self.label_font, self.label_fontfile)

    def to_dict(self) -> dict:
        return {
            "figure": self.figure.to_dict(),
            "rect": [self.rect.x0, self.rect.y0, self.rect.x1, self.rect.y1],
            "label": self.label,
            "label_mode": self.label_mode,
            "label_size": self.label_size,
            "label_font": self.label_font,
            "label_fontfile": self.label_fontfile,
            "label_color": list(self.label_color),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Placement":
        return cls(
            figure=Figure.from_dict(data["figure"]),
            rect=fitz.Rect(*data["rect"]),
            label=data.get("label", ""),
            label_mode=data.get("label_mode", LABEL_BELOW),
            label_size=float(data.get("label_size", 9.0)),
            label_font=data.get("label_font", ""),
            label_fontfile=data.get("label_fontfile", ""),
            label_color=tuple(
                data.get("label_color", LABEL_COLOR_DEFAULT)),
        )


@dataclass
class Shape:
    """A simple graphic element drawn on top of the figures.

    The two points are in the composition's user space, like everything else
    here.  An arrow runs from ``start`` to ``end`` and puts its head at
    ``end``; a box or an ellipse is the rectangle spanned by the two points.

    The border and the fill are styled separately: a box may have a red
    border and a pale yellow wash inside it, with the border dashed and the
    fill half transparent.
    """

    kind: str
    start: fitz.Point
    end: fitz.Point
    #: The border colour, as (r, g, b) in 0..1.
    color: Tuple[float, float, float] = SHAPE_COLOR_DEFAULT
    #: The border width, in points.
    width: float = SHAPE_WIDTH_DEFAULT
    #: Whether a box or an ellipse is filled.
    fill: bool = False
    #: The fill colour; the border colour when empty.
    fill_color: Optional[Tuple[float, float, float]] = None
    #: How opaque the fill is, 0..1.
    fill_opacity: float = SHAPE_FILL_OPACITY_DEFAULT
    #: How opaque the border is, 0..1.
    stroke_opacity: float = 1.0
    #: The border's line style: "solid", "dashed" or "dotted".
    dash: str = DASH_SOLID

    def __post_init__(self) -> None:
        self.start = fitz.Point(self.start)
        self.end = fitz.Point(self.end)
        self.color = tuple(float(c) for c in self.color)[:3]
        if self.fill_color is not None:
            self.fill_color = tuple(float(c) for c in self.fill_color)[:3]
        self.width = max(0.1, float(self.width))
        self.fill_opacity = min(1.0, max(0.0, float(self.fill_opacity)))
        self.stroke_opacity = min(1.0, max(0.0, float(self.stroke_opacity)))
        if self.dash not in DASH_STYLES:
            self.dash = DASH_SOLID

    @property
    def fill_rgb(self) -> Tuple[float, float, float]:
        """The fill colour, falling back to the border colour."""
        if self.fill_color is not None:
            return self.fill_color
        return self.color

    @property
    def rect(self) -> fitz.Rect:
        """The box the shape occupies."""
        return fitz.Rect(
            min(self.start.x, self.end.x), min(self.start.y, self.end.y),
            max(self.start.x, self.end.x), max(self.start.y, self.end.y),
        )

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "start": [self.start.x, self.start.y],
            "end": [self.end.x, self.end.y],
            "color": list(self.color),
            "width": self.width,
            "fill": self.fill,
            "fill_color": (list(self.fill_color)
                           if self.fill_color is not None else None),
            "fill_opacity": self.fill_opacity,
            "stroke_opacity": self.stroke_opacity,
            "dash": self.dash,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Shape":
        fill_color = data.get("fill_color")
        return cls(
            kind=data["kind"],
            start=fitz.Point(*data["start"]),
            end=fitz.Point(*data["end"]),
            color=tuple(data.get("color", SHAPE_COLOR_DEFAULT)),
            width=float(data.get("width", SHAPE_WIDTH_DEFAULT)),
            fill=bool(data.get("fill", False)),
            fill_color=(tuple(fill_color) if fill_color is not None else None),
            fill_opacity=float(
                data.get("fill_opacity", SHAPE_FILL_OPACITY_DEFAULT)),
            stroke_opacity=float(data.get("stroke_opacity", 1.0)),
            dash=str(data.get("dash", DASH_SOLID)),
        )


@dataclass
class Composition:
    """A page being assembled."""

    width: float
    height: float
    placements: List[Placement] = field(default_factory=list)
    #: Graphic elements drawn on top of the figures.
    shapes: List[Shape] = field(default_factory=list)
    margin_pt: float = 0.0

    def content_bounds(self) -> Optional[fitz.Rect]:
        """Bounding box of everything placed, labels included."""
        boxes = [fitz.Rect(p.rect) for p in self.placements]
        for p in self.placements:
            if p.label and p.label_mode == LABEL_BELOW:
                boxes.append(label_rect(p))
        for s in self.shapes:
            boxes.append(s.rect)
        if not boxes:
            return None
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
            "shapes": [s.to_dict() for s in self.shapes],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Composition":
        return cls(
            width=float(data["width"]),
            height=float(data["height"]),
            margin_pt=float(data.get("margin_pt", 0.0)),
            placements=[Placement.from_dict(p) for p in data.get("placements", [])],
            shapes=[Shape.from_dict(s) for s in data.get("shapes", [])],
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
# Alignment
# ---------------------------------------------------------------------------

#: The ways a selection can be lined up.  "top" and "bottom" are named after
#: the sheet, whose y points up -- the top edge is the *larger* y.
ALIGN_LEFT = "left"
ALIGN_HCENTER = "hcenter"
ALIGN_RIGHT = "right"
ALIGN_TOP = "top"
ALIGN_VCENTER = "vcenter"
ALIGN_BOTTOM = "bottom"

ALIGN_MODES: Tuple[str, ...] = (ALIGN_LEFT, ALIGN_HCENTER, ALIGN_RIGHT,
                                 ALIGN_TOP, ALIGN_VCENTER, ALIGN_BOTTOM)


def align_target(rects: Sequence[fitz.Rect], mode: str,
                 bounds: Optional[fitz.Rect] = None) -> float:
    """The coordinate a selection is lined up on.

    With more than one rectangle they are lined up against each other: the
    reference is their combined bounding box, so "align left" puts every
    left edge on the leftmost one.  With a single rectangle the reference is
    ``bounds`` -- the sheet -- so a lone figure can be pushed against the
    page.
    """
    if not rects:
        return 0.0
    if len(rects) > 1 or bounds is None:
        ref = fitz.Rect(rects[0])
        for r in rects[1:]:
            ref |= r
    else:
        ref = fitz.Rect(bounds)
    if mode == ALIGN_LEFT:
        return ref.x0
    if mode == ALIGN_RIGHT:
        return ref.x1
    if mode == ALIGN_HCENTER:
        return (ref.x0 + ref.x1) / 2
    if mode == ALIGN_TOP:
        return ref.y1
    if mode == ALIGN_BOTTOM:
        return ref.y0
    if mode == ALIGN_VCENTER:
        return (ref.y0 + ref.y1) / 2
    raise ValueError(f"unknown alignment mode: {mode!r}")


def aligned_rect(rect: fitz.Rect, mode: str, target: float) -> fitz.Rect:
    """The rectangle moved so that the given edge or centre sits on target.

    The rectangle keeps its size: aligning moves it, it does not resize it.
    """
    out = fitz.Rect(rect)
    if mode == ALIGN_LEFT:
        shift = target - out.x0
        out.x0 += shift
        out.x1 += shift
    elif mode == ALIGN_RIGHT:
        shift = target - out.x1
        out.x0 += shift
        out.x1 += shift
    elif mode == ALIGN_HCENTER:
        shift = target - (out.x0 + out.x1) / 2
        out.x0 += shift
        out.x1 += shift
    elif mode == ALIGN_TOP:
        shift = target - out.y1
        out.y0 += shift
        out.y1 += shift
    elif mode == ALIGN_BOTTOM:
        shift = target - out.y0
        out.y0 += shift
        out.y1 += shift
    elif mode == ALIGN_VCENTER:
        shift = target - (out.y0 + out.y1) / 2
        out.y0 += shift
        out.y1 += shift
    else:
        raise ValueError(f"unknown alignment mode: {mode!r}")
    return out


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _draw_arrow_head(page: fitz.Page, tip: fitz.Point, tail: fitz.Point,
                     color: Tuple[float, float, float], width: float,
                     dashes: Optional[str] = None, line_cap: int = 0,
                     opacity: float = 1.0) -> None:
    """Two short strokes at the tip, so the arrow points somewhere."""
    dx = tip.x - tail.x
    dy = tip.y - tail.y
    length = math.hypot(dx, dy)
    if length < 1e-6:
        return
    ux, uy = dx / length, dy / length
    head = max(6.0, 3.0 * width)
    # The head starts a little behind the tip, so the point stays sharp.
    bx, by = tip.x - ux * head * 0.45, tip.y - uy * head * 0.45
    for sign in (1, -1):
        angle = math.radians(25.0) * sign
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        px = bx + (ux * cos_a - uy * sin_a) * head
        py = by + (ux * sin_a + uy * cos_a) * head
        page.draw_line(tip, fitz.Point(px, py), color=color, width=width,
                       dashes=dashes, lineCap=line_cap,
                       stroke_opacity=opacity)


def draw_shape(page: fitz.Page, shape: Shape, page_height: float) -> None:
    """Draw one graphic element on a page.

    The shape's points are in user space; the page counts downwards, so they
    are flipped here -- the one place the two conventions meet.
    """
    p0 = fitz.Point(shape.start.x, page_height - shape.start.y)
    p1 = fitz.Point(shape.end.x, page_height - shape.end.y)
    color = shape.color
    width = shape.width
    dashes, line_cap = dash_pattern(shape.dash)
    stroke_opacity = shape.stroke_opacity
    # MuPDF treats a fill opacity of 0 as "no fill at all", so the fill is
    # only asked for when it is actually wanted.
    fill = shape.fill_rgb if shape.fill else None
    fill_opacity = shape.fill_opacity if shape.fill else 0.0

    if shape.kind == SHAPE_LINE:
        page.draw_line(p0, p1, color=color, width=width, dashes=dashes,
                       lineCap=line_cap, stroke_opacity=stroke_opacity)
    elif shape.kind in (SHAPE_RECT, SHAPE_ELLIPSE):
        r = fitz.Rect(min(p0.x, p1.x), min(p0.y, p1.y),
                      max(p0.x, p1.x), max(p0.y, p1.y))
        draw = page.draw_rect if shape.kind == SHAPE_RECT else page.draw_oval
        draw(r, color=color, fill=fill, width=width, dashes=dashes,
             lineCap=line_cap, stroke_opacity=stroke_opacity,
             fill_opacity=fill_opacity)
    else:  # arrow
        page.draw_line(p0, p1, color=color, width=width, dashes=dashes,
                       lineCap=line_cap, stroke_opacity=stroke_opacity)
        _draw_arrow_head(page, p1, p0, color, width, dashes, line_cap,
                         stroke_opacity)


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

        # 1. the figures
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

        # 2. the graphic elements, on top of the figures
        for shape in comp.shapes:
            # The whole style travels with the shape -- rebuilding it from
            # the old fields would quietly drop the fill colour, the
            # opacities and the line style.
            moved = dataclasses.replace(
                shape,
                start=fitz.Point(shape.start.x + dx, shape.start.y + dy),
                end=fitz.Point(shape.end.x + dx, shape.end.y + dy),
            )
            draw_shape(page, moved, h)

        # 3. the labels, on top of everything
        for placement in comp.placements:
            if not placement.label:
                continue
            rect = fitz.Rect(
                placement.rect.x0 + dx, placement.rect.y0 + dy,
                placement.rect.x1 + dx, placement.rect.y1 + dy,
            )
            size = placement.label_size
            fontname, fontfile = placement.label_font_info()
            if placement.label_mode == LABEL_OVERLAY:
                baseline = fitz.Point(
                    rect.x0 + 3,
                    h - (rect.y0 + 3) - size * 0.85,
                )
            else:
                # insert_text has no alignment parameter, so the centred
                # position is computed from the measured text width.
                width = text_width(placement.label, fontname, size, fontfile)
                baseline = fitz.Point(
                    (rect.x0 + rect.x1) / 2 - width / 2,
                    h - (rect.y0 - LABEL_GAP_PT) + size * 0.2,
                )
            page.insert_text(
                baseline, placement.label, fontsize=size,
                fontname=fontname, fontfile=fontfile or None,
                color=placement.label_color,
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
    "LABEL_COLOR_DEFAULT",
    "LABEL_FONT_DEFAULT",
    "LABEL_FONTS",
    "LABEL_OVERLAY",
    "Placement",
    "SHAPE_ARROW",
    "SHAPE_COLOR_DEFAULT",
    "SHAPE_ELLIPSE",
    "SHAPE_KINDS",
    "SHAPE_LINE",
    "SHAPE_RECT",
    "SHAPE_WIDTH_DEFAULT",
    "Shape",
    "auto_label",
    "bake_figure",
    "clear_bake_cache",
    "detect_figures",
    "displayed_size",
    "draw_shape",
    "embed_rotation",
    "export_composition",
    "figure_display_size",
    "fit_rect",
    "from_mupdf_rect",
    "label_rect",
    "load_composition",
    "region_to_page_rect",
    "resolve_label_font",
    "save_composition",
    "source_visible_rect",
    "text_width",
    "to_mupdf_rect",
]
