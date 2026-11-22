"""Cross-platform auto-discovery for installed AI coding agents."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from pydantic import BaseModel, Field

from agentsync.config import AgentSyncConfig, EntityType, ToolConfig


class DiscoveredTool(BaseModel):
    """Discovery snapshot for one supported AI tool."""

    name: str
    display_name: str
    installed: bool = False
    root_path: str | None = None
    extra_paths: dict[str, str] = Field(default_factory=dict)
    executable: str | None = None
    supported_entities: list[EntityType] = Field(default_factory=list)
    writable_entities: list[EntityType] = Field(default_factory=list)
    notes: str = ""


class DiscoveryEnv(BaseModel):
    """Injectable OS environment for deterministic cross-platform discovery and unit tests."""

    platform: str = Field(default_factory=lambda: sys.platform)
    home: Path = Field(default_factory=Path.home)
    appdata: Path | None = None
    local_appdata: Path | None = None
    xdg_config_home: Path | None = None
    xdg_data_home: Path | None = None
    env: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def from_os(cls) -> DiscoveryEnv:
        home = Path(os.environ.get("USERPROFILE", str(Path.home())))
        appdata_str = os.environ.get("APPDATA")
        local_str = os.environ.get("LOCALAPPDATA")
        xdg_cfg = os.environ.get("XDG_CONFIG_HOME")
        xdg_data = os.environ.get("XDG_DATA_HOME")
        return cls(
            platform=sys.platform,
            home=home,
            appdata=Path(appdata_str) if appdata_str else (home / "AppData" / "Roaming"),
            local_appdata=Path(local_str) if local_str else (home / "AppData" / "Local"),
            xdg_config_home=Path(xdg_cfg) if xdg_cfg else (home / ".config"),
            xdg_data_home=Path(xdg_data) if xdg_data else (home / ".local" / "share"),
            env=dict(os.environ),
        )

    def which(self, names: tuple[str, ...]) -> str | None:
        for name in names:
            found = shutil.which(name)
            if found:
                return found
        return None


def _discover_codex(denv: DiscoveryEnv) -> DiscoveredTool:
    env_home = denv.env.get("CODEX_HOME")
    root = Path(env_home) if env_home else (denv.home / ".codex")
    exe = denv.which(("codex", "codex.exe"))
    if not exe and denv.platform == "win32" and denv.local_appdata:
        bin_root = denv.local_appdata / "OpenAI" / "Codex" / "bin"
        if bin_root.exists():
            matches = list(bin_root.rglob("codex.exe"))
            if matches:
                exe = str(matches[0])
    installed = root.exists() or exe is not None
    entities: list[EntityType] = ["projects", "instructions", "skills", "mcp", "plugins", "chats"]
    return DiscoveredTool(
        name="codex",
        display_name="OpenAI Codex",
        installed=installed,
        root_path=str(root),
        executable=exe,
        supported_entities=list(entities),
        writable_entities=list(entities),
        notes="TOML config, SQLite state_5/thread_history_1, rollout JSONL sessions",
    )


def _discover_antigravity(denv: DiscoveryEnv) -> DiscoveredTool:
    env_home = denv.env.get("ANTIGRAVITY_HOME")
    root = Path(env_home) if env_home else (denv.home / ".gemini")
    if denv.platform == "win32":
        appdata = (denv.appdata or (denv.home / "AppData" / "Roaming")) / "Antigravity"
    elif denv.platform == "darwin":
        appdata = denv.home / "Library" / "Application Support" / "Antigravity"
    else:
        appdata = (denv.xdg_config_home or (denv.home / ".config")) / "Antigravity"

    exe = denv.which(("agy", "antigravity", "Antigravity.exe"))
    if not exe and denv.platform == "win32" and denv.local_appdata:
        cand = denv.local_appdata / "Programs" / "Antigravity" / "Antigravity.exe"
        if cand.exists():
            exe = str(cand)

    installed = (root / "antigravity").exists() or (root / "config").exists() or appdata.exists() or exe is not None
    entities: list[EntityType] = ["projects", "instructions", "skills", "mcp", "plugins", "chats"]
    return DiscoveredTool(
        name="antigravity",
        display_name="Google Antigravity",
        installed=installed,
        root_path=str(root),
        extra_paths={"appdata": str(appdata)},
        executable=exe,
        supported_entities=list(entities),
        writable_entities=list(entities),
        notes="JSON configs, skills/plugins/rules, SQLite + protobuf hub summary store",
    )


def _discover_claude_code(denv: DiscoveryEnv) -> DiscoveredTool:
    env_home = denv.env.get("CLAUDE_CONFIG_DIR")
    root = Path(env_home) if env_home else (denv.home / ".claude")
    claude_json = denv.home / ".claude.json"
    exe = denv.which(("claude", "claude.exe"))
    installed = root.exists() or claude_json.exists() or exe is not None
    entities: list[EntityType] = ["projects", "instructions", "skills", "mcp", "plugins", "chats"]
    return DiscoveredTool(
        name="claude-code",
        display_name="Claude Code",
        installed=installed,
        root_path=str(root),
        extra_paths={"claude_json": str(claude_json)},
        executable=exe,
        supported_entities=list(entities),
        writable_entities=list(entities),
        notes="~/.claude.json MCPs, CLAUDE.md rules, skills/commands, JSONL project sessions",
    )


def _discover_opencode(denv: DiscoveryEnv) -> DiscoveredTool:
    env_cfg = denv.env.get("OPENCODE_CONFIG_DIR")
    cfg_root = Path(env_cfg) if env_cfg else ((denv.xdg_config_home or (denv.home / ".config")) / "opencode")
    data_root = (denv.xdg_data_home or (denv.home / ".local" / "share")) / "opencode"
    if denv.platform == "win32" and not data_root.exists():
        # OpenCode on Windows stores SQLite under %USERPROFILE%\.local\share\opencode
        win_local_share = denv.home / ".local" / "share" / "opencode"
        if win_local_share.exists():
            data_root = win_local_share
    exe = denv.which(("opencode", "opencode.exe"))
    desktop_dir = (denv.appdata / "ai.opencode.desktop") if denv.appdata else None
    installed = (
        cfg_root.exists()
        or data_root.exists()
        or (desktop_dir is not None and desktop_dir.exists())
        or exe is not None
    )
    entities: list[EntityType] = ["projects", "instructions", "skills", "mcp", "chats"]
    return DiscoveredTool(
        name="opencode",
        display_name="OpenCode",
        installed=installed,
        root_path=str(cfg_root),
        extra_paths={"data_dir": str(data_root)},
        executable=exe,
        supported_entities=list(entities),
        writable_entities=list(entities),
        notes="opencode.json MCPs, AGENTS.md, commands/skills, SQLite opencode.db sessions",
    )


def _discover_deepseek_harness(denv: DiscoveryEnv) -> DiscoveredTool:
    env_home = denv.env.get("DSH_HOME") or denv.env.get("DEEPSEEK_HARNESS_HOME")
    root = Path(env_home) if env_home else (denv.home / ".dsh")
    exe = denv.which(("dsh", "dsh.exe", "deepseek-harness"))
    if not exe and denv.platform == "win32" and denv.local_appdata:
        cand = denv.local_appdata / "Programs" / "DeepSeek Harness" / "DeepSeek Harness.exe"
        if cand.exists():
            exe = str(cand)
    installed = root.exists() or exe is not None
    return DiscoveredTool(
        name="deepseek-harness",
        display_name="DeepSeek Harness",
        installed=installed,
        root_path=str(root),
        executable=exe,
        supported_entities=["projects", "instructions", "chats"],
        writable_entities=["projects", "instructions"],
        notes="workspace.json projects, AGENTS.md rules, zstd v4 session reader (chat write disabled until multi-turn native schema verified)",
    )


def discover_tools(denv: DiscoveryEnv | None = None) -> dict[str, DiscoveredTool]:
    """Scan standard OS paths, env vars, and PATH for all registered tools."""
    env = denv or DiscoveryEnv.from_os()
    detectors = (
        _discover_codex,
        _discover_antigravity,
        _discover_claude_code,
        _discover_opencode,
        _discover_deepseek_harness,
    )
    discovered: dict[str, DiscoveredTool] = {}
    for fn in detectors:
        item = fn(env)
        discovered[item.name] = item
    return discovered


def merge_discovery_into_config(
    config: AgentSyncConfig,
    discovered: dict[str, DiscoveredTool],
) -> AgentSyncConfig:
    """Populate newly discovered tools into config while preserving user overrides."""
    updated = config.model_copy(deep=True)
    for name, info in discovered.items():
        if name not in updated.tools:
            updated.tools[name] = ToolConfig(
                enabled=info.installed,
                path=info.root_path,
                extra_paths=dict(info.extra_paths),
                entities=list(info.supported_entities),
            )
        else:
            existing = updated.tools[name]
            if not existing.path and info.root_path:
                existing.path = info.root_path
            for ek, ev in info.extra_paths.items():
                existing.extra_paths.setdefault(ek, ev)
    return updated
