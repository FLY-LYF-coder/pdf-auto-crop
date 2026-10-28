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

import contextlib
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
from . import fonts

#: Label placement modes.
LABEL_BELOW = "below"      # centred under the figure -- the journal convention
LABEL_OVERLAY = "overlay"  # in the bottom-left corner of the figure
LABEL_TOP_LEFT = "topleft"  # in the top-left corner of the figure
LABEL_BOTTOM_RIGHT = "bottomright"  # in the bottom-right corner
LABEL_FREE = "free"        # wherever the user dropped it

#: The label modes that place the label by itself rather than by a rule.
LABEL_MODES: Tuple[str, ...] = (LABEL_BELOW, LABEL_OVERLAY, LABEL_TOP_LEFT,
                                LABEL_BOTTOM_RIGHT, LABEL_FREE)

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

#: The bold, italic and bold-italic variant of each base-14 face, so that
#: a single "bold" switch works with every font in the list.
FONT_VARIANTS: Dict[str, Dict[str, str]] = {
    "tiro": {"bold": "tibo", "italic": "tiit", "bolditalic": "tibiz"},
    "tibo": {"bold": "tibo", "italic": "tibiz", "bolditalic": "tibiz"},
    "tiit": {"bold": "tibiz", "italic": "tiit", "bolditalic": "tibiz"},
    "tibiz": {"bold": "tibiz", "italic": "tibiz", "bolditalic": "tibiz"},
    "helv": {"bold": "hebo", "italic": "hebi", "bolditalic": "hebo"},
    "hebo": {"bold": "hebo", "italic": "hebi", "bolditalic": "hebo"},
    "hebi": {"bold": "hebo", "italic": "hebi", "bolditalic": "hebo"},
    "cour": {"bold": "cobo", "italic": "coit", "bolditalic": "cobi"},
    "cobo": {"bold": "cobo", "italic": "cobi", "bolditalic": "cobi"},
    "symb": {"bold": "symb", "italic": "symb", "bolditalic": "symb"},
    "zadb": {"bold": "zadb", "italic": "zadb", "bolditalic": "zadb"},
}

#: The default label font.
LABEL_FONT_DEFAULT = "Times-Roman"

#: The label colour used until the user picks another one.
LABEL_COLOR_DEFAULT: Tuple[float, float, float] = (0.0, 0.0, 0.0)

#: Gap between a figure and its label, in points.
LABEL_GAP_PT = 4.0

#: The graphic elements that can be drawn on top of the figures.
SHAPE_ARROW = "arrow"
SHAPE_LINE = "line"
SHAPE_RECT = "rect"
SHAPE_ROUNDRECT = "roundrect"
SHAPE_ELLIPSE = "ellipse"
SHAPE_TRIANGLE = "triangle"
SHAPE_DIAMOND = "diamond"
SHAPE_PARALLELOGRAM = "parallelogram"
SHAPE_RIGHT_ARROW = "rightarrow"
SHAPE_LEFT_ARROW = "leftarrow"
SHAPE_UP_ARROW = "uparrow"
SHAPE_DOWN_ARROW = "downarrow"

SHAPE_KINDS: Tuple[str, ...] = (SHAPE_ARROW, SHAPE_RECT, SHAPE_ROUNDRECT,
                                 SHAPE_ELLIPSE, SHAPE_TRIANGLE, SHAPE_DIAMOND,
                                 SHAPE_PARALLELOGRAM, SHAPE_LINE,
                                 SHAPE_RIGHT_ARROW, SHAPE_LEFT_ARROW,
                                 SHAPE_UP_ARROW, SHAPE_DOWN_ARROW)

#: The shapes whose two points span a box they are inscribed in.
SHAPE_BOX_KINDS: Tuple[str, ...] = (
    SHAPE_RECT, SHAPE_ROUNDRECT, SHAPE_ELLIPSE, SHAPE_TRIANGLE,
    SHAPE_DIAMOND, SHAPE_PARALLELOGRAM, SHAPE_RIGHT_ARROW, SHAPE_LEFT_ARROW,
    SHAPE_UP_ARROW, SHAPE_DOWN_ARROW,
)

#: The shapes that can carry a caption, like a PowerPoint shape can.
SHAPE_TEXT_KINDS: Tuple[str, ...] = (
    SHAPE_RECT, SHAPE_ROUNDRECT, SHAPE_ELLIPSE, SHAPE_TRIANGLE,
    SHAPE_DIAMOND, SHAPE_PARALLELOGRAM, SHAPE_RIGHT_ARROW, SHAPE_LEFT_ARROW,
    SHAPE_UP_ARROW, SHAPE_DOWN_ARROW,
)

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


def apply_style(fontname: str, bold: bool = False, italic: bool = False
                ) -> str:
    """The variant of a base-14 face with bold and italic applied.

    A face that has no bold-italic variant falls back to the bold one: a
    slightly heavier letter is better than a silently dropped switch.
    """
    variants = FONT_VARIANTS.get(fontname)
    if variants is None:
        return fontname
    if bold and italic:
        return variants.get("bolditalic", variants.get("bold", fontname))
    if bold:
        return variants.get("bold", fontname)
    if italic:
        return variants.get("italic", fontname)
    return fontname


#: While this is on, the labels are written with the built-in faces, even
#: where a font file could be used.  The export falls back to it when the
#: file cannot carry the text.
_BUILTIN_FACES = False


@contextlib.contextmanager
def builtin_faces():
    """Inside this block the labels are written with the built-in faces.

    Every reader has them, and they always write the text -- a built-in
    face cannot leave an empty label behind.
    """
    global _BUILTIN_FACES
    old = _BUILTIN_FACES
    _BUILTIN_FACES = True
    try:
        yield
    finally:
        _BUILTIN_FACES = old


def _labels_survived(path: Path, comp: "Composition") -> bool:
    """Whether every label is really in the written file.

    A font that cannot write a character leaves .notdef in the file, and a
    label written that way is invisible: the text is in the file, but there
    is nothing to see.  The written file is read back to find out.
    """
    try:
        doc = fitz.open(str(path))
    except Exception:  # noqa: BLE001 -- a file that cannot be read is a failure
        return False
    try:
        text = " ".join("".join(page.get_text() for page in doc).split())
    finally:
        doc.close()
    for placement in comp.placements:
        label = " ".join((placement.label or "").split())
        if label and label not in text:
            return False
    return True


