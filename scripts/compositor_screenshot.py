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
import time
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
import tkinter as tk  # noqa: E402
import tkinter.font as tkfont  # noqa: E402
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


def _settle(win) -> None:
    """Let the window finish laying out before anything is clicked.

    The canvas is only the right size after the window manager has had a
    turn, and a click that lands before that misses the figure.
    """
    for _ in range(5):
        win.update()
        time.sleep(0.05)
    win._redraw()
    win.update()


def _pick_tool(win, name: str) -> None:
    """Choose a tool the way a user does: through the dropdown.

    The dropdown's event carries an argument, so calling the handler
    directly would not exercise the same path -- and a handler that cannot
    take the argument fails exactly where the user is looking.
    """
    win.var_tool.set(name)
    win.cmb_tool.event_generate("<<ComboboxSelected>>")
    win.update()


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
    _settle(win)
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

    # -- hovering over a palette entry shows the figure -------------------
    # The name of a file says nothing about the picture in it.
    box = win.lst_palette.bbox(0)
    hover_y = box[1] + box[3] // 2
    win.lst_palette.event_generate("<Motion>", x=10, y=hover_y)
    win.update()
    preview = win._preview_win
    check("hovering over a palette entry shows a preview",
          preview is not None and win._preview_index == 0,
          str(win._preview_index))
    if preview is not None:
        shown = preview.winfo_width(), preview.winfo_height()
        check("the preview shows the picture",
              shown[0] > 10 and shown[1] > 10, str(shown))
        preview.destroy()
        win._preview_win = None
        win._preview_photo = None
        win._preview_index = -1
    win.lst_palette.event_generate("<Leave>", x=10, y=hover_y)
    win.update()
    check("the preview goes when the cursor leaves",
          win._preview_win is None, str(win._preview_win))

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
    # The key event goes to whatever has the focus, and a screenshot in
    # between can take it away from the canvas -- so the focus is forced
    # onto the canvas itself.
    try:
        win.canvas.focus_force()
    except tk.TclError:
        pass
    before_y = win.composition.placements[1].rect.y0
    win.canvas.event_generate("<Up>", when="now")
    win.update()
    after_y = win.composition.placements[1].rect.y0
    check("the up arrow nudges the figure up",
          abs((after_y - before_y) - 1.0) < 1e-6,
          f"{after_y - before_y:.3f}")
    before_y = after_y
    win.canvas.event_generate("<Down>", when="now")
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
    win.cmb_label_font.event_generate("<<ComboboxSelected>>")
    win.update()
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

    # The label on the canvas has to sit where the exporter puts it: the
    # baseline on the anchor.  A label drawn a descender too high is a
    # label the reader will not find where the user left it.
    anchor = placement.label_anchor()
    ax, ay = win._to_canvas(anchor.x, anchor.y)
    label_item = None
    for item in win.canvas.find_withtag("fig"):
        if win.canvas.type(item) != "text":
            continue
        if abs(win.canvas.bbox(item)[0] - ax) <= 4:
            label_item = item
            break
    if label_item is None:
        check("the label is drawn on its anchor", False, "no text item found")
    else:
        spec = win.canvas.itemcget(label_item, "font")
        actual = tkfont.Font(root=win, font=spec).actual()
        descent = compositor_gui.font_descent_px(actual)
        baseline = win.canvas.bbox(label_item)[3] - descent
        check("the label is drawn on its anchor",
              abs(baseline - ay) <= 1.5,
              f"baseline {baseline:.1f} vs anchor {ay:.1f}")
        check("the label is drawn in the face the export carries",
              actual["family"] == compositor_gui.TK_FONTS["Helvetica-Bold"],
              f"{actual['family']}")
    _shot(win, "06_label_style.png")

    # -- every callback has to take what Tk hands it ------------------------
    # A dropdown's event carries an argument; a handler that cannot take
    # it fails exactly where the user is looking, and the control silently
    # does nothing.
    callbacks = [
        ("the tool dropdown", win.cmb_tool, win._on_tool_changed),
        ("the label font dropdown", win.cmb_label_font, win._on_label_changed),
        ("the text font dropdown", win.cmb_text_font, win._on_text_changed),
        ("the dash dropdown", win.cmb_shape_dash, win._on_shape_changed),
    ]
    for name, widget, handler in callbacks:
        try:
            handler(object())        # what Tk passes a virtual event
            ok = True
        except TypeError as exc:
            ok = False
            detail = str(exc)
        else:
            detail = ""
        check(f"{name} accepts the event Tk sends", ok, detail)

    # -- drawing a graphic element ---------------------------------------
    # The coordinates are worked out from the sheet, so the test does not
    # depend on the window's size or the display's resolution.
    sheet = win._canvas_rect(fitz.Rect(0, 0, win.sheet_w, win.sheet_h))
    sx, sy, ex, ey = sheet
    sw, sh = ex - sx, ey - sy

    def at(fx: float, fy: float) -> tuple[int, int]:
        return (int(sx + sw * fx), int(sy + sh * fy))

    n_shapes = len(win.composition.shapes)
    _pick_tool(win, "箭头")
    check("the arrow tool is active", win.tool == "arrow", win.tool)
    _drag(win, *at(0.60, 0.12), *at(0.78, 0.28))
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

    # A click alone is enough: the element is placed with a sensible size.
    _pick_tool(win, "方框")
    n_shapes = len(win.composition.shapes)
    _click(win, *at(0.20, 0.80))
    check("a click places an element",
          len(win.composition.shapes) == n_shapes + 1,
          f"{len(win.composition.shapes)} shapes")
    if len(win.composition.shapes) > n_shapes:
        placed = win.composition.shapes[-1]
        check("the placed element has a size",
              placed.rect.width > 1 and placed.rect.height > 1,
              f"{placed.rect}")
        check("the placed element is on the sheet",
              0 <= placed.rect.x0 and placed.rect.x1 <= win.sheet_w
              and 0 <= placed.rect.y0 and placed.rect.y1 <= win.sheet_h,
              f"{placed.rect}")
        # Take it away again, so the checks that follow are not disturbed.
        win._clear_selection()
        win._selected_shape = len(win.composition.shapes) - 1
        win._delete_selected()
        win.update()

    # A box, a circle and a triangle, to show the other tools work too.
    for tool, fx0, fy0, fx1, fy1 in (("方框", 0.60, 0.36, 0.78, 0.48),
                                     ("圆圈", 0.60, 0.52, 0.78, 0.64),
                                     ("三角形", 0.60, 0.68, 0.78, 0.80)):
        _pick_tool(win, tool)
        _drag(win, *at(fx0, fy0), *at(fx1, fy1))
    check("all the graphic tools drew something",
          len(win.composition.shapes) == n_shapes + 3,
          f"{len(win.composition.shapes)} shapes")
    drawn = win.composition.shapes[-1]
    check("a new element takes the style of the panel",
          drawn.fill_color == pytest.approx((1.0, 0.878, 0.541), abs=0.02)
          and drawn.dash == comp.DASH_SOLID
          and drawn.stroke_opacity == pytest.approx(1.0),
          f"{drawn.fill_color} {drawn.dash} {drawn.stroke_opacity}")
    _shot(win, "07_shapes.png")

    # -- moving a graphic element ----------------------------------------
    _pick_tool(win, "选择/移动")
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
    win.cmb_shape_dash.event_generate("<<ComboboxSelected>>")
    win.update()
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
    _pick_tool(win, "选择/移动")
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
    win._clear_selection()
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
    win._clear_selection()
    win._sel_figs = [0, 1]
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
    rect_before = fitz.Rect(placement.rect)
    # Grab the label where it is *drawn*, the way a person does: the ink has
    # to be where the grab area is, or the label cannot be caught at all.
    anchor = placement.label_anchor()
    drawn_x, drawn_y = win._to_canvas(anchor.x, anchor.y)
    grab_x = int(drawn_x + placement.label_size * win._zoom / 2)
    grab_y = int(drawn_y - placement.label_size * 0.6 * win._zoom)
    check("the label can be caught where it is drawn",
          win._hit_label(grab_x, grab_y) == 0,
          str(win._hit_label(grab_x, grab_y)))
    _drag(win, grab_x, grab_y, grab_x + 60, grab_y - 40)
    check("the label can be dragged off the figure",
          placement.label_mode == comp.LABEL_FREE, placement.label_mode)
    check("the dragged label moved",
          abs(placement.label_offset[0] - before_offset[0]) > 10,
          f"{before_offset} -> {placement.label_offset}")
    check("dragging the label leaves the figure where it was",
          rect_before == fitz.Rect(placement.rect),
          f"{rect_before} vs {placement.rect}")
    _shot(win, "10_free_label.png")

    # -- the built-in places for a label ------------------------------------
    for mode, name in ((comp.LABEL_TOP_LEFT, "左上"),
                       (comp.LABEL_BOTTOM_RIGHT, "右下"),
                       (comp.LABEL_OVERLAY, "左下")):
        win.var_label_mode.set(mode)
        win._on_label_mode_changed()
        win.update()
        check(f"the label can be put {name}",
              placement.label_mode == mode, placement.label_mode)
    win.var_label_mode.set(comp.LABEL_BELOW)
    win._on_label_mode_changed()
    win.update()

    # -- dropping a piece of text ------------------------------------------
    # The texts go to the far right of the sheet, where no figure is: a
    # colour test on the panels would otherwise find them.
    _pick_tool(win, "文本")
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
    _pick_tool(win, "文本框")
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

    # -- aligning a mixed selection -----------------------------------------
    # A figure, a box and a note, all lined up against each other.
    win._clear_selection()
    win._sel_figs = [0]
    win._sel_shapes = [0]
    win._sel_texts = [0]
    win._redraw()
    win._align(comp.ALIGN_LEFT)
    win.update()
    fig_x = win.composition.placements[0].rect.x0
    shape_x = win.composition.shapes[0].rect.x0
    text_x = win.composition.texts[0].rect().x0
    check("a figure, a box and a note can be lined up together",
          abs(fig_x - shape_x) < 0.01 and abs(fig_x - text_x) < 0.01,
          f"{fig_x:.1f} {shape_x:.1f} {text_x:.1f}")

    # -- turning an element ------------------------------------------------
    _pick_tool(win, "选择/移动")
    win._clear_selection()
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

    win._clear_selection()
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
    win._clear_selection()
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
        # The face in the export has to be the face the canvas shows: the
        # file that holds "Helvetica-Bold" here is the Windows Arial Bold.
        expected_font = ""
        from pdf_auto_crop import fonts as font_files

        face_file = font_files.face_file("Helvetica-Bold")
        if face_file:
            expected_font = fitz.Font(fontfile=face_file).name
        check("the exported label uses the chosen font",
              a_span and (expected_font in a_span[0]["font"]
                          if expected_font else "Helvetica" in a_span[0]["font"]),
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

    # -- making one figure the size of another ------------------------------
    win._clear_selection()
    win._sel_figs = [0, 1]
    win.composition.placements[0].rect = fitz.Rect(100, 100, 200, 150)
    win.composition.placements[1].rect = fitz.Rect(300, 300, 500, 400)
    win._match_size("width")
    win.update()
    a = win.composition.placements[0].rect
    b = win.composition.placements[1].rect
    check("a figure can be made the width of another",
          abs(a.width - b.width) < 0.5, f"{a.width} vs {b.width}")
    win._match_size("height")
    win.update()
    a = win.composition.placements[0].rect
    check("a figure can be made the height of another",
          abs(a.height - b.height) < 0.5, f"{a.height} vs {b.height}")

    # -- an array of the selected figures -----------------------------------
    win._clear_selection()
    win._sel_figs = [0, 1, 2]
    win._apply_array(2, 2)
    win.update()
    placed = [win.composition.placements[i].rect for i in (0, 1, 2)]
    check("the figures can be laid out in an array",
          len({round(r.x0, 1) for r in placed}) == 2
          and len({round(r.y0, 1) for r in placed}) == 2,
          str([(round(r.x0), round(r.y0)) for r in placed]))

    # -- lining the labels up on their own ----------------------------------
    win._clear_selection()
    win._sel_labels = [0, 1]
    win._align(comp.ALIGN_LEFT)
    win.update()
    x0 = comp.free_label_rect(win.composition.placements[0]).x0
    x1 = comp.free_label_rect(win.composition.placements[1]).x0
    check("the labels can be lined up on their own",
          abs(x0 - x1) < 0.01, f"{x0} vs {x1}")

    # -- a graphic element can be made the size of another ------------------
    if len(win.composition.shapes) >= 2:
        win._clear_selection()
        win._sel_shapes = [0, 1]
        win._match_size("width")
        first = win.composition.shapes[0].rect
        second = win.composition.shapes[1].rect
        check("two graphic elements can be made the same width",
              abs(first.width - second.width) < 0.5,
              f"{first.width:.1f} vs {second.width:.1f}")
        win._sel_shapes = [0, 1]
        win._match_size("both")
        first = win.composition.shapes[0].rect
        second = win.composition.shapes[1].rect
        check("two graphic elements can be made the same size",
              abs(first.width - second.width) < 0.5
              and abs(first.height - second.height) < 0.5,
              f"{first} vs {second}")
        win._clear_selection()

    # -- a dragged figure snaps to its neighbour ----------------------------
    if len(win.composition.placements) >= 2:
        win._selected = 0
        win._sync_properties()
        first = win.composition.placements[0]
        second = win.composition.placements[1]
        # Put the second one a little to the right of the first.
        second.rect = fitz.Rect(second.rect.x0 + 40, second.rect.y0,
                                 second.rect.x1 + 40, second.rect.y1)
        win._redraw()
        win.update()
        gap = second.rect.x0 - first.rect.x1
        # Drag the first one so that its right edge would land a hair short
        # of the second one's left edge.
        target = second.rect.x0 - first.rect.width
        cx, cy = win._to_canvas((first.rect.x0 + first.rect.x1) / 2,
                                (first.rect.y0 + first.rect.y1) / 2)
        want_x = win._to_canvas(target + first.rect.width / 2,
                                (first.rect.y0 + first.rect.y1) / 2)[0]
        _drag(win, int(cx), int(cy), int(want_x), int(cy))
        moved = win.composition.placements[0].rect
        check("a dragged figure snaps to its neighbour",
              abs(moved.x1 - second.rect.x0) < 0.5,
              f"the gap is {second.rect.x0 - moved.x1:.2f} pt")

    # -- undo and redo ------------------------------------------------------
    if win.composition.placements:
        size_before = win.composition.placements[0].label_size
        win._selected = 0
        win._sync_properties()
        # Setting the variable fires the change handler, which remembers the
        # sheet as it was.
        win.var_label_size.set("21")
        win.update()
        check("the label size can be changed",
              abs(win.composition.placements[0].label_size - 21.0) < 1e-6,
              str(win.composition.placements[0].label_size))
        # The key goes to whatever has the focus.
        win.canvas.focus_set()
        try:
            win.canvas.focus_force()
        except tk.TclError:
            pass
        win.canvas.event_generate("<Control-z>")
        win.update()
        check("Ctrl+Z takes the change back",
              abs(win.composition.placements[0].label_size - size_before) < 1e-6,
              str(win.composition.placements[0].label_size))
        win.canvas.event_generate("<Control-y>")
        win.update()
        check("Ctrl+Y does it again",
              abs(win.composition.placements[0].label_size - 21.0) < 1e-6,
              str(win.composition.placements[0].label_size))
        win.canvas.event_generate("<Control-z>")
        win.update()

    # -- the sheet's edge follows the mouse ---------------------------------
    height_before = win.sheet_h
    handles = win._sheet_handle_positions(
        win._origin[0], win._origin[1],
        win.sheet_w * win._zoom, win.sheet_h * win._zoom)
    hx, hy = handles[1]          # the grip on the top edge
    _drag(win, int(hx), int(hy), int(hx), int(hy) - 30)
    check("pulling the top edge up makes the sheet taller",
          win.sheet_h > height_before,
          f"{height_before:.0f} -> {win.sheet_h:.0f}")
    check("the bottom edge of the sheet stays where it was",
          abs(win.sheet_y) < 0.5, f"{win.sheet_y:.2f}")
    width_before = win.sheet_w
    handles = win._sheet_handle_positions(
        win._origin[0], win._origin[1],
        win.sheet_w * win._zoom, win.sheet_h * win._zoom)
    hx, hy = handles[7]          # the grip on the left edge
    _drag(win, int(hx), int(hy), int(hx) - 30, int(hy))
    check("pulling the left edge out makes the sheet wider",
          win.sheet_w > width_before,
          f"{width_before:.0f} -> {win.sheet_w:.0f}")
    check("the right edge of the sheet stays where it was",
          abs(win.sheet_x + win.sheet_w - width_before) < 0.5,
          f"{win.sheet_x + win.sheet_w:.2f} vs {width_before:.2f}")

    # -- the sheet can be resized by its grips ------------------------------
    width_before = win.sheet_w
    handles = win._sheet_handle_positions(
        win._origin[0], win._origin[1],
        win.sheet_w * win._zoom, win.sheet_h * win._zoom)
    hx, hy = handles[3]          # the grip on the right-hand edge
    _drag(win, int(hx), int(hy), int(hx) + 40, int(hy))
    check("the sheet can be made wider by its grips",
          win.sheet_w > width_before,
          f"{width_before:.0f} -> {win.sheet_w:.0f}")

    # -- a click is not a drag ---------------------------------------------
    # A hand does not hold still: a click comes with a pixel or two of
    # travel, and at the sheet's zoom that is enough to nudge a figure out
    # of the layout the user has just arranged.
    win._selected = 0
    win._sync_properties()
    placement = win.composition.placements[0]
    before = fitz.Rect(placement.rect)
    cx, cy = win._to_canvas((before.x0 + before.x1) / 2,
                            (before.y0 + before.y1) / 2)
    canvas = win.canvas
    canvas.event_generate("<Button-1>", x=int(cx), y=int(cy))
    win.update()
    canvas.event_generate("<B1-Motion>", x=int(cx) + 1, y=int(cy))
    win.update()
    canvas.event_generate("<ButtonRelease-1>", x=int(cx) + 1, y=int(cy))
    win.update()
    check("a click does not move the figure",
          fitz.Rect(placement.rect) == before,
          f"{tuple(round(v, 2) for v in before)} -> "
          f"{tuple(round(v, 2) for v in placement.rect)}")

    # -- the sheet takes the size of the content, and the content comes with
    # it: left where it was, it would end up outside the page, and the first
    # click on a figure would pull it back in.
    win.var_sheet.set("适应内容")
    win._on_sheet_changed()
    win.update()
    bounds = win.composition.content_bounds()
    margin = win.composition.margin_pt
    check("the sheet takes the size of the content",
          bounds is not None
          and abs(win.sheet_w - bounds.width - 2 * margin) < 1.0
          and abs(win.sheet_h - bounds.height - 2 * margin) < 1.0,
          f"{win.sheet_w:.1f}x{win.sheet_h:.1f}")
    check("the content comes with the sheet",
          all(0 <= p.rect.x0 and p.rect.x1 <= win.sheet_w + 1
              and 0 <= p.rect.y0 and p.rect.y1 <= win.sheet_h + 1
              for p in win.composition.placements),
          str([tuple(round(v, 1) for v in p.rect)
               for p in win.composition.placements]))
    win.var_sheet.set("A4 横向")
    win._on_sheet_changed()
    win.update()

    # -- taking a figure out of the palette -------------------------------
    # Last, because it takes a figure off the sheet.
    n_palette = len(win.palette)
    win.lst_palette.selection_clear(0, "end")
    win.lst_palette.selection_set(0)
    n_placements = len(win.composition.placements)
    win._remove_from_palette()
    win.update()
    check("a palette entry can be removed",
          len(win.palette) == n_palette - 1,
          f"{len(win.palette)} entries")
    check("the figure it had on the sheet goes with it",
          len(win.composition.placements) < n_placements,
          f"{len(win.composition.placements)} figures")

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
