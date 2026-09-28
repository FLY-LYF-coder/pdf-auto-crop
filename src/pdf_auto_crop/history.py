"""Persistent last-used settings stored in ``~/.pdf_auto_crop.json``.

The history file is intentionally tiny: it remembers the last input directory,
output directory, and preset name so the wizard can pre-fill defaults.  Failures
to read or write the history are non-fatal; the wizard still works without
persistent state.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional


HISTORY_FILENAME = ".pdf_auto_crop.json"


def history_path() -> Path:
    """Return the path of the history file (always under user home)."""
    return Path(os.path.expanduser("~")) / HISTORY_FILENAME


def load() -> Dict[str, Any]:
    """Load the history dict; return empty dict on any failure."""
    path = history_path()
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def save(data: Dict[str, Any]) -> None:
    """Write ``data`` to the history file; silently ignore I/O errors."""
    path = history_path()
    try:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def update(**fields: Any) -> Dict[str, Any]:
    """Merge ``fields`` into the existing history and persist it."""
    merged = {**load(), **fields}
    save(merged)
    return merged


def get(key: str, default: Any = None) -> Any:
    """Return a single field from the history; missing key returns ``default``."""
    return load().get(key, default)


def last_input_dir(default: Optional[Path] = None) -> Path:
    """Return the last input directory as a :class:`Path` if it still exists."""
    raw = get("last_input_dir")
    if isinstance(raw, str) and raw:
        candidate = Path(raw)
        if candidate.exists():
            return candidate
    return default or Path.cwd()


def last_output_dir(default: Path = Path("./output")) -> Path:
    """Return the last output directory as a :class:`Path`."""
    raw = get("last_output_dir")
    if isinstance(raw, str) and raw:
        return Path(raw)
    return default


def last_preset(default: str = "standard") -> str:
    """Return the last used preset name (default ``standard``)."""
    raw = get("last_preset")
    if isinstance(raw, str) and raw:
        return raw
    return default


__all__ = [
    "HISTORY_FILENAME",
    "history_path",
    "load",
    "save",
    "update",
    "get",
    "last_input_dir",
    "last_output_dir",
    "last_preset",
]