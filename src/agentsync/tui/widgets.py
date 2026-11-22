"""Custom accessible TUI widgets and Global Command Palette (Ctrl+K) provider."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from textual.command import Hit, Hits, Provider
from textual.widgets import Checkbox

from agentsync.i18n import t

if TYPE_CHECKING:
    from agentsync.tui.app import AgentSyncApp


class AgentCheckbox(Checkbox):
    """High-contrast, zero-half-block checkbox that renders cleanly in every shell including legacy conhost."""

    BUTTON_LEFT = "["
    BUTTON_RIGHT = "]"
    BUTTON_INNER = "✓"

    def __init__(
        self,
        label: str = "",
        value: bool = False,
        *,
        ascii_mode: bool = False,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(label=label, value=value, name=name, id=id, classes=classes)
        self.BUTTON_INNER = "X" if ascii_mode else "✓"


class AgentSyncCommandProvider(Provider):
    """Global Ctrl+K Command Palette searching screens, actions, tools, skills, MCP servers, and chats."""

    def _collect_entries(self) -> list[tuple[str, str, Callable[[], None]]]:
        app: AgentSyncApp = self.app  # type: ignore[assignment]
        entries: list[tuple[str, str, Callable[[], None]]] = [
            (f"Screen: {t('nav.dashboard')}", "Switch to Dashboard & Quick Patch Launcher", lambda: app.action_switch_screen("screen-dashboard")),
            (f"Screen: {t('nav.patch')}", "Switch to Patch, Sync & Bootstrap Center", lambda: app.action_switch_screen("screen-patch")),
            (f"Screen: {t('nav.handoff')}", "Switch to Context & Session Handoff Center", lambda: app.action_switch_screen("screen-handoff")),
            (f"Screen: {t('nav.watch')}", "Switch to Live Watch & Sync-on-Close Monitor", lambda: app.action_switch_screen("screen-watch")),
            (f"Screen: {t('nav.tools')}", "Switch to Tools, Paths & Auto-Discovery", lambda: app.action_switch_screen("screen-tools")),
            (f"Screen: {t('nav.health')}", "Switch to Health, Audit & Deep Repair", lambda: app.action_switch_screen("screen-health")),
            (f"Screen: {t('nav.backups')}", "Switch to Backups, Ledger & Integrity", lambda: app.action_switch_screen("screen-backups")),
            (f"Screen: {t('nav.settings')}", "Switch to Settings, Autostart & Updates", lambda: app.action_switch_screen("screen-settings")),
            ("Action: Re-Scan Installed AI Tools", "Run full parallel auto-discovery scan", lambda: app.action_trigger_scan(open_wizard_after=False)),
            ("Action: Dry-Run Preview Diff", "Preview planned sync & patch changes safely", lambda: app.action_open_dry_run()),
            ("Action: Patch Selected Apps", "Execute patch across checked [x] apps", lambda: app.action_trigger_quick_patch()),
            ("Action: Toggle Auto / Manual Patch Mode", "Switch between Auto-Patch and Manual Patch", lambda: app.action_toggle_patch_mode()),
            ("Action: Run Full Health Check (Doctor)", "Audit SQLite DBs, rollouts, configs, symlinks", lambda: app.action_run_doctor()),
            ("Action: Preview Deep Repair", "Dry-run repair of broken rollouts & DBs", lambda: app.action_run_repair_preview()),
            ("Action: Create Manual Backup Snapshot Now", "Create verified SHA-256 + SQLite snapshot", lambda: app.action_create_manual_backup()),
            ("Action: Verify All Backups", "Check SHA-256 + SQLite PRAGMA integrity_check", lambda: app.action_verify_all_backups()),
            ("Action: Check for Updates", "Check GitHub Releases for newer AgentSync", lambda: app.action_check_updates(force=True)),
            ("Action: Generate Offline HTML Inventory Board", "Build self-contained HTML board", lambda: app.action_generate_board()),
            ("Action: Export Portable Profile Bundle (.zip)", "Export secret-redacted skills, MCP & rules", lambda: app.action_export_bundle()),
            ("Action: Create Anonymized Support Bundle (.zip)", "Create privacy-safe diagnostics archive", lambda: app.action_create_support_bundle()),
            ("Action: Cycle Interface Language (EN / RU / ZH)", "Switch TUI language immediately", lambda: app.action_cycle_language()),
            ("Action: Select All Detected Apps", "Check [x] all installed tools", lambda: app.action_select_all_apps()),
            ("Action: Clear All App Selections", "Uncheck [ ] all tools", lambda: app.action_deselect_all_apps()),
        ]

        # Dynamic live entries from detected apps
        for st in app.detected_apps:
            if not st.installed:
                continue
            tname = st.tool_name
            checked_str = "checked" if tname in app.checked_tools else "unchecked"
            entries.append(
                (
                    f"Tool: {st.display_name} ({tname})",
                    f"{st.client_kind} | {checked_str} | {st.root_path}",
                    lambda tn=tname: app.action_toggle_tool_checked(tn),
                )
            )

        # Dynamic search over cached skills, MCP servers, rules, and chats
        for item_kind, item_label, item_detail, target_screen in app.palette_index:
            entries.append(
                (
                    f"{item_kind}: {item_label}",
                    item_detail,
                    lambda sc=target_screen: app.action_switch_screen(sc),
                )
            )
        return entries

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for label, detail, callback in self._collect_entries():
            score = matcher.match(f"{label} {detail}")
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(label),
                    callback,
                    help=detail,
                )
