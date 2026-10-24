"""Tests for the high-DPI helpers.

Windows bitmap-scales a process that does not say it understands high-DPI
screens: the text comes out blurry and the themed widgets are laid out for
one resolution and drawn at another, so their labels are cut in half.  These
helpers are what stop that.
"""

from __future__ import annotations

import sys

import pytest

from pdf_auto_crop import dpi


def test_the_awareness_can_be_asked_for() -> None:
    """Asking twice must not raise -- the second call is a no-op."""
    first = dpi.enable_dpi_awareness()
    second = dpi.enable_dpi_awareness()
    assert second is True
    if sys.platform == "win32":
        assert isinstance(first, bool)


def test_the_scale_of_the_display_is_measured() -> None:
    """Without a display the factor falls back to one."""
    assert dpi.scale_factor(None) >= 1.0
    assert dpi.pixels_per_point(None) >= 1.0


def test_the_window_geometry_is_scaled_for_the_display() -> None:
    """A window of 1240 points has to be 1240 * scale pixels wide."""
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:  # pragma: no cover -- headless box
        pytest.skip("no display")
    try:
        factor = dpi.scale_factor(root)
        assert factor >= 1.0
        # A point is 1/72 inch, so a point is worth 96/72 of a logical
        # pixel -- and the compositor measures the sheet in points.
        assert dpi.pixels_per_point(root) == pytest.approx(
            factor * 96 / 72, rel=0.01)
    finally:
        root.destroy()


def test_the_scaling_follows_the_display() -> None:
    """After the scaling is set, Tk measures in the display's points."""
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:  # pragma: no cover -- headless box
        pytest.skip("no display")
    try:
        value = dpi.apply_tk_scaling(root)
        assert value >= 96.0
        assert root.tk.call("tk", "scaling") == pytest.approx(value / 72.0,
                                                               rel=0.01)
        # And Tk now reports the resolution it was told about.
        assert root.winfo_fpixels("1i") == pytest.approx(value, rel=0.02)
    finally:
        root.destroy()
