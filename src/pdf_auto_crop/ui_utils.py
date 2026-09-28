"""Terminal UI utilities -- ANSI colours, progress bars, prompts.

All functions in this module are deliberately small and free of business
logic.  Tests can call them in isolation without touching the rest of the
package.

Design notes
------------

* ANSI escapes are emitted as plain strings; no third-party dependency.
  Windows 10+ terminals honour them once the ``ENABLE_VIRTUAL_TERMINAL_PROCESSING``
  flag is set on the console -- :func:`enable_ansi` does this defensively.
* When stdout is not a TTY (e.g. piped into a file) colour is automatically
  suppressed so logs stay clean.
* Every interactive function accepts an optional ``prompt_input`` callable
  so tests can monkeypatch the user response without touching ``builtins``.
"""

from __future__ import annotations

import io
import os
import sys
from typing import Callable, Iterable, List, Optional, Sequence, TextIO


# ---------------------------------------------------------------------------
# Colour primitives.

class C:
    """ANSI SGR escape codes as plain strings."""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    BOLD_GREEN = "\033[1;32m"
    BOLD_RED = "\033[1;31m"
    BOLD_YELLOW = "\033[1;33m"
    BOLD_CYAN = "\033[1;36m"


_NO_COLOR = os.environ.get("NO_COLOR") is not None
_IS_TTY = sys.stdout.isatty() if hasattr(sys.stdout, "isatty") else False


def _force_color() -> bool:
    """Return True when colour should be emitted even if not a TTY.

    Honours the user-controlled ``PDF_AUTO_CROP_FORCE_COLOR=1`` escape hatch
    so tests and CI logs can produce deterministic coloured output.
    """
    return os.environ.get("PDF_AUTO_CROP_FORCE_COLOR") == "1"


def should_use_color() -> bool:
    """Decide whether to emit ANSI escape codes."""
    if _NO_COLOR:
        return False
    if _force_color():
        return True
    return _IS_TTY


