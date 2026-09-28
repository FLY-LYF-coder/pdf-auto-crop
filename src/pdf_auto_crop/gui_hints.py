"""Detection feedback rules shared by the GUI.

This module is deliberately **free of any tkinter import** so the advice engine
can be unit tested on a headless machine, and so the same rules could be reused
by a future CLI ``--explain`` mode.

The problem it solves: the cropper has several parameters that interact, and a
blank "0 figures found" tells the user nothing about which one to change.  The
legacy wizard made this worse by asking the user to tune ``merge_gap_mm``,
which the layout-adaptive detector no longer consults whenever it can measure
real gaps.  These rules only ever suggest parameters that are actually live:

===========================  =========================================
Parameter                    Live in the smart detector?
===========================  =========================================
``threshold``                yes
``dpi``                      yes
``min_area_mm2``             yes
``min_width_mm``             yes
``min_height_mm``            yes
``margin_mm`` / ``padding``  yes (output only)
``merge_gap_mm``             fallback only -- no inline suggestion
``morph_kernel_multiplier``  no -- never suggested
===========================  =========================================
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Optional, Sequence, Tuple

from .config import CropConfig

#: Severity levels, ordered from most to least serious.
LEVEL_ERROR = "error"
LEVEL_WARN = "warn"
LEVEL_OK = "ok"

#: A single figure is considered to have swallowed the page above this
#: fraction of the page area.
PAGE_SWALLOW_RATIO = 0.90

#: More figures than this, combined with small ones, means the page is
#: fragmenting rather than being read as separate figures.
FRAGMENT_MIN_COUNT = 5

#: ...where "small" means the median figure's *shorter side* is under this many
#: millimetres.  An absolute bound is deliberate: comparing against the page
#: instead would flag every legitimate 2x2 sub-figure grid, where each panel
#: really is only a few percent of an A4 sheet.
FRAGMENT_MEDIAN_SIDE_MM = 25.0

#: A single fix is never allowed to move a parameter further than this, so a
#: surprising detection cannot nuke the user's settings in one click.
MAX_STEP = {"threshold": 15, "min_area_mm2": 200.0, "min_width_mm": 6.0, "min_height_mm": 6.0}


@dataclass(frozen=True)
class Hint:
    """One piece of feedback about a detection result.

    Attributes:
        level: One of :data:`LEVEL_ERROR`, :data:`LEVEL_WARN`, :data:`LEVEL_OK`.
        title: Short headline, safe for a status bar.
        detail: One or two sentences explaining what happened and why.
        fixes: ``(config_field, new_value)`` pairs the GUI can offer as
            one-click actions.  Empty when no safe automatic fix exists.
    """

    level: str
    title: str
    detail: str
    fixes: Tuple[Tuple[str, Any], ...] = ()

    @property
    def is_actionable(self) -> bool:
        """True when at least one clickable fix is attached."""
        return bool(self.fixes)


@dataclass(frozen=True)
class PageAnalysis:
    """Result of inspecting one page's detection."""

    figure_count: int
    hints: Tuple[Hint, ...]

    @property
    def level(self) -> str:
        """Worst level across all hints."""
        for level in (LEVEL_ERROR, LEVEL_WARN):
            for hint in self.hints:
                if hint.level == level:
                    return level
        return LEVEL_OK

    @property
    def headline(self) -> str:
        """Single-line summary suitable for a status bar."""
        if not self.hints:
            return ""
        return self.hints[0].title

    def suggested_config(self, config: CropConfig) -> Optional[CropConfig]:
        """Return ``config`` with the first actionable fix applied.

        Returns ``None`` when nothing actionable was found.  Values are
        clamped so a single click can never move a parameter further than
        :data:`MAX_STEP`, and the result is re-validated.
        """
        for hint in self.hints:
            if not hint.fixes:
                continue
            updates = dict(hint.fixes)
            merged = config.to_dict()
            for field, value in updates.items():
                current = merged.get(field)
                step = MAX_STEP.get(field)
                if step is None or not isinstance(current, (int, float)):
                    merged[field] = value
                    continue
                # Always move in the direction of the suggestion, capped.
                if value > current:
                    merged[field] = min(value, current + step)
                else:
                    merged[field] = max(value, current - step)
            candidate = CropConfig.from_dict(merged)
            try:
                candidate.validate()
            except ValueError:
                return None
            return candidate
        return None


