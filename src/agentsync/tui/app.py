"""Full-featured responsive 8-screen interactive Textual TUI for AgentSync (EN/RU/ZH)."""

from __future__ import annotations

import os
import subprocess
import sys
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.events import Resize
from textual.timer import Timer
from textual.widgets import (
    Button,
    Checkbox,
    Collapsible,
    ContentSwitcher,
    DataTable,
    Input,
    Label,
    ProgressBar,
    RichLog,
    Select,
    Static,
    Switch,
)

from agentsync import __version__
from agentsync.adapters.registry import get_registry
from agentsync.config import AgentSyncConfig, ConfigManager, PatchMode, SyncOptions, ToolConfig
from agentsync.discovery import DiscoveryEnv, discover_tools, merge_discovery_into_config
from agentsync.engine.autostart import autostart_status, disable_autostart, enable_autostart
from agentsync.engine.board import generate_inventory_board
from agentsync.engine.bundle import export_profile_bundle
from agentsync.engine.doctor import DoctorEngine
from agentsync.engine.handoff import HandoffEngine, LaunchMode
from agentsync.engine.patch import DetectedAppStatus, PatchEngine, PatchRunResult
from agentsync.engine.skill_export import install_agentsync_skill
from agentsync.engine.support import create_support_bundle
from agentsync.engine.updater import UpdateCheckResult, check_for_updates, perform_update
from agentsync.engine.watcher import SyncWatcher
from agentsync.i18n import cycle_locale, get_locale, set_locale, t
from agentsync.models.canonical import SyncAction, SyncPlan
from agentsync.state.backup import BackupManager
from agentsync.state.ledger import LedgerStore
from agentsync.state.state_db import StateStore
from agentsync.tui.modals import ConfirmModal, DiffModal, OnboardingModal, UpdateModal
from agentsync.tui.widgets import AgentCheckbox, AgentSyncCommandProvider
from agentsync.utils.env_adapt import detect_environment
from agentsync.utils.events import CancelledError, ProgressBus, ProgressEvent

SCREEN_IDS: tuple[str, ...] = (
    "screen-dashboard",
    "screen-patch",
    "screen-handoff",
    "screen-watch",
    "screen-tools",
    "screen-health",
    "screen-backups",
    "screen-settings",
)

NAV_KEYS: tuple[tuple[str, str, str, str], ...] = (
    ("nav-1", "screen-dashboard", "nav.dashboard", "1:Home"),
    ("nav-2", "screen-patch", "nav.patch", "2:Sync"),
    ("nav-3", "screen-handoff", "nav.handoff", "3:Hand"),
    ("nav-4", "screen-watch", "nav.watch", "4:Wtch"),
    ("nav-5", "screen-tools", "nav.tools", "5:Tool"),
    ("nav-6", "screen-health", "nav.health", "6:Hlth"),
    ("nav-7", "screen-backups", "nav.backups", "7:Bkup"),
    ("nav-8", "screen-settings", "nav.settings", "8:Cfg"),
)


