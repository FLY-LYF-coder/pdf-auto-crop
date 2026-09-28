"""Configuration primitives for pdf-auto-crop.

A single :class:`CropConfig` dataclass carries every tunable parameter.  All
length-style parameters are expressed in **millimetres** so the values remain
intuitive regardless of the render DPI.  Internally the detection code
translates them to pixels using :mod:`pdf_auto_crop.image_processor`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
import json
from pathlib import Path
from typing import Any, Dict


@dataclass(frozen=True)
class CropConfig:
    """User-facing configuration controlling figure detection.

    Every parameter has a sensible default; supplying ``--config path/to.json``
    on the CLI will override the defaults with values loaded from that file.
    """

    #: Extra white-space (mm) added around each detected figure before saving.
    margin_mm: float = 3.0

    #: Pixel brightness threshold (0-255) for the binarisation step.  Pixels
    #: brighter than this are treated as background.  ``245`` matches typical
    #: "white" coming out of PDF renderers.
    threshold: int = 245

    #: Render DPI used when rasterising PDF pages for analysis.  Higher DPI
    #: gives finer boundaries at the cost of memory & CPU.
    dpi: int = 250

    #: Components smaller than this many square millimetres are discarded.
    min_area_mm2: float = 25.0

    #: Components narrower than this many millimetres are discarded.
    min_width_mm: float = 10.0

    #: Components shorter than this many millimetres are discarded.
    min_height_mm: float = 10.0

    #: Multiplier applied to the adaptive close-kernel size.  Increase this if
    #: thin-line figures get fragmented, decrease if distinct figures merge.
    morph_kernel_multiplier: float = 1.0

    #: Two components whose dilated rectangles overlap (after expanding by this
    #: many mm on every side) are merged into a single figure.
    merge_gap_mm: float = 1.5

    #: Optional extra padding (mm) applied to each saved crop in addition to
    #: :attr:`margin_mm`.  Useful when the user wants extra whitespace for
    #: further manual editing.
    padding_mm: float = 0.0

    #: File-naming strategy passed to :mod:`pdf_auto_crop.namer`.  ``"per-page"``
    #: produces ``figure_p01_f02.pdf``; ``"flat"`` produces ``figure_001.pdf``.
    naming: str = "per-page"

    #: Detection algorithm.  ``"smart"`` (default) adapts the closing kernel
    #: and the merge threshold to each page's measured layout; ``"legacy"``
    #: restores the original fixed-parameter detector.
    algorithm: str = "smart"

    #: Filename prefix used by the namer.
    output_prefix: str = "figure"

    def validate(self) -> None:
        """Fail fast on nonsensical parameter combinations."""
        guard = [
            ("margin_mm", self.margin_mm, 0.0, 100.0),
            ("threshold", self.threshold, 0, 255),
            ("dpi", self.dpi, 50, 1200),
            ("min_area_mm2", self.min_area_mm2, 0.0, 10000.0),
            ("min_width_mm", self.min_width_mm, 0.0, 500.0),
            ("min_height_mm", self.min_height_mm, 0.0, 500.0),
            ("morph_kernel_multiplier", self.morph_kernel_multiplier, 0.1, 10.0),
            ("merge_gap_mm", self.merge_gap_mm, 0.0, 50.0),
            ("padding_mm", self.padding_mm, 0.0, 100.0),
        ]
        for name, value, lo, hi in guard:
            if value < lo or value > hi:
                raise ValueError(
                    f"Invalid {name}={value!r}; expected {lo} <= value <= {hi}"
                )
        if self.naming not in {"per-page", "flat"}:
            raise ValueError(
                f"Invalid naming={self.naming!r}; expected 'per-page' or 'flat'"
            )
        if self.algorithm not in {"smart", "legacy"}:
            raise ValueError(
                f"Invalid algorithm={self.algorithm!r}; expected 'smart' or 'legacy'"
            )

    def to_dict(self) -> Dict[str, Any]:
        """Return a plain ``dict`` representation (JSON-serialisable)."""
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    def write_json(self, path: Path) -> None:
        path.write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CropConfig":
        """Build a config from a dict, ignoring unknown keys."""
        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in data.items() if k in known}
        return cls(**clean)

    @classmethod
    def from_json_file(cls, path: Path) -> "CropConfig":
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError(f"Config JSON must be an object, got {type(data).__name__}")
        return cls.from_dict(data)


__all__ = ["CropConfig"]