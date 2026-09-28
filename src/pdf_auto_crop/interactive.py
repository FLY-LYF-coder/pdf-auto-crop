"""Interactive wizard -- step-by-step guide for first-time users.

The wizard runs when no ``--input`` is supplied on the CLI.  It is also the
default mode for ``python -m pdf_auto_crop`` (which re-exports
:func:`run_wizard`).

Each individual step is exposed as a small ``_wizard_*`` function so tests
can drive them with monkeypatched ``builtins.input``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from . import history as hist
from . import ui_utils
from .batch import BatchResult, collect_pdfs, process_batch
from .config import CropConfig
from .presets import PRESETS, apply_preset, list_preset_names


PromptFn = Callable[[str], str]


# ---------------------------------------------------------------------------
# Wizard step helpers -- each returns either a value or ``None`` for "quit".

def _wizard_pick_input(
    cwd: Path,
    prompt_input: Optional[PromptFn] = None,
) -> Optional[Path]:
    """Step 1: choose an input PDF (or directory for batch).

    Lists PDFs in ``cwd``; the user can either pick a number, type a path,
    or type ``q`` to quit.
    """
    ui_utils.step("Step 1/4: Select input PDF")
    pdfs = sorted(cwd.glob("*.pdf"))
    if pdfs:
        ui_utils.info(f"Found {len(pdfs)} PDF file(s) in {cwd}:")
        for idx, p in enumerate(pdfs, start=1):
            tag = "DIR " if p.is_dir() else "FILE"
            print(f"  {ui_utils.colorize(f'[{idx}]', ui_utils.C.CYAN)} "
                  f"{ui_utils.colorize(tag, ui_utils.C.DIM)} {p.name}")
    else:
        ui_utils.warn(f"No PDFs found in {cwd}. Enter a path instead.")

    print()
    raw = ui_utils.prompt_line(
        "Choice (number, path, or 'q' to quit)",
        default="1" if pdfs else None,
        prompt_input=prompt_input,
    ).strip()
    if raw.lower() in ("q", "quit", "exit"):
        return None
    if not raw:
        return None

    # Numeric choice refers to the listing.
    if raw.isdigit():
        n = int(raw)
        if 1 <= n <= len(pdfs):
            return pdfs[n - 1].resolve()
        ui_utils.error(f"Choice {raw} out of range.")
        return None

    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = (cwd / candidate).resolve()
    if not candidate.exists():
        ui_utils.error(f"Path does not exist: {candidate}")
        return None
    return candidate


def _wizard_pick_preset(
    prompt_input: Optional[PromptFn] = None,
    default: str = "standard",
) -> Optional[str]:
    """Step 2: choose a preset (tight / standard / loose)."""
    ui_utils.step("Step 2/4: Select preset")
    choices = list_preset_names()
    pretty: list[str] = []
    for name in choices:
        info = PRESETS[name]
        pretty.append(f"{info['label']:9s} - {info['description']}")
    default_idx = choices.index(default) + 1 if default in choices else 2
    chosen = ui_utils.menu(
        "Which preset matches your layout?",
        pretty,
        default=default_idx,
        prompt_input=prompt_input,
    )
    # ``menu`` returns the pretty label; map back to the preset key.
    for name in choices:
        if chosen.startswith(PRESETS[name]["label"]):
            return name
    return choices[0]


def _wizard_pick_output(
    default_dir: Path,
    prompt_input: Optional[PromptFn] = None,
) -> Optional[Path]:
    """Step 3: choose the output directory."""
    ui_utils.step("Step 3/4: Output directory")
    print(f"  Default: {ui_utils.colorize(str(default_dir), ui_utils.C.DIM)}")
    raw = ui_utils.prompt_line(
        "Press Enter to accept default, or enter a custom path",
        default=str(default_dir),
        prompt_input=prompt_input,
    ).strip()
    if raw.lower() in ("q", "quit", "exit"):
        return None
    if not raw:
        return default_dir
    return Path(raw).expanduser().resolve()


def _wizard_summarise(
    input_path: Path,
    preset_name: str,
    cfg: CropConfig,
    output_dir: Path,
    is_batch: bool,
) -> None:
    """Print a one-shot summary of the chosen settings."""
    ui_utils.step("Step 4/4: Settings summary")
    info = PRESETS[preset_name]
    print(f"  {ui_utils.colorize('Input:    ', ui_utils.C.BOLD)} {input_path}")
    print(
        f"  {ui_utils.colorize('Preset:   ', ui_utils.C.BOLD)} "
        f"{preset_name} (margin={cfg.margin_mm}mm, threshold={cfg.threshold}, "
        f"merge_gap={cfg.merge_gap_mm}mm)"
    )
    print(f"  {ui_utils.colorize('Output:   ', ui_utils.C.BOLD)} {output_dir}")
    print(
        f"  {ui_utils.colorize('Mode:     ', ui_utils.C.BOLD)} "
        f"{'batch' if is_batch else 'single'}"
    )
    print()


def _wizard_confirm(
    prompt_input: Optional[PromptFn] = None,
) -> str:
    """Ask the user to confirm / adjust / quit.

    Returns one of: ``"y"``, ``"a"`` (adjust), ``"q"`` (quit).
    """
    raw = ui_utils.prompt_line(
        "[Y] Confirm and proceed  /  [a] Adjust parameters  /  [q] Quit",
        default="y",
        prompt_input=prompt_input,
    ).strip().lower()
    if raw in ("a", "adjust"):
        return "a"
    if raw in ("q", "quit", "exit"):
        return "q"
    return "y"


def _wizard_adjust_params(
    cfg: CropConfig,
    prompt_input: Optional[PromptFn] = None,
) -> CropConfig:
    """Let the user tweak individual parameters; return the new config."""
    print()
    ui_utils.info("Adjust parameters (press Enter to keep current value)")
    margin = ui_utils.prompt_float(
        "margin_mm",
        default=cfg.margin_mm,
        minimum=0.0,
        maximum=50.0,
        prompt_input=prompt_input,
    )
    threshold = ui_utils.prompt_int(
        "threshold",
        default=cfg.threshold,
        minimum=0,
        maximum=255,
        prompt_input=prompt_input,
    )
    merge_gap = ui_utils.prompt_float(
        "merge_gap_mm",
        default=cfg.merge_gap_mm,
        minimum=0.0,
        maximum=20.0,
        prompt_input=prompt_input,
    )
    return CropConfig.from_dict(
        {
            **cfg.to_dict(),
            "margin_mm": float(margin),
            "threshold": int(threshold),
            "merge_gap_mm": float(merge_gap),
        }
    )


# ---------------------------------------------------------------------------
# Public entry point.

def run_wizard(
    start_dir: Optional[Path] = None,
    prompt_input: Optional[PromptFn] = None,
) -> int:
    """Run the full interactive wizard.

    Returns a process exit code (0 = success).
    """
    ui_utils.enable_ansi()
    cwd = start_dir or Path.cwd()
    history = hist.load()

    ui_utils.section("PDF Auto Crop - Interactive Wizard")

    input_path = _wizard_pick_input(cwd, prompt_input=prompt_input)
    if input_path is None:
        ui_utils.warn("Aborted.")
        return 1

    is_batch = input_path.is_dir()

    last_preset = hist.last_preset()
    preset_name = _wizard_pick_preset(prompt_input=prompt_input, default=last_preset)
    if preset_name is None:
        ui_utils.warn("Aborted.")
        return 1

    default_out = (
        input_path.parent / f"{input_path.name}_out"
        if not is_batch
        else input_path.parent / f"{input_path.name}_out"
    )
    output_dir = _wizard_pick_output(default_out, prompt_input=prompt_input)
    if output_dir is None:
        ui_utils.warn("Aborted.")
        return 1

    cfg = apply_preset(CropConfig(), preset_name)
    _wizard_summarise(input_path, preset_name, cfg, output_dir, is_batch)

    while True:
        verdict = _wizard_confirm(prompt_input=prompt_input)
        if verdict == "q":
            ui_utils.warn("Aborted.")
            return 1
        if verdict == "a":
            cfg = _wizard_adjust_params(cfg, prompt_input=prompt_input)
            _wizard_summarise(input_path, preset_name, cfg, output_dir, is_batch)
            continue
        break

    # Persist for next time.
    hist.update(
        last_input_dir=str(input_path.parent),
        last_output_dir=str(output_dir),
        last_preset=preset_name,
    )

    return _execute(input_path, output_dir, cfg, is_batch)


def _execute(
    input_path: Path,
    output_dir: Path,
    cfg: CropConfig,
    is_batch: bool,
) -> int:
    """Run the crop pipeline once we have the user's final settings."""
    if is_batch:
        pdfs = collect_pdfs([input_path], recursive=False)
        if not pdfs:
            ui_utils.error(f"No PDFs found in {input_path}.")
            return 2
        result = process_batch(pdfs, output_dir, cfg=cfg, args=_dummy_args(cfg))
        _report_batch(result)
        return 0 if result.failed == 0 else 4

    return _run_single(input_path, output_dir, cfg)


