"""Tests for i18n locale parity (EN/RU/ZH) and adaptive OS/WSL/terminal/symlink fallbacks."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentsync.cli import app
from agentsync.i18n import SUPPORTED_LOCALES, cycle_locale, load_locale_catalog, set_locale, t
from agentsync.utils.env_adapt import (
    detect_environment,
    link_or_copy_skill_dir,
    windows_to_wsl_path,
    wsl_to_windows_path,
)

_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z0-9_]+)\}")


def test_i18n_en_ru_zh_exact_key_and_placeholder_parity() -> None:
    """en.json, ru.json, and zh.json must share identical keys, zero empty strings, and identical placeholders."""
    catalogs = {code: load_locale_catalog(code) for code in SUPPORTED_LOCALES}
    en_keys = set(catalogs["en"].keys())
    assert len(en_keys) >= 100

    for code, cat in catalogs.items():
        assert set(cat.keys()) == en_keys, f"Locale '{code}' key mismatch vs 'en'"
        for key, val in cat.items():
            assert val.strip() != "", f"Empty translation for '{code}:{key}'"
            en_ph = set(_PLACEHOLDER_RE.findall(catalogs["en"][key]))
            loc_ph = set(_PLACEHOLDER_RE.findall(val))
            assert loc_ph == en_ph, f"Placeholder mismatch in '{code}:{key}': {loc_ph} != {en_ph}"

    # Verify runtime switching and cycling
    set_locale("en")
    assert t("mode.auto") == "Auto-Patch"
    assert cycle_locale() == "ru"
    assert t("mode.auto") == "Авто-патч"
    assert cycle_locale() == "zh"
    assert t("mode.auto") == "自动修补"
    assert cycle_locale() == "en"


def test_wsl_windows_path_translation_roundtrip() -> None:
    """WSL `/mnt/c/...` and Windows `C:\\...` paths translate losslessly in both directions."""
    win_p = r"C:\Users\dev\Projects\agentsync"
    wsl_p = windows_to_wsl_path(win_p)
    assert wsl_p == "/mnt/c/Users/dev/Projects/agentsync"
    assert wsl_to_windows_path(wsl_p) == win_p
    assert wsl_to_windows_path("/home/dev/repo") == "/home/dev/repo"


def test_skill_link_strategy_falls_back_to_copy_when_symlink_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When OS denies symlinks (e.g. Windows WinError 1314) and junction fails, strategy falls back cleanly to copy."""
    src = tmp_path / "src-skill"
    src.mkdir()
    (src / "SKILL.md").write_text("---\nname: demo\ndescription: demo\n---\n# Demo\n", encoding="utf-8")
    dst = tmp_path / "dst-skill"
    files = {"SKILL.md": (src / "SKILL.md").read_bytes()}

    def _deny_symlink(*_args: object, **_kwargs: object) -> None:
        raise OSError(1314, "A required privilege is not held by the client")

    monkeypatch.setattr(os, "symlink", _deny_symlink)
    monkeypatch.setattr("agentsync.utils.env_adapt.sys.platform", "linux")

    changed, strategy = link_or_copy_skill_dir(src, dst, files, mode="auto")
    assert changed is True
    assert strategy == "copy"
    assert (dst / "SKILL.md").exists()
    assert (dst / "SKILL.md").read_bytes() == files["SKILL.md"]


def test_non_tty_and_no_color_graceful_cli_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """In non-TTY/CI/NO_COLOR environments, detect_environment disables TUI and bare CLI prints help cleanly."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("CI", "true")
    env = detect_environment()
    assert env.no_color is True
    assert env.is_ci is True
    assert env.tui_recommended is False

    runner = CliRunner()
    res = runner.invoke(app, ["--no-tui"])
    assert res.exit_code == 0
    assert "agentsync" in res.output.lower()
    assert "patch" in res.output.lower()
    assert "handoff" in res.output.lower()
