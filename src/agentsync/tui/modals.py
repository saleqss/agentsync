"""Safety confirmation modal and animated 4-step First-Run Auto-Setup Wizard modal."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Label, ProgressBar, Static

from agentsync.config import PatchMode
from agentsync.engine.patch import DetectedAppStatus
from agentsync.i18n import LOCALE_LABELS, SUPPORTED_LOCALES, set_locale, t


class ConfirmModal(ModalScreen[bool]):
    """Safety confirmation dialog required before mutating disk state."""

    def __init__(self, title: str, body: str, confirm_label: str | None = None) -> None:
        super().__init__()
        self._title = title
        self._body = body
        self._confirm_label = confirm_label or t("modal.confirm_apply")

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label(self._title, classes="section-title")
            yield Static(self._body)
            with Horizontal(classes="btn-row"):
                yield Button(self._confirm_label, id="btn-modal-confirm", variant="success")
                yield Button(t("modal.cancel"), id="btn-modal-cancel", variant="default")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "btn-modal-confirm")


class OnboardingModal(ModalScreen[dict[str, Any]]):
    """4-step interactive First-Run Setup Wizard with live language switching and app checklist."""

    def __init__(
        self,
        initial_apps: list[DetectedAppStatus],
        initial_locale: str = "en",
        initial_patch_mode: PatchMode = "manual",
        on_locale_changed: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__()
        self.step = 1
        self.apps = list(initial_apps)
        self.selected_locale = initial_locale
        self.selected_patch_mode: PatchMode = initial_patch_mode
        self.selected_tools: set[str] = {a.tool_name for a in self.apps if a.installed}
        self._on_locale_changed = on_locale_changed

    def compose(self) -> ComposeResult:
        with Vertical(id="wizard-box"):
            yield Label(t("wizard.title"), id="wizard-title", classes="section-title")
            yield Label(t("wizard.step1"), id="wizard-step-label")
            yield ProgressBar(total=4, show_eta=False, id="wizard-progress")
            with VerticalScroll(id="wizard-body"):
                yield Static("", id="wizard-info")
            with Horizontal(id="wizard-nav", classes="btn-row"):
                yield Button(t("wizard.skip"), id="btn-wiz-skip", variant="default")
                yield Button(t("wizard.back"), id="btn-wiz-back", variant="default")
                yield Button(t("wizard.next"), id="btn-wiz-next", variant="primary")
                yield Button(t("wizard.finish_dry"), id="btn-wiz-dry", variant="warning")
                yield Button(t("wizard.finish"), id="btn-wiz-finish", variant="success")

    async def on_mount(self) -> None:
        await self._render_step()

    async def _render_step(self) -> None:
        self.query_one("#wizard-title", Label).update(t("wizard.title"))
        self.query_one("#wizard-step-label", Label).update(t(f"wizard.step{self.step}"))
        self.query_one("#wizard-progress", ProgressBar).update(progress=self.step)
        self.query_one("#btn-wiz-skip", Button).label = t("wizard.skip")
        self.query_one("#btn-wiz-back", Button).label = t("wizard.back")
        self.query_one("#btn-wiz-next", Button).label = t("wizard.next")
        self.query_one("#btn-wiz-dry", Button).label = t("wizard.finish_dry")
        self.query_one("#btn-wiz-finish", Button).label = t("wizard.finish")

        self.query_one("#btn-wiz-back", Button).display = self.step > 1
        self.query_one("#btn-wiz-next", Button).display = self.step < 4
        self.query_one("#btn-wiz-dry", Button).display = self.step == 4
        self.query_one("#btn-wiz-finish", Button).display = self.step == 4

        body = self.query_one("#wizard-body", VerticalScroll)
        await body.remove_children()

        if self.step == 1:
            for code in SUPPORTED_LOCALES:
                variant = "success" if code == self.selected_locale else "default"
                marker = "●" if code == self.selected_locale else "○"
                await body.mount(
                    Button(
                        f"{marker} {LOCALE_LABELS[code]} ({code.upper()})",
                        id=f"wiz-lang-{code}",
                        variant=variant,
                    )
                )
        elif self.step == 2:
            detected = [a for a in self.apps if a.installed]
            await body.mount(
                Static(
                    t("progress.scan_done", detected=len(detected))
                    + "\n\n"
                    + "\n".join(
                        f"  + {a.display_name} ({a.client_kind}) - {a.root_path}"
                        for a in detected
                    ),
                    markup=False,
                )
            )
        elif self.step == 3:
            await body.mount(
                Button(
                    f"{t('settings.patch_mode')}: {t('mode.auto' if self.selected_patch_mode == 'auto' else 'mode.manual')}",
                    id="wiz-toggle-mode",
                    variant="primary",
                )
            )
            for app_st in self.apps:
                if not app_st.installed:
                    continue
                cb = Checkbox(
                    f"{app_st.display_name} ({app_st.client_kind}) - {app_st.root_path}",
                    value=app_st.tool_name in self.selected_tools,
                    id=f"wiz-chk-{app_st.tool_name}",
                )
                await body.mount(cb)
        elif self.step == 4:
            issues = sum(a.issue_count for a in self.apps if a.tool_name in self.selected_tools)
            fixable = sum(a.fixable_count for a in self.apps if a.tool_name in self.selected_tools)
            locked = [a.display_name for a in self.apps if a.tool_name in self.selected_tools and a.running_pids]
            lines = [
                t("health.found", count=issues, fixable=fixable) if issues else t("health.clean"),
                f"{t('settings.patch_mode')}: {t('mode.auto' if self.selected_patch_mode == 'auto' else 'mode.manual')}",
                f"Checked apps: {', '.join(sorted(self.selected_tools)) or '(none)'}",
            ]
            if locked:
                lines.append(f"Running app lock warning: {', '.join(locked)}")
            await body.mount(Static("\n".join(lines), markup=False))

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        cid = event.checkbox.id or ""
        if cid.startswith("wiz-chk-"):
            tname = cid.removeprefix("wiz-chk-")
            if event.value:
                self.selected_tools.add(tname)
            else:
                self.selected_tools.discard(tname)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid.startswith("wiz-lang-"):
            code = bid.removeprefix("wiz-lang-")
            self.selected_locale = set_locale(code)
            if self._on_locale_changed:
                self._on_locale_changed(self.selected_locale)
            await self._render_step()
            return
        if bid == "wiz-toggle-mode":
            self.selected_patch_mode = "auto" if self.selected_patch_mode == "manual" else "manual"
            await self._render_step()
            return
        if bid == "btn-wiz-next" and self.step < 4:
            self.step += 1
            await self._render_step()
            return
        if bid == "btn-wiz-back" and self.step > 1:
            self.step -= 1
            await self._render_step()
            return
        if bid in ("btn-wiz-skip", "btn-wiz-finish", "btn-wiz-dry"):
            action = "skip" if bid == "btn-wiz-skip" else ("dry_run" if bid == "btn-wiz-dry" else "finish")
            self.dismiss(
                {
                    "action": action,
                    "locale": self.selected_locale,
                    "patch_mode": self.selected_patch_mode,
                    "selected_tools": sorted(self.selected_tools),
                }
            )
