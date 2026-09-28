"""Entry point for ``python -m pdf_auto_crop``.

Two front-ends are available:

* **图形界面** -- a tkinter window (``python -m pdf_auto_crop.gui``).  Run it
  without arguments below to get there.
* **命令行向导** -- the original terminal wizard.

For scripted use, pass arguments explicitly::

    python -m pdf_auto_crop.cli --input input.pdf

The choice is only offered when there is an interactive terminal; when stdin
is redirected the terminal wizard runs directly, so existing automation that
calls this module keeps its old behaviour.
"""

from __future__ import annotations

import sys

from . import ui_utils
from .interactive import run_wizard


def _gui_available() -> bool:
    """True when the optional GUI dependencies can be imported."""
    try:
        import tkinter  # noqa: F401
        from PIL import ImageTk  # noqa: F401
    except ImportError:
        return False
    return True


def _ask_mode() -> str:
    """Ask which front-end to launch.  Returns ``"gui"`` or ``"wizard"``."""
    if not _gui_available():
        return "wizard"
    if not sys.stdin.isatty():
        return "wizard"

    ui_utils.section("PDF Auto Crop")
    print(f"  {ui_utils.colorize('[1]', ui_utils.C.CYAN)} 图形界面"
          f"  {ui_utils.colorize('(推荐：先预览再导出)', ui_utils.C.DIM)}")
    print(f"  {ui_utils.colorize('[2]', ui_utils.C.CYAN)} 命令行向导")
    print()
    try:
        raw = ui_utils.prompt_line("请选择", default="1")
    except (EOFError, KeyboardInterrupt):
        print()
        return "wizard"
    return "wizard" if raw.strip() == "2" else "gui"


def main() -> int:
    ui_utils.enable_ansi()
    try:
        if _ask_mode() == "gui":
            from .gui import launch

            return launch()
        return run_wizard()
    except KeyboardInterrupt:
        print()
        ui_utils.warn("Interrupted by user.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
