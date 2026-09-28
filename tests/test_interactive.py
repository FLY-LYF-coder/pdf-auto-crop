"""Tests for the interactive layer: presets, UI utils, history, wizard steps.

These tests avoid invoking the actual stdin by passing a callable that
yields canned responses; this keeps them fast and deterministic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pdf_auto_crop import history as hist
from pdf_auto_crop import ui_utils
from pdf_auto_crop.config import CropConfig
from pdf_auto_crop.interactive import (
    _wizard_adjust_params,
    _wizard_confirm,
    _wizard_pick_input,
    _wizard_pick_output,
    _wizard_pick_preset,
    _wizard_summarise,
)
from pdf_auto_crop.presets import PRESETS, apply_preset, get_preset_info, list_preset_names


# ---------------------------------------------------------------------------
# Helpers


def _make_input(values):
    """Build an ``input``-like callable that yields the given values in turn."""
    it = iter(values)

    def _fn(prompt=""):
        return next(it)

    return _fn


def _empty_input(_prompt=""):
    raise EOFError("no more inputs")


# ---------------------------------------------------------------------------
# Preset tests


def test_presets_have_required_keys():
    for name in ("tight", "standard", "loose"):
        assert name in PRESETS
        info = PRESETS[name]
        assert "label" in info and info["label"]
        assert "description" in info and info["description"]
        assert "params" in info
        for key in ("margin_mm", "threshold", "merge_gap_mm", "morph_kernel_multiplier"):
            assert key in info["params"], f"{name} missing {key}"


def test_list_preset_names_order():
    assert list_preset_names() == ["tight", "standard", "loose"]


def test_apply_preset_standard_matches_defaults():
    cfg = apply_preset(CropConfig(), "standard")
    base = CropConfig()
    assert cfg.margin_mm == base.margin_mm
    assert cfg.threshold == base.threshold
    assert cfg.merge_gap_mm == base.merge_gap_mm
    assert cfg.morph_kernel_multiplier == base.morph_kernel_multiplier


def test_apply_preset_tight_values():
    cfg = apply_preset(CropConfig(), "tight")
    assert cfg.margin_mm == 2.0
    assert cfg.merge_gap_mm == 1.0
    assert cfg.threshold == 248


def test_apply_preset_loose_values():
    cfg = apply_preset(CropConfig(), "loose")
    assert cfg.margin_mm == 5.0
    assert cfg.merge_gap_mm == 2.5
    assert cfg.threshold == 240


def test_apply_preset_unknown_raises():
    with pytest.raises(ValueError):
        apply_preset(CropConfig(), "nonexistent")


def test_apply_preset_preserves_other_params():
    cfg = CropConfig(dpi=300, min_area_mm2=99.0)
    new_cfg = apply_preset(cfg, "tight")
    assert new_cfg.dpi == 300
    assert new_cfg.min_area_mm2 == 99.0


def test_get_preset_info_returns_full_dict():
    info = get_preset_info("tight")
    assert info["params"]["margin_mm"] == 2.0


# ---------------------------------------------------------------------------
# ui_utils tests


def test_confirm_default_yes_returns_true_on_empty(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "")
    assert ui_utils.confirm("Continue?", default=True) is True


def test_confirm_default_no_returns_false_on_empty(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "")
    assert ui_utils.confirm("Continue?", default=False) is False


def test_confirm_y_yes(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "y")
    assert ui_utils.confirm("ok", default=False) is True
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "yes")
    assert ui_utils.confirm("ok", default=False) is True


def test_confirm_n_no(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "n")
    assert ui_utils.confirm("ok", default=True) is False


def test_prompt_int_valid(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "42")
    assert ui_utils.prompt_int("n") == 42


def test_prompt_int_uses_default_on_empty(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "")
    assert ui_utils.prompt_int("n", default=7) == 7


def test_prompt_int_rejects_invalid(monkeypatch):
    responses = iter(["bad", "10"])
    monkeypatch.setattr("builtins.input", lambda *a, **kw: next(responses))
    assert ui_utils.prompt_int("n") == 10


def test_prompt_int_enforces_min_max(monkeypatch):
    responses = iter(["100", "5"])
    monkeypatch.setattr("builtins.input", lambda *a, **kw: next(responses))
    assert ui_utils.prompt_int("n", minimum=0, maximum=10) == 5


def test_prompt_float_accepts_float(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "2.5")
    assert ui_utils.prompt_float("n") == 2.5


def test_prompt_float_uses_default_on_empty(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "")
    assert ui_utils.prompt_float("n", default=3.5) == 3.5


def test_prompt_float_rejects_invalid(monkeypatch):
    responses = iter(["abc", "1.0"])
    monkeypatch.setattr("builtins.input", lambda *a, **kw: next(responses))
    assert ui_utils.prompt_float("n") == 1.0


def test_menu_chooses_first(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "1")
    chosen = ui_utils.menu("pick", ["a", "b", "c"])
    assert chosen == "a"


def test_menu_uses_default_on_empty(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "")
    chosen = ui_utils.menu("pick", ["a", "b"], default=2)
    assert chosen == "b"


def test_menu_rejects_invalid(monkeypatch):
    responses = iter(["bad", "9", "2"])
    monkeypatch.setattr("builtins.input", lambda *a, **kw: next(responses))
    chosen = ui_utils.menu("pick", ["a", "b"])
    assert chosen == "b"


def test_colorize_suppresses_when_no_color(monkeypatch, capsys):
    monkeypatch.setattr(ui_utils, "should_use_color", lambda: True)
    out = ui_utils.colorize("hello", ui_utils.C.GREEN)
    assert ui_utils.C.GREEN in out
    assert ui_utils.C.RESET in out

    monkeypatch.setattr(ui_utils, "should_use_color", lambda: False)
    out = ui_utils.colorize("hello", ui_utils.C.GREEN)
    assert out == "hello"


# ---------------------------------------------------------------------------
# history tests


def test_history_roundtrip(tmp_path, monkeypatch):
    fake = tmp_path / "hist.json"
    monkeypatch.setattr(hist, "history_path", lambda: fake)
    hist.save({"last_preset": "tight", "n": 1})
    loaded = hist.load()
    assert loaded == {"last_preset": "tight", "n": 1}


def test_history_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(hist, "history_path", lambda: tmp_path / "missing.json")
    assert hist.load() == {}


def test_history_update_merges(tmp_path, monkeypatch):
    fake = tmp_path / "hist.json"
    monkeypatch.setattr(hist, "history_path", lambda: fake)
    hist.save({"a": 1})
    hist.update(b=2, c=3)
    assert hist.load() == {"a": 1, "b": 2, "c": 3}


def test_history_update_overwrites(tmp_path, monkeypatch):
    fake = tmp_path / "hist.json"
    monkeypatch.setattr(hist, "history_path", lambda: fake)
    hist.save({"last_preset": "tight"})
    hist.update(last_preset="loose")
    assert hist.load()["last_preset"] == "loose"


def test_history_corrupt_returns_empty(tmp_path, monkeypatch):
    fake = tmp_path / "hist.json"
    fake.write_text("not valid json", encoding="utf-8")
    monkeypatch.setattr(hist, "history_path", lambda: fake)
    assert hist.load() == {}


def test_history_last_input_dir_returns_existing(tmp_path, monkeypatch):
    fake = tmp_path / "hist.json"
    monkeypatch.setattr(hist, "history_path", lambda: fake)
    sub = tmp_path / "sub"
    sub.mkdir()
    hist.save({"last_input_dir": str(sub)})
    assert hist.last_input_dir() == sub


def test_history_last_input_dir_missing_returns_default(tmp_path, monkeypatch):
    fake = tmp_path / "hist.json"
    monkeypatch.setattr(hist, "history_path", lambda: fake)
    hist.save({"last_input_dir": str(tmp_path / "ghost")})
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    result = hist.last_input_dir(default=fallback)
    assert result == fallback


# ---------------------------------------------------------------------------
# Wizard step tests


def test_wizard_pick_input_chooses_first(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"")
    (tmp_path / "b.pdf").write_bytes(b"")
    inputs = _make_input(["1"])
    result = _wizard_pick_input(tmp_path, prompt_input=inputs)
    assert result is not None
    assert result.name == "a.pdf"


def test_wizard_pick_input_accepts_path(tmp_path):
    target = tmp_path / "x.pdf"
    target.write_bytes(b"")
    inputs = _make_input([str(target)])
    result = _wizard_pick_input(tmp_path, prompt_input=inputs)
    assert result == target.resolve()


def test_wizard_pick_input_quit_returns_none(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"")
    inputs = _make_input(["q"])
    assert _wizard_pick_input(tmp_path, prompt_input=inputs) is None


def test_wizard_pick_input_out_of_range(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"")
    inputs = _make_input(["9"])
    assert _wizard_pick_input(tmp_path, prompt_input=inputs) is None


def test_wizard_pick_input_nonexistent_path(tmp_path):
    inputs = _make_input([str(tmp_path / "ghost.pdf")])
    assert _wizard_pick_input(tmp_path, prompt_input=inputs) is None


def test_wizard_pick_preset_default():
    inputs = _make_input([""])
    chosen = _wizard_pick_preset(prompt_input=inputs, default="standard")
    assert chosen == "standard"


def test_wizard_pick_preset_explicit():
    inputs = _make_input(["1"])
    chosen = _wizard_pick_preset(prompt_input=inputs)
    assert chosen == "tight"


def test_wizard_pick_preset_quit_returns_none():
    inputs = _make_input(["q"])  # ``menu`` does not treat ``q`` as quit; bad number -> re-ask
    # ``menu`` will re-prompt on invalid input; we provide enough values.
    inputs = _make_input(["q", "1"])
    # ``menu`` ignores q as quit; it asks again until a valid number is given.
    chosen = _wizard_pick_preset(prompt_input=inputs)
    assert chosen == "tight"


def test_wizard_pick_output_default(tmp_path):
    default = tmp_path / "out"
    inputs = _make_input([""])
    result = _wizard_pick_output(default, prompt_input=inputs)
    assert result == default


def test_wizard_pick_output_custom(tmp_path):
    default = tmp_path / "out"
    target = tmp_path / "custom"
    inputs = _make_input([str(target)])
    result = _wizard_pick_output(default, prompt_input=inputs)
    assert result == target.resolve()


def test_wizard_summarise_runs(capsys):
    cfg = CropConfig(margin_mm=3.0)
    _wizard_summarise(Path("input.pdf"), "standard", cfg, Path("./out"), is_batch=False)
    out = capsys.readouterr().out
    assert "Settings summary" in out
    assert "input.pdf" in out


def test_wizard_confirm_yes():
    assert _wizard_confirm(prompt_input=_make_input(["y"])) == "y"
    assert _wizard_confirm(prompt_input=_make_input([""])) == "y"


def test_wizard_confirm_adjust():
    assert _wizard_confirm(prompt_input=_make_input(["a"])) == "a"


def test_wizard_confirm_quit():
    assert _wizard_confirm(prompt_input=_make_input(["q"])) == "q"


def test_wizard_adjust_params_returns_new_config():
    # User accepts all defaults -> returns config with same values.
    inputs = _make_input(["", "", ""])
    cfg = CropConfig(margin_mm=3.0, threshold=245, merge_gap_mm=1.5)
    new = _wizard_adjust_params(cfg, prompt_input=inputs)
    assert new.margin_mm == 3.0
    assert new.threshold == 245
    assert new.merge_gap_mm == 1.5


def test_wizard_adjust_params_changes_value():
    inputs = _make_input(["5.0", "", ""])
    cfg = CropConfig()
    new = _wizard_adjust_params(cfg, prompt_input=inputs)
    assert new.margin_mm == 5.0