def resolve_label_font(text: str, font: str = "", fontfile: str = "",
                       bold: bool = False, italic: bool = False
                       ) -> Tuple[str, str]:
    """Which font to write a label with.

    Returns ``(fontname, fontfile)``.  A font file is embedded in the output;
    a base-14 name is not.

    The **file** comes first, and it is the file the canvas draws with.  A
    built-in "Times-Roman" is drawn by the reader with whatever Times it
    happens to have, which is not the Times New Roman the canvas showed -- and
    a paper set in Times New Roman then gets captions in another face.  So the
    file is embedded, and the reader is bound to show the letters the user
    arranged.  The built-in face is the fallback, for a machine that has no
    such file.

    When the chosen face cannot render the text -- an ASCII face asked for
    Chinese, say -- the Chinese face is used instead, because a label that
    shows nothing is worse than a label in another typeface.

    Whether a face can render the text is asked of the *encoder*, not of the
    font's character map: a font can hold a character and still be unable to
    write it, and a label written that way comes out empty.
    """
    if fontfile:
        path = Path(fontfile)
        if path.is_file() and fonts.can_write(str(path), text):
            return _safe_font_name(path.stem), str(path)
    key = font or LABEL_FONT_DEFAULT
    if key not in LABEL_FONTS:
        # A name nobody knows is the default face, file and all.
        key = LABEL_FONT_DEFAULT
    name = apply_style(LABEL_FONTS[key], bold, italic)
    if _BUILTIN_FACES:
        # The built-in faces: every reader has them, and they always write.
        if _font_has_text(name, text):
            return name, ""
        if _font_has_text(FONT_CJK, text):
            return FONT_CJK, ""
        return name, ""
    # The very file the canvas draws with.
    path = fonts.face_file(key, bold, italic)
    if path and fonts.can_write(path, text):
        return _safe_font_name(Path(path).stem), path
    if _font_has_text(name, text):
        return name, ""
    # Chinese in a Latin face: the face the canvas falls back to.
    path = fonts.cjk_file()
    if path and fonts.can_write(path, text):
        return _safe_font_name(Path(path).stem), path
    if _font_has_text(FONT_CJK, text):
        return FONT_CJK, ""
    return name, ""


def font_renders(text: str, fontname: str, fontfile: str = "") -> bool:
    """Whether the font a label is written with can draw the text.

    Args:
        text: The text to draw.
        fontname: The name of the font.
        fontfile: The file that holds it, if there is one.
    """
    if fontfile:
        return fonts.has_glyphs(fontfile, text)
    return _font_has_text(fontname, text)


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


def subset_fonts(doc: fitz.Document) -> None:
    """Shrink the embedded fonts to the glyphs the document actually uses.

    An embedded Times New Roman is a third of a megabyte; the dozen glyphs a
    caption needs are a few kilobytes.  The figures' own fonts live in the
    PDF they came from and are not touched.
    """
    try:
        doc.subset_fonts()
    except Exception:  # noqa: BLE001 -- a font that cannot be subset stays whole
        pass


def repair_spaces(doc: fitz.Document, written: Dict[str, str]) -> None:
    """Put the space back into the ToUnicode map of the fonts we wrote.

    A font file can map the space and the no-break space onto one glyph.  The
    reverse table can then keep only one of them, and it keeps the no-break
    space -- so the text comes out of the PDF with U+00A0 where a space was
    written.  LaTeX refuses that character, and a search for a phrase fails.

    Only the fonts this document wrote are touched: the figures' own fonts
    keep their own spelling.
    """
    for page in doc:
        for font in page.get_fonts(full=True):
            # (xref, extension, type, basefont, resource name, encoding, ...)
            xref = font[0]
            refname = font[4] if len(font) > 4 else ""
            text = written.get(refname, "")
            if " " not in text:
                continue
            obj = doc.xref_object(xref)
            match = re.search(r"/ToUnicode\s+(\d+)\s+0\s+R", obj)
            if not match:
                continue
            stream_xref = int(match.group(1))
            stream = doc.xref_stream(stream_xref)
            fixed = re.sub(rb"(<[0-9a-fA-F]{4}>\s*)<00a0>", rb"\g<1><0020>",
                           stream)
            if fixed != stream:
                doc.update_stream(stream_xref, fixed)


