"""Cross-platform auto-discovery and persistent configuration/tool-toggle tests."""

from __future__ import annotations

from pathlib import Path

from agentsync.adapters.registry import get_registry
from agentsync.config import AgentSyncConfig, ConfigManager, ToolConfig
from agentsync.discovery import DiscoveryEnv, discover_tools, merge_discovery_into_config
from agentsync.engine.sync import SyncEngine


def test_discovery_windows_mocked_filesystem(tmp_path: Path) -> None:
    home = tmp_path / "Users" / "dev"
    appdata = home / "AppData" / "Roaming"
    local = home / "AppData" / "Local"

    # Simulate installed Codex, Antigravity, OpenCode, DeepSeek Harness (Claude absent)
    (home / ".codex").mkdir(parents=True)
    (home / ".gemini" / "antigravity").mkdir(parents=True)
    (appdata / "Antigravity").mkdir(parents=True)
    (home / ".config" / "opencode").mkdir(parents=True)
    (home / ".local" / "share" / "opencode").mkdir(parents=True)
    (home / ".dsh").mkdir(parents=True)

    denv = DiscoveryEnv(
        platform="win32",
        home=home,
        appdata=appdata,
        local_appdata=local,
        xdg_config_home=home / ".config",
        xdg_data_home=home / ".local" / "share",
        env={},
    )
    found = discover_tools(denv)
    assert found["codex"].installed is True
    assert found["antigravity"].installed is True
    assert found["opencode"].installed is True
    assert found["deepseek-harness"].installed is True
    assert found["claude-code"].installed is False


def test_discovery_linux_mocked_filesystem_and_env_overrides(tmp_path: Path) -> None:
    home = tmp_path / "home" / "dev"
    custom_codex = tmp_path / "custom-codex"
    custom_codex.mkdir(parents=True)
    (home / ".claude").mkdir(parents=True)
    (home / ".claude.json").write_text("{}", encoding="utf-8")

    denv = DiscoveryEnv(
        platform="linux",
        home=home,
        xdg_config_home=home / ".config",
        xdg_data_home=home / ".local" / "share",
        env={"CODEX_HOME": str(custom_codex)},
    )
    found = discover_tools(denv)
    assert found["codex"].installed is True
    assert found["codex"].root_path == str(custom_codex)
    assert found["claude-code"].installed is True
    assert found["antigravity"].installed is False


def test_config_persistence_and_per_tool_enable_disable_filtering(
    sandbox: dict[str, object], tmp_path: Path
) -> None:
    cfg: AgentSyncConfig = sandbox["config"]  # type: ignore[assignment]
    cfg_file = tmp_path / "persisted" / "config.toml"
    mgr = ConfigManager(cfg_file)
    mgr.save(cfg)

    reloaded = mgr.load()
    assert reloaded.is_tool_enabled("codex") is True
    assert reloaded.is_tool_enabled("antigravity") is True
    assert reloaded.is_tool_enabled("claude-code") is False

    # Disable skills on antigravity -> sync must NOT copy codex-only-skill to antigravity
    reloaded.tools["antigravity"].entities = ["projects", "mcp", "chats"]
    mgr.save(reloaded)

    reloaded2 = mgr.load()
    adapters = get_registry().create_enabled(reloaded2)
    assert [a.tool_name for a in adapters] == ["antigravity", "codex"]

    SyncEngine(reloaded2, adapters).run(dry_run=False, check_processes=False)
    agy = next(a for a in adapters if a.tool_name == "antigravity")
    # Skills were disabled for antigravity so codex-only-skill was not copied
    assert "codex-only-skill" not in agy.read_skills()
    # MCP was enabled so context7 WAS synced
    assert "context7" in agy.read_mcp_servers()

    # Merge discovery preserves existing disabled state & custom paths
    disc = discover_tools(DiscoveryEnv(platform="linux", home=tmp_path / "empty"))
    merged = merge_discovery_into_config(
        AgentSyncConfig(tools={"codex": ToolConfig(enabled=False, path="/custom/codex")}),
        disc,
    )
    assert merged.tools["codex"].enabled is False
    assert merged.tools["codex"].path == "/custom/codex"
