"""The font files behind the names the compositor offers.

The canvas draws with the faces Windows has -- Times New Roman, Arial,
Courier New, Symbol, SimSun -- because Tk only knows the fonts the system
offers.  The export has to carry **the very same letters**, or the figure the
reader sees is not the one the user arranged: a built-in "Times-Roman" is
drawn by the reader with whatever Times it happens to have, and that is not
the Times New Roman the canvas showed.  A paper set in Times New Roman then
ends up with captions in a different face.

So every name is tied to the file that holds it, and that file is embedded in
the output.  A missing file is not fatal: the export falls back to the
built-in face, which is what it did before.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

#: The face a name belongs to, and the style the name itself carries.
#: The name is the one the compositor lists; the face is the family the file
#: is looked up in.
FACE_OF: Dict[str, Tuple[str, bool, bool]] = {
    "Times-Roman": ("Times", False, False),
    "Times-Bold": ("Times", True, False),
    "Times-Italic": ("Times", False, True),
    "Times-Bold Italic": ("Times", True, True),
    "Helvetica": ("Helvetica", False, False),
    "Helvetica-Bold": ("Helvetica", True, False),
    "Helvetica-Oblique": ("Helvetica", False, True),
    "Courier": ("Courier", False, False),
    "Courier-Bold": ("Courier", True, False),
    "Symbol": ("Symbol", False, False),
    "ZapfDingbats": ("ZapfDingbats", False, False),
    "宋体 (中文)": ("宋体", False, False),
}

#: The files that hold each face, one per style.
FONT_FILES: Dict[str, Dict[str, str]] = {
    "Times": {
        "regular": "times.ttf",
        "bold": "timesbd.ttf",
        "italic": "timesi.ttf",
        "bolditalic": "timesbi.ttf",
    },
    "Helvetica": {
        "regular": "arial.ttf",
        "bold": "arialbd.ttf",
        "italic": "ariali.ttf",
        "bolditalic": "arialbi.ttf",
    },
    "Courier": {
        "regular": "cour.ttf",
        "bold": "courbd.ttf",
        "italic": "couri.ttf",
        "bolditalic": "courbi.ttf",
    },
    "Symbol": {"regular": "symbol.ttf"},
    "ZapfDingbats": {"regular": "webdings.ttf"},
    # SimSun has no bold of its own: Windows fakes it, and so does the
    # canvas, which is close enough for a caption.
    "宋体": {"regular": "simsun.ttc"},
}

#: The face Chinese text is written in when the chosen one cannot hold it.
CJK_FACE = "宋体"

#: The style keys, in the order they are looked for.
_STYLES = ("bolditalic", "bold", "italic", "regular")


def font_dirs() -> list:
    """The directories the system keeps its fonts in."""
    dirs = []
    windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
    if windir:
        dirs.append(Path(windir) / "Fonts")
    if sys.platform == "darwin":
        dirs.append(Path("/System/Library/Fonts"))
        dirs.append(Path("/Library/Fonts"))
    elif sys.platform.startswith("linux"):
        dirs.append(Path("/usr/share/fonts/truetype"))
        dirs.append(Path("/usr/share/fonts"))
    home = os.environ.get("USERPROFILE") or os.environ.get("HOME")
    if home:
        dirs.append(Path(home) / ".fonts")
        dirs.append(Path(home) / ".local/share/fonts")
    return dirs


#: The lookups already made, so the disk is not searched twice.
_CACHE: Dict[str, Optional[str]] = {}


def _find(file_name: str) -> Optional[str]:
    """The path of a font file, or ``None`` when the system has none."""
    if file_name in _CACHE:
        return _CACHE[file_name]
    found = None
    for directory in font_dirs():
        candidate = directory / file_name
        if candidate.is_file():
            found = str(candidate)
            break
    _CACHE[file_name] = found
    return found


def style_of(name: str, bold: bool = False, italic: bool = False) -> str:
    """The style a name and the user's switches amount to.

    A name that carries its own style -- "Times-Bold" -- keeps it: the face
    is what it is, and the canvas draws it that way too.
    """
    _, own_bold, own_italic = FACE_OF.get(name, (name, False, False))
    if own_bold and own_italic:
        return "bolditalic"
    if own_bold:
        return "bold"
    if own_italic:
        return "italic"
    if bold and italic:
        return "bolditalic"
    if bold:
        return "bold"
    if italic:
        return "italic"
    return "regular"


def face_file(name: str, bold: bool = False, italic: bool = False
              ) -> Optional[str]:
    """The file that holds a face, or ``None`` when the system has none.

    Args:
        name: The face name the compositor lists.
        bold: Whether the user asked for the bold variant.
        italic: Whether the user asked for the italic variant.

    Returns:
        The path of the font file, or ``None``.
    """
    face = FACE_OF.get(name, (name, False, False))[0]
    files = FONT_FILES.get(face)
    if not files:
        return None
    style = style_of(name, bold, italic)
    # A face without a file of this style falls back to its regular one.
    for key in (style,) + _STYLES:
        file_name = files.get(key)
        if file_name:
            found = _find(file_name)
            if found:
                return found
    return None


def cjk_file() -> Optional[str]:
    """The file that holds Chinese text, or ``None``."""
    return face_file(CJK_FACE)


#: Which file holds each of the built-in faces.
BASE14_TIMES: Dict[str, str] = {
    "Times-Roman": "times.ttf",
    "Times-Bold": "timesbd.ttf",
    "Times-Italic": "timesi.ttf",
    "Times-BoldItalic": "timesbi.ttf",
}

#: The file that holds each of the built-in faces.
BASE14_FILES: Dict[str, str] = {
    "Times-Roman": "times.ttf",
    "Times-Bold": "timesbd.ttf",
    "Times-Italic": "timesi.ttf",
    "Times-BoldItalic": "timesbi.ttf",
    "Helvetica": "arial.ttf",
    "Helvetica-Bold": "arialbd.ttf",
    "Helvetica-Oblique": "ariali.ttf",
    "Helvetica-BoldOblique": "arialbi.ttf",
    "Courier": "cour.ttf",
    "Courier-Bold": "courbd.ttf",
    "Courier-Oblique": "couri.ttf",
    "Courier-BoldOblique": "courbi.ttf",
    "Symbol": "symbol.ttf",
    "ZapfDingbats": "webdings.ttf",
}


def file_for_base14(basefont: str) -> Optional[str]:
    """The file that holds a built-in face, or ``None``.

    The built-in faces have no file of their own -- they are the ones the
    PDF reader draws with whatever Times it has.  This maps them onto the
    Windows file that holds the same face.
    """
    file_name = BASE14_FILES.get(basefont or "")
    if not file_name:
        return None
    return _find(file_name)


def has_glyphs(path: str, text: str) -> bool:
    """Whether a font file can render every character of ``text``."""
    if not path:
        return False
    try:
        import pymupdf as fitz

        font = fitz.Font(fontfile=path)
    except Exception:  # noqa: BLE001 -- an unreadable file has no glyphs
        return False
    return all(font.has_glyph(ord(ch)) for ch in text)


__all__ = [
    "BASE14_FILES",
    "BASE14_TIMES",
    "CJK_FACE",
    "FACE_OF",
    "FONT_FILES",
    "cjk_file",
    "face_file",
    "file_for_base14",
    "font_dirs",
    "has_glyphs",
    "style_of",
]
