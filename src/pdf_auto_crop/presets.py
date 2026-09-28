"""Preset parameter sets for common figure-cropping scenarios.

Three named presets cover most use cases:

* ``tight``    -- small margin + small merge gap, ideal for dense multi-figure
  layouts (e.g. 2x2 sub-figures with labels on the outside).
* ``standard`` -- balanced defaults; matches the original CLI behaviour.
* ``loose``    -- generous margin + larger merge gap, ideal for single big
  figures where false splits are the bigger concern.

Each preset is a dict with three keys:

* ``label``        -- short human-readable name.
* ``description``  -- one-line hint shown in the wizard.
* ``params``       -- dict of keyword arguments accepted by
                      :class:`pdf_auto_crop.config.CropConfig.from_dict`.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .config import CropConfig

PRESETS: Dict[str, Dict[str, Any]] = {
    "tight": {
        "label": "tight",
        "description": "紧凑裁剪: 小 margin、紧凑 merge_gap，适合多图排版",
        "params": {
            "margin_mm": 2.0,
            "threshold": 248,
            "merge_gap_mm": 1.0,
            "morph_kernel_multiplier": 1.2,
        },
    },
    "standard": {
        "label": "standard",
        "description": "标准: 默认值，平衡 (margin=3mm，推荐)",
        "params": {
            "margin_mm": 3.0,
            "threshold": 245,
            "merge_gap_mm": 1.5,
            "morph_kernel_multiplier": 1.0,
        },
    },
    "loose": {
        "label": "loose",
        "description": "宽松裁剪: 大 margin、宽松 merge_gap，适合单图",
        "params": {
            "margin_mm": 5.0,
            "threshold": 240,
            "merge_gap_mm": 2.5,
            "morph_kernel_multiplier": 0.8,
        },
    },
}


def list_preset_names() -> List[str]:
    """Return the available preset names in a stable order."""
    return ["tight", "standard", "loose"]


def get_preset_info(name: str) -> Dict[str, Any]:
    """Return the raw preset dict for ``name``; raise if unknown."""
    if name not in PRESETS:
        raise ValueError(
            f"Unknown preset {name!r}; expected one of {list_preset_names()}"
        )
    return PRESETS[name]


def apply_preset(config: CropConfig, name: str) -> CropConfig:
    """Return a new :class:`CropConfig` with preset ``name``'s params applied.

    Parameters not present in the preset are left untouched, so the wizard
    can layer its own overrides on top of any loaded JSON config.
    """
    info = get_preset_info(name)  # raises ValueError on bad name
    merged = {**config.to_dict(), **info["params"]}
    return CropConfig.from_dict(merged)


__all__ = ["PRESETS", "apply_preset", "get_preset_info", "list_preset_names"]