"""Full-featured 8-screen interactive Textual TUI for AgentSync with live EN/RU/ZH localization."""

from __future__ import annotations

import os
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Any

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
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
from agentsync.engine.board import generate_inventory_board
from agentsync.engine.doctor import DoctorEngine
from agentsync.engine.handoff import HandoffEngine, LaunchMode
from agentsync.engine.patch import DetectedAppStatus, PatchEngine, PatchRunResult
from agentsync.engine.skill_export import install_agentsync_skill
from agentsync.engine.watcher import SyncWatcher
from agentsync.i18n import cycle_locale, get_locale, set_locale, t
from agentsync.state.backup import BackupManager
from agentsync.state.ledger import LedgerStore
from agentsync.state.state_db import StateStore
from agentsync.tui.modals import ConfirmModal, OnboardingModal
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

NAV_KEYS: tuple[tuple[str, str, str], ...] = (
    ("nav-1", "screen-dashboard", "nav.dashboard"),
    ("nav-2", "screen-patch", "nav.patch"),
    ("nav-3", "screen-handoff", "nav.handoff"),
    ("nav-4", "screen-watch", "nav.watch"),
    ("nav-5", "screen-tools", "nav.tools"),
    ("nav-6", "screen-health", "nav.health"),
    ("nav-7", "screen-backups", "nav.backups"),
    ("nav-8", "screen-settings", "nav.settings"),
)


