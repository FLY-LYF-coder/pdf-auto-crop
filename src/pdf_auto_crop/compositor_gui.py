"""The figure compositor window.

A companion to the cropping window: instead of splitting one page into its
figures, this takes figures from *several* PDFs, lets you arrange them on a
sheet, number them, and writes the result as one vector PDF.

Interaction
------------
* **Palette** -- imported figures, shown as thumbnails.  Double-click to drop
  one onto the sheet.
* **Sheet** -- the composition page.  Drag a figure to move it, drag a corner
  handle to resize it (aspect ratio locked, because a stretched scientific
  figure is a defect), click to select.  ``Delete`` removes, arrow keys nudge.
* **Properties** -- label text, label placement, label size, stacking order.

Everything the user sees is the same geometry the exporter uses, so what is on
the sheet is what lands in the PDF.  The rectangles live in PDF user space
(bottom-left origin) and are converted for the canvas through the same
convention the cropping window uses -- see :mod:`pdf_auto_crop.gui_edit`.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import functools
import math
import sys
import tkinter as tk
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, ttk
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import pymupdf as fitz
from PIL import Image, ImageTk

from . import compositor as comp
from . import dpi
from .config import CropConfig
from .geometry import mm_to_pt

#: Working sheet size: A4 landscape, in points.
SHEET_LANDSCAPE = (842.0, 595.0)
SHEET_PORTRAIT = (595.0, 842.0)

UI_FONT = ("Microsoft YaHei UI", 9)
UI_FONT_BOLD = ("Microsoft YaHei UI", 9, "bold")
UI_FONT_TITLE = ("Microsoft YaHei UI", 12, "bold")

PREVIEW_DPI = 96
HANDLE_PX = 5.0


def HANDLE_POSITIONS(left: float, top: float, right: float, bottom: float
                     ) -> Tuple[Tuple[float, float], ...]:
    """The four corner handles of a canvas rectangle, in a fixed order.

    The order is the canvas's own: top-left, top-right, bottom-right,
    bottom-left.  The resize code numbers the corners the same way, so a
    handle that is drawn in one corner drags that corner.
    """
    return ((left, top), (right, top), (right, bottom), (left, bottom))


def sheet_to_canvas_box(rect: fitz.Rect, origin: Tuple[float, float],
                         zoom: float, sheet_top: float
                         ) -> Tuple[float, float, float, float]:
    """The canvas rectangle of a sheet rectangle: left, top, right, bottom.

    The canvas counts downwards while the sheet counts upwards, so the *top*
    of a placement comes from its top edge in user space -- getting that
    backwards puts the picture outside its own frame, and makes the hit test
    miss every click.

    ``sheet_top`` is the height of the sheet's top edge in user space: the
    sheet can stand anywhere on the canvas, and its edge is what the picture
    is measured against.
    """
    ox, oy = origin
    left = ox + rect.x0 * zoom
    right = ox + rect.x1 * zoom
    top = oy + (sheet_top - rect.y1) * zoom
    bottom = oy + (sheet_top - rect.y0) * zoom
    return (left, top, right, bottom)


#: How close the cursor has to be to grab a graphic element, in pixels.
SHAPE_HIT_PX = 6.0

#: How big the grips on the sheet's edges are, in pixels.
SHEET_HANDLE_PX = 4.0

#: How far the cursor has to travel before a press counts as a drag, in
#: pixels.  A click comes with a pixel or two of travel, and at the sheet's
#: zoom that is enough to nudge a figure out of the layout.
CLICK_SLOP_PX = 3.0

#: How close two edges have to be for one to be pulled onto the other, in
#: points.  A hair's breadth, so the snap is felt rather than seen.
SNAP_PT = 4.0

#: The smallest the sheet may be dragged to, in points.
MIN_SHEET_PT = 36.0

#: How finely the page is shown in the crop dialog.
CROP_PREVIEW_DPI = 96

#: How big the floating preview of a palette entry may get, in logical
#: pixels.
PREVIEW_MAX_W = 360
PREVIEW_MAX_H = 280

#: How far the floating preview keeps from the cursor, in pixels.
PREVIEW_OFFSET_PX = 16

#: The tools that drop a piece of text on the sheet.
TOOL_TEXT = "text"
TOOL_TEXTBOX = "textbox"

#: The tools that draw a graphic element.
SHAPE_TOOLS = (comp.SHAPE_ARROW, comp.SHAPE_RECT, comp.SHAPE_ELLIPSE,
               comp.SHAPE_LINE)

#: The shapes the toolbar offers, in the order they are listed.  The names
#: are the ones PowerPoint uses, so the list reads the way a user expects.
SHAPE_CHOICES: Tuple[Tuple[str, str], ...] = (
    ("方框", comp.SHAPE_RECT),
    ("圆角框", comp.SHAPE_ROUNDRECT),
    ("圆圈", comp.SHAPE_ELLIPSE),
    ("三角形", comp.SHAPE_TRIANGLE),
    ("菱形", comp.SHAPE_DIAMOND),
    ("平行四边形", comp.SHAPE_PARALLELOGRAM),
    ("箭头", comp.SHAPE_ARROW),
    ("右箭头", comp.SHAPE_RIGHT_ARROW),
    ("左箭头", comp.SHAPE_LEFT_ARROW),
    ("上箭头", comp.SHAPE_UP_ARROW),
    ("下箭头", comp.SHAPE_DOWN_ARROW),
    ("直线", comp.SHAPE_LINE),
)

#: Every tool the toolbar offers: what it is called and what it draws.
TOOL_CHOICES: Tuple[Tuple[str, str], ...] = (
    ("选择/移动", "select"),
) + SHAPE_CHOICES + (
    ("文本", TOOL_TEXT),
    ("文本框", TOOL_TEXTBOX),
)

#: The name of a tool -> the thing it draws.
TOOL_BY_NAME: Dict[str, str] = dict(TOOL_CHOICES)


def distance_to_segment(px: float, py: float, x0: float, y0: float,
                        x1: float, y1: float) -> float:
    """The distance from a point to a line segment."""
    dx, dy = x1 - x0, y1 - y0
    length_sq = dx * dx + dy * dy
    if length_sq < 1e-12:
        return math.hypot(px - x0, py - y0)
    t = ((px - x0) * dx + (py - y0) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (x0 + t * dx), py - (y0 + t * dy))


def hex_to_rgb(value: str) -> Tuple[float, float, float]:
    """``#rrggbb`` -> (r, g, b) in 0..1."""
    v = (value or "").lstrip("#")
    if len(v) != 6:
        return (0.0, 0.0, 0.0)
    try:
        return tuple(int(v[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    except ValueError:
        return (0.0, 0.0, 0.0)


def rgb_to_hex(rgb: Tuple[float, float, float]) -> str:
    """(r, g, b) in 0..1 -> ``#rrggbb``."""
    r, g, b = (max(0.0, min(1.0, float(c))) for c in tuple(rgb)[:3])
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def USER_CORNER(rect: fitz.Rect, handle: int) -> Tuple[float, float]:
    """The user-space corner a handle stands for.

    ``handle`` follows :func:`HANDLE_POSITIONS`: 0 is the top-left corner on
    the canvas, which in the sheet's bottom-up coordinates is the *top* left
    corner.
    """
    return (
        (rect.x0, rect.y1),   # top-left
        (rect.x1, rect.y1),   # top-right
        (rect.x1, rect.y0),   # bottom-right
        (rect.x0, rect.y0),   # bottom-left
    )[handle]


#: How the PDF's font names map onto the families Tk knows on Windows.  The
#: preview should look like the output, and Tk does not know a base-14 name.
TK_FONTS: Dict[str, str] = {
    "Times-Roman": "Times New Roman",
    "Times-Bold": "Times New Roman",
    "Times-Italic": "Times New Roman",
    "Times-Bold Italic": "Times New Roman",
    "Helvetica": "Arial",
    "Helvetica-Bold": "Arial",
    "Helvetica-Oblique": "Arial",
    "Courier": "Courier New",
    "Courier-Bold": "Courier New",
    "Symbol": "Symbol",
    "ZapfDingbats": "Segoe UI Symbol",
    "宋体 (中文)": "SimSun",
}


def tk_font(label_font: str, size_px: int, style: str = "normal"
            ) -> Tuple[str, int, str]:
    """The Tk font tuple for a label font.

    ``label_font`` is a key of :data:`pdf_auto_crop.compositor.LABEL_FONTS`,
    or the name of a font file the user picked.

    The size is in **pixels** (a negative number in Tk), not in points: the
    canvas draws the sheet at the user's zoom, and a size in points would
    ignore it -- the labels would come out far too big, and would not sit
    where the exporter puts them.
    """
    family = TK_FONTS.get(label_font, label_font or "Times New Roman")
    if style == "normal":
        # The face itself may already be the bold or the italic one.
        if "Bold" in label_font:
            style = ("bold italic"
                     if ("Italic" in label_font or "Oblique" in label_font)
                     else "bold")
        elif "Italic" in label_font or "Oblique" in label_font:
            style = "italic"
    return (family, -max(1, int(size_px)), style)


#: How far each line of text drops below its baseline, in pixels.
_DESCENTS: Dict[Tuple[str, int], float] = {}


def label_style(bold: bool, italic: bool) -> str:
    """The Tk style a label's bold and italic switches amount to."""
    if bold and italic:
        return "bold italic"
    if bold:
        return "bold"
    if italic:
        return "italic"
    return "normal"


def font_descent_px(font) -> float:
    """How far a line of text drops below its baseline, in pixels.

    The exporter writes the text **on** its baseline, so the preview has to
    put the baseline on the anchor as well.  Tk's "sw" anchor is the bottom of
    the line box, which sits a descender lower.

    ``font`` is a Tk font tuple, or the dictionary :meth:`Font.actual` returns.
    """
    if isinstance(font, dict):
        family = font.get("family", "")
        size = int(font.get("size", 0))
    else:
        family, size = font[0], int(font[1])
    key = (family, size)
    if key in _DESCENTS:
        return _DESCENTS[key]
    descent = 0.0
    try:
        import tkinter.font as tkfont

        descent = float(
            tkfont.Font(family=family, size=size).metrics("descent"))
    except Exception:  # noqa: BLE001 -- no display, no font, no descent
        descent = 0.0
    _DESCENTS[key] = descent
    return descent


def resolve_pick(shape_hit: Optional[int], figure_hit: Optional[int],
                 part: Optional[str]) -> Tuple[Optional[int], Optional[int],
                                              Optional[str]]:
    """Which object a press picks.

    The graphic elements are painted *on top of* the figures, so a hit on an
    element wins over a hit on a figure underneath it: a box drawn across a
    figure must still be grabbable.

    Returns:
        ``(figure, shape, part)`` -- ``part`` is the figure's handle, and is
        empty when an element was picked instead.
    """
    if shape_hit is not None:
        return None, shape_hit, None
    return figure_hit, None, part

def update_selection(sel_figs: List[int], sel_shapes: List[int],
                     sel_texts: List[int], sel_labels: List[int],
                     kind: Optional[str], index: Optional[int],
                     additive: bool
                     ) -> Tuple[List[int], List[int], List[int], List[int],
                                Optional[str]]:
    """The selection after a click, and the drag mode it starts.

    A plain click replaces the selection; holding ``Ctrl`` (or ``Shift``)
    toggles that object in it.  The kinds can be selected together -- a
    figure, a box, a note and a number can be lined up against each other
    -- so a Ctrl+click never throws away what was already picked.

    Args:
        sel_figs: The selected figures.
        sel_shapes: The selected graphic elements.
        sel_texts: The selected free texts.
        sel_labels: The selected labels (by the figure they belong to).
        kind: ``"figure"``, ``"shape"``, ``"text"``, ``"label"`` or ``None``.
        index: The index of the object under the cursor.
        additive: Whether Ctrl or Shift was held.

    Returns:
        ``(figures, shapes, texts, labels, drag_mode)``.
    """
    figs = list(sel_figs)
    shapes = list(sel_shapes)
    texts = list(sel_texts)
    labels = list(sel_labels)

    if kind is None:
        # The empty sheet.  A plain click clears the selection; a modified
        # one leaves it alone, so a stray Ctrl-click does not lose it.
        if not additive:
            return [], [], [], [], None
        return figs, shapes, texts, labels, None

    if additive:
        if kind == "figure":
            if index in figs:
                figs.remove(index)
            else:
                figs.append(index)
        elif kind == "shape":
            if index in shapes:
                shapes.remove(index)
            else:
                shapes.append(index)
        elif kind == "label":
            if index in labels:
                labels.remove(index)
            else:
                labels.append(index)
        else:
            if index in texts:
                texts.remove(index)
            else:
                texts.append(index)
    else:
        figs, shapes, texts, labels = [], [], [], []
        if kind == "figure":
            figs = [index]
        elif kind == "shape":
            shapes = [index]
        elif kind == "label":
            labels = [index]
        else:
            texts = [index]

    mode: Optional[str] = None
    if kind == "figure" and index in figs:
        mode = "body"
    elif kind == "shape" and index in shapes:
        mode = "shape-body"
    elif kind == "text" and index in texts:
        mode = "text-body"
    elif kind == "label" and index in labels:
        mode = "label"
    return figs, shapes, texts, labels, mode


class _TextEditDialog(tk.Toplevel):
    """The text inside a figure, listed and editable.

    The lines are recognised from the figure itself.  Leaving a line empty
    takes it away; changing it writes the new text in its place.
    """

    def __init__(self, master: tk.Misc, blocks: List[comp.FigureText],
                 edits: Dict[int, str],
                 on_ok: Callable[[Dict[int, str]], None]) -> None:
        super().__init__(master)
        self.title("编辑图中的文字")
        self.transient(master)
        self.geometry("460x460")
        self.minsize(360, 320)
        self._blocks = blocks
        self._on_ok = on_ok
        self._entries: Dict[int, tk.Entry] = {}
        self._vars: Dict[int, tk.StringVar] = {}

        body = ttk.Frame(self, padding=10)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        ttk.Label(
            body,
            text="识别到的文字（留空即删除该行）：",
            style="Hint.TLabel",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))

        frame = ttk.Frame(body)
        frame.grid(row=1, column=0, columnspan=2, sticky="nsew")
        body.rowconfigure(1, weight=1)
        body.columnconfigure(0, weight=1)
        canvas = tk.Canvas(frame, highlightthickness=0, background="#f0f0f0")
        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL,
                                command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>",
                   lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda _e: canvas.itemconfigure(window, width=canvas.winfo_width()))
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        for row, block in enumerate(blocks):
            current = edits.get(block.index, block.text)
            var = tk.StringVar(value=current)
            self._vars[block.index] = var
            ttk.Label(inner, text=f"第 {block.index + 1} 行:").grid(
                row=row, column=0, sticky="w", padx=(0, 6), pady=2)
            entry = ttk.Entry(inner, textvariable=var, width=40)
            entry.grid(row=row, column=1, sticky="ew", pady=2)
            self._entries[block.index] = entry
            if block.index in edits:
                entry.configure(background="#fff3cd")
        inner.columnconfigure(1, weight=1)

        buttons = ttk.Frame(self, padding=(10, 0, 10, 10))
        buttons.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Button(buttons, text="取消", command=self.destroy).pack(
            side=tk.RIGHT, padx=4)
        ttk.Button(buttons, text="恢复原样", command=self._restore).pack(
            side=tk.RIGHT, padx=4)
        ttk.Button(buttons, text="确定", command=self._apply).pack(
            side=tk.RIGHT, padx=4)
        self.bind("<Return>", lambda _e: self._apply())
        self.bind("<Escape>", lambda _e: self.destroy())

    def _restore(self) -> None:
        for block in self._blocks:
            self._vars[block.index].set(block.text)

    def _apply(self) -> None:
        edits: Dict[int, str] = {}
        for block in self._blocks:
            new = self._vars[block.index].get()
            if new != block.text:
                edits[block.index] = new
        try:
            self._on_ok(edits)
        finally:
            self.destroy()


class _ArrayDialog(tk.Toplevel):
    """How many rows and columns the array should have."""

    def __init__(self, master: tk.Misc, count: int,
                 on_ok: Callable[[int, int], None]) -> None:
        super().__init__(master)
        self.title("矩阵阵列")
        self.transient(master)
        self.geometry("260x150")
        self._on_ok = on_ok

        body = ttk.Frame(self, padding=12)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.var_cols = tk.IntVar(value=max(1, count))
        self.var_rows = tk.IntVar(value=1)
        ttk.Label(body, text="列").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Spinbox(body, from_=1, to=50, textvariable=self.var_cols,
                     width=6).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(body, text="行").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Spinbox(body, from_=1, to=50, textvariable=self.var_rows,
                     width=6).grid(row=1, column=1, sticky="w", padx=6)

        buttons = ttk.Frame(self, padding=(12, 0, 12, 12))
        buttons.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Button(buttons, text="取消", command=self.destroy).pack(
            side=tk.RIGHT, padx=4)
        ttk.Button(buttons, text="确定", command=self._apply).pack(
            side=tk.RIGHT, padx=4)
        self.bind("<Return>", lambda _e: self._apply())
        self.bind("<Escape>", lambda _e: self.destroy())

    def _apply(self) -> None:
        try:
            columns = max(1, int(self.var_cols.get()))
            rows = max(1, int(self.var_rows.get()))
        except (ValueError, tk.TclError):
            return
        try:
            self._on_ok(columns, rows)
        finally:
            self.destroy()


class _CropDialog(tk.Toplevel):
    """Choose the part of the source that becomes the figure.

    The page is shown as it is, with the region that is currently taken
    marked out.  Drag a new box over the part you want and it takes its
    place; the rest of the page is left alone.
    """

    def __init__(self, master: tk.Misc, figure: comp.Figure,
                 on_ok: Callable[[fitz.Rect], None]) -> None:
        super().__init__(master)
        self.title("取图范围")
        self.transient(master)
        self.geometry("720x620")
        self._figure = figure
        self._on_ok = on_ok
        self._drag_from: Optional[Tuple[float, float]] = None
        self._new_rect: Optional[fitz.Rect] = None

        body = ttk.Frame(self, padding=8)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.canvas = tk.Canvas(body, background="#606060", highlightthickness=0)
        self.canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        # The canvas is only the right size once the dialog is shown.
        self.canvas.bind("<Configure>", lambda _e: self._render())

        buttons = ttk.Frame(self, padding=(8, 0, 8, 8))
        buttons.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Button(buttons, text="取消", command=self.destroy).pack(
            side=tk.RIGHT, padx=4)
        ttk.Button(buttons, text="确定", command=self._apply).pack(
            side=tk.RIGHT, padx=4)
        ttk.Label(
            buttons,
            text="在页面上拖出一个框 = 新的取图范围",
            style="Hint.TLabel",
        ).pack(side=tk.LEFT, padx=6)
        self.bind("<Escape>", lambda _e: self.destroy())

        self._render()

    def _render(self) -> None:
        """Draw the page and the region that is currently taken."""
        self.canvas.delete("all")
        doc = fitz.open(str(self._figure.path))
        try:
            page = doc[self._figure.page_index]
            visible = comp.source_visible_rect(doc, self._figure.page_index)
            pix = page.get_pixmap(dpi=CROP_PREVIEW_DPI)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        finally:
            doc.close()
        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())
        scale = min(cw / img.width, ch / img.height)
        if scale <= 0:
            scale = 1.0
        self._scale = scale
        self._img_size = (img.width, img.height)
        self._visible = visible
        self._photo = ImageTk.PhotoImage(img)
        self._origin = ((cw - img.width * scale) / 2, (ch - img.height * scale) / 2)
        self.canvas.create_image(self._origin[0], self._origin[1],
                                image=self._photo, anchor=tk.NW)
        self._draw_boxes()

    def _draw_boxes(self) -> None:
        """The region that is taken, and the box being dragged.

        Only these are redrawn while the mouse moves: the page itself is
        drawn once, because rendering it again on every pixel of a drag
        makes the box lag behind the cursor.
        """
        self.canvas.delete("cropbox")
        if self._new_rect is None:
            # The region as it stands.
            box = self._page_box_to_canvas(self._figure.region, self._visible)
            self.canvas.create_rectangle(
                box.x0, box.y0, box.x1, box.y1, outline="#ff7f0e", width=2,
                tags=("cropbox",))
        else:
            # The new box takes the old one's place, so there is only ever
            # one frame to look at.
            self.canvas.create_rectangle(
                self._new_rect.x0, self._new_rect.y0,
                self._new_rect.x1, self._new_rect.y1,
                outline="#4dabf7", width=2, dash=(4, 2), tags=("cropbox",))

    def _page_box_to_canvas(self, rect: fitz.Rect, visible: fitz.Rect) -> fitz.Rect:
        """A rectangle on the page, in canvas pixels."""
        ox, oy = self._origin
        s = self._scale
        # The page is shown top-down; a region is measured from the bottom.
        return fitz.Rect(
            ox + rect.x0 * s,
            oy + (visible.height - rect.y1) * s,
            ox + rect.x1 * s,
            oy + (visible.height - rect.y0) * s,
        )

    def _on_press(self, event: tk.Event) -> None:
        self._drag_from = (event.x, event.y)

    def _on_drag(self, event: tk.Event) -> None:
        if self._drag_from is None:
            return
        x0, y0 = self._drag_from
        self._new_rect = fitz.Rect(
            min(x0, event.x), min(y0, event.y),
            max(x0, event.x), max(y0, event.y))
        # Only the frames are redrawn; the page is drawn once.
        self._draw_boxes()

    def _on_release(self, event: tk.Event) -> None:
        self._drag_from = None

    def _apply(self) -> None:
        if self._new_rect is None or self._new_rect.is_empty:
            self.destroy()
            return
        doc = fitz.open(str(self._figure.path))
        try:
            visible = comp.source_visible_rect(doc, self._figure.page_index)
        finally:
            doc.close()
        ox, oy = self._origin
        s = self._scale
        # Back to the page's own coordinates, from the top down.
        region = fitz.Rect(
            (self._new_rect.x0 - ox) / s,
            visible.height - (self._new_rect.y1 - oy) / s,
            (self._new_rect.x1 - ox) / s,
            visible.height - (self._new_rect.y0 - oy) / s,
        )
        region = region & visible
        if region.is_empty:
            self.destroy()
            return
        try:
            self._on_ok(region)
        finally:
            self.destroy()


