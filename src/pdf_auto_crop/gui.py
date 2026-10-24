"""Tkinter desktop front-end for pdf-auto-crop.

Design goals
------------
**Preview first.**  Nothing is written to disk until the user has looked at
the detection overlay and pressed *导出*.  The legacy wizard went straight
from "confirm settings" to writing files, so a wrong split was only
discoverable by opening every output PDF afterwards.

**Only live knobs.**  The parameter panel exposes exactly the settings the
layout-adaptive detector still reads.  ``merge_gap_mm`` and
``morph_kernel_multiplier`` are hidden rather than shown as dead controls --
see :mod:`pdf_auto_crop.gui_hints` for the full table.

**Actionable feedback.**  Every detection result is run through
:func:`~pdf_auto_crop.gui_hints.analyze_page`, which explains what happened and,
where a safe fix exists, offers it as a one-click action.  The hint engine is
suppressed on manual pages, since "raise min_area" cannot help rectangles the
user positioned by hand.

**Two ways in, per page.**  The detector is fast and right most of the time;
hand-drawn boxes are right exactly right.  Rather than make the user choose
one, the window offers both and applies them *per page*: a page the user never
touched is cropped by the detector, a page they drew on is cropped by their
rectangles.  On a forty-page paper you fix the two pages the algorithm got
wrong instead of redrawing all forty.  See :mod:`pdf_auto_crop.gui_edit` for the
store that keeps that split explicit, and for the geometry behind the handles.

**CLI untouched.**  Every widget here calls the same library entry points the
command line uses -- :func:`pdf_auto_crop.pipeline.run_pipeline` for export --
so the files this GUI produces are byte-for-byte what ``--input`` would have
produced, and the CLI tests keep their meaning.  Hand-drawn rectangles reach the
pipeline through a single additive keyword-only argument that defaults to
``None``; the command line never supplies it.

Launch with::

    python -m pdf_auto_crop.gui
"""

from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pymupdf as fitz

from . import dpi, gui_edit, gui_hints
from .config import CropConfig
from .geometry import add_margin_pt, mm_to_pt
from .pdf_processor import render_page_to_array
from .pipeline import get_detector, run_pipeline

try:  # tkinter is stdlib on Windows/macOS but may be missing on slim Linux.
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError as exc:  # pragma: no cover -- environment dependent
    raise ImportError(
        "tkinter is required for the graphical interface. "
        "On Debian/Ubuntu install it with: sudo apt install python3-tk"
    ) from exc

from PIL import Image, ImageTk

MM_PER_PT = 25.4 / 72.0

#: Box outline colour used on the preview overlay.  Red means "the detector
#: proposed this", which is a claim the user has not yet endorsed.
BOX_COLOUR = "#d62728"
#: Blue means "you drew this yourself" -- the boxes that will be exported as-is.
MANUAL_COLOUR = "#1f6feb"
#: The selected box and its handles.
BOX_ACTIVE_COLOUR = "#ff7f0e"
LABEL_BG = "#d62728"
LABEL_BG_MANUAL = "#1f6feb"
HANDLE_COLOUR = "#ff7f0e"

#: Font stack that renders CJK text on Windows.
UI_FONT = ("Microsoft YaHei UI", 10)
UI_FONT_BOLD = ("Microsoft YaHei UI", 10, "bold")
UI_FONT_TITLE = ("Microsoft YaHei UI", 14, "bold")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def page_area_mm2(page_size_pt: Tuple[float, float]) -> float:
    """Page area in square millimetres."""
    return abs(page_size_pt[0] * page_size_pt[1]) * MM_PER_PT * MM_PER_PT


def build_pipeline_args(
    src: Path,
    out_dir: Path,
    *,
    save_config: bool = False,
    preview: bool = False,
) -> argparse.Namespace:
    """Build the ``argparse.Namespace`` that :func:`run_pipeline` expects.

    The GUI has no argparse parser, so this mirrors the defaults the CLI would
    have supplied.  Keeping the construction here (rather than sprinkling
    ``SimpleNamespace`` calls) makes the contract with the pipeline explicit
    and gives the tests something concrete to assert against.
    """
    return argparse.Namespace(
        input=Path(src),
        output=Path(out_dir),
        preview=preview,
        preview_only=False,
        interactive=False,
        save_config=save_config,
        output_prefix=None,
        naming=None,
        margin=None,
        threshold=None,
        dpi=None,
        min_area=None,
        min_width=None,
        min_height=None,
        morph_kernel_multiplier=None,
        merge_gap_mm=None,
        padding_mm=None,
        preset=None,
        config=None,
    )


# ---------------------------------------------------------------------------
# Background worker plumbing
# ---------------------------------------------------------------------------


class _Task:
    """A unit of background work whose result comes back on the UI thread."""

    def __init__(self, fn: Callable[["App"], Any], on_done: Callable[[Any], None]):
        self.fn = fn
        self.on_done = on_done