def _note_font(written: Optional[Dict[str, str]], fontname: str,
               text: str) -> None:
    """Remember what was written with which font."""
    if written is None or not text:
        return
    written[fontname] = written.get(fontname, "") + text


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
    #: Whether the label is bold / italic, on top of the face itself.
    label_bold: bool = False
    label_italic: bool = False
    #: Whether the label is LaTeX mathematics rather than plain text.
    label_formula: bool = False
    #: In :data:`LABEL_FREE` mode, where the label sits: an offset from the
    #: figure's bottom-left corner, in points.  Keeping the offset rather
    #: than an absolute position means the label travels with the figure.
    label_offset: Tuple[float, float] = (0.0, 0.0)
    #: How far the figure is turned about the middle of its box, in
    #: degrees, clockwise.
    rotation: float = 0.0
    #: The text inside the figure that the user has changed: the line's
    #: number -> its new text.  An empty text takes the line away.
    text_edits: Dict[int, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.rect = fitz.Rect(self.rect)
        self.label_color = tuple(float(c) for c in self.label_color)[:3]
        self.label_offset = (float(self.label_offset[0]),
                             float(self.label_offset[1]))
        self.rotation = float(self.rotation) % 360.0
        self.text_edits = {int(k): str(v) for k, v in self.text_edits.items()}

    @property
    def aspect(self) -> float:
        """Width / height of the figure as the viewer sees it."""
        w, h = figure_display_size(self.figure)
        return w / h if h else 1.0

    def label_font_info(self) -> Tuple[str, str]:
        """The ``(fontname, fontfile)`` the label is written with."""
        return resolve_label_font(
            self.label, self.label_font, self.label_fontfile,
            self.label_bold, self.label_italic)

    def label_anchor(self) -> fitz.Point:
        """Where the label's baseline starts, in composition user space."""
        size = self.label_size
        fontname, fontfile = self.label_font_info()
        width = text_width(self.label, fontname, size, fontfile)
        if self.label_mode == LABEL_OVERLAY:
            # The bottom-left corner of the figure itself.
            return fitz.Point(self.rect.x0 + 3,
                               self.rect.y0 + 3 + size * 0.85)
        if self.label_mode == LABEL_TOP_LEFT:
            # The top-left corner: the baseline sits an ascent below the top.
            return fitz.Point(self.rect.x0 + 3,
                               self.rect.y1 - 3 - size * 0.85)
        if self.label_mode == LABEL_BOTTOM_RIGHT:
            # The bottom-right corner, right-aligned to the edge.
            return fitz.Point(self.rect.x1 - 3 - width,
                               self.rect.y0 + 3 + size * 0.85)
        if self.label_mode == LABEL_FREE:
            return fitz.Point(self.rect.x0 + self.label_offset[0],
                               self.rect.y0 + self.label_offset[1])
        # Below the figure, centred: the journal convention.
        return fitz.Point((self.rect.x0 + self.rect.x1) / 2 - width / 2,
                          self.rect.y0 - LABEL_GAP_PT - size * 0.2)

    def set_label_anchor(self, point: fitz.Point) -> None:
        """Put the label's baseline at ``point``.

        The label becomes a free one: it keeps that place until the figure
        moves, and then it travels with the figure.
        """
        self.label_mode = LABEL_FREE
        self.label_offset = (point.x - self.rect.x0, point.y - self.rect.y0)

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
            "label_bold": self.label_bold,
            "label_italic": self.label_italic,
            "label_formula": self.label_formula,
            "label_offset": list(self.label_offset),
            "rotation": self.rotation,
            "text_edits": {str(k): v for k, v in self.text_edits.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Placement":
        offset = data.get("label_offset", (0.0, 0.0))
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
            label_bold=bool(data.get("label_bold", False)),
            label_italic=bool(data.get("label_italic", False)),
            label_formula=bool(data.get("label_formula", False)),
            label_offset=(float(offset[0]), float(offset[1])),
            rotation=float(data.get("rotation", 0.0)),
            text_edits={int(k): str(v)
                        for k, v in (data.get("text_edits") or {}).items()},
        )


@dataclass
class FigureText:
    """A line of text inside an imported figure.

    The figure is an embedded PDF page, so its text comes with it; these
    are the lines that can be changed.
    """

    index: int
    text: str
    #: The line's box, in the figure's own user space.
    rect: fitz.Rect
    #: Where the line's baseline starts.
    origin: fitz.Point
    font: str = ""
    size: float = 9.0
    color: Tuple[float, float, float] = (0.0, 0.0, 0.0)


def figure_text_blocks(figure: Figure) -> List[FigureText]:
    """The text inside a figure, in the order the editor lists it.

    The order has to be stable, because the edits are stored by their
    number: the same figure must always give the same list.
    """
    baked = fitz.open(str(bake_figure(figure)))
    try:
        page = baked[0]
        out: List[FigureText] = []
        index = 0
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                text = "".join(s.get("text", "") for s in spans)
                if not text.strip():
                    continue
                first = spans[0]
                out.append(FigureText(
                    index=index,
                    text=text,
                    rect=fitz.Rect(line["bbox"]),
                    origin=fitz.Point(*first["origin"]),
                    font=first.get("font", ""),
                    size=float(first.get("size", 9.0)),
                    color=_srgb_to_rgb(first.get("color", 0)),
                ))
                index += 1
        return out
    finally:
        baked.close()


def _srgb_to_rgb(value: int) -> Tuple[float, float, float]:
    """PyMuPDF's packed sRGB integer -> (r, g, b) in 0..1."""
    return (((value >> 16) & 255) / 255.0,
            ((value >> 8) & 255) / 255.0,
            (value & 255) / 255.0)


def _replacement_font(name: str) -> str:
    """The base-14 face to write a rewritten line in.

    The font of the source is not necessarily available on the machine that
    opens the output, so the closest built-in face is used -- a line written
    in Times stays in Times.
    """
    lowered = (name or "").lower()
    if "symbol" in lowered:
        return "symb"
    if "zapf" in lowered or "dingbat" in lowered:
        return "zadb"
    if "courier" in lowered or "mono" in lowered:
        base = "cour"
    elif "times" in lowered or "serif" in lowered or "roman" in lowered:
        base = "tiro"
    else:
        base = "helv"
    if "bold" in lowered:
        return {"tiro": "tibo", "cour": "cobo", "helv": "hebo"}[base]
    if "italic" in lowered or "oblique" in lowered:
        return {"tiro": "tiit", "cour": "coit", "helv": "hebi"}[base]
    return base


def bake_with_edits(figure: Figure, edits: Dict[int, str]) -> Path:
    """The figure with some of its text replaced.

    The text is taken out of the figure and written again, so the result is
    still vector -- and the rest of the artwork is left alone.
    """
    if not edits:
        return bake_figure(figure)
    key = hashlib.sha256(
        (figure.key + "|" + repr(sorted(edits.items()))).encode("utf-8")
    ).hexdigest()[:24]
    cache = Path(tempfile.gettempdir()) / "pdf_auto_crop_bake"
    cache.mkdir(parents=True, exist_ok=True)
    out_path = cache / f"edited_{key}.pdf"
    if out_path.exists():
        return out_path

    blocks = figure_text_blocks(figure)
    doc = fitz.open(str(bake_figure(figure)))
    try:
        page = doc[0]
        touched = []
        for index, text in edits.items():
            if not 0 <= index < len(blocks):
                continue
            block = blocks[index]
            page.add_redact_annot(block.rect, fill=None)
            touched.append((block, text))
        if touched:
            # Only the text goes: the artwork underneath must survive.
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                text=fitz.PDF_REDACT_TEXT_REMOVE,
            )
        for block, text in touched:
            if not text:
                continue
            page.insert_text(
                block.origin, text, fontsize=block.size,
                fontname=_replacement_font(block.font), color=block.color,
            )
        doc.save(str(out_path), garbage=4, deflate=True)
    finally:
        doc.close()
    return out_path if out_path.exists() else bake_figure(figure)