#: How many steps can be undone.
UNDO_DEPTH = 50


def _signature(composition: comp.Composition) -> Tuple:
    """A short description of the sheet, to tell one state from another."""
    return (
        tuple(tuple(p.rect) + (p.label, p.label_size, p.label_mode,
                               p.label_offset, p.rotation)
              for p in composition.placements),
        tuple((tuple(s.start), tuple(s.end), s.kind) for s in composition.shapes),
        tuple((tuple(t.position), t.text, t.size, t.bold, t.italic)
              for t in composition.texts),
        tuple((tuple(g.figs), tuple(g.shapes), tuple(g.texts))
              for g in composition.groups),
    )


def _action(func):
    """Remember the sheet before an action, so it can be undone.

    The decorator is applied when the class is made, so it has to take the
    panel as its first argument itself.  While the panel is being filled in
    from the model nothing is remembered: that is the undo bringing the
    sheet back, and it must not push a step of its own -- nor clear the
    steps that can be redone.
    """
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        if not self._syncing:
            self._remember()
        return func(self, *args, **kwargs)
    return wrapper


class ComposePanel(tk.Frame):
    """The compositor, as a panel that can live inside another window.

    The cropping window and the compositor are two halves of one job, so
    the compositor is built as a panel: the main window can drop it into
    the same place as its own content and switch between the two.
    """

    def __init__(self, master: tk.Misc, cfg: Optional[CropConfig] = None) -> None:
        super().__init__(master)
        # Tk does not notice a high-DPI display by itself; tell it the real
        # resolution of the monitor this window is on.
        dpi.apply_tk_scaling(self)

        self.cfg = cfg or CropConfig()
        self.sheet_w, self.sheet_h = SHEET_LANDSCAPE
        #: Where the sheet's bottom-left corner is, in the composition's frame.
        #: The sheet is an object on the sheet: an edge that is dragged has to
        #: be able to move, and the content stays where it is.
        self.sheet_x = 0.0
        self.sheet_y = 0.0
        self.composition = comp.Composition(width=self.sheet_w, height=self.sheet_h)

        #: Figures available to place, keyed by an incrementing id.
        self.palette: List[comp.Figure] = []
        self._thumb_cache: Dict[str, Image.Image] = {}
        self._photo_cache: Dict[str, ImageTk.PhotoImage] = {}
        #: The states the sheet has been in, so a mistake can be undone.
        self._undo: List[Tuple[Tuple, Dict[str, Any]]] = []
        self._redo: List[Tuple[Tuple, Dict[str, Any]]] = []
        #: The floating preview of the palette entry under the cursor.
        self._preview_win: Optional[tk.Toplevel] = None
        self._preview_photo: Optional[ImageTk.PhotoImage] = None
        self._preview_index = -1

        self._sel_figs: List[int] = []
        self._sel_shapes: List[int] = []
        self._sel_texts: List[int] = []
        #: The labels that are picked, by the figure they belong to.
        self._sel_labels: List[int] = []
        self._drag_mode: Optional[str] = None
        self._drag_start: Optional[Tuple[float, float]] = None
        self._drag_rect: Optional[fitz.Rect] = None
        self._drag_shape: Optional[comp.Shape] = None
        #: Which half of the property panel to show: the kind picked last.
        self._last_kind: Optional[str] = None
        #: Where every selected object was when the drag started, so a group
        #: moves by one offset instead of by one offset per motion event.
        self._drag_rects: Dict[int, fitz.Rect] = {}
        self._drag_shapes: Dict[int, comp.Shape] = {}
        self._drag_texts: Dict[int, comp.TextItem] = {}
        #: Where on the label the cursor grabbed it, so it does not jump.
        self._drag_label_grab: Tuple[float, float] = (0.0, 0.0)
        #: Which grip of the sheet is being dragged, and the size it had.
        self._drag_sheet_handle: Optional[int] = None
        self._drag_sheet_size: Tuple[float, float] = (0.0, 0.0)
        #: Where the sheet's corner was when the drag started.
        self._drag_sheet_origin: Tuple[float, float] = (0.0, 0.0)
        self._zoom = 1.0
        #: The user's own zoom, on top of the zoom that fits the sheet.
        self._zoom_factor = 1.0
        self._fit_zoom = 1.0
        self._pan_origin: Optional[Tuple[float, float]] = None
        self._pan_from: Tuple[float, float] = (0.0, 0.0)
        self._origin = (0.0, 0.0)

        #: The active tool: "select" moves things, the others draw.
        self.tool = "select"
        #: The shape being drawn, while the mouse is down.
        self._shape_preview: Optional[comp.Shape] = None
        #: A font file the user picked, embedded in the output.
        self._font_file: str = ""
        #: The same, for the free text.
        self._text_font_file: str = ""
        #: Set while the property panel is being filled in from the model.
        self._syncing = False

        self._build_style()
        self._build_toolbar()
        self._build_body()
        self._build_footer()
        # The margin shown has to be the margin the model uses: a variable
        # that was set before its trace was connected never fires it.
        self._redraw()

    # -- undo and redo ------------------------------------------------------

    def _snapshot(self) -> Dict[str, Any]:
        """The sheet as it is now, ready to be put back later."""
        return {
            "placements": copy.deepcopy(self.composition.placements),
            "shapes": copy.deepcopy(self.composition.shapes),
            "texts": copy.deepcopy(self.composition.texts),
            "groups": copy.deepcopy(self.composition.groups),
            "width": self.composition.width,
            "height": self.composition.height,
            "margin": self.composition.margin_pt,
            "sheet_x": self.sheet_x,
            "sheet_y": self.sheet_y,
            "sheet_w": self.sheet_w,
            "sheet_h": self.sheet_h,
        }

    def _restore(self, state: Dict[str, Any]) -> None:
        """Put the sheet back the way it was."""
        self.composition.placements = state["placements"]
        self.composition.shapes = state["shapes"]
        self.composition.texts = state["texts"]
        self.composition.groups = state["groups"]
        self.composition.width = state["width"]
        self.composition.height = state["height"]
        self.composition.margin_pt = state["margin"]
        self.sheet_x = state["sheet_x"]
        self.sheet_y = state["sheet_y"]
        self.sheet_w = state["sheet_w"]
        self.sheet_h = state["sheet_h"]
        # The selection stays: the objects are the same ones, only further
        # back, and a panel that has lost its target cannot be used.
        n_figs = len(self.composition.placements)
        n_shapes = len(self.composition.shapes)
        n_texts = len(self.composition.texts)
        self._sel_figs = [i for i in self._sel_figs if 0 <= i < n_figs]
        self._sel_shapes = [i for i in self._sel_shapes if 0 <= i < n_shapes]
        self._sel_texts = [i for i in self._sel_texts if 0 <= i < n_texts]
        self._sel_labels = [i for i in self._sel_labels if 0 <= i < n_figs]
        self._sync_properties()
        self._redraw()
        self._update_count()

    def _remember(self) -> None:
        """Remember the sheet as it is, so the change can be undone."""
        signature = _signature(self.composition)
        if self._undo and self._undo[-1][0] == signature:
            # Nothing has changed since the last time: no step to undo.
            return
        self._undo.append((signature, self._snapshot()))
        if len(self._undo) > UNDO_DEPTH:
            self._undo.pop(0)
        self._redo.clear()

    def _undo_step(self) -> None:
        """Take back the last change."""
        if not self._undo:
            self._status("没有可撤销的操作。")
            return
        _, state = self._undo.pop()
        self._redo.append((_signature(self.composition), self._snapshot()))
        self._restore(state)
        self._status("已撤销一步。")

    def _redo_step(self) -> None:
        """Do the change again that was taken back."""
        if not self._redo:
            self._status("没有可重做的操作。")
            return
        _, state = self._redo.pop()
        self._undo.append((_signature(self.composition), self._snapshot()))
        self._restore(state)
        self._status("已重做一步。")

    # -- selection ---------------------------------------------------------

    @property
    def _selected(self) -> Optional[int]:
        """The figure the property panel edits: the last one picked."""
        return self._sel_figs[-1] if self._sel_figs else None

    @_selected.setter
    def _selected(self, value: Optional[int]) -> None:
        # The three kinds can be selected together, so setting one of them
        # does not throw the others away.
        self._sel_figs = [] if value is None else [value]

    @property
    def _selected_shape(self) -> Optional[int]:
        """The graphic element the property panel edits."""
        return self._sel_shapes[-1] if self._sel_shapes else None

    @_selected_shape.setter
    def _selected_shape(self, value: Optional[int]) -> None:
        self._sel_shapes = [] if value is None else [value]

    @property
    def _selected_text(self) -> Optional[int]:
        """The free text the property panel edits."""
        return self._sel_texts[-1] if self._sel_texts else None

    @_selected_text.setter
    def _selected_text(self, value: Optional[int]) -> None:
        self._sel_texts = [] if value is None else [value]

    @property
    def _selected_label(self) -> Optional[int]:
        """The label that is picked, by the figure it belongs to."""
        return self._sel_labels[-1] if self._sel_labels else None

    # -- styling ----------------------------------------------------------

    def _build_style(self) -> None:
        style = ttk.Style(self)
        for theme in ("vista", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        self.option_add("*Font", UI_FONT)
        style.configure("Title.TLabel", font=UI_FONT_TITLE)
        style.configure("Hint.TLabel", foreground="#555555")
        style.configure("Section.TLabelframe.Label", font=UI_FONT_BOLD)

    # -- layout -----------------------------------------------------------

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self, padding=(10, 8))
        bar.pack(side=tk.TOP, fill=tk.X)

        ttk.Button(bar, text="导入 PDF…", command=self._import_pdf).grid(
            row=0, column=0, sticky="w")
        ttk.Button(bar, text="导入图片…", command=self._import_images).grid(
            row=0, column=1, padx=6, sticky="w")
        ttk.Button(bar, text="导入整页…", command=self._import_whole_pages).grid(
            row=0, column=2, padx=6, sticky="w")
        ttk.Button(bar, text="导入已裁剪的图…", command=self._import_cropped).grid(
            row=0, column=3, padx=6, sticky="w")
        ttk.Separator(bar, orient=tk.VERTICAL).grid(
            row=0, column=4, sticky="ns", padx=8)

        ttk.Label(bar, text="纸张").grid(row=0, column=5, padx=(0, 4), sticky="e")
        self.var_sheet = tk.StringVar(value="A4 横向")
        ttk.Combobox(
            bar, textvariable=self.var_sheet, state="readonly", width=10,
            values=["A4 横向", "A4 纵向", "适应内容", "自定义"],
        ).grid(row=0, column=6, sticky="w")
        self.var_sheet.trace_add("write", lambda *_a: self._on_sheet_changed())

        # The custom page size, in millimetres.
        ttk.Label(bar, text="宽").grid(row=0, column=7, padx=(12, 2), sticky="e")
        self.var_sheet_w = tk.StringVar(value="297")
        self.ent_sheet_w = ttk.Entry(bar, textvariable=self.var_sheet_w, width=6)
        self.ent_sheet_w.grid(row=0, column=8, sticky="w")
        self.var_sheet_w.trace_add("write", lambda *_a: self._on_sheet_changed())
        ttk.Label(bar, text="高 mm").grid(row=0, column=9, padx=(6, 2), sticky="e")
        self.var_sheet_h = tk.StringVar(value="210")
        self.ent_sheet_h = ttk.Entry(bar, textvariable=self.var_sheet_h, width=6)
        self.ent_sheet_h.grid(row=0, column=10, sticky="w")
        self.var_sheet_h.trace_add("write", lambda *_a: self._on_sheet_changed())

        ttk.Label(bar, text="边距 (mm)").grid(row=0, column=11, padx=(12, 4), sticky="e")
        self.var_margin = tk.StringVar(value="3")
        ttk.Entry(bar, textvariable=self.var_margin, width=6).grid(
            row=0, column=12, sticky="w")
        self.var_margin.trace_add("write", lambda *_a: self._on_margin_changed())

        ttk.Separator(bar, orient=tk.VERTICAL).grid(
            row=0, column=13, sticky="ns", padx=8)
        ttk.Button(bar, text="自动编号", command=self._auto_label).grid(
            row=0, column=14, padx=(0, 6), sticky="w")
        ttk.Button(bar, text="横向排列", command=lambda: self._arrange("row")).grid(
            row=0, column=15, padx=6, sticky="w")
        ttk.Button(bar, text="网格排列", command=lambda: self._arrange("grid")).grid(
            row=0, column=16, padx=6, sticky="w")

        ttk.Button(bar, text="导出 PDF…", command=self._export).grid(
            row=0, column=17, padx=12, sticky="e")

        # The drawing tools, on a second row of the toolbar.
        tools = ttk.Frame(bar)
        tools.grid(row=1, column=0, columnspan=18, sticky="w", pady=(6, 0))
        ttk.Label(tools, text="工具").pack(side=tk.LEFT, padx=(0, 6))
        self.var_tool = tk.StringVar(value=TOOL_CHOICES[0][0])
        self.cmb_tool = ttk.Combobox(
            tools, textvariable=self.var_tool, state="readonly", width=12,
            values=[name for name, _ in TOOL_CHOICES],
        )
        self.cmb_tool.pack(side=tk.LEFT)
        self.cmb_tool.bind("<<ComboboxSelected>>", self._on_tool_changed)
        ttk.Separator(tools, orient=tk.VERTICAL).pack(side=tk.LEFT,
                                                       fill=tk.Y, padx=10)
        ttk.Label(
            tools, text="滚轮缩放画布 · 中键平移；Ctrl+点击 可多选并对齐",
            style="Hint.TLabel",
        ).pack(side=tk.LEFT)

    def _build_body(self) -> None:
        body = ttk.Frame(self, padding=(10, 0, 10, 0))
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        # -- palette
        left = ttk.Labelframe(body, text="素材（双击放到画布）",
                              style="Section.TLabelframe", padding=6)
        left.pack(side=tk.LEFT, fill=tk.Y)
        self.lst_palette = tk.Listbox(left, width=26, height=28,
                                      exportselection=False)
        self.lst_palette.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.lst_palette.bind("<Double-Button-1>", lambda _e: self._add_selected())
        # The cursor over an entry shows what the figure looks like.
        self.lst_palette.bind("<Motion>", self._on_palette_motion)
        self.lst_palette.bind("<Leave>", lambda _e: self._hide_palette_preview())
        # Delete takes the selected entry out of the palette.
        self.lst_palette.bind("<Delete>", lambda _e: self._remove_from_palette())
        self.lst_palette.bind("<BackSpace>", lambda _e: self._remove_from_palette())
        ttk.Label(
            left,
            text="「导入 PDF」自动检测子图；\n「导入整页」一点不裁；\n"
                 "「导入已裁剪的图」取整页。",
            style="Hint.TLabel", wraplength=170, justify=tk.LEFT,
        ).pack(side=tk.TOP, fill=tk.X, pady=(6, 0))

        # How much white space the detection keeps around a figure.  The
        # detector measures the ink, so an element that is very pale -- or
        # that sits just outside the ink -- can end up cut off; the margin
        # gives it room.
        margin_row = ttk.Frame(left)
        margin_row.pack(side=tk.TOP, fill=tk.X, pady=(8, 0))
        ttk.Label(margin_row, text="识别留白").pack(side=tk.LEFT)
        self.var_detect_margin = tk.StringVar(value="2")
        ttk.Spinbox(
            margin_row, from_=0, to=20, increment=1,
            textvariable=self.var_detect_margin, width=5,
        ).pack(side=tk.LEFT, padx=4)
        ttk.Label(margin_row, text="mm", style="Hint.TLabel").pack(side=tk.LEFT)

        # What to do with a palette entry that is not wanted.
        palette_buttons = ttk.Frame(left)
        palette_buttons.pack(side=tk.TOP, fill=tk.X, pady=(8, 0))
        ttk.Button(palette_buttons, text="删除选中", width=9,
                   command=self._remove_from_palette).pack(side=tk.LEFT)
        ttk.Button(palette_buttons, text="清空素材", width=9,
                   command=self._clear_palette).pack(side=tk.LEFT, padx=4)

        # -- sheet
        mid = ttk.Frame(body)
        mid.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=10)
        self.canvas = tk.Canvas(mid, background="#606060", highlightthickness=0)
        self.canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.canvas.bind("<Configure>", lambda _e: self._redraw())
        self.canvas.bind("<Button-1>", self._on_press)
        # A Ctrl-click is a *different* event in Tk -- the plain <Button-1>
        # binding does not see it -- so the modified clicks are bound too.
        self.canvas.bind("<Control-Button-1>", self._on_press)
        self.canvas.bind("<Shift-Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        # The same for the release: a modified drag has to end too.
        self.canvas.bind("<Control-ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Shift-ButtonRelease-1>", self._on_release)
        # The wheel zooms the view, and the middle button pans it.
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Button-2>", self._on_pan_start)
        self.canvas.bind("<B2-Motion>", self._on_pan_move)
        self.canvas.bind("<ButtonRelease-2>", self._on_pan_end)
        self.canvas.bind("<Delete>", self._on_key)
        self.canvas.bind("<BackSpace>", self._on_key)
        self.canvas.bind("<Escape>", self._on_key)
        self.canvas.bind("<Control-z>", self._on_undo_key)
        self.canvas.bind("<Control-y>", self._on_redo_key)
        self.canvas.bind("<Control-Shift-Z>", self._on_redo_key)
        self.bind("<Control-z>", self._on_undo_key)
        self.bind("<Control-y>", self._on_redo_key)
        self.bind("<Control-Shift-Z>", self._on_redo_key)
        for key in ("<Left>", "<Right>", "<Up>", "<Down>"):
            self.canvas.bind(key, self._on_key)
            self.canvas.bind(f"<Shift-{key[1:-1]}>", self._on_key)

        # -- properties
        right = ttk.Labelframe(body, text="属性", style="Section.TLabelframe",
                               padding=8)
        right.pack(side=tk.RIGHT, fill=tk.Y)
        right.columnconfigure(0, weight=1)

        # The panel has grown a lot -- a label, a free text, a graphic
        # element and the alignment tools -- so it is split over tabs, one
        # per kind of object.  Selecting an object switches to its tab.
        self.nb = ttk.Notebook(right)
        self.nb.grid(row=0, column=0, sticky="nsew")
        self.tab_label = ttk.Frame(self.nb, padding=6)
        self.tab_text = ttk.Frame(self.nb, padding=6)
        self.tab_shape = ttk.Frame(self.nb, padding=6)
        self.tab_align = ttk.Frame(self.nb, padding=6)
        self.nb.add(self.tab_label, text="标签")
        self.nb.add(self.tab_text, text="文本")
        self.nb.add(self.tab_shape, text="图形")
        self.nb.add(self.tab_align, text="对齐")

        self._build_label_tab(self.tab_label)
        self._build_text_tab(self.tab_text)
        self._build_shape_tab(self.tab_shape)
        self._build_align_tab(self.tab_align)

        ttk.Button(right, text="置于顶层", command=lambda: self._restack(+1)).grid(
            row=1, column=0, sticky="ew", pady=(10, 2))
        ttk.Button(right, text="置于底层", command=lambda: self._restack(-1)).grid(
            row=2, column=0, sticky="ew", pady=2)
        edits = ttk.Frame(right)
        edits.grid(row=3, column=0, sticky="ew", pady=(8, 2))
        ttk.Button(edits, text="复制", width=6,
                   command=self._duplicate_selected).pack(side=tk.LEFT, padx=1)
        ttk.Button(edits, text="水平翻转", width=8,
                   command=lambda: self._flip_selected("x")).pack(side=tk.LEFT,
                                                                   padx=1)
        ttk.Button(edits, text="垂直翻转", width=8,
                   command=lambda: self._flip_selected("y")).pack(side=tk.LEFT,
                                                                   padx=1)
        groups = ttk.Frame(right)
        groups.grid(row=4, column=0, sticky="ew", pady=2)
        ttk.Button(groups, text="组合", width=6,
                   command=self._group_selected).pack(side=tk.LEFT, padx=1)
        ttk.Button(groups, text="取消组合", width=8,
                   command=self._ungroup_selected).pack(side=tk.LEFT, padx=1)
        ttk.Button(right, text="删除选中", command=self._delete_selected).grid(
            row=5, column=0, sticky="ew", pady=2)
        ttk.Button(right, text="清空画布", command=self._clear_sheet).grid(
            row=6, column=0, sticky="ew", pady=2)

        ttk.Label(
            right,
            text="拖动图体移动，橙点缩放\n编号可直接拖到任意位置\n"
                 "Ctrl+点击 多选（可一起对齐/分布）\n"
                 "Ctrl+G 组合 · Ctrl+U 取消组合\n"
                 "滚轮缩放画布，中键平移\n"
                 "方向键微调 · Delete 删除",
            style="Hint.TLabel", wraplength=190, justify=tk.LEFT,
        ).grid(row=7, column=0, sticky="w", pady=(10, 0))

    # -- the label tab ------------------------------------------------------

    def _build_label_tab(self, tab: ttk.Frame) -> None:
        tab.columnconfigure(1, weight=1)

        ttk.Label(tab, text="标签").grid(row=0, column=0, sticky="w", pady=3)
        self.var_label = tk.StringVar(value="")
        self.ent_label = ttk.Entry(tab, textvariable=self.var_label)
        self.ent_label.grid(row=0, column=1, sticky="ew", pady=3)
        self.var_label.trace_add("write", lambda *_a: self._on_label_changed())

        ttk.Label(tab, text="位置").grid(row=1, column=0, sticky="w", pady=3)
        self.var_label_mode = tk.StringVar(value=comp.LABEL_BELOW)
        box = ttk.Frame(tab)
        box.grid(row=1, column=1, columnspan=2, sticky="w", pady=3)
        for text, value in (("图下", comp.LABEL_BELOW),
                            ("左上", comp.LABEL_TOP_LEFT),
                            ("左下", comp.LABEL_OVERLAY),
                            ("右下", comp.LABEL_BOTTOM_RIGHT),
                            ("自由", comp.LABEL_FREE)):
            ttk.Radiobutton(box, text=text, value=value,
                            variable=self.var_label_mode,
                            command=self._on_label_mode_changed).pack(
                side=tk.LEFT, padx=1)

        ttk.Label(tab, text="字号").grid(row=2, column=0, sticky="w", pady=3)
        self.var_label_size = tk.StringVar(value="9")
        self.spin_label_size = ttk.Spinbox(
            tab, from_=4, to=48, increment=1,
            textvariable=self.var_label_size, width=6)
        self.spin_label_size.grid(row=2, column=1, sticky="w", pady=3)
        self.var_label_size.trace_add("write", lambda *_a: self._on_label_changed())

        ttk.Label(tab, text="字体").grid(row=3, column=0, sticky="w", pady=3)
        self.var_label_font = tk.StringVar(value=comp.LABEL_FONT_DEFAULT)
        self.cmb_label_font = ttk.Combobox(
            tab, textvariable=self.var_label_font, state="readonly", width=14,
            values=list(comp.LABEL_FONTS),
        )
        self.cmb_label_font.grid(row=3, column=1, sticky="ew", pady=3)
        self.cmb_label_font.bind("<<ComboboxSelected>>", self._on_label_changed)
        ttk.Button(tab, text="文件…", width=5,
                   command=self._choose_font_file).grid(
            row=3, column=2, sticky="w", padx=(4, 0))

        ttk.Label(tab, text="颜色").grid(row=4, column=0, sticky="w", pady=3)
        self.var_label_color = tk.StringVar(value="#000000")
        self.btn_label_color = tk.Button(
            tab, width=6, background="#000000", relief=tk.RAISED,
            borderwidth=2, command=self._choose_label_color,
        )
        self.btn_label_color.grid(row=4, column=1, sticky="w", pady=3)

        ttk.Label(tab, text="样式").grid(row=5, column=0, sticky="w", pady=3)
        style_box = ttk.Frame(tab)
        style_box.grid(row=5, column=1, columnspan=2, sticky="w", pady=3)
        self.var_label_bold = tk.BooleanVar(value=False)
        self.chk_label_bold = ttk.Checkbutton(
            style_box, text="加粗", variable=self.var_label_bold,
            command=self._on_label_changed)
        self.chk_label_bold.pack(side=tk.LEFT)
        self.var_label_italic = tk.BooleanVar(value=False)
        self.chk_label_italic = ttk.Checkbutton(
            style_box, text="斜体", variable=self.var_label_italic,
            command=self._on_label_changed)
        self.chk_label_italic.pack(side=tk.LEFT, padx=6)
        self.var_label_formula = tk.BooleanVar(value=False)
        self.chk_label_formula = ttk.Checkbutton(
            style_box, text="LaTeX 公式", variable=self.var_label_formula,
            command=self._on_label_changed)
        self.chk_label_formula.pack(side=tk.LEFT)

        ttk.Label(tab, text="角度").grid(row=6, column=0, sticky="w", pady=3)
        self.var_fig_rotation = tk.StringVar(value="0")
        self.spin_fig_rotation = ttk.Spinbox(
            tab, from_=-180, to=180, increment=1,
            textvariable=self.var_fig_rotation, width=6)
        self.spin_fig_rotation.grid(row=6, column=1, sticky="w", pady=3)
        self.var_fig_rotation.trace_add(
            "write", lambda *_a: self._on_figure_changed())
        ttk.Label(tab, text="度", style="Hint.TLabel").grid(
            row=6, column=2, sticky="w", padx=(4, 0))

        ttk.Button(tab, text="编辑图中的文字…", width=16,
                   command=self._edit_figure_text).grid(
            row=7, column=0, columnspan=3, sticky="w", pady=(8, 3))
        ttk.Button(tab, text="取图范围…", width=16,
                   command=self._edit_figure_region).grid(
            row=8, column=0, columnspan=3, sticky="w", pady=(3, 3))
        ttk.Label(
            tab,
            text="识别并改写导入图里的文字\n（矢量保留，留空即删除）\n"
                 "取图范围：只保留框住的部分",
            style="Hint.TLabel", wraplength=180, justify=tk.LEFT,
        ).grid(row=9, column=0, columnspan=3, sticky="w")

    # -- the free text tab --------------------------------------------------

    def _build_text_tab(self, tab: ttk.Frame) -> None:
        tab.columnconfigure(1, weight=1)

        ttk.Label(tab, text="内容").grid(row=0, column=0, sticky="nw", pady=3)
        self.txt_text = tk.Text(tab, height=3, width=18, wrap="word")
        self.txt_text.grid(row=0, column=1, columnspan=2, sticky="ew", pady=3)
        self.txt_text.bind("<<Modified>>", self._on_text_changed)
        self._text_modified = False

        ttk.Label(tab, text="字体").grid(row=1, column=0, sticky="w", pady=3)
        self.var_text_font = tk.StringVar(value=comp.LABEL_FONT_DEFAULT)
        self.cmb_text_font = ttk.Combobox(
            tab, textvariable=self.var_text_font, state="readonly", width=14,
            values=list(comp.LABEL_FONTS),
        )
        self.cmb_text_font.grid(row=1, column=1, sticky="ew", pady=3)
        self.cmb_text_font.bind("<<ComboboxSelected>>", self._on_text_changed)
        ttk.Button(tab, text="文件…", width=5,
                   command=self._choose_text_font_file).grid(
            row=1, column=2, sticky="w", padx=(4, 0))

        ttk.Label(tab, text="字号").grid(row=2, column=0, sticky="w", pady=3)
        self.var_text_size = tk.StringVar(value="10")
        self.spin_text_size = ttk.Spinbox(
            tab, from_=4, to=48, increment=1,
            textvariable=self.var_text_size, width=6)
        self.spin_text_size.grid(row=2, column=1, sticky="w", pady=3)
        self.var_text_size.trace_add("write", lambda *_a: self._on_text_changed())

        ttk.Label(tab, text="颜色").grid(row=3, column=0, sticky="w", pady=3)
        self.var_text_color = tk.StringVar(value="#000000")
        self.btn_text_color = tk.Button(
            tab, width=6, background="#000000", relief=tk.RAISED,
            borderwidth=2, command=self._choose_text_color,
        )
        self.btn_text_color.grid(row=3, column=1, sticky="w", pady=3)

        ttk.Label(tab, text="样式").grid(row=4, column=0, sticky="w", pady=3)
        style_box = ttk.Frame(tab)
        style_box.grid(row=4, column=1, columnspan=2, sticky="w", pady=3)
        self.var_text_bold = tk.BooleanVar(value=False)
        self.chk_text_bold = ttk.Checkbutton(
            style_box, text="加粗", variable=self.var_text_bold,
            command=self._on_text_changed)
        self.chk_text_bold.pack(side=tk.LEFT)
        self.var_text_italic = tk.BooleanVar(value=False)
        self.chk_text_italic = ttk.Checkbutton(
            style_box, text="斜体", variable=self.var_text_italic,
            command=self._on_text_changed)
        self.chk_text_italic.pack(side=tk.LEFT, padx=6)
        self.var_text_formula = tk.BooleanVar(value=False)
        self.chk_text_formula = ttk.Checkbutton(
            style_box, text="LaTeX 公式", variable=self.var_text_formula,
            command=self._on_text_changed)
        self.chk_text_formula.pack(side=tk.LEFT)
        self.var_text_box = tk.BooleanVar(value=False)
        self.chk_text_box = ttk.Checkbutton(
            style_box, text="文本框", variable=self.var_text_box,
            command=self._on_text_changed)
        self.chk_text_box.pack(side=tk.LEFT, padx=6)

        ttk.Label(tab, text="角度").grid(row=5, column=0, sticky="w", pady=3)
        self.var_text_rotation = tk.StringVar(value="0")
        self.spin_text_rotation = ttk.Spinbox(
            tab, from_=-180, to=180, increment=1,
            textvariable=self.var_text_rotation, width=6)
        self.spin_text_rotation.grid(row=5, column=1, sticky="w", pady=3)
        self.var_text_rotation.trace_add(
            "write", lambda *_a: self._on_text_changed())
        ttk.Label(tab, text="度", style="Hint.TLabel").grid(
            row=5, column=2, sticky="w", padx=(4, 0))

    # -- the graphic element tab --------------------------------------------

    def _build_shape_tab(self, tab: ttk.Frame) -> None:
        tab.columnconfigure(1, weight=1)

        ttk.Label(tab, text="边框色").grid(row=0, column=0, sticky="w", pady=3)
        self.var_shape_color = tk.StringVar(value="#d91a1a")
        self.btn_shape_color = tk.Button(
            tab, width=6, background="#d91a1a", relief=tk.RAISED,
            borderwidth=2, command=self._choose_shape_color,
        )
        self.btn_shape_color.grid(row=0, column=1, sticky="w", pady=3)

        ttk.Label(tab, text="边框宽").grid(row=1, column=0, sticky="w", pady=3)
        self.var_shape_width = tk.StringVar(value="1.5")
        self.spin_shape_width = ttk.Spinbox(
            tab, from_=0.2, to=12, increment=0.5,
            textvariable=self.var_shape_width, width=6,
        )
        self.spin_shape_width.grid(row=1, column=1, sticky="w", pady=3)
        self.var_shape_width.trace_add("write", lambda *_a: self._on_shape_changed())

        ttk.Label(tab, text="边框线型").grid(row=2, column=0, sticky="w", pady=3)
        self.var_shape_dash = tk.StringVar(value=comp.DASH_SOLID)
        self.cmb_shape_dash = ttk.Combobox(
            tab, textvariable=self.var_shape_dash, state="readonly", width=8,
            values=[comp.DASH_SOLID, comp.DASH_DASHED, comp.DASH_DOTTED],
        )
        self.cmb_shape_dash.grid(row=2, column=1, sticky="w", pady=3)
        self.cmb_shape_dash.bind("<<ComboboxSelected>>", self._on_shape_changed)

        ttk.Label(tab, text="边框透明").grid(row=3, column=0, sticky="w", pady=3)
        self.var_shape_opacity = tk.StringVar(value="100")
        self.spin_shape_opacity = ttk.Spinbox(
            tab, from_=0, to=100, increment=5,
            textvariable=self.var_shape_opacity, width=6,
        )
        self.spin_shape_opacity.grid(row=3, column=1, sticky="w", pady=3)
        self.var_shape_opacity.trace_add(
            "write", lambda *_a: self._on_shape_changed())

        ttk.Label(tab, text="填充色").grid(row=4, column=0, sticky="w", pady=3)
        self.var_shape_fill_color = tk.StringVar(value="#ffe08a")
        self.btn_shape_fill_color = tk.Button(
            tab, width=6, background="#ffe08a", relief=tk.RAISED,
            borderwidth=2, command=self._choose_shape_fill_color,
        )
        self.btn_shape_fill_color.grid(row=4, column=1, sticky="w", pady=3)

        ttk.Label(tab, text="填充").grid(row=5, column=0, sticky="w", pady=3)
        self.var_shape_fill = tk.BooleanVar(value=False)
        self.chk_shape_fill = ttk.Checkbutton(
            tab, text="填充内部", variable=self.var_shape_fill,
            command=self._on_shape_changed,
        )
        self.chk_shape_fill.grid(row=5, column=1, columnspan=2, sticky="w", pady=3)

        ttk.Label(tab, text="填充透明").grid(row=6, column=0, sticky="w", pady=3)
        self.var_shape_fill_opacity = tk.StringVar(value="25")
        self.spin_shape_fill_opacity = ttk.Spinbox(
            tab, from_=0, to=100, increment=5,
            textvariable=self.var_shape_fill_opacity, width=6,
        )
        self.spin_shape_fill_opacity.grid(row=6, column=1, sticky="w", pady=3)
        self.var_shape_fill_opacity.trace_add(
            "write", lambda *_a: self._on_shape_changed())

        ttk.Label(tab, text="角度").grid(row=7, column=0, sticky="w", pady=3)
        self.var_shape_rotation = tk.StringVar(value="0")
        self.spin_shape_rotation = ttk.Spinbox(
            tab, from_=-180, to=180, increment=1,
            textvariable=self.var_shape_rotation, width=6,
        )
        self.spin_shape_rotation.grid(row=7, column=1, sticky="w", pady=3)
        self.var_shape_rotation.trace_add(
            "write", lambda *_a: self._on_shape_changed())
        ttk.Label(tab, text="度", style="Hint.TLabel").grid(
            row=7, column=2, sticky="w", padx=(4, 0))

        ttk.Label(tab, text="内容").grid(row=8, column=0, sticky="w", pady=3)
        self.var_shape_text = tk.StringVar(value="")
        self.ent_shape_text = ttk.Entry(tab, textvariable=self.var_shape_text)
        self.ent_shape_text.grid(row=8, column=1, columnspan=2, sticky="ew", pady=3)
        self.var_shape_text.trace_add(
            "write", lambda *_a: self._on_shape_changed())

        ttk.Label(tab, text="文字字号").grid(row=9, column=0, sticky="w", pady=3)
        self.var_shape_text_size = tk.StringVar(value="9")
        self.spin_shape_text_size = ttk.Spinbox(
            tab, from_=4, to=48, increment=1,
            textvariable=self.var_shape_text_size, width=6,
        )
        self.spin_shape_text_size.grid(row=9, column=1, sticky="w", pady=3)
        self.var_shape_text_size.trace_add(
            "write", lambda *_a: self._on_shape_changed())

    # -- the alignment tab ---------------------------------------------------

    def _build_align_tab(self, tab: ttk.Frame) -> None:
        tab.columnconfigure(0, weight=1)

        ttk.Label(tab, text="对齐").grid(row=0, column=0, sticky="w", pady=3)
        align = ttk.Frame(tab)
        align.grid(row=0, column=1, columnspan=2, sticky="w", pady=3)
        for i, (text, mode) in enumerate((
                ("左", comp.ALIGN_LEFT),
                ("中", comp.ALIGN_HCENTER),
                ("右", comp.ALIGN_RIGHT),
                ("顶", comp.ALIGN_TOP),
                ("中", comp.ALIGN_VCENTER),
                ("底", comp.ALIGN_BOTTOM))):
            ttk.Button(align, text=text, width=3,
                       command=lambda m=mode: self._align(m)).grid(
                row=i // 3, column=i % 3, padx=1, pady=1)

        ttk.Label(tab, text="分布").grid(row=1, column=0, sticky="w", pady=3)
        spread = ttk.Frame(tab)
        spread.grid(row=1, column=1, columnspan=2, sticky="w", pady=3)
        ttk.Button(spread, text="水平等间距", width=10,
                   command=lambda: self._distribute(
                       comp.DISTRIBUTE_HORIZONTAL)).pack(side=tk.LEFT, padx=1)
        ttk.Button(spread, text="垂直等间距", width=10,
                   command=lambda: self._distribute(
                       comp.DISTRIBUTE_VERTICAL)).pack(side=tk.LEFT, padx=1)

        ttk.Label(tab, text="一样大").grid(row=2, column=0, sticky="w", pady=3)
        same = ttk.Frame(tab)
        same.grid(row=3, column=0, columnspan=3, sticky="w", pady=3)
        ttk.Button(same, text="同宽", width=7,
                   command=lambda: self._match_size("width")).pack(side=tk.LEFT,
                                                                    padx=1)
        ttk.Button(same, text="同高", width=7,
                   command=lambda: self._match_size("height")).pack(side=tk.LEFT,
                                                                     padx=1)
        ttk.Button(same, text="等大", width=7,
                   command=lambda: self._match_size("both")).pack(side=tk.LEFT,
                                                                   padx=1)

        ttk.Label(tab, text="阵列").grid(row=4, column=0, sticky="w", pady=3)
        ttk.Button(tab, text="矩阵阵列…", width=12,
                   command=self._array_dialog).grid(
            row=4, column=1, columnspan=2, sticky="w", pady=3)

        ttk.Label(
            tab,
            text="多选（Ctrl+点击）后：\n"
                 "对齐 = 彼此对齐；单个时对齐到纸张\n"
                 "分布 = 首尾不动，中间等间距\n"
                 "一样大 = 第一个跟第二个（先选的改）\n"
                 "编号也能单独选中一起对齐",
            style="Hint.TLabel", wraplength=190, justify=tk.LEFT,
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 0))

        ttk.Label(
            tab,
            text="多选（Ctrl+点击）后：\n"
                 "对齐 = 彼此对齐；单个时对齐到纸张\n"
                 "分布 = 首尾不动，中间等间距",
            style="Hint.TLabel", wraplength=190, justify=tk.LEFT,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))
    def _build_footer(self) -> None:
        foot = ttk.Frame(self, padding=(10, 4, 10, 8))
        foot.pack(side=tk.BOTTOM, fill=tk.X)
        self.lbl_status = ttk.Label(foot, text="", style="Hint.TLabel")
        self.lbl_status.pack(side=tk.LEFT)
        ttk.Button(foot, text="−", width=3,
                   command=lambda: self._zoom_by(1.0 / 1.15)).pack(side=tk.RIGHT)
        self.lbl_zoom = ttk.Label(foot, text="100%", style="Hint.TLabel",
                                  width=6, anchor="e")
        self.lbl_zoom.pack(side=tk.RIGHT, padx=4)
        ttk.Button(foot, text="+", width=3,
                   command=lambda: self._zoom_by(1.15)).pack(side=tk.RIGHT)
        ttk.Button(foot, text="适应窗口", width=8,
                   command=self._zoom_fit).pack(side=tk.RIGHT, padx=6)
        self.lbl_count = ttk.Label(foot, text="", style="Hint.TLabel")
        self.lbl_count.pack(side=tk.RIGHT, padx=(0, 12))

    # -- status ------------------------------------------------------------

    def _status(self, message: str) -> None:
        self.lbl_status.configure(text=message)

    def _update_count(self) -> None:
        n = len(self.composition.placements)
        m = len(self.composition.shapes)
        t = len(self.composition.texts)
        text = f"画布上 {n} 个图"
        if m:
            text += f" · {m} 个图形"
        if t:
            text += f" · {t} 个文本"
        if self.palette:
            text += f"  ·  素材 {len(self.palette)} 个"
        self.lbl_count.configure(text=text)
        try:
            self.lbl_zoom.configure(text=f"{self._zoom_factor * 100:.0f}%")
        except tk.TclError:  # pragma: no cover -- before the footer exists
            pass

    # -- import ------------------------------------------------------------

    def _detect_margin(self) -> float:
        """How much white space the detection keeps, in points."""
        try:
            return max(0.0, float(self.var_detect_margin.get())) * 72.0 / 25.4
        except (ValueError, tk.TclError):
            return 2.0 * 72.0 / 25.4

    def import_pdfs(self, paths: Sequence[Path]
                    ) -> Tuple[int, List[str], List[str]]:
        """Import the figures of several PDFs into the palette.

        Args:
            paths: The files to read.

        Returns:
            ``(added, empty, failed)`` -- how many figures went in, the
            files that held no figure, and the files that could not be read.
            One unreadable file does not stop the others.
        """
        added = 0
        empty: List[str] = []
        failed: List[str] = []
        margin = self._detect_margin()
        for path in paths:
            try:
                found = comp.detect_figures(Path(path), self.cfg)
            except Exception as exc:  # noqa: BLE001 -- shown to the user
                failed.append(f"{Path(path).name}：{exc}")
                continue
            if not found:
                empty.append(Path(path).name)
                continue
            for fig in found:
                if margin > 0:
                    # The detector measures the ink; a pale element, or one
                    # that sits just outside it, would be cut off.  The
                    # margin gives everything a little room.
                    fig.region = fitz.Rect(
                        fig.region.x0 - margin, fig.region.y0 - margin,
                        fig.region.x1 + margin, fig.region.y1 + margin)
                self.palette.append(fig)
            added += len(found)
        if added:
            self._refresh_palette()
            self._update_count()
        return added, empty, failed

    def _import_images(self) -> None:
        """Bring one or more pictures in.

        A picture is turned into a page of its own first, so it can be
        placed and aligned like any other figure.
        """
        paths = filedialog.askopenfilenames(
            title="选择图片（可多选）",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.tif *.tiff *.bmp *.gif"),
                       ("所有文件", "*.*")],
        )
        if not paths:
            return
        added = 0
        failed: List[str] = []
        for name in paths:
            path = Path(name)
            try:
                pdf = comp.image_to_pdf(path)
            except Exception as exc:  # noqa: BLE001 -- shown to the user
                failed.append(f"{path.name}：{exc}")
                continue
            doc = fitz.open(str(pdf))
            try:
                visible = comp.source_visible_rect(doc, 0)
                self.palette.append(comp.Figure(
                    path=pdf, page_index=0,
                    region=fitz.Rect(0, 0, visible.width, visible.height)))
            finally:
                doc.close()
            added += 1
        if added:
            self._refresh_palette()
            self._update_count()
            self._status(f"已导入 {added} 张图片。")
        if failed:
            messagebox.showwarning(
                "部分图片读取失败",
                "以下图片没有读进来：\n\n" + "\n".join(failed),
            )

    def _import_whole_pages(self) -> None:
        """Import pages as they are -- nothing is cut off.

        This is the way to bring in a figure the detector would split or
        trim: the whole page becomes one figure.
        """
        paths = filedialog.askopenfilenames(
            title="选择要整页导入的 PDF（可多选）",
            filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")],
        )
        if not paths:
            return
        added = 0
        failed: List[str] = []
        for name in paths:
            path = Path(name)
            try:
                doc = fitz.open(str(path))
            except Exception as exc:  # noqa: BLE001 -- shown to the user
                failed.append(f"{path.name}：{exc}")
                continue
            try:
                for pno in range(len(doc)):
                    visible = comp.source_visible_rect(doc, pno)
                    self.palette.append(comp.Figure(
                        path=path, page_index=pno,
                        region=fitz.Rect(0, 0, visible.width, visible.height)))
                    added += 1
            finally:
                doc.close()
        if added:
            self._refresh_palette()
            self._update_count()
        if failed:
            messagebox.showwarning(
                "部分文件读取失败",
                "以下文件没有读进来：\n\n" + "\n".join(failed),
            )
        if added:
            self._status(f"已整页导入 {added} 个图。")

    def _import_pdf(self) -> None:
        """Import the figures of one or more PDFs into the palette."""
        paths = filedialog.askopenfilenames(
            title="选择要取子图的 PDF（可多选）",
            filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")],
        )
        if not paths:
            return
        added, empty, failed = self.import_pdfs([Path(p) for p in paths])
        if failed:
            messagebox.showwarning(
                "部分文件读取失败",
                "以下文件没有读进来：\n\n" + "\n".join(failed),
            )
        if empty:
            messagebox.showinfo(
                "没有检测到子图",
                "这些 PDF 里没有自动检测出图片：\n"
                + "\n".join(empty)
                + "\n\n可以用「导入已裁剪的图」把整页作为一个图放进来。",
            )
        if added:
            names = "、".join(Path(n).name for n in paths[:3])
            more = " 等" if len(paths) > 3 else ""
            self._status(f"从 {names}{more} 导入 {added} 个子图。")
        elif not failed and not empty:
            self._status("没有导入任何图。")

    def add_paths(self, paths: Sequence[Path]) -> int:
        """Put whole pages of files into the palette.

        This is what the cropping window hands over: each cropped file is
        already one figure, so the page is taken whole.
        """
        added = 0
        for p in paths:
            try:
                doc = fitz.open(str(p))
            except Exception:  # noqa: BLE001 -- a file that cannot be read
                continue
            try:
                visible = comp.source_visible_rect(doc, 0)
                # A region is measured from the visible rect's bottom-left.
                region = fitz.Rect(0, 0, visible.width, visible.height)
            finally:
                doc.close()
            self.palette.append(comp.Figure(path=Path(p), page_index=0,
                                             region=region))
            added += 1
        if added:
            self._refresh_palette()
            self._update_count()
        return added

    def _import_cropped(self) -> None:
        """Import one or more already-cropped figures, whole page each."""
        paths = filedialog.askopenfilenames(
            title="选择已裁剪好的图（可多选，整页作为一个图）",
            filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")],
        )
        if not paths:
            return
        added = self.add_paths([Path(p) for p in paths])
        if added:
            self._status(f"已导入 {added} 个图（整页）。")
        else:
            self._status("没有文件能读进来。")

    def _refresh_palette(self) -> None:
        self._hide_palette_preview()
        self.lst_palette.delete(0, tk.END)
        for fig in self.palette:
            size = self._figure_size(fig)
            self.lst_palette.insert(
                tk.END, f"{fig.name}  {size[0]:.0f}×{size[1]:.0f}pt"
            )

    # -- the floating preview of a palette entry ---------------------------

    def _on_palette_motion(self, event: tk.Event) -> None:
        """Show what the figure under the cursor looks like.

        The name of a file says nothing about the picture in it, so the
        figure is shown floating over the palette while the cursor rests on
        its entry.
        """
        index = self.lst_palette.nearest(event.y)
        box = self.lst_palette.bbox(index) if index >= 0 else None
        if not box or not (box[1] <= event.y <= box[1] + box[3]):
            # The cursor is between the entries.
            self._hide_palette_preview()
            return
        if index == self._preview_index and self._preview_win is not None:
            return
        self._show_palette_preview(index, event)

    def _show_palette_preview(self, index: int, event: tk.Event) -> None:
        """Put the figure of a palette entry in a little floating window."""
        if not (0 <= index < len(self.palette)):
            self._hide_palette_preview()
            return
        image = self._figure_image(self.palette[index])
        if image is None:
            self._hide_palette_preview()
            return
        scale = dpi.scale_factor(self)
        limit_w = int(PREVIEW_MAX_W * scale)
        limit_h = int(PREVIEW_MAX_H * scale)
        if image.width > limit_w or image.height > limit_h:
            factor = min(limit_w / image.width, limit_h / image.height)
            image = image.resize(
                (max(1, int(image.width * factor)),
                 max(1, int(image.height * factor))),
                Image.LANCZOS)
        photo = ImageTk.PhotoImage(image)
        self._hide_palette_preview()
        win = tk.Toplevel(self)
        win.overrideredirect(True)
        try:
            win.attributes("-topmost", True)
        except tk.TclError:  # pragma: no cover -- a window without a manager
            pass
        tk.Label(win, image=photo, borderwidth=1, relief="solid").pack()
        # Beside the cursor, but on the screen.
        x = event.x_root + PREVIEW_OFFSET_PX
        y = event.y_root + PREVIEW_OFFSET_PX
        if x + image.width > win.winfo_screenwidth():
            x = max(0, event.x_root - image.width - PREVIEW_OFFSET_PX)
        if y + image.height > win.winfo_screenheight():
            y = max(0, event.y_root - image.height - PREVIEW_OFFSET_PX)
        win.geometry("+%d+%d" % (x, y))
        self._preview_win = win
        self._preview_photo = photo
        self._preview_index = index

    def _hide_palette_preview(self) -> None:
        """Take the floating preview away."""
        win = self._preview_win
        self._preview_win = None
        self._preview_photo = None
        self._preview_index = -1
        if win is not None:
            try:
                win.destroy()
            except tk.TclError:  # pragma: no cover -- already gone
                pass

    def _figure_size(self, fig: comp.Figure) -> Tuple[float, float]:
        try:
            return comp.figure_display_size(fig)
        except Exception:  # noqa: BLE001
            return (0.0, 0.0)

    @_action
    def _remove_from_palette(self) -> None:
        """Take the selected entries out of the palette.

        A figure that is already on the sheet goes with them: it is the same
        picture, and leaving it there would be a figure nobody can take
        away any more.
        """
        selection = self.lst_palette.curselection()
        if not selection:
            self._status("先在素材栏里选中要删除的条目。")
            return
        chosen = set(selection)
        gone = [self.palette[i] for i in selection]
        # The figures on the sheet that came from these entries.
        keys = {fig.key for fig in gone}
        keep = [p for p in self.composition.placements
                if p.figure.key not in keys]
        removed = len(self.composition.placements) - len(keep)
        if removed:
            self.composition.placements = keep
            self._clear_selection()
        # Back to front, so the indices stay valid while deleting.
        for i in sorted(chosen, reverse=True):
            del self.palette[i]
        self._refresh_palette()
        self._update_count()
        self._redraw()
        message = f"已从素材栏删除 {len(chosen)} 个条目。"
        if removed:
            message += f"画布上的 {removed} 个图也一并移除了。"
        self._status(message)

    @_action
    def _clear_palette(self) -> None:
        """Empty the palette -- and the sheet with it."""
        if not self.palette:
            self._status("素材栏已经是空的。")
            return
        if not messagebox.askyesno(
            "清空素材",
            f"素材栏里的 {len(self.palette)} 个条目都要去掉吗？\n\n"
            "已经放到画布上的图也会一起移除。",
        ):
            return
        n = len(self.palette)
        keys = {fig.key for fig in self.palette}
        self.palette.clear()
        self.composition.placements = [
            p for p in self.composition.placements if p.figure.key not in keys
        ]
        self._clear_selection()
        self._refresh_palette()
        self._update_count()
        self._redraw()
        self._status(f"已清空素材栏（{n} 个条目）。")

    @_action
    def _add_selected(self) -> None:
        sel = self.lst_palette.curselection()
        if not sel:
            return
        fig = self.palette[sel[0]]
        w, h = self._figure_size(fig)
        if w <= 0:
            return
        # Drop it in the middle of the sheet, scaled to a sensible size.
        max_w = self.sheet_w * 0.4
        max_h = self.sheet_h * 0.4
        scale = min(max_w / w, max_h / h, 1.0)
        pw, ph = w * scale, h * scale
        cx = self.sheet_x + self.sheet_w / 2
        cy = self.sheet_y + self.sheet_h / 2
        self.composition.placements.append(
            comp.Placement(
                figure=fig,
                rect=fitz.Rect(cx - pw / 2, cy - ph / 2, cx + pw / 2, cy + ph / 2),
                label=comp.auto_label(len(self.composition.placements) + 1),
            )
        )
        self._selected = len(self.composition.placements) - 1
        self._sync_properties()
        self._redraw()
        self._update_count()

    # -- sheet -------------------------------------------------------------

    @_action
    def _on_sheet_changed(self) -> None:
        mode = self.var_sheet.get()
        custom = mode == "自定义"
        # The size fields only mean something for a custom page.
        for entry in (self.ent_sheet_w, self.ent_sheet_h):
            entry.configure(state="normal" if custom else "disabled")
        if mode == "A4 横向":
            self.sheet_w, self.sheet_h = SHEET_LANDSCAPE
            self.sheet_x = self.sheet_y = 0.0
        elif mode == "A4 纵向":
            self.sheet_w, self.sheet_h = SHEET_PORTRAIT
            self.sheet_x = self.sheet_y = 0.0
        elif custom:
            try:
                w = mm_to_pt(float(self.var_sheet_w.get()))
                h = mm_to_pt(float(self.var_sheet_h.get()))
            except ValueError:
                return
            if w <= 0 or h <= 0:
                return
            self.sheet_w, self.sheet_h = w, h
            self.sheet_x = self.sheet_y = 0.0
        else:
            bounds = self.composition.content_bounds()
            if bounds is not None:
                # The sheet takes the size of the content, and the content
                # comes with it: left where it was, it would end up outside
                # the page -- and the first click on a figure would then pull
                # it back in, which looks like the figure jumping about.
                self.sheet_w, self.sheet_h = self.composition.fitted_size()
                margin = self.composition.margin_pt
                self.sheet_x = self.sheet_y = 0.0
                self._move_content(-bounds.x0 + margin, -bounds.y0 + margin)
        self.composition.width = self.sheet_w
        self.composition.height = self.sheet_h
        self._redraw()

    def _move_content(self, dx: float, dy: float) -> None:
        """Move everything on the sheet by an offset.

        The labels are placed relative to their figure, so they travel with
        it and need no adjusting of their own.
        """
        for placement in self.composition.placements:
            rect = placement.rect
            placement.rect = fitz.Rect(rect.x0 + dx, rect.y0 + dy,
                                       rect.x1 + dx, rect.y1 + dy)
        for shape in self.composition.shapes:
            shape.start = fitz.Point(shape.start.x + dx, shape.start.y + dy)
            shape.end = fitz.Point(shape.end.x + dx, shape.end.y + dy)
        for item in self.composition.texts:
            item.position = fitz.Point(item.position.x + dx,
                                       item.position.y + dy)

    @_action
    def _on_margin_changed(self) -> None:
        try:
            self.composition.margin_pt = mm_to_pt(float(self.var_margin.get()))
        except ValueError:
            return
        self._redraw()

    # -- the view: zoom and pan --------------------------------------------

    def _on_wheel(self, event: tk.Event) -> None:
        """The wheel zooms the view around the cursor."""
        step = 1.15 if event.delta > 0 else 1.0 / 1.15
        target = self._zoom_factor * step
        if not 0.05 <= target <= 20.0:
            return
        # The point under the cursor has to stay under the cursor.
        ux, uy = self._to_user(event.x, event.y)
        self._zoom_factor = target
        self._redraw()
        nx, ny = self._to_canvas(ux, uy)
        self._origin = (self._origin[0] + (event.x - nx),
                        self._origin[1] + (event.y - ny))
        self._redraw()

    def _on_pan_start(self, event: tk.Event) -> None:
        self._pan_origin = (event.x, event.y)
        self._pan_from = self._origin

    def _on_pan_move(self, event: tk.Event) -> None:
        if self._pan_origin is None:
            return
        dx = event.x - self._pan_origin[0]
        dy = event.y - self._pan_origin[1]
        self._origin = (self._pan_from[0] + dx, self._pan_from[1] + dy)
        self._redraw()

    def _on_pan_end(self, event: tk.Event) -> None:
        self._pan_origin = None

    def _zoom_by(self, factor: float) -> None:
        """Zoom in or out around the middle of the canvas."""
        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())
        target = self._zoom_factor * factor
        if not 0.05 <= target <= 20.0:
            return
        ux, uy = self._to_user(cw / 2, ch / 2)
        self._zoom_factor = target
        self._redraw()
        nx, ny = self._to_canvas(ux, uy)
        self._origin = (self._origin[0] + (cw / 2 - nx),
                        self._origin[1] + (ch / 2 - ny))
        self._redraw()

    def _zoom_fit(self) -> None:
        """Back to the view that fits the whole sheet."""
        self._zoom_factor = 1.0
        self._redraw()

    # -- coordinate mapping -------------------------------------------------

    def _to_canvas(self, x: float, y: float) -> Tuple[float, float]:
        """User space (bottom-up) -> canvas pixels (top-down)."""
        ox, oy = self._origin
        return (ox + (x - self.sheet_x) * self._zoom,
                oy + (self.sheet_y + self.sheet_h - y) * self._zoom)

    def _to_user(self, cx: float, cy: float) -> Tuple[float, float]:
        ox, oy = self._origin
        return (self.sheet_x + (cx - ox) / self._zoom,
                self.sheet_y + self.sheet_h - (cy - oy) / self._zoom)

    def _sheet_rect(self) -> fitz.Rect:
        """The sheet on the canvas, in the composition's frame."""
        return fitz.Rect(self.sheet_x, self.sheet_y,
                         self.sheet_x + self.sheet_w,
                         self.sheet_y + self.sheet_h)

    def _canvas_rect(self, rect: fitz.Rect) -> Tuple[float, float, float, float]:
        """The canvas rectangle of a placement: left, top, right, bottom."""
        return sheet_to_canvas_box(
            rect, self._origin, self._zoom, self.sheet_y + self.sheet_h)

    # -- drawing -----------------------------------------------------------

    def _redraw(self) -> None:
        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())
        # A point of the sheet is this many pixels on this display.
        pp = dpi.pixels_per_point(self)
        # The zoom that fits the sheet, and the user's own factor on top of
        # it -- the wheel changes the factor, so the view stays where the
        # user put it.
        self._fit_zoom = min(cw / (self.sheet_w * pp), ch / (self.sheet_h * pp), pp)
        self._zoom = self._fit_zoom * self._zoom_factor
        pw = max(1, self.sheet_w * self._zoom)
        ph = max(1, self.sheet_h * self._zoom)
        self._origin = (cw / 2 - pw / 2, ch / 2 - ph / 2)

        self.canvas.delete("all")
        self.canvas.create_rectangle(
            self._origin[0], self._origin[1],
            self._origin[0] + pw, self._origin[1] + ph,
            fill="white", outline="#303030", width=1, tags=("sheet",),
        )
        self._draw_sheet_handles(self._origin[0], self._origin[1], pw, ph)
        for idx, placement in enumerate(self.composition.placements):
            self._draw_placement(idx, placement)
        # The graphic elements sit on top of the figures, the free text on
        # top of those, and the labels on top of everything -- a label under
        # an arrow is unreadable.
        for idx, shape in enumerate(self.composition.shapes):
            self._draw_shape(idx, shape)
        for idx, item in enumerate(self.composition.texts):
            self._draw_text(idx, item)
        for idx, placement in enumerate(self.composition.placements):
            self._draw_label(idx, placement)
        if self._shape_preview is not None:
            self._draw_shape(-1, self._shape_preview)
        self._update_count()

    def _figure_image(self, fig: comp.Figure) -> Optional[Image.Image]:
        """Raster of a figure, for the canvas preview."""
        if fig.key in self._thumb_cache:
            return self._thumb_cache[fig.key]
        try:
            baked = fitz.open(str(comp.bake_figure(fig)))
            try:
                pix = baked[0].get_pixmap(dpi=PREVIEW_DPI)
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            finally:
                baked.close()
        except Exception:  # noqa: BLE001
            return None
        self._thumb_cache[fig.key] = img
        return img

    def _sheet_handle_positions(self, left: float, top: float,
                                width: float, height: float
                                ) -> List[Tuple[float, float]]:
        """Where the sheet's edges and corners are, on the canvas.

        The order is: top-left, top-middle, top-right, right-middle,
        bottom-right, bottom-middle, bottom-left, left-middle -- so a
        handle's position says which edge it moves.
        """
        cx = left + width / 2
        cy = top + height / 2
        return [
            (left, top), (cx, top), (left + width, top), (left + width, cy),
            (left + width, top + height), (cx, top + height),
            (left, top + height), (left, cy),
        ]

    def _draw_sheet_handles(self, left: float, top: float,
                             width: float, height: float) -> None:
        """The grips that make the sheet bigger or smaller."""
        for hx, hy in self._sheet_handle_positions(left, top, width, height):
            self.canvas.create_rectangle(
                hx - SHEET_HANDLE_PX, hy - SHEET_HANDLE_PX,
                hx + SHEET_HANDLE_PX, hy + SHEET_HANDLE_PX,
                fill="#606060", outline="#d0d0d0", width=1, tags=("sheet",),
            )

    def _hit_sheet_handle(self, cx: float, cy: float) -> Optional[int]:
        """Which grip of the sheet the cursor is on."""
        pw = self.sheet_w * self._zoom
        ph = self.sheet_h * self._zoom
        for i, (hx, hy) in enumerate(self._sheet_handle_positions(
                self._origin[0], self._origin[1], pw, ph)):
            if abs(cx - hx) <= SHEET_HANDLE_PX + 2 and \
                    abs(cy - hy) <= SHEET_HANDLE_PX + 2:
                return i
        return None

    def _draw_placement(self, idx: int, placement: comp.Placement) -> None:
        left, top, right, bottom = self._canvas_rect(placement.rect)
        # A turned figure takes up the box its corners sweep, the same way
        # it does on the exported page.
        if abs(placement.rotation) > 1e-6:
            box = comp.rotated_bbox(fitz.Rect(left, top, right, bottom),
                                    placement.rotation)
            left, top, right, bottom = box.x0, box.y0, box.x1, box.y1
        # Every selected figure is outlined; the handles belong to the one
        # the property panel is editing.
        selected = idx in self._sel_figs
        primary = idx == self._selected and not self._sel_shapes

        img = self._figure_image(placement.figure)
        if img is not None and right > left and bottom > top:
            tw, th = int(right - left), int(bottom - top)
            if tw > 0 and th > 0:
                scaled = img.resize((tw, th), Image.LANCZOS)
                if abs(placement.rotation) > 1e-6:
                    # Pillow turns anticlockwise; the sheet turns clockwise.
                    scaled = scaled.rotate(-placement.rotation, expand=True,
                                           resample=Image.BICUBIC)
                photo = ImageTk.PhotoImage(scaled)
                self._photo_cache[placement.figure.key] = photo
                self.canvas.create_image(
                    (left + right) / 2, (top + bottom) / 2, image=photo,
                    anchor=tk.CENTER, tags=("fig", str(idx))
                )
        else:
            self.canvas.create_rectangle(
                left, top, right, bottom, fill="#eeeeee", outline="#999999",
                tags=("fig", str(idx)),
            )

        outline = "#ff7f0e" if selected else "#1f6feb"
        self.canvas.create_rectangle(
            left, top, right, bottom, outline=outline,
            width=3 if selected else 2, tags=("fig", str(idx)),
        )

        if primary:
            for hx, hy in HANDLE_POSITIONS(left, top, right, bottom):
                self.canvas.create_rectangle(
                    hx - HANDLE_PX / 2, hy - HANDLE_PX / 2,
                    hx + HANDLE_PX / 2, hy + HANDLE_PX / 2,
                    fill="#ff7f0e", outline="white", width=1, tags=("fig", str(idx)),
                )

    def _draw_label(self, idx: int, placement: comp.Placement) -> None:
        if not placement.label:
            return
        family = placement.label_font or comp.LABEL_FONT_DEFAULT
        # The label is drawn where the exporter will put it: the same
        # anchor, the same size -- scaled with the sheet.
        anchor = placement.label_anchor()
        x, y = self._to_canvas(anchor.x, anchor.y)
        r, g, b = placement.label_color
        style = label_style(placement.label_bold, placement.label_italic)
        size_px = placement.label_size * self._zoom
        # A font file the user picked: Tk cannot load a file, so the text is
        # drawn with Pillow -- from the very file the export embeds.
        if placement.label_fontfile and Path(placement.label_fontfile).is_file():
            self._draw_text_from_file(
                f"label{idx}", placement.label, placement.label_fontfile,
                size_px, "#%02x%02x%02x" % (int(r * 255), int(g * 255),
                                            int(b * 255)),
                x, y, ("fig", str(idx)))
            return
        font = tk_font(family, size_px, style)
        self.canvas.create_text(
            x, y + font_descent_px(font), text=placement.label,
            fill="#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255)),
            font=font,
            anchor=tk.SW, tags=("fig", str(idx)),
        )

    def _draw_text_from_file(self, key: str, text: str, fontfile: str,
                             size_px: float, colour: str, x: float, y: float,
                             tags: Tuple[str, ...]) -> bool:
        """Draw a piece of text with a font file, the way the export writes it.

        The baseline goes on ``(x, y)``, exactly where the exporter puts it.
        """
        from PIL import ImageDraw, ImageFont

        try:
            font = ImageFont.truetype(fontfile, max(1, int(round(size_px))))
        except OSError:
            return False
        ascent, descent = font.getmetrics()
        width = max(1, int(math.ceil(font.getlength(text))))
        pad = 2
        img = Image.new("RGBA", (width + 2 * pad, ascent + descent + 2 * pad),
                        (255, 255, 255, 0))
        ImageDraw.Draw(img).text((pad, pad + ascent), text, font=font,
                                 fill=colour, anchor="ls")
        photo = ImageTk.PhotoImage(img)
        self._photo_cache[key] = photo
        self.canvas.create_image(x - pad, y - (pad + ascent), image=photo,
                                 anchor=tk.NW, tags=tags)
        return True

    def _draw_text(self, idx: int, item: comp.TextItem) -> None:
        """Draw a piece of free-standing text on the canvas."""
        x, y = self._to_canvas(item.position.x, item.position.y)
        width, height = item.text_size()
        w = width * self._zoom
        h = height * self._zoom
        selected = idx >= 0 and idx in self._sel_texts
        if not item.text:
            # An empty text is still an object: show where it is.
            self.canvas.create_rectangle(
                x, y - h, x + w, y, outline="#999999",
                dash=(2, 2), tags=("text", str(idx)))
            return
        if abs(item.rotation) > 1e-6:
            # Tk cannot turn a text, so the very PDF the export writes is
            # rendered and shown instead.
            self._draw_turned_text(idx, item)
            return
        r, g, b = item.color
        family = item.font or comp.LABEL_FONT_DEFAULT
        style = label_style(item.bold, item.italic)
        if item.box:
            self.canvas.create_rectangle(
                x, y - h, x + w, y, outline="#%02x%02x%02x" % (
                    int(r * 255), int(g * 255), int(b * 255)),
                width=1, tags=("text", str(idx)))
        colour = "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))
        size_px = item.size * self._zoom
        # A font file the user picked: Tk cannot load a file, so the text is
        # drawn with Pillow -- from the very file the export embeds.
        if item.fontfile and Path(item.fontfile).is_file():
            self._draw_text_from_file(f"text{idx}", item.text, item.fontfile,
                                      size_px, colour, x, y, ("text", str(idx)))
        else:
            font = tk_font(family, size_px, style)
            self.canvas.create_text(
                x, y + font_descent_px(font), text=item.text, fill=colour,
                font=font,
                anchor=tk.SW, tags=("text", str(idx)))
        if selected:
            for hx, hy in ((x, y - h), (x + w, y - h), (x + w, y), (x, y)):
                self.canvas.create_rectangle(
                    hx - HANDLE_PX / 2, hy - HANDLE_PX / 2,
                    hx + HANDLE_PX / 2, hy + HANDLE_PX / 2,
                    fill="#ff7f0e", outline="white", width=1,
                    tags=("text", str(idx)),
                )

    def _draw_turned_text(self, idx: int, item: comp.TextItem) -> None:
        """Show a turned text the way it will be printed."""
        rendered = None
        if item.formula:
            from .tex import render_math
            rendered = render_math(item.text, item.size)
        else:
            rendered = comp.render_text_pdf(item)
        if rendered is None:
            return
        doc = fitz.open(str(rendered))
        try:
            page = doc[0]
            spans = [s for b in page.get_text("dict")["blocks"]
                     for line in b.get("lines", []) for s in line["spans"]]
            if not spans:
                return
            box = spans[0]["bbox"]
            origin = spans[0]["origin"]
            # The raster has to match the zoom, so the text is the same
            # size on the canvas as it is on the sheet.
            render_dpi = int(round(max(36.0, 72.0 * self._zoom)))
            pix = page.get_pixmap(dpi=render_dpi)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            scale = render_dpi / 72.0
            # The ink, cropped out of the rendered page.
            ink = img.crop((int(box[0] * scale), int(box[1] * scale),
                            int(box[2] * scale) + 1,
                            int(box[3] * scale) + 1))
            if abs(item.rotation) > 1e-6:
                # Pillow turns anticlockwise; the sheet turns clockwise.
                ink = ink.rotate(-item.rotation, expand=True,
                                 resample=Image.BICUBIC)
            photo = ImageTk.PhotoImage(ink)
            self._photo_cache[f"text{idx}"] = photo
            # The middle of the ink, in the composition's user space.
            cx = item.position.x + ((box[0] + box[2]) / 2 - origin[0])
            cy = item.position.y - ((box[1] + box[3]) / 2 - origin[1])
            px, py = self._to_canvas(cx, cy)
            self.canvas.create_image(px, py, image=photo, anchor=tk.CENTER,
                                     tags=("text", str(idx)))
        finally:
            doc.close()

    def _draw_shape(self, idx: int, shape: comp.Shape) -> None:
        """Draw a graphic element on the canvas."""
        x0, y0 = self._to_canvas(shape.start.x, shape.start.y)
        x1, y1 = self._to_canvas(shape.end.x, shape.end.y)
        # A turned shape is turned about the middle of its own box -- the
        # canvas counts downwards, the same way the exported page does.
        centre = fitz.Point((x0 + x1) / 2, (y0 + y1) / 2)
        p0 = comp.rotate_point(fitz.Point(x0, y0), centre, shape.rotation)
        p1 = comp.rotate_point(fitz.Point(x1, y1), centre, shape.rotation)
        left, top = min(p0.x, p1.x), min(p0.y, p1.y)
        right, bottom = max(p0.x, p1.x), max(p0.y, p1.y)
        r, g, b = shape.color
        colour = "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))
        width = max(1, int(round(shape.width * self._zoom)))
        selected = idx >= 0 and idx in self._sel_shapes
        primary = idx >= 0 and idx == self._selected_shape
        tag = str(idx) if idx >= 0 else "preview"
        dash = comp.DASH_PATTERNS_TK.get(shape.dash, ())
        # Tk has no transparency for a shape, so a translucent fill is
        # approximated by mixing it into the white of the sheet.
        fill_colour = ""
        if shape.fill:
            fr, fg, fb = shape.fill_rgb
            a = min(1.0, max(0.0, shape.fill_opacity))
            fill_colour = "#%02x%02x%02x" % (
                int((fr * a + (1 - a)) * 255),
                int((fg * a + (1 - a)) * 255),
                int((fb * a + (1 - a)) * 255),
            )

        box = fitz.Rect(left, top, right, bottom)
        if shape.kind == comp.SHAPE_LINE:
            item = self.canvas.create_line(
                p0.x, p0.y, p1.x, p1.y, fill=colour, width=width, dash=dash,
                tags=("shape", tag))
        elif shape.kind == comp.SHAPE_ARROW:
            item = self.canvas.create_line(
                p0.x, p0.y, p1.x, p1.y, fill=colour, width=width, dash=dash,
                arrow=tk.LAST, arrowshape=(8, 10, 4), tags=("shape", tag))
        elif shape.kind == comp.SHAPE_ELLIPSE:
            item = self.canvas.create_oval(
                left, top, right, bottom, outline=colour, width=width,
                fill=fill_colour, dash=dash, tags=("shape", tag))
        elif shape.kind in (comp.SHAPE_RECT, comp.SHAPE_ROUNDRECT):
            item = self.canvas.create_rectangle(
                left, top, right, bottom, outline=colour, width=width,
                fill=fill_colour, dash=dash, tags=("shape", tag))
        else:
            points = comp.shape_outline(shape.kind, box)
            flat: List[float] = []
            for point in points:
                flat.extend((point.x, point.y))
            if flat:
                item = self.canvas.create_polygon(
                    flat, outline=colour, width=width, fill=fill_colour,
                    dash=dash, tags=("shape", tag))
            else:  # pragma: no cover -- every kind has an outline
                item = self.canvas.create_line(
                    p0.x, p0.y, p1.x, p1.y, fill=colour, width=width,
                    tags=("shape", tag))
        self.canvas.addtag_withtag("shape", item)
        self.canvas.itemconfigure(item, tags=("shape", tag))

        if shape.text:
            tw, th = comp.text_width(shape.text, "helv", shape.text_size), \
                shape.text_size * 1.25
            self.canvas.create_text(
                (left + right) / 2, (top + bottom) / 2, text=shape.text,
                fill="#%02x%02x%02x" % tuple(
                    int(c * 255) for c in shape.caption_rgb),
                font=tk_font("Helvetica", shape.text_size * self._zoom),
                anchor=tk.CENTER, tags=("shape", tag))

        if primary:
            for hx, hy in ((left, top), (right, top), (right, bottom),
                           (left, bottom)):
                self.canvas.create_rectangle(
                    hx - HANDLE_PX / 2, hy - HANDLE_PX / 2,
                    hx + HANDLE_PX / 2, hy + HANDLE_PX / 2,
                    fill="#ff7f0e", outline="white", width=1, tags=("shape", tag),
                )

    # -- interaction --------------------------------------------------------

    def _hit(self, cx: float, cy: float) -> Tuple[Optional[int], Optional[str]]:
        """Which placement, and which part, is under the cursor."""
        if self._selected is not None and 0 <= self._selected < len(self.composition.placements):
            p = self.composition.placements[self._selected]
            left, top, right, bottom = self._canvas_rect(p.rect)
            for i, (hx, hy) in enumerate(
                    HANDLE_POSITIONS(left, top, right, bottom)):
                if abs(cx - hx) <= HANDLE_PX and abs(cy - hy) <= HANDLE_PX:
                    return self._selected, f"handle{i}"
        for i in range(len(self.composition.placements) - 1, -1, -1):
            p = self.composition.placements[i]
            left, top, right, bottom = self._canvas_rect(p.rect)
            if abs(p.rotation) > 1e-6:
                box = comp.rotated_bbox(fitz.Rect(left, top, right, bottom),
                                        p.rotation)
                left, top, right, bottom = box.x0, box.y0, box.x1, box.y1
            if left <= cx <= right and top <= cy <= bottom:
                return i, "body"
        return None, None

    def _hit_shape(self, cx: float, cy: float) -> Optional[int]:
        """Which graphic element the cursor is on.

        Only the *stroke* of an element is grabbable, so a translucent box
        drawn over a figure does not stop the figure from being selected.
        """
        for i in range(len(self.composition.shapes) - 1, -1, -1):
            shape = self.composition.shapes[i]
            x0, y0 = self._to_canvas(shape.start.x, shape.start.y)
            x1, y1 = self._to_canvas(shape.end.x, shape.end.y)
            centre = fitz.Point((x0 + x1) / 2, (y0 + y1) / 2)
            p0 = comp.rotate_point(fitz.Point(x0, y0), centre, shape.rotation)
            p1 = comp.rotate_point(fitz.Point(x1, y1), centre, shape.rotation)
            left, top = min(p0.x, p1.x), min(p0.y, p1.y)
            right, bottom = max(p0.x, p1.x), max(p0.y, p1.y)
            if shape.kind == comp.SHAPE_ELLIPSE:
                cxr, cyr = (left + right) / 2, (top + bottom) / 2
                rx = max((right - left) / 2, 1e-6)
                ry = max((bottom - top) / 2, 1e-6)
                r = math.hypot((cx - cxr) / rx, (cy - cyr) / ry)
                if abs(r - 1.0) * min(rx, ry) <= SHAPE_HIT_PX:
                    return i
            elif shape.kind in (comp.SHAPE_LINE, comp.SHAPE_ARROW):
                if distance_to_segment(cx, cy, p0.x, p0.y, p1.x, p1.y) \
                        <= SHAPE_HIT_PX:
                    return i
            else:
                # A closed outline: the box for a rectangle, the shape's own
                # outline for a polygon.
                box = fitz.Rect(left, top, right, bottom)
                points = comp.shape_outline(shape.kind, box) or [
                    fitz.Point(left, top), fitz.Point(right, top),
                    fitz.Point(right, bottom), fitz.Point(left, bottom),
                ]
                for j in range(len(points)):
                    a = points[j]
                    b = points[(j + 1) % len(points)]
                    if distance_to_segment(cx, cy, a.x, a.y, b.x, b.y) \
                            <= SHAPE_HIT_PX:
                        return i
        return None

    def _hit_text(self, cx: float, cy: float) -> Optional[int]:
        """Which piece of free text the cursor is on."""
        for i in range(len(self.composition.texts) - 1, -1, -1):
            item = self.composition.texts[i]
            r = item.rect()
            r.y0 -= self._text_descent_pt(item)
            left, top, right, bottom = self._canvas_rect(r)
            if left - 2 <= cx <= right + 2 and top - 2 <= cy <= bottom + 2:
                return i
        return None

    def _text_descent_pt(self, item: comp.TextItem) -> float:
        """How far a text drops below its baseline, in points."""
        family = item.font or comp.LABEL_FONT_DEFAULT
        font = tk_font(family, item.size * self._zoom,
                       label_style(item.bold, item.italic))
        return font_descent_px(font) / max(self._zoom, 1e-6)

    def _label_descent_pt(self, placement: comp.Placement) -> float:
        """How far a label drops below its baseline, in points."""
        family = placement.label_font or comp.LABEL_FONT_DEFAULT
        font = tk_font(family, placement.label_size * self._zoom,
                       label_style(placement.label_bold, placement.label_italic))
        return font_descent_px(font) / max(self._zoom, 1e-6)

    def _hit_label(self, cx: float, cy: float) -> Optional[int]:
        """Which figure's label the cursor is on.

        The label is drawn on top of everything, so it is picked before the
        figure it belongs to -- which is what makes it draggable.
        """
        for i in range(len(self.composition.placements) - 1, -1, -1):
            placement = self.composition.placements[i]
            if not placement.label:
                continue
            r = comp.free_label_rect(placement)
            # The ink drops below the baseline; the grab area has to hold it,
            # or a click aimed at the tail of a "g" would miss.
            r.y0 -= self._label_descent_pt(placement)
            left, top, right, bottom = self._canvas_rect(r)
            if left - 2 <= cx <= right + 2 and top - 2 <= cy <= bottom + 2:
                return i
        return None

    def _clear_selection(self) -> None:
        """Forget the selection, whichever kinds it held."""
        self._sel_figs = []
        self._sel_shapes = []
        self._sel_texts = []
        self._sel_labels = []
        self._last_kind = None

    def _clamp_to_sheet_point(self, x: float, y: float) -> Tuple[float, float]:
        """A point pulled back onto the sheet.

        The area around the sheet is not part of the page, so an element
        drawn there would be invisible -- and the user would think nothing
        happened.  Pulling it back to the edge keeps it where it can be
        seen.
        """
        sheet = self._sheet_rect()
        return (min(max(x, sheet.x0), sheet.x1),
                min(max(y, sheet.y0), sheet.y1))

    def _on_press(self, event: tk.Event) -> None:
        self.canvas.focus_set()
        if self.tool in (TOOL_TEXT, TOOL_TEXTBOX):
            # Drop a piece of text where the cursor is.
            ux, uy = self._to_user(event.x, event.y)
            ux, uy = self._clamp_to_sheet_point(ux, uy)
            self.composition.texts.append(comp.TextItem(
                text="", position=fitz.Point(ux, uy),
                size=10.0, box=(self.tool == TOOL_TEXTBOX),
            ))
            self._sel_figs = []
            self._sel_shapes = []
            self._sel_texts = [len(self.composition.texts) - 1]
            self._last_kind = "text"
            self._sync_properties()
            self._redraw()
            self._update_count()
            # An empty text shows nothing, so the caret goes straight into
            # the box where it is typed.
            self.nb.select(self.tab_text)
            self.txt_text.focus_set()
            return
        if self.tool != "select":
            # Start drawing a graphic element, in the style the panel shows.
            ux, uy = self._to_user(event.x, event.y)
            ux, uy = self._clamp_to_sheet_point(ux, uy)
            self._shape_preview = comp.Shape(
                self.tool, fitz.Point(ux, uy), fitz.Point(ux, uy),
                color=hex_to_rgb(self.var_shape_color.get()),
                width=self._shape_width(),
                fill=bool(self.var_shape_fill.get()),
                fill_color=hex_to_rgb(self.var_shape_fill_color.get()),
                fill_opacity=self._shape_opacity(
                    self.var_shape_fill_opacity.get()),
                stroke_opacity=self._shape_opacity(
                    self.var_shape_opacity.get()),
                dash=self.var_shape_dash.get(),
            )
            self._drag_mode = "shape"
            self._drag_start = (event.x, event.y)
            self._redraw()
            return

        label_idx = self._hit_label(event.x, event.y)
        text_idx = self._hit_text(event.x, event.y)
        shape_idx = self._hit_shape(event.x, event.y)
        sheet_handle = self._hit_sheet_handle(event.x, event.y)
        idx, part = self._hit(event.x, event.y)
        fig_hit, shape_hit, part = resolve_pick(shape_idx, idx, part)

        if sheet_handle is not None and self.tool == "select":
            # A grip of the sheet itself: the page is resized, not its
            # contents.
            self._clear_selection()
            self._drag_mode = "sheet"
            self._drag_start = (event.x, event.y)
            self._drag_sheet_handle = sheet_handle
            self._drag_sheet_size = (self.sheet_w, self.sheet_h)
            self._drag_sheet_origin = (self.sheet_x, self.sheet_y)
            self._sync_properties()
            self._redraw()
            return

        if label_idx is not None:
            # A label is an object of its own: it can be picked, dragged
            # and lined up without moving the figure it belongs to.
            kind: Optional[str] = "label"
            index: Optional[int] = label_idx
        elif text_idx is not None:
            kind, index = "text", text_idx
        elif shape_hit is not None:
            kind, index = "shape", shape_hit
        elif fig_hit is not None:
            kind, index = "figure", fig_hit
        else:
            kind, index = None, None

        # Ctrl (or Shift) adds to the selection instead of replacing it.
        additive = bool(event.state & (0x0004 | 0x0001))
        if part and part.startswith("handle"):
            # A resize handle belongs to one figure alone.
            self._sel_figs = [fig_hit] if fig_hit is not None else []
            self._sel_shapes = []
            self._sel_texts = []
            self._sel_labels = []
            mode: Optional[str] = part
        else:
            (self._sel_figs, self._sel_shapes, self._sel_texts,
             self._sel_labels, mode) = update_selection(
                self._sel_figs, self._sel_shapes, self._sel_texts,
                self._sel_labels, kind, index, additive)
            self._last_kind = kind
            # An object that belongs to a group brings the whole group
            # along: a group is one thing, as far as the cursor is
            # concerned.
            if mode == "body" and index is not None:
                members = self._expand_to_group("figure", index)
                if additive:
                    # Ctrl+click adds the group, not just the one object.
                    for m in members:
                        if m not in self._sel_figs:
                            self._sel_figs.append(m)
                else:
                    self._sel_figs = members
            elif mode == "shape-body" and index is not None:
                members = self._expand_to_group("shape", index)
                if additive:
                    for m in members:
                        if m not in self._sel_shapes:
                            self._sel_shapes.append(m)
                else:
                    self._sel_shapes = members
            elif mode == "text-body" and index is not None:
                members = self._expand_to_group("text", index)
                if additive:
                    for m in members:
                        if m not in self._sel_texts:
                            self._sel_texts.append(m)
                else:
                    self._sel_texts = members

        self._drag_mode = mode
        self._drag_start = (event.x, event.y)
        self._drag_rect = None
        self._drag_shape = None
        # The sheet as it was before the drag, so the whole drag is one step
        # to undo.
        self._remember()
        # Remember where every selected object was, so the whole group moves
        # by one offset rather than by one offset per motion event.
        self._drag_rects = {
            i: fitz.Rect(self.composition.placements[i].rect)
            for i in self._sel_figs
        }
        self._drag_shapes = {
            i: dataclasses.replace(self.composition.shapes[i])
            for i in self._sel_shapes
        }
        self._drag_texts = {
            i: dataclasses.replace(self.composition.texts[i])
            for i in self._sel_texts
        }
        if mode == "label" and self._selected_label is not None:
            placement = self.composition.placements[self._selected_label]
            anchor = placement.label_anchor()
            ux, uy = self._to_user(event.x, event.y)
            self._drag_label_grab = (ux - anchor.x, uy - anchor.y)
        self._sync_properties()
        self._redraw()

    def _shape_width(self) -> float:
        try:
            return max(0.2, min(12.0, float(self.var_shape_width.get())))
        except ValueError:
            return comp.SHAPE_WIDTH_DEFAULT

    def _drag_sheet(self, event: tk.Event) -> None:
        """Resize the sheet by dragging one of its grips.

        The edge that was grabbed follows the mouse and the opposite one
        stays where it was.  The canvas counts downwards and the sheet
        counts upwards, so the movement is taken in the sheet's own frame:
        pulling the top edge up makes the sheet taller.
        """
        handle = self._drag_sheet_handle
        start_w, start_h = self._drag_sheet_size
        start_x, start_y = self._drag_sheet_origin
        ux, uy = self._to_user(event.x, event.y)
        sx, sy = self._to_user(*self._drag_start)
        dx = ux - sx
        dy = uy - sy
        # The grips: 0 top-left, 1 top-middle, 2 top-right, 3 right-middle,
        # 4 bottom-right, 5 bottom-middle, 6 bottom-left, 7 left-middle.
        new_x, new_y = start_x, start_y
        new_w, new_h = start_w, start_h
        if handle in (0, 6, 7):
            # The left edge follows the mouse; the right one stays.
            new_x = start_x + dx
            new_w = start_w - dx
        elif handle in (2, 3):
            new_w = start_w + dx
        if handle in (0, 1, 2):
            # The top edge follows the mouse; the bottom one stays.
            new_h = start_h + dy
            new_y = start_y
        elif handle in (4, 5, 6):
            # The bottom edge follows the mouse; the top one stays.
            new_y = start_y + dy
            new_h = start_h - dy
        new_w = max(MIN_SHEET_PT, new_w)
        new_h = max(MIN_SHEET_PT, new_h)
        self.sheet_x, self.sheet_y = new_x, new_y
        self.sheet_w, self.sheet_h = new_w, new_h
        self.composition.width = self.sheet_w
        self.composition.height = self.sheet_h
        self._redraw()

    def _snap_offset(self, rect: fitz.Rect, dx: float, dy: float,
                     others: Sequence[fitz.Rect], state: int = 0
                     ) -> Tuple[float, float]:
        """Nudge a dragged object so that it sits tight against a neighbour.

        The edges are pulled to the edges of the objects around: left to
        left, right to right, and so on.  The middle edges count too, so a
        figure can be lined up with the middle of another one.

        Holding Alt switches the snapping off, for the times when the
        object should land exactly where it was dropped.
        """
        if not others or state & (0x0008 | 0x0400):
            # Alt is held: the object lands exactly where it was dropped.
            # The state carries the modifier bit and the virtual key bit,
            # and which of them is set depends on the platform.
            return dx, dy
        best_x = best_y = None
        for other in others:
            for edge, target in ((rect.x0 + dx, other.x0),
                                 (rect.x0 + dx, other.x1),
                                 (rect.x1 + dx, other.x0),
                                 (rect.x1 + dx, other.x1),
                                 ((rect.x0 + rect.x1) / 2 + dx,
                                  (other.x0 + other.x1) / 2)):
                distance = abs(target - edge)
                if distance <= SNAP_PT and (best_x is None
                                            or distance < best_x[0]):
                    best_x = (distance, target - edge)
            for edge, target in ((rect.y0 + dy, other.y0),
                                 (rect.y0 + dy, other.y1),
                                 (rect.y1 + dy, other.y0),
                                 (rect.y1 + dy, other.y1),
                                 ((rect.y0 + rect.y1) / 2 + dy,
                                  (other.y0 + other.y1) / 2)):
                distance = abs(target - edge)
                if distance <= SNAP_PT and (best_y is None
                                            or distance < best_y[0]):
                    best_y = (distance, target - edge)
        if best_x is not None:
            dx += best_x[1]
        if best_y is not None:
            dy += best_y[1]
        return dx, dy

    def _other_boxes(self, exclude_figs: Sequence[int],
                     exclude_shapes: Sequence[int],
                     exclude_texts: Sequence[int]) -> List[fitz.Rect]:
        """The boxes of everything that is not being dragged."""
        out: List[fitz.Rect] = []
        for i, placement in enumerate(self.composition.placements):
            if i in exclude_figs:
                continue
            out.append(fitz.Rect(placement.rect))
        for i, shape in enumerate(self.composition.shapes):
            if i in exclude_shapes:
                continue
            out.append(fitz.Rect(shape.rect))
        for i, item in enumerate(self.composition.texts):
            if i in exclude_texts:
                continue
            out.append(fitz.Rect(item.rect()))
        return out

    def _drag_offset(self, event: tk.Event) -> Optional[Tuple[float, float]]:
        """How far the object has been dragged, in the sheet's own units.

        The first few pixels are the wobble of the hand: a click comes with
        a pixel or two of travel, and at the sheet's zoom that is a visible
        nudge -- enough to break a layout the user has just arranged.  So
        nothing moves until the cursor has really travelled; a drag then
        goes the whole way, exactly as the cursor does.
        """
        if self._drag_start is None:
            return None
        dx_px = event.x - self._drag_start[0]
        dy_px = event.y - self._drag_start[1]
        if (abs(dx_px) < CLICK_SLOP_PX
                and abs(dy_px) < CLICK_SLOP_PX):
            return None
        ux, uy = self._to_user(event.x, event.y)
        sx, sy = self._to_user(*self._drag_start)
        return ux - sx, uy - sy

    def _on_drag(self, event: tk.Event) -> None:
        if self._drag_mode is None:
            return
        if self._drag_mode == "sheet":
            self._drag_sheet(event)
            return
        if self._drag_mode == "shape":
            ux, uy = self._to_user(event.x, event.y)
            ux, uy = self._clamp_to_sheet_point(ux, uy)
            if self._shape_preview is not None:
                self._shape_preview.end = fitz.Point(ux, uy)
                self._redraw()
            return
        if self._drag_mode == "shape-body":
            offset = self._drag_offset(event)
            if offset is None:
                return
            dx, dy = offset
            if self._drag_shapes:
                first = next(iter(self._drag_shapes.values()))
                others = self._other_boxes([], list(self._drag_shapes),
                                          list(self._drag_texts))
                dx, dy = self._snap_offset(fitz.Rect(first.rect), dx, dy,
                                           others, event.state)
            for i, base in self._drag_shapes.items():
                self.composition.shapes[i] = dataclasses.replace(
                    base,
                    start=fitz.Point(base.start.x + dx, base.start.y + dy),
                    end=fitz.Point(base.end.x + dx, base.end.y + dy),
                )
            self._redraw()
            return
        if self._drag_mode == "text-body":
            offset = self._drag_offset(event)
            if offset is None:
                return
            dx, dy = offset
            if self._drag_texts:
                first = next(iter(self._drag_texts.values()))
                others = self._other_boxes(list(self._drag_rects),
                                          list(self._drag_shapes),
                                          list(self._drag_texts))
                dx, dy = self._snap_offset(fitz.Rect(first.rect()), dx, dy,
                                           others, event.state)
            for i, base in self._drag_texts.items():
                self.composition.texts[i] = dataclasses.replace(
                    base,
                    position=fitz.Point(base.position.x + dx,
                                        base.position.y + dy),
                )
            self._redraw()
            return
        if self._drag_mode == "label":
            # The label is dragged free: it gets an offset from the
            # figure's corner, so it travels with the figure afterwards.
            i = self._selected_label
            if i is None:
                return
            if self._drag_offset(event) is None:
                return
            ux, uy = self._to_user(event.x, event.y)
            anchor = fitz.Point(ux - self._drag_label_grab[0],
                                uy - self._drag_label_grab[1])
            placement = self.composition.placements[i]
            placement.set_label_anchor(anchor)
            self._redraw()
            return
        if not self._drag_rects:
            return
        if self._drag_mode == "body":
            offset = self._drag_offset(event)
            if offset is None:
                # Still a press: nothing moves yet.
                return
            dx, dy = offset
            # The figure moves by the *total* offset from where the press
            # happened -- adding the offset to the current rectangle would
            # make every motion event add to the last one.
            first = next(iter(self._drag_rects.values()))
            others = self._other_boxes(list(self._drag_rects),
                                      list(self._drag_shapes),
                                      list(self._drag_texts))
            dx, dy = self._snap_offset(fitz.Rect(first), dx, dy, others,
                                       event.state)
            for i, base in self._drag_rects.items():
                new = fitz.Rect(
                    base.x0 + dx, base.y0 + dy, base.x1 + dx, base.y1 + dy
                )
                self.composition.placements[i].rect = self._clamp_to_sheet(new)
        elif self._drag_mode.startswith("handle"):
            ux, uy = self._to_user(event.x, event.y)
            i = self._selected
            if i is None:
                return
            r = fitz.Rect(self._drag_rects[i])
            h = int(self._drag_mode[-1])
            # The corner opposite the handle stays put.
            corner = USER_CORNER(r, (h + 2) % 4)
            new = fitz.Rect(r)
            if h in (0, 3):
                new.x0 = ux
            else:
                new.x1 = ux
            if h in (0, 1):
                new.y1 = uy
            else:
                new.y0 = uy
            new = self._aspect_locked(new, corner)
            self.composition.placements[i].rect = self._clamp_to_sheet(new)
        self._redraw()

    def _aspect_locked(self, rect: fitz.Rect, anchor: Tuple[float, float]
                       ) -> fitz.Rect:
        """Resize keeping the figure's aspect ratio, pivoting on ``anchor``.

        ``anchor`` is the corner that stays put -- the one opposite the
        handle being dragged -- so the figure grows away from it instead of
        jumping.
        """
        p = self.composition.placements[self._selected]
        try:
            w, h = comp.figure_display_size(p.figure)
        except Exception:  # noqa: BLE001
            return rect
        aspect = w / h if h else 1.0
        new_w = abs(rect.x1 - rect.x0)
        if new_w <= 0:
            return rect
        # Drive the height from the width, so a wide drag widens rather than
        # shears the figure.
        target_h = new_w / aspect

        if anchor[0] <= (rect.x0 + rect.x1) / 2:
            x0, x1 = anchor[0], anchor[0] + new_w
        else:
            x1, x0 = anchor[0], anchor[0] - new_w
        if anchor[1] >= (rect.y0 + rect.y1) / 2:
            y1, y0 = anchor[1], anchor[1] - target_h
        else:
            y0, y1 = anchor[1], anchor[1] + target_h
        return fitz.Rect(x0, y0, x1, y1)

    def _clamp_to_sheet(self, rect: fitz.Rect) -> fitz.Rect:
        """A rectangle pulled back onto the sheet, without changing its size."""
        sheet = self._sheet_rect()
        w = rect.width
        h = rect.height
        x0 = min(max(rect.x0, sheet.x0), max(sheet.x0, sheet.x1 - w))
        y0 = min(max(rect.y0, sheet.y0), max(sheet.y0, sheet.y1 - h))
        return fitz.Rect(x0, y0, x0 + w, y0 + h)

    def _default_shape_size(self) -> Tuple[float, float]:
        """How big a shape is when it is placed with a click.

        A quarter of the sheet is big enough to see and small enough to
        place anywhere.
        """
        return (self.sheet_w * 0.25, self.sheet_h * 0.25)

    def _on_release(self, event: tk.Event) -> None:
        if self._drag_mode == "shape":
            ux, uy = self._to_user(event.x, event.y)
            ux, uy = self._clamp_to_sheet_point(ux, uy)
            preview = self._shape_preview
            self._shape_preview = None
            self._drag_mode = None
            self._drag_start = None
            if preview is None:
                self._redraw()
                return
            if (abs(ux - preview.start.x) > 1.0
                    or abs(uy - preview.start.y) > 1.0):
                # A drag: the shape is exactly what was dragged out.
                end = fitz.Point(ux, uy)
            else:
                # A click: the shape is placed with a sensible size, so a
                # single click is enough to put one on the sheet.
                w, h = self._default_shape_size()
                start = fitz.Point(ux - w / 2, uy - h / 2)
                end = fitz.Point(start.x + w, start.y + h)
                preview = dataclasses.replace(preview, start=start)
            # Keep the whole style of the preview -- the fill, the
            # opacities and the line style included.
            self.composition.shapes.append(dataclasses.replace(
                preview, end=end))
            self._clear_selection()
            self._selected_shape = len(self.composition.shapes) - 1
            self._last_kind = "shape"
            self._sync_properties()
            self._redraw()
            return
        self._drag_mode = None
        self._drag_start = None
        self._drag_rect = None
        self._drag_shape = None
        self._drag_rects = {}
        self._drag_shapes = {}
        self._update_count()

    def _edit_figure_region(self) -> None:
        """Choose the part of the source that becomes the figure.

        The figure gets a copy of the source with a new region, so the
        palette keeps the original and other placements of it are not
        changed behind the user's back.
        """
        i = self._selected
        if i is None:
            i = self._selected_label
        if i is None or not (0 <= i < len(self.composition.placements)):
            self._status("先选中一个图。")
            return
        placement = self.composition.placements[i]
        _CropDialog(self, placement.figure,
                    lambda region: self._apply_figure_region(i, region))

    def _apply_figure_region(self, index: int, region: fitz.Rect) -> None:
        """Give the figure a new region, as a copy of its own."""
        placement = self.composition.placements[index]
        placement.figure = dataclasses.replace(placement.figure, region=region)
        self._redraw()
        self._status("已改取图范围。")

    def _edit_figure_text(self) -> None:
        """Let the user change the text inside an imported figure.

        The figure is an embedded PDF page, so its text comes with it.  The
        lines are recognised, listed, and may be rewritten -- or taken away
        altogether.  Everything stays vector: the old glyphs are removed and
        the new ones written in their place.
        """
        i = self._selected
        if i is None:
            self._status("先选中一个图。")
            return
        placement = self.composition.placements[i]
        try:
            blocks = comp.figure_text_blocks(placement.figure)
        except Exception as exc:  # noqa: BLE001 -- shown to the user
            self._status(f"无法读取图中的文字：{exc}")
            return
        if not blocks:
            self._status("这个图里没有可编辑的文字（它多半是位图）。")
            return
        _TextEditDialog(self, blocks, placement.text_edits,
                        self._apply_text_edits)

    def _apply_text_edits(self, edits: Dict[int, str]) -> None:
        i = self._selected
        if i is None:
            return
        self.composition.placements[i].text_edits = dict(edits)
        self._redraw()
        n = len(edits)
        self._status(f"已修改图中的 {n} 处文字。" if n else "已恢复图中文字。")

    @_action
    def _group_selected(self) -> None:
        """Make the selection one object that travels together."""
        if not (self._sel_figs or self._sel_shapes or self._sel_texts):
            self._status("先选中要组合的元素。")
            return
        if len(self._sel_figs) + len(self._sel_shapes) + len(self._sel_texts) < 2:
            self._status("至少选中两个元素才能组合。")
            return
        # An object that is already in a group joins that group instead of
        # starting a new one.
        existing = None
        for group in self.composition.groups:
            if any(group.holds("figure", i) for i in self._sel_figs) or \
                    any(group.holds("shape", i) for i in self._sel_shapes) or \
                    any(group.holds("text", i) for i in self._sel_texts):
                existing = group
                break
        if existing is None:
            existing = comp.Group()
            self.composition.groups.append(existing)
        for i in self._sel_figs:
            if i not in existing.figs:
                existing.figs.append(i)
        for i in self._sel_shapes:
            if i not in existing.shapes:
                existing.shapes.append(i)
        for i in self._sel_texts:
            if i not in existing.texts:
                existing.texts.append(i)
        self._redraw()
        self._status("已组合：组内的元素会一起移动。")

    @_action
    def _ungroup_selected(self) -> None:
        """Let the objects of a group go their own ways again."""
        removed = 0
        for group in list(self.composition.groups):
            if (any(group.holds("figure", i) for i in self._sel_figs)
                    or any(group.holds("shape", i) for i in self._sel_shapes)
                    or any(group.holds("text", i) for i in self._sel_texts)):
                self.composition.groups.remove(group)
                removed += 1
        if not removed:
            self._status("选中的元素不在任何组里。")
            return
        self._redraw()
        self._status("已取消组合。")

    def _expand_to_group(self, kind: str, index: int) -> List[int]:
        """The members of the group an object belongs to, or just itself."""
        group = self.composition.group_of(kind, index)
        if group is None:
            return [index]
        return group.members(kind)

    def _on_undo_key(self, event: tk.Event) -> None:
        """Ctrl+Z: take back the last change."""
        self._undo_step()
        return "break"

    def _on_redo_key(self, event: tk.Event) -> None:
        """Ctrl+Y (or Ctrl+Shift+Z): do it again."""
        self._redo_step()
        return "break"

    def _on_key(self, event: tk.Event) -> None:
        key = event.keysym
        # Ctrl+G groups the selection, Ctrl+U lets it go.
        if (event.state & 0x0004) and key.lower() == "g":
            self._group_selected()
            return
        if (event.state & 0x0004) and key.lower() == "u":
            self._ungroup_selected()
            return
        if key in ("Delete", "BackSpace"):
            self._delete_selected()
            return
        if key == "Escape":
            self._clear_selection()
            self._sync_properties()
            self._redraw()
            return
        if key not in ("Left", "Right", "Up", "Down"):
            return
        step = 10.0 if event.state & 0x0001 else 1.0
        # The sheet is drawn with the user's y pointing up, so "up" on the
        # canvas is a *larger* user y -- the same direction the mouse drag
        # moves things.
        arrows = {"Left": (-1, 0), "Right": (1, 0), "Up": (0, 1), "Down": (0, -1)}
        dx, dy = arrows[key]
        moved = False
        for i in self._sel_shapes:
            shape = self.composition.shapes[i]
            self.composition.shapes[i] = dataclasses.replace(
                shape,
                start=fitz.Point(shape.start.x + dx * step,
                                 shape.start.y + dy * step),
                end=fitz.Point(shape.end.x + dx * step,
                               shape.end.y + dy * step),
            )
            moved = True
        for i in self._sel_figs:
            p = self.composition.placements[i]
            p.rect = self._clamp_to_sheet(fitz.Rect(
                p.rect.x0 + dx * step, p.rect.y0 + dy * step,
                p.rect.x1 + dx * step, p.rect.y1 + dy * step,
            ))
            moved = True
        for i in self._sel_texts:
            item = self.composition.texts[i]
            self.composition.texts[i] = dataclasses.replace(
                item,
                position=fitz.Point(item.position.x + dx * step,
                                    item.position.y + dy * step),
            )
            moved = True
        if moved:
            self._redraw()

    # -- properties ---------------------------------------------------------

    def _sync_properties(self) -> None:
        # While the panel is being filled in from the model, the widgets
        # fire their change handlers -- which would write the *old* panel
        # values back over the object being shown.  The flag stops that.
        self._syncing = True
        try:
            self._sync_properties_inner()
        finally:
            self._syncing = False

    def _sync_properties_inner(self) -> None:
        # A label may be picked on its own; then its figure's properties are
        # what the panel shows.
        label_index = self._selected_label
        if label_index is None:
            label_index = self._selected
        has_fig = (label_index is not None
                   and 0 <= label_index < len(self.composition.placements))
        has_shape = (self._selected_shape is not None
                     and 0 <= self._selected_shape
                     < len(self.composition.shapes))
        has_text = (self._selected_text is not None
                    and 0 <= self._selected_text
                    < len(self.composition.texts))

        # -- the figure's label
        self.ent_label.configure(state="normal" if has_fig else "disabled")
        self.cmb_label_font.configure(
            state="readonly" if has_fig else "disabled")
        self.spin_label_size.configure(
            state="normal" if has_fig else "disabled")
        self.btn_label_color.configure(
            state="normal" if has_fig else "disabled")
        for widget in (self.chk_label_bold, self.chk_label_italic,
                       self.chk_label_formula):
            widget.configure(state="normal" if has_fig else "disabled")
        self.spin_fig_rotation.configure(
            state="normal" if has_fig else "disabled")
        if has_fig:
            p = self.composition.placements[label_index]
            self.var_label.set(p.label)
            self.var_label_mode.set(p.label_mode)
            self.var_label_size.set(f"{p.label_size:g}")
            self.var_label_font.set(p.label_font or comp.LABEL_FONT_DEFAULT)
            self.var_label_bold.set(bool(p.label_bold))
            self.var_label_italic.set(bool(p.label_italic))
            self.var_label_formula.set(bool(p.label_formula))
            self.var_fig_rotation.set(f"{p.rotation:g}")
            self._set_swatch(self.btn_label_color, p.label_color)
        else:
            self.var_label.set("")

        # -- the free text
        self.txt_text.configure(state="normal" if has_text else "disabled")
        self.cmb_text_font.configure(
            state="readonly" if has_text else "disabled")
        self.spin_text_size.configure(
            state="normal" if has_text else "disabled")
        self.btn_text_color.configure(
            state="normal" if has_text else "disabled")
        for widget in (self.chk_text_bold, self.chk_text_italic,
                       self.chk_text_formula, self.chk_text_box):
            widget.configure(state="normal" if has_text else "disabled")
        if has_text:
            item = self.composition.texts[self._selected_text]
            self._set_text_widget(item.text)
            self.var_text_font.set(item.font or comp.LABEL_FONT_DEFAULT)
            self.var_text_size.set(f"{item.size:g}")
            self.var_text_color.set(rgb_to_hex(item.color))
            self.var_text_bold.set(bool(item.bold))
            self.var_text_italic.set(bool(item.italic))
            self.var_text_formula.set(bool(item.formula))
            self.var_text_box.set(bool(item.box))
            self.var_text_rotation.set(f"{item.rotation:g}")
            self._set_swatch(self.btn_text_color, item.color)
        else:
            self._set_text_widget("")

        # -- the graphic element
        self.spin_shape_width.configure(
            state="normal" if has_shape else "disabled")
        self.chk_shape_fill.configure(
            state="normal" if has_shape else "disabled")
        self.btn_shape_color.configure(
            state="normal" if has_shape else "disabled")
        self.btn_shape_fill_color.configure(
            state="normal" if has_shape else "disabled")
        self.cmb_shape_dash.configure(
            state="readonly" if has_shape else "disabled")
        self.spin_shape_opacity.configure(
            state="normal" if has_shape else "disabled")
        self.spin_shape_fill_opacity.configure(
            state="normal" if has_shape else "disabled")
        self.spin_shape_rotation.configure(
            state="normal" if has_shape else "disabled")
        self.ent_shape_text.configure(
            state="normal" if has_shape else "disabled")
        self.spin_shape_text_size.configure(
            state="normal" if has_shape else "disabled")
        if has_shape:
            shape = self.composition.shapes[self._selected_shape]
            self.var_shape_width.set(f"{shape.width:g}")
            self.var_shape_fill.set(bool(shape.fill))
            self.var_shape_color.set(rgb_to_hex(shape.color))
            self.var_shape_fill_color.set(rgb_to_hex(shape.fill_rgb))
            self.var_shape_dash.set(shape.dash)
            self.var_shape_opacity.set(f"{round(shape.stroke_opacity * 100):g}")
            self.var_shape_fill_opacity.set(
                f"{round(shape.fill_opacity * 100):g}")
            self.var_shape_rotation.set(f"{shape.rotation:g}")
            self.var_shape_text.set(shape.text)
            self.var_shape_text_size.set(f"{shape.text_size:g}")
            self._set_swatch(self.btn_shape_color, shape.color)
            self._set_swatch(self.btn_shape_fill_color, shape.fill_rgb)

        # -- which tab to show: the kind that was picked last
        if self._last_kind == "shape" and has_shape:
            self.nb.select(self.tab_shape)
        elif self._last_kind == "text" and has_text:
            self.nb.select(self.tab_text)
        elif has_fig:
            self.nb.select(self.tab_label)

    def _set_swatch(self, button: tk.Button, color) -> None:
        r, g, b = tuple(color)[:3]
        button.configure(
            background="#%02x%02x%02x" % (int(r * 255), int(g * 255),
                                          int(b * 255)))

    def _on_tool_changed(self, event=None) -> None:
        """The tool dropdown changed.

        The event is accepted because Tk hands one to a callback bound to
        ``<<ComboboxSelected>>``; without it the handler would raise and
        the tool would never change.
        """
        self.tool = TOOL_BY_NAME.get(self.var_tool.get(), "select")
        self.canvas.configure(cursor="crosshair" if self.tool != "select"
                              else "")
        self._status("已选择工具：{}".format(self.var_tool.get()))

    def _choose_font_file(self) -> None:
        """Use a font file from disk and embed it in the output."""
        path = filedialog.askopenfilename(
            title="选择字体文件",
            filetypes=[("字体文件", "*.ttf *.otf *.ttc"), ("所有文件", "*.*")],
        )
        if not path:
            return
        p = Path(path)
        self.var_label_font.set(p.name)
        self._font_file = str(p)
        self._on_label_changed()

    def _choose_label_color(self) -> None:
        rgb, hexv = colorchooser.askcolor(
            color=self.var_label_color.get(), title="标签颜色")
        if not hexv:
            return
        self.var_label_color.set(hexv)
        self._on_label_changed()

    def _choose_shape_color(self) -> None:
        rgb, hexv = colorchooser.askcolor(
            color=self.var_shape_color.get(), title="边框颜色")
        if not hexv:
            return
        self.var_shape_color.set(hexv)
        self._on_shape_changed()

    def _choose_shape_fill_color(self) -> None:
        rgb, hexv = colorchooser.askcolor(
            color=self.var_shape_fill_color.get(), title="填充颜色")
        if not hexv:
            return
        self.var_shape_fill_color.set(hexv)
        self._on_shape_changed()

    @_action
    def _on_shape_changed(self, event=None) -> None:
        """Apply the style panel to every selected graphic element.

        The event is accepted because Tk hands one to a callback bound to
        ``<<ComboboxSelected>>``.
        """
        if self._syncing:
            # The panel is being filled in from the model.
            return
        if not self._sel_shapes:
            return
        color = hex_to_rgb(self.var_shape_color.get())
        fill_color = hex_to_rgb(self.var_shape_fill_color.get())
        width = self._shape_width()
        fill = bool(self.var_shape_fill.get())
        dash = self.var_shape_dash.get()
        opacity = self._shape_opacity(self.var_shape_opacity.get())
        fill_opacity = self._shape_opacity(self.var_shape_fill_opacity.get())
        rotation = self._shape_rotation()
        text = self.var_shape_text.get()
        text_size = self._shape_text_size()
        for i in self._sel_shapes:
            if not 0 <= i < len(self.composition.shapes):
                continue
            shape = self.composition.shapes[i]
            shape.width = width
            shape.fill = fill
            shape.color = color
            shape.fill_color = fill_color
            shape.dash = dash
            shape.stroke_opacity = opacity
            shape.fill_opacity = fill_opacity
            shape.rotation = rotation
            shape.text = text
            shape.text_size = text_size
        self._set_swatch(self.btn_shape_color, color)
        self._set_swatch(self.btn_shape_fill_color, fill_color)
        self._redraw()

    def _shape_rotation(self) -> float:
        try:
            return float(self.var_shape_rotation.get()) % 360.0
        except ValueError:
            return 0.0

    def _shape_text_size(self) -> float:
        try:
            return max(4.0, min(48.0, float(self.var_shape_text_size.get())))
        except ValueError:
            return 9.0

    def _on_figure_changed(self) -> None:
        """Apply the figure's angle to every selected figure."""
        if not self._sel_figs:
            return
        try:
            rotation = float(self.var_fig_rotation.get()) % 360.0
        except ValueError:
            return
        for i in self._sel_figs:
            if 0 <= i < len(self.composition.placements):
                self.composition.placements[i].rotation = rotation
        self._redraw()

    def _duplicate_selected(self) -> None:
        """A copy of everything that is selected, a little to one side."""
        made = False
        for i in list(self._sel_figs):
            if not 0 <= i < len(self.composition.placements):
                continue
            p = self.composition.placements[i]
            self.composition.placements.append(dataclasses.replace(
                p, rect=fitz.Rect(p.rect.x0 + 10, p.rect.y0 - 10,
                                   p.rect.x1 + 10, p.rect.y1 - 10)))
            made = True
        for i in list(self._sel_shapes):
            if not 0 <= i < len(self.composition.shapes):
                continue
            shape = self.composition.shapes[i]
            self.composition.shapes.append(dataclasses.replace(
                shape,
                start=fitz.Point(shape.start.x + 10, shape.start.y - 10),
                end=fitz.Point(shape.end.x + 10, shape.end.y - 10)))
            made = True
        for i in list(self._sel_texts):
            if not 0 <= i < len(self.composition.texts):
                continue
            item = self.composition.texts[i]
            self.composition.texts.append(dataclasses.replace(
                item, position=fitz.Point(item.position.x + 10,
                                          item.position.y - 10)))
            made = True
        if not made:
            self._status("先选中要复制的元素。")
            return
        self._clear_selection()
        self._redraw()
        self._update_count()
        self._status("已复制所选元素。")

    def _flip_selected(self, axis: str) -> None:
        """Mirror the selected graphic elements.

        A figure is an embedded PDF page, and mirroring one would mean
        rasterising it -- which would give up the vector output the whole
        tool is about.  So figures are left alone; the elements, whose
        geometry is ours to turn, are mirrored about their own middle.
        """
        flipped = 0
        for i in self._sel_shapes:
            if not 0 <= i < len(self.composition.shapes):
                continue
            shape = self.composition.shapes[i]
            box = shape.rect
            cx, cy = (box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2
            start, end = shape.start, shape.end
            if axis == "x":
                start = fitz.Point(2 * cx - start.x, start.y)
                end = fitz.Point(2 * cx - end.x, end.y)
            else:
                start = fitz.Point(start.x, 2 * cy - start.y)
                end = fitz.Point(end.x, 2 * cy - end.y)
            self.composition.shapes[i] = dataclasses.replace(
                shape, start=start, end=end)
            flipped += 1
        if not flipped:
            if self._sel_figs:
                self._status("图是嵌入的 PDF 片段，镜像会破坏矢量，故不支持。")
            else:
                self._status("先选中要翻转的图形。")
            return
        self._redraw()
        self._status("已水平翻转。" if axis == "x" else "已垂直翻转。")

    @staticmethod
    def _shape_opacity(value: str) -> float:
        """A percentage from the spinbox as a number in 0..1."""
        try:
            return min(1.0, max(0.0, float(value) / 100.0))
        except ValueError:
            return 1.0

    # -- alignment ---------------------------------------------------------

    def _selected_boxes(self) -> List[Tuple[str, int, fitz.Rect]]:
        """The selected objects, with the box each one occupies.

        Every kind of object -- figures, graphic elements, free text and the
        labels -- has a box, so the tools that work on boxes can work on all
        of them.
        """
        items: List[Tuple[str, int, fitz.Rect]] = []
        for i in self._sel_figs:
            if 0 <= i < len(self.composition.placements):
                items.append(("figure", i,
                              fitz.Rect(self.composition.placements[i].rect)))
        for i in self._sel_shapes:
            if 0 <= i < len(self.composition.shapes):
                items.append(("shape", i,
                              fitz.Rect(self.composition.shapes[i].rect)))
        for i in self._sel_texts:
            if 0 <= i < len(self.composition.texts):
                items.append(("text", i,
                              fitz.Rect(self.composition.texts[i].rect())))
        for i in self._sel_labels:
            if 0 <= i < len(self.composition.placements):
                placement = self.composition.placements[i]
                if placement.label:
                    items.append(("label", i, comp.free_label_rect(placement)))
        return items

    def _set_box(self, kind: str, index: int, rect: fitz.Rect,
                 resize: bool = False) -> None:
        """Put an object where its box says it is.

        With ``resize`` the object also takes the box's size; without it the
        object keeps its size and only moves -- which is what lining up
        means.
        """
        if kind == "figure":
            self.composition.placements[index].rect = self._clamp_to_sheet(rect)
        elif kind == "label":
            # A label is moved by its anchor, which is the baseline it is
            # written on.
            placement = self.composition.placements[index]
            if resize:
                size = placement.label_size
                width = comp.text_width(placement.label, "tiro", size)
                if width > 0:
                    placement.label_size = max(
                        4.0, min(48.0, size * rect.width / width))
            placement.set_label_anchor(fitz.Point(rect.x0, rect.y0))
        elif kind == "shape":
            shape = self.composition.shapes[index]
            if resize:
                # The shape keeps the direction it was drawn in: an arrow
                # that was drawn to the left still points to the left.
                if shape.end.x >= shape.start.x:
                    x0, x1 = rect.x0, rect.x1
                else:
                    x0, x1 = rect.x1, rect.x0
                if shape.end.y >= shape.start.y:
                    y0, y1 = rect.y0, rect.y1
                else:
                    y0, y1 = rect.y1, rect.y0
                self.composition.shapes[index] = dataclasses.replace(
                    shape,
                    start=fitz.Point(x0, y0), end=fitz.Point(x1, y1),
                )
            else:
                dx = rect.x0 - shape.rect.x0
                dy = rect.y0 - shape.rect.y0
                self.composition.shapes[index] = dataclasses.replace(
                    shape,
                    start=fitz.Point(shape.start.x + dx, shape.start.y + dy),
                    end=fitz.Point(shape.end.x + dx, shape.end.y + dy),
                )
        else:
            item = self.composition.texts[index]
            if resize:
                width, _ = item.text_size()
                if width > 0:
                    item.size = max(1.0, item.size * rect.width / width)
            item.position = fitz.Point(rect.x0, rect.y0)

    @_action
    def _match_size(self, how: str) -> None:
        """Make the first selected object the size of the second.

        "width" and "height" keep the object's own proportions; "both" makes
        it exactly the other's size, which may stretch it.
        """
        items = self._selected_boxes()
        if len(items) < 2:
            self._status("选中两个元素：先选的要改，后选的是参照。")
            return
        (kind_a, index_a, box_a), (kind_b, index_b, box_b) = items[0], items[1]
        source = box_b
        target = box_a
        if how == "width":
            scale = source.width / target.width if target.width else 1.0
            new = fitz.Rect(target.x0, target.y0,
                            target.x0 + source.width,
                            target.y0 + source.width * target.height / target.width)
        elif how == "height":
            new = fitz.Rect(target.x0, target.y0,
                            target.x0 + source.height * target.width / target.height,
                            target.y0 + source.height)
        else:
            new = fitz.Rect(target.x0, target.y0,
                            target.x0 + source.width,
                            target.y0 + source.height)
        self._set_box(kind_a, index_a, new, resize=True)
        self._redraw()
        name = {"width": "同宽", "height": "同高", "both": "等大"}[how]
        kind_name = {"figure": "图", "shape": "图形", "text": "文字",
                     "label": "标签"}[kind_a]
        self._status(f"已把{kind_name}改为与参照元素{name}。")

    def _array_dialog(self) -> None:
        """Ask how many rows and columns the array should have."""
        _ArrayDialog(self, len(self._sel_figs), self._apply_array)

    def _apply_array(self, columns: int, rows: int) -> None:
        """Lay the selected figures out in a grid of the given size."""
        items = self._sel_figs
        if not items:
            self._status("先选中要排成阵列的图。")
            return
        if columns < 1 or rows < 1:
            return
        needed = columns * rows
        if needed < len(items):
            self._status(f"{columns}×{rows} 放不下 {len(items)} 个图。")
            return
        cell_w = max(self.composition.placements[i].rect.width
                     for i in items)
        cell_h = max(self.composition.placements[i].rect.height
                     for i in items)
        gap = mm_to_pt(4.0)
        total_w = columns * cell_w + (columns - 1) * gap
        total_h = rows * cell_h + (rows - 1) * gap
        x0 = self.sheet_x + (self.sheet_w - total_w) / 2
        y0 = self.sheet_y + (self.sheet_h - total_h) / 2
        for n, i in enumerate(items):
            r, c = divmod(n, columns)
            p = self.composition.placements[i]
            p.rect = fitz.Rect(
                x0 + c * (cell_w + gap), y0 + total_h - (r + 1) * cell_h - r * gap,
                x0 + c * (cell_w + gap) + cell_w,
                y0 + total_h - r * cell_h - r * gap)
        self._redraw()
        self._status(f"已排成 {columns}×{rows} 的阵列。")

    @_action
    def _align(self, mode: str) -> None:
        """Line the selection up.

        With several objects selected they are lined up against each other;
        with a single one it is pushed against the edge of the sheet.
        """
        items = self._selected_boxes()
        if not items:
            self._status("先选中要对齐的元素（Ctrl+点击 可多选）。")
            return
        bounds = self._sheet_rect()
        target = comp.align_target([r for _, _, r in items], mode, bounds)
        for kind, index, rect in items:
            new = comp.aligned_rect(rect, mode, target)
            self._set_box(kind, index, new)
        self._redraw()
        names = {"left": "左对齐", "hcenter": "水平居中", "right": "右对齐",
                 "top": "顶对齐", "vcenter": "垂直居中", "bottom": "底对齐"}
        self._status(f"已{ names.get(mode, mode) } {len(items)} 个元素。")

    @_action
    def _on_label_changed(self, event=None) -> None:
        """Apply the label panel to the selected figure.

        The event is accepted because Tk hands one to a callback bound to
        ``<<ComboboxSelected>>``.
        """
        if self._syncing:
            # The panel is being filled in from the model: the widgets still
            # hold the values of the object that was shown before, and
            # writing them back would change the object being shown.
            return
        if self._selected is None or not (0 <= self._selected < len(self.composition.placements)):
            return
        p = self.composition.placements[self._selected]
        p.label = self.var_label.get()
        try:
            p.label_size = max(4.0, min(48.0, float(self.var_label_size.get())))
        except ValueError:
            pass
        font = self.var_label_font.get()
        p.label_font = "" if font == comp.LABEL_FONT_DEFAULT else font
        p.label_fontfile = getattr(self, "_font_file", "")
        p.label_color = hex_to_rgb(self.var_label_color.get())
        p.label_bold = bool(self.var_label_bold.get())
        p.label_italic = bool(self.var_label_italic.get())
        p.label_formula = bool(self.var_label_formula.get())
        self._set_swatch(self.btn_label_color, p.label_color)
        self._redraw()

    # -- the free text ------------------------------------------------------

    def _set_text_widget(self, text: str) -> None:
        """Show a text in the content box without firing the change handler."""
        if self.txt_text.get("1.0", "end-1c") == text:
            return
        self.txt_text.delete("1.0", "end")
        self.txt_text.insert("1.0", text)
        self.txt_text.edit_modified(False)

    @_action
    def _on_text_changed(self, event=None) -> None:
        """Apply the text tab to every selected piece of text."""
        if self._syncing:
            return
        if self._selected_text is None:
            return
        if event is not None and not self.txt_text.edit_modified():
            return
        self.txt_text.edit_modified(False)
        text = self.txt_text.get("1.0", "end-1c")
        try:
            size = max(4.0, min(48.0, float(self.var_text_size.get())))
        except ValueError:
            size = 10.0
        font = self.var_text_font.get()
        fontfile = getattr(self, "_text_font_file", "")
        color = hex_to_rgb(self.var_text_color.get())
        bold = bool(self.var_text_bold.get())
        italic = bool(self.var_text_italic.get())
        formula = bool(self.var_text_formula.get())
        box = bool(self.var_text_box.get())
        try:
            rotation = float(self.var_text_rotation.get()) % 360.0
        except ValueError:
            rotation = 0.0
        for i in self._sel_texts:
            if not 0 <= i < len(self.composition.texts):
                continue
            item = self.composition.texts[i]
            item.text = text
            item.size = size
            item.font = "" if font == comp.LABEL_FONT_DEFAULT else font
            item.fontfile = fontfile
            item.color = color
            item.bold = bold
            item.italic = italic
            item.formula = formula
            item.box = box
            item.rotation = rotation
        self._set_swatch(self.btn_text_color, color)
        self._redraw()

    def _choose_text_color(self) -> None:
        rgb, hexv = colorchooser.askcolor(
            color=self.var_text_color.get(), title="文字颜色")
        if not hexv:
            return
        self.var_text_color.set(hexv)
        self._on_text_changed()

    def _choose_text_font_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择字体文件",
            filetypes=[("字体文件", "*.ttf *.otf *.ttc"), ("所有文件", "*.*")],
        )
        if not path:
            return
        p = Path(path)
        self.var_text_font.set(p.name)
        self._text_font_file = str(p)
        self._on_text_changed()

    # -- distribution -------------------------------------------------------

    def _distribute(self, axis: str) -> None:
        """Spread the selection out evenly.

        The two outermost objects stay where they are; the ones in between
        are moved so that the gaps between them come out equal.
        """
        items: List[Tuple[str, int, fitz.Rect]] = []
        for i in self._sel_figs:
            if 0 <= i < len(self.composition.placements):
                items.append(("figure", i,
                              fitz.Rect(self.composition.placements[i].rect)))
        for i in self._sel_shapes:
            if 0 <= i < len(self.composition.shapes):
                items.append(("shape", i,
                              fitz.Rect(self.composition.shapes[i].rect)))
        for i in self._sel_texts:
            if 0 <= i < len(self.composition.texts):
                items.append(("text", i,
                              fitz.Rect(self.composition.texts[i].rect())))
        if len(items) < 3:
            self._status("至少选中 3 个元素才能分布。")
            return
        spread = comp.distribute_rects([r for _, _, r in items], axis)
        for (kind, index, rect), new in zip(items, spread):
            dx = new.x0 - rect.x0
            dy = new.y0 - rect.y0
            if kind == "figure":
                p = self.composition.placements[index]
                p.rect = self._clamp_to_sheet(fitz.Rect(
                    p.rect.x0 + dx, p.rect.y0 + dy,
                    p.rect.x1 + dx, p.rect.y1 + dy))
            elif kind == "shape":
                shape = self.composition.shapes[index]
                self.composition.shapes[index] = dataclasses.replace(
                    shape,
                    start=fitz.Point(shape.start.x + dx, shape.start.y + dy),
                    end=fitz.Point(shape.end.x + dx, shape.end.y + dy),
                )
            else:
                item = self.composition.texts[index]
                self.composition.texts[index] = dataclasses.replace(
                    item,
                    position=fitz.Point(item.position.x + dx,
                                        item.position.y + dy),
                )
        self._redraw()
        name = "水平" if axis == comp.DISTRIBUTE_HORIZONTAL else "垂直"
        self._status(f"已{name}等间距分布 {len(items)} 个元素。")

    def _on_label_mode_changed(self) -> None:
        # The label may be picked on its own, or the figure may be picked
        # and the label edited with it.
        index = self._selected_label
        if index is None:
            index = self._selected
        if index is None or not (0 <= index < len(self.composition.placements)):
            return
        self.composition.placements[index].label_mode = self.var_label_mode.get()
        self._redraw()

    @_action
    def _restack(self, direction: int) -> None:
        if self._selected is None:
            return
        i = self._selected
        j = i + direction
        if not 0 <= j < len(self.composition.placements):
            return
        items = self.composition.placements
        items[i], items[j] = items[j], items[i]
        # The selection follows the figure it was on.
        self._sel_figs = [j if k == i else (i if k == j else k)
                          for k in self._sel_figs]
        self._redraw()

    @_action
    def _delete_selected(self) -> None:
        """Remove everything that is selected."""
        if self._sel_texts:
            # Back to front, so the indices stay valid while deleting.
            for i in sorted(self._sel_texts, reverse=True):
                if 0 <= i < len(self.composition.texts):
                    del self.composition.texts[i]
                    self.composition.regroup_after_removal("text", i)
            self._sel_texts = []
            self._sync_properties()
            self._redraw()
            self._update_count()
            return
        if self._sel_shapes:
            # Back to front, so the indices stay valid while deleting.
            for i in sorted(self._sel_shapes, reverse=True):
                if 0 <= i < len(self.composition.shapes):
                    del self.composition.shapes[i]
                    self.composition.regroup_after_removal("shape", i)
            self._sel_shapes = []
            self._sync_properties()
            self._redraw()
            self._update_count()
            return
        if not self._sel_figs:
            return
        for i in sorted(self._sel_figs, reverse=True):
            if 0 <= i < len(self.composition.placements):
                del self.composition.placements[i]
                self.composition.regroup_after_removal("figure", i)
        self._sel_figs = []
        self._sync_properties()
        self._redraw()
        self._update_count()

    def _clear_sheet(self) -> None:
        if not (self.composition.placements or self.composition.shapes
                or self.composition.texts):
            return
        if not messagebox.askyesno("清空画布", "移除画布上的所有图？"):
            return
        self.composition.placements.clear()
        self.composition.shapes.clear()
        self.composition.texts.clear()
        self._clear_selection()
        self._sync_properties()
        self._redraw()
        self._update_count()

    # -- bulk operations ----------------------------------------------------

    @_action
    def _auto_label(self) -> None:
        style = "letter"
        for i, p in enumerate(self.composition.placements, start=1):
            p.label = comp.auto_label(i, style)
        self._sync_properties()
        self._redraw()
        self._status("已按顺序重新编号。")

    @_action
    def _arrange(self, mode: str) -> None:
        items = self.composition.placements
        if not items:
            return
        n = len(items)
        gap = mm_to_pt(4.0)
        if mode == "row":
            cols, rows = n, 1
        else:
            cols = int(n ** 0.5 + 0.999)
            rows = int((n + cols - 1) / cols)
        # Every cell gets the size of the largest figure in it, so the layout
        # is even instead of ragged.
        cell_w = max((p.rect.width for p in items), default=100.0)
        cell_h = max((p.rect.height for p in items), default=100.0)
        total_w = cols * cell_w + (cols - 1) * gap
        total_h = rows * cell_h + (rows - 1) * gap
        # A figure that was made bigger than the sheet must not push the
        # layout off it: the cells shrink until the whole arrangement fits.
        # Room is kept under the bottom row for the labels, which sit below
        # their figures by default.
        label_room = 0.0
        for p in items:
            if p.label and p.label_mode == comp.LABEL_BELOW:
                label_room = max(label_room, p.label_size * 1.6)
        scale = min(1.0,
                    self.sheet_w / total_w if total_w else 1.0,
                    (self.sheet_h - label_room) / total_h if total_h else 1.0)
        cell_w *= scale
        cell_h *= scale
        gap *= scale
        total_w = cols * cell_w + (cols - 1) * gap
        total_h = rows * cell_h + (rows - 1) * gap
        x0 = self.sheet_x + (self.sheet_w - total_w) / 2
        y0 = self.sheet_y + (self.sheet_h - label_room - total_h) / 2
        for i, p in enumerate(items):
            r, c = divmod(i, cols)
            # Rows are laid out from the top of the sheet downwards.
            top = self.sheet_y + self.sheet_h - (y0 + r * (cell_h + gap))
            p.rect = fitz.Rect(
                x0 + c * (cell_w + gap), top - cell_h,
                x0 + c * (cell_w + gap) + cell_w, top,
            )
        self._redraw()
        self._status(f"已{'横向' if mode == 'row' else '网格'}排列 {n} 个图。")

    # -- export -------------------------------------------------------------

    def _export(self) -> None:
        if not self.composition.placements:
            self._status("画布上还没有图。")
            return
        path = filedialog.asksaveasfilename(
            title="导出组合 PDF",
            defaultextension=".pdf",
            filetypes=[("PDF 文件", "*.pdf")],
            initialfile="composed.pdf",
        )
        if not path:
            return
        mode = self.var_sheet.get()
        try:
            if mode == "适应内容":
                comp.export_composition(self.composition, Path(path), fit_to_content=True)
            else:
                comp.export_composition(
                    self.composition, Path(path), fit_to_content=False,
                    page_size=(self.sheet_w, self.sheet_h),
                    offset=(self.sheet_x, self.sheet_y),
                )
        except Exception as exc:  # noqa: BLE001
            self._status(f"导出失败：{exc}")
            messagebox.showerror("导出失败", str(exc))
            return
        self._status(f"已导出：{path}")