def _dummy_args(cfg: CropConfig):
    """Create an ``argparse.Namespace`` matching the cropper's expectations."""
    import argparse

    return argparse.Namespace(
        input=None,
        output=None,
        margin=None,
        threshold=None,
        dpi=None,
        min_area=None,
        min_width=None,
        min_height=None,
        morph_kernel_multiplier=None,
        merge_gap_mm=None,
        padding_mm=None,
        preview=False,
        preview_only=False,
        naming=None,
        output_prefix=None,
        log_level="WARNING",
        save_config=False,
        config=None,
        interactive=False,
        preset=None,
        _batch_cfg=cfg,
    )


def _run_single(input_path: Path, output_dir: Path, cfg: CropConfig) -> int:
    """Process a single PDF using the same machinery as the CLI."""
    from .pipeline import run_pipeline

    args = _dummy_args(cfg)
    args.input = input_path
    args.output = output_dir
    rc = run_pipeline(args, cfg)
    if rc == 0:
        ui_utils.success(f"Done. Output -> {output_dir}")
    else:
        ui_utils.error(f"Cropper exited with code {rc}")
    return rc


def _report_batch(result: BatchResult) -> None:
    """Print a final summary for a batch run."""
    ui_utils.section("Batch summary")
    print(f"  Total:     {result.total}")
    print(f"  Succeeded: {ui_utils.colorize(str(result.succeeded), ui_utils.C.GREEN)}")
    print(f"  Failed:    {ui_utils.colorize(str(result.failed), ui_utils.C.RED)}")
    print(f"  Skipped:   {result.skipped}")
    if result.failures:
        ui_utils.warn("Failures:")
        for f in result.failures:
            print(f"    - {f}")


__all__ = [
    "run_wizard",
    "_wizard_pick_input",
    "_wizard_pick_preset",
    "_wizard_pick_output",
    "_wizard_summarise",
    "_wizard_confirm",
    "_wizard_adjust_params",
]