def image_to_pdf(path: Path, out_dir: Optional[Path] = None) -> Path:
    """A one-page PDF of an image, so it can be placed like any figure.

    A picture is a picture, whatever it was drawn in: turning it into a
    page means the compositor needs no new kind of object, and the image
    ends up in the output as the image it always was.

    The page is the size the image would be printed at, so its own
    resolution decides how large it comes out.
    """
    source = Path(path)
    if out_dir is None:
        out_dir = Path(tempfile.gettempdir()) / "pdf_auto_crop_images"
    out_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(
        f"{source.resolve()}|{source.stat().st_mtime_ns}".encode("utf-8")
    ).hexdigest()[:16]
    out_path = out_dir / f"img_{key}.pdf"
    if out_path.exists():
        return out_path

    pix = fitz.Pixmap(str(source))
    # The resolution the picture was made at.  PyMuPDF reports 96 for
    # everything, so the file's own metadata is asked for first; a picture
    # without any is assumed to be a screen picture.
    dpi = 0.0
    try:
        from PIL import Image

        with Image.open(str(source)) as img:
            info = img.info.get("dpi") or (0, 0)
            dpi = float(info[0] or 0)
    except Exception:  # noqa: BLE001 -- a picture without metadata
        dpi = 0.0
    if dpi < 1:
        dpi = 150.0
    width = max(1.0, pix.width * 72.0 / dpi)
    height = max(1.0, pix.height * 72.0 / dpi)

    doc = fitz.open()
    try:
        page = doc.new_page(width=width, height=height)
        page.insert_image(page.rect, filename=str(source))
        doc.save(str(out_path), garbage=4, deflate=True)
    finally:
        doc.close()
    return out_path


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
    #: How far the shape is turned about its centre, in degrees, clockwise.
    rotation: float = 0.0
    #: A caption inside the shape, as in PowerPoint.
    text: str = ""
    #: The caption's size, in points.
    text_size: float = 9.0
    #: The caption's colour; the border colour when empty.
    text_color: Optional[Tuple[float, float, float]] = None

    def __post_init__(self) -> None:
        self.start = fitz.Point(self.start)
        self.end = fitz.Point(self.end)
        self.color = tuple(float(c) for c in self.color)[:3]
        if self.fill_color is not None:
            self.fill_color = tuple(float(c) for c in self.fill_color)[:3]
        if self.text_color is not None:
            self.text_color = tuple(float(c) for c in self.text_color)[:3]
        self.width = max(0.1, float(self.width))
        self.fill_opacity = min(1.0, max(0.0, float(self.fill_opacity)))
        self.stroke_opacity = min(1.0, max(0.0, float(self.stroke_opacity)))
        self.rotation = float(self.rotation) % 360.0
        if self.dash not in DASH_STYLES:
            self.dash = DASH_SOLID

    @property
    def fill_rgb(self) -> Tuple[float, float, float]:
        """The fill colour, falling back to the border colour."""
        if self.fill_color is not None:
            return self.fill_color
        return self.color

    @property
    def caption_rgb(self) -> Tuple[float, float, float]:
        """The caption's colour, falling back to the border colour."""
        if self.text_color is not None:
            return self.text_color
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
            "rotation": self.rotation,
            "text": self.text,
            "text_size": self.text_size,
            "text_color": (list(self.text_color)
                           if self.text_color is not None else None),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Shape":
        fill_color = data.get("fill_color")
        text_color = data.get("text_color")
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
            rotation=float(data.get("rotation", 0.0)),
            text=str(data.get("text", "")),
            text_size=float(data.get("text_size", 9.0)),
            text_color=(tuple(text_color) if text_color is not None else None),
        )


@dataclass
class TextItem:
    """A piece of text standing on the sheet.

    This is the annotation the user drops on the composition: a caption of
    their own, a note, a formula.  It may be plain text or LaTeX
    mathematics, it may be bold and italic, and it may sit inside a frame.
    """

    text: str
    #: The baseline's start, in the composition's user space.
    position: fitz.Point
    size: float = 10.0
    #: The typeface: a key of :data:`LABEL_FONTS`, or empty for the default.
    font: str = ""
    #: An optional font file to embed instead of a built-in face.
    fontfile: str = ""
    color: Tuple[float, float, float] = LABEL_COLOR_DEFAULT
    bold: bool = False
    italic: bool = False
    #: Whether the text is LaTeX mathematics rather than plain text.
    formula: bool = False
    #: Whether a frame is drawn around the text.
    box: bool = False
    #: The frame's colour and width; the text's colour when the frame's is
    #: left empty.
    box_color: Optional[Tuple[float, float, float]] = None
    box_width: float = 0.8
    #: How far the text is turned about its own start, in degrees, clockwise.
    rotation: float = 0.0

    def __post_init__(self) -> None:
        self.position = fitz.Point(self.position)
        self.color = tuple(float(c) for c in self.color)[:3]
        if self.box_color is not None:
            self.box_color = tuple(float(c) for c in self.box_color)[:3]
        self.size = max(1.0, float(self.size))
        self.box_width = max(0.1, float(self.box_width))
        self.rotation = float(self.rotation) % 360.0

    def font_info(self) -> Tuple[str, str]:
        """The ``(fontname, fontfile)`` the text is written with."""
        return resolve_label_font(
            self.text, self.font, self.fontfile, self.bold, self.italic)

    @property
    def box_rgb(self) -> Tuple[float, float, float]:
        """The frame's colour, falling back to the text's."""
        if self.box_color is not None:
            return self.box_color
        return self.color

    def text_size(self) -> Tuple[float, float]:
        """How wide and how tall the text is, in points."""
        fontname, fontfile = self.font_info()
        width = text_width(self.text, fontname, self.size, fontfile)
        # A text line is a little taller than its point size: ascenders and
        # descenders have to fit.
        return width, self.size * 1.25

    def rect(self) -> fitz.Rect:
        """The box the text occupies, in composition user space."""
        width, height = self.text_size()
        return fitz.Rect(self.position.x, self.position.y,
                          self.position.x + width, self.position.y + height)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "position": [self.position.x, self.position.y],
            "size": self.size,
            "font": self.font,
            "fontfile": self.fontfile,
            "color": list(self.color),
            "bold": self.bold,
            "italic": self.italic,
            "formula": self.formula,
            "box": self.box,
            "box_color": (list(self.box_color)
                          if self.box_color is not None else None),
            "box_width": self.box_width,
            "rotation": self.rotation,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TextItem":
        box_color = data.get("box_color")
        return cls(
            text=data.get("text", ""),
            position=fitz.Point(*data.get("position", (0.0, 0.0))),
            size=float(data.get("size", 10.0)),
            font=data.get("font", ""),
            fontfile=data.get("fontfile", ""),
            color=tuple(data.get("color", LABEL_COLOR_DEFAULT)),
            bold=bool(data.get("bold", False)),
            italic=bool(data.get("italic", False)),
            formula=bool(data.get("formula", False)),
            box=bool(data.get("box", False)),
            box_color=(tuple(box_color) if box_color is not None else None),
            box_width=float(data.get("box_width", 0.8)),
            rotation=float(data.get("rotation", 0.0)),
        )


