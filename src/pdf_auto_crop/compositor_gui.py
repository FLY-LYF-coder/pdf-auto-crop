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

import math
import sys
import tkinter as tk
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, ttk
from typing import Dict, List, Optional, Tuple

import pymupdf as fitz
from PIL import Image, ImageTk

from . import compositor as comp
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
                         zoom: float, sheet_h: float
                         ) -> Tuple[float, float, float, float]:
    """The canvas rectangle of a sheet rectangle: left, top, right, bottom.

    The canvas counts downwards while the sheet counts upwards, so the *top*
    of a placement comes from its top edge in user space -- getting that
    backwards puts the picture outside its own frame, and makes the hit test
    miss every click.
    """
    ox, oy = origin
    left = ox + rect.x0 * zoom
    right = ox + rect.x1 * zoom
    top = oy + (sheet_h - rect.y1) * zoom
    bottom = oy + (sheet_h - rect.y0) * zoom
    return (left, top, right, bottom)


#: How close the cursor has to be to grab a graphic element, in pixels.
SHAPE_HIT_PX = 6.0


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


def tk_font(label_font: str, size: int) -> Tuple[str, int, str]:
    """The Tk font tuple for a label font.

    ``label_font`` is a key of :data:`pdf_auto_crop.compositor.LABEL_FONTS`,
    or the name of a font file the user picked.
    """
    family = TK_FONTS.get(label_font, label_font or "Times New Roman")
    bold = "Bold" in label_font
    italic = "Italic" in label_font or "Oblique" in label_font
    if bold and italic:
        style = "bold italic"
    elif bold:
        style = "bold"
    elif italic:
        style = "italic"
    else:
        style = "normal"
    return (family, max(8, size), style)


def resolve_pick(shape_hit: Optional[int], figure_hit: Optional[int],
                 part: Optional[str]) -> Tuple[Optional[int], Optional[int],
                                              Optional[str]]:
    """What a press on the canvas selects, and what it starts dragging.

    The graphic elements are painted *on top of* the figures, so a hit on an
    element wins over a hit on a figure underneath it: a box drawn across a
    figure must still be grabbable.

    The drag mode has to come from the hit *kind* as well.  Picking an
    element and then leaving the mode empty leaves the element sitting still
    while the cursor drags it -- which looks exactly like a broken feature.

    Returns:
        ``(selected_figure, selected_shape, drag_mode)``.
    """
    if shape_hit is not None:
        return None, shape_hit, "shape-body"
    return figure_hit, None, part


