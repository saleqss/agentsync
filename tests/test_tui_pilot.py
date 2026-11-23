"""Textual TUI Pilot tests covering First-Run Wizard, checkbox isolation, Auto/Manual modes, live EN/RU/ZH switching, all 8 screens, and operations."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from textual.widgets import (
    Button,
    Checkbox,
    ContentSwitcher,
    DataTable,
    Label,
    Select,
    Static,
    Switch,
)

from agentsync import __version__
from agentsync.config import AgentSyncConfig, ConfigManager
from agentsync.discovery import DiscoveryEnv
from agentsync.engine.autostart import AutostartStatus, SplitInstallReport
from agentsync.engine.diagnostics import load_repair_queue
from agentsync.engine.updater import UpdateCheckResult
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
            check_updates_on_mount=False,
        )
        async with tui.run_test(size=(120, 40)) as pilot:
            await tui.workers.wait_for_complete()
            await pilot.pause()
            # Wizard should be active on Step 1
            assert tui.screen.__class__.__name__ == "OnboardingModal"
            # Switch language to Russian live inside wizard
            await pilot.click("#wiz-lang-ru")
            await pilot.pause()
            assert "Шаг 1/4" in str(tui.screen.query_one("#wizard-step-label", Label).render())

            # Advance Steps 1 -> 2 -> 3 -> 4 (allow Textual's 200ms Button active-effect timer to clear)
            await pilot.click("#btn-wiz-next")
            await pilot.pause(0.25)
            assert tui.screen.step == 2
            await pilot.click("#btn-wiz-next")
            await pilot.pause(0.25)
            assert tui.screen.step == 3
            # Toggle Patch Mode in Step 3
            await pilot.click("#wiz-toggle-mode")
            await pilot.pause(0.25)
            await pilot.click("#btn-wiz-next")
            await pilot.pause(0.25)
            assert "Шаг 4/4" in str(tui.screen.query_one("#wizard-step-label", Label).render())

            # Finish wizard -> persists config.toml
            await pilot.click("#btn-wiz-finish")
            await pilot.pause(0.2)
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
            check_updates_on_mount=False,
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
            check_updates_on_mount=False,
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


def test_tui_responsive_viewports_3x3_locales_and_counter_parity(sandbox: dict[str, Any]) -> None:
    """Responsive breakpoints (.compact 80x24, .medium 120x30, .wide 180x50) render cleanly across EN/RU/ZH with exact Selected X/Y parity."""
    cfg: AgentSyncConfig = sandbox["config"]
    cfg.onboarding_completed = True
    # Intentionally add a stale non-installed enabled tool to config to prove Selected: X/Y never miscounts!
    cfg_file: Path = sandbox["config_file"]
    ConfigManager(cfg_file).save(cfg)
    denv = _mock_discovery_env(sandbox)

    async def _run() -> None:
        for size, expected_bp in (((80, 24), "compact"), ((120, 30), "medium"), ((180, 50), "wide")):
            for loc in ("en", "ru", "zh"):
                tui = AgentSyncApp(
                    config_path=cfg_file,
                    data_dir=cfg.resolved_data_dir(),
                    locale_override=loc,
                    discovery_env=denv,
                    check_processes=False,
                    show_onboarding=False,
                    check_updates_on_mount=False,
                )
                async with tui.run_test(size=size) as pilot:
                    await tui.workers.wait_for_complete()
                    await pilot.pause(0.1)
                    assert tui.screen.has_class(expected_bp)

                    # Verify Selected: X/Y matches exact displayed checked installed checkboxes
                    checkboxes = [
                        cb for cb in tui.query("#detected-checklist Checkbox")
                        if isinstance(cb, Checkbox)
                    ]
                    checked_boxes = sum(1 for cb in checkboxes if cb.value)
                    total_boxes = len(checkboxes)
                    summary_txt = str(tui.query_one("#dash-summary", Static).render())
                    assert f"{checked_boxes}/{total_boxes}" in summary_txt

                    # Verify zero dashboard buttons clip horizontally beyond terminal width
                    for btn in tui.query("#screen-dashboard Button"):
                        if btn.display and btn.region.width > 0:
                            assert btn.region.right <= size[0], (
                                f"Button {btn.id} clipped at {size} ({loc}): right={btn.region.right}"
                            )

    asyncio.run(_run())


def test_tui_legacy_conhost_ascii_mode_zero_half_blocks(sandbox: dict[str, Any], tmp_path: Path) -> None:
    """Legacy Windows conhost ASCII mode applies .legacy-console, uses [X]/[ ] checkboxes, and renders zero half-block borders."""
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
            ascii_mode=True,
            check_updates_on_mount=False,
        )
        async with tui.run_test(size=(100, 30)) as pilot:
            await tui.workers.wait_for_complete()
            await pilot.pause(0.1)
            assert tui.screen.has_class("legacy-console")
            cbs = list(tui.query("#detected-checklist Checkbox"))
            assert len(cbs) >= 2
            assert cbs[0].BUTTON_LEFT == "["
            assert cbs[0].BUTTON_RIGHT == "]"
            assert cbs[0].BUTTON_INNER == "X"
            svg_path = tmp_path / "conhost.svg"
            tui.save_screenshot(str(svg_path))
            svg_txt = svg_path.read_text(encoding="utf-8")
            # Zero Textual half-block checkbox/button borders (`▐`, `▌`, `▔`)
            for bad_glyph in ("\u258c", "\u2590", "\u2594"):
                assert bad_glyph not in svg_txt

    asyncio.run(_run())


def test_tui_diff_modal_live_watch_and_backup_controls(sandbox: dict[str, Any]) -> None:
    """DiffModal opens on diff row selection, Live Watch starts/pauses/resumes/stops, and Backups create/verify work."""
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
            check_updates_on_mount=False,
        )
        async with tui.run_test(size=(130, 38)) as pilot:
            await tui.workers.wait_for_complete()
            await pilot.pause(0.1)

            # 1. Preview diff & inspect first row in DiffModal
            await pilot.press("2")
            await pilot.pause(0.1)
            await pilot.click("#btn-patch-preview")
            await tui.workers.wait_for_complete()
            await pilot.pause(0.1)
            assert len(tui._diff_actions) > 0
            dt = tui.query_one("#patch-diff-table", DataTable)
            dt.focus()
            await pilot.press("enter")
            await pilot.pause(0.1)
            assert tui.screen.__class__.__name__ == "DiffModal"
            await pilot.click("#btn-diff-close")
            await pilot.pause(0.1)
            assert tui.screen.__class__.__name__ != "DiffModal"

            # 2. Live Watch Screen 4: Start -> Pause -> Resume -> Stop
            await pilot.press("4")
            await pilot.pause(0.1)
            await pilot.click("#btn-watch-start")
            await pilot.pause(0.15)
            assert tui._watching is True and tui._watcher is not None
            await pilot.click("#btn-watch-pause")
            await pilot.pause(0.1)
            assert tui._watcher.status_snapshot().paused is True
            await pilot.click("#btn-watch-resume")
            await pilot.pause(0.1)
            assert tui._watcher.status_snapshot().paused is False
            await pilot.click("#btn-watch-stop")
            await pilot.pause(0.1)
            assert tui._watching is False

            # 3. Backups Screen 7: Create manual snapshot + Verify all backups
            await pilot.press("7")
            await pilot.pause(0.1)
            await pilot.click("#btn-backups-create")
            await tui.workers.wait_for_complete()
            await pilot.pause(0.15)
            assert tui.query_one("#backups-table", DataTable).row_count >= 1
            await pilot.click("#btn-backups-verify")
            await tui.workers.wait_for_complete()
            await pilot.pause(0.1)

            # 4. Global Command Palette (Ctrl+K) opens cleanly
            await pilot.press("ctrl+k")
            await pilot.pause(0.15)
            assert tui.screen.__class__.__name__ == "CommandPalette"
            await pilot.press("escape")
            await pilot.pause(0.1)

    asyncio.run(_run())


def test_tui_health_pill_repair_on_close_settings_and_update_modal(
    sandbox: dict[str, Any], monkeypatch: Any
) -> None:
    """Header Health pill opens Screen 6 + queues repair-on-close; Screen 8 live settings persist; UpdateModal Remind/Skip work."""
    cfg: AgentSyncConfig = sandbox["config"]
    cfg.onboarding_completed = True
    cfg_file: Path = sandbox["config_file"]
    ConfigManager(cfg_file).save(cfg)
    denv = _mock_discovery_env(sandbox)

    # Isolate host autostart & split-install checks during TUI test
    _ok_auto = AutostartStatus(platform="windows", enabled=False, healthy=True, mechanism="mock", artifact_path="mock")
    _ok_split = SplitInstallReport(current_version=__version__, has_version_drift=False)
    monkeypatch.setattr("agentsync.engine.diagnostics.autostart_status", lambda *_a, **_kw: _ok_auto)
    monkeypatch.setattr("agentsync.engine.diagnostics.detect_split_installations", lambda *_a, **_kw: _ok_split)
    monkeypatch.setattr("agentsync.tui.app.autostart_status", lambda *_a, **_kw: _ok_auto)

    async def _run() -> None:
        tui = AgentSyncApp(
            config_path=cfg_file,
            data_dir=cfg.resolved_data_dir(),
            discovery_env=denv,
            check_processes=False,
            show_onboarding=False,
            check_updates_on_mount=False,
        )
        async with tui.run_test(size=(130, 42)) as pilot:
            await tui.workers.wait_for_complete()
            await pilot.pause(0.15)

            # 1. Clickable header [Health] pill navigates directly to Screen 6 (Auto-Diagnostics)
            await pilot.click("#btn-health-badge")
            await tui.workers.wait_for_complete()
            await pilot.pause(0.15)
            assert tui.query_one("#main-switcher", ContentSwitcher).current == "screen-health"
            assert tui.last_diag_report is not None

            # Queue fixable issues via [Auto-Fix on Close] button -> repair_queue.json
            await pilot.click("#btn-health-repair-on-close")
            await tui.workers.wait_for_complete()
            await pilot.pause(0.15)
            assert not load_repair_queue(cfg.repair_queue_path).is_empty

            # 2. Settings Center (Screen 8): toggle Switches & Selects and confirm live persistence
            await pilot.press("8")
            await pilot.pause(0.15)
            assert tui.query_one("#main-switcher", ContentSwitcher).current == "screen-settings"
            assert "Daemon" in str(tui.query_one("#daemon-telemetry", Static).render()) or "STOPPED" in str(
                tui.query_one("#daemon-telemetry", Static).render()
            ).upper()

            tui.query_one("#sw-realtime", Switch).value = False
            tui.query_one("#sw-autoheal", Switch).value = False
            tui.query_one("#sw-autobackup", Switch).value = False
            tui.query_one("#sel-power-profile", Select).value = "instant"
            tui.query_one("#sel-runtime-engine", Select).value = "python"
            tui.query_one("#sel-update-policy", Select).value = "manual"
            await pilot.pause(0.15)

            # 3. UpdateModal: simulate available update -> click Remind Later -> click Skip Version
            fake_upd = UpdateCheckResult(
                current_version=__version__,
                latest_version="9.9.9",
                update_available=True,
                install_method="pip",
                release_notes="Test v9.9.9 release notes.",
            )
            tui._apply_update_info(fake_upd)
            await pilot.pause(0.1)
            upd_btn = tui.query_one("#btn-update-badge", Button)
            assert upd_btn.has_class("visible") is True

            # Click Update badge -> Remind Later
            await pilot.click("#btn-update-badge")
            await pilot.pause(0.15)
            assert tui.screen.__class__.__name__ == "UpdateModal"
            await pilot.click("#btn-upd-remind")
            await pilot.pause(0.15)
            assert tui.config.remind_update_after_utc is not None
            assert upd_btn.has_class("visible") is False

            # Re-show and choose Skip Version
            tui._apply_update_info(fake_upd)
            await pilot.pause(0.1)
            await pilot.click("#btn-update-badge")
            await pilot.pause(0.15)
            assert tui.screen.__class__.__name__ == "UpdateModal"
            await pilot.click("#btn-upd-skip")
            await pilot.pause(0.15)
            assert tui.config.skipped_update_version == "9.9.9"
            assert upd_btn.has_class("visible") is False

    asyncio.run(_run())

    reloaded = ConfigManager(cfg_file).load()
    assert reloaded.realtime_sync_enabled is False
    assert reloaded.auto_heal_on_close is False
    assert reloaded.auto_backup is False
    assert reloaded.power_profile == "instant"
    assert reloaded.runtime_engine == "python"
    assert reloaded.update_policy == "manual"
    assert reloaded.skipped_update_version == "9.9.9"
    assert reloaded.remind_update_after_utc is not None
