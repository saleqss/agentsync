"""Persistent configuration management and live settings helpers for agentsync."""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from agentsync.utils.atomic_io import atomic_write_toml

EntityType = Literal["skills", "mcp", "plugins", "instructions", "projects", "chats"]
LocaleCode = Literal["en", "ru", "zh"]
PatchMode = Literal["auto", "manual"]
SkillLinkMode = Literal["auto", "symlink", "copy"]
SyncScope = Literal["user", "project", "all"]
PowerProfile = Literal["eco", "instant", "battery_safe"]
RuntimeEnginePref = Literal["auto", "python"]
UpdatePolicy = Literal["notify", "auto", "manual"]
UpdateChannel = Literal["stable", "prerelease"]

ALL_ENTITIES: tuple[EntityType, ...] = (
    "projects",
    "instructions",
    "skills",
    "mcp",
    "plugins",
    "chats",
)

POWER_PROFILE_DEBOUNCE: dict[PowerProfile, float] = {
    "eco": 2.5,
    "instant": 0.8,
    "battery_safe": 3.0,
}

# Editable scalar settings exposed via `agentsync config list|get|set`
EDITABLE_CONFIG_KEYS: tuple[str, ...] = (
    "locale",
    "patch_mode",
    "skill_link_mode",
    "auto_scan_on_startup",
    "strip_secrets",
    "strict_vocab_guard",
    "include_subagents",
    "debounce_seconds",
    "close_grace_seconds",
    "auto_backup",
    "max_backups",
    "max_backup_days",
    "autostart_enabled",
    "realtime_sync_enabled",
    "sync_on_close_enabled",
    "auto_diagnostics_enabled",
    "diagnostics_interval_hours",
    "auto_heal_on_close",
    "power_profile",
    "runtime_engine",
    "auto_check_updates",
    "update_policy",
    "update_channel",
    "notifications",
)