def enable_ansi() -> None:
    """Enable ANSI processing on Windows consoles when possible.

    On non-Windows platforms this is a no-op.  On Windows the
    ``ENABLE_VIRTUAL_TERMINAL_PROCESSING`` flag must be set on the stdout
    handle; otherwise escape codes are rendered as literal text.
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        ENABLE_VT_PROCESSING = 0x0004
        for handle_id in (-11, -12):  # STDOUT, STDERR
            handle = kernel32.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                if not (mode.value & ENABLE_VT_PROCESSING):
                    kernel32.SetConsoleMode(handle, mode.value | ENABLE_VT_PROCESSING)
    except Exception:
        # If anything goes wrong we just silently fall back to no colour.
        pass


def _ensure_utf8_stream(stream: TextIO) -> TextIO:
    """Return ``stream`` reconfigured for UTF-8 when its current encoding
    cannot represent the unicode characters the UI uses.

    Falls back to wrapping the underlying file descriptor in a UTF-8
    ``TextIOWrapper`` if reconfiguration fails.
    """
    enc = getattr(stream, "encoding", None)
    if not enc:
        return stream
    # Use a probe character that is widely supported -- if even it fails,
    # the stream encoding cannot be used safely for our UI output.
    try:
        "\u2591".encode(enc)  # LIGHT SHADE: not in cp936/GBK
    except (UnicodeEncodeError, LookupError):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
                return stream
            except Exception:
                pass
        try:
            raw_fd = stream.fileno() if hasattr(stream, "fileno") else None
            if raw_fd is not None:
                return io.TextIOWrapper(
                    open(raw_fd, mode="w", buffering=1, closefd=False),
                    encoding="utf-8",
                    errors="replace",
                    write_through=True,
                )
        except Exception:
            pass
    return stream


def _utf8_stdout() -> TextIO:
    """Return ``sys.stdout`` configured for UTF-8 (best effort)."""
    return _ensure_utf8_stream(sys.stdout)


def colorize(text: str, color: str) -> str:
    """Wrap ``text`` in ``color`` when colour is supported."""
    if not should_use_color():
        return text
    return f"{color}{text}{C.RESET}"


def info(msg: str) -> None:
    """Print an informational message (cyan INFO prefix)."""
    print(f"{colorize('[INFO]', C.CYAN)} {msg}")


def success(msg: str) -> None:
    """Print a success message (green OK prefix)."""
    print(f"{colorize('[ OK ]', C.BOLD_GREEN)} {msg}")


def warn(msg: str) -> None:
    """Print a warning (yellow WARN prefix)."""
    print(f"{colorize('[WARN]', C.BOLD_YELLOW)} {msg}")


def error(msg: str) -> None:
    """Print an error (red ERR prefix)."""
    print(f"{colorize('[ERR ]', C.BOLD_RED)} {msg}")


def step(msg: str) -> None:
    """Print a numbered step marker (bold cyan)."""
    print(f"{colorize('->', C.BOLD_CYAN)} {msg}")


def section(title: str) -> None:
    """Print a 60-char banner header around ``title``."""
    bar = "=" * 60
    print()
    print(colorize(bar, f"{C.BOLD}{C.CYAN}"))
    print(colorize(f"  {title}", f"{C.BOLD}{C.CYAN}"))
    print(colorize(bar, f"{C.BOLD}{C.CYAN}"))
    print()


def subsection(title: str) -> None:
    """Print a lighter banner."""
    print()
    print(colorize(f"--- {title} ---", f"{C.BOLD}{C.BLUE}"))
    print()


# ---------------------------------------------------------------------------
# Progress bar.

def progress_bar(
    current: int,
    total: int,
    prefix: str = "",
    width: int = 40,
) -> None:
    """Print an in-place progress bar.

    Emits a carriage return so the next call overwrites the line.  Caller is
    responsible for emitting ``print()`` after the final call.
    """
    if total <= 0:
        return
    pct = current / total
    filled = int(width * pct)
    bar = "\u2588" * filled + "\u2591" * (width - filled)
    stream = _utf8_stdout()
    stream.write(f"\r{prefix} |{bar}| {current}/{total} ({pct * 100:.0f}%)")
    stream.flush()


# ---------------------------------------------------------------------------
# Interactive prompts.

def confirm(
    prompt: str,
    default: bool = True,
    prompt_input: Optional[Callable[[str], str]] = None,
) -> bool:
    """Ask a yes/no question and return the boolean answer."""
    suffix = "[Y/n]" if default else "[y/N]"
    raw = prompt_line(f"{prompt} {suffix}", prompt_input=prompt_input)
    if not raw:
        return default
    return raw.strip().lower() in ("y", "yes")


def prompt_line(
    prompt: str,
    default: Optional[str] = None,
    prompt_input: Optional[Callable[[str], str]] = None,
) -> str:
    """Prompt for free-form text; return the user input (or ``default``)."""
    fn = prompt_input or input
    suffix = f" [{default}]" if default is not None else ""
    return fn(f"{prompt}{suffix}: ")


def prompt_int(
    prompt: str,
    default: Optional[int] = None,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
    prompt_input: Optional[Callable[[str], str]] = None,
) -> int:
    """Prompt for an integer; re-ask on invalid input."""
    fn = prompt_input or input
    while True:
        suffix = f" [{default}]" if default is not None else ""
        raw = fn(f"{prompt}{suffix}: ").strip()
        if not raw and default is not None:
            return default
        try:
            value = int(raw)
        except ValueError:
            warn(f"'{raw}' is not an integer, try again.")
            continue
        if minimum is not None and value < minimum:
            warn(f"value must be >= {minimum}.")
            continue
        if maximum is not None and value > maximum:
            warn(f"value must be <= {maximum}.")
            continue
        return value


def prompt_float(
    prompt: str,
    default: Optional[float] = None,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
    prompt_input: Optional[Callable[[str], str]] = None,
) -> float:
    """Prompt for a float; re-ask on invalid input."""
    fn = prompt_input or input
    while True:
        suffix = f" [{default}]" if default is not None else ""
        raw = fn(f"{prompt}{suffix}: ").strip()
        if not raw and default is not None:
            return float(default)
        try:
            value = float(raw)
        except ValueError:
            warn(f"'{raw}' is not a number, try again.")
            continue
        if minimum is not None and value < minimum:
            warn(f"value must be >= {minimum}.")
            continue
        if maximum is not None and value > maximum:
            warn(f"value must be <= {maximum}.")
            continue
        return value


def menu(
    prompt: str,
    choices: Sequence[str],
    default: Optional[int] = None,
    prompt_input: Optional[Callable[[str], str]] = None,
) -> str:
    """Present a numbered menu and return the chosen item.

    Args:
        prompt: Header text.
        choices: Non-empty sequence of choice labels.
        default: 1-based default index.  ``None`` means no default.

    Returns:
        The chosen choice label (a string from ``choices``).
    """
    if not choices:
        raise ValueError("menu() requires at least one choice")
    fn = prompt_input or input
    print(colorize(prompt, C.BOLD))
    for idx, label in enumerate(choices, start=1):
        marker = "*" if default and idx == default else " "
        print(f"  {colorize(f'[{marker}{idx}]', C.CYAN)} {label}")
    while True:
        suffix = f" [{default}]" if default is not None else ""
        raw = fn(f"Choice{suffix}: ").strip()
        if not raw and default is not None:
            return choices[default - 1]
        try:
            n = int(raw)
        except ValueError:
            warn(f"'{raw}' is not a number, try again.")
            continue
        if 1 <= n <= len(choices):
            return choices[n - 1]
        warn(f"Please enter a number between 1 and {len(choices)}.")


def format_listing(items: Iterable[str], bullet: str = "  - ") -> str:
    """Format ``items`` as a multi-line string with a bullet per line."""
    return "\n".join(f"{bullet}{x}" for x in items)


__all__ = [
    "C",
    "colorize",
    "enable_ansi",
    "info",
    "success",
    "warn",
    "error",
    "step",
    "section",
    "subsection",
    "should_use_color",
    "progress_bar",
    "confirm",
    "prompt_line",
    "prompt_int",
    "prompt_float",
    "menu",
    "format_listing",
]