def _rect_area_mm2(rect: Any) -> float:
    """Area of a :class:`fitz.Rect` in square millimetres."""
    return abs(rect.width * rect.height) * (25.4 / 72.0) ** 2


def analyze_page(
    *,
    figure_count: int,
    has_ink: bool,
    page_area_mm2: float,
    rects: Sequence[Any] = (),
    config: Optional[CropConfig] = None,
) -> PageAnalysis:
    """Inspect one page's detection and return user-facing feedback.

    Args:
        figure_count: How many figures the detector returned.
        has_ink: Whether the rendered page contained any ink at all.  This
            separates "blank page" from "detection failed".
        page_area_mm2: Page area in mm^2, used for the relative-size checks.
        rects: The detected rectangles (anything exposing ``.width``/``.height``
            in PDF points, i.e. :class:`fitz.Rect`).
        config: Active config, used to compute sensible suggested values.

    Returns:
        A :class:`PageAnalysis`.  The ``hints`` tuple is ordered most serious
        first and is empty when the result looks healthy.
    """
    config = config or CropConfig()
    hints: list[Hint] = []

    if not has_ink:
        return PageAnalysis(
            figure_count,
            (
                Hint(
                    LEVEL_WARN,
                    "这一页是空白的",
                    "页面没有任何内容，检测不到图片属于正常结果。",
                ),
            ),
        )

    if figure_count == 0:
        lower = max(120, config.threshold - 10)
        hints.append(
            Hint(
                LEVEL_ERROR,
                "没有检测到任何图片",
                "页面有内容但没找到图片边界。最常见的原因是线条或文字太浅，"
                "被二值化阈值当成了背景；也可以适当调小最小面积门槛。",
                fixes=(("threshold", lower),),
            )
        )
        return PageAnalysis(figure_count, tuple(hints))

    sides = [
        min(abs(r.width), abs(r.height)) * (25.4 / 72.0) for r in rects
    ] or [0.0]
    sides_sorted = sorted(sides)
    median_side = sides_sorted[len(sides_sorted) // 2]
    largest = max(_rect_area_mm2(r) for r in rects) if rects else 0.0

    if figure_count >= FRAGMENT_MIN_COUNT and median_side < FRAGMENT_MEDIAN_SIDE_MM:
        hints.append(
            Hint(
                LEVEL_WARN,
                f"疑似切得太碎（{figure_count} 个区域）",
                "检出的区域普遍很小，可能把一张图里的坐标轴、图例或标注"
                "当成了独立的图。调大最小面积和最小边长可以过滤掉这些碎片。",
                fixes=(
                    ("min_area_mm2", config.min_area_mm2 * 1.8),
                    ("min_width_mm", config.min_width_mm * 1.5),
                    ("min_height_mm", config.min_height_mm * 1.5),
                ),
            )
        )

    if page_area_mm2 > 0 and largest > page_area_mm2 * PAGE_SWALLOW_RATIO:
        if figure_count == 1:
            hints.append(
                Hint(
                    LEVEL_WARN,
                    "整页被当成了 1 张图",
                    "检出的框几乎覆盖整页。如果这一页本来就只有一张大图，"
                    "这是正常的；如果本来是多张图紧贴在一起，说明它们在版面上"
                    "没有留出可分辨的空白，需要你手动把它们拉开一点。",
                )
            )
        else:
            hints.append(
                Hint(
                    LEVEL_WARN,
                    "有一个区域几乎覆盖整页",
                    "这通常意味着某个区域把整页内容都圈了进去，"
                    "可以检查一下预览图里红色框的位置是否正确。",
                )
            )

    return PageAnalysis(figure_count, tuple(hints))


def summarise_figures(rects: Sequence[Any], limit: int = 4) -> str:
    """One-line size summary for the status bar, e.g. ``80x60, 80x60, ...``.

    Args:
        rects: Detected rectangles in PDF points.
        limit: Maximum number of sizes to spell out before eliding.
    """
    if not rects:
        return ""
    to_mm = 25.4 / 72.0
    sizes = [f"{abs(r.width) * to_mm:.0f}x{abs(r.height) * to_mm:.0f}" for r in rects]
    shown = sizes[:limit]
    if len(sizes) > limit:
        shown.append(f"... 共{len(sizes)}个")
    return ", ".join(shown) + " mm"


__all__ = [
    "LEVEL_ERROR",
    "LEVEL_WARN",
    "LEVEL_OK",
    "Hint",
    "PageAnalysis",
    "analyze_page",
    "summarise_figures",
]