class ComposeWindow(tk.Toplevel):
    """The compositor."""

    def __init__(self, master: tk.Misc, cfg: Optional[CropConfig] = None) -> None:
        super().__init__(master)
        self.title("组合 — 把多张 PDF 的子图拼成一张大图")
        self.geometry("1240x820")
        self.minsize(1000, 680)

        self.cfg = cfg or CropConfig()
        self.sheet_w, self.sheet_h = SHEET_LANDSCAPE
        self.composition = comp.Composition(width=self.sheet_w, height=self.sheet_h)

        #: Figures available to place, keyed by an incrementing id.
        self.palette: List[comp.Figure] = []
        self._thumb_cache: Dict[str, Image.Image] = {}
        self._photo_cache: Dict[str, ImageTk.PhotoImage] = {}

        self._selected: Optional[int] = None
        self._selected_shape: Optional[int] = None
        self._drag_mode: Optional[str] = None
        self._drag_start: Optional[Tuple[float, float]] = None
        self._drag_rect: Optional[fitz.Rect] = None
        self._drag_shape: Optional[comp.Shape] = None
        self._zoom = 1.0
        self._origin = (0.0, 0.0)

        #: The active tool: "select" moves things, the others draw.
        self.tool = "select"
        #: The shape being drawn, while the mouse is down.
        self._shape_preview: Optional[comp.Shape] = None
        #: A font file the user picked, embedded in the output.
        self._font_file: str = ""

        self._build_style()
        self._build_toolbar()
        self._build_body()
        self._build_footer()
        self._redraw()

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
        ttk.Button(bar, text="导入已裁剪的图…", command=self._import_cropped).grid(
            row=0, column=1, padx=6, sticky="w")
        ttk.Separator(bar, orient=tk.VERTICAL).grid(
            row=0, column=2, sticky="ns", padx=8)

        ttk.Label(bar, text="纸张").grid(row=0, column=3, padx=(0, 4), sticky="e")
        self.var_sheet = tk.StringVar(value="A4 横向")
        ttk.Combobox(
            bar, textvariable=self.var_sheet, state="readonly", width=10,
            values=["A4 横向", "A4 纵向", "适应内容"],
        ).grid(row=0, column=4, sticky="w")
        self.var_sheet.trace_add("write", lambda *_a: self._on_sheet_changed())

        ttk.Label(bar, text="边距 (mm)").grid(row=0, column=5, padx=(12, 4), sticky="e")
        self.var_margin = tk.StringVar(value="3")
        ttk.Entry(bar, textvariable=self.var_margin, width=6).grid(row=0, column=6, sticky="w")
        self.var_margin.trace_add("write", lambda *_a: self._on_margin_changed())

        ttk.Separator(bar, orient=tk.VERTICAL).grid(
            row=0, column=7, sticky="ns", padx=8)
        ttk.Button(bar, text="自动编号", command=self._auto_label).grid(
            row=0, column=8, padx=(0, 6), sticky="w")
        ttk.Button(bar, text="横向排列", command=lambda: self._arrange("row")).grid(
            row=0, column=9, padx=6, sticky="w")
        ttk.Button(bar, text="网格排列", command=lambda: self._arrange("grid")).grid(
            row=0, column=10, padx=6, sticky="w")

        ttk.Button(bar, text="导出 PDF…", command=self._export).grid(
            row=0, column=11, padx=12, sticky="e")

        # The drawing tools, on a second row of the toolbar.
        tools = ttk.Frame(bar)
        tools.grid(row=1, column=0, columnspan=12, sticky="w", pady=(6, 0))
        ttk.Label(tools, text="工具").pack(side=tk.LEFT, padx=(0, 6))
        self.var_tool = tk.StringVar(value="select")
        for text, value in (("选择/移动", "select"), ("箭头", comp.SHAPE_ARROW),
                            ("方框", comp.SHAPE_RECT), ("圆圈", comp.SHAPE_ELLIPSE),
                            ("直线", comp.SHAPE_LINE)):
            ttk.Radiobutton(
                tools, text=text, value=value, variable=self.var_tool,
                command=self._on_tool_changed,
            ).pack(side=tk.LEFT, padx=2)
        ttk.Separator(tools, orient=tk.VERTICAL).pack(side=tk.LEFT,
                                                       fill=tk.Y, padx=10)
        ttk.Label(
            tools, text="选中图形后可在右侧改颜色和线宽",
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
        ttk.Label(
            left,
            text="「导入 PDF」会自动检测子图；\n「导入已裁剪的图」直接取整页。",
            style="Hint.TLabel", wraplength=170, justify=tk.LEFT,
        ).pack(side=tk.TOP, fill=tk.X, pady=(6, 0))

        # -- sheet
        mid = ttk.Frame(body)
        mid.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=10)
        self.canvas = tk.Canvas(mid, background="#606060", highlightthickness=0)
        self.canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.canvas.bind("<Configure>", lambda _e: self._redraw())
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Delete>", self._on_key)
        self.canvas.bind("<BackSpace>", self._on_key)
        self.canvas.bind("<Escape>", self._on_key)
        for key in ("<Left>", "<Right>", "<Up>", "<Down>"):
            self.canvas.bind(key, self._on_key)
            self.canvas.bind(f"<Shift-{key[1:-1]}>", self._on_key)

        # -- properties
        right = ttk.Labelframe(body, text="属性", style="Section.TLabelframe",
                               padding=8)
        right.pack(side=tk.RIGHT, fill=tk.Y)
        right.columnconfigure(1, weight=1)

        ttk.Label(right, text="标签").grid(row=0, column=0, sticky="w", pady=3)
        self.var_label = tk.StringVar(value="")
        self.ent_label = ttk.Entry(right, textvariable=self.var_label)
        self.ent_label.grid(row=0, column=1, sticky="ew", pady=3)
        self.var_label.trace_add("write", lambda *_a: self._on_label_changed())

        ttk.Label(right, text="位置").grid(row=1, column=0, sticky="w", pady=3)
        self.var_label_mode = tk.StringVar(value=comp.LABEL_BELOW)
        box = ttk.Frame(right)
        box.grid(row=1, column=1, sticky="w", pady=3)
        ttk.Radiobutton(box, text="图下", value=comp.LABEL_BELOW,
                        variable=self.var_label_mode,
                        command=self._on_label_mode_changed()).pack(side=tk.LEFT)
        ttk.Radiobutton(box, text="角上", value=comp.LABEL_OVERLAY,
                        variable=self.var_label_mode,
                        command=self._on_label_mode_changed()).pack(side=tk.LEFT, padx=6)

        ttk.Label(right, text="字号").grid(row=2, column=0, sticky="w", pady=3)
        self.var_label_size = tk.StringVar(value="9")
        self.spin_label_size = ttk.Spinbox(
            right, from_=4, to=48, increment=1,
            textvariable=self.var_label_size, width=6)
        self.spin_label_size.grid(row=2, column=1, sticky="w", pady=3)
        self.var_label_size.trace_add("write", lambda *_a: self._on_label_changed())

        ttk.Label(right, text="字体").grid(row=3, column=0, sticky="w", pady=3)
        self.var_label_font = tk.StringVar(value=comp.LABEL_FONT_DEFAULT)
        self.cmb_label_font = ttk.Combobox(
            right, textvariable=self.var_label_font, state="readonly", width=14,
            values=list(comp.LABEL_FONTS),
        )
        self.cmb_label_font.grid(row=3, column=1, sticky="ew", pady=3)
        self.cmb_label_font.bind("<<ComboboxSelected>>", self._on_label_changed)
        ttk.Button(right, text="文件…", width=5, command=self._choose_font_file).grid(
            row=3, column=2, sticky="w", padx=(4, 0))

        ttk.Label(right, text="颜色").grid(row=4, column=0, sticky="w", pady=3)
        self.var_label_color = tk.StringVar(value="#000000")
        self.btn_label_color = tk.Button(
            right, width=6, background="#000000", relief=tk.RAISED,
            borderwidth=2, command=self._choose_label_color,
        )
        self.btn_label_color.grid(row=4, column=1, sticky="w", pady=3)
        ttk.Label(right, text="", style="Hint.TLabel").grid(
            row=4, column=2, sticky="w", padx=(4, 0))

        ttk.Separator(right, orient=tk.HORIZONTAL).grid(
            row=5, column=0, columnspan=3, sticky="ew", pady=8)

        ttk.Label(right, text="线宽").grid(row=6, column=0, sticky="w", pady=3)
        self.var_shape_width = tk.StringVar(value="1.5")
        self.spin_shape_width = ttk.Spinbox(
            right, from_=0.2, to=12, increment=0.5,
            textvariable=self.var_shape_width, width=6,
        )
        self.spin_shape_width.grid(row=6, column=1, sticky="w", pady=3)
        self.var_shape_width.trace_add("write", lambda *_a: self._on_shape_changed())

        ttk.Label(right, text="填充").grid(row=7, column=0, sticky="w", pady=3)
        self.var_shape_fill = tk.BooleanVar(value=False)
        self.chk_shape_fill = ttk.Checkbutton(
            right, text="半透明填充", variable=self.var_shape_fill,
            command=self._on_shape_changed,
        )
        self.chk_shape_fill.grid(row=7, column=1, columnspan=2, sticky="w", pady=3)

        ttk.Label(right, text="线色").grid(row=8, column=0, sticky="w", pady=3)
        self.var_shape_color = tk.StringVar(value="#d91a1a")
        self.btn_shape_color = tk.Button(
            right, width=6, background="#d91a1a", relief=tk.RAISED,
            borderwidth=2, command=self._choose_shape_color,
        )
        self.btn_shape_color.grid(row=8, column=1, sticky="w", pady=3)

        ttk.Separator(right, orient=tk.HORIZONTAL).grid(
            row=9, column=0, columnspan=3, sticky="ew", pady=8)

        ttk.Button(right, text="置于顶层", command=lambda: self._restack(+1)).grid(
            row=10, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Button(right, text="置于底层", command=lambda: self._restack(-1)).grid(
            row=11, column=0, columnspan=2, sticky="ew", pady=2)
        ttk.Button(right, text="删除选中", command=self._delete_selected).grid(
            row=12, column=0, columnspan=2, sticky="ew", pady=(8, 2))
        ttk.Button(right, text="清空画布", command=self._clear_sheet).grid(
            row=13, column=0, columnspan=2, sticky="ew", pady=2)

        ttk.Label(
            right,
            text="拖动图体移动\n拖动橙色角点缩放（保持比例）\n"
                 "方向键微调，Shift 加大步长\nDelete 删除\n"
                 "选好工具后在画布上拖动即可画箭头/方框/圆圈",
            style="Hint.TLabel", wraplength=170, justify=tk.LEFT,
        ).grid(row=14, column=0, columnspan=3, sticky="w", pady=(12, 0))

    def _build_footer(self) -> None:
        foot = ttk.Frame(self, padding=(10, 4, 10, 8))
        foot.pack(side=tk.BOTTOM, fill=tk.X)
        self.lbl_status = ttk.Label(foot, text="", style="Hint.TLabel")
        self.lbl_status.pack(side=tk.LEFT)
        self.lbl_count = ttk.Label(foot, text="", style="Hint.TLabel")
        self.lbl_count.pack(side=tk.RIGHT)

    # -- status ------------------------------------------------------------

    def _status(self, message: str) -> None:
        self.lbl_status.configure(text=message)

    def _update_count(self) -> None:
        n = len(self.composition.placements)
        m = len(self.composition.shapes)
        text = f"画布上 {n} 个图"
        if m:
            text += f" · {m} 个图形"
        if self.palette:
            text += f"  ·  素材 {len(self.palette)} 个"
        self.lbl_count.configure(text=text)

    # -- import ------------------------------------------------------------

    def _import_pdf(self) -> None:
        path = filedialog.askopenfilename(
            title="选择要取子图的 PDF",
            filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            found = comp.detect_figures(Path(path), self.cfg)
        except Exception as exc:  # noqa: BLE001 -- shown to the user
            self._status(f"读取失败：{exc}")
            return
        if not found:
            messagebox.showinfo(
                "没有检测到子图",
                "这份 PDF 里没有自动检测出图片。\n"
                "可以用「导入已裁剪的图」把整页作为一个图放进来。",
            )
            return
        for fig in found:
            self.palette.append(fig)
        self._refresh_palette()
        self._status(f"从 {Path(path).name} 导入 {len(found)} 个子图。")

    def _import_cropped(self) -> None:
        path = filedialog.askopenfilename(
            title="选择已裁剪好的图（整页作为一个图）",
            filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")],
        )
        if not path:
            return
        p = Path(path)
        try:
            doc = fitz.open(str(p))
        except Exception as exc:  # noqa: BLE001
            self._status(f"无法打开：{exc}")
            return
        try:
            visible = comp.source_visible_rect(doc, 0)
            # A region is measured relative to the visible rect's bottom-left,
            # so "the whole page" is the visible rect's own size.
            region = fitz.Rect(0, 0, visible.width, visible.height)
        finally:
            doc.close()
        self.palette.append(comp.Figure(path=p, page_index=0, region=region))
        self._refresh_palette()
        self._status(f"已导入 {p.name}（整页）。")

    def _refresh_palette(self) -> None:
        self.lst_palette.delete(0, tk.END)
        for fig in self.palette:
            size = self._figure_size(fig)
            self.lst_palette.insert(
                tk.END, f"{fig.name}  {size[0]:.0f}×{size[1]:.0f}pt"
            )

    def _figure_size(self, fig: comp.Figure) -> Tuple[float, float]:
        try:
            return comp.figure_display_size(fig)
        except Exception:  # noqa: BLE001
            return (0.0, 0.0)

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
        cx = self.sheet_w / 2
        cy = self.sheet_h / 2
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

    def _on_sheet_changed(self) -> None:
        mode = self.var_sheet.get()
        if mode == "A4 横向":
            self.sheet_w, self.sheet_h = SHEET_LANDSCAPE
        elif mode == "A4 纵向":
            self.sheet_w, self.sheet_h = SHEET_PORTRAIT
        else:
            bounds = self.composition.content_bounds()
            if bounds is not None:
                self.sheet_w = max(bounds.width, 10.0)
                self.sheet_h = max(bounds.height, 10.0)
        self.composition.width = self.sheet_w
        self.composition.height = self.sheet_h
        self._redraw()

    def _on_margin_changed(self) -> None:
        try:
            self.composition.margin_pt = mm_to_pt(float(self.var_margin.get()))
        except ValueError:
            return
        self._redraw()

    # -- coordinate mapping -------------------------------------------------

    def _to_canvas(self, x: float, y: float) -> Tuple[float, float]:
        """User space (bottom-up) -> canvas pixels (top-down)."""
        ox, oy = self._origin
        return (ox + x * self._zoom, oy + (self.sheet_h - y) * self._zoom)

    def _to_user(self, cx: float, cy: float) -> Tuple[float, float]:
        ox, oy = self._origin
        return ((cx - ox) / self._zoom, self.sheet_h - (cy - oy) / self._zoom)

    def _canvas_rect(self, rect: fitz.Rect) -> Tuple[float, float, float, float]:
        """The canvas rectangle of a placement: left, top, right, bottom."""
        return sheet_to_canvas_box(
            rect, self._origin, self._zoom, self.sheet_h)

    # -- drawing -----------------------------------------------------------

    def _redraw(self) -> None:
        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())
        self._zoom = min(cw / self.sheet_w, ch / self.sheet_h, 1.0)
        pw = max(1, self.sheet_w * self._zoom)
        ph = max(1, self.sheet_h * self._zoom)
        self._origin = (cw / 2 - pw / 2, ch / 2 - ph / 2)

        self.canvas.delete("all")
        self.canvas.create_rectangle(
            self._origin[0], self._origin[1],
            self._origin[0] + pw, self._origin[1] + ph,
            fill="white", outline="#303030", width=1, tags=("sheet",),
        )
        for idx, placement in enumerate(self.composition.placements):
            self._draw_placement(idx, placement)
        # The graphic elements sit on top of the figures, and the labels on
        # top of everything -- a label under an arrow is unreadable.
        for idx, shape in enumerate(self.composition.shapes):
            self._draw_shape(idx, shape)
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

    def _draw_placement(self, idx: int, placement: comp.Placement) -> None:
        left, top, right, bottom = self._canvas_rect(placement.rect)
        selected = idx == self._selected and self._selected_shape is None

        img = self._figure_image(placement.figure)
        if img is not None and right > left and bottom > top:
            tw, th = int(right - left), int(bottom - top)
            if tw > 0 and th > 0:
                scaled = img.resize((tw, th), Image.LANCZOS)
                photo = ImageTk.PhotoImage(scaled)
                self._photo_cache[placement.figure.key] = photo
                self.canvas.create_image(
                    left, top, image=photo, anchor=tk.NW, tags=("fig", str(idx))
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

        if selected:
            for hx, hy in HANDLE_POSITIONS(left, top, right, bottom):
                self.canvas.create_rectangle(
                    hx - HANDLE_PX / 2, hy - HANDLE_PX / 2,
                    hx + HANDLE_PX / 2, hy + HANDLE_PX / 2,
                    fill="#ff7f0e", outline="white", width=1, tags=("fig", str(idx)),
                )

    def _draw_label(self, idx: int, placement: comp.Placement) -> None:
        if not placement.label:
            return
        left, top, right, bottom = self._canvas_rect(placement.rect)
        family = placement.label_font or comp.LABEL_FONT_DEFAULT
        if placement.label_mode == comp.LABEL_OVERLAY:
            # In the corner of the figure itself.
            tx, ty = left + 4, bottom - 4
            anchor = tk.SW
        else:
            # Under the figure, which is the journal convention.
            tx, ty = (left + right) / 2, bottom + 4
            anchor = tk.N
        r, g, b = placement.label_color
        self.canvas.create_text(
            tx, ty, text=placement.label, fill="#%02x%02x%02x" % (
                int(r * 255), int(g * 255), int(b * 255)),
            font=tk_font(family, int(placement.label_size)),
            anchor=anchor, tags=("fig", str(idx)),
        )

    def _draw_shape(self, idx: int, shape: comp.Shape) -> None:
        """Draw a graphic element on the canvas."""
        x0, y0 = self._to_canvas(shape.start.x, shape.start.y)
        x1, y1 = self._to_canvas(shape.end.x, shape.end.y)
        r, g, b = shape.color
        colour = "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))
        width = max(1, int(round(shape.width * self._zoom)))
        selected = idx >= 0 and idx == self._selected_shape
        tag = str(idx) if idx >= 0 else "preview"

        if shape.kind in (comp.SHAPE_RECT, comp.SHAPE_ELLIPSE):
            left, top = min(x0, x1), min(y0, y1)
            right, bottom = max(x0, x1), max(y0, y1)
            if shape.kind == comp.SHAPE_RECT:
                item = self.canvas.create_rectangle(
                    left, top, right, bottom, outline=colour, width=width,
                    fill=colour if shape.fill else "",
                )
            else:
                item = self.canvas.create_oval(
                    left, top, right, bottom, outline=colour, width=width,
                    fill=colour if shape.fill else "",
                )
        else:
            item = self.canvas.create_line(
                x0, y0, x1, y1, fill=colour, width=width, arrow=tk.LAST,
                arrowshape=(8, 10, 4) if shape.kind == comp.SHAPE_ARROW else (),
            )
        self.canvas.addtag_withtag("shape", item)
        self.canvas.itemconfigure(item, tags=("shape", tag))

        if selected:
            for hx, hy in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
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
            if shape.kind in (comp.SHAPE_RECT, comp.SHAPE_ELLIPSE):
                left, top = min(x0, x1), min(y0, y1)
                right, bottom = max(x0, x1), max(y0, y1)
                if shape.kind == comp.SHAPE_ELLIPSE:
                    cxr, cyr = (left + right) / 2, (top + bottom) / 2
                    rx = max((right - left) / 2, 1e-6)
                    ry = max((bottom - top) / 2, 1e-6)
                    r = math.hypot((cx - cxr) / rx, (cy - cyr) / ry)
                    if abs(r - 1.0) * min(rx, ry) <= SHAPE_HIT_PX:
                        return i
                elif (top <= cy <= bottom
                        and min(abs(cx - left), abs(cx - right)) <= SHAPE_HIT_PX):
                    return i
                elif (left <= cx <= right
                      and min(abs(cy - top), abs(cy - bottom)) <= SHAPE_HIT_PX):
                    return i
            elif distance_to_segment(cx, cy, x0, y0, x1, y1) <= SHAPE_HIT_PX:
                return i
        return None

    def _on_press(self, event: tk.Event) -> None:
        self.canvas.focus_set()
        if self.tool != "select":
            # Start drawing a graphic element.
            ux, uy = self._to_user(event.x, event.y)
            self._shape_preview = comp.Shape(
                self.tool, fitz.Point(ux, uy), fitz.Point(ux, uy),
                color=hex_to_rgb(self.var_shape_color.get()),
                width=self._shape_width(),
            )
            self._drag_mode = "shape"
            self._drag_start = (event.x, event.y)
            self._redraw()
            return

        shape_idx = self._hit_shape(event.x, event.y)
        idx, part = self._hit(event.x, event.y)
        self._selected, self._selected_shape, self._drag_mode = resolve_pick(
            shape_idx, idx, part)
        self._drag_start = (event.x, event.y)
        self._drag_rect = None
        self._drag_shape = None
        if self._selected is not None:
            self._drag_rect = fitz.Rect(self.composition.placements[self._selected].rect)
        elif self._selected_shape is not None:
            self._drag_shape = comp.Shape(
                **vars(self.composition.shapes[self._selected_shape]))
        self._sync_properties()
        self._redraw()

    def _shape_width(self) -> float:
        try:
            return max(0.2, min(12.0, float(self.var_shape_width.get())))
        except ValueError:
            return comp.SHAPE_WIDTH_DEFAULT

    def _on_drag(self, event: tk.Event) -> None:
        if self._drag_mode is None or (self._selected is None
                                       and self._selected_shape is None
                                       and self._drag_mode != "shape"):
            return
        if self._drag_mode == "shape":
            ux, uy = self._to_user(event.x, event.y)
            if self._shape_preview is not None:
                self._shape_preview.end = fitz.Point(ux, uy)
                self._redraw()
            return
        if self._drag_mode == "shape-body" and self._selected_shape is not None:
            ux, uy = self._to_user(event.x, event.y)
            sx, sy = self._to_user(*self._drag_start)
            dx, dy = ux - sx, uy - sy
            base = self._drag_shape
            self.composition.shapes[self._selected_shape] = comp.Shape(
                base.kind,
                fitz.Point(base.start.x + dx, base.start.y + dy),
                fitz.Point(base.end.x + dx, base.end.y + dy),
                base.color, base.width, base.fill,
            )
            self._redraw()
            return
        if self._selected is None:
            return
        p = self.composition.placements[self._selected]
        if self._drag_mode == "body":
            ux, uy = self._to_user(event.x, event.y)
            sx, sy = self._to_user(*self._drag_start)
            dx, dy = ux - sx, uy - sy
            # The figure moves by the *total* offset from where the press
            # happened -- adding the offset to the current rectangle would
            # make every motion event add to the last one.
            base = fitz.Rect(self._drag_rect)
            new = fitz.Rect(
                base.x0 + dx, base.y0 + dy, base.x1 + dx, base.y1 + dy
            )
            new = self._clamp_to_sheet(new)
            p.rect = new
        elif self._drag_mode.startswith("handle"):
            ux, uy = self._to_user(event.x, event.y)
            r = fitz.Rect(self._drag_rect)
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
            p.rect = self._clamp_to_sheet(new)
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
        w = rect.width
        h = rect.height
        x0 = min(max(rect.x0, 0.0), max(self.sheet_w - w, 0.0))
        y0 = min(max(rect.y0, 0.0), max(self.sheet_h - h, 0.0))
        return fitz.Rect(x0, y0, x0 + w, y0 + h)

    def _on_release(self, event: tk.Event) -> None:
        if self._drag_mode == "shape":
            ux, uy = self._to_user(event.x, event.y)
            preview = self._shape_preview
            self._shape_preview = None
            self._drag_mode = None
            self._drag_start = None
            if preview is not None and (
                    abs(ux - preview.start.x) > 1.0
                    or abs(uy - preview.start.y) > 1.0):
                self.composition.shapes.append(comp.Shape(
                    preview.kind, preview.start, fitz.Point(ux, uy),
                    color=preview.color, width=preview.width,
                ))
                self._selected = None
                self._selected_shape = len(self.composition.shapes) - 1
                self._sync_properties()
            self._redraw()
            return
        self._drag_mode = None
        self._drag_start = None
        self._drag_rect = None
        self._drag_shape = None
        self._update_count()

    def _on_key(self, event: tk.Event) -> None:
        key = event.keysym
        if key in ("Delete", "BackSpace"):
            self._delete_selected()
            return
        if key == "Escape":
            self._selected = None
            self._selected_shape = None
            self._sync_properties()
            self._redraw()
            return
        step = 10.0 if event.state & 0x0001 else 1.0
        if self._selected_shape is not None:
            if key not in ("Left", "Right", "Up", "Down"):
                return
            arrows = {"Left": (-1, 0), "Right": (1, 0), "Up": (0, 1),
                      "Down": (0, -1)}
            dx, dy = arrows[key]
            shape = self.composition.shapes[self._selected_shape]
            self.composition.shapes[self._selected_shape] = comp.Shape(
                shape.kind,
                fitz.Point(shape.start.x + dx * step, shape.start.y + dy * step),
                fitz.Point(shape.end.x + dx * step, shape.end.y + dy * step),
                shape.color, shape.width, shape.fill,
            )
            self._redraw()
            return
        if self._selected is None:
            return
        if key not in ("Left", "Right", "Up", "Down"):
            return
        # The sheet is drawn with the user's y pointing up, so "up" on the
        # canvas is a *larger* user y -- the same direction the mouse drag
        # moves things.
        arrows = {"Left": (-1, 0), "Right": (1, 0), "Up": (0, 1), "Down": (0, -1)}
        dx, dy = arrows[key]
        p = self.composition.placements[self._selected]
        p.rect = self._clamp_to_sheet(fitz.Rect(
            p.rect.x0 + dx * step, p.rect.y0 + dy * step,
            p.rect.x1 + dx * step, p.rect.y1 + dy * step,
        ))
        self._redraw()

    # -- properties ---------------------------------------------------------

    def _sync_properties(self) -> None:
        has_fig = (self._selected is not None
                   and 0 <= self._selected < len(self.composition.placements))
        has_shape = (self._selected_shape is not None
                     and 0 <= self._selected_shape
                     < len(self.composition.shapes))

        # -- the figure's label
        self.ent_label.configure(state="normal" if has_fig else "disabled")
        self.cmb_label_font.configure(
            state="readonly" if has_fig else "disabled")
        self.spin_label_size.configure(
            state="normal" if has_fig else "disabled")
        self.btn_label_color.configure(
            state="normal" if has_fig else "disabled")
        if has_fig:
            p = self.composition.placements[self._selected]
            self.var_label.set(p.label)
            self.var_label_mode.set(p.label_mode)
            self.var_label_size.set(f"{p.label_size:g}")
            self.var_label_font.set(p.label_font or comp.LABEL_FONT_DEFAULT)
            self._set_swatch(self.btn_label_color, p.label_color)
        else:
            self.var_label.set("")

        # -- the graphic element
        self.spin_shape_width.configure(
            state="normal" if has_shape else "disabled")
        self.chk_shape_fill.configure(
            state="normal" if has_shape else "disabled")
        self.btn_shape_color.configure(
            state="normal" if has_shape else "disabled")
        if has_shape:
            shape = self.composition.shapes[self._selected_shape]
            self.var_shape_width.set(f"{shape.width:g}")
            self.var_shape_fill.set(bool(shape.fill))
            self.var_shape_color.set(rgb_to_hex(shape.color))
            self._set_swatch(self.btn_shape_color, shape.color)

    def _set_swatch(self, button: tk.Button, color) -> None:
        r, g, b = tuple(color)[:3]
        button.configure(
            background="#%02x%02x%02x" % (int(r * 255), int(g * 255),
                                          int(b * 255)))

    def _on_tool_changed(self) -> None:
        self.tool = self.var_tool.get()
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
            color=self.var_shape_color.get(), title="线条颜色")
        if not hexv:
            return
        self.var_shape_color.set(hexv)
        self._on_shape_changed()

    def _on_shape_changed(self) -> None:
        if self._selected_shape is None:
            return
        shape = self.composition.shapes[self._selected_shape]
        shape.width = self._shape_width()
        shape.fill = bool(self.var_shape_fill.get())
        shape.color = hex_to_rgb(self.var_shape_color.get())
        self._set_swatch(self.btn_shape_color, shape.color)
        self._redraw()

    def _on_label_changed(self) -> None:
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
        self._set_swatch(self.btn_label_color, p.label_color)
        self._redraw()

    def _on_label_mode_changed(self) -> None:
        if self._selected is None or not (0 <= self._selected < len(self.composition.placements)):
            return
        self.composition.placements[self._selected].label_mode = self.var_label_mode.get()
        self._redraw()

    def _restack(self, direction: int) -> None:
        if self._selected is None:
            return
        i = self._selected
        j = i + direction
        if not 0 <= j < len(self.composition.placements):
            return
        items = self.composition.placements
        items[i], items[j] = items[j], items[i]
        self._selected = j
        self._redraw()

    def _delete_selected(self) -> None:
        if self._selected_shape is not None and (
                0 <= self._selected_shape < len(self.composition.shapes)):
            del self.composition.shapes[self._selected_shape]
            self._selected_shape = None
            self._sync_properties()
            self._redraw()
            self._update_count()
            return
        if self._selected is None or not (0 <= self._selected < len(self.composition.placements)):
            return
        del self.composition.placements[self._selected]
        self._selected = None
        self._sync_properties()
        self._redraw()
        self._update_count()

    def _clear_sheet(self) -> None:
        if not self.composition.placements and not self.composition.shapes:
            return
        if not messagebox.askyesno("清空画布", "移除画布上的所有图？"):
            return
        self.composition.placements.clear()
        self.composition.shapes.clear()
        self._selected = None
        self._selected_shape = None
        self._sync_properties()
        self._redraw()
        self._update_count()

    # -- bulk operations ----------------------------------------------------

    def _auto_label(self) -> None:
        style = "letter"
        for i, p in enumerate(self.composition.placements, start=1):
            p.label = comp.auto_label(i, style)
        self._sync_properties()
        self._redraw()
        self._status("已按顺序重新编号。")

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
        x0 = (self.sheet_w - total_w) / 2
        y0 = (self.sheet_h - total_h) / 2
        for i, p in enumerate(items):
            r, c = divmod(i, cols)
            # Rows are laid out from the top of the sheet downwards.
            top = self.sheet_h - (y0 + r * (cell_h + gap))
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
                )
        except Exception as exc:  # noqa: BLE001
            self._status(f"导出失败：{exc}")
            messagebox.showerror("导出失败", str(exc))
            return
        self._status(f"已导出：{path}")


def open_compositor(master: tk.Misc, cfg: Optional[CropConfig] = None
                    ) -> ComposeWindow:
    """Open the compositor window and return it."""
    return ComposeWindow(master, cfg)


__all__ = ["ComposeWindow", "open_compositor", "SHEET_LANDSCAPE", "SHEET_PORTRAIT"]
