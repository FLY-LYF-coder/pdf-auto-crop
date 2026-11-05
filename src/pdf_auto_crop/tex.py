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
from typing import List, Optional, Tuple

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

    The delimiters are the user's, not mathtext's: ``\\[...\\]`` and
    ``$$...$$`` are taken off before the text is handed over -- mathtext
    would draw the brackets as letters, which is not what they mean.
    """
    body = (text or "").strip()
    if not body:
        return ""
    if body.startswith("\\[") and body.endswith("\\]"):
        body = body[2:-2].strip()
    elif body.startswith("$$") and body.endswith("$$"):
        body = body[2:-2].strip()
    if not body:
        return ""
    if body.startswith("$") and body.endswith("$"):
        return body
    return f"${body}$"


def looks_like_math(text: str) -> bool:
    """Whether a text holds mathematics.

    A formula is written between dollar signs, and it is recognised on
    sight: the user does not have to say that the text is mathematics.
    The formula does not have to be the whole text -- "公式：$x^2$" holds
    mathematics as well.

    Args:
        text: The text the user wrote.

    Returns:
        Whether the text is written like a formula.
    """
    body = (text or "").strip()
    if len(body) > 2 and body.startswith("$") and body.endswith("$"):
        return True
    if body.startswith("\\[") and body.endswith("\\]"):
        return True
    return any(is_math for _, is_math in split_math(body))


def _has_cjk(text: str) -> bool:
    """Whether the text holds Chinese characters.

    Matplotlib's mathtext cannot write them -- it puts a dummy symbol in
    their place -- so a stretch of text that holds them is ordinary text,
    even when it is between dollar signs.
    """
    return any("一" <= ch <= "鿿" for ch in text)


def split_math(text: str) -> List[Tuple[str, bool]]:
    """The text in parts: ordinary text and mathematics.

    "公式：$\\times 10$" comes back as ``("公式：", False)`` and
    ``("$\\times 10$", True)``.  The two kinds are drawn differently: the
    ordinary text is written as text, in the face the user chose, and the
    mathematics is typeset.

    A formula that is written between ``\\[`` and ``\\]`` stands on its own,
    so it is one part.
    """
    body = (text or "").strip()
    if not body:
        return []
    if body.startswith("\\[") and body.endswith("\\]"):
        return [(body, True)]
    parts: List[Tuple[str, bool]] = []
    pos = 0
    while pos < len(body):
        start = body.find("$", pos)
        if start < 0:
            break
        end = body.find("$", start + 1)
        if end < 0:
            break
        if start > pos:
            parts.append((body[pos:start], False))
        span = body[start:end + 1]
        # Chinese between the dollar signs is ordinary text: mathtext
        # cannot write it.
        parts.append((span, not _has_cjk(span)))
        pos = end + 1
    if pos < len(body):
        parts.append((body[pos:], False))
    return [(t, is_math) for t, is_math in parts if t]


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


def math_parts(text: str, size: float = 12.0
               ) -> Optional[List[Tuple[str, bool]]]:
    """The parts of a text that holds mathematics, when they all render.

    Returns ``None`` when the text holds no mathematics, or when the
    mathematics cannot be typeset -- "价格 $5 和 $10" is ordinary text with
    a dollar sign in it, not a formula.
    """
    parts = split_math(text)
    if not any(is_math for _, is_math in parts):
        return None
    for body, is_math in parts:
        if is_math and render_math(body, size) is None:
            return None
    return parts


def math_available() -> bool:
    """Whether formulas can be rendered on this machine."""
    try:
        import matplotlib  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


__all__ = ["MATH_FONTSET", "looks_like_math", "math_available", "render_math"]
