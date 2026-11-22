"""Persistent configuration management for agentsync."""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from agentsync.utils.atomic_io import atomic_write_toml

EntityType = Literal["skills", "mcp", "plugins", "instructions", "projects", "chats"]
LocaleCode = Literal["en", "ru", "zh"]
PatchMode = Literal["auto", "manual"]
SkillLinkMode = Literal["auto", "symlink", "copy"]
SyncScope = Literal["user", "project", "all"]

ALL_ENTITIES: tuple[EntityType, ...] = (
    "projects",
    "instructions",
    "skills",
    "mcp",
    "plugins",
    "chats",
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
    """Return platform-standard local data directory (state DB, backups, ledgers, conflicts, quarantine)."""
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

    schema_version: int = 2
    locale: LocaleCode = "en"
    patch_mode: PatchMode = "manual"
    skill_link_mode: SkillLinkMode = "auto"
    auto_scan_on_startup: bool = True
    onboarding_completed: bool = False
    strip_secrets: bool = False
    strict_vocab_guard: bool = True
    data_dir: str | None = None
    include_subagents: bool = False
    debounce_seconds: float = Field(default=2.0, ge=0.2, le=60.0)
    tools: dict[str, ToolConfig] = Field(default_factory=dict)

    def resolved_data_dir(self) -> Path:
        return Path(self.data_dir) if self.data_dir else default_data_dir()

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

    def is_tool_enabled(self, tool_name: str) -> bool:
        tcfg = self.tools.get(tool_name)
        return bool(tcfg and tcfg.enabled)

    def is_entity_enabled(self, tool_name: str, entity: EntityType) -> bool:
        tcfg = self.tools.get(tool_name)
        if not tcfg or not tcfg.enabled:
            return False
        return entity in tcfg.entities


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
    debounce_seconds: float = Field(default=2.0, ge=0.2, le=60.0)
    scope: SyncScope = "all"
    prune: bool = False
    strip_secrets: bool = False
    strict_vocab_guard: bool = True
    skill_link_mode: SkillLinkMode = "auto"
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    targets: list[str] = Field(default_factory=list)
    project_root: str | None = None