class _Background:
    """Run one callable at a time on a worker thread, delivering via a queue.

    Detection and cropping take seconds on a 250 DPI page, which would freeze
    the window.  Every result travels back through :attr:`queue` and is drained
    by :meth:`pump` on the Tk main loop, so Tk is only ever touched from the
    thread that created it.
    """

    def __init__(self, app: "App") -> None:
        self._app = app
        self._queue: "queue.Queue[Tuple[str, Any, Any]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._busy = False
        self._on_done: Optional[Callable[[Any], None]] = None
        self._on_error: Optional[Callable[[Any, Any], None]] = None

    @property
    def busy(self) -> bool:
        return self._busy

    def submit(
        self,
        fn: Callable[["App"], Any],
        on_done: Callable[[Any], None],
        on_error: Optional[Callable[[Any, Any], None]] = None,
    ) -> None:
        if self._busy:
            return
        self._busy = True
        self._on_done = on_done
        self._on_error = on_error
        self._thread = threading.Thread(
            target=self._run, args=(fn,), daemon=True
        )
        self._thread.start()
        self._app.after(30, self.pump)

    def _run(self, fn: Callable[["App"], Any]) -> None:
        try:
            result = fn(self._app)
            self._queue.put(("ok", result, None))
        except Exception as exc:  # noqa: BLE001 -- surfaced in the UI
            self._queue.put(("err", exc, traceback.format_exc()))

    def pump(self) -> None:
        """Drain completed work; reschedule until the queue is empty."""
        finished = False
        while True:
            try:
                kind, payload, extra = self._queue.get_nowait()
            except queue.Empty:
                break
            finished = True
            if kind == "ok":
                self._busy = False
                self._on_done(payload)
            else:
                self._busy = False
                self._on_error(payload, extra)
        if not finished and self._busy:
            self._app.after(30, self.pump)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class App(tk.Tk):
    """The pdf-auto-crop desktop window."""

    def __init__(self) -> None:
        super().__init__()
        self.title("PDF Auto Crop  —  科研论文图片智能裁剪")
        # Tall enough that the right-hand column (parameters, feedback, export
        # button) plus the status footer all fit without the canvas stealing
        # the space; the footer packs at the bottom, so anything that overflows
        # pushes it off-screen entirely.
        # The window is sized in physical pixels, so on a high-DPI display the
        # geometry has to be scaled to come out the same size on screen.
        scale = dpi.scale_factor(self)
        self.geometry(f"{int(1280 * scale)}x{int(920 * scale)}")
        self.minsize(int(1000 * scale), int(700 * scale))

        self._doc: Optional[fitz.Document] = None
        self._src_path: Optional[Path] = None
        self._page_index = 0
        self._page_count = 0
        self._rects: List[fitz.Rect] = []
        self._page_size_pt: Tuple[float, float] = (595.0, 842.0)
        self._page_image: Optional[Image.Image] = None
        self._photo: Optional[ImageTk.PhotoImage] = None
        self._origin: Tuple[float, float] = (0.0, 0.0)
        self._box_items: List[int] = []
        self._zoom = 1.0
        self._bg = _Background(self)

        # -- manual editor state.  ``_manual`` is the per-page box store (the
        # single source of truth for what gets exported); ``_boxes`` mirrors
        # the current page for painting; ``_detected`` keeps the detector's
        # answer so a reset can restore it without re-running detection.
        self._manual = gui_edit.ManualBoxStore()
        self._boxes: List[fitz.Rect] = []
        self._detected: List[fitz.Rect] = []
        self._selected: Optional[int] = None
        self._drag_mode: Optional[str] = None
        self._drag_anchor: Optional[Tuple[float, float]] = None
        self._drag_index: Optional[int] = None
        self._drag_handle: Optional[int] = None
        # Rubber-band corner while a new box is being dragged out, in canvas
        # pixels.  The band itself lives on the canvas under the "marquee" tag
        # so it can be cleared without touching the page bitmap or the boxes.
        self._marquee_x = 0.0
        self._marquee_y = 0.0

        self._build_style()
        self._build_toolbar()
        self._build_body()
        self._build_footer()
        self._set_status("选择一个 PDF 文件或文件夹开始。", level=gui_hints.LEVEL_OK)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- styling ------------------------------------------------------------

    def _build_style(self) -> None:
        style = ttk.Style(self)
        for theme in ("vista", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        self.option_add("*Font", UI_FONT)
        style.configure("Title.TLabel", font=UI_FONT_TITLE)
        style.configure("Hint.TLabel", foreground="#444444")
        style.configure("Section.TLabelframe.Label", font=UI_FONT_BOLD)
        style.configure("Status.TLabel", font=UI_FONT)
        style.configure("Card.TFrame", relief="solid", borderwidth=1)
        style.configure("Export.TButton", font=("Microsoft YaHei UI", 11, "bold"))

    # -- layout -------------------------------------------------------------

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self, padding=(10, 8))
        bar.pack(side=tk.TOP, fill=tk.X)
        # Column 1 holds the output path, column 4 the input path; both stretch.
        bar.columnconfigure(1, weight=3)
        bar.columnconfigure(4, weight=2)

        self.var_input = tk.StringVar(value="—")
        self.var_output = tk.StringVar(value="./output")

        ttk.Button(bar, text="打开 PDF…", command=self._choose_pdf).grid(
            row=0, column=0, padx=(0, 6), sticky="w"
        )
        ttk.Button(bar, text="选择文件夹…", command=self._choose_folder).grid(
            row=0, column=2, padx=6, sticky="w"
        )
        ttk.Button(bar, text="组合…", command=self._open_compositor).grid(
            row=0, column=6, padx=(14, 0), sticky="w"
        )
        ttk.Label(bar, text="输入:").grid(row=0, column=3, padx=(10, 4), sticky="e")
        # An explicit width caps each entry's *natural* size.  Without it a
        # long path sets that width and shoves the "分析" button off the right
        # edge of the toolbar; the column weights still stretch them to fill.
        ttk.Entry(bar, textvariable=self.var_input, state="readonly", width=32).grid(
            row=0, column=4, columnspan=2, sticky="ew", padx=(0, 6)
        )

        ttk.Label(bar, text="输出:").grid(row=1, column=0, sticky="e", pady=(6, 0))
        ttk.Entry(bar, textvariable=self.var_output, width=32).grid(
            row=1, column=1, sticky="ew", pady=(6, 0), padx=(6, 6)
        )
        ttk.Button(bar, text="…", width=3, command=self._choose_output).grid(
            row=1, column=2, pady=(6, 0), sticky="w"
        )
        ttk.Button(bar, text="分析", command=self._analyse).grid(
            row=1, column=3, padx=(10, 0), pady=(6, 0), sticky="e"
        )
        self.bar = bar

    def _build_body(self) -> None:
        body = ttk.Frame(self, padding=(10, 0, 10, 0))
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        # -- preview canvas
        left = ttk.Frame(body)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # The editor bar sits directly above the canvas because that is where
        # the user's eyes already are when they decide to draw a box.
        editbar = ttk.Frame(left, padding=(0, 0, 0, 6))
        editbar.pack(side=tk.TOP, fill=tk.X)

        self.var_edit_mode = tk.StringVar(value="view")
        ttk.Radiobutton(
            editbar, text="查看", value="view", variable=self.var_edit_mode,
            command=self._on_edit_mode_changed,
        ).pack(side=tk.LEFT)
        ttk.Radiobutton(
            editbar, text="手动框选", value="edit", variable=self.var_edit_mode,
            command=self._on_edit_mode_changed,
        ).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Separator(editbar, orient=tk.VERTICAL).pack(
            side=tk.LEFT, fill=tk.Y, padx=8
        )

        self.btn_del_box = ttk.Button(
            editbar, text="删除选中框", command=self._delete_selected, width=12
        )
        self.btn_del_box.pack(side=tk.LEFT)
        self.btn_use_detected = ttk.Button(
            editbar, text="恢复自动检测", command=self._reset_page_to_detected, width=13
        )
        self.btn_use_detected.pack(side=tk.LEFT, padx=(6, 0))
        self.btn_clear_all = ttk.Button(
            editbar, text="清空所有手动修改", command=self._clear_all_manual, width=17
        )
        self.btn_clear_all.pack(side=tk.LEFT, padx=(6, 0))

        self.canvas = tk.Canvas(left, background="#5a5a5a", highlightthickness=0)
        self.canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.canvas.bind("<Configure>", lambda _e: self._redraw())
        self._bind_canvas_events()

        nav = ttk.Frame(left, padding=(0, 6))
        nav.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Button(nav, text="◀ 上一页", command=lambda: self._goto_page(self._page_index - 1)).pack(
            side=tk.LEFT
        )
        ttk.Button(nav, text="下一页 ▶", command=lambda: self._goto_page(self._page_index + 1)).pack(
            side=tk.LEFT, padx=6
        )
        self.lbl_page = ttk.Label(nav, text="第 - / - 页")
        self.lbl_page.pack(side=tk.LEFT, padx=10)
        self.lbl_page_mode = ttk.Label(nav, text="", style="Hint.TLabel")
        self.lbl_page_mode.pack(side=tk.LEFT)
        self.lbl_legend = ttk.Label(
            nav,
            text="",
            style="Hint.TLabel",
        )
        self.lbl_legend.pack(side=tk.RIGHT)

        # -- right column: parameters + feedback + actions
        right = ttk.Frame(body, padding=(10, 0, 0, 0))
        right.pack(side=tk.RIGHT, fill=tk.Y)
        right.columnconfigure(0, weight=1)

        params = ttk.LabelFrame(right, text="参数", style="Section.TLabelframe", padding=8)
        params.grid(row=0, column=0, sticky="ew")
        params.columnconfigure(1, weight=1)

        self.var_algorithm = tk.StringVar(value="smart")
        ttk.Label(params, text="算法").grid(row=0, column=0, sticky="w", pady=2)
        box = ttk.Frame(params)
        box.grid(row=0, column=1, sticky="w", pady=2)
        ttk.Radiobutton(
            box, text="智能（推荐）", value="smart", variable=self.var_algorithm
        ).pack(side=tk.LEFT)
        ttk.Radiobutton(
            box, text="传统", value="legacy", variable=self.var_algorithm
        ).pack(side=tk.LEFT, padx=(8, 0))

        self.var_naming = tk.StringVar(value="per-page")
        ttk.Label(params, text="命名方式").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Combobox(
            params, textvariable=self.var_naming, values=["per-page", "flat"],
            state="readonly", width=12,
        ).grid(row=1, column=1, sticky="w", pady=2)

        self.var_prefix = tk.StringVar(value="figure")
        ttk.Label(params, text="文件前缀").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Entry(params, textvariable=self.var_prefix, width=14).grid(
            row=2, column=1, sticky="w", pady=2
        )

        self._fields: Dict[str, tk.StringVar] = {}
        numeric = [
            ("margin_mm", "白边 (mm)", 3.0),
            ("padding_mm", "额外内边距 (mm)", 0.0),
            ("threshold", "二值化阈值", 245),
            ("dpi", "分析分辨率 DPI", 250),
            ("min_area_mm2", "最小面积 (mm²)", 25.0),
            ("min_width_mm", "最小宽度 (mm)", 10.0),
            ("min_height_mm", "最小高度 (mm)", 10.0),
        ]
        for i, (key, label, default) in enumerate(numeric, start=3):
            ttk.Label(params, text=label).grid(row=i, column=0, sticky="w", pady=2)
            var = tk.StringVar(value=f"{default:g}")
            self._fields[key] = var
            ttk.Entry(params, textvariable=var, width=14).grid(
                row=i, column=1, sticky="w", pady=2
            )

        ttk.Button(params, text="恢复默认", command=self._reset_params).grid(
            row=len(numeric) + 3, column=0, columnspan=2, sticky="ew", pady=(8, 0)
        )
        # The border only moves frames, never detection, so repaint on edit
        # rather than re-analysing.  Wired to the two fields that feed it.
        for key in ("margin_mm", "padding_mm"):
            self._fields[key].trace_add("write", self._on_margin_changed)
        ttk.Label(
            params,
            text="智能算法按每页实测\n"
                 "间距自动切分，无需\n"
                 "设置合并间距。\n\n"
                 "检测不准的页面可切到\n"
                 "「手动框选」自行拖框。\n\n"
                 "虚线 = 图片内容，\n"
                 "实线框 = 实际裁剪范围\n"
                 "（含白边）。手动框\n"
                 "按原样导出，不加白边。",
            style="Hint.TLabel",
            wraplength=180,
            justify=tk.LEFT,
        ).grid(row=len(numeric) + 4, column=0, columnspan=2, sticky="w", pady=(8, 0))

        # -- feedback card
        self.feedback = ttk.LabelFrame(
            right, text="检测结果", style="Section.TLabelframe", padding=8
        )
        self.feedback.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self.feedback.columnconfigure(0, weight=1)
        self.lbl_hint_title = ttk.Label(self.feedback, text="—", wraplength=250)
        self.lbl_hint_title.grid(row=0, column=0, sticky="w")
        self.lbl_hint_detail = ttk.Label(
            self.feedback, text="", style="Hint.TLabel", wraplength=250, justify=tk.LEFT
        )
        self.lbl_hint_detail.grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.btn_apply = ttk.Button(
            self.feedback, text="应用建议参数", command=self._apply_hint
        )
        self.btn_apply.grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.btn_apply.state(["disabled"])
        self._pending_config: Optional[CropConfig] = None

        # -- actions
        actions = ttk.Frame(right)
        actions.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self.btn_export = ttk.Button(
            actions, text="导出裁剪结果", style="Export.TButton", command=self._export
        )
        self.btn_export.pack(fill=tk.X)
        self.btn_open = ttk.Button(
            actions, text="打开输出文件夹", command=self._open_output
        )
        self.btn_open.pack(fill=tk.X, pady=(6, 0))
        self.progress = ttk.Progressbar(actions, mode="indeterminate")
        self.progress.pack(fill=tk.X, pady=(8, 0))
        self.lbl_progress = ttk.Label(actions, text="", style="Hint.TLabel")
        self.lbl_progress.pack(fill=tk.X)

    def _build_footer(self) -> None:
        foot = ttk.Frame(self, padding=(10, 4, 10, 8))
        foot.pack(side=tk.BOTTOM, fill=tk.X)
        self.lbl_status = ttk.Label(foot, text="", style="Status.TLabel")
        self.lbl_status.pack(side=tk.LEFT)
        self.lbl_figures = ttk.Label(foot, text="", style="Hint.TLabel")
        self.lbl_figures.pack(side=tk.RIGHT)

    # -- config plumbing ---------------------------------------------------

    def _reset_params(self) -> None:
        cfg = CropConfig()
        for key, var in self._fields.items():
            var.set(f"{getattr(cfg, key):g}")

    def current_config(self) -> CropConfig:
        """Assemble a :class:`CropConfig` from the parameter panel.

        Invalid or out-of-range entries fall back to the previous valid config
        so a half-typed number can never crash an analysis run.
        """
        base = getattr(self, "_last_config", None) or CropConfig()
        values: Dict[str, Any] = {
            "naming": self.var_naming.get(),
            "output_prefix": self.var_prefix.get() or "figure",
            "algorithm": self.var_algorithm.get(),
        }
        for key, var in self._fields.items():
            text = var.get().strip()
            try:
                value = float(text)
            except ValueError:
                continue
            values[key] = int(value) if key in ("threshold", "dpi") else value

        merged = {**base.to_dict(), **values}
        try:
            cfg = CropConfig.from_dict(merged)
            cfg.validate()
        except ValueError as exc:
            self._set_status(f"参数无效：{exc}", level=gui_hints.LEVEL_ERROR)
            return base
        self._last_config = cfg
        return cfg

    # -- source selection --------------------------------------------------

    def _choose_pdf(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 PDF",
            filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")],
        )
        if not path:
            return
        self._set_source(Path(path))
        suggested = Path(path).with_suffix("")
        self.var_output.set(str(suggested.parent / f"{suggested.name}_out"))
        self._analyse()

    def _choose_folder(self) -> None:
        folder = filedialog.askdirectory(title="选择包含 PDF 的文件夹")
        if not folder:
            return
        self._set_source(Path(folder))
        self.var_output.set(str(Path(folder) / "output"))
        self._set_status(
            "已选择文件夹。点「分析」查看第一页，或直接点「导出」批量处理。",
            level=gui_hints.LEVEL_OK,
        )

    def _open_compositor(self) -> None:
        """Open the figure compositor in its own window.

        Imported lazily: the compositor pulls in the compositing module, and
        a user who only ever crops should not pay for it.
        """
        try:
            from .compositor_gui import open_compositor
        except ImportError as exc:  # pragma: no cover -- environment dependent
            self._set_status(f"无法打开组合窗口：{exc}", level=gui_hints.LEVEL_ERROR)
            return
        try:
            # ``_last_config`` only exists once something has been analysed,
            # and the compositor must open on a fresh start too.
            cfg = getattr(self, "_last_config", None) or CropConfig()
            open_compositor(self, cfg)
        except Exception as exc:  # noqa: BLE001 -- reported in the status bar
            self._set_status(f"无法打开组合窗口：{exc}", level=gui_hints.LEVEL_ERROR)

    def _choose_output(self) -> None:
        folder = filedialog.askdirectory(title="选择输出文件夹")
        if folder:
            self.var_output.set(folder)

    def _set_source(self, path: Path) -> None:
        self._close_doc()
        self._src_path = path
        self._page_index = 0
        # Manual boxes are coordinates into a *specific* document; carrying
        # them onto a different file would crop the wrong artwork.
        self._manual.clear()
        self._boxes = []
        self._detected = []
        self._selected = None
        self.var_input.set(str(path))
        if path.is_file():
            try:
                self._doc = fitz.open(str(path))
                self._page_count = len(self._doc)
            except Exception as exc:
                self._doc = None
                self._page_count = 0
                self._set_status(f"无法打开 PDF：{exc}", level=gui_hints.LEVEL_ERROR)
                return
        else:
            pdfs = sorted(path.glob("*.pdf"))
            self._page_count = len(pdfs)
            if not pdfs:
                self._set_status(
                    "该文件夹里没有 PDF 文件。", level=gui_hints.LEVEL_WARN
                )
        self.lbl_page.configure(text=f"第 {min(1, self._page_count)} / {self._page_count} 页")
        self._update_page_labels()

    def _close_doc(self) -> None:
        if self._doc is not None:
            self._doc.close()
            self._doc = None

    # -- analysis ----------------------------------------------------------

    def _goto_page(self, index: int) -> None:
        if self._doc is None:
            return
        if not 0 <= index < self._page_count:
            return
        self._page_index = index
        self._selected = None
        self._analyse()


    def _analyse(self) -> None:
        if self._src_path is None or not self._src_path.exists():
            self._set_status("请先选择输入文件。", level=gui_hints.LEVEL_ERROR)
            return
        if self._doc is None:
            self._set_source(self._src_path)
        if self._doc is None:
            return

        cfg = self.current_config()
        self._set_busy(True, "正在分析…")
        self._bg.submit(
            lambda app: _analyse_page(self._doc, self._page_index, cfg),
            self._on_analysed,
            on_error=self._on_failed,
        )

    def _on_analysed(self, payload: Any) -> None:
        rgb, size_pt, rects = payload
        self._set_busy(False)
        self._render_page(rgb, size_pt, rects, cfg=self._last_config or CropConfig())

    def _on_failed(self, exc: BaseException, tb: Optional[str]) -> None:
        self._set_busy(False)
        self._set_status(f"分析失败：{exc}", level=gui_hints.LEVEL_ERROR)

    # -- rendering ---------------------------------------------------------

    def _render_page(
        self,
        rgb: np.ndarray,
        size_pt: Tuple[float, float],
        rects: Sequence[fitz.Rect],
        cfg: CropConfig,
    ) -> None:
        self._page_size_pt = size_pt
        self._detected = list(rects)

        # A page the user has taken over keeps *their* rectangles, even when a
        # fresh detection arrives (parameter change, DPI change, page revisit).
        # Re-running the detector must never silently discard hand-drawn crops.
        self._selected = None
        self._cancel_drag()
        self._load_page_boxes()

        photo_src = Image.fromarray(rgb)
        self._page_image = photo_src
        self._photo = ImageTk.PhotoImage(photo_src)
        self._redraw()
        self._sync_edit_state()

        if self._manual.is_manual(self._page_index):
            # The hint engine reasons about detector output; its advice ("raise
            # min_area") cannot help a page whose boxes are the user's own, so
            # reporting it here would send people tuning the wrong knob.
            self._show_edit_summary()
            return

        has_ink = bool((rgb < cfg.threshold).any())
        # The hint engine is given the *tight* detected rectangles, not the
        # border-expanded ones on screen.  Its advice is about detector
        # parameters (min_area, threshold), which operate on content bounds;
        # feeding it a 3 mm border would shift every size judgement and send
        # the user tuning against a number the detector never sees.
        analysis = gui_hints.analyze_page(
            figure_count=len(self._detected),
            has_ink=has_ink,
            page_area_mm2=page_area_mm2(size_pt),
            rects=self._detected,
            config=cfg,
        )
        self._pending_config = analysis.suggested_config(cfg)
        self._show_analysis(analysis)


    def _redraw(self) -> None:
        """Repaint the canvas at the current size (called on resize too)."""
        if self._page_image is None:
            return
        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())
        pw, ph = self._page_image.size
        factor = min(cw / pw, ch / ph, 1.0)
        w, h = max(1, int(pw * factor)), max(1, int(ph * factor))
        if (w, h) == (pw, ph):
            scaled = self._page_image
        else:
            scaled = self._page_image.resize((w, h), Image.LANCZOS)
        # Keep a reference alive: Tk only holds a weak claim on the image.
        self._photo = ImageTk.PhotoImage(scaled)
        self._zoom = w / pw if pw else 1.0
        # The page is centred in the canvas, so overlay coordinates have to be
        # shifted by the same offset -- otherwise the boxes drift away from the
        # artwork by half the leftover space.
        self._origin = (cw / 2 - w / 2, ch / 2 - h / 2)
        self.canvas.delete("all")
        self.canvas.create_image(
            cw / 2, ch / 2, image=self._photo, anchor=tk.CENTER, tags=("page",)
        )
        self._refresh_boxes()

    def _refresh_boxes(self) -> None:
        """Repaint only the overlay boxes, leaving the page bitmap alone.

        Dragging a box fires this on every mouse-move event; rebuilding the
        scaled :class:`ImageTk.PhotoImage` each time would re-resample a
        multi-megapixel page per event and make the drag crawl.
        """
        self.canvas.delete("box")
        self._box_items = []
        shown = self._display_boxes()
        for idx, rect in enumerate(shown, start=1):
            self._box_items.extend(self._draw_box(idx, rect))

    def _margin_pt(self) -> float:
        """White border to add around *detected* boxes, in PDF points.

        Read straight from the entry fields rather than through
        :meth:`current_config` so that a half-typed number simply contributes
        nothing instead of flagging the whole config as invalid mid-keystroke.
        """
        total = 0.0
        for key in ("margin_mm", "padding_mm"):
            var = self._fields.get(key)
            if var is None:
                continue
            try:
                total += float(var.get().strip())
            except ValueError:
                continue
        return mm_to_pt(total)

    def _display_boxes(self) -> List[fitz.Rect]:
        """The rectangles the canvas shows -- and therefore the ones exported.

        This is the single most important invariant in the window: **what you
        see is what you get**.  Detected boxes are drawn with the white border
        already applied, because that is the CropBox
        :func:`pdf_auto_crop.pipeline.run_pipeline` will write.  Drawing the
        raw detection instead would make every auto crop silently 3 mm larger
        than the frame around it -- the exact bug this method exists to
        prevent.

        Manual pages are returned verbatim.  The border is *not* re-applied to a
        hand-drawn rectangle: the user positioned that frame deliberately, and
        growing it afterwards would undo the whitespace they just trimmed.
        """
        if self._manual.is_manual(self._page_index):
            return list(self._boxes)
        margin = self._margin_pt()
        if margin <= 0:
            return list(self._detected)
        page = self._page_rect()
        out: List[fitz.Rect] = []
        for rect in self._detected:
            try:
                out.append(add_margin_pt(rect, margin, page))
            except ValueError:  # degenerate box: show it as detected
                out.append(rect)
        return out

    def _on_margin_changed(self, *_args: Any) -> None:
        """Repaint the overlay when the white border is edited.

        Detection is unaffected by the border, so there is no reason to re-run
        it -- only the frames move, and they move the instant the number
        changes.
        """
        if self._page_image is None:
            return
        self._refresh_boxes()
        self._update_figures_label()

    def _pdf_rect_to_canvas(self, rect: fitz.Rect) -> Tuple[float, float, float, float]:
        return gui_edit.pdf_rect_to_canvas(
            rect,
            dpi=(self._last_config or CropConfig()).dpi,
            page_height_pt=self._page_size_pt[1],
            zoom=self._zoom,
            origin=self._origin,
        )

    def _page_rect(self) -> fitz.Rect:
        """The current page's own rectangle, in PDF points."""
        return gui_edit.page_rect(self._page_size_pt)

    def _draw_box(self, idx: int, rect: fitz.Rect) -> List[int]:
        """Paint one box, its label, and -- when selected -- its four handles.

        Boxes live on a ``"box"`` tag so that :meth:`_refresh_boxes` can repaint
        them during a drag without rebuilding the page bitmap, which is the
        difference between a smooth drag and a stuttering one.
        """
        x0, y0, x1, y1 = self._pdf_rect_to_canvas(rect)
        # Colour reports what the *export* will do, not which toggle is set:
        # blue means these rectangles are the user's, red means the detector
        # still proposes them.
        manual = self._manual.is_manual(self._page_index)
        selected = self._selected == idx - 1
        outline = BOX_ACTIVE_COLOUR if selected else (MANUAL_COLOUR if manual else BOX_COLOUR)
        items: List[int] = []
        if not manual and self._margin_pt() > 0 and idx <= len(self._detected):
            # Ghost the raw detection inside the crop window, so the white
            # border the user is paying for is visible rather than implied.
            gx0, gy0, gx1, gy1 = self._pdf_rect_to_canvas(self._detected[idx - 1])
            if (gx1 - gx0) > 2 and (gy1 - gy0) > 2:
                items.append(
                    self.canvas.create_rectangle(
                        gx0, gy0, gx1, gy1, outline="#9aa0a6", width=1,
                        dash=(3, 3), tags=("box",),
                    )
                )
        items.append(
            self.canvas.create_rectangle(
                x0, y0, x1, y1, outline=outline, width=3, tags=("box",)
            )
        )
        if selected and self._editing():
            items.extend(self._draw_handles(rect))
        label = f"图 {idx}"
        bg = MANUAL_COLOUR if manual else LABEL_BG
        items.append(
            self.canvas.create_rectangle(
                x0, y0 - 20, x0 + 52, y0, fill=bg, outline="", tags=("box",)
            )
        )
        items.append(
            self.canvas.create_text(
                x0 + 26, y0 - 10, text=label, fill="white", font=UI_FONT_BOLD,
                tags=("box",),
            )
        )
        return items

    def _draw_handles(self, rect: fitz.Rect) -> List[int]:
        """Four corner squares marking the edges a drag can grab."""
        items: List[int] = []
        for hx, hy in gui_edit.handle_points(rect):
            x, y = self._pdf_to_canvas_point(hx, hy)
            items.append(
                self.canvas.create_rectangle(
                    x - gui_edit.EDGE_GRAB_PX / 2,
                    y - gui_edit.EDGE_GRAB_PX / 2,
                    x + gui_edit.EDGE_GRAB_PX / 2,
                    y + gui_edit.EDGE_GRAB_PX / 2,
                    fill=HANDLE_COLOUR, outline="white", width=1, tags=("box",),
                )
            )
        return items

    def _draw_marquee(self) -> None:
        """Repaint the rubber band while a new box is being dragged out."""
        self.canvas.delete("marquee")
        if self._drag_mode != "draw" or self._drag_anchor is None:
            return
        x0, y0, x1, y1 = gui_edit.normalise(
            self._drag_anchor[0], self._drag_anchor[1], self._marquee_x, self._marquee_y
        )
        self.canvas.create_rectangle(
            x0, y0, x1, y1, outline=MANUAL_COLOUR, width=2, dash=(4, 3),
            tags=("marquee",),
        )

    # -- manual editor ----------------------------------------------------
    #
    # Everything below is the "draw it yourself" path.  The invariant that keeps
    # it sane: rectangles are converted to PDF points the moment the mouse is
    # released and are never read back out of canvas space.  Resizing the
    # window, changing the analysis DPI or re-zooming therefore cannot move a
    # crop the user drew -- the canvas is only ever a view.

    def _bind_canvas_events(self) -> None:
        c = self.canvas
        c.bind("<Button-1>", self._on_canvas_press)
        c.bind("<B1-Motion>", self._on_canvas_drag)
        c.bind("<ButtonRelease-1>", self._on_canvas_release)
        c.bind("<Button-3>", self._on_canvas_right_click)
        c.bind("<Delete>", self._on_canvas_key)
        c.bind("<BackSpace>", self._on_canvas_key)
        c.bind("<Escape>", self._on_canvas_key)
        for key in ("<Left>", "<Right>", "<Up>", "<Down>"):
            c.bind(key, self._on_canvas_key)
            c.bind(f"<Shift-{key[1:-1]}>", self._on_canvas_key)

    def _editing(self) -> bool:
        """True when the user is in manual-box mode."""
        return self.var_edit_mode.get() == "edit"

    def _on_edit_mode_changed(self) -> None:
        """Switch between inspecting the detection and drawing over it."""
        if self._editing():
            self._manual.mark(self._page_index)
            if not self._manual.get(self._page_index):
                # Seed from what is *currently on screen* -- the detected boxes
                # with the white border already applied -- so that taking a
                # page over never silently resizes its crops.  The border stops
                # being re-applied from this moment on, which is exactly why
                # seeding from the raw detection would make every figure jump
                # outward by 3 mm at the instant the user switched modes.
                self._manual.set(self._page_index, self._display_boxes())
            self._set_status(
                "手动模式：在页面上按住左键拖动即可新建裁剪框；"
                "拖动框体移动，拖动橙色角点调整大小，右键或 Delete 删除。"
                "手动框按原样导出，不再叠加白边。",
                level=gui_hints.LEVEL_OK,
            )
        else:
            self._selected = None
        self._load_page_boxes()
        self._sync_edit_state()
        self._refresh_boxes()

    def _load_page_boxes(self) -> None:
        """Point the canvas at the rectangles that will actually be exported.

        The manual store is the single source of truth: a page the user has
        taken over shows their rectangles, everything else shows the detector's.
        """
        self._boxes = (
            self._manual.get(self._page_index)
            if self._manual.is_manual(self._page_index)
            else list(self._detected)
        )
        self._rects = list(self._boxes)

    def _sync_edit_state(self) -> None:
        """Refresh widgets whose enabled-state depends on the current page.

        Deliberately **read-only with respect to the store**.  An earlier
        version re-marked the page whenever the mode toggle read "edit" and
        reloaded the boxes from the store, which meant that pressing "restore
        automatic" was undone the instant it finished -- the toggle rebuilt the
        manual page it had just cleared.
        """
        if self._selected is not None and not 0 <= self._selected < len(self._boxes):
            self._selected = None
        manual = self._manual.is_manual(self._page_index)
        has_edit = self._boxes != self._detected
        self.btn_del_box.state(
            ["!disabled"] if (self._editing() and self._selected is not None) else ["disabled"]
        )
        self.btn_use_detected.state(
            ["!disabled"] if (manual or has_edit) else ["disabled"]
        )
        self.btn_clear_all.state(
            ["!disabled"] if not self._manual.is_empty else ["disabled"]
        )
        self._update_page_labels()


    def _update_page_labels(self) -> None:
        if self._page_count:
            self.lbl_page.configure(
                text=f"第 {self._page_index + 1} / {self._page_count} 页"
            )
        manual = self._manual.is_manual(self._page_index)
        self.lbl_page_mode.configure(
            text=("  ·  本页：手动框选（将按此导出）" if manual else "  ·  本页：自动检测"),
            foreground=MANUAL_COLOUR if manual else "#666666",
        )
        if self._editing():
            self.lbl_legend.configure(
                text="拖动=新建 · 拖框体=移动 · 拖橙角=缩放 · 右键/Delete=删除"
            )
        elif manual:
            self.lbl_legend.configure(text=f"蓝框=手动（将按此导出）  {self._manual.summary()}")
        else:
            self.lbl_legend.configure(text="红框 = 自动检出的图片区域（导出后即为裁剪范围）")

    def _grab_pt(self) -> float:
        """The on-screen grab tolerance expressed in PDF points.

        ``gui_edit.EDGE_GRAB_PX`` is a canvas-pixel distance, but hit-testing
        happens in PDF space.  Converting through the live zoom keeps the target
        the same physical size on screen at any window size.
        """
        cfg = self._last_config or CropConfig()
        return gui_edit.EDGE_GRAB_PX / max(self._zoom, 1e-6) / (cfg.dpi / 72.0)

    def _canvas_to_pdf_point(self, x: float, y: float) -> Tuple[float, float]:
        cfg = self._last_config or CropConfig()
        rect = gui_edit.canvas_rect_to_pdf(
            x, y, x, y,
            dpi=cfg.dpi,
            page_height_pt=self._page_size_pt[1],
            zoom=self._zoom,
            origin=self._origin,
        )
        return rect.x0, rect.y0

    def _pdf_to_canvas_point(self, px: float, py: float) -> Tuple[float, float]:
        cfg = self._last_config or CropConfig()
        x0, y0, x1, y1 = gui_edit.pdf_rect_to_canvas(
            fitz.Rect(px, py, px, py),
            dpi=cfg.dpi,
            page_height_pt=self._page_size_pt[1],
            zoom=self._zoom,
            origin=self._origin,
        )
        return x0, y0

    def _on_canvas_press(self, event: tk.Event) -> None:
        self.canvas.focus_set()
        if self._doc is None:
            return
        self._cancel_drag()
        if not self._editing():
            # In view mode a click still selects, so a user can inspect a
            # single figure, but it must not move anything.  Hit-test against
            # the *displayed* boxes so the highlight lands where the frame is.
            px, py = self._canvas_to_pdf_point(event.x, event.y)
            hit = gui_edit.box_at(px, py, self._display_boxes(), self._grab_pt())
            self._selected = hit
            self._sync_edit_state()
            self._refresh_boxes()
            return

        self._manual.mark(self._page_index)
        px, py = self._canvas_to_pdf_point(event.x, event.y)
        grab = self._grab_pt()
        target = self._boxes[self._selected] if self._selected is not None else None
        handle = (
            gui_edit.nearest_handle(px, py, target, grab) if target is not None else None
        )
        if handle is not None:
            self._drag_mode = "resize"
            self._drag_handle = handle
            self._drag_index = self._selected
        else:
            hit = gui_edit.box_at(px, py, self._boxes, grab)
            self._selected = hit
            if hit is None:
                self._drag_mode = "draw"
                self._drag_anchor = (event.x, event.y)
                self._marquee_x, self._marquee_y = event.x, event.y
            else:
                self._drag_mode = "move"
                self._drag_index = hit
                self._drag_anchor = (event.x, event.y)
        self._sync_edit_state()
        self._refresh_boxes()

    def _on_canvas_drag(self, event: tk.Event) -> None:
        if self._drag_mode is None:
            return
        if self._drag_mode == "draw":
            self._marquee_x, self._marquee_y = event.x, event.y
            self._draw_marquee()
            return

        cfg = self._last_config or CropConfig()
        index = self._drag_index
        if index is None or not 0 <= index < len(self._boxes):
            return
        rect = self._boxes[index]
        page = self._page_rect()
        if self._drag_mode == "move" and self._drag_anchor is not None:
            # The box is rigid, so the displacement is simply how far the
            # cursor travelled since the press -- *not* that minus the offset
            # at which the box was grabbed.  Subtracting the grab offset would
            # make grabbing the middle of a wide box teleport it sideways.
            #
            # y needs its sign flipped: canvas y grows downward while PDF y
            # grows upward, so "cursor moved down" is a negative PDF delta.
            dx_px = event.x - self._drag_anchor[0]
            dy_px = event.y - self._drag_anchor[1]
            scale = cfg.dpi / 72.0
            new = gui_edit.move_rect(
                rect, dx_px / scale, -dy_px / scale, page
            )
        elif self._drag_mode == "resize" and self._drag_handle is not None:
            px, py = self._canvas_to_pdf_point(event.x, event.y)
            edges = gui_edit.edges_for_handle(self._drag_handle)
            new = gui_edit.resize_rect(
                rect,
                left=px if "left" in edges else None,
                right=px if "right" in edges else None,
                top=py if "top" in edges else None,
                bottom=py if "bottom" in edges else None,
                page=page,
            )
        else:  # pragma: no cover -- defensive
            return
        if gui_edit.is_too_small(new):
            return
        self._boxes[index] = new
        self._manual.set(self._page_index, self._boxes)
        self._refresh_boxes()

    def _on_canvas_release(self, event: tk.Event) -> None:
        if self._drag_mode == "draw" and self._drag_anchor is not None:
            cfg = self._last_config or CropConfig()
            rect = gui_edit.clamp_to_page(
                gui_edit.canvas_rect_to_pdf(
                    self._drag_anchor[0], self._drag_anchor[1], event.x, event.y,
                    dpi=cfg.dpi,
                    page_height_pt=self._page_size_pt[1],
                    zoom=self._zoom,
                    origin=self._origin,
                ),
                self._page_rect(),
            )
            if not gui_edit.is_too_small(rect):
                self._boxes.append(rect)
                self._selected = len(self._boxes) - 1
            elif self._selected is None and not self._boxes:
                # A stray click on an empty page is worth saying out loud,
                # otherwise it looks like the editor swallowed the input.
                self._set_status(
                    "框太小，已忽略。拖动时按住左键划过图片区域即可。",
                    level=gui_hints.LEVEL_WARN,
                )
        self._cancel_drag()
        if self._editing():
            self._manual.set(self._page_index, self._boxes)
            self._rects = list(self._boxes)
        self._sync_edit_state()
        self._refresh_boxes()
        self._show_edit_summary()

    def _on_canvas_right_click(self, event: tk.Event) -> None:
        """Right-click deletes -- the gesture every other editor uses for it."""
        self._cancel_drag()
        if not self._editing() or self._doc is None:
            return
        px, py = self._canvas_to_pdf_point(event.x, event.y)
        hit = gui_edit.box_at(px, py, self._boxes, self._grab_pt())
        if hit is None:
            return
        del self._boxes[hit]
        self._manual.set(self._page_index, self._boxes)
        self._selected = None if not self._boxes else min(hit, len(self._boxes) - 1)
        self._sync_edit_state()
        self._refresh_boxes()
        self._show_edit_summary()

    def _on_canvas_key(self, event: tk.Event) -> None:
        key = event.keysym
        if key in ("Delete", "BackSpace"):
            self._delete_selected()
            return
        if key == "Escape":
            self._selected = None
            self._cancel_drag()
            self._sync_edit_state()
            self._refresh_boxes()
            return
        arrows = {"Left": (-1, 0), "Right": (1, 0), "Up": (0, -1), "Down": (0, 1)}
        if key not in arrows or not self._editing() or self._selected is None:
            return
        if not 0 <= self._selected < len(self._boxes):
            return
        # Shift is the coarse step: 1 pt is a hair at 250 DPI, 10 pt is about
        # a third of a millimetre and is what most nudges actually want.
        step = 10.0 if event.state & 0x0001 else 1.0
        dx, dy = arrows[key]
        new = gui_edit.move_rect(
            self._boxes[self._selected], dx * step, dy * step, self._page_rect()
        )
        self._boxes[self._selected] = new
        self._manual.set(self._page_index, self._boxes)
        self._refresh_boxes()
        self._show_edit_summary()

    def _cancel_drag(self) -> None:
        self._drag_mode = None
        self._drag_anchor = None
        self._drag_index = None
        self._drag_handle = None
        self.canvas.delete("marquee")

    def _delete_selected(self) -> None:
        if not self._editing() or self._selected is None:
            return
        if not 0 <= self._selected < len(self._boxes):
            self._selected = None
            return
        del self._boxes[self._selected]
        self._manual.set(self._page_index, self._boxes)
        self._selected = None if not self._boxes else min(self._selected, len(self._boxes) - 1)
        self._sync_edit_state()
        self._refresh_boxes()
        self._show_edit_summary()

    def _reset_page_to_detected(self) -> None:
        """Hand the current page back to the detector, dropping manual boxes."""
        if not self._boxes and not self._manual.is_manual(self._page_index):
            return
        if self._editing() and self._boxes:
            if not messagebox.askyesno(
                "恢复自动检测",
                f"第 {self._page_index + 1} 页的手动框将被丢弃，"
                "改回自动检测的结果。继续？",
            ):
                return
        self._manual.reset_page(self._page_index)
        self._selected = None
        self._load_page_boxes()
        if not self._detected:
            self.var_edit_mode.set("view")
        self._sync_edit_state()
        self._refresh_boxes()
        self._set_status(
            f"第 {self._page_index + 1} 页已恢复为自动检测结果。",
            level=gui_hints.LEVEL_OK,
        )

    def _clear_all_manual(self) -> None:
        if self._manual.is_empty:
            return
        if not messagebox.askyesno(
            "清空所有手动修改",
            f"将丢弃 {self._manual.summary()}，所有页面改回自动检测。继续？",
        ):
            return
        self._manual.clear()
        self._selected = None
        self._load_page_boxes()
        if not self._boxes:
            self.var_edit_mode.set("view")
        self._sync_edit_state()
        self._refresh_boxes()
        self._set_status("已清空全部手动修改，所有页面恢复自动检测。", level=gui_hints.LEVEL_OK)

    def _update_figures_label(self, count: Optional[int] = None) -> None:
        """Footer summary, in the sizes that will actually be cropped.

        Uses the displayed (border-expanded) rectangles, not the raw detection,
        so the millimetre figures in the footer describe the output rather than
        the artwork.
        """
        shown = self._display_boxes()
        if count is None:
            count = len(shown)
        prefix = "本页手动" if self._manual.is_manual(self._page_index) else "共"
        self.lbl_figures.configure(
            text=f"{prefix} {count} 个裁剪框" + (
                f"  ·  {gui_hints.summarise_figures(shown)}" if count else ""
            )
        )

    def _show_edit_summary(self) -> None:
        """Status line describing what the export will now produce."""
        if not self._editing():
            return
        n = len(self._boxes)
        self._update_figures_label(n)
        self._set_status(
            f"第 {self._page_index + 1} 页：{n} 个裁剪框（按原样导出，不加白边）。"
            f"{self._manual.summary()}。",
            level=gui_hints.LEVEL_OK,
        )

    # -- feedback ----------------------------------------------------------

    def _show_analysis(self, analysis: gui_hints.PageAnalysis) -> None:
        if not analysis.hints:
            self.lbl_hint_title.configure(text="✓ 检测正常", foreground="#1a7f37")
            self.lbl_hint_detail.configure(text="")
            self.btn_apply.state(["disabled"])
        else:
            hint = analysis.hints[0]
            colour = {"error": "#b42318", "warn": "#b54708"}.get(hint.level, "#444444")
            self.lbl_hint_title.configure(text=hint.title, foreground=colour)
            self.lbl_hint_detail.configure(text=hint.detail)
            self.btn_apply.state(["!disabled"] if hint.is_actionable else ["disabled"])

        count = analysis.figure_count
        self._update_figures_label(count)
        if analysis.level == gui_hints.LEVEL_OK and count:
            self._set_status("检测完成。确认红框位置正确后点「导出裁剪结果」。",
                             level=gui_hints.LEVEL_OK)
        else:
            # Never leave a stale "all good" message on screen next to a
            # warning -- the two contradicting each other is exactly the kind
            # of thing that sends people tuning the wrong knob.
            self._set_status(
                analysis.headline or "检测完成。", level=analysis.level
            )

    def _apply_hint(self) -> None:
        if self._pending_config is None:
            return
        for key, var in self._fields.items():
            var.set(f"{getattr(self._pending_config, key):g}")
        self.var_algorithm.set(self._pending_config.algorithm)
        self._set_status(
            "已应用建议参数，正在重新分析…", level=gui_hints.LEVEL_OK
        )
        self._analyse()

    def _set_status(self, message: str, level: str = gui_hints.LEVEL_OK) -> None:
        colour = {"error": "#b42318", "warn": "#b54708"}.get(level, "#1f2328")
        self.lbl_status.configure(text=message, foreground=colour)

    def _set_busy(self, busy: bool, message: str = "") -> None:
        state = "disabled" if busy else "!disabled"
        for widget in (self.btn_export, self.btn_del_box, self.btn_use_detected,
                       self.btn_clear_all):
            widget.state([state])
        if busy:
            self.progress.start(12)
            if message:
                self.lbl_progress.configure(text=message)
        else:
            self.progress.stop()
            self.lbl_progress.configure(text="")
            # Busy-mode only ever *disables*; the editor's own enablement rules
            # decide what comes back, so they have to be re-applied afterwards.
            self._sync_edit_state()

    # -- export ------------------------------------------------------------

    def _export(self) -> None:
        if self._src_path is None or not self._src_path.exists():
            self._set_status("请先选择输入文件。", level=gui_hints.LEVEL_ERROR)
            return
        out_dir = Path(self.var_output.get().strip() or "./output")
        cfg = self.current_config()

        if self._src_path.is_dir():
            pdfs = sorted(self._src_path.glob("*.pdf"))
            if not pdfs:
                self._set_status("该文件夹里没有 PDF 文件。", level=gui_hints.LEVEL_WARN)
                return
            if not self._manual.is_empty:
                # Page-keyed rectangles mean nothing across a folder of
                # different documents, so refuse rather than crop the wrong
                # artwork.  The store is empty in practice -- batch mode has no
                # single page to draw on -- but a stale store would be silent
                # data loss, which is the worst kind.
                self._set_status(
                    "批量模式下无法使用手动框选，请先打开单个 PDF。",
                    level=gui_hints.LEVEL_WARN,
                )
                return
            self._set_busy(True, f"正在批量处理 0/{len(pdfs)}…")
            self._bg.submit(
                lambda app: _export_batch(pdfs, out_dir, cfg, self._on_progress),
                self._on_exported,
                on_error=self._on_failed,
            )
        else:
            self._set_busy(True, "正在导出…")
            overrides = self._manual_overrides()
            self._bg.submit(
                lambda app: _export_single(self._src_path, out_dir, cfg, overrides),
                self._on_exported,
                on_error=self._on_failed,
            )

    def _manual_overrides(self) -> Optional[Dict[int, List[fitz.Rect]]]:
        """Per-page rectangles that must bypass the detector.

        Only *marked* pages appear here, so a page the user merely looked at
        still goes through detection.  ``None`` (rather than an empty dict) when
        nothing is manual, which keeps :func:`run_pipeline` on its original
        code path entirely.
        """
        if self._manual.is_empty:
            return None
        return {p: self._manual.get(p) for p in self._manual.manual_pages}


    def _on_progress(self, text: str) -> None:
        """Called from the worker thread -- hand the text back to the UI."""
        self.after(0, lambda: self.lbl_progress.configure(text=text))

    def _on_exported(self, result: Any) -> None:
        self._set_busy(False)
        out_dir, written = result
        if written:
            self._set_status(
                f"完成：已写出 {written} 个 PDF 到 {out_dir}", level=gui_hints.LEVEL_OK
            )
            if messagebox.askyesno(
                "导出完成",
                f"已写出 {written} 个 PDF。\n\n是否现在打开输出文件夹？",
            ):
                self._open_output()
        else:
            self._set_status("没有检测到可导出的图片。", level=gui_hints.LEVEL_WARN)

    def _open_output(self) -> None:
        folder = Path(self.var_output.get().strip() or "./output")
        folder.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(folder))  # noqa: S606 -- user-initiated
            elif sys.platform == "darwin":
                os.system(f'open "{folder}"')
            else:
                os.system(f'xdg-open "{folder}"')
        except Exception as exc:  # pragma: no cover -- platform dependent
            self._set_status(f"无法打开文件夹：{exc}", level=gui_hints.LEVEL_ERROR)

    def _on_close(self) -> None:
        self._close_doc()
        self.destroy()


