"""pdf-auto-crop: Automatically crop multi-figure layouts from scientific paper PDFs.

This package provides tooling to split a single PDF page (or multi-page document)
containing several figures (typical of figures exported from PowerPoint) into
individual single-figure PDFs while **preserving vector content** via the PDF
CropBox mechanism rather than rasterisation.

Public API entry points:
    - ``pdf_auto_crop.cli``  -- command-line interface.
    - ``pdf_auto_crop.gui``  -- tkinter front-end with a manual crop editor.
    - ``pdf_auto_crop.config.CropConfig``  -- configuration dataclass.
    - ``pdf_auto_crop.smart_detector.smart_detect_figures``  -- default,
      layout-adaptive algorithm.
    - ``pdf_auto_crop.detector.detect_figures``  -- original fixed-parameter
      algorithm, kept for ``--algorithm legacy``.
    - ``pdf_auto_crop.gui_edit.ManualBoxStore``  -- per-page rectangles drawn
      by hand in the GUI; the export combines them with the detector's result.
"""

from __future__ import annotations

from typing import Any

__version__ = "0.3.0"

#: Names re-exported lazily so that ``import pdf_auto_crop`` stays cheap (the
#: detectors pull in OpenCV and NumPy, which the lightweight config/history
#: helpers have no use for).
_LAZY_EXPORTS = {
    "smart_detect_figures": "pdf_auto_crop.smart_detector",
    "detect_figures": "pdf_auto_crop.detector",
    "CropConfig": "pdf_auto_crop.config",
    "ManualBoxStore": "pdf_auto_crop.gui_edit",
}

__all__ = ["__version__", *sorted(_LAZY_EXPORTS)]


def __getattr__(name: str) -> Any:
    """Resolve a re-exported symbol on first access (PEP 562)."""
    module_path = _LAZY_EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    from importlib import import_module

    return getattr(import_module(module_path), name)


def __dir__() -> list:
    return sorted(__all__)