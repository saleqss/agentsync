"""Textual TUI Pilot tests covering First-Run Wizard, checkbox isolation, Auto/Manual modes, live EN/RU/ZH switching, all 8 screens, and operations."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from textual.widgets import Button, Checkbox, ContentSwitcher, DataTable, Label, Static

from agentsync.config import AgentSyncConfig, ConfigManager
from agentsync.discovery import DiscoveryEnv
from agentsync.tui.app import SCREEN_IDS, AgentSyncApp


def _mock_discovery_env(sandbox: dict[str, Any]) -> DiscoveryEnv:
    codex_root = sandbox["codex"].root_path
    home = codex_root.parent
    return DiscoveryEnv(
        platform="linux",
        home=home,
        appdata=home / "AppData" / "Roaming",
        local_appdata=home / "AppData" / "Local",
        xdg_config_home=home / ".config",
        xdg_data_home=home / ".local" / "share",
        env={},
        probe_system_binaries=False,
    )


def test_tui_onboarding_wizard_live_language_and_persistence(sandbox: dict[str, Any]) -> None:
    """First-run Auto-Setup Wizard opens automatically, switches language live, completes all 4 steps, and persists config.toml."""
    cfg: AgentSyncConfig = sandbox["config"]
    cfg_file: Path = sandbox["config_file"]
    denv = _mock_discovery_env(sandbox)

    async def _run() -> None:
        tui = AgentSyncApp(
            config_path=cfg_file,
            data_dir=cfg.resolved_data_dir(),
            discovery_env=denv,
            check_processes=False,
            show_onboarding=True,
        )
        async with tui.run_test(size=(120, 40)) as pilot:
            await tui.workers.wait_for_complete()
            await pilot.pause(0.15)
            # Wizard should be active on Step 1
            assert tui.screen.__class__.__name__ == "OnboardingModal"
            # Switch language to Russian live inside wizard
            await pilot.click("#wiz-lang-ru")
            await pilot.pause(0.1)
            assert "Шаг 1/4" in str(tui.screen.query_one("#wizard-step-label", Label).render())

            # Advance Steps 1 -> 2 -> 3 -> 4
            await pilot.click("#btn-wiz-next")
            await pilot.pause(0.1)
            await pilot.click("#btn-wiz-next")
            await pilot.pause(0.1)
            # Toggle Patch Mode in Step 3
            await pilot.click("#wiz-toggle-mode")
            await pilot.pause(0.1)
            await pilot.click("#btn-wiz-next")
            await pilot.pause(0.1)
            assert "Шаг 4/4" in str(tui.screen.query_one("#wizard-step-label", Label).render())

            # Finish wizard -> persists config.toml
            await pilot.click("#btn-wiz-finish")
            await pilot.pause(0.15)
            assert tui.config.onboarding_completed is True
            assert tui.config.locale == "ru"
            assert tui.config.patch_mode == "auto"

    asyncio.run(_run())
    reloaded = ConfigManager(cfg_file).load()
    assert reloaded.onboarding_completed is True
    assert reloaded.locale == "ru"
    assert reloaded.patch_mode == "auto"


def test_tui_checkbox_selection_strictly_leaves_unchecked_apps_untouched(sandbox: dict[str, Any]) -> None:
    """Unchecking an app `[ ]` in the Dashboard guarantees zero writes to that app during patch/sync."""
    cfg: AgentSyncConfig = sandbox["config"]
    cfg.onboarding_completed = True
    cfg.tools["claude-code"].enabled = True
    cfg_file: Path = sandbox["config_file"]
    ConfigManager(cfg_file).save(cfg)
    denv = _mock_discovery_env(sandbox)

    claude = sandbox["claude"]
    assert not (claude.skills_dir / "codex-only-skill").exists()

    async def _run() -> None:
        tui = AgentSyncApp(
            config_path=cfg_file,
            data_dir=cfg.resolved_data_dir(),
            discovery_env=denv,
            check_processes=False,
            show_onboarding=False,
        )
        async with tui.run_test(size=(120, 40)) as pilot:
            await tui.workers.wait_for_complete()
            await pilot.pause(0.1)
            # Test Deselect All (`n`) and Select All (`a`) hotkeys
            await pilot.press("n")
            await pilot.pause(0.05)
            assert len(tui.checked_tools) == 0

            await pilot.press("a")
            await pilot.pause(0.05)
            assert "codex" in tui.checked_tools
            assert "antigravity" in tui.checked_tools

            # Uncheck `claude-code` explicitly!
            tui.checked_tools.discard("claude-code")
            tui._sync_checklist_widgets()
            await pilot.pause(0.05)
            assert "claude-code" not in tui.checked_tools

            # Switch to Patch & Sync and apply (with dry-run unchecked)
            await pilot.press("2")
            await pilot.pause(0.1)
            tui.query_one("#chk-opt-dryrun", Checkbox).value = False
            await pilot.click("#btn-patch-apply")
            await pilot.pause(0.1)
            # Safety confirmation modal must appear before applying!
            assert tui.screen.__class__.__name__ == "ConfirmModal"
            await pilot.click("#btn-modal-confirm")
            await tui.workers.wait_for_complete()
            await pilot.pause(0.15)

    asyncio.run(_run())
    # Checked apps (codex + antigravity) were synced, while unchecked `claude-code` was strictly untouched!
    assert "codex-only-skill" in sandbox["antigravity"].read_skills()
    assert "codex-only-skill" not in claude.read_skills()


def test_tui_live_language_mode_switch_all_8_screens_and_operations(sandbox: dict[str, Any]) -> None:
    """Live EN -> RU -> ZH -> EN switching, Auto/Manual mode toggle, all 8 screens, Dry-Run, Doctor, and Handoff work cleanly."""
    cfg: AgentSyncConfig = sandbox["config"]
    cfg.onboarding_completed = True
    cfg_file: Path = sandbox["config_file"]
    ConfigManager(cfg_file).save(cfg)
    denv = _mock_discovery_env(sandbox)

    async def _run() -> None:
        tui = AgentSyncApp(
            config_path=cfg_file,
            data_dir=cfg.resolved_data_dir(),
            discovery_env=denv,
            check_processes=False,
            show_onboarding=False,
        )
        async with tui.run_test(size=(120, 40)) as pilot:
            await tui.workers.wait_for_complete()
            await pilot.pause(0.1)

            # 1. Live language switching via hotkey `l` (en -> ru -> zh -> en)
            assert "Dashboard" in str(tui.query_one("#lbl-dash-title", Label).render())
            await pilot.press("l")
            await pilot.pause(0.05)
            assert "Панель" in str(tui.query_one("#lbl-dash-title", Label).render())
            await pilot.press("l")
            await pilot.pause(0.05)
            assert "仪表盘" in str(tui.query_one("#lbl-dash-title", Label).render())
            await pilot.press("l")
            await pilot.pause(0.05)
            assert "Dashboard" in str(tui.query_one("#lbl-dash-title", Label).render())

            # 2. Patch Mode switching (`m` hotkey) + persistence
            assert tui.config.patch_mode == "manual"
            await pilot.press("m")
            await pilot.pause(0.05)
            assert tui.config.patch_mode == "auto"
            assert "Auto-Patch" in str(tui.query_one("#btn-mode-toggle", Button).label)
            await pilot.press("m")
            await pilot.pause(0.05)
            assert tui.config.patch_mode == "manual"

            # 3. Navigate all 8 screens cleanly without errors
            switcher = tui.query_one("#main-switcher", ContentSwitcher)
            for idx, expected_id in enumerate(SCREEN_IDS, start=1):
                await pilot.press(str(idx))
                await pilot.pause(0.05)
                assert switcher.current == expected_id

            # 4. Run Dry-Run Patch inside TUI (Manual mode preview)
            await pilot.press("2")
            await pilot.pause(0.1)
            await pilot.click("#btn-patch-preview")
            await tui.workers.wait_for_complete()
            await pilot.pause(0.1)
            assert tui.last_patch_result is not None
            assert tui.last_patch_result.dry_run is True
            assert tui.query_one("#patch-diff-table", DataTable).row_count > 0

            # 5. Run Doctor inside TUI
            await pilot.press("6")
            await pilot.pause(0.1)
            await pilot.click("#btn-health-run")
            await tui.workers.wait_for_complete()
            await pilot.pause(0.1)
            assert tui.query_one("#health-table", DataTable).row_count >= 1

            # 6. Run Handoff Preview + Execute inside TUI
            await pilot.press("3")
            await pilot.pause(0.1)
            await pilot.click("#btn-handoff-refresh")
            await tui.workers.wait_for_complete()
            await pilot.pause(0.1)
            assert tui.query_one("#handoff-table", DataTable).row_count >= 1
            await pilot.click("#btn-handoff-preview")
            await tui.workers.wait_for_complete()
            await pilot.pause(0.1)
            assert "Preview:" in str(tui.query_one("#handoff-output", Static).render())

    asyncio.run(_run())
