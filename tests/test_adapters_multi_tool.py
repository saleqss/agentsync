"""Multi-tool adapter tests: Claude Code, OpenCode, DeepSeek Harness, and extensible AdapterRegistry."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentsync.adapters.base import ToolAdapter, ToolCapabilities
from agentsync.adapters.claude_code import ClaudeCodeAdapter
from agentsync.adapters.deepseek_harness import DeepSeekHarnessAdapter
from agentsync.adapters.opencode import OpenCodeAdapter
from agentsync.adapters.registry import AdapterRegistry, get_registry
from agentsync.config import AgentSyncConfig, ToolConfig
from agentsync.engine.sync import SyncEngine
from agentsync.models.canonical import (
    ChatMessage,
    ChatSession,
    InstructionDoc,
    McpServer,
    PluginItem,
    ProjectItem,
    SkillItem,
)
from agentsync.utils.paths import path_dedup_key


def test_claude_code_full_roundtrip(sandbox: dict[str, Any]) -> None:
    """Claude Code adapter reads/writes skills, commands, MCP, plugins, instructions, projects, and JSONL chats."""
    claude: ClaudeCodeAdapter = sandbox["claude"]
    ws_app: Path = sandbox["workspaces"]["app"]

    # Flat command markdown should be discovered as a skill with synthesized frontmatter
    claude.commands_dir.mkdir(parents=True, exist_ok=True)
    (claude.commands_dir / "deploy-check.md").write_text("# Deploy Check\nVerify green CI.", encoding="utf-8")

    # Skills round-trip
    skill = SkillItem(
        name="custom-audit",
        frontmatter_name="custom-audit",
        description="Audit skill",
        files={
            "SKILL.md": b"---\nname: custom-audit\ndescription: Audit skill\n---\n\n# Audit\n",
            "refs/guide.txt": b"guide content",
        },
        source_tool="codex",
    ).compute_hashes()
    assert claude.write_skill(skill) is True
    assert claude.write_skill(skill) is False  # strictly idempotent

    skills = claude.read_skills()
    assert "custom-audit" in skills
    assert "deploy-check" in skills
    assert skills["deploy-check"].synthesized_frontmatter is True

    # MCP servers round-trip (stdio + http + sse)
    mcps = {
        "ctx7": McpServer(
            name="ctx7",
            transport="stdio",
            command="npx",
            args=["-y", "@upstash/context7-mcp"],
            env={"API_KEY": "test-key"},
            source_tool="codex",
        ).compute_hash(),
        "remote_http": McpServer(
            name="remote_http",
            transport="http",
            url="https://mcp.example.com/v1",
            headers={"X-Token": "abc"},
            source_tool="codex",
        ).compute_hash(),
        "legacy_sse": McpServer(
            name="legacy_sse",
            transport="sse",
            url="https://mcp.example.com/sse",
            source_tool="codex",
        ).compute_hash(),
    }
    changed, warns = claude.write_mcp_servers(mcps)
    assert changed is True
    assert warns == []
    read_mcps = claude.read_mcp_servers()
    assert set(read_mcps) == {"ctx7", "remote_http", "legacy_sse"}
    assert read_mcps["ctx7"].transport == "stdio"
    assert read_mcps["remote_http"].transport == "http"
    assert read_mcps["legacy_sse"].transport == "sse"

    # Plugin round-trip
    plugin = PluginItem(
        name="eng-pack",
        display_name="Engineering Pack",
        version="2.0.0",
        description="Bundled engineering skills and MCP",
        skills={"custom-audit": skill},
        mcp_servers={"ctx7": mcps["ctx7"]},
        portable=True,
        source_tool="codex",
    ).compute_hash()
    p_changed, p_warns = claude.write_plugin(plugin)
    assert p_changed is True
    assert p_warns == []
    read_plugins = claude.read_plugins()
    assert "eng-pack" in read_plugins
    assert "custom-audit" in read_plugins["eng-pack"].skills
    assert "ctx7" in read_plugins["eng-pack"].mcp_servers

    # Instructions + Projects + Chats round-trip
    doc = InstructionDoc.from_markdown("# Global Claude Rules\n\nFollow tests.", scope="global", source_tool="codex")
    claude.write_global_instructions(doc)
    assert claude.read_global_instructions() is not None
    assert "Follow tests." in claude.read_global_instructions().raw_text

    proj_doc = InstructionDoc.from_markdown("# Project Claude Rules\n", scope="project", source_tool="codex")
    claude.write_project_instructions(ws_app, proj_doc)
    assert (ws_app / "CLAUDE.md").exists()

    pkey = path_dedup_key(str(ws_app))
    projs = {pkey: ProjectItem.from_path(str(ws_app), name="core-app")}
    _, written_projs = claude.write_projects(projs)
    read_projs = claude.read_projects()
    assert pkey in read_projs

    chat = ChatSession(
        tool="codex",
        native_id="src-chat-1",
        title="Claude Sync Test",
        project_key=pkey,
        created_at_ms=1_795_500_000_000,
        updated_at_ms=1_795_500_005_000,
        messages=[
            ChatMessage(role="user", text="Explain the adapter pattern.", timestamp_ms=1_795_500_000_000),
            ChatMessage(role="assistant", text="Adapters isolate tool-specific schemas.", timestamp_ms=1_795_500_005_000),
        ],
    ).compute_hashes()
    sid, c_changed = claude.write_chat(chat, target_native_id=None, projects_by_key=written_projs)
    assert c_changed is True
    read_chats = claude.read_chats(read_projs)
    assert len(read_chats) == 1
    assert read_chats[0].native_id == sid
    assert read_chats[0].title == "Claude Sync Test"
    assert len(read_chats[0].messages) == 2
    assert claude.doctor() == []


def test_opencode_bom_and_sqlite_roundtrip(sandbox: dict[str, Any]) -> None:
    """OpenCode reads BOM-prefixed opencode.json cleanly, writes BOM-free JSON, and syncs SQLite projects/chats."""
    oc: OpenCodeAdapter = sandbox["opencode"]
    ws_app: Path = sandbox["workspaces"]["app"]

    # Seed BOM-prefixed opencode.json matching real local installation
    bom_payload = json.dumps(
        {
            "$schema": "https://opencode.ai/config.json",
            "mcp": {
                "servers": {
                    "local_tool": {
                        "type": "local",
                        "command": ["uvx", "mcp-server-git"],
                        "environment": {"GIT_TRACE": "0"},
                        "enabled": True,
                    },
                    "remote_tool": {
                        "type": "remote",
                        "url": "https://api.example.com/mcp",
                        "headers": {"Authorization": "Bearer secret-tok"},
                        "enabled": True,
                    },
                }
            },
        }
    )
    oc.opencode_json.write_bytes(b"\xef\xbb\xbf" + bom_payload.encode("utf-8"))

    # Doctor must flag BOM cleanly
    issues = oc.doctor()
    assert any(i.code == "OPENCODE_JSON_BOM" and i.fixable for i in issues)

    mcps = oc.read_mcp_servers()
    assert set(mcps) == {"local_tool", "remote_tool"}
    assert mcps["local_tool"].command == "uvx"
    assert mcps["local_tool"].args == ["mcp-server-git"]
    assert mcps["local_tool"].env == {"GIT_TRACE": "0"}
    assert mcps["remote_tool"].transport == "http"

    # Writing back strips UTF-8 BOM
    oc.write_mcp_servers(mcps)
    assert not oc.opencode_json.read_bytes().startswith(b"\xef\xbb\xbf")
    assert oc.doctor() == []

    # Projects + Chats in opencode.db
    pkey = path_dedup_key(str(ws_app))
    projs = {pkey: ProjectItem.from_path(str(ws_app), name="core-app")}
    changed, written_projs = oc.write_projects(projs)
    assert changed is True
    assert pkey in oc.read_projects()

    chat = ChatSession(
        tool="codex",
        native_id="cx-to-oc",
        title="OpenCode DB Sync",
        project_key=pkey,
        created_at_ms=1_795_600_000_000,
        updated_at_ms=1_795_600_009_000,
        messages=[
            ChatMessage(role="user", text="Check SQLite schema.", timestamp_ms=1_795_600_000_000),
            ChatMessage(role="assistant", text="Verified session_v2 and session_message.", timestamp_ms=1_795_600_009_000),
        ],
    ).compute_hashes()
    sid, _ = oc.write_chat(chat, target_native_id=None, projects_by_key=written_projs)
    chats = oc.read_chats(oc.read_projects())
    assert len(chats) == 1
    assert chats[0].native_id == sid
    assert chats[0].messages[0].text == "Check SQLite schema."
    assert chats[0].messages[1].text == "Verified session_v2 and session_message."


def test_deepseek_harness_workspaces_zstd_read_and_honest_ro_chats(sandbox: dict[str, Any]) -> None:
    """DeepSeek Harness reads/writes workspaces + project AGENTS.md, reads zstd v4 sessions, and honestly skips chat writes."""
    dsh: DeepSeekHarnessAdapter = sandbox["dsh"]
    codex = sandbox["codex"]
    cfg: AgentSyncConfig = sandbox["config"]
    ws_app: Path = sandbox["workspaces"]["app"]

    # Confirm explicit capability honesty
    assert dsh.capabilities.chats is True
    assert dsh.capabilities.chat_write is False

    # Read seeded zstd v4 session
    pkey = path_dedup_key(str(ws_app))
    projs = {pkey: ProjectItem.from_path(str(ws_app), name="core-app")}
    changed, written_projs = dsh.write_projects(projs)
    assert changed is True
    assert dsh.workspace_json.exists()
    assert pkey in dsh.read_projects()

    dsh_chats = dsh.read_chats(written_projs)
    assert len(dsh_chats) == 1
    assert dsh_chats[0].native_id == "session-dsh-1"
    assert dsh_chats[0].messages[0].text == "Inspect DeepSeek workspace"
    assert dsh.doctor() == []

    # When syncing Codex + DeepSeek Harness, chats from DSH propagate to Codex, while Codex -> DSH emits explicit SKIP
    cfg.tools["antigravity"].enabled = False
    cfg.tools["deepseek-harness"].enabled = True
    engine = SyncEngine(cfg, [codex, dsh])
    plan = engine.run(dry_run=False, check_processes=False)

    dsh_skips = [a for a in plan.skipped_actions if a.category == "chats" and a.target_tool == "deepseek-harness"]
    assert len(dsh_skips) >= 1
    assert any("read-only" in a.summary.lower() for a in dsh_skips)

    # DSH session should have been synced into Codex
    codex_chats = codex.read_chats(codex.read_projects())
    assert any("Inspect DeepSeek workspace" in (c.messages[0].text if c.messages else "") for c in codex_chats)


def test_three_way_sync_codex_antigravity_claude_and_opencode(sandbox: dict[str, Any]) -> None:
    """Multi-tool SyncEngine synchronizes 4 enabled tools cleanly and achieves 0 diff on rerun."""
    cfg: AgentSyncConfig = sandbox["config"]
    cfg.tools["claude-code"].enabled = True
    cfg.tools["opencode"].enabled = True
    adapters = [
        sandbox["codex"],
        sandbox["antigravity"],
        sandbox["claude"],
        sandbox["opencode"],
    ]

    engine = SyncEngine(cfg, adapters)
    plan1 = engine.run(dry_run=False, check_processes=False)
    assert len(plan1.mutating_actions) > 0

    # All 4 tools must now hold the shared skills, projects, global rules, and chats
    for adapter in adapters:
        assert "codex-only-skill" in adapter.read_skills()
        assert len(adapter.read_projects()) == 3
        assert adapter.read_global_instructions() is not None
        assert len(adapter.read_chats(adapter.read_projects())) == 3

    # Consecutive rerun must produce strictly 0 mutating actions
    plan2 = engine.run(dry_run=False, check_processes=False)
    assert plan2.mutating_actions == []


def test_extensible_adapter_registry_custom_plugin(tmp_path: Path) -> None:
    """Third-party adapters can be registered into AdapterRegistry without modifying core sync logic."""
    builtin = get_registry()
    assert set(builtin.list_names()) == {
        "antigravity",
        "claude-code",
        "cline",
        "codex",
        "cursor",
        "deepseek-harness",
        "gemini-cli",
        "opencode",
        "vscode-copilot",
        "windsurf",
        "zed",
    }

    local_reg = AdapterRegistry()

    class MockCursorAdapter(ToolAdapter):
        tool_name = "cursor"
        display_name = "Cursor"
        capabilities = ToolCapabilities(instructions=True, projects=True)

        def read_global_instructions(self) -> InstructionDoc | None:
            p = self.root_path / "rules.md"
            if not p.exists():
                return None
            return InstructionDoc.from_markdown(p.read_text(encoding="utf-8"), scope="global", source_tool="cursor")

        def write_global_instructions(self, doc: InstructionDoc) -> tuple[bool, list[str]]:
            self.root_path.mkdir(parents=True, exist_ok=True)
            (self.root_path / "rules.md").write_text(doc.raw_text, encoding="utf-8")
            return True, []

    local_reg.register(MockCursorAdapter)
    assert "cursor" in local_reg.list_names()

    cfg = AgentSyncConfig(
        data_dir=str(tmp_path / "data"),
        tools={"cursor": ToolConfig(enabled=True, path=str(tmp_path / ".cursor"), entities=["instructions"])},
    )
    created = local_reg.create_enabled(cfg)
    assert len(created) == 1
    assert isinstance(created[0], MockCursorAdapter)
