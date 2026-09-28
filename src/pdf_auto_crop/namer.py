"""Filename generation strategies for cropped output figures.

Two strategies are provided:

* :func:`per_page_name` -- ``figure_p<page>_f<fig>.pdf`` (default).  Useful
  when users want the layout to mirror the original PDF.
* :func:`flat_name` -- ``figure_<global_idx>.pdf`` (zero-padded).  Useful when
  figures will be re-assembled later and order is more important than origin.
"""

from __future__ import annotations

__all__ = ["per_page_name", "flat_name"]


def per_page_name(
    page_idx: int,
    fig_idx: int,
    prefix: str = "figure",
) -> str:
    """Return a filename like ``figure_p01_f02.pdf``.

    Args:
        page_idx: Zero-based source page index.
        fig_idx: Zero-based figure index on that page.
        prefix: Filename stem.
    """
    if page_idx < 0:
        raise ValueError("page_idx must be non-negative")
    if fig_idx < 0:
        raise ValueError("fig_idx must be non-negative")
    return f"{prefix}_p{page_idx + 1:02d}_f{fig_idx + 1:02d}.pdf"


def flat_name(global_idx: int, prefix: str = "figure") -> str:
    """Return a filename like ``figure_001.pdf``.

    Args:
        global_idx: Zero-based figure index across the whole document.
        prefix: Filename stem.
    """
    if global_idx < 0:
        raise ValueError("global_idx must be non-negative")
    return f"{prefix}_{global_idx + 1:03d}.pdf"