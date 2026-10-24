"""Rendering LaTeX mathematics as vector PDF.

Matplotlib ships **mathtext**, a small TeX-like typesetter that needs no
TeX installation.  It understands the parts of LaTeX a figure caption
actually uses: fractions, roots, sums and integrals, sub- and
superscripts, Greek letters, matrices, and the usual operators.

The formula is rendered into a one-page PDF with TrueType fonts embedded
(``pdf.fonttype = 42``), so the result is **vector** and the text stays
selectable -- the same properties the rest of the composition keeps.

Matplotlib is an *optional* dependency: when it is missing, :func:`render_math`
returns ``None`` and the caller falls back to plain text.
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Optional

#: The font set mathtext uses.  "stix" is the Times-like set, which matches
#: the body font of most papers.
MATH_FONTSET = "stix"

#: Where the rendered formulas are cached.
CACHE_DIR = Path(tempfile.gettempdir()) / "pdf_auto_crop_math"


def _math_source(text: str) -> str:
    """Wrap the text in ``$...$`` unless the user already did it.

    Matplotlib's mathtext treats ``$...$`` as mathematics and everything
    else as ordinary text, so a formula typed without the dollars would
    come out as plain letters.
    """
    body = (text or "").strip()
    if not body:
        return ""
    if body.startswith("$") and body.endswith("$"):
        return body
    return f"${body}$"


def render_math(text: str, size: float = 12.0) -> Optional[Path]:
    """Render a formula to a single-page PDF.

    Args:
        text: The formula, with or without the surrounding ``$``.
        size: The font size in points.

    Returns:
        The path of the rendered PDF, or ``None`` when the formula cannot be
        rendered -- no matplotlib, or a formula mathtext does not understand.
        The file is cached, so the same formula is rendered only once.
    """
    source = _math_source(text)
    if not source:
        return None

    key = hashlib.sha256(
        f"{source}\x00{size:.4f}\x00{MATH_FONTSET}".encode("utf-8")
    ).hexdigest()[:24]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CACHE_DIR / f"math_{key}.pdf"
    if out_path.exists():
        return out_path

    try:
        import matplotlib

        matplotlib.use("Agg")
        from matplotlib.backends.backend_pdf import FigureCanvasPdf
        from matplotlib.figure import Figure
    except Exception:  # noqa: BLE001 -- matplotlib is optional
        return None

    try:
        matplotlib.rcParams["mathtext.fontset"] = MATH_FONTSET
        # 42 embeds TrueType instead of Type 3, so the formula stays real
        # text rather than a set of glyph procedures.
        matplotlib.rcParams["pdf.fonttype"] = 42
        fig = Figure(figsize=(0.01, 0.01))
        FigureCanvasPdf(fig)
        fig.text(0.0, 0.0, source, fontsize=size, color=(0.0, 0.0, 0.0))
        fig.savefig(
            str(out_path),
            format="pdf",
            bbox_inches="tight",
            pad_inches=0.0,
        )
    except Exception:  # noqa: BLE001 -- a formula mathtext cannot parse
        return None
    finally:
        try:
            fig.clf()
        except Exception:  # noqa: BLE001
            pass
    return out_path if out_path.exists() else None


def math_available() -> bool:
    """Whether formulas can be rendered on this machine."""
    try:
        import matplotlib  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


__all__ = ["MATH_FONTSET", "math_available", "render_math"]