# ---------------------------------------------------------------------------
# Worker bodies (pure functions -- no Tk access, safe to call from a thread)
# ---------------------------------------------------------------------------


def _analyse_page(
    doc: Optional[fitz.Document], page_index: int, cfg: CropConfig
) -> Tuple[np.ndarray, Tuple[float, float], List[fitz.Rect]]:
    """Render one page and run the configured detector over it."""
    if doc is None:
        raise ValueError("no document is open")
    rgb, size_pt = render_page_to_array(doc, page_index, cfg.dpi)
    detect = get_detector(cfg)
    rects = detect(rgb, size_pt, cfg)
    return rgb, size_pt, list(rects)


def _export_single(
    src: Path,
    out_dir: Path,
    cfg: CropConfig,
    manual_boxes: Optional[Dict[int, List[fitz.Rect]]] = None,
) -> Tuple[Path, int]:
    """Crop one PDF through the shared pipeline; return ``(out_dir, count)``.

    ``manual_boxes`` supplies per-page rectangle overrides from the GUI's
    editor.  Pages it does not mention are cropped by the detector exactly as
    the CLI would, which is what makes a partly hand-drawn document work.

    The count comes from the pipeline's own tally, not from globbing the output
    directory: a directory listing would also pick up figures left behind by an
    earlier run with different settings, so the status bar could announce a
    total the user has no matching files for.
    """
    args = build_pipeline_args(src, out_dir)
    report: Dict[str, Any] = {}
    rc = run_pipeline(args, cfg, manual_boxes=manual_boxes, report=report)
    if rc != 0:
        raise RuntimeError(f"裁剪失败，退出码 {rc}")
    return out_dir, int(report.get("total", 0))