def default_config_dir() -> Path:
    """Return platform-standard config directory for agentsync."""
    override = os.environ.get("AGENTSYNC_CONFIG_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "agentsync"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "agentsync"
    return Path.home() / ".config" / "agentsync"


def default_data_dir() -> Path:
    """Return platform-standard local data directory (state DB, backups, ledgers, daemon telemetry)."""
    override = os.environ.get("AGENTSYNC_DATA_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            return Path(local_appdata) / "agentsync"
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "agentsync"
    return Path.home() / ".local" / "share" / "agentsync"


class ToolConfig(BaseModel):
    """Per-tool persistent configuration."""

    enabled: bool = True
    path: str | None = None
    extra_paths: dict[str, str] = Field(default_factory=dict)
    entities: list[EntityType] = Field(default_factory=lambda: list(ALL_ENTITIES))


class AgentSyncConfig(BaseModel):
    """Top-level persistent user configuration stored in config.toml."""

    schema_version: int = 3
    locale: LocaleCode = "en"
    patch_mode: PatchMode = "manual"
    skill_link_mode: SkillLinkMode = "auto"
    auto_scan_on_startup: bool = True
    onboarding_completed: bool = False
    strip_secrets: bool = False
    strict_vocab_guard: bool = True
    data_dir: str | None = None
    include_subagents: bool = False
    debounce_seconds: float = Field(default=1.5, ge=0.2, le=60.0)
    close_grace_seconds: float = Field(default=2.5, ge=0.2, le=30.0)
    auto_backup: bool = True
    max_backups: int = Field(default=20, ge=1, le=500)
    max_backup_days: int = Field(default=30, ge=1, le=3650)
    autostart_enabled: bool = False
    realtime_sync_enabled: bool = True
    sync_on_close_enabled: bool = True
    auto_diagnostics_enabled: bool = True
    diagnostics_interval_hours: float = Field(default=6.0, ge=0.25, le=168.0)
    auto_heal_on_close: bool = False
    power_profile: PowerProfile = "eco"
    runtime_engine: RuntimeEnginePref = "auto"
    auto_check_updates: bool = True
    update_policy: UpdatePolicy = "notify"
    update_channel: UpdateChannel = "stable"
    skipped_update_version: str | None = None
    remind_update_after_utc: str | None = None
    notifications: bool = False
    enabled_entities: list[EntityType] = Field(default_factory=lambda: list(ALL_ENTITIES))
    tools: dict[str, ToolConfig] = Field(default_factory=dict)

    def resolved_data_dir(self) -> Path:
        return Path(self.data_dir) if self.data_dir else default_data_dir()

    def effective_debounce_seconds(self) -> float:
        """Return effective debounce interval honoring the selected power profile."""
        if self.power_profile == "instant":
            return min(self.debounce_seconds, POWER_PROFILE_DEBOUNCE["instant"])
        if self.power_profile == "battery_safe":
            return max(self.debounce_seconds, POWER_PROFILE_DEBOUNCE["battery_safe"])
        return self.debounce_seconds

    @property
    def state_db_path(self) -> Path:
        return self.resolved_data_dir() / "sync_state.db"

    @property
    def backups_dir(self) -> Path:
        return self.resolved_data_dir() / "backups"

    @property
    def ledgers_dir(self) -> Path:
        return self.resolved_data_dir() / "ledgers"

    @property
    def conflicts_dir(self) -> Path:
        return self.resolved_data_dir() / "conflicts"

    @property
    def quarantine_dir(self) -> Path:
        return self.resolved_data_dir() / "quarantine"

    @property
    def handoffs_ledger_path(self) -> Path:
        return self.resolved_data_dir() / "handoff_ledger.json"

    @property
    def adapted_skills_path(self) -> Path:
        return self.resolved_data_dir() / "adapted_skills.json"

    @property
    def daemon_log_path(self) -> Path:
        return self.resolved_data_dir() / "daemon.log"

    @property
    def daemon_state_path(self) -> Path:
        return self.resolved_data_dir() / "daemon_state.json"

    @property
    def daemon_lock_path(self) -> Path:
        return self.resolved_data_dir() / "daemon.lock"

    @property
    def daemon_control_path(self) -> Path:
        return self.resolved_data_dir() / "daemon_control.json"

    @property
    def repair_queue_path(self) -> Path:
        return self.resolved_data_dir() / "repair_queue.json"

    @property
    def file_cache_path(self) -> Path:
        return self.resolved_data_dir() / "file_hash_cache.json"

    @property
    def update_cache_path(self) -> Path:
        return self.resolved_data_dir() / "update_cache.json"

    def is_tool_enabled(self, tool_name: str) -> bool:
        tcfg = self.tools.get(tool_name)
        return bool(tcfg and tcfg.enabled)

    def is_entity_enabled(self, tool_name: str, entity: EntityType) -> bool:
        if entity not in self.enabled_entities:
            return False
        tcfg = self.tools.get(tool_name)
        if not tcfg or not tcfg.enabled:
            return False
        return entity in tcfg.entities


def parse_config_value(key: str, raw: str) -> Any:
    """Coerce a CLI string value to the Pydantic field type of `AgentSyncConfig`."""
    if key not in EDITABLE_CONFIG_KEYS:
        raise KeyError(
            f"Unknown or non-editable setting '{key}'. Editable keys: {', '.join(EDITABLE_CONFIG_KEYS)}"
        )
    field_info = AgentSyncConfig.model_fields[key]
    ann = field_info.annotation
    low = raw.strip().lower()
    if ann is bool:
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"Expected boolean (true/false) for '{key}', got '{raw}'")
    if ann is int:
        return int(raw.strip())
    if ann is float:
        return float(raw.strip())
    return raw.strip()


def set_config_setting(config: AgentSyncConfig, key: str, raw_value: str) -> AgentSyncConfig:
    """Return a validated copy of `config` with `key` updated to `raw_value`."""
    coerced = parse_config_value(key, raw_value)
    data = config.model_dump()
    data[key] = coerced
    return AgentSyncConfig.model_validate(data)


class ConfigManager:
    """Loads, updates, and atomically persists AgentSyncConfig."""

    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path or (default_config_dir() / "config.toml")

    def exists(self) -> bool:
        return self.config_path.exists()

    def load(self) -> AgentSyncConfig:
        if not self.config_path.exists():
            return AgentSyncConfig()
        raw = self.config_path.read_text(encoding="utf-8-sig").strip()
        if not raw:
            return AgentSyncConfig()
        data = tomllib.loads(raw)
        return AgentSyncConfig.model_validate(data)

    def save(self, config: AgentSyncConfig) -> bool:
        payload = config.model_dump(exclude_none=True)
        return atomic_write_toml(self.config_path, payload)


class SyncOptions(BaseModel):
    """Runtime execution flags for sync/patch/bootstrap/watch operations."""

    dry_run: bool = False
    force: bool = False
    include_subagents: bool = False
    debounce_seconds: float = Field(default=1.5, ge=0.2, le=60.0)
    scope: SyncScope = "all"
    prune: bool = False
    strip_secrets: bool = False
    strict_vocab_guard: bool = True
    skill_link_mode: SkillLinkMode = "auto"
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    targets: list[str] = Field(default_factory=list)
    project_root: str | None = None
