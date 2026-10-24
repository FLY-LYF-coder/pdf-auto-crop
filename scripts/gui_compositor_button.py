"""Open the compositor from the main window's toolbar button and check it.

The button is the only way into the compositor for a user, so it gets its own
check: the window really opens, and the button is not sitting on top of
another one.

Run:  python scripts/gui_compositor_button.py
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


def main() -> int:
    app = gui.App()
    app.geometry("1320x880+30+30")
    app.update()

    # The toolbar buttons must not share a grid cell: two widgets in one cell
    # sit on top of each other and only the last one can be clicked.
    bar = app.bar
    cells = {}
    for child in bar.winfo_children():
        info = child.grid_info()
        key = (info.get("row"), info.get("column"))
        cells.setdefault(key, []).append(child)
    overlaps = {k: v for k, v in cells.items() if len(v) > 1}
    check("no two toolbar widgets share a cell", not overlaps,
          str(sorted(overlaps)))

    before = len(app.winfo_children())
    app._open_compositor()
    app.update()
    toplevels = [w for w in app.winfo_children()
                 if isinstance(w, tk.Toplevel)]
    check("the compositor window opens", len(toplevels) >= 1,
          f"{len(toplevels)} toplevel(s)")
    if toplevels:
        win = toplevels[0]
        check("it is the compose window",
              type(win).__name__ == "ComposeWindow", type(win).__name__)
        check("it has the palette, the sheet and the properties panel",
              hasattr(win, "lst_palette") and hasattr(win, "canvas")
              and hasattr(win, "var_label"))
        app.update()
        x, y = win.winfo_rootx(), win.winfo_rooty()
        w, h = win.winfo_width(), win.winfo_height()
        ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(
            str(SHOTS / "compositor_from_button.png"))
        win.destroy()
    else:
        check("it is the compose window", False, "no window opened")

    app.update()
    x, y = app.winfo_rootx(), app.winfo_rooty()
    w, h = app.winfo_width(), app.winfo_height()
    ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(str(SHOTS / "main_window.png"))

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
