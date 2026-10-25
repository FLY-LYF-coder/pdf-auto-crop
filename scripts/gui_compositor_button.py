"""Switch the main window between cropping and composing, and check it.

The compositor used to live in a window of its own; it is a panel in the
same window now, so the mode switch is the only way into it.  It gets its
own check: the switch really swaps the content, and the two halves do not
share a grid cell.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:  # pragma: no cover
    pass

from PIL import ImageGrab  # noqa: E402

import tkinter as tk  # noqa: E402

from pdf_auto_crop import gui  # noqa: E402

SHOTS = ROOT / "diag_out" / "gui"
SHOTS.mkdir(parents=True, exist_ok=True)

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'ok ' if condition else 'FAIL'}] {label}"
          f"{(' -> ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


def _grab(win, path: Path) -> None:
    """Save a picture of the window.

    Tk reports the geometry in logical pixels and ImageGrab grabs physical
    ones, so on a scaled display the two have to be reconciled -- otherwise
    the picture is the top-left corner, magnified.
    """
    bbox = None
    try:
        import ctypes
        from ctypes import wintypes

        hwnd = ctypes.windll.user32.GetAncestor(win.winfo_id(), 2)  # GA_ROOT
        ctypes.windll.user32.SetThreadDpiAwarenessContext(
            ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
        rect = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        bbox = (rect.left, rect.top, rect.right, rect.bottom)
    except Exception:  # noqa: BLE001 -- the picture is a nicety
        x, y = win.winfo_rootx(), win.winfo_rooty()
        w, h = win.winfo_width(), win.winfo_height()
        bbox = (x, y, x + w, y + h)
    try:
        ImageGrab.grab(bbox=bbox).save(str(path))
    except OSError as exc:
        # A locked or switched-away desktop cannot be grabbed; the checks
        # do not need the picture.
        print(f"(no screenshot for {path.name}: {exc})")


def main() -> int:
    app = gui.App()
    app.geometry("1320x880+30+30")
    app.update()

    # The toolbar buttons must not share a grid cell: two widgets in one cell
    # sit on top of each other and only the last one can be clicked.
    for bar in (app.bar, app._crop_tools):
        cells = {}
        for child in bar.winfo_children():
            info = child.grid_info()
            key = (info.get("row"), info.get("column"))
            cells.setdefault(key, []).append(child)
        overlaps = {k: v for k, v in cells.items() if len(v) > 1}
        check("no two toolbar widgets share a cell", not overlaps,
              str(sorted(overlaps)))

    # -- switching to the compositor
    app._open_compositor()
    app.update()
    check("the mode switches to compose", app._mode == "compose", app._mode)
    check("the cropping content is put away",
          app._crop_body.winfo_manager() == "",
          app._crop_body.winfo_manager())
    check("the cropping tools are put away",
          app._crop_tools.winfo_manager() == "",
          app._crop_tools.winfo_manager())
    panel = app._compose_panel
    check("the compose panel is there", panel is not None)
    if panel is not None:
        check("it has the palette, the sheet and the properties panel",
              hasattr(panel, "lst_palette") and hasattr(panel, "canvas")
              and hasattr(panel, "var_label"))
        check("it is packed into the same window",
              panel.winfo_manager() != "", panel.winfo_manager())
    toplevels = [w for w in app.winfo_children()
                 if isinstance(w, tk.Toplevel)]
    check("no second window is opened", not toplevels,
          f"{len(toplevels)} toplevel(s)")
    app.update()
    _grab(app, SHOTS / "compositor_from_button.png")

    # -- and back again
    app.var_mode.set("crop")
    app._on_mode_changed()
    app.update()
    check("the mode switches back to cropping", app._mode == "crop", app._mode)
    check("the cropping content comes back",
          app._crop_body.winfo_manager() != "",
          app._crop_body.winfo_manager())
    check("the compose panel is put away",
          app._compose_panel.winfo_manager() == "",
          app._compose_panel.winfo_manager())

    app.update()
    _grab(app, SHOTS / "main_window.png")

    app.destroy()
    print()
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
