"""Making the window draw at the display's real resolution.

Windows bitmap-scales a process that does not say it understands high-DPI
screens.  The result is blurry text, and -- worse -- themed widgets (the
notebook's tabs, the combo boxes, the spin boxes) laid out for one
resolution and drawn at another, so their labels are cut through the
middle.

Asking for the real resolution before any window exists fixes both: Tk then
knows the true pixel-per-point ratio, and everything is measured and drawn at
the same scale.

On other platforms this is a no-op: they have no such virtualisation.
"""

from __future__ import annotations

import sys

#: Process DPI awareness, as understood by ``SetProcessDpiAwareness``.
DPI_AWARENESS_SYSTEM_AWARE = 1
DPI_AWARENESS_PER_MONITOR_AWARE = 2

_done = False


def enable_dpi_awareness() -> bool:
    """Tell Windows that this process draws at the display's real size.

    Returns:
        Whether the request went through.  It cannot: on a machine without
        high-DPI support, or when a window already exists.  Neither is
        fatal -- the window is just less crisp.
    """
    global _done
    if _done:
        return True
    _done = True
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(
                ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
            return True
        except AttributeError:
            pass
        try:
            return ctypes.windll.shcore.SetProcessDpiAwareness(
                DPI_AWARENESS_PER_MONITOR_AWARE) == 0
        except AttributeError:
            pass
        try:
            return bool(ctypes.windll.user32.SetProcessDPIAware())
        except AttributeError:
            return False
    except Exception:  # noqa: BLE001 -- a cosmetic problem, never fatal
        return False


def window_dpi(window=None) -> float:
    """The resolution of the display the window is on, in dots per inch.

    Tk 8.6 reads the screen's resolution once, at start-up, and never
    notices that the display is a high-DPI one -- so it keeps drawing in
    96 dpi metrics.  The window itself knows better: Windows tells it
    exactly how many dots its monitor has.
    """
    if window is None or sys.platform != "win32":
        return 96.0
    try:
        import ctypes

        hwnd = ctypes.windll.user32.GetAncestor(window.winfo_id(), 2)  # GA_ROOT
        value = ctypes.windll.user32.GetDpiForWindow(hwnd)
        if value:
            return float(value)
    except Exception:  # noqa: BLE001 -- a cosmetic problem, never fatal
        pass
    return 96.0


def apply_tk_scaling(window) -> float:
    """Teach Tk the real resolution of the display, and return it.

    Tk measures points and screen units against the resolution it read at
    start-up.  Left alone, a window on a 200 % display is drawn with
    96 dpi metrics: the text is small and the themed widgets are laid out
    for one resolution and drawn at another.  Setting the scaling puts
    both back in step.
    """
    value = window_dpi(window)
    try:
        window.tk.call("tk", "scaling", value / 72.0)
    except Exception:  # noqa: BLE001
        pass
    return value


def scale_factor(root=None) -> float:
    """How many physical pixels a logical pixel is worth on this display.

    A process that draws at the real resolution gets its geometry in
    physical pixels, so a window meant to be 1240 points wide has to be
    1240 * scale pixels wide to come out the same size on the screen.

    Args:
        root: The widget to ask; the default root is used when omitted.
    """
    try:
        import tkinter as tk

        widget = root if root is not None else getattr(tk, "_default_root", None)
        if widget is None:
            return 1.0
        dpi = widget.winfo_fpixels("1i")
        if not dpi:
            return 1.0
        return max(1.0, dpi / 96.0)
    except Exception:  # noqa: BLE001 -- a cosmetic problem, never fatal
        return 1.0


def pixels_per_point(root=None) -> float:
    """How many physical pixels a point of paper is worth here.

    A point is 1/72 inch, so this is the display's resolution divided by
    72.
    """
    try:
        import tkinter as tk

        widget = root if root is not None else getattr(tk, "_default_root", None)
        if widget is None:
            return 1.0
        dpi = widget.winfo_fpixels("1i")
        if not dpi:
            return 1.0
        return max(1.0, dpi / 72.0)
    except Exception:  # noqa: BLE001
        return 1.0


__all__ = ["apply_tk_scaling", "enable_dpi_awareness", "pixels_per_point",
           "scale_factor", "window_dpi"]