def _export_batch(
    pdfs: Sequence[Path],
    out_dir: Path,
    cfg: CropConfig,
    progress: Callable[[str], None],
) -> Tuple[Path, int]:
    """Crop many PDFs, mirroring the CLI's per-file subdirectory layout."""
    out_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    failures: List[str] = []
    for i, pdf in enumerate(pdfs, start=1):
        progress(f"正在批量处理 {i}/{len(pdfs)}…  {pdf.name}")
        job_out = out_dir / pdf.stem
        try:
            args = build_pipeline_args(pdf, job_out)
            rc = run_pipeline(args, cfg)
        except Exception as exc:  # noqa: BLE001 -- reported at the end
            failures.append(f"{pdf.name}: {exc}")
            continue
        if rc != 0:
            failures.append(f"{pdf.name}: 退出码 {rc}")
            continue
        total += len(sorted(job_out.glob("*.pdf")))
    if failures:
        raise RuntimeError("部分文件处理失败：" + "；".join(failures[:3]))
    return out_dir, total


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def launch() -> int:
    """Open the desktop window.  Returns a process exit code."""
    # Before any window exists: on a high-DPI display Windows otherwise
    # bitmap-scales the whole interface, which blurs the text and clips the
    # labels of the themed widgets.
    from .dpi import enable_dpi_awareness

    enable_dpi_awareness()
    try:
        app = App()
    except tk.TclError as exc:  # pragma: no cover -- headless environment
        print(f"无法启动图形界面：{exc}", file=sys.stderr)
        print("请改用命令行：python -m pdf_auto_crop.cli --help", file=sys.stderr)
        return 1
    app.mainloop()
    return 0


__all__ = ["App", "launch", "build_pipeline_args", "page_area_mm2"]


if __name__ == "__main__":
    sys.exit(launch())