class ComposeWindow(tk.Toplevel):
    """The compositor in a window of its own.

    The panel is what the main window embeds; this is the same thing for
    anyone who would rather have a second window.
    """

    def __init__(self, master: tk.Misc, cfg: Optional[CropConfig] = None) -> None:
        super().__init__(master)
        self.title("组合 — 把多张 PDF 的子图拼成一张大图")
        # The window is sized in physical pixels, so on a high-DPI display
        # the geometry has to be scaled to come out the same size on screen.
        scale = dpi.scale_factor(self)
        self.geometry(f"{int(1240 * scale)}x{int(820 * scale)}")
        self.minsize(int(1000 * scale), int(680 * scale))
        self.panel = ComposePanel(self, cfg)
        self.panel.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

    def __getattr__(self, name: str) -> Any:
        """Everything lives in the panel; forward to it.

        This keeps the window interchangeable with the panel -- the old
        code that reached for ``window.canvas`` still works.
        """
        if name == "panel":
            raise AttributeError(name)
        return getattr(self.panel, name)

    def __setattr__(self, name: str, value: Any) -> None:
        """Writes go to the panel too, or the window would be a half proxy.

        Setting ``window._selected`` has to reach the panel's selection, not
        shadow it with an attribute of the window's own.
        """
        if name == "panel" or "panel" not in self.__dict__:
            object.__setattr__(self, name, value)
        else:
            setattr(self.panel, name, value)


def open_compositor(master: tk.Misc, cfg: Optional[CropConfig] = None
                    ) -> ComposeWindow:
    """Open the compositor window and return it."""
    return ComposeWindow(master, cfg)


__all__ = ["ComposePanel", "ComposeWindow", "open_compositor",
           "SHEET_LANDSCAPE", "SHEET_PORTRAIT"]
