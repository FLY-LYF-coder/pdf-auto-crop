"""Drive the compositor window with synthetic events and verify the result.

Unit tests cover the geometry, but they cannot prove that the canvas wiring
points the right way round -- a press handler bound to the wrong event, or a
coordinate flipped the wrong way, produces perfectly correct ``compositor``
calls attached to the wrong place.  This script drives the *real* window
with ``event_generate``, exactly the way a user moves the mouse, and then
checks the composition that comes out.

It also captures screenshots so the layout can be eyeballed.

Run:  python scripts/compositor_screenshot.py
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path
from typing import Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:  # pragma: no cover
    pass

import numpy as np  # noqa: E402
import pytest  # noqa: E402
import pymupdf as fitz  # noqa: E402
from PIL import ImageGrab  # noqa: E402

from pdf_auto_crop import compositor as comp  # noqa: E402
from pdf_auto_crop import compositor_gui  # noqa: E402
from pdf_auto_crop.dpi import enable_dpi_awareness  # noqa: E402

# Before any window exists, so the window is drawn at the display's real
# resolution instead of being bitmap-scaled.
enable_dpi_awareness()

SHOTS = ROOT / "diag_out" / "compositor"
SHOTS.mkdir(parents=True, exist_ok=True)
WORK = ROOT / "diag_out" / "compositor_work"
WORK.mkdir(parents=True, exist_ok=True)

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'ok ' if condition else 'FAIL'}] {label}"
          f"{(' -> ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


def make_fixture(path: Path) -> None:
    """A page with three panels in three different colours."""
    doc = fitz.open()
    page = doc.new_page(width=420, height=560)
    page.draw_rect(fitz.Rect(30, 30, 390, 250), color=(0.1, 0.3, 0.8),
                   fill=(0.1, 0.3, 0.8), width=0)
    page.draw_rect(fitz.Rect(30, 290, 200, 520), color=(0.8, 0.3, 0.1),
                   fill=(0.8, 0.3, 0.1), width=0)
    page.draw_rect(fitz.Rect(220, 290, 390, 520), color=(0.1, 0.6, 0.2),
                   fill=(0.1, 0.6, 0.2), width=0)
    doc.save(str(path))
    doc.close()


def figure_colour(pdf: Path, page_index: int, region: fitz.Rect) -> tuple[int, int, int]:
    """The colour at the middle of a region, read from the source page."""
    doc = fitz.open(str(pdf))
    try:
        pix = doc[page_index].get_pixmap(dpi=72)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n)[:, :, :3].astype(int)
        # The region is in user space; the render is top-down.
        h = arr.shape[0]
        cx = int((region.x0 + region.x1) / 2)
        cy = int(h - (region.y0 + region.y1) / 2)
        return tuple(int(v) for v in arr[cy, cx])
    finally:
        doc.close()


def _physical_rect(win) -> Optional[Tuple[int, int, int, int]]:
    """The window's rectangle in physical screen pixels.

    Tk reports the geometry in logical pixels and ImageGrab grabs physical
    ones, so on a scaled display the two have to be reconciled.  The thread
    is made DPI-aware for the call, and the window rectangle is then read
    in physical pixels.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes.wintypes

        hwnd = ctypes.windll.user32.GetAncestor(win.winfo_id(), 2)  # GA_ROOT
        ctypes.windll.user32.SetThreadDpiAwarenessContext(
            ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
        rect = ctypes.wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        return rect.left, rect.top, rect.right, rect.bottom
    except Exception:  # noqa: BLE001 -- only the picture suffers
        return None


def _shot(win, name: str) -> Optional[Path]:
    win.update()
    # The screen grab takes whatever is on top, and the user's own window
    # may well be -- so the compositor comes forward for the picture.
    was_topmost = bool(win.attributes("-topmost"))
    if not was_topmost:
        win.attributes("-topmost", True)
    win.lift()
    win.update()
    bbox = _physical_rect(win)
    if bbox is None:
        x, y = win.winfo_rootx(), win.winfo_rooty()
        w, h = win.winfo_width(), win.winfo_height()
        bbox = (x, y, x + w, y + h)
    path = SHOTS / name
    try:
        ImageGrab.grab(bbox=bbox).save(str(path))
    except OSError as exc:
        # A locked or switched-away desktop cannot be grabbed; the checks
        # themselves do not need the picture.
        print(f"  (no screenshot for {name}: {exc})")
        path = None
    finally:
        if not was_topmost:
            win.attributes("-topmost", False)
    return path


def _click(win, x: int, y: int, ctrl: bool = False) -> None:
    """A press and a release, with or without Ctrl held down."""
    canvas = win.canvas
    # Tk gives a Ctrl-click its own event name; the plain <Button-1>
    # binding never sees it.
    name = "<Control-Button-1>" if ctrl else "<Button-1>"
    canvas.event_generate(name, x=x, y=y)
    win.update()
    canvas.event_generate("<ButtonRelease-1>", x=x, y=y)
    win.update()


def _drag(win, x0: int, y0: int, x1: int, y1: int) -> None:
    """Press, move, release -- the three events a real drag produces."""
    canvas = win.canvas
    canvas.event_generate("<Button-1>", x=x0, y=y0)
    win.update()
    for step in range(1, 5):
        canvas.event_generate(
            "<B1-Motion>",
            x=x0 + (x1 - x0) * step // 4,
            y=y0 + (y1 - y0) * step // 4,
        )
        win.update()
    canvas.event_generate("<ButtonRelease-1>", x=x1, y=y1)
    win.update()


def main() -> int:
    fixture = WORK / "panels.pdf"
    make_fixture(fixture)

    win = compositor_gui.ComposeWindow(None)
    # The window sizes itself, scaled for the display's resolution.
    win.update()
    win._redraw()
    win.update()

    # -- import ---------------------------------------------------------
    found = comp.detect_figures(fixture)
    check("detector finds the three panels", len(found) == 3, str(len(found)))
    win.palette.extend(found)
    win._refresh_palette()
    win.update()
    check("palette lists them",
          win.lst_palette.size() == 3, str(win.lst_palette.size()))

    # -- place them -----------------------------------------------------
    for i in range(3):
        win.lst_palette.selection_clear(0, "end")
        win.lst_palette.selection_set(i)
        win._add_selected()
    win.update()
    check("all three are on the canvas",
          len(win.composition.placements) == 3,
          str(len(win.composition.placements)))
    check("they were numbered (a), (b), (c)",
          [p.label for p in win.composition.placements] == ["(a)", "(b)", "(c)"],
          str([p.label for p in win.composition.placements]))
    check("each placement shows a different figure",
          len({p.figure.key for p in win.composition.placements}) == 3)
    _shot(win, "01_placed.png")

    # -- drag the topmost figure under the cursor ------------------------
    # The three are dropped in the middle, so they overlap; the one that is
    # picked up is the last one drawn.
    picked = len(win.composition.placements) - 1
    p = win.composition.placements[picked]
    before = fitz.Rect(p.rect)
    cx, cy = win._to_canvas((before.x0 + before.x1) / 2,
                            (before.y0 + before.y1) / 2)
    zoom = win._zoom
    _drag(win, int(cx), int(cy), int(cx) + 40, int(cy) - 25)
    after = fitz.Rect(p.rect)
    check("the press picked the figure under the cursor",
          win._selected == picked, f"{win._selected} vs {picked}")
    dx_pt = 40 / zoom
    dy_pt = 25 / zoom
    check("dragging right moves the figure right",
          abs((after.x0 - before.x0) - dx_pt) < 2.0,
          f"{after.x0 - before.x0:.2f} vs {dx_pt:.2f}")
    check("dragging up moves the figure up",
          abs((after.y0 - before.y0) - dy_pt) < 2.0,
          f"{after.y0 - before.y0:.2f} vs {dy_pt:.2f}")
    _shot(win, "02_dragged.png")

    # -- resize with a corner handle ------------------------------------
    win._selected = 0
    win._redraw()
    win.update()
    p = win.composition.placements[0]
    r = fitz.Rect(p.rect)
    hx, hy = win._to_canvas(r.x1, r.y1)     # the bottom-right handle
    aspect_before = r.width / r.height
    _drag(win, int(hx), int(hy), int(hx) + 30, int(hy) - 15)
    r2 = fitz.Rect(p.rect)
    aspect_after = r2.width / r2.height
    check("the resize keeps the aspect ratio",
          abs(aspect_after - aspect_before) < 0.05,
          f"{aspect_before:.3f} -> {aspect_after:.3f}")
    check("the resize made the figure bigger",
          r2.width > r.width, f"{r.width:.1f} -> {r2.width:.1f}")
    _shot(win, "03_resized.png")

    # -- keyboard nudge --------------------------------------------------
    win._selected = 1
    win.canvas.focus_set()
    before_y = win.composition.placements[1].rect.y0
    win.canvas.event_generate("<Up>")
    win.update()
    after_y = win.composition.placements[1].rect.y0
    check("the up arrow nudges the figure up",
          abs((after_y - before_y) - 1.0) < 1e-6,
          f"{after_y - before_y:.3f}")
    before_y = after_y
    win.canvas.event_generate("<Down>")
    win.update()
    after_y = win.composition.placements[1].rect.y0
    check("the down arrow nudges the figure down",
          abs((after_y - before_y) + 1.0) < 1e-6,
          f"{after_y - before_y:.3f}")

    # -- label editing ---------------------------------------------------
    win._selected = 2
    win._sync_properties()
    win.var_label.set("(c1)")
    win._on_label_changed()
    win.update()
    check("the label can be edited",
          win.composition.placements[2].label == "(c1)",
          win.composition.placements[2].label)

    # -- auto numbering and layout ---------------------------------------
    win._auto_label()
    win.update()
    check("auto numbering renumbers in order",
          [p.label for p in win.composition.placements] == ["(a)", "(b)", "(c)"],
          str([p.label for p in win.composition.placements]))
    win._arrange("grid")
    win.update()
    rects = [fitz.Rect(p.rect) for p in win.composition.placements]
    check("the grid arrangement does not overlap",
          not (rects[0].intersects(rects[1])
               or rects[0].intersects(rects[2])
               or rects[1].intersects(rects[2])))
    check("the grid arrangement stays on the sheet",
          all(0 <= r.x0 and r.x1 <= win.sheet_w + 1
              and 0 <= r.y0 and r.y1 <= win.sheet_h + 1 for r in rects))
    _shot(win, "04_arranged.png")

    # -- label formatting ------------------------------------------------
    win._selected = 0
    win._selected_shape = None
    win._sync_properties()
    win.var_label_font.set("Helvetica-Bold")
    win._on_label_changed()
    win.var_label_size.set("14")
    win._on_label_changed()
    win.var_label_color.set("#cc0000")
    win._on_label_changed()
    win.update()
    placement = win.composition.placements[0]
    check("the label font can be changed",
          placement.label_font == "Helvetica-Bold", placement.label_font)
    check("the label size can be changed",
          abs(placement.label_size - 14.0) < 1e-6, str(placement.label_size))
    check("the label colour can be changed",
          placement.label_color == pytest.approx((0.8, 0.0, 0.0), abs=0.01),
          str(placement.label_color))
    _shot(win, "06_label_style.png")

    # -- drawing a graphic element ---------------------------------------
    n_shapes = len(win.composition.shapes)
    win.var_tool.set("箭头")
    win._on_tool_changed()
    check("the arrow tool is active", win.tool == "arrow", win.tool)
    _drag(win, 700, 200, 780, 260)
    check("dragging draws an arrow",
          len(win.composition.shapes) == n_shapes + 1,
          f"{len(win.composition.shapes)} shapes")
    if win.composition.shapes:
        arrow = win.composition.shapes[-1]
        check("the arrow has the right kind", arrow.kind == "arrow", arrow.kind)
        check("the arrow runs from where the press was to where the release was",
              arrow.start.x < arrow.end.x and arrow.start.y > arrow.end.y,
              f"{arrow.start} -> {arrow.end}")
        check("the new arrow is selected", win._selected_shape is not None)

    # A box, a circle and a triangle, to show the other tools work too.
    for tool, x0, y0, x1, y1 in (("方框", 700, 320, 800, 400),
                                 ("圆圈", 700, 420, 800, 500),
                                 ("三角形", 640, 540, 720, 610)):
        win.var_tool.set(tool)
        win._on_tool_changed()
        _drag(win, x0, y0, x1, y1)
    check("all the graphic tools drew something",
          len(win.composition.shapes) == n_shapes + 4,
          f"{len(win.composition.shapes)} shapes")
    drawn = win.composition.shapes[-1]
    check("a new element takes the style of the panel",
          drawn.fill_color == pytest.approx((1.0, 0.878, 0.541), abs=0.02)
          and drawn.dash == comp.DASH_SOLID
          and drawn.stroke_opacity == pytest.approx(1.0),
          f"{drawn.fill_color} {drawn.dash} {drawn.stroke_opacity}")
    _shot(win, "07_shapes.png")

    # -- moving a graphic element ----------------------------------------
    win.var_tool.set("选择/移动")
    win._on_tool_changed()
    arrow = win.composition.shapes[-3]
    before = fitz.Point(arrow.start.x, arrow.start.y)
    cx, cy = win._to_canvas(arrow.start.x, arrow.start.y)
    _drag(win, int(cx), int(cy), int(cx) + 30, int(cy))
    arrow = win.composition.shapes[-3]
    check("a graphic element can be dragged",
          arrow.start.x > before.x + 10,
          f"{before.x:.1f} -> {arrow.start.x:.1f}")

    # -- deleting a graphic element --------------------------------------
    n_shapes = len(win.composition.shapes)
    win._selected_shape = 0
    win._delete_selected()
    win.update()
    check("a graphic element can be deleted",
          len(win.composition.shapes) == n_shapes - 1,
          f"{len(win.composition.shapes)} shapes")

    # -- styling a graphic element ----------------------------------------
    win._selected_shape = 0
    win._sync_properties()
    win.var_shape_color.set("#0070c0")
    win.var_shape_width.set("3")
    win.var_shape_dash.set(comp.DASH_DASHED)
    win.var_shape_opacity.set("60")
    win.var_shape_fill.set(True)
    win.var_shape_fill_color.set("#ffe08a")
    win.var_shape_fill_opacity.set("40")
    win._on_shape_changed()
    win.update()
    styled = win.composition.shapes[0]
    check("the border colour can be changed",
          styled.color == pytest.approx((0.0, 0.44, 0.75), abs=0.02),
          str(styled.color))
    check("the border width can be changed",
          styled.width == pytest.approx(3.0), str(styled.width))
    check("the border style can be changed",
          styled.dash == comp.DASH_DASHED, styled.dash)
    check("the border opacity can be changed",
          styled.stroke_opacity == pytest.approx(0.6), str(styled.stroke_opacity))
    check("the fill colour can be changed",
          styled.fill_color == pytest.approx((1.0, 0.878, 0.541), abs=0.02),
          str(styled.fill_color))
    check("the fill opacity can be changed",
          styled.fill_opacity == pytest.approx(0.4), str(styled.fill_opacity))
    _shot(win, "08_style.png")

    # -- selecting several figures and lining them up ---------------------
    win.var_tool.set("选择/移动")
    win._on_tool_changed()
    win._selected = 0
    win._sel_figs = [0]
    win._redraw()
    widths_before = [win.composition.placements[i].rect.width
                     for i in (0, 1)]
    second = win.composition.placements[1]
    cx, cy = win._to_canvas((second.rect.x0 + second.rect.x1) / 2,
                            (second.rect.y0 + second.rect.y1) / 2)
    _click(win, int(cx), int(cy), ctrl=True)
    check("ctrl+click adds to the selection",
          set(win._sel_figs) == {0, 1}, str(win._sel_figs))
    win._align(comp.ALIGN_LEFT)
    win.update()
    lefts = [win.composition.placements[i].rect.x0 for i in (0, 1)]
    check("aligning left lines the two figures up",
          abs(lefts[0] - lefts[1]) < 0.01, f"{lefts}")
    win._align(comp.ALIGN_TOP)
    tops = [win.composition.placements[i].rect.y1 for i in (0, 1)]
    check("aligning top lines the two figures up",
          abs(tops[0] - tops[1]) < 0.01, f"{tops}")
    widths_after = [win.composition.placements[i].rect.width for i in (0, 1)]
    check("aligning keeps the figures their size",
          all(abs(a - b) < 0.01
              for a, b in zip(widths_before, widths_after)),
          f"{widths_before} -> {widths_after}")
    _shot(win, "09_aligned.png")

    # The two figures now sit on top of each other, which is what aligning
    # them means -- put the layout back before the export checks.
    win._arrange("grid")
    win.update()

    # -- editing the text inside an imported figure --------------------------
    # The panels above are plain colours; a figure with text is needed here.
    text_fig = WORK / "with_text.pdf"
    if not text_fig.exists():
        doc = fitz.open()
        pg = doc.new_page(width=300, height=200)
        pg.draw_rect(fitz.Rect(50, 40, 250, 150), color=(0, 0, 0), width=1)
        pg.insert_text(fitz.Point(120, 170), "time t (s)", fontsize=9)
        doc.save(str(text_fig))
        doc.close()
    win.palette.append(comp.Figure(
        path=text_fig, page_index=0, region=fitz.Rect(0, 0, 300, 200)))
    win._refresh_palette()
    text_index = len(win.composition.placements)
    win.composition.placements.append(comp.Placement(
        figure=win.palette[-1], rect=fitz.Rect(20, 20, 220, 140), label=""))
    win._selected = text_index
    win._sync_properties()
    blocks = comp.figure_text_blocks(win.composition.placements[text_index].figure)
    check("the text inside a figure is recognised",
          len(blocks) == 1 and blocks[0].text == "time t (s)",
          f"{[b.text for b in blocks]}")
    win._apply_text_edits({0: "edited"})
    win.update()
    check("the text inside a figure can be changed",
          win.composition.placements[text_index].text_edits == {0: "edited"},
          str(win.composition.placements[text_index].text_edits))
    win._apply_text_edits({})
    win.update()
    # The extra figure must not upset the checks that come later.
    del win.composition.placements[text_index]
    win._selected = 0
    win.update()

    # -- grouping ----------------------------------------------------------
    win._selected = 0
    win._sel_figs = [0, 1]
    win._sel_shapes = []
    win._sel_texts = []
    win._group_selected()
    win.update()
    check("objects can be grouped",
          len(win.composition.groups) == 1
          and sorted(win.composition.groups[0].figs) == [0, 1],
          str([g.figs for g in win.composition.groups]))
    # Clicking a member brings the whole group with it.
    win._sel_figs = []
    win._selected = 0
    second = win.composition.placements[1]
    cx, cy = win._to_canvas((second.rect.x0 + second.rect.x1) / 2,
                            (second.rect.y0 + second.rect.y1) / 2)
    _click(win, int(cx), int(cy))
    check("clicking a member selects the whole group",
          set(win._sel_figs) == {0, 1}, str(win._sel_figs))
    win._ungroup_selected()
    win.update()
    check("a group can be dissolved",
          not win.composition.groups, str(win.composition.groups))

    # -- spreading a group out --------------------------------------------
    # Three free-standing boxes: on a grid the figures overlap, so there is
    # nothing sensible to spread there.
    for x in (100.0, 200.0, 400.0):
        win.composition.shapes.append(comp.Shape(
            comp.SHAPE_RECT, fitz.Point(x, 100), fitz.Point(x + 40, 140),
            color=(0.0, 0.0, 0.0)))
    win._sel_figs = []
    win._sel_texts = []
    win._sel_shapes = list(range(len(win.composition.shapes) - 3,
                                 len(win.composition.shapes)))
    win._redraw()
    win._distribute(comp.DISTRIBUTE_HORIZONTAL)
    win.update()
    rects = sorted((fitz.Rect(win.composition.shapes[i].rect)
                    for i in win._sel_shapes), key=lambda r: r.x0)
    gaps = [rects[i + 1].x0 - rects[i].x1 for i in range(len(rects) - 1)]
    check("distributing makes the gaps equal",
          len(gaps) == 2 and abs(gaps[0] - gaps[1]) < 0.5 and gaps[0] > 0,
          f"{gaps}")
    check("distributing leaves the outer ones alone",
          abs(rects[0].x0 - 100.0) < 0.5 and abs(rects[-1].x0 - 400.0) < 0.5,
          f"{rects[0].x0} {rects[-1].x0}")

    # -- dragging the label anywhere ---------------------------------------
    win._selected = 0
    win._sync_properties()
    placement = win.composition.placements[0]
    before_offset = tuple(placement.label_offset)
    anchor = placement.label_anchor()
    ax, ay = win._to_canvas(anchor.x, anchor.y)
    _drag(win, int(ax), int(ay), int(ax) + 60, int(ay) - 40)
    check("the label can be dragged off the figure",
          placement.label_mode == comp.LABEL_FREE, placement.label_mode)
    check("the dragged label moved",
          abs(placement.label_offset[0] - before_offset[0]) > 10,
          f"{before_offset} -> {placement.label_offset}")
    _shot(win, "10_free_label.png")

    # -- dropping a piece of text ------------------------------------------
    # The texts go to the far right of the sheet, where no figure is: a
    # colour test on the panels would otherwise find them.
    win.var_tool.set("文本")
    win._on_tool_changed()
    tx, ty = win._to_canvas(750, 520)
    _click(win, int(tx), int(ty))
    check("the text tool drops a text",
          len(win.composition.texts) == 1, str(len(win.composition.texts)))
    win._set_text_widget("Hello")
    win._on_text_changed()
    win.var_text_bold.set(True)
    win.var_text_italic.set(True)
    win._on_text_changed()
    win.var_text_color.set("#6a0dad")
    win._on_text_changed()
    win.update()
    item = win.composition.texts[0]
    check("the text can be typed", item.text == "Hello", item.text)
    check("the text can be bold", item.bold is True)
    check("the text can be italic", item.italic is True)
    check("the text colour can be changed",
          item.color == pytest.approx((0.416, 0.051, 0.678), abs=0.02),
          str(item.color))

    # A LaTeX formula, drawn as vector mathematics.
    win.var_tool.set("文本框")
    win._on_tool_changed()
    fx, fy = win._to_canvas(750, 460)
    _click(win, int(fx), int(fy))
    win._set_text_widget("E = mc^2")
    win._on_text_changed()
    win.var_text_formula.set(True)
    win._on_text_changed()
    win.update()
    check("a formula can be typed",
          win.composition.texts[1].formula is True)
    check("the formula is a text box",
          win.composition.texts[1].box is True)
    _shot(win, "11_text.png")

    # -- turning an element ------------------------------------------------
    win.var_tool.set("选择/移动")
    win._on_tool_changed()
    win._selected_text = 0
    win._sync_properties()
    win.var_text_rotation.set("45")
    win._on_text_changed()
    win.update()
    check("a piece of text can be turned",
          win.composition.texts[0].rotation == pytest.approx(45.0),
          str(win.composition.texts[0].rotation))
    win.var_text_rotation.set("0")
    win._on_text_changed()
    win.update()

    win._selected_shape = 0
    win._sync_properties()
    win.var_shape_rotation.set("45")
    win._on_shape_changed()
    win.update()
    check("a graphic element can be turned",
          win.composition.shapes[0].rotation == pytest.approx(45.0),
          str(win.composition.shapes[0].rotation))
    win.var_shape_rotation.set("0")
    win._on_shape_changed()

    win._selected = 0
    win._sync_properties()
    win.var_fig_rotation.set("90")
    win._on_figure_changed()
    win.update()
    check("a figure can be turned",
          win.composition.placements[0].rotation == pytest.approx(90.0),
          str(win.composition.placements[0].rotation))
    win.var_fig_rotation.set("0")
    win._on_figure_changed()
    win.update()

    # -- a caption inside a shape ------------------------------------------
    win._selected_shape = 0
    win._sync_properties()
    win.var_shape_text.set("Note")
    win._on_shape_changed()
    win.update()
    check("a shape can carry a caption",
          win.composition.shapes[0].text == "Note",
          win.composition.shapes[0].text)

    # -- copying and flipping ----------------------------------------------
    n_shapes = len(win.composition.shapes)
    win._selected_shape = 0
    win._duplicate_selected()
    win.update()
    check("the selection can be copied",
          len(win.composition.shapes) == n_shapes + 1,
          f"{len(win.composition.shapes)} shapes")
    win._selected_shape = 0
    left_before = win.composition.shapes[0].start.x
    win._flip_selected("x")
    win.update()
    check("an element can be flipped",
          abs(win.composition.shapes[0].start.x - left_before) > 1,
          f"{left_before} -> {win.composition.shapes[0].start.x}")

    # -- the wheel zooms the view -------------------------------------------
    zoom_before = win._zoom_factor
    win.canvas.event_generate("<MouseWheel>", delta=120, x=400, y=300)
    win.update()
    check("the wheel zooms the view in",
          win._zoom_factor > zoom_before,
          f"{zoom_before} -> {win._zoom_factor}")
    win.canvas.event_generate("<MouseWheel>", delta=-120, x=400, y=300)
    win.update()
    check("the wheel zooms the view out",
          win._zoom_factor == pytest.approx(zoom_before, abs=0.01),
          f"{win._zoom_factor}")
    win._zoom_fit()

    # -- a page of one's own size -------------------------------------------
    win.var_sheet.set("自定义")
    win.var_sheet_w.set("200")
    win.var_sheet_h.set("150")
    win._on_sheet_changed()
    win.update()
    check("the page can be given a size of its own",
          abs(win.sheet_w - 200 * 72 / 25.4) < 0.5
          and abs(win.sheet_h - 150 * 72 / 25.4) < 0.5,
          f"{win.sheet_w:.1f}x{win.sheet_h:.1f}")
    win.var_sheet.set("A4 横向")
    win._on_sheet_changed()
    win.update()

    # -- export -----------------------------------------------------------
    out = WORK / "composed.pdf"
    comp.export_composition(
        win.composition, out, fit_to_content=False,
        page_size=(win.sheet_w, win.sheet_h))
    doc = fitz.open(str(out))
    try:
        page = doc[0]
        text = page.get_text()
        check("the exported PDF carries the labels",
              "(a)" in text and "(b)" in text and "(c)" in text, repr(text))
        check("the exported PDF stays vector",
              len(page.get_drawings()) > 0 and len(page.get_images()) == 0,
              f"{len(page.get_drawings())} drawings, "
              f"{len(page.get_images())} images")
        check("the exported page has the sheet's size",
              abs(page.rect.width - win.sheet_w) < 1
              and abs(page.rect.height - win.sheet_h) < 1,
              f"{page.rect.width:.0f}x{page.rect.height:.0f}")
        spans = [s for b in page.get_text("dict")["blocks"]
                 for line in b.get("lines", []) for s in line["spans"]]
        a_span = [s for s in spans if s["text"] == "(a)"]
        check("the exported label uses the chosen font",
              a_span and "Helvetica" in a_span[0]["font"],
              a_span[0]["font"] if a_span else "no span")
        check("the exported label uses the chosen size",
              a_span and abs(a_span[0]["size"] - 14.0) < 0.2,
              str(a_span[0]["size"]) if a_span else "no span")
        check("the exported label uses the chosen colour",
              a_span and a_span[0]["color"] == 0xCC0000,
              hex(a_span[0]["color"]) if a_span else "no span")
        # The styled element has to arrive with its style.
        styled_drawings = [d for d in page.get_drawings()
                           if d.get("dashes") and d["dashes"] != "[] 0"]
        check("the exported PDF carries the border style",
              bool(styled_drawings),
              str([d.get("dashes") for d in page.get_drawings()]))
        check("the exported PDF carries the fill opacity",
              any(abs((d.get("fill_opacity") or 1.0) - 0.4) < 0.02
                  for d in page.get_drawings()),
              str([round(d.get("fill_opacity") or 1.0, 2)
                   for d in page.get_drawings()]))
        check("the exported PDF carries the free text",
              "Hello" in text, repr(text))
        check("the formula is vector, not a picture",
              len(page.get_images()) == 0,
              f"{len(page.get_images())} images")
    finally:
        doc.close()

    # Each figure has a colour of its own, so each one can be looked for in
    # the exported page and compared with the box it was dropped into.
    #
    # The check runs on an export that holds *only* the figures.  The graphic
    # elements and the labels are painted on top of them, and the default red
    # is close enough to the orange panel that the anti-aliased edge of a red
    # label lands inside the orange colour mask -- which makes the panel look
    # taller than it is.
    saved_shapes = list(win.composition.shapes)
    saved_labels = [p.label for p in win.composition.placements]
    win.composition.shapes = []
    for p in win.composition.placements:
        p.label = ""
    pos_out = WORK / "composed_figures_only.pdf"
    comp.export_composition(
        win.composition, pos_out, fit_to_content=False,
        page_size=(win.sheet_w, win.sheet_h))
    win.composition.shapes = saved_shapes
    for p, label in zip(win.composition.placements, saved_labels):
        p.label = label
    doc = fitz.open(str(pos_out))
    try:
        pix = doc[0].get_pixmap(dpi=72)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n)[:, :, :3].astype(int)
    finally:
        doc.close()

    for idx, placement in enumerate(win.composition.placements):
        colour = figure_colour(
            fixture, placement.figure.page_index, placement.figure.region)
        mask = ((np.abs(arr[:, :, 0].astype(int) - colour[0]) < 40)
                & (np.abs(arr[:, :, 1].astype(int) - colour[1]) < 40)
                & (np.abs(arr[:, :, 2].astype(int) - colour[2]) < 40))
        ys, xs = np.where(mask)
        rect = fitz.Rect(placement.rect)
        if not len(xs):
            check(f"figure {idx} reached the exported page", False,
                  f"colour {colour} not found")
            continue
        # The figure keeps its aspect ratio inside the box it was dropped
        # into, so it is fitted rather than stretched.
        fw, fh = comp.figure_display_size(placement.figure)
        fitted = comp.fit_rect(fw / fh if fh else 1.0, rect)
        expect_x0 = fitted.x0
        expect_x1 = fitted.x1
        expect_y0 = win.sheet_h - fitted.y1
        expect_y1 = win.sheet_h - fitted.y0
        check(f"figure {idx} sits where it was dropped",
              abs(xs.min() - expect_x0) < 3 and abs(xs.max() - expect_x1) < 3
              and abs(ys.min() - expect_y0) < 3 and abs(ys.max() - expect_y1) < 3,
              f"got {xs.min()}..{xs.max()}/{ys.min()}..{ys.max()}, "
              f"want {expect_x0:.0f}..{expect_x1:.0f}/"
              f"{expect_y0:.0f}..{expect_y1:.0f}")
    _shot(win, "05_final.png")

    win.destroy()

    print()
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    print(f"screenshots in {SHOTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