@dataclass
class Group:
    """A set of objects that travel together.

    The members are held by their place in the composition's lists, which
    is stable: objects are added at the end, and removing one shifts the
    indices after it -- so the groups have to be told about that.
    """

    figs: List[int] = field(default_factory=list)
    shapes: List[int] = field(default_factory=list)
    texts: List[int] = field(default_factory=list)

    def members(self, kind: str) -> List[int]:
        """The members of one kind."""
        if kind == "figure":
            return list(self.figs)
        if kind == "shape":
            return list(self.shapes)
        return list(self.texts)

    def holds(self, kind: str, index: int) -> bool:
        """Whether an object is in this group."""
        return index in self.members(kind)

    def to_dict(self) -> dict:
        return {
            "figs": list(self.figs),
            "shapes": list(self.shapes),
            "texts": list(self.texts),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Group":
        return cls(
            figs=[int(i) for i in data.get("figs", [])],
            shapes=[int(i) for i in data.get("shapes", [])],
            texts=[int(i) for i in data.get("texts", [])],
        )


@dataclass
class Composition:
    """A page being assembled."""

    width: float
    height: float
    placements: List[Placement] = field(default_factory=list)
    #: Graphic elements drawn on top of the figures.
    shapes: List[Shape] = field(default_factory=list)
    #: Free-standing text: annotations, notes, formulas.
    texts: List[TextItem] = field(default_factory=list)
    #: Objects that travel together.
    groups: List[Group] = field(default_factory=list)
    margin_pt: float = 0.0

    def group_of(self, kind: str, index: int) -> Optional[Group]:
        """The group an object belongs to, if any."""
        for group in self.groups:
            if group.holds(kind, index):
                return group
        return None

    def regroup_after_removal(self, kind: str, removed: int) -> None:
        """Fix the groups after an object has been taken out of the list.

        The objects behind the removed one move up by one place, so the
        groups have to move with them -- and the group that held the removed
        object loses it.
        """
        members = "figs" if kind == "figure" else (
            "shapes" if kind == "shape" else "texts")
        for group in self.groups:
            fixed = []
            for i in getattr(group, members):
                if i == removed:
                    continue
                fixed.append(i - 1 if i > removed else i)
            setattr(group, members, fixed)
        self.groups = [g for g in self.groups
                       if g.figs or g.shapes or g.texts]

    def content_bounds(self) -> Optional[fitz.Rect]:
        """Bounding box of everything placed, labels included."""
        boxes = [rotated_bbox(fitz.Rect(p.rect), p.rotation)
                 for p in self.placements]
        for p in self.placements:
            if p.label and p.label_mode == LABEL_BELOW:
                boxes.append(label_rect(p))
            elif p.label and p.label_mode == LABEL_FREE:
                boxes.append(free_label_rect(p))
        for s in self.shapes:
            boxes.append(s.rect)
        for t in self.texts:
            boxes.append(t.rect())
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
            "texts": [t.to_dict() for t in self.texts],
            "groups": [g.to_dict() for g in self.groups],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Composition":
        return cls(
            width=float(data["width"]),
            height=float(data["height"]),
            margin_pt=float(data.get("margin_pt", 0.0)),
            placements=[Placement.from_dict(p) for p in data.get("placements", [])],
            shapes=[Shape.from_dict(s) for s in data.get("shapes", [])],
            texts=[TextItem.from_dict(t) for t in data.get("texts", [])],
            groups=[Group.from_dict(g) for g in data.get("groups", [])],
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

    The detector finds the *ink* of a plot, so the axis ticks, the axis
    titles and the figure's own title -- all a millimetre or two outside the
    frame -- are not part of the box it returns.  They are folded back in
    from the page's text, otherwise a curve plot would arrive without its
    axes.
    """
    from .figure_text import expand_regions_with_text, text_blocks
    from .pdf_processor import render_page_to_array
    from .pipeline import get_detector

    cfg = cfg or CropConfig()
    detect = get_detector(cfg)
    out: List[Figure] = []
    doc = fitz.open(str(pdf_path))
    try:
        for pno in range(len(doc)):
            rgb, size_pt = render_page_to_array(doc, pno, cfg.dpi)
            regions = detect(rgb, size_pt, cfg)
            page_h = size_pt[1]
            if regions:
                # The detector speaks bottom-up user space; the page's text
                # is reported top-down.  Both conversions are the same flip.
                top_down = [
                    fitz.Rect(r.x0, page_h - r.y1, r.x1, page_h - r.y0)
                    for r in regions
                ]
                grown = expand_regions_with_text(
                    top_down, text_blocks(doc[pno]))
                regions = [
                    fitz.Rect(r.x0, page_h - r.y1, r.x1, page_h - r.y0)
                    for r in grown
                ]
            for i, rect in enumerate(regions, start=1):
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
    size = placement.label_size
    if placement.label_mode == LABEL_BELOW:
        # Under the figure: the whole width of the figure counts, so the
        # page is big enough for a label of any size.
        return fitz.Rect(
            placement.rect.x0,
            placement.rect.y0 - LABEL_GAP_PT - size * 1.2,
            placement.rect.x1,
            placement.rect.y0 - LABEL_GAP_PT,
        )
    return free_label_rect(placement)


def free_label_rect(placement: Placement) -> fitz.Rect:
    """The box a freely placed label occupies.

    The label is written at its baseline, so the box starts there and runs
    up by the height of a line.
    """
    anchor = placement.label_anchor()
    fontname, fontfile = placement.label_font_info()
    width = text_width(placement.label, fontname, placement.label_size, fontfile)
    height = placement.label_size * 1.25
    return fitz.Rect(anchor.x, anchor.y, anchor.x + width, anchor.y + height)


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


#: The two directions a group can be spread out in.
DISTRIBUTE_HORIZONTAL = "horizontal"
DISTRIBUTE_VERTICAL = "vertical"


def distribute_rects(rects: Sequence[fitz.Rect], axis: str
                     ) -> List[fitz.Rect]:
    """Spread a group of rectangles out evenly.

    The two outermost objects stay where they are and the ones in between
    are moved so that the gaps between them are equal.  Nothing is resized.

    Args:
        rects: The rectangles to spread, in the order they were selected.
        axis: :data:`DISTRIBUTE_HORIZONTAL` or :data:`DISTRIBUTE_VERTICAL`.

    Returns:
        One rectangle per input rectangle, in the same order.  With fewer
        than three rectangles there is nothing to spread, so the input is
        returned unchanged.
    """
    out = [fitz.Rect(r) for r in rects]
    if axis not in (DISTRIBUTE_HORIZONTAL, DISTRIBUTE_VERTICAL):
        raise ValueError(f"unknown distribution axis: {axis!r}")
    if len(out) < 3:
        return out

    if axis == DISTRIBUTE_HORIZONTAL:
        order = sorted(range(len(out)), key=lambda i: out[i].x0)
        first, last = order[0], order[-1]
        # The free space between the outermost objects, minus what the
        # objects in between already occupy: what is left is shared out
        # equally between the gaps.
        span = out[last].x0 - out[first].x1
        used = sum(out[i].width for i in order[1:-1])
        gap = (span - used) / (len(order) - 1)
        cursor = out[first].x1
        for i in order[1:-1]:
            shift = cursor + gap - out[i].x0
            out[i].x0 += shift
            out[i].x1 += shift
            cursor = out[i].x1
        return out

    if axis == DISTRIBUTE_VERTICAL:
        # The sheet counts upwards, so "first" is the *lowest* one.
        order = sorted(range(len(out)), key=lambda i: out[i].y0)
        first, last = order[0], order[-1]
        span = out[last].y0 - out[first].y1
        used = sum(out[i].height for i in order[1:-1])
        gap = (span - used) / (len(order) - 1)
        cursor = out[first].y1
        for i in order[1:-1]:
            shift = cursor + gap - out[i].y0
            out[i].y0 += shift
            out[i].y1 += shift
            cursor = out[i].y1
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


def rotate_point(point: fitz.Point, centre: fitz.Point, degrees: float
                 ) -> fitz.Point:
    """A point turned about a centre.

    The angle is positive **clockwise** on the sheet, which is the way a
    user thinks of a dial: turning a shape to the right.
    """
    if abs(degrees) < 1e-9:
        return fitz.Point(point)
    rad = math.radians(degrees)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    dx = point.x - centre.x
    dy = point.y - centre.y
    return fitz.Point(
        centre.x + dx * cos_a - dy * sin_a,
        centre.y + dx * sin_a + dy * cos_a,
    )


def rotated_bbox(rect: fitz.Rect, degrees: float) -> fitz.Rect:
    """The box a rectangle takes up once it is turned about its centre."""
    if abs(degrees) < 1e-9:
        return fitz.Rect(rect)
    centre = fitz.Point((rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2)
    points = [
        rotate_point(fitz.Point(x, y), centre, degrees)
        for x, y in ((rect.x0, rect.y0), (rect.x1, rect.y0),
                     (rect.x1, rect.y1), (rect.x0, rect.y1))
    ]
    xs = [p.x for p in points]
    ys = [p.y for p in points]
    return fitz.Rect(min(xs), min(ys), max(xs), max(ys))


#: The outline of a block arrow, in unit coordinates (0..1) of its box.
_ARROW_OUTLINE: Tuple[Tuple[float, float], ...] = (
    (0.0, 0.30), (0.60, 0.30), (0.60, 0.0), (1.0, 0.5),
    (0.60, 1.0), (0.60, 0.70), (0.0, 0.70),
)


def shape_outline(kind: str, rect: fitz.Rect) -> List[fitz.Point]:
    """The outline of a polygon-shaped graphic element.

    The two points of the shape span a box, and the outline is inscribed in
    it -- the same way a PowerPoint shape is drawn in the box it occupies.
    """
    x0, y0 = rect.x0, rect.y0
    w, h = rect.width, rect.height

    def place(u: float, v: float) -> fitz.Point:
        return fitz.Point(x0 + u * w, y0 + v * h)

    if kind == SHAPE_TRIANGLE:
        # The box is in the page's frame, where v = 0 is the top.
        return [place(0.5, 0.0), place(1.0, 1.0), place(0.0, 1.0)]
    if kind == SHAPE_DIAMOND:
        return [place(0.5, 0.0), place(1.0, 0.5), place(0.5, 1.0), place(0.0, 0.5)]
    if kind == SHAPE_PARALLELOGRAM:
        # Leaning to the right, the way a PowerPoint parallelogram does.
        skew = 0.2
        return [place(skew, 0.0), place(1.0, 0.0), place(1.0 - skew, 1.0),
                place(0.0, 1.0)]
    if kind in (SHAPE_RIGHT_ARROW, SHAPE_LEFT_ARROW, SHAPE_UP_ARROW,
                SHAPE_DOWN_ARROW):
        points = list(_ARROW_OUTLINE)
        if kind == SHAPE_LEFT_ARROW:
            points = [(1.0 - u, v) for u, v in points]
        elif kind == SHAPE_UP_ARROW:
            # The right arrow turned a quarter turn anticlockwise.
            points = [(v, 1.0 - u) for u, v in points]
        elif kind == SHAPE_DOWN_ARROW:
            points = [(1.0 - v, u) for u, v in points]
        return [place(u, v) for u, v in points]
    return []


def draw_shape(page: fitz.Page, shape: Shape, page_height: float,
              written: Optional[Dict[str, str]] = None) -> None:
    """Draw one graphic element on a page.

    The shape's points are in user space; the page counts downwards, so they
    are flipped here -- the one place the two conventions meet.

    ``written`` collects the text that was put on the page, by the font it was
    written with, so that the fonts can be repaired afterwards.
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

    # A turned shape is turned about the middle of its own box.
    centre = fitz.Point((p0.x + p1.x) / 2, (p0.y + p1.y) / 2)
    p0 = rotate_point(p0, centre, shape.rotation)
    p1 = rotate_point(p1, centre, shape.rotation)
    box = fitz.Rect(min(p0.x, p1.x), min(p0.y, p1.y),
                    max(p0.x, p1.x), max(p0.y, p1.y))

    if shape.kind == SHAPE_LINE:
        page.draw_line(p0, p1, color=color, width=width, dashes=dashes,
                       lineCap=line_cap, stroke_opacity=stroke_opacity)
    elif shape.kind in (SHAPE_RECT, SHAPE_ROUNDRECT):
        page.draw_rect(box, color=color, fill=fill, width=width, dashes=dashes,
                       lineCap=line_cap, stroke_opacity=stroke_opacity,
                       fill_opacity=fill_opacity,
                       radius=0.15 if shape.kind == SHAPE_ROUNDRECT else None)
    elif shape.kind == SHAPE_ELLIPSE:
        page.draw_oval(box, color=color, fill=fill, width=width, dashes=dashes,
                       lineCap=line_cap, stroke_opacity=stroke_opacity,
                       fill_opacity=fill_opacity)
    elif shape.kind == SHAPE_ARROW:
        page.draw_line(p0, p1, color=color, width=width, dashes=dashes,
                       lineCap=line_cap, stroke_opacity=stroke_opacity)
        _draw_arrow_head(page, p1, p0, color, width, dashes, line_cap,
                         stroke_opacity)
    else:
        # A polygon shape: its outline is inscribed in the box it spans.
        points = shape_outline(shape.kind, box)
        if points:
            page.draw_polyline(points + [points[0]], color=color, fill=fill,
                               width=width, dashes=dashes, lineCap=line_cap,
                               stroke_opacity=stroke_opacity,
                               fill_opacity=fill_opacity)

    if shape.text:
        _draw_shape_caption(page, shape, box, page_height, written)


def _draw_shape_caption(page: fitz.Page, shape: Shape, box: fitz.Rect,
                        page_height: float,
                        written: Optional[Dict[str, str]] = None) -> None:
    """Write a caption in the middle of a shape, as PowerPoint does."""
    fontname, fontfile = resolve_label_font(shape.text, "", "")
    width = text_width(shape.text, fontname, shape.text_size, fontfile)
    baseline = fitz.Point(
        (box.x0 + box.x1) / 2 - width / 2,
        (box.y0 + box.y1) / 2 + shape.text_size * 0.35,
    )
    _note_font(written, fontname, shape.text)
    page.insert_text(baseline, shape.text, fontsize=shape.text_size,
                     fontname=fontname, fontfile=fontfile or None,
                     color=shape.caption_rgb)


def place_rendered(page: fitz.Page, rendered: Path, anchor: fitz.Point,
                   page_height: float, angle: float = 0.0) -> None:
    """Put a rendered page on the page so that its text starts at ``anchor``.

    ``anchor`` is in the composition's user space.  ``angle`` turns the
    content clockwise about its own middle, and the anchor travels with the
    text -- so turning a caption does not make it jump away.
    """
    doc = fitz.open(str(rendered))
    try:
        src = doc[0]
        w, h = src.rect.width, src.rect.height
        # Where the baseline starts inside the rendered page.
        origin = fitz.Point(0.0, h * 0.8)
        for block in src.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    origin = fitz.Point(*span["origin"])
                    break
                break
            break
        centre = fitz.Point(w / 2, h / 2)
        d = fitz.Point(origin.x - centre.x, origin.y - centre.y)
        rad = math.radians(angle)
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        # The page's frame counts downwards, so a positive angle turns the
        # content clockwise on the sheet.
        dx = d.x * cos_a - d.y * sin_a
        dy = d.x * sin_a + d.y * cos_a
        anchor_mupdf = fitz.Point(anchor.x, page_height - anchor.y)
        target_centre = fitz.Point(anchor_mupdf.x - dx, anchor_mupdf.y - dy)
        size = rotated_bbox(fitz.Rect(0, 0, w, h), angle)
        target = fitz.Rect(target_centre.x - size.width / 2,
                           target_centre.y - size.height / 2,
                           target_centre.x + size.width / 2,
                           target_centre.y + size.height / 2)
        page.show_pdf_page(target, doc, 0, rotate=angle)
    finally:
        doc.close()


def draw_formula(page: fitz.Page, formula_pdf: Path, anchor: fitz.Point,
                 page_height: float, angle: float = 0.0) -> None:
    """Draw a rendered formula so that its baseline starts at ``anchor``."""
    place_rendered(page, formula_pdf, anchor, page_height, angle)


def render_text_pdf(item: "TextItem") -> Optional[Path]:
    """Write a piece of text to a one-page PDF, as real text.

    A turned text cannot be inserted straight onto the page -- MuPDF writes
    text along the baseline only -- so it is written here first and then
    placed with an angle, which keeps it real, selectable text.
    """
    if not item.text:
        return None
    fontname, fontfile = item.font_info()
    width = text_width(item.text, fontname, item.size, fontfile)
    height = item.size * 1.6
    pad = max(1.0, item.size * 0.3)
    key = hashlib.sha256(
        f"{item.text}\x00{item.size:.3f}\x00{fontname}\x00{fontfile}\x00"
        f"{item.color}\x00{item.bold}\x00{item.italic}".encode("utf-8")
    ).hexdigest()[:24]
    cache = Path(tempfile.gettempdir()) / "pdf_auto_crop_text"
    cache.mkdir(parents=True, exist_ok=True)
    out_path = cache / f"text_{key}.pdf"
    if out_path.exists():
        return out_path
    doc = fitz.open()
    try:
        page = doc.new_page(width=width + 2 * pad, height=height + 2 * pad)
        page.insert_text(
            fitz.Point(pad, pad + item.size * 0.95), item.text,
            fontsize=item.size, fontname=fontname,
            fontfile=fontfile or None, color=item.color,
        )
        subset_fonts(doc)
        repair_spaces(doc, {fontname: item.text})
        doc.save(str(out_path), garbage=4, deflate=True)
    finally:
        doc.close()
    return out_path if out_path.exists() else None


def draw_text_item(page: fitz.Page, item: TextItem, page_height: float,
                  written: Optional[Dict[str, str]] = None) -> None:
    """Draw one piece of free-standing text on a page.

    A formula is drawn from its rendered PDF, so it keeps its mathematics;
    plain text is inserted as real, selectable text.
    """
    if not item.text:
        return
    anchor = fitz.Point(item.position.x, page_height - item.position.y)

    if item.formula:
        from .tex import render_math
        rendered = render_math(item.text, item.size)
        if rendered is not None:
            draw_formula(page, rendered, anchor, page_height, item.rotation)
            _draw_text_frame(page, item, anchor, page_height)
            return

    if abs(item.rotation) > 1e-6:
        # A turned text is written on its own page and then placed with an
        # angle, so it stays real text rather than a picture.
        rendered = render_text_pdf(item)
        if rendered is not None:
            place_rendered(page, rendered, item.position, page_height,
                           item.rotation)
            _draw_text_frame(page, item, anchor, page_height)
            return

    fontname, fontfile = item.font_info()
    _note_font(written, fontname, item.text)
    page.insert_text(
        anchor, item.text, fontsize=item.size,
        fontname=fontname, fontfile=fontfile or None,
        color=item.color,
    )
    if item.box:
        width, height = item.text_size()
        page.draw_rect(
            fitz.Rect(item.position.x, anchor.y,
                       item.position.x + width, anchor.y + height),
            color=item.box_rgb, width=item.box_width,
        )


def _draw_text_frame(page: fitz.Page, item: TextItem, anchor: fitz.Point,
                     page_height: float) -> None:
    """Draw the frame around a turned text, turned with it."""
    if not item.box:
        return
    width, height = item.text_size()
    corners = [
        fitz.Point(item.position.x, anchor.y),
        fitz.Point(item.position.x + width, anchor.y),
        fitz.Point(item.position.x + width, anchor.y + height),
        fitz.Point(item.position.x, anchor.y + height),
    ]
    centre = fitz.Point(item.position.x + width / 2, anchor.y + height / 2)
    turned = [rotate_point(p, centre, item.rotation) for p in corners]
    page.draw_polyline(turned + [turned[0]], color=item.box_rgb,
                       width=item.box_width)


def export_composition(
    comp: Composition,
    out_path: Path,
    *,
    fit_to_content: bool = True,
    page_size: Optional[Tuple[float, float]] = None,
    offset: Tuple[float, float] = (0.0, 0.0),
) -> Path:
    """Write the composition to a PDF.

    Args:
        comp: The composition to render.
        out_path: Destination file.
        fit_to_content: When true the page is sized to the content plus the
            margin, which is what you want for a figure destined for a
            manuscript.  When false, ``page_size`` is used instead.
        page_size: ``(width_pt, height_pt)`` for a fixed-size page.
        offset: Where the sheet stands on the canvas.  The page is the sheet,
            so the content is moved by this much to sit on it.

    Returns:
        ``out_path``, for chaining.
    """
    if fit_to_content:
        bounds = comp.content_bounds()
        if bounds is None:
            w, h = comp.width, comp.height
            dx, dy = offset
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
        dx, dy = offset
    else:
        w, h = comp.width, comp.height
        dx, dy = offset
    w, h = max(w, 1.0), max(h, 1.0)

    _write_composition(comp, out_path, w, h, dx, dy, subset=True)
    if not _labels_survived(out_path, comp):
        # The fonts were mangled on the way in -- the subsetting can lose a
        # label -- so the sheet is written again with the fonts whole.
        _write_composition(comp, out_path, w, h, dx, dy, subset=False)
    if not _labels_survived(out_path, comp):
        # The face itself cannot write the text: the built-in faces can.
        with builtin_faces():
            _write_composition(comp, out_path, w, h, dx, dy, subset=False)
    return out_path


def _write_composition(comp: "Composition", out_path: Path, w: float, h: float,
                       dx: float, dy: float, subset: bool = True) -> Path:
    """Write the sheet itself."""
    out = fitz.open()
    try:
        page = out.new_page(width=w, height=h)
        #: What was written, by the font it was written with.
        written: Dict[str, str] = {}

        # 1. the figures
        for placement in comp.placements:
            rect = fitz.Rect(
                placement.rect.x0 + dx, placement.rect.y0 + dy,
                placement.rect.x1 + dx, placement.rect.y1 + dy,
            )
            baked = fitz.open(str(bake_with_edits(placement.figure,
                                                  placement.text_edits)))
            try:
                # The figure's rect is in user space; MuPDF wants top-down.
                target = to_mupdf_rect(rect, h)
                if abs(placement.rotation) > 1e-6:
                    # A turned figure keeps its size and takes up the box its
                    # corners sweep, so the target has to grow with it.
                    target = rotated_bbox(target, placement.rotation)
                page.show_pdf_page(target, baked, 0,
                                   rotate=placement.rotation)
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
            draw_shape(page, moved, h, written=written)

        # 3. the free-standing text, on top of the graphic elements
        for item in comp.texts:
            moved = dataclasses.replace(
                item,
                position=fitz.Point(item.position.x + dx,
                                    item.position.y + dy),
            )
            draw_text_item(page, moved, h, written=written)

        # 4. the labels, on top of everything
        for placement in comp.placements:
            if not placement.label:
                continue
            fontname, fontfile = placement.label_font_info()
            # The label travels with the figure, so it moves with the page
            # when the page is resized to the content.
            anchor = placement.label_anchor()
            anchor = fitz.Point(anchor.x + dx, anchor.y + dy)
            if placement.label_formula:
                from .tex import render_math
                rendered = render_math(placement.label, placement.label_size)
                if rendered is not None:
                    draw_formula(page, rendered, anchor, h)
                    continue
            # The anchor is in user space; MuPDF wants top-down.
            baseline = fitz.Point(anchor.x, h - anchor.y)
            _note_font(written, fontname, placement.label)
            page.insert_text(
                baseline, placement.label, fontsize=placement.label_size,
                fontname=fontname, fontfile=fontfile or None,
                color=placement.label_color,
            )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if subset:
            subset_fonts(out)
        repair_spaces(out, written)
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
    "font_renders",
    "from_mupdf_rect",
    "label_rect",
    "load_composition",
    "region_to_page_rect",
    "resolve_label_font",
    "save_composition",
    "source_visible_rect",
    "subset_fonts",
    "text_width",
    "to_mupdf_rect",
]