class AgentSyncApp(App[None]):
    """Interactive Terminal UI Suite for universal AI coding agent synchronization."""

    CSS_PATH = "styles.tcss"
    TITLE = f"AGENTSYNC v{__version__}"
    COMMANDS = App.COMMANDS | {AgentSyncCommandProvider}

    BINDINGS = [
        Binding("1", "switch_screen('screen-dashboard')", "Dashboard", show=False),
        Binding("2", "switch_screen('screen-patch')", "Patch", show=False),
        Binding("3", "switch_screen('screen-handoff')", "Handoff", show=False),
        Binding("4", "switch_screen('screen-watch')", "Watch", show=False),
        Binding("5", "switch_screen('screen-tools')", "Tools", show=False),
        Binding("6", "switch_screen('screen-health')", "Health", show=False),
        Binding("7", "switch_screen('screen-backups')", "Backups", show=False),
        Binding("8", "switch_screen('screen-settings')", "Settings", show=False),
        Binding("ctrl+k", "command_palette", "Command Palette", show=False),
        Binding("a", "select_all_apps", "Select All", show=False),
        Binding("n", "deselect_all_apps", "Deselect All", show=False),
        Binding("r", "trigger_scan", "Re-Scan", show=False),
        Binding("p", "trigger_quick_patch", "Patch Selected", show=False),
        Binding("m", "toggle_patch_mode", "Auto/Manual", show=False),
        Binding("h", "switch_screen('screen-handoff')", "Handoff", show=False),
        Binding("l", "cycle_language", "Language", show=False),
        Binding("question_mark", "switch_screen('screen-settings')", "Guide", show=False),
        Binding("q", "quit", "Quit", show=False),
    ]

    def __init__(
        self,
        *,
        config_path: Path | None = None,
        data_dir: Path | None = None,
        locale_override: str | None = None,
        discovery_env: DiscoveryEnv | None = None,
        check_processes: bool = True,
        show_onboarding: bool | None = None,
        ascii_mode: bool | None = None,
        check_updates_on_mount: bool = True,
    ) -> None:
        super().__init__()
        self.cfg_mgr = ConfigManager(config_path)
        self.config: AgentSyncConfig = self.cfg_mgr.load()
        if data_dir is not None:
            self.config.data_dir = str(data_dir)
        initial_loc = locale_override or self.config.locale or "en"
        self.config.locale = set_locale(initial_loc)  # type: ignore[assignment]
        self.discovery_env = discovery_env
        self.check_processes = check_processes
        self._force_onboarding = show_onboarding
        self.env_profile = detect_environment()
        self.ascii_mode = self.env_profile.legacy_console if ascii_mode is None else ascii_mode
        self._check_updates_on_mount = check_updates_on_mount

        self.detected_apps: list[DetectedAppStatus] = []
        self.checked_tools: set[str] = {
            name for name, tc in self.config.tools.items() if tc.enabled
        }
        self.active_bus = ProgressBus(self._on_progress_event_bg)
        self.last_patch_result: PatchRunResult | None = None
        self._diff_actions: list[SyncAction] = []
        self.handoff_sessions: list[tuple[str, str, str, str, int]] = []
        self.palette_index: list[tuple[str, str, str, str]] = []
        self.update_info: UpdateCheckResult | None = None
        self._watcher: SyncWatcher | None = None
        self._watch_timer: Timer | None = None
        self._watching = False
        self._breakpoint = "wide"
        self._suppress_switch_events = False

    def compose(self) -> ComposeResult:
        # Persistent Responsive Header
        with Horizontal(id="top-header"):
            yield Label(f"{t('app.title')} v{__version__}", id="brand-title")
            yield Label(t("status.idle"), id="status-badge")
            yield Button("", id="btn-update-badge")
            yield Label(self.env_profile.badge(), id="env-badge")
            yield Static("", classes="header-spacer")
            yield Button(
                self._mode_pill_label(),
                id="btn-mode-toggle",
                classes="auto-mode" if self.config.patch_mode == "auto" else "",
            )
            yield Button("EN", id="btn-lang-en", classes="lang-btn")
            yield Button("RU", id="btn-lang-ru", classes="lang-btn")
            yield Button("ZH", id="btn-lang-zh", classes="lang-btn")

        # Main Navigation + 8-Screen Switcher
        with Horizontal(id="body-row"):
            with Vertical(id="nav-rail"):
                for btn_id, screen_id, loc_key, _short in NAV_KEYS:
                    cls = "nav-btn active" if screen_id == "screen-dashboard" else "nav-btn"
                    yield Button(t(loc_key), id=btn_id, classes=cls)

            with ContentSwitcher(initial="screen-dashboard", id="main-switcher"):
                # 1. Dashboard & Quick Patch Launcher
                with VerticalScroll(id="screen-dashboard", classes="pane-scroll"):
                    yield Label(t("dash.title"), id="lbl-dash-title", classes="section-title")
                    with Container(classes="action-grid-3"):
                        yield Button(t("dash.select_all"), id="btn-select-all", variant="default")
                        yield Button(t("dash.deselect_all"), id="btn-deselect-all", variant="default")
                        yield Button(t("dash.rescan"), id="btn-rescan", variant="primary")
                    with Vertical(id="detected-card", classes="card"):
                        yield Label(t("dash.detected_apps"), id="lbl-detected-apps", classes="section-title")
                        yield Vertical(id="detected-checklist")
                    with Collapsible(title=t("dash.undetected_apps"), id="undetected-collapsible", collapsed=True):
                        yield Vertical(id="undetected-list")
                    with Container(classes="action-grid"):
                        yield Button(t("dash.patch_selected"), id="btn-quick-patch", variant="success")
                        yield Button(t("dash.dry_run"), id="btn-quick-dryrun", variant="primary")
                        yield Button(t("dash.handoff"), id="btn-quick-handoff", variant="default")
                        yield Button(t("dash.run_doctor"), id="btn-quick-doctor", variant="warning")
                        yield Button(t("dash.open_board"), id="btn-quick-board", variant="default")
                        yield Button(t("dash.run_wizard"), id="btn-quick-wizard", variant="default")
                    yield Static("", id="dash-summary", markup=False)

                # 2. Patch, Sync & Bootstrap Center
                with VerticalScroll(id="screen-patch", classes="pane-scroll"):
                    yield Label(t("patch.title"), id="lbl-patch-title", classes="section-title")
                    with Vertical(classes="card"):
                        with Container(classes="action-grid-3"):
                            yield Button(t("patch.btn_preview"), id="btn-patch-preview", variant="primary")
                            yield Button(t("patch.btn_apply"), id="btn-patch-apply", variant="success")
                            yield Button(t("patch.btn_cancel"), id="btn-patch-cancel", variant="error")
                        with Container(classes="opt-grid"):
                            yield Select(
                                [
                                    (t("patch.wf_manual"), "manual"),
                                    (t("patch.wf_auto"), "auto"),
                                    (t("patch.wf_sync"), "sync"),
                                    (t("patch.wf_bootstrap"), "bootstrap"),
                                ],
                                value=self.config.patch_mode,
                                id="sel-patch-workflow",
                            )
                            yield Select(
                                [
                                    (t("patch.scope_all"), "all"),
                                    (t("patch.scope_user"), "user"),
                                    (t("patch.scope_project"), "project"),
                                ],
                                value="all",
                                id="sel-patch-scope",
                            )
                            yield Input(placeholder=t("patch.include_placeholder"), id="inp-include")
                            yield Input(placeholder=t("patch.exclude_placeholder"), id="inp-exclude")
                            yield AgentCheckbox(
                                t("patch.opt_dry_run"), value=True, ascii_mode=self.ascii_mode, id="chk-opt-dryrun"
                            )
                            yield AgentCheckbox(
                                t("patch.opt_prune"), value=False, ascii_mode=self.ascii_mode, id="chk-opt-prune"
                            )
                            yield AgentCheckbox(
                                t("patch.opt_strip_secrets"),
                                value=self.config.strip_secrets,
                                ascii_mode=self.ascii_mode,
                                id="chk-opt-strip",
                            )
                            yield AgentCheckbox(
                                t("patch.opt_force"), value=False, ascii_mode=self.ascii_mode, id="chk-opt-force"
                            )
                    with Vertical(id="progress-panel"):
                        yield Label(t("status.idle"), id="op-status")
                        yield ProgressBar(total=100, show_eta=False, id="op-progress")
                    yield Static(t("patch.diff_hint"), id="lbl-diff-hint", classes="muted-hint", markup=False)
                    yield DataTable(id="patch-diff-table", cursor_type="row")
                    yield RichLog(id="op-log", highlight=True, markup=False)

                # 3. Context & Session Handoff Center
                with VerticalScroll(id="screen-handoff", classes="pane-scroll"):
                    yield Label(t("handoff.title"), id="lbl-handoff-title", classes="section-title")
                    yield Static(t("handoff.subtitle"), id="lbl-handoff-sub", markup=False)
                    with Container(classes="opt-grid"):
                        yield Select(
                            [
                                ("codex", "codex"),
                                ("antigravity", "antigravity"),
                                ("claude-code", "claude-code"),
                                ("opencode", "opencode"),
                            ],
                            value="codex",
                            id="sel-handoff-src",
                        )
                        yield Select(
                            [
                                ("antigravity", "antigravity"),
                                ("codex", "codex"),
                                ("claude-code", "claude-code"),
                                ("opencode", "opencode"),
                            ],
                            value="antigravity",
                            id="sel-handoff-tgt",
                        )
                        yield Select(
                            [
                                (t("handoff.open_none"), "none"),
                                (t("handoff.open_auto"), "auto"),
                                (t("handoff.open_app"), "app"),
                                (t("handoff.open_terminal"), "terminal"),
                            ],
                            value="none",
                            id="sel-handoff-open",
                        )
                        yield Input(placeholder=t("handoff.filter_placeholder"), id="inp-handoff-filter")
                    with Container(classes="action-grid-3"):
                        yield Button(t("handoff.btn_refresh"), id="btn-handoff-refresh", variant="primary")
                        yield Button(t("handoff.btn_preview"), id="btn-handoff-preview", variant="default")
                        yield Button(t("handoff.btn_execute"), id="btn-handoff-execute", variant="success")
                    yield DataTable(id="handoff-table", cursor_type="row")
                    yield Static("", id="handoff-output", classes="card", markup=False)

                # 4. Live Watch & Sync-on-Close Monitor
                with VerticalScroll(id="screen-watch", classes="pane-scroll"):
                    yield Label(t("watch.title"), id="lbl-watch-title", classes="section-title")
                    yield Static(t("watch.subtitle"), id="lbl-watch-sub", markup=False)
                    with Container(classes="action-grid"):
                        yield Button(t("watch.btn_start"), id="btn-watch-start", variant="success")
                        yield Button(t("watch.btn_pause"), id="btn-watch-pause", variant="warning")
                        yield Button(t("watch.btn_resume"), id="btn-watch-resume", variant="primary")
                        yield Button(t("watch.btn_stop"), id="btn-watch-stop", variant="error")
                    yield Static(t("watch.stopped"), id="watch-state-label", classes="card", markup=False)
                    yield Static("", id="watch-roots-label", classes="muted-hint", markup=False)
                    yield RichLog(id="watch-log", highlight=True, markup=False)

                # 5. Tools, Paths & Auto-Discovery
                with VerticalScroll(id="screen-tools", classes="pane-scroll"):
                    yield Label(t("tools.title"), id="lbl-tools-title", classes="section-title")
                    with Container(classes="action-grid-3"):
                        yield Button(t("tools.btn_rescan"), id="btn-tools-rescan", variant="primary")
                        yield Button(t("tools.btn_save"), id="btn-tools-save", variant="success")
                        yield Button(t("tools.btn_set_path"), id="btn-set-custom-path", variant="default")
                    yield DataTable(id="tools-table", cursor_type="row")
                    yield Input(placeholder=t("tools.custom_path_placeholder"), id="inp-custom-path")

                # 6. Health, Audit & Deep Repair
                with VerticalScroll(id="screen-health", classes="pane-scroll"):
                    yield Label(t("health.title"), id="lbl-health-title", classes="section-title")
                    with Container(classes="action-grid-3"):
                        yield Button(t("health.btn_run"), id="btn-health-run", variant="primary")
                        yield Button(t("health.btn_repair_dry"), id="btn-health-repair-dry", variant="warning")
                        yield Button(t("health.btn_repair"), id="btn-health-repair", variant="success")
                    yield Static("", id="health-summary", classes="card", markup=False)
                    yield DataTable(id="health-table", cursor_type="row")

                # 7. Backups, Apply Ledger & Integrity
                with VerticalScroll(id="screen-backups", classes="pane-scroll"):
                    yield Label(t("backups.title"), id="lbl-backups-title", classes="section-title")
                    with Container(classes="action-grid"):
                        yield Button(t("backups.btn_create"), id="btn-backups-create", variant="success")
                        yield Button(t("backups.btn_verify"), id="btn-backups-verify", variant="primary")
                        yield Button(t("backups.btn_refresh"), id="btn-backups-refresh", variant="default")
                        yield Button(t("backups.btn_restore"), id="btn-backups-restore", variant="warning")
                    yield Static("", id="backups-summary", markup=False)
                    yield DataTable(id="backups-table", cursor_type="row")
                    yield Label(t("backups.ledger_heading"), id="lbl-ledger-heading", classes="section-title")
                    yield DataTable(id="ledger-table", cursor_type="row")

                # 8. Settings, Autostart, Updates & Bundles
                with VerticalScroll(id="screen-settings", classes="pane-scroll"):
                    yield Label(t("settings.title"), id="lbl-settings-title", classes="section-title")
                    with Vertical(classes="card"):
                        yield Label(t("settings.language"), id="lbl-set-lang")
                        with Container(classes="action-grid-3"):
                            yield Button("English", id="btn-set-lang-en", variant="default")
                            yield Button("Русский", id="btn-set-lang-ru", variant="default")
                            yield Button("简体中文", id="btn-set-lang-zh", variant="default")
                        yield Label(t("settings.patch_mode"), id="lbl-set-mode")
                        with Container(classes="action-grid-3"):
                            yield Button(t("mode.auto"), id="btn-set-mode-auto", variant="default")
                            yield Button(t("mode.manual"), id="btn-set-mode-manual", variant="default")
                        yield Label(t("settings.link_mode"), id="lbl-set-link")
                        yield Select(
                            [
                                ("Auto (Symlink -> Junction -> Copy)", "auto"),
                                ("Symlink", "symlink"),
                                ("Atomic Copy", "copy"),
                            ],
                            value=self.config.skill_link_mode,
                            id="sel-link-mode",
                        )
                        with Horizontal(classes="switch-row"):
                            yield Label(t("settings.auto_scan"), id="lbl-set-autoscan")
                            yield Switch(value=self.config.auto_scan_on_startup, id="sw-autoscan")
                        with Horizontal(classes="switch-row"):
                            yield Label(t("settings.strict_vocab"), id="lbl-set-vocab")
                            yield Switch(value=self.config.strict_vocab_guard, id="sw-strict-vocab")
                        with Horizontal(classes="switch-row"):
                            yield Label(t("settings.autostart"), id="lbl-set-autostart")
                            yield Switch(value=self.config.autostart_enabled, id="sw-autostart")
                        with Horizontal(classes="switch-row"):
                            yield Label(t("settings.notifications"), id="lbl-set-notify")
                            yield Switch(value=self.config.notifications, id="sw-notifications")
                        yield Static("", id="lbl-autostart-status", classes="muted-hint", markup=False)

                    with Vertical(classes="card"):
                        yield Label(t("settings.updates_heading"), id="lbl-updates-heading", classes="section-title")
                        yield Static(
                            t("settings.update_current", current=__version__, method="checking"),
                            id="lbl-update-status",
                            markup=False,
                        )
                        with Container(classes="action-grid-3"):
                            yield Button(t("settings.btn_check_update"), id="btn-check-update", variant="primary")
                            yield Button(t("settings.btn_apply_update"), id="btn-apply-update", variant="success")

                    with Container(classes="action-grid"):
                        yield Button(t("settings.btn_board"), id="btn-open-board", variant="primary")
                        yield Button(t("settings.btn_install_skill"), id="btn-install-skill", variant="success")
                        yield Button(t("settings.btn_export_bundle"), id="btn-export-bundle", variant="default")
                        yield Button(t("settings.btn_support_bundle"), id="btn-support-bundle", variant="default")
                        yield Button(t("settings.btn_wizard"), id="btn-rerun-wizard", variant="default")
                        yield Button(t("settings.btn_open_config"), id="btn-open-cfg-dir", variant="default")
                        yield Button(t("settings.btn_open_backups"), id="btn-open-bkp-dir", variant="default")

                    with Vertical(classes="card"):
                        yield Label(t("guide.title"), id="lbl-guide-title", classes="section-title")
                        yield Static(t("guide.body"), id="guide-body", markup=False)

        yield Static(t("footer.keys"), id="bottom-footer", markup=False)

    def on_mount(self) -> None:
        if self.ascii_mode:
            self.screen.add_class("legacy-console")
        self._apply_responsive_breakpoint(self.size.width)
        self._init_tables()
        self._refresh_locale_widgets()
        self._refresh_backups_view()
        self._refresh_autostart_status()
        if self._check_updates_on_mount and self.config.auto_check_updates:
            self._check_updates_worker(force=False)
        if self.config.auto_scan_on_startup:
            self.action_trigger_scan(open_wizard_after=self._should_open_onboarding())
        elif self._should_open_onboarding():
            self._open_onboarding_wizard()

    def on_unmount(self) -> None:
        self._stop_watch()

    def on_resize(self, event: Resize) -> None:
        self._apply_responsive_breakpoint(event.size.width)

    def _apply_responsive_breakpoint(self, width: int) -> None:
        if width < 100:
            bp = "compact"
        elif width < 140:
            bp = "medium"
        else:
            bp = "wide"
        self._breakpoint = bp
        self.screen.remove_class("compact", "medium", "wide")
        self.screen.add_class(bp)
        for btn_id, _screen_id, loc_key, short_lbl in NAV_KEYS:
            try:
                btn = self.query_one(f"#{btn_id}", Button)
                btn.label = Content.from_text(short_lbl if bp == "compact" else t(loc_key))
            except Exception:
                pass

    def _should_open_onboarding(self) -> bool:
        if self._force_onboarding is not None:
            return self._force_onboarding
        return not self.cfg_mgr.exists() and not self.config.onboarding_completed

    def _init_tables(self) -> None:
        pt = self.query_one("#patch-diff-table", DataTable)
        pt.clear(columns=True)
        pt.add_columns(
            t("patch.col_category"),
            t("patch.col_action"),
            t("patch.col_target"),
            t("patch.col_risk"),
            t("patch.col_entity"),
            t("patch.col_summary"),
        )
        ht = self.query_one("#handoff-table", DataTable)
        ht.clear(columns=True)
        ht.add_columns("Tool", "Session ID", "Title", "Project", "Msgs")
        tt = self.query_one("#tools-table", DataTable)
        tt.clear(columns=True)
        tt.add_columns("Tool", "Type", "Inst", "Chk", "Status", "Path")
        dt = self.query_one("#health-table", DataTable)
        dt.clear(columns=True)
        dt.add_columns("Severity", "Tool", "Code", "Fix", "Details")
        bt = self.query_one("#backups-table", DataTable)
        bt.clear(columns=True)
        bt.add_columns("Backup ID", "Created (UTC)", "Reason", "Tools", "Files")
        lt = self.query_one("#ledger-table", DataTable)
        lt.clear(columns=True)
        lt.add_columns("Timestamp", "Mode", "Tools", "Items", "Plan Hash")

    def _mode_pill_label(self) -> str:
        return t("mode.auto") if self.config.patch_mode == "auto" else t("mode.manual")

    def _set_status(self, key: str, style: str = "") -> None:
        badge = self.query_one("#status-badge", Label)
        badge.update(Content.from_text(t(key)))
        badge.remove_class("warn", "busy")
        if style:
            badge.add_class(style)

    def _on_progress_event_bg(self, ev: ProgressEvent) -> None:
        self.call_from_thread(self._apply_progress_event, ev)

    def _apply_progress_event(self, ev: ProgressEvent) -> None:
        msg = t(ev.message_key, **ev.params)
        self.query_one("#op-status", Label).update(Content.from_text(f"[{ev.phase}] {msg}"))
        self.query_one("#op-progress", ProgressBar).update(progress=ev.percent)
        self.query_one("#op-log", RichLog).write(f"[{ev.level}] {msg}")

    # --- Live Language & Patch Mode Switching ---

    def _apply_locale(self, code: str, *, persist: bool = True) -> None:
        normalized = set_locale(code)
        self.config.locale = normalized  # type: ignore[assignment]
        if persist and (self.cfg_mgr.exists() or self.config.onboarding_completed):
            self.cfg_mgr.save(self.config)
        self._refresh_locale_widgets()

    def _refresh_locale_widgets(self) -> None:
        cur = get_locale()
        for code in ("en", "ru", "zh"):
            btn = self.query_one(f"#btn-lang-{code}", Button)
            if code == cur:
                btn.add_class("active")
            else:
                btn.remove_class("active")
        self.query_one("#btn-lang-zh", Button).label = Content.from_text("ZH" if self.ascii_mode else "中文")

        self.query_one("#brand-title", Label).update(Content.from_text(f"{t('app.title')} v{__version__}"))
        mode_btn = self.query_one("#btn-mode-toggle", Button)
        mode_btn.label = Content.from_text(self._mode_pill_label())
        if self.config.patch_mode == "auto":
            mode_btn.add_class("auto-mode")
        else:
            mode_btn.remove_class("auto-mode")

        for btn_id, _screen_id, loc_key, short_lbl in NAV_KEYS:
            lbl = short_lbl if self._breakpoint == "compact" else t(loc_key)
            self.query_one(f"#{btn_id}", Button).label = Content.from_text(lbl)

        self.query_one("#bottom-footer", Static).update(Content.from_text(t("footer.keys")))
        self.query_one("#lbl-dash-title", Label).update(Content.from_text(t("dash.title")))
        self.query_one("#lbl-detected-apps", Label).update(Content.from_text(t("dash.detected_apps")))
        self.query_one("#undetected-collapsible", Collapsible).title = t("dash.undetected_apps")
        self.query_one("#btn-select-all", Button).label = Content.from_text(t("dash.select_all"))
        self.query_one("#btn-deselect-all", Button).label = Content.from_text(t("dash.deselect_all"))
        self.query_one("#btn-rescan", Button).label = Content.from_text(t("dash.rescan"))
        self.query_one("#btn-quick-patch", Button).label = Content.from_text(t("dash.patch_selected"))
        self.query_one("#btn-quick-dryrun", Button).label = Content.from_text(t("dash.dry_run"))
        self.query_one("#btn-quick-handoff", Button).label = Content.from_text(t("dash.handoff"))
        self.query_one("#btn-quick-doctor", Button).label = Content.from_text(t("dash.run_doctor"))
        self.query_one("#btn-quick-board", Button).label = Content.from_text(t("dash.open_board"))
        self.query_one("#btn-quick-wizard", Button).label = Content.from_text(t("dash.run_wizard"))

        self.query_one("#lbl-patch-title", Label).update(Content.from_text(t("patch.title")))
        self.query_one("#btn-patch-preview", Button).label = Content.from_text(t("patch.btn_preview"))
        self.query_one("#btn-patch-apply", Button).label = Content.from_text(t("patch.btn_apply"))
        self.query_one("#btn-patch-cancel", Button).label = Content.from_text(t("patch.btn_cancel"))
        self.query_one("#chk-opt-dryrun", Checkbox).label = Content.from_text(t("patch.opt_dry_run"))
        self.query_one("#chk-opt-prune", Checkbox).label = Content.from_text(t("patch.opt_prune"))
        self.query_one("#chk-opt-strip", Checkbox).label = Content.from_text(t("patch.opt_strip_secrets"))
        self.query_one("#chk-opt-force", Checkbox).label = Content.from_text(t("patch.opt_force"))
        self.query_one("#lbl-diff-hint", Static).update(Content.from_text(t("patch.diff_hint")))

        self.query_one("#lbl-handoff-title", Label).update(Content.from_text(t("handoff.title")))
        self.query_one("#lbl-handoff-sub", Static).update(Content.from_text(t("handoff.subtitle")))
        self.query_one("#btn-handoff-refresh", Button).label = Content.from_text(t("handoff.btn_refresh"))
        self.query_one("#btn-handoff-preview", Button).label = Content.from_text(t("handoff.btn_preview"))
        self.query_one("#btn-handoff-execute", Button).label = Content.from_text(t("handoff.btn_execute"))

        self.query_one("#lbl-watch-title", Label).update(Content.from_text(t("watch.title")))
        self.query_one("#lbl-watch-sub", Static).update(Content.from_text(t("watch.subtitle")))
        self.query_one("#btn-watch-start", Button).label = Content.from_text(t("watch.btn_start"))
        self.query_one("#btn-watch-pause", Button).label = Content.from_text(t("watch.btn_pause"))
        self.query_one("#btn-watch-resume", Button).label = Content.from_text(t("watch.btn_resume"))
        self.query_one("#btn-watch-stop", Button).label = Content.from_text(t("watch.btn_stop"))

        self.query_one("#lbl-tools-title", Label).update(Content.from_text(t("tools.title")))
        self.query_one("#btn-tools-rescan", Button).label = Content.from_text(t("tools.btn_rescan"))
        self.query_one("#btn-tools-save", Button).label = Content.from_text(t("tools.btn_save"))
        self.query_one("#btn-set-custom-path", Button).label = Content.from_text(t("tools.btn_set_path"))

        self.query_one("#lbl-health-title", Label).update(Content.from_text(t("health.title")))
        self.query_one("#btn-health-run", Button).label = Content.from_text(t("health.btn_run"))
        self.query_one("#btn-health-repair-dry", Button).label = Content.from_text(t("health.btn_repair_dry"))
        self.query_one("#btn-health-repair", Button).label = Content.from_text(t("health.btn_repair"))

        self.query_one("#lbl-backups-title", Label).update(Content.from_text(t("backups.title")))
        self.query_one("#btn-backups-create", Button).label = Content.from_text(t("backups.btn_create"))
        self.query_one("#btn-backups-verify", Button).label = Content.from_text(t("backups.btn_verify"))
        self.query_one("#btn-backups-refresh", Button).label = Content.from_text(t("backups.btn_refresh"))
        self.query_one("#btn-backups-restore", Button).label = Content.from_text(t("backups.btn_restore"))
        self.query_one("#lbl-ledger-heading", Label).update(Content.from_text(t("backups.ledger_heading")))

        self.query_one("#lbl-settings-title", Label).update(Content.from_text(t("settings.title")))
        self.query_one("#lbl-set-lang", Label).update(Content.from_text(t("settings.language")))
        self.query_one("#lbl-set-mode", Label).update(Content.from_text(t("settings.patch_mode")))
        self.query_one("#btn-set-mode-auto", Button).label = Content.from_text(t("mode.auto"))
        self.query_one("#btn-set-mode-manual", Button).label = Content.from_text(t("mode.manual"))
        self.query_one("#lbl-set-link", Label).update(Content.from_text(t("settings.link_mode")))
        self.query_one("#lbl-set-autoscan", Label).update(Content.from_text(t("settings.auto_scan")))
        self.query_one("#lbl-set-vocab", Label).update(Content.from_text(t("settings.strict_vocab")))
        self.query_one("#lbl-set-autostart", Label).update(Content.from_text(t("settings.autostart")))
        self.query_one("#lbl-set-notify", Label).update(Content.from_text(t("settings.notifications")))
        self.query_one("#lbl-updates-heading", Label).update(Content.from_text(t("settings.updates_heading")))
        self.query_one("#btn-check-update", Button).label = Content.from_text(t("settings.btn_check_update"))
        self.query_one("#btn-apply-update", Button).label = Content.from_text(t("settings.btn_apply_update"))
        self.query_one("#btn-open-board", Button).label = Content.from_text(t("settings.btn_board"))
        self.query_one("#btn-install-skill", Button).label = Content.from_text(t("settings.btn_install_skill"))
        self.query_one("#btn-export-bundle", Button).label = Content.from_text(t("settings.btn_export_bundle"))
        self.query_one("#btn-support-bundle", Button).label = Content.from_text(t("settings.btn_support_bundle"))
        self.query_one("#btn-rerun-wizard", Button).label = Content.from_text(t("settings.btn_wizard"))
        self.query_one("#btn-open-cfg-dir", Button).label = Content.from_text(t("settings.btn_open_config"))
        self.query_one("#btn-open-bkp-dir", Button).label = Content.from_text(t("settings.btn_open_backups"))
        self.query_one("#lbl-guide-title", Label).update(Content.from_text(t("guide.title")))
        self.query_one("#guide-body", Static).update(Content.from_text(t("guide.body")))
        self._update_dash_summary()

    def _set_patch_mode(self, mode: PatchMode, *, persist: bool = True) -> None:
        self.config.patch_mode = mode
        if persist and (self.cfg_mgr.exists() or self.config.onboarding_completed):
            self.cfg_mgr.save(self.config)
        self.query_one("#sel-patch-workflow", Select).value = mode
        self._refresh_locale_widgets()

    # --- Actions (also called from Global Command Palette) ---

    def action_switch_screen(self, screen_id: str) -> None:
        self.query_one("#main-switcher", ContentSwitcher).current = screen_id
        for btn_id, sid, _, _ in NAV_KEYS:
            btn = self.query_one(f"#{btn_id}", Button)
            if sid == screen_id:
                btn.add_class("active")
            else:
                btn.remove_class("active")

    def action_cycle_language(self) -> None:
        self._apply_locale(cycle_locale(), persist=True)

    def action_toggle_patch_mode(self) -> None:
        nxt: PatchMode = "auto" if self.config.patch_mode == "manual" else "manual"
        self._set_patch_mode(nxt, persist=True)

    def action_select_all_apps(self) -> None:
        self.checked_tools = {a.tool_name for a in self.detected_apps if a.installed}
        self._sync_checklist_widgets()

    def action_deselect_all_apps(self) -> None:
        self.checked_tools.clear()
        self._sync_checklist_widgets()

    def action_toggle_tool_checked(self, tool_name: str) -> None:
        installed = {a.tool_name for a in self.detected_apps if a.installed}
        if tool_name not in installed:
            return
        if tool_name in self.checked_tools:
            self.checked_tools.discard(tool_name)
        else:
            self.checked_tools.add(tool_name)
        self._sync_checklist_widgets()

    def action_open_dry_run(self) -> None:
        self.action_switch_screen("screen-patch")
        self._run_patch_worker(dry_run=True)

    def action_run_doctor(self) -> None:
        self.action_switch_screen("screen-health")
        self._run_health_worker(repair=False, dry_run=True)

    def action_run_repair_preview(self) -> None:
        self.action_switch_screen("screen-health")
        self._run_health_worker(repair=True, dry_run=True)

    def action_create_manual_backup(self) -> None:
        self.action_switch_screen("screen-backups")
        self._create_manual_backup_worker()

    def action_verify_all_backups(self) -> None:
        self.action_switch_screen("screen-backups")
        self._verify_backups_worker()

    def action_check_updates(self, force: bool = True) -> None:
        self._check_updates_worker(force=force)

    def action_generate_board(self) -> None:
        self._generate_and_open_board(open_browser=False)

    def action_export_bundle(self) -> None:
        self._export_bundle_now()

    def action_create_support_bundle(self) -> None:
        self._create_support_bundle_now()

    def _sync_checklist_widgets(self) -> None:
        installed = {a.tool_name for a in self.detected_apps if a.installed}
        self.checked_tools &= installed
        for cb in self.query("#detected-checklist Checkbox"):
            if isinstance(cb, Checkbox) and cb.id and cb.id.startswith("chk-app-"):
                tname = cb.id.removeprefix("chk-app-")
                cb.value = tname in self.checked_tools
        self._update_dash_summary()
        self._refresh_tools_table()

    # --- Background Workers ---

    @work(thread=True, exclusive=True, group="scan")
    def action_trigger_scan(self, open_wizard_after: bool = False) -> None:
        self.call_from_thread(self._set_status, "status.scanning", "busy")
        self.active_bus.reset_cancel()
        engine = PatchEngine(self.config, bus=self.active_bus)
        try:
            apps = engine.scan(self.discovery_env, check_processes=self.check_processes)
        except CancelledError:
            self.call_from_thread(self._set_status, "status.idle", "")
            return
        self.call_from_thread(self._on_scan_complete, apps, open_wizard_after)

    async def _on_scan_complete(self, apps: list[DetectedAppStatus], open_wizard_after: bool) -> None:
        self.detected_apps = apps
        installed_names = {a.tool_name for a in apps if a.installed}
        if not self.config.tools:
            disc_map = discover_tools(self.discovery_env)
            self.config = merge_discovery_into_config(self.config, disc_map)
            self.checked_tools = set(installed_names)
        else:
            # Strict 1:1 parity: ONLY installed tools with enabled config (never stale non-installed keys!)
            self.checked_tools = {
                a.tool_name
                for a in apps
                if a.installed and self.config.is_tool_enabled(a.tool_name)
            }
        self.checked_tools &= installed_names

        await self._render_dashboard_checklist()
        self._refresh_tools_table()
        self._refresh_health_table_from_scan()
        self._update_dash_summary()
        self._rebuild_palette_index_worker()

        has_running = any(a.running_pids for a in apps if a.tool_name in self.checked_tools)
        self._set_status("status.running_warn" if has_running else "status.idle", "warn" if has_running else "")

        if open_wizard_after:
            self._open_onboarding_wizard()

    @work(thread=True, exclusive=True, group="palette")
    def _rebuild_palette_index_worker(self) -> None:
        reg = get_registry()
        entries: list[tuple[str, str, str, str]] = []
        for a_st in self.detected_apps:
            if not a_st.installed or a_st.tool_name not in self.config.tools:
                continue
            try:
                adapter = reg.create(a_st.tool_name, self.config.tools[a_st.tool_name])
                if adapter.capabilities.skills:
                    for sname, sk in list(adapter.read_skills().items())[:40]:
                        entries.append(("Skill", sname, f"{a_st.tool_name} - {sk.description[:50]}", "screen-patch"))
                if adapter.capabilities.mcp:
                    for mname, ms in list(adapter.read_mcp_servers().items())[:40]:
                        entries.append(("MCP", mname, f"{a_st.tool_name} ({ms.transport})", "screen-patch"))
                if adapter.capabilities.chats:
                    projs = adapter.read_projects() if adapter.capabilities.projects else {}
                    for ch in adapter.read_chats(projs)[:25]:
                        entries.append(("Chat", ch.title or ch.native_id[:16], f"{a_st.tool_name} ({len(ch.messages)} msgs)", "screen-handoff"))
            except Exception:
                continue
        self.palette_index = entries

    @work(thread=True, exclusive=True, group="updates")
    def _check_updates_worker(self, *, force: bool = False) -> None:
        info = check_for_updates(self.config.update_cache_path, force=force)
        self.call_from_thread(self._apply_update_info, info)

    def _apply_update_info(self, info: UpdateCheckResult) -> None:
        self.update_info = info
        badge = self.query_one("#btn-update-badge", Button)
        status_lbl = self.query_one("#lbl-update-status", Static)
        if info.error:
            badge.remove_class("visible")
            status_lbl.update(Content.from_text(t("settings.update_err", error=info.error[:80])))
        elif info.update_available:
            badge.label = Content.from_text(t("status.update_badge", version=info.latest_version))
            badge.add_class("visible")
            status_lbl.update(
                Content.from_text(
                    t(
                        "settings.update_avail",
                        current=info.current_version,
                        latest=info.latest_version,
                        method=info.install_method,
                    )
                )
            )
        else:
            badge.remove_class("visible")
            status_lbl.update(
                Content.from_text(
                    t(
                        "settings.update_current",
                        current=info.current_version,
                        method=info.install_method,
                    )
                )
            )

    @work(thread=True, exclusive=True, group="update_apply")
    def _apply_update_worker(self) -> None:
        self.call_from_thread(self._set_status, "status.patching", "busy")
        res = perform_update()
        self.call_from_thread(self._set_status, "status.idle", "")
        if res.success:
            msg = f"Updated agentsync: v{res.previous_version} -> v{res.new_version or 'latest'}"
            if res.restart_notice:
                msg += f" ({res.restart_notice})"
            self.call_from_thread(self.notify, msg, severity="information")
        else:
            err = res.restart_notice or res.error or "Update failed"
            self.call_from_thread(self.notify, err, severity="error")

    def _status_text_for(self, st: DetectedAppStatus) -> str:
        if st.status == "locked":
            return f"({t('dash.status_locked')})"
        if st.status == "needs_patch":
            return f"({t('dash.status_needs_patch', issues=st.issue_count)})"
        if st.status == "up_to_date":
            return f"({t('dash.status_up_to_date')})"
        if st.status == "detected":
            return f"({t('dash.status_detected')})"
        return f"({t('dash.status_not_found')})"

    async def _render_dashboard_checklist(self) -> None:
        det_box = self.query_one("#detected-checklist", Vertical)
        undet_box = self.query_one("#undetected-list", Vertical)
        await det_box.remove_children()
        await undet_box.remove_children()

        for app_st in self.detected_apps:
            if app_st.installed:
                counts = " ".join(f"{k}:{v}" for k, v in app_st.entity_counts.items())
                label = (
                    f"{app_st.display_name} ({app_st.client_kind.upper()}) "
                    f"{self._status_text_for(app_st)} {counts} - {app_st.root_path}"
                )
                cb = AgentCheckbox(
                    label,
                    value=app_st.tool_name in self.checked_tools,
                    ascii_mode=self.ascii_mode,
                    id=f"chk-app-{app_st.tool_name}",
                    classes="app-check-row",
                )
                await det_box.mount(cb)
            else:
                await undet_box.mount(
                    Static(
                        f"  [ ] {app_st.display_name} ({app_st.client_kind}) - {t('dash.status_not_found')} ({app_st.root_path})",
                        markup=False,
                    )
                )

    def _refresh_tools_table(self) -> None:
        tbl = self.query_one("#tools-table", DataTable)
        tbl.clear()
        for a in self.detected_apps:
            tbl.add_row(
                a.tool_name,
                a.client_kind,
                "yes" if a.installed else "no",
                "[X]" if (a.installed and a.tool_name in self.checked_tools) else "[ ]",
                self._status_text_for(a),
                a.root_path,
                key=a.tool_name,
            )

    def _refresh_health_table_from_scan(self) -> None:
        tbl = self.query_one("#health-table", DataTable)
        tbl.clear()
        issues = [
            iss
            for a in self.detected_apps
            if a.installed and a.tool_name in self.checked_tools
            for iss in a.issues
        ]
        for iss in issues:
            tbl.add_row(iss.severity.upper(), iss.tool, iss.code, "yes" if iss.fixable else "no", iss.message)
        fixable = sum(1 for i in issues if i.fixable)
        summary = t("health.found", count=len(issues), fixable=fixable) if issues else t("health.clean")
        self.query_one("#health-summary", Static).update(Content.from_text(summary))

    def _update_dash_summary(self) -> None:
        installed_names = {a.tool_name for a in self.detected_apps if a.installed}
        detected_cnt = len(installed_names)
        selected_cnt = len(self.checked_tools & installed_names)
        backups = len(BackupManager(self.config).list_backups())
        last_run = "-"
        conflicts = 0
        if self.config.state_db_path.exists():
            st = StateStore(self.config.state_db_path, readonly=True)
            try:
                runs = st.list_runs(limit=1)
                if runs:
                    last_run = str(runs[0].get("mode") or "-")
                conflicts = len(st.list_conflicts(limit=50))
            finally:
                st.close()
        self.query_one("#dash-summary", Static).update(
            Content.from_text(
                t(
                    "dash.summary",
                    selected=selected_cnt,
                    detected=detected_cnt,
                    last_run=last_run,
                    backups=backups,
                    conflicts=conflicts,
                )
            )
        )

    def _refresh_backups_view(self) -> None:
        bmgr = BackupManager(self.config)
        backups = bmgr.list_backups()
        bt = self.query_one("#backups-table", DataTable)
        bt.clear()
        for b in backups[:25]:
            bt.add_row(
                b.backup_id,
                b.created_at_utc[:19],
                b.reason,
                ", ".join(b.tools),
                str(len(b.entries)),
                key=b.backup_id,
            )
        size_kb = bmgr.total_backup_bytes() // 1024
        self.query_one("#backups-summary", Static).update(
            Content.from_text(
                t(
                    "backups.summary",
                    count=len(backups),
                    max_backups=self.config.max_backups,
                    max_days=self.config.max_backup_days,
                    size_kb=size_kb,
                )
            )
        )

        lt = self.query_one("#ledger-table", DataTable)
        lt.clear()
        for entry in LedgerStore(self.config).list_apply_ledgers(limit=15):
            lt.add_row(
                entry.timestamp_utc[:19],
                entry.mode,
                ", ".join(entry.tools),
                str(len(entry.items)),
                entry.plan_hash[:12],
            )

    def _refresh_autostart_status(self) -> None:
        st = autostart_status(self.config, config_path=self.cfg_mgr.config_path)
        self._suppress_switch_events = True
        try:
            self.query_one("#sw-autostart", Switch).value = st.enabled
        finally:
            self._suppress_switch_events = False
        self.query_one("#lbl-autostart-status", Static).update(
            Content.from_text(
                t(
                    "settings.autostart_status",
                    state="ON" if st.enabled else "OFF",
                    detail=st.detail,
                )
            )
        )

    def _open_onboarding_wizard(self) -> None:
        def _done(result: dict[str, Any] | None) -> None:
            if not result:
                return
            self._apply_locale(result.get("locale", self.config.locale), persist=False)
            self._set_patch_mode(result.get("patch_mode", self.config.patch_mode), persist=False)
            selected = set(result.get("selected_tools") or [])
            if selected:
                self.checked_tools = selected
                for name, tc in self.config.tools.items():
                    tc.enabled = name in selected
            self.config.onboarding_completed = True
            self.cfg_mgr.save(self.config)
            self._sync_checklist_widgets()
            if result.get("action") == "dry_run":
                self.action_switch_screen("screen-patch")
                self._run_patch_worker(dry_run=True)

        self.push_screen(
            OnboardingModal(
                initial_apps=self.detected_apps,
                initial_locale=get_locale(),
                initial_patch_mode=self.config.patch_mode,
                ascii_mode=self.ascii_mode,
                on_locale_changed=lambda loc: self._apply_locale(loc, persist=False),
            ),
            _done,
        )

    # --- Patch / Sync / Repair / Backup / Handoff Workers ---

    def _selected_installed_tools(self) -> list[str]:
        installed = {a.tool_name for a in self.detected_apps if a.installed}
        return sorted(self.checked_tools & installed) if installed else sorted(self.checked_tools)

    def action_trigger_quick_patch(self) -> None:
        selected = self._selected_installed_tools()
        if not selected:
            self.notify(t("dash.none_selected"), severity="warning")
            return
        self.action_switch_screen("screen-patch")
        if self.config.patch_mode == "manual":
            self._run_patch_worker(dry_run=True)
        else:
            self._confirm_and_apply_patch()

    def _confirm_and_apply_patch(self) -> None:
        selected = self._selected_installed_tools()
        if not selected:
            self.notify(t("dash.none_selected"), severity="warning")
            return
        tools_str = ", ".join(selected)
        body = t("modal.confirm_patch_body", count=len(selected), tools=tools_str)

        def _on_confirm(ok: bool | None) -> None:
            if ok:
                self._run_patch_worker(dry_run=False)

        self.push_screen(ConfirmModal(t("modal.confirm_title"), body), _on_confirm)

    def _collect_patch_options(self, *, dry_run: bool) -> SyncOptions:
        inc_raw = self.query_one("#inp-include", Input).value.strip()
        exc_raw = self.query_one("#inp-exclude", Input).value.strip()
        scope_val = str(self.query_one("#sel-patch-scope", Select).value or "all")
        return SyncOptions(
            dry_run=dry_run,
            force=self.query_one("#chk-opt-force", Checkbox).value,
            prune=self.query_one("#chk-opt-prune", Checkbox).value,
            strip_secrets=self.query_one("#chk-opt-strip", Checkbox).value,
            strict_vocab_guard=self.config.strict_vocab_guard,
            skill_link_mode=self.config.skill_link_mode,
            scope=scope_val,  # type: ignore[arg-type]
            include=[inc_raw] if inc_raw else [],
            exclude=[exc_raw] if exc_raw else [],
            targets=self._selected_installed_tools(),
        )

    @work(thread=True, exclusive=True, group="patch")
    def _run_patch_worker(self, *, dry_run: bool) -> None:
        self.call_from_thread(self._set_status, "status.patching", "busy")
        self.active_bus.reset_cancel()
        opts = self.call_from_thread(self._collect_patch_options, dry_run=dry_run)
        engine = PatchEngine(self.config, bus=self.active_bus)
        try:
            result = engine.run_patch(
                selected_tools=opts.targets,
                mode=self.config.patch_mode,
                dry_run=dry_run,
                options=opts,
                check_processes=self.check_processes,
            )
            self.call_from_thread(self._on_patch_complete, result)
        except CancelledError:
            self.call_from_thread(self._set_status, "status.idle", "")
            self.call_from_thread(self.notify, t("progress.cancelled"), severity="warning")
        except Exception as exc:
            self.call_from_thread(self._set_status, "status.idle", "")
            self.call_from_thread(self.notify, t("progress.error", error=str(exc)), severity="error")

    def _on_patch_complete(self, result: PatchRunResult) -> None:
        self.last_patch_result = result
        self._diff_actions = list(result.sync_plan.actions)
        self._set_status("status.idle", "")
        tbl = self.query_one("#patch-diff-table", DataTable)
        tbl.clear()
        for idx, act in enumerate(self._diff_actions):
            tbl.add_row(
                act.category,
                act.action,
                act.target_tool,
                act.risk_level.upper(),
                act.entity_id[:36],
                act.summary[:80],
                key=str(idx),
            )
        if result.sync_plan.is_empty:
            self.notify(t("patch.no_changes"), severity="information")
        elif result.dry_run:
            self.notify(
                t(
                    "patch.preview_ready",
                    mutating=len(result.sync_plan.mutating_actions),
                    skipped=len(result.sync_plan.skipped_actions),
                    conflicts=len(result.sync_plan.conflict_actions),
                ),
                severity="information",
            )
        else:
            self.notify(
                t(
                    "patch.applied_ok",
                    mutating=len(result.sync_plan.mutating_actions),
                    tools=len(result.selected_tools),
                    backup=result.backup_id or "-",
                ),
                severity="information",
            )
            self._refresh_backups_view()
            self._update_dash_summary()

    @work(thread=True, exclusive=True, group="health")
    def _run_health_worker(self, *, repair: bool, dry_run: bool) -> None:
        self.call_from_thread(self._set_status, "status.scanning", "busy")
        reg = get_registry()
        adapters = [
            reg.create(name, self.config.tools[name])
            for name in self._selected_installed_tools()
            if name in self.config.tools and name in reg.list_names()
        ]
        doc = DoctorEngine(self.config, adapters)
        issues = doc.diagnose()
        rep_msg = ""
        if repair and issues:
            try:
                results, bid = doc.repair(
                    dry_run=dry_run,
                    force=not self.check_processes,
                    check_processes=self.check_processes,
                )
                total_fixed = sum(r.fixed_count for r in results)
                rep_msg = f" | {'Would fix' if dry_run else 'Fixed'}: {total_fixed}" + (
                    f" (backup: {bid})" if bid else ""
                )
                if not dry_run:
                    issues = doc.diagnose()
            except Exception as exc:
                self.call_from_thread(self.notify, t("progress.error", error=str(exc)), severity="error")
        self.call_from_thread(self._on_health_done, issues, rep_msg)

    def _on_health_done(self, issues: list[Any], rep_msg: str) -> None:
        self._set_status("status.idle", "")
        tbl = self.query_one("#health-table", DataTable)
        tbl.clear()
        for iss in issues:
            tbl.add_row(iss.severity.upper(), iss.tool, iss.code, "yes" if iss.fixable else "no", iss.message)
        fixable = sum(1 for i in issues if i.fixable)
        base = t("health.found", count=len(issues), fixable=fixable) if issues else t("health.clean")
        self.query_one("#health-summary", Static).update(Content.from_text(base + rep_msg))
        self._refresh_backups_view()

    @work(thread=True, exclusive=True, group="backups")
    def _create_manual_backup_worker(self) -> None:
        self.call_from_thread(self._set_status, "status.patching", "busy")
        reg = get_registry()
        adapters = [
            reg.create(n, self.config.tools[n])
            for n in self._selected_installed_tools()
            if n in self.config.tools and n in reg.list_names()
        ]
        try:
            mf = BackupManager(self.config).create_backup(adapters, reason="manual-tui")
            self.call_from_thread(
                self.notify,
                t("backups.created", backup_id=mf.backup_id, files=len(mf.entries)),
                severity="information",
            )
        except Exception as exc:
            self.call_from_thread(self.notify, t("progress.error", error=str(exc)), severity="error")
        finally:
            self.call_from_thread(self._set_status, "status.idle", "")
            self.call_from_thread(self._refresh_backups_view)
            self.call_from_thread(self._update_dash_summary)

    @work(thread=True, exclusive=True, group="backups")
    def _verify_backups_worker(self) -> None:
        self.call_from_thread(self._set_status, "status.scanning", "busy")
        reports = BackupManager(self.config).verify_all_backups()
        failed = [r for r in reports if not r.ok]
        self.call_from_thread(self._set_status, "status.idle", "")
        if not failed:
            self.call_from_thread(
                self.notify,
                t("backups.verify_ok", count=len(reports)),
                severity="information",
            )
        else:
            detail = "; ".join(f"{r.backup_id}: {r.errors[0]}" for r in failed[:3] if r.errors)
            self.call_from_thread(
                self.notify,
                t("backups.verify_fail", failed=len(failed), count=len(reports), detail=detail),
                severity="error",
            )

    @work(thread=True, exclusive=True, group="handoff")
    def _refresh_handoff_sessions(self) -> None:
        src = str(self.query_one("#sel-handoff-src", Select).value or "codex")
        flt = self.query_one("#inp-handoff-filter", Input).value.strip().lower()
        reg = get_registry()
        adapters = {
            name: reg.create(name, tc)
            for name, tc in self.config.tools.items()
            if name in reg.list_names() and tc.path
        }
        eng = HandoffEngine(self.config, adapters)
        sessions = eng.list_sessions(src)
        rows = []
        for s in sessions:
            proj = s.project_path or "-"
            if flt and flt not in (s.title or "").lower() and flt not in proj.lower():
                continue
            rows.append((src, s.native_id, s.title or s.native_id, proj, len(s.messages)))
        self.call_from_thread(self._render_handoff_rows, rows)

    def _render_handoff_rows(self, rows: list[tuple[str, str, str, str, int]]) -> None:
        self.handoff_sessions = rows
        tbl = self.query_one("#handoff-table", DataTable)
        tbl.clear()
        for src, sid, title, proj, cnt in rows:
            tbl.add_row(src, sid[:16], title[:42], proj[:32], str(cnt), key=sid)

    @work(thread=True, exclusive=True, group="handoff")
    def _run_handoff_worker(self, *, execute: bool) -> None:
        src = str(self.query_one("#sel-handoff-src", Select).value or "codex")
        tgt = str(self.query_one("#sel-handoff-tgt", Select).value or "antigravity")
        launch: LaunchMode = str(self.query_one("#sel-handoff-open", Select).value or "none")  # type: ignore[assignment]
        tbl = self.query_one("#handoff-table", DataTable)
        sid: str | None = None
        if self.handoff_sessions and tbl.cursor_row is not None and 0 <= tbl.cursor_row < len(self.handoff_sessions):
            sid = self.handoff_sessions[tbl.cursor_row][1]
        reg = get_registry()
        adapters = {
            name: reg.create(name, tc)
            for name, tc in self.config.tools.items()
            if name in reg.list_names() and tc.path
        }
        eng = HandoffEngine(self.config, adapters, bus=self.active_bus)
        try:
            if not execute:
                prev = eng.preview(src, tgt, session_id=sid)
                msg = (
                    f"Preview: {prev.source_tool} -> {prev.target_tool}\n"
                    f"Session: {prev.title} ({prev.session_id})\n"
                    f"Messages: {prev.message_count} | Skills: {prev.active_skills_count} | "
                    f"Project rules: {'yes' if prev.has_project_instructions else 'no'}"
                )
            else:
                res = eng.execute(
                    src,
                    tgt,
                    session_id=sid,
                    open_mode=launch,
                    force=not self.check_processes,
                    check_processes=self.check_processes,
                )
                msg = (
                    t("handoff.reused", target=res.target_tool, thread_id=res.target_session_id)
                    if res.reused_existing
                    else t(
                        "handoff.verified",
                        target=res.target_tool,
                        thread_id=res.target_session_id,
                        messages=res.verified_message_count,
                    )
                )
            self.call_from_thread(self._set_handoff_output, msg)
        except Exception as exc:
            self.call_from_thread(self._set_handoff_output, t("progress.error", error=str(exc)))

    def _set_handoff_output(self, msg: str) -> None:
        self.query_one("#handoff-output", Static).update(Content.from_text(msg))
        self._refresh_backups_view()

    # --- Event Handlers ---

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "patch-diff-table" and self._diff_actions:
            idx = event.cursor_row
            if 0 <= idx < len(self._diff_actions):
                self.push_screen(DiffModal(self._diff_actions[idx]))

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        cid = event.checkbox.id or ""
        if cid.startswith("chk-app-"):
            tname = cid.removeprefix("chk-app-")
            if event.value:
                self.checked_tools.add(tname)
            else:
                self.checked_tools.discard(tname)
            if tname in self.config.tools:
                self.config.tools[tname].enabled = event.value
            self._update_dash_summary()
            self._refresh_tools_table()

    def on_switch_changed(self, event: Switch.Changed) -> None:
        if self._suppress_switch_events:
            return
        sid = event.switch.id or ""
        if sid == "sw-autoscan":
            self.config.auto_scan_on_startup = event.value
        elif sid == "sw-strict-vocab":
            self.config.strict_vocab_guard = event.value
        elif sid == "sw-notifications":
            self.config.notifications = event.value
        elif sid == "sw-autostart":
            try:
                if event.value:
                    st = enable_autostart(self.config, config_path=self.cfg_mgr.config_path)
                else:
                    st = disable_autostart(self.config)
                self.config.autostart_enabled = st.enabled
                self.query_one("#lbl-autostart-status", Static).update(
                    Content.from_text(
                        t(
                            "settings.autostart_status",
                            state="ON" if st.enabled else "OFF",
                            detail=st.detail,
                        )
                    )
                )
            except Exception as exc:
                self.notify(t("progress.error", error=str(exc)), severity="error")
                self._refresh_autostart_status()
                return
        if self.cfg_mgr.exists() or self.config.onboarding_completed:
            self.cfg_mgr.save(self.config)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "sel-link-mode" and event.value in ("auto", "symlink", "copy"):
            self.config.skill_link_mode = str(event.value)  # type: ignore[assignment]
            if self.cfg_mgr.exists():
                self.cfg_mgr.save(self.config)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""

        for nav_btn_id, screen_id, _, _ in NAV_KEYS:
            if bid == nav_btn_id:
                self.action_switch_screen(screen_id)
                return

        if bid in ("btn-lang-en", "btn-set-lang-en"):
            self._apply_locale("en", persist=True)
            return
        if bid in ("btn-lang-ru", "btn-set-lang-ru"):
            self._apply_locale("ru", persist=True)
            return
        if bid in ("btn-lang-zh", "btn-set-lang-zh"):
            self._apply_locale("zh", persist=True)
            return

        if bid == "btn-mode-toggle":
            self.action_toggle_patch_mode()
            return
        if bid == "btn-set-mode-auto":
            self._set_patch_mode("auto", persist=True)
            return
        if bid == "btn-set-mode-manual":
            self._set_patch_mode("manual", persist=True)
            return

        if bid == "btn-update-badge" and self.update_info and self.update_info.update_available:
            self._open_update_modal()
            return

        # Dashboard
        if bid == "btn-select-all":
            self.action_select_all_apps()
        elif bid == "btn-deselect-all":
            self.action_deselect_all_apps()
        elif bid in ("btn-rescan", "btn-tools-rescan"):
            self.action_trigger_scan(open_wizard_after=False)
        elif bid == "btn-quick-patch":
            self.action_trigger_quick_patch()
        elif bid == "btn-quick-dryrun":
            self.action_open_dry_run()
        elif bid == "btn-quick-handoff":
            self.action_switch_screen("screen-handoff")
            self._refresh_handoff_sessions()
        elif bid == "btn-quick-doctor":
            self.action_run_doctor()
        elif bid in ("btn-quick-board", "btn-open-board"):
            self._generate_and_open_board(open_browser=False)
        elif bid in ("btn-quick-wizard", "btn-rerun-wizard"):
            self._open_onboarding_wizard()

        # Patch & Sync
        elif bid == "btn-patch-preview":
            self._run_patch_worker(dry_run=True)
        elif bid == "btn-patch-apply":
            if self.query_one("#chk-opt-dryrun", Checkbox).value:
                self._run_patch_worker(dry_run=True)
            else:
                self._confirm_and_apply_patch()
        elif bid == "btn-patch-cancel":
            self.active_bus.cancel()

        # Handoff
        elif bid == "btn-handoff-refresh":
            self._refresh_handoff_sessions()
        elif bid == "btn-handoff-preview":
            self._run_handoff_worker(execute=False)
        elif bid == "btn-handoff-execute":
            self._run_handoff_worker(execute=True)

        # Live Watch
        elif bid == "btn-watch-start":
            self._start_watch()
        elif bid == "btn-watch-pause":
            self._pause_watch()
        elif bid == "btn-watch-resume":
            self._resume_watch()
        elif bid == "btn-watch-stop":
            self._stop_watch()

        # Tools
        elif bid == "btn-tools-save":
            for name, tc in self.config.tools.items():
                tc.enabled = name in self.checked_tools
            self.cfg_mgr.save(self.config)
            self.notify(t("tools.saved", path=str(self.cfg_mgr.config_path)))
        elif bid == "btn-set-custom-path":
            self._apply_custom_tool_path()

        # Health
        elif bid == "btn-health-run":
            self._run_health_worker(repair=False, dry_run=True)
        elif bid == "btn-health-repair-dry":
            self._run_health_worker(repair=True, dry_run=True)
        elif bid == "btn-health-repair":
            self._run_health_worker(repair=True, dry_run=False)

        # Backups
        elif bid == "btn-backups-create":
            self._create_manual_backup_worker()
        elif bid == "btn-backups-verify":
            self._verify_backups_worker()
        elif bid == "btn-backups-refresh":
            self._refresh_backups_view()
        elif bid == "btn-backups-restore":
            self._confirm_restore_backup()

        # Settings extras
        elif bid == "btn-check-update":
            self._check_updates_worker(force=True)
        elif bid == "btn-apply-update":
            self._open_update_modal()
        elif bid == "btn-export-bundle":
            self._export_bundle_now()
        elif bid == "btn-support-bundle":
            self._create_support_bundle_now()
        elif bid == "btn-install-skill":
            reg = get_registry()
            adapters = [
                reg.create(n, self.config.tools[n])
                for n in self._selected_installed_tools()
                if n in self.config.tools and n in reg.list_names()
            ]
            installed = install_agentsync_skill(adapters, dry_run=False)
            self.notify(t("settings.skill_installed", count=len(installed)))
        elif bid == "btn-open-cfg-dir":
            self._open_folder(self.cfg_mgr.config_path.parent)
        elif bid == "btn-open-bkp-dir":
            self._open_folder(self.config.backups_dir)

    def _open_update_modal(self) -> None:
        info = self.update_info or check_for_updates(self.config.update_cache_path, force=False)
        self.update_info = info

        def _on_confirm(ok: bool | None) -> None:
            if ok:
                self._apply_update_worker()

        self.push_screen(UpdateModal(info), _on_confirm)

    def _export_bundle_now(self) -> None:
        reg = get_registry()
        adapters = [
            reg.create(n, self.config.tools[n])
            for n in self._selected_installed_tools()
            if n in self.config.tools and n in reg.list_names()
        ]
        ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        out_zip = self.config.resolved_data_dir() / f"agentsync-profile-{ts}.zip"
        mf = export_profile_bundle(out_zip, adapters, include_secrets=False)
        self.notify(
            t("settings.bundle_exported", path=str(out_zip), skills=mf.skill_count, mcp=mf.mcp_count),
            severity="information",
        )

    def _create_support_bundle_now(self) -> None:
        reg = get_registry()
        adapters = [
            reg.create(n, self.config.tools[n])
            for n in self._selected_installed_tools()
            if n in self.config.tools and n in reg.list_names()
        ]
        ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        out_zip = self.config.resolved_data_dir() / f"agentsync-support-{ts}.zip"
        create_support_bundle(out_zip, self.config, adapters)
        self.notify(t("settings.support_exported", path=str(out_zip)), severity="information")

    def _apply_custom_tool_path(self) -> None:
        tbl = self.query_one("#tools-table", DataTable)
        new_path = self.query_one("#inp-custom-path", Input).value.strip()
        if not new_path or tbl.cursor_row is None or not (0 <= tbl.cursor_row < len(self.detected_apps)):
            return
        tname = self.detected_apps[tbl.cursor_row].tool_name
        if tname not in self.config.tools:
            self.config.tools[tname] = ToolConfig(enabled=True, path=new_path)
        else:
            self.config.tools[tname].path = new_path
        self.cfg_mgr.save(self.config)
        self.notify(f"{tname} -> {new_path}")
        self.action_trigger_scan(open_wizard_after=False)

    def _generate_and_open_board(self, *, open_browser: bool = False) -> None:
        reg = get_registry()
        adapters = [
            reg.create(n, self.config.tools[n])
            for n in self._selected_installed_tools()
            if n in self.config.tools and n in reg.list_names()
        ]
        out_path = self.config.resolved_data_dir() / "inventory-board.html"
        path, _count = generate_inventory_board(adapters, out_path)
        self.notify(t("settings.board_generated", path=str(path)))
        if open_browser:
            webbrowser.open(path.as_uri())

    def _confirm_restore_backup(self) -> None:
        backups = BackupManager(self.config).list_backups()[:25]
        tbl = self.query_one("#backups-table", DataTable)
        if not backups or tbl.cursor_row is None or not (0 <= tbl.cursor_row < len(backups)):
            return
        bid = backups[tbl.cursor_row].backup_id

        def _on_ok(ok: bool | None) -> None:
            if not ok:
                return
            reg = get_registry()
            adapters = {
                n: reg.create(n, tc)
                for n, tc in self.config.tools.items()
                if n in reg.list_names() and tc.path
            }
            try:
                res = BackupManager(self.config).restore_backup(
                    bid,
                    adapters_by_name=adapters,
                    force=not self.check_processes,
                    check_processes=self.check_processes,
                )
                self.notify(
                    t("backups.restored", backup_id=bid, restored=res.restored_files, removed=res.removed_files)
                )
                self._refresh_backups_view()
            except Exception as exc:
                self.notify(t("progress.error", error=str(exc)), severity="error")

        self.push_screen(
            ConfirmModal(t("modal.confirm_title"), t("modal.confirm_restore_body", backup_id=bid)),
            _on_ok,
        )

    # --- Real Live SyncWatcher wired to Screen 4 ---

    def _on_watch_log_bg(self, level: str, message: str) -> None:
        try:
            self.call_from_thread(self._append_watch_log, level, message)
        except Exception:
            pass

    def _append_watch_log(self, level: str, message: str) -> None:
        ts = datetime.now(UTC).strftime("%H:%M:%S")
        self.query_one("#watch-log", RichLog).write(f"{ts} [{level.upper()}] {message}")

    def _on_watch_sync_done_bg(self, _plan: SyncPlan) -> None:
        try:
            self.call_from_thread(self._refresh_watch_panel)
            self.call_from_thread(self._refresh_backups_view)
            self.call_from_thread(self._update_dash_summary)
        except Exception:
            pass

    def _refresh_watch_panel(self) -> None:
        if self._watcher is None:
            self.query_one("#watch-state-label", Static).update(Content.from_text(t("watch.stopped")))
            self.query_one("#watch-roots-label", Static).update("")
            return
        snap = self._watcher.status_snapshot()
        if snap.paused:
            self._set_status("status.paused", "warn")
            self.query_one("#watch-state-label", Static).update(
                Content.from_text(
                    t("watch.paused_state", pending=len(snap.pending_queue), syncs=snap.sync_count)
                )
            )
        else:
            locked_str = ", ".join(snap.running_locked_apps) if snap.running_locked_apps else "none"
            self._set_status("status.running_warn" if snap.running_locked_apps else "status.watching", "warn" if snap.running_locked_apps else "busy")
            self.query_one("#watch-state-label", Static).update(
                Content.from_text(
                    t(
                        "watch.active",
                        count=len(self._watcher.adapters),
                        roots=len(snap.watched_roots),
                        locked=locked_str,
                        pending=len(snap.pending_queue),
                        syncs=snap.sync_count,
                    )
                )
            )
        roots_preview = " | ".join(snap.watched_roots[:4])
        if len(snap.watched_roots) > 4:
            roots_preview += f" (+{len(snap.watched_roots) - 4} more)"
        self.query_one("#watch-roots-label", Static).update(Content.from_text(f"Watched: {roots_preview or '-'}"))

    def _start_watch(self) -> None:
        if self._watching and self._watcher is not None:
            self._watcher.resume()
            self._refresh_watch_panel()
            return
        reg = get_registry()
        adapters = [
            reg.create(n, self.config.tools[n])
            for n in self._selected_installed_tools()
            if n in self.config.tools and n in reg.list_names()
        ]
        if not adapters:
            self.notify(t("dash.none_selected"), severity="warning")
            return
        opts = SyncOptions(
            dry_run=False,
            force=not self.check_processes,
            include_subagents=self.config.include_subagents,
            debounce_seconds=self.config.debounce_seconds,
            targets=[a.tool_name for a in adapters],
        )
        self._watcher = SyncWatcher(
            self.config,
            adapters,
            options=opts,
            on_sync_done=self._on_watch_sync_done_bg,
            on_log=self._on_watch_log_bg,
            daemon_mode=False,
            run_startup_sync=False,
            poll_interval_seconds=1.0,
        )
        self._watcher.start()
        self._watching = True
        self._refresh_watch_panel()
        if self._watch_timer is None:
            self._watch_timer = self.set_interval(1.0, self._refresh_watch_panel)

    def _pause_watch(self) -> None:
        if self._watcher is not None:
            self._watcher.pause()
            self._refresh_watch_panel()

    def _resume_watch(self) -> None:
        if self._watcher is not None:
            self._watcher.resume()
            self._refresh_watch_panel()

    def _stop_watch(self) -> None:
        if self._watch_timer is not None:
            try:
                self._watch_timer.stop()
            except Exception:
                pass
            self._watch_timer = None
        if self._watcher is not None:
            try:
                self._watcher.stop()
            except Exception:
                pass
            self._watcher = None
        self._watching = False
        try:
            self._set_status("status.idle", "")
            self._refresh_watch_panel()
        except Exception:
            pass

    def _open_folder(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(str(folder))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception:
            self.notify(str(folder))


def launch_tui(
    *,
    config_path: Path | None = None,
    data_dir: Path | None = None,
    locale_override: str | None = None,
) -> None:
    """Launch the interactive Textual TUI application."""
    tui = AgentSyncApp(
        config_path=config_path,
        data_dir=data_dir,
        locale_override=locale_override,
    )
    tui.run()