class AgentSyncApp(App[None]):
    """Interactive Terminal UI Suite for universal AI coding agent synchronization."""

    CSS_PATH = "styles.tcss"
    TITLE = f"AGENTSYNC v{__version__}"

    BINDINGS = [
        Binding("1", "switch_screen('screen-dashboard')", "Dashboard", show=False),
        Binding("2", "switch_screen('screen-patch')", "Patch", show=False),
        Binding("3", "switch_screen('screen-handoff')", "Handoff", show=False),
        Binding("4", "switch_screen('screen-watch')", "Watch", show=False),
        Binding("5", "switch_screen('screen-tools')", "Tools", show=False),
        Binding("6", "switch_screen('screen-health')", "Health", show=False),
        Binding("7", "switch_screen('screen-backups')", "Backups", show=False),
        Binding("8", "switch_screen('screen-settings')", "Settings", show=False),
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

        self.detected_apps: list[DetectedAppStatus] = []
        self.checked_tools: set[str] = {
            name for name, tc in self.config.tools.items() if tc.enabled
        }
        self.active_bus = ProgressBus(self._on_progress_event_bg)
        self.last_patch_result: PatchRunResult | None = None
        self.handoff_sessions: list[tuple[str, str, str, str, int]] = []
        self._watcher: SyncWatcher | None = None
        self._watching = False

    def compose(self) -> ComposeResult:
        # Persistent Animated Header
        with Horizontal(id="top-header"):
            yield Label(f"{t('app.title')} v{__version__}", id="brand-title")
            yield Label(t("status.idle"), id="status-badge")
            yield Label(self.env_profile.badge(), id="env-badge")
            yield Static("", classes="header-spacer")
            yield Button(
                self._mode_pill_label(),
                id="btn-mode-toggle",
                classes="auto-mode" if self.config.patch_mode == "auto" else "",
            )
            yield Button("EN", id="btn-lang-en", classes="lang-btn")
            yield Button("RU", id="btn-lang-ru", classes="lang-btn")
            yield Button("中文", id="btn-lang-zh", classes="lang-btn")

        # Main Navigation + 8-Screen Switcher
        with Horizontal(id="body-row"):
            with Vertical(id="nav-rail"):
                for btn_id, screen_id, loc_key in NAV_KEYS:
                    cls = "nav-btn active" if screen_id == "screen-dashboard" else "nav-btn"
                    yield Button(t(loc_key), id=btn_id, classes=cls)

            with ContentSwitcher(initial="screen-dashboard", id="main-switcher"):
                # 1. Dashboard & Quick Patch Launcher
                with VerticalScroll(id="screen-dashboard", classes="pane-scroll"):
                    yield Label(t("dash.title"), id="lbl-dash-title", classes="section-title")
                    with Horizontal(classes="btn-row"):
                        yield Button(t("dash.select_all"), id="btn-select-all", variant="default")
                        yield Button(t("dash.deselect_all"), id="btn-deselect-all", variant="default")
                        yield Button(t("dash.rescan"), id="btn-rescan", variant="primary")
                    with Vertical(id="detected-card", classes="card"):
                        yield Label(t("dash.detected_apps"), id="lbl-detected-apps", classes="section-title")
                        yield Vertical(id="detected-checklist")
                    with Collapsible(title=t("dash.undetected_apps"), id="undetected-collapsible", collapsed=True):
                        yield Vertical(id="undetected-list")
                    with Horizontal(classes="btn-row"):
                        yield Button(t("dash.patch_selected"), id="btn-quick-patch", variant="success")
                        yield Button(t("dash.dry_run"), id="btn-quick-dryrun", variant="primary")
                        yield Button(t("dash.handoff"), id="btn-quick-handoff", variant="default")
                        yield Button(t("dash.run_doctor"), id="btn-quick-doctor", variant="warning")
                        yield Button(t("dash.open_board"), id="btn-quick-board", variant="default")
                        yield Button(t("dash.run_wizard"), id="btn-quick-wizard", variant="default")
                    yield Static("", id="dash-summary")

                # 2. Patch, Sync & Bootstrap Center
                with VerticalScroll(id="screen-patch", classes="pane-scroll"):
                    yield Label(t("patch.title"), id="lbl-patch-title", classes="section-title")
                    with Vertical(classes="card"):
                        with Horizontal(classes="btn-row"):
                            yield Button(t("patch.btn_preview"), id="btn-patch-preview", variant="primary")
                            yield Button(t("patch.btn_apply"), id="btn-patch-apply", variant="success")
                            yield Button(t("patch.btn_cancel"), id="btn-patch-cancel", variant="error")
                        with Horizontal(classes="btn-row"):
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
                        with Horizontal(classes="btn-row"):
                            yield Input(placeholder=t("patch.include_placeholder"), id="inp-include")
                            yield Input(placeholder=t("patch.exclude_placeholder"), id="inp-exclude")
                        with Horizontal(classes="btn-row"):
                            yield Checkbox(t("patch.opt_dry_run"), value=True, id="chk-opt-dryrun")
                            yield Checkbox(t("patch.opt_prune"), value=False, id="chk-opt-prune")
                            yield Checkbox(t("patch.opt_strip_secrets"), value=self.config.strip_secrets, id="chk-opt-strip")
                            yield Checkbox(t("patch.opt_force"), value=False, id="chk-opt-force")
                    with Vertical(id="progress-panel"):
                        yield Label(t("status.idle"), id="op-status")
                        yield ProgressBar(total=100, show_eta=False, id="op-progress")
                    yield DataTable(id="patch-diff-table", cursor_type="row")
                    yield RichLog(id="op-log", highlight=True, markup=True)

                # 3. Context & Session Handoff Center
                with VerticalScroll(id="screen-handoff", classes="pane-scroll"):
                    yield Label(t("handoff.title"), id="lbl-handoff-title", classes="section-title")
                    yield Static(t("handoff.subtitle"), id="lbl-handoff-sub")
                    with Horizontal(classes="btn-row"):
                        yield Select(
                            [("codex", "codex"), ("antigravity", "antigravity"), ("claude-code", "claude-code"), ("opencode", "opencode")],
                            value="codex",
                            id="sel-handoff-src",
                        )
                        yield Select(
                            [("antigravity", "antigravity"), ("codex", "codex"), ("claude-code", "claude-code"), ("opencode", "opencode")],
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
                    with Horizontal(classes="btn-row"):
                        yield Button(t("handoff.btn_refresh"), id="btn-handoff-refresh", variant="primary")
                        yield Button(t("handoff.btn_preview"), id="btn-handoff-preview", variant="default")
                        yield Button(t("handoff.btn_execute"), id="btn-handoff-execute", variant="success")
                    yield DataTable(id="handoff-table", cursor_type="row")
                    yield Static("", id="handoff-output", classes="card")

                # 4. Live Watch Mode
                with VerticalScroll(id="screen-watch", classes="pane-scroll"):
                    yield Label(t("watch.title"), id="lbl-watch-title", classes="section-title")
                    yield Static(t("watch.subtitle"), id="lbl-watch-sub")
                    with Horizontal(classes="btn-row"):
                        yield Button(t("watch.btn_start"), id="btn-watch-start", variant="success")
                        yield Button(t("watch.btn_stop"), id="btn-watch-stop", variant="error")
                    yield Static(t("watch.stopped"), id="watch-state-label", classes="card")
                    yield RichLog(id="watch-log", highlight=True, markup=True)

                # 5. Tools, Paths & Auto-Discovery
                with VerticalScroll(id="screen-tools", classes="pane-scroll"):
                    yield Label(t("tools.title"), id="lbl-tools-title", classes="section-title")
                    with Horizontal(classes="btn-row"):
                        yield Button(t("tools.btn_rescan"), id="btn-tools-rescan", variant="primary")
                        yield Button(t("tools.btn_save"), id="btn-tools-save", variant="success")
                    yield DataTable(id="tools-table", cursor_type="row")
                    with Horizontal(classes="btn-row"):
                        yield Input(placeholder=t("tools.custom_path_placeholder"), id="inp-custom-path")
                        yield Button(t("tools.btn_set_path"), id="btn-set-custom-path", variant="default")

                # 6. Health, Audit & Repair
                with VerticalScroll(id="screen-health", classes="pane-scroll"):
                    yield Label(t("health.title"), id="lbl-health-title", classes="section-title")
                    with Horizontal(classes="btn-row"):
                        yield Button(t("health.btn_run"), id="btn-health-run", variant="primary")
                        yield Button(t("health.btn_repair_dry"), id="btn-health-repair-dry", variant="warning")
                        yield Button(t("health.btn_repair"), id="btn-health-repair", variant="success")
                    yield Static("", id="health-summary", classes="card")
                    yield DataTable(id="health-table", cursor_type="row")

                # 7. Backups, Apply Ledger & Conflicts
                with VerticalScroll(id="screen-backups", classes="pane-scroll"):
                    yield Label(t("backups.title"), id="lbl-backups-title", classes="section-title")
                    with Horizontal(classes="btn-row"):
                        yield Button(t("backups.btn_refresh"), id="btn-backups-refresh", variant="primary")
                        yield Button(t("backups.btn_restore"), id="btn-backups-restore", variant="warning")
                    yield DataTable(id="backups-table", cursor_type="row")
                    yield Label(t("backups.ledger_heading"), id="lbl-ledger-heading", classes="section-title")
                    yield DataTable(id="ledger-table", cursor_type="row")

                # 8. Settings, Board & User Guide
                with VerticalScroll(id="screen-settings", classes="pane-scroll"):
                    yield Label(t("settings.title"), id="lbl-settings-title", classes="section-title")
                    with Vertical(classes="card"):
                        yield Label(t("settings.language"), id="lbl-set-lang")
                        with Horizontal(classes="btn-row"):
                            yield Button("English", id="btn-set-lang-en", variant="default")
                            yield Button("Русский", id="btn-set-lang-ru", variant="default")
                            yield Button("简体中文", id="btn-set-lang-zh", variant="default")
                        yield Label(t("settings.patch_mode"), id="lbl-set-mode")
                        with Horizontal(classes="btn-row"):
                            yield Button(t("mode.auto"), id="btn-set-mode-auto", variant="default")
                            yield Button(t("mode.manual"), id="btn-set-mode-manual", variant="default")
                        yield Label(t("settings.link_mode"), id="lbl-set-link")
                        yield Select(
                            [("Auto (Symlink -> Junction -> Copy)", "auto"), ("Symlink", "symlink"), ("Atomic Copy", "copy")],
                            value=self.config.skill_link_mode,
                            id="sel-link-mode",
                        )
                        with Horizontal(classes="btn-row"):
                            yield Label(t("settings.auto_scan"), id="lbl-set-autoscan")
                            yield Switch(value=self.config.auto_scan_on_startup, id="sw-autoscan")
                            yield Label(t("settings.strict_vocab"), id="lbl-set-vocab")
                            yield Switch(value=self.config.strict_vocab_guard, id="sw-strict-vocab")
                    with Horizontal(classes="btn-row"):
                        yield Button(t("settings.btn_board"), id="btn-open-board", variant="primary")
                        yield Button(t("settings.btn_install_skill"), id="btn-install-skill", variant="success")
                        yield Button(t("settings.btn_wizard"), id="btn-rerun-wizard", variant="default")
                        yield Button(t("settings.btn_open_config"), id="btn-open-cfg-dir", variant="default")
                        yield Button(t("settings.btn_open_backups"), id="btn-open-bkp-dir", variant="default")
                    with Vertical(classes="card"):
                        yield Label(t("guide.title"), id="lbl-guide-title", classes="section-title")
                        yield Static(t("guide.body"), id="guide-body")

        yield Static(t("footer.keys"), id="bottom-footer", markup=False)

    def on_mount(self) -> None:
        self._init_tables()
        self._refresh_locale_widgets()
        self._refresh_backups_view()
        if self.config.auto_scan_on_startup:
            self.action_trigger_scan(open_wizard_after=self._should_open_onboarding())
        elif self._should_open_onboarding():
            self._open_onboarding_wizard()

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
        tt.add_columns("Tool", "Type", "Installed", "Checked", "Status", "Path")
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
        return f"Mode: {t('mode.auto')}" if self.config.patch_mode == "auto" else f"Mode: {t('mode.manual')}"

    def _set_status(self, key: str, style: str = "") -> None:
        badge = self.query_one("#status-badge", Label)
        badge.update(t(key))
        badge.remove_class("warn", "busy")
        if style:
            badge.add_class(style)

    def _on_progress_event_bg(self, ev: ProgressEvent) -> None:
        self.call_from_thread(self._apply_progress_event, ev)

    def _apply_progress_event(self, ev: ProgressEvent) -> None:
        msg = t(ev.message_key, **ev.params)
        self.query_one("#op-status", Label).update(f"[{ev.phase}] {msg}")
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

        self.query_one("#brand-title", Label).update(Content.from_text(f"{t('app.title')} v{__version__}"))
        mode_btn = self.query_one("#btn-mode-toggle", Button)
        mode_btn.label = Content.from_text(self._mode_pill_label())
        if self.config.patch_mode == "auto":
            mode_btn.add_class("auto-mode")
        else:
            mode_btn.remove_class("auto-mode")

        for btn_id, _screen_id, loc_key in NAV_KEYS:
            self.query_one(f"#{btn_id}", Button).label = Content.from_text(t(loc_key))

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

        self.query_one("#lbl-patch-title", Label).update(t("patch.title"))
        self.query_one("#btn-patch-preview", Button).label = t("patch.btn_preview")
        self.query_one("#btn-patch-apply", Button).label = t("patch.btn_apply")
        self.query_one("#btn-patch-cancel", Button).label = t("patch.btn_cancel")
        self.query_one("#chk-opt-dryrun", Checkbox).label = t("patch.opt_dry_run")
        self.query_one("#chk-opt-prune", Checkbox).label = t("patch.opt_prune")
        self.query_one("#chk-opt-strip", Checkbox).label = t("patch.opt_strip_secrets")
        self.query_one("#chk-opt-force", Checkbox).label = t("patch.opt_force")

        self.query_one("#lbl-handoff-title", Label).update(t("handoff.title"))
        self.query_one("#lbl-handoff-sub", Static).update(t("handoff.subtitle"))
        self.query_one("#btn-handoff-refresh", Button).label = t("handoff.btn_refresh")
        self.query_one("#btn-handoff-preview", Button).label = t("handoff.btn_preview")
        self.query_one("#btn-handoff-execute", Button).label = t("handoff.btn_execute")

        self.query_one("#lbl-watch-title", Label).update(t("watch.title"))
        self.query_one("#lbl-watch-sub", Static).update(Content.from_text(t("watch.subtitle")))
        self.query_one("#btn-watch-start", Button).label = t("watch.btn_start")
        self.query_one("#btn-watch-stop", Button).label = t("watch.btn_stop")

        self.query_one("#lbl-tools-title", Label).update(t("tools.title"))
        self.query_one("#btn-tools-rescan", Button).label = t("tools.btn_rescan")
        self.query_one("#btn-tools-save", Button).label = t("tools.btn_save")
        self.query_one("#btn-set-custom-path", Button).label = t("tools.btn_set_path")

        self.query_one("#lbl-health-title", Label).update(t("health.title"))
        self.query_one("#btn-health-run", Button).label = t("health.btn_run")
        self.query_one("#btn-health-repair-dry", Button).label = t("health.btn_repair_dry")
        self.query_one("#btn-health-repair", Button).label = t("health.btn_repair")

        self.query_one("#lbl-backups-title", Label).update(t("backups.title"))
        self.query_one("#btn-backups-refresh", Button).label = t("backups.btn_refresh")
        self.query_one("#btn-backups-restore", Button).label = t("backups.btn_restore")
        self.query_one("#lbl-ledger-heading", Label).update(t("backups.ledger_heading"))

        self.query_one("#lbl-settings-title", Label).update(t("settings.title"))
        self.query_one("#lbl-set-lang", Label).update(t("settings.language"))
        self.query_one("#lbl-set-mode", Label).update(t("settings.patch_mode"))
        self.query_one("#btn-set-mode-auto", Button).label = t("mode.auto")
        self.query_one("#btn-set-mode-manual", Button).label = t("mode.manual")
        self.query_one("#lbl-set-link", Label).update(t("settings.link_mode"))
        self.query_one("#lbl-set-autoscan", Label).update(t("settings.auto_scan"))
        self.query_one("#lbl-set-vocab", Label).update(t("settings.strict_vocab"))
        self.query_one("#btn-open-board", Button).label = t("settings.btn_board")
        self.query_one("#btn-install-skill", Button).label = t("settings.btn_install_skill")
        self.query_one("#btn-rerun-wizard", Button).label = t("settings.btn_wizard")
        self.query_one("#btn-open-cfg-dir", Button).label = t("settings.btn_open_config")
        self.query_one("#btn-open-bkp-dir", Button).label = t("settings.btn_open_backups")
        self.query_one("#lbl-guide-title", Label).update(t("guide.title"))
        self.query_one("#guide-body", Static).update(Content.from_text(t("guide.body")))
        self._update_dash_summary()

    def _set_patch_mode(self, mode: PatchMode, *, persist: bool = True) -> None:
        self.config.patch_mode = mode
        if persist and (self.cfg_mgr.exists() or self.config.onboarding_completed):
            self.cfg_mgr.save(self.config)
        self.query_one("#sel-patch-workflow", Select).value = mode
        self._refresh_locale_widgets()

    # --- Actions ---

    def action_switch_screen(self, screen_id: str) -> None:
        self.query_one("#main-switcher", ContentSwitcher).current = screen_id
        for btn_id, sid, _ in NAV_KEYS:
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

    def _sync_checklist_widgets(self) -> None:
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
        # Preserve checked selection for known tools; auto-check newly discovered installed tools if config empty
        if not self.config.tools:
            disc_map = discover_tools(self.discovery_env)
            self.config = merge_discovery_into_config(self.config, disc_map)
            self.checked_tools = {a.tool_name for a in apps if a.installed}
        else:
            self.checked_tools = {
                a.tool_name
                for a in apps
                if a.installed and self.config.is_tool_enabled(a.tool_name)
            }

        await self._render_dashboard_checklist()
        self._refresh_tools_table()
        self._refresh_health_table_from_scan()
        self._update_dash_summary()

        has_running = any(a.running_pids for a in apps if a.tool_name in self.checked_tools)
        self._set_status("status.running_warn" if has_running else "status.idle", "warn" if has_running else "")

        if open_wizard_after:
            self._open_onboarding_wizard()

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
                    f"{app_st.display_name} ({app_st.client_kind.upper()})  "
                    f"{self._status_text_for(app_st)}  {counts}  -  {app_st.root_path}"
                )
                cb = Checkbox(
                    label,
                    value=app_st.tool_name in self.checked_tools,
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
                "[x]" if a.tool_name in self.checked_tools else "[ ]",
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
            if a.tool_name in self.checked_tools
            for iss in a.issues
        ]
        for iss in issues:
            tbl.add_row(iss.severity.upper(), iss.tool, iss.code, "yes" if iss.fixable else "no", iss.message)
        fixable = sum(1 for i in issues if i.fixable)
        summary = t("health.found", count=len(issues), fixable=fixable) if issues else t("health.clean")
        self.query_one("#health-summary", Static).update(summary)

    def _update_dash_summary(self) -> None:
        detected_cnt = sum(1 for a in self.detected_apps if a.installed)
        selected_cnt = len(self.checked_tools)
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
            t(
                "dash.summary",
                selected=selected_cnt,
                detected=detected_cnt,
                last_run=last_run,
                backups=backups,
                conflicts=conflicts,
            )
        )

    def _refresh_backups_view(self) -> None:
        bt = self.query_one("#backups-table", DataTable)
        bt.clear()
        for b in BackupManager(self.config).list_backups()[:20]:
            bt.add_row(b.backup_id, b.created_at_utc[:19], b.reason, ", ".join(b.tools), str(len(b.entries)), key=b.backup_id)

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
                on_locale_changed=lambda loc: self._apply_locale(loc, persist=False),
            ),
            _done,
        )

    # --- Patch / Sync / Repair / Handoff Workers ---

    def action_trigger_quick_patch(self) -> None:
        if not self.checked_tools:
            self.notify(t("dash.none_selected"), severity="warning")
            return
        self.action_switch_screen("screen-patch")
        if self.config.patch_mode == "manual":
            self._run_patch_worker(dry_run=True)
        else:
            self._confirm_and_apply_patch()

    def _confirm_and_apply_patch(self) -> None:
        if not self.checked_tools:
            self.notify(t("dash.none_selected"), severity="warning")
            return
        tools_str = ", ".join(sorted(self.checked_tools))
        body = t("modal.confirm_patch_body", count=len(self.checked_tools), tools=tools_str)

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
            targets=sorted(self.checked_tools),
        )

    @work(thread=True, exclusive=True, group="patch")
    def _run_patch_worker(self, *, dry_run: bool) -> None:
        self.call_from_thread(self._set_status, "status.patching", "busy")
        self.active_bus.reset_cancel()
        opts = self.call_from_thread(self._collect_patch_options, dry_run=dry_run)
        engine = PatchEngine(self.config, bus=self.active_bus)
        try:
            result = engine.run_patch(
                selected_tools=sorted(self.checked_tools),
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
        self._set_status("status.idle", "")
        tbl = self.query_one("#patch-diff-table", DataTable)
        tbl.clear()
        for act in result.sync_plan.actions:
            tbl.add_row(
                act.category,
                act.action,
                act.target_tool,
                act.risk_level.upper(),
                act.entity_id[:36],
                act.summary[:80],
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
            for name in sorted(self.checked_tools)
            if name in self.config.tools and name in reg.list_names()
        ]
        doc = DoctorEngine(self.config, adapters)
        issues = doc.diagnose()
        rep_msg = ""
        if repair and issues:
            results, bid = doc.repair(dry_run=dry_run, force=not self.check_processes, check_processes=self.check_processes)
            total_fixed = sum(r.fixed_count for r in results)
            rep_msg = f" | {'Would fix' if dry_run else 'Fixed'}: {total_fixed}" + (f" (backup: {bid})" if bid else "")
            if not dry_run:
                issues = doc.diagnose()
        self.call_from_thread(self._on_health_done, issues, rep_msg)

    def _on_health_done(self, issues: list[Any], rep_msg: str) -> None:
        self._set_status("status.idle", "")
        tbl = self.query_one("#health-table", DataTable)
        tbl.clear()
        for iss in issues:
            tbl.add_row(iss.severity.upper(), iss.tool, iss.code, "yes" if iss.fixable else "no", iss.message)
        fixable = sum(1 for i in issues if i.fixable)
        base = t("health.found", count=len(issues), fixable=fixable) if issues else t("health.clean")
        self.query_one("#health-summary", Static).update(base + rep_msg)
        self._refresh_backups_view()

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
        self.query_one("#handoff-output", Static).update(msg)

    # --- Event Handlers ---

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
        if event.switch.id == "sw-autoscan":
            self.config.auto_scan_on_startup = event.value
        elif event.switch.id == "sw-strict-vocab":
            self.config.strict_vocab_guard = event.value
        if self.cfg_mgr.exists():
            self.cfg_mgr.save(self.config)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "sel-link-mode" and event.value in ("auto", "symlink", "copy"):
            self.config.skill_link_mode = str(event.value)  # type: ignore[assignment]
            if self.cfg_mgr.exists():
                self.cfg_mgr.save(self.config)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""

        # Navigation buttons
        for nav_btn_id, screen_id, _ in NAV_KEYS:
            if bid == nav_btn_id:
                self.action_switch_screen(screen_id)
                return

        # Header & Settings language switchers
        if bid in ("btn-lang-en", "btn-set-lang-en"):
            self._apply_locale("en", persist=True)
            return
        if bid in ("btn-lang-ru", "btn-set-lang-ru"):
            self._apply_locale("ru", persist=True)
            return
        if bid in ("btn-lang-zh", "btn-set-lang-zh"):
            self._apply_locale("zh", persist=True)
            return

        # Patch mode switchers
        if bid == "btn-mode-toggle":
            self.action_toggle_patch_mode()
            return
        if bid == "btn-set-mode-auto":
            self._set_patch_mode("auto", persist=True)
            return
        if bid == "btn-set-mode-manual":
            self._set_patch_mode("manual", persist=True)
            return

        # Dashboard actions
        if bid == "btn-select-all":
            self.action_select_all_apps()
        elif bid == "btn-deselect-all":
            self.action_deselect_all_apps()
        elif bid in ("btn-rescan", "btn-tools-rescan"):
            self.action_trigger_scan(open_wizard_after=False)
        elif bid == "btn-quick-patch":
            self.action_trigger_quick_patch()
        elif bid == "btn-quick-dryrun":
            self.action_switch_screen("screen-patch")
            self._run_patch_worker(dry_run=True)
        elif bid == "btn-quick-handoff":
            self.action_switch_screen("screen-handoff")
            self._refresh_handoff_sessions()
        elif bid == "btn-quick-doctor":
            self.action_switch_screen("screen-health")
            self._run_health_worker(repair=False, dry_run=True)
        elif bid in ("btn-quick-board", "btn-open-board"):
            self._generate_and_open_board(open_browser=False)
        elif bid in ("btn-quick-wizard", "btn-rerun-wizard"):
            self._open_onboarding_wizard()

        # Patch & Sync actions
        elif bid == "btn-patch-preview":
            self._run_patch_worker(dry_run=True)
        elif bid == "btn-patch-apply":
            if self.query_one("#chk-opt-dryrun", Checkbox).value:
                self._run_patch_worker(dry_run=True)
            else:
                self._confirm_and_apply_patch()
        elif bid == "btn-patch-cancel":
            self.active_bus.cancel()

        # Handoff actions
        elif bid == "btn-handoff-refresh":
            self._refresh_handoff_sessions()
        elif bid == "btn-handoff-preview":
            self._run_handoff_worker(execute=False)
        elif bid == "btn-handoff-execute":
            self._run_handoff_worker(execute=True)

        # Watch actions
        elif bid == "btn-watch-start":
            self._start_watch()
        elif bid == "btn-watch-stop":
            self._stop_watch()

        # Tools actions
        elif bid == "btn-tools-save":
            for name, tc in self.config.tools.items():
                tc.enabled = name in self.checked_tools
            self.cfg_mgr.save(self.config)
            self.notify(t("tools.saved", path=str(self.cfg_mgr.config_path)))
        elif bid == "btn-set-custom-path":
            self._apply_custom_tool_path()

        # Health actions
        elif bid == "btn-health-run":
            self._run_health_worker(repair=False, dry_run=True)
        elif bid == "btn-health-repair-dry":
            self._run_health_worker(repair=True, dry_run=True)
        elif bid == "btn-health-repair":
            self._run_health_worker(repair=True, dry_run=False)

        # Backups actions
        elif bid == "btn-backups-refresh":
            self._refresh_backups_view()
        elif bid == "btn-backups-restore":
            self._confirm_restore_backup()

        # Settings extras
        elif bid == "btn-install-skill":
            reg = get_registry()
            adapters = [
                reg.create(n, self.config.tools[n])
                for n in sorted(self.checked_tools)
                if n in self.config.tools and n in reg.list_names()
            ]
            installed = install_agentsync_skill(adapters, dry_run=False)
            self.notify(t("settings.skill_installed", count=len(installed)))
        elif bid == "btn-open-cfg-dir":
            self._open_folder(self.cfg_mgr.config_path.parent)
        elif bid == "btn-open-bkp-dir":
            self._open_folder(self.config.backups_dir)

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
            for n in sorted(self.checked_tools)
            if n in self.config.tools and n in reg.list_names()
        ]
        out_path = self.config.resolved_data_dir() / "inventory-board.html"
        path, _count = generate_inventory_board(adapters, out_path)
        self.notify(t("settings.board_generated", path=str(path)))
        if open_browser:
            webbrowser.open(path.as_uri())

    def _confirm_restore_backup(self) -> None:
        backups = BackupManager(self.config).list_backups()[:20]
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
            res = BackupManager(self.config).restore_backup(
                bid,
                adapters_by_name=adapters,
                force=not self.check_processes,
                check_processes=self.check_processes,
            )
            self.notify(t("backups.restored", backup_id=bid, restored=res.restored_files, removed=res.removed_files))
            self._refresh_backups_view()

        self.push_screen(
            ConfirmModal(t("modal.confirm_title"), t("modal.confirm_restore_body", backup_id=bid)),
            _on_ok,
        )

    def _start_watch(self) -> None:
        if self._watching:
            return
        reg = get_registry()
        adapters = [
            reg.create(n, self.config.tools[n])
            for n in sorted(self.checked_tools)
            if n in self.config.tools and n in reg.list_names()
        ]
        if not adapters:
            self.notify(t("dash.none_selected"), severity="warning")
            return
        self._watching = True
        self._set_status("status.watching", "busy")
        self.query_one("#watch-state-label", Static).update(t("watch.active", count=len(adapters)))
        self.query_one("#watch-log", RichLog).write(
            f"Watching: {', '.join(a.tool_name for a in adapters)}"
        )

    def _stop_watch(self) -> None:
        self._watching = False
        self._set_status("status.idle", "")
        self.query_one("#watch-state-label", Static).update(t("watch.stopped"))

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
