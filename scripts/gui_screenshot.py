"""Launch the GUI headlessly-but-really and capture a screenshot.

Constructs the real :class:`~pdf_auto_crop.gui.App`, drives an analysis
through the same code path the buttons use, grabs the window with
``PIL.ImageGrab`` and saves it.  This is the only way to confirm the widget
tree actually builds and paints on a real desktop.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# The Windows console defaults to GBK, which cannot encode the check mark the
# UI shows; force UTF-8 so the printed report is readable.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:  # pragma: no cover
    pass

from PIL import ImageGrab  # noqa: E402

from pdf_auto_crop import gui  # noqa: E402
from pdf_auto_crop.config import CropConfig  # noqa: E402


def _grab(win, path) -> None:
    """Save a picture of the window, if the screen allows it.

    A locked or switched-away desktop cannot be grabbed; the checks do not
    need the picture, so it must not stop the run.
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
    except Exception:  # noqa: BLE001
        x, y = win.winfo_rootx(), win.winfo_rooty()
        w, h = win.winfo_width(), win.winfo_height()
        bbox = (x, y, x + w, y + h)
    try:
        ImageGrab.grab(bbox=bbox).save(str(path))
    except OSError as exc:
        print(f"(no screenshot for {path}: {exc})")

SHOTS = ROOT / "diag_out" / "gui"
SHOTS.mkdir(parents=True, exist_ok=True)


def _pump(app, seconds: float = 6.0) -> None:
    """Let the worker thread finish and the UI repaint."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.update()
        if not app._bg.busy:
            break
        time.sleep(0.05)
    for _ in range(6):
        app.update()
        time.sleep(0.05)


def main() -> int:
    fixture = ROOT / "tests" / "fixtures_diag" / "D3_big_left_4right.pdf"
    app = gui.App()
    app.geometry("1280x860+40+40")
    app.update()

    app._set_source(fixture)
    app.var_output.set(str(SHOTS / "out"))
    app._analyse()
    _pump(app)
    app.update()

    x, y = app.winfo_rootx(), app.winfo_rooty()
    w, h = app.winfo_width(), app.winfo_height()
    shot = SHOTS / "gui_main.png"
    _grab(app, shot)
    print(f"figures detected: {len(app._rects)}")
    print(f"status: {app.lbl_status.cget('text')}")
    print(f"hint  : {app.lbl_hint_title.cget('text')}")
    print(f"footer: {app.lbl_figures.cget('text')}")
    print(f"saved : {shot}")

    # Now exercise the failure path: a page that yields nothing useful.
    blank_cfg = CropConfig(min_area_mm2=9000.0, min_width_mm=90.0, min_height_mm=90.0)
    app._fields["min_area_mm2"].set("9000")
    app._fields["min_width_mm"].set("90")
    app._fields["min_height_mm"].set("90")
    app._analyse()
    _pump(app)
    app.update()
    shot2 = SHOTS / "gui_fragmented.png"
    _grab(app, shot2)
    print(f"fragmented hint: {app.lbl_hint_title.cget('text')}")
    print(f"saved : {shot2}")

    app._on_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
