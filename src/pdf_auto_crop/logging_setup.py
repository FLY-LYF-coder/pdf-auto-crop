"""Logging helpers for pdf-auto-crop.

Centralised so the CLI and library code share a consistent format.  The
default format is short enough to remain readable when the tool is wired into
Make / shell scripts.
"""

from __future__ import annotations

import logging
import sys
from typing import Optional

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%H:%M:%S"


def configure_logging(level: str = "INFO", *, quiet: bool = False) -> logging.Logger:
    """Configure root logger for the ``pdf_auto_crop`` namespace.

    Args:
        level: Logging level name (case-insensitive).  ``"DEBUG"`` is verbose.
        quiet: When ``True`` only ``WARNING`` and above are shown.
    """
    numeric_level = getattr(logging, level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"Invalid log level: {level!r}")
    effective = logging.WARNING if quiet else numeric_level

    root_logger = logging.getLogger("pdf_auto_crop")
    root_logger.setLevel(effective)
    root_logger.propagate = False

    # Reset any handlers previously installed (e.g. on re-invocation).
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    stream = logging.StreamHandler(stream=_safe_stderr())
    stream.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    stream.setLevel(effective)
    root_logger.addHandler(stream)

    # Silence chatty PyMuPDF info messages unless user explicitly asked for DEBUG.
    if numeric_level > logging.DEBUG:
        logging.getLogger("pymupdf").setLevel(logging.WARNING)

    return root_logger


def _safe_stderr():
    """Return a stderr stream that survives Windows console encoding issues.

    Delegates to :func:`pdf_auto_crop.ui_utils._ensure_utf8_stream` so the
    same defensive UTF-8 reconfiguration is shared between logging and
    progress-bar output.
    """
    from .ui_utils import _ensure_utf8_stream

    return _ensure_utf8_stream(sys.stderr)


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Return a child logger under the ``pdf_auto_crop`` namespace."""
    full_name = "pdf_auto_crop" if name is None else f"pdf_auto_crop.{name}"
    return logging.getLogger(full_name)


__all__ = ["configure_logging", "get_logger"]