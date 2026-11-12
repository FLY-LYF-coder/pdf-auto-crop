"""The keyboard shortcuts of the compositor.

Every button that is used often enough ought to have a key as well, and the
user has to be able to find out which key does what.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import tkinter as tk  # noqa: E402
import pymupdf as fitz  # noqa: E402

from pdf_auto_crop import compositor as comp  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _panel():
    """A panel with a few objects on the sheet."""
    from pdf_auto_crop import compositor_gui

    try:
        root = tk.Tk()
    except tk.TclError:  # pragma: no cover -- headless box
        pytest.skip("no display")
    panel = compositor_gui.ComposePanel(root)
    # The panel has to be on the screen: a window that is not mapped gets
    # no key events at all.
    panel.pack(fill=tk.BOTH, expand=True)
    root.update()
    figure = comp.Figure(path=Path("probe.pdf"), page_index=0,
                         region=fitz.Rect(0, 0, 10, 10))
    panel.composition.placements.extend([
        comp.Placement(figure=figure, rect=fitz.Rect(40, 0, 140, 50)),
        comp.Placement(figure=figure, rect=fitz.Rect(0, 60, 40, 140)),
    ])
    panel.composition.shapes.append(comp.Shape(
        comp.SHAPE_LINE, fitz.Point(0, 160), fitz.Point(50, 160)))
    panel.composition.texts.append(comp.TextItem(
        text="a note", position=fitz.Point(0, 200)))
    return root, panel


def _press(panel, keysym: str, state: int = 0) -> None:
    """Press a key on the canvas, the way the user does."""
    # Tk hands a key to the widget that has the focus, so the canvas has to
    # have it -- otherwise the key goes to the window itself.  The focus is
    # forced: a window that was closed after the text box had the focus
    # leaves the new one without any, and this is a test, not a user.
    panel.canvas.focus_force()
    panel.update()
    panel.canvas.event_generate(f"<KeyPress-{keysym}>", state=state)
    panel.update()


#: The three modifier bits Tk uses.
CTRL = 0x0004
SHIFT = 0x0001


# ---------------------------------------------------------------------------
# The table of shortcuts
# ---------------------------------------------------------------------------


def test_the_common_commands_have_a_shortcut() -> None:
    """The buttons that are used every day can be reached by the keyboard."""
    from pdf_auto_crop import compositor_gui

    labels = {s.label for s in compositor_gui.SHORTCUTS}
    assert {"全选", "复制一份", "保存布局", "打开布局", "导入 PDF", "导出 PDF",
            "同宽", "同高", "等大"} <= labels
    # Every shortcut points at a method that really exists.
    for shortcut in compositor_gui.SHORTCUTS:
        assert hasattr(compositor_gui.ComposePanel, shortcut.method), shortcut


def test_the_tools_have_a_shortcut() -> None:
    """The tools that are used the most can be picked by a key."""
    from pdf_auto_crop import compositor_gui

    kinds = {tool for _key, tool, _label in compositor_gui.TOOL_KEYS}
    assert "select" in kinds
    assert comp.SHAPE_LINE in kinds
    assert comp.SHAPE_RECT in kinds


def test_no_two_shortcuts_use_the_same_key() -> None:
    """Two commands cannot share a key: the second would never be reached."""
    from pdf_auto_crop import compositor_gui

    seen = set()
    for shortcut in compositor_gui.SHORTCUTS:
        assert shortcut.sequence not in seen, shortcut.sequence
        seen.add(shortcut.sequence)
    for key, _tool, _label in compositor_gui.TOOL_KEYS:
        assert key not in seen, key
        seen.add(key)


# ---------------------------------------------------------------------------
# The keys themselves
# ---------------------------------------------------------------------------


def test_a_letter_picks_the_tool() -> None:
    """Pressing "l" picks the line tool."""
    root, panel = _panel()
    try:
        _press(panel, "l")
        assert panel.tool == comp.SHAPE_LINE
        _press(panel, "v")
        assert panel.tool == "select"
    finally:
        panel.destroy()
        root.destroy()


def test_a_letter_does_not_pick_the_tool_while_the_user_is_typing() -> None:
    """A letter written into the text box is a letter, not a shortcut."""
    root, panel = _panel()
    try:
        panel.nb.select(panel.tab_text)
        panel.txt_text.focus_set()
        panel.update()
        panel.txt_text.insert("1.0", "l")
        panel.update()
        assert panel.tool != comp.SHAPE_LINE
    finally:
        panel.destroy()
        root.destroy()


def test_control_a_picks_everything() -> None:
    """Ctrl+A picks every object on the sheet."""
    root, panel = _panel()
    try:
        _press(panel, "a", CTRL)
        assert panel._sel_figs == [0, 1]
        assert panel._sel_shapes == [0]
        assert panel._sel_texts == [0]
    finally:
        panel.destroy()
        root.destroy()


def test_control_shift_d_makes_them_the_same_size() -> None:
    """Ctrl+Shift+D is 等大: the picked things all get the reference's size."""
    root, panel = _panel()
    try:
        panel._sel_figs = [0, 1]
        panel._last_picked = ("figure", 1)
        _press(panel, "D", CTRL | SHIFT)
        assert panel.composition.placements[0].rect.width == pytest.approx(40.0)
        assert panel.composition.placements[1].rect.width == pytest.approx(40.0)
    finally:
        panel.destroy()
        root.destroy()


def test_control_shift_left_lines_them_up() -> None:
    """Ctrl+Shift+Left is 左对齐."""
    root, panel = _panel()
    try:
        panel._sel_figs = [0, 1]
        _press(panel, "Left", CTRL | SHIFT)
        assert panel.composition.placements[0].rect.x0 == pytest.approx(0.0)
        assert panel.composition.placements[1].rect.x0 == pytest.approx(0.0)
    finally:
        panel.destroy()
        root.destroy()

def test_a_shortcut_of_a_dialog_does_not_reach_the_sheet() -> None:
    """A key pressed in a window of its own is not a shortcut of the sheet."""
    root, panel = _panel()
    try:
        dialog = tk.Toplevel(root)
        entry = tk.Entry(dialog)
        entry.pack()
        entry.focus_set()
        panel.update()
        entry.event_generate("<KeyPress-a>", state=CTRL)
        panel.update()
        assert panel._sel_figs == []
    finally:
        root.destroy()


def test_the_list_of_shortcuts_can_be_shown() -> None:
    """The user has to be able to find out which key does what."""
    root, panel = _panel()
    try:
        panel._show_shortcuts()
        panel.update()
        windows = [w for w in root.winfo_children()
                   if isinstance(w, tk.Toplevel)]
        assert windows, "the list of the shortcuts did not open"
        labels = []
        for child in windows[0].winfo_children():
            for grandchild in child.winfo_children():
                if grandchild.winfo_class().endswith("Label"):
                    labels.append(grandchild.cget("text"))
        text = "".join(labels)
        assert "Ctrl" in text
        assert "全选" in text
    finally:
        panel.destroy()
        root.destroy()
