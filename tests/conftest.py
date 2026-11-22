"""Shared isolated sandbox fixtures mirroring real Codex, Antigravity, Claude Code, OpenCode, and DeepSeek Harness layouts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import tomli_w
import zstandard

from agentsync.adapters.antigravity import AntigravityAdapter
from agentsync.adapters.claude_code import ClaudeCodeAdapter
from agentsync.adapters.codex import CodexAdapter
from agentsync.adapters.deepseek_harness import DeepSeekHarnessAdapter
from agentsync.adapters.opencode import OpenCodeAdapter
from agentsync.config import ALL_ENTITIES, AgentSyncConfig, ToolConfig
from agentsync.models.canonical import ChatMessage, ChatSession
from agentsync.utils.paths import path_to_file_uri, to_native_path


def create_skill(
    skills_dir: Path,
    name: str,
    body: str,
    with_frontmatter: bool = True,
    extra_file: str | None = None,
) -> None:
    sdir = skills_dir / name
    sdir.mkdir(parents=True, exist_ok=True)
    if with_frontmatter:
        content = f"---\nname: {name}\ndescription: Skill {name} description\n---\n\n{body}\n"
    else:
        content = body + "\n"
    (sdir / "SKILL.md").write_text(content, encoding="utf-8")
    if extra_file:
        (sdir / "references").mkdir(exist_ok=True)
        (sdir / "references" / "ref.txt").write_text(extra_file, encoding="utf-8")


@pytest.fixture
def sandbox(tmp_path: Path) -> dict[str, object]:
    """Create a realistic isolated sandbox with Codex and Antigravity enabled by default."""
    codex_home = tmp_path / ".codex"
    gemini_home = tmp_path / ".gemini"
    agy_appdata = tmp_path / "AppData" / "Roaming" / "Antigravity"
    claude_home = tmp_path / ".claude"
    claude_json = tmp_path / ".claude.json"
    opencode_cfg = tmp_path / ".config" / "opencode"
    opencode_data = tmp_path / ".local" / "share" / "opencode"
    dsh_home = tmp_path / ".dsh"
    data_dir = tmp_path / "agentsync-data"
    config_file = tmp_path / "agentsync-config" / "config.toml"

    ws_app = tmp_path / "Workspaces" / "core-app"
    ws_docs = tmp_path / "Workspaces" / "docs-portal"
    ws_sync = tmp_path / "Workspaces" / "agentsync-repo"
    for d in (ws_app, ws_docs, ws_sync):
        d.mkdir(parents=True, exist_ok=True)
    (ws_app / ".git").mkdir()

    cfg = AgentSyncConfig(
        data_dir=str(data_dir),
        tools={
            "codex": ToolConfig(enabled=True, path=str(codex_home), entities=list(ALL_ENTITIES)),
            "antigravity": ToolConfig(
                enabled=True,
                path=str(gemini_home),
                extra_paths={"appdata": str(agy_appdata)},
                entities=list(ALL_ENTITIES),
            ),
            "claude-code": ToolConfig(
                enabled=False,
                path=str(claude_home),
                extra_paths={"claude_json": str(claude_json)},
                entities=list(ALL_ENTITIES),
            ),
            "opencode": ToolConfig(
                enabled=False,
                path=str(opencode_cfg),
                extra_paths={"data_dir": str(opencode_data)},
                entities=["projects", "instructions", "skills", "mcp", "chats"],
            ),
            "deepseek-harness": ToolConfig(
                enabled=False,
                path=str(dsh_home),
                entities=["projects", "instructions", "chats"],
            ),
        },
    )

    codex = CodexAdapter(cfg.tools["codex"])
    agy = AntigravityAdapter(cfg.tools["antigravity"])
    claude = ClaudeCodeAdapter(cfg.tools["claude-code"])
    opencode = OpenCodeAdapter(cfg.tools["opencode"])
    dsh = DeepSeekHarnessAdapter(cfg.tools["deepseek-harness"])

    # Create skeletons
    for d in (
        codex.skills_dir / ".system" / "openai-docs",
        codex.plugins_cache_dir / "openai-bundled" / "browser" / "26.1103.32239" / ".codex-plugin",
        codex.plugins_cache_dir / "openai-curated-remote" / "figma" / "20.0.0" / ".codex-plugin",
        codex.plugins_cache_dir / "openai-curated-remote" / "github" / "0.1.0" / ".codex-plugin",
        codex.plugins_cache_dir / "mattpocock" / "mattpocock-skills" / "1.3.2" / ".claude-plugin",
        codex.sessions_dir,
        agy.skills_dir,
        agy.plugins_dir / "stale-agy-plugin",
        agy.plugins_dir / "opencode",
        agy.projects_dir,
        agy.data_dir / "builtin" / "core-builtin",
        agy.conversations_dir,
        agy.brain_dir,
        agy.annotations_dir,
        agy_appdata,
        claude_home,
        opencode_cfg,
        opencode_data,
        dsh_home / "storages" / "session_projcache" / "sessions",
        dsh_home / "sessions" / "--workspace--" / "session-dsh-1",
    ):
        d.mkdir(parents=True, exist_ok=True)

    # Protected auth/system files
    (codex_home / "auth.json").write_text('{"access_token": "secret-codex-auth"}', encoding="utf-8")
    (agy.data_dir / "builtin" / "core-builtin" / "SKILL.md").write_text("builtin", encoding="utf-8")
    (codex.skills_dir / ".system" / "openai-docs" / "SKILL.md").write_text("system", encoding="utf-8")

    # Skills
    create_skill(codex.skills_dir, "shared-skill", "# Shared from Codex v2", extra_file="v2 ref")
    create_skill(codex.skills_dir, "codex-only-skill", "# Codex Exclusive")
    create_skill(codex.skills_dir, "no-frontmatter-skill", "# Raw Skill\nPlain body without yaml", with_frontmatter=False)
    create_skill(agy.skills_dir, "shared-skill", "# Old Antigravity v1")
    create_skill(agy.skills_dir, "only-agy-old", "# Should be wiped on bootstrap")

    # Codex plugin cache
    (codex.plugins_cache_dir / "openai-bundled" / "browser" / "26.1103.32239" / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "browser", "version": "26.1103.32239"}), encoding="utf-8"
    )
    (codex.plugins_cache_dir / "openai-curated-remote" / "github" / "0.1.0" / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "github", "version": "0.1.0"}), encoding="utf-8"
    )
    (codex.plugins_cache_dir / "openai-curated-remote" / "github" / "0.1.0" / ".app.json").write_text("{}", encoding="utf-8")

    figma_dir = codex.plugins_cache_dir / "openai-curated-remote" / "figma" / "20.0.0"
    (figma_dir / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "figma", "version": "20.0.0", "description": "Figma MCP plugin"}), encoding="utf-8"
    )
    (figma_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"figma": {"url": "https://mcp.figma.com/mcp"}}}), encoding="utf-8"
    )
    create_skill(figma_dir / "skills", "figma-helper", "# Figma helper")

    mp_dir = codex.plugins_cache_dir / "mattpocock" / "mattpocock-skills" / "1.3.2"
    (mp_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "mattpocock-skills", "version": "1.3.2", "description": "Engineering skills"}),
        encoding="utf-8",
    )
    create_skill(mp_dir / "skills" / "engineering", "tdd-mp", "# TDD skill")

    # Antigravity stale plugin
    (agy.plugins_dir / "stale-agy-plugin" / "plugin.json").write_text(
        json.dumps({"name": "stale-agy-plugin", "version": "0.1.0"}), encoding="utf-8"
    )

    # Codex config.toml
    codex_cfg = {
        "model": "gpt-6.2-sol",
        "model_reasoning_effort": "high",
        "plugins": {"browser@openai-bundled": {"enabled": True}},
        "mcp_servers": {
            "node_repl": {
                "command": "C:\\Users\\dev\\AppData\\Local\\OpenAI\\Codex\\runtimes\\cua_node\\bin\\node_repl.exe",
                "args": [],
                "env": {"SKY_CUA_NATIVE_PIPE_DIRECTORY": "\\\\.\\pipe\\codex-computer-use-123"},
            },
            "context7": {
                "command": "npx",
                "args": ["-y", "@upstash/context7-mcp"],
                "env": {"CONTEXT7_API_KEY": "sk-secret-value-1234567890abcdef"},
                "startup_timeout_sec": 30,
            },
            "motionsites": {
                "url": "https://motionsites.ai/api/mcp",
                "bearer_token_env_var": "MOTIONSITES_TOKEN",
                "http_headers": {"X-Custom": "1"},
            },
            "legacy_sse_srv": {"url": "https://example.com/sse", "type": "sse"},
        },
        "projects": {
            str(ws_app).lower(): {"trust_level": "trusted"},
            str(ws_docs): {"trust_level": "trusted"},
        },
    }
    codex.config_toml.write_text(tomli_w.dumps(codex_cfg), encoding="utf-8")

    # Antigravity config.json + mcp_config.json
    agy.config_json.write_text(
        json.dumps(
            {
                "sidecars": {"swarm/swarm_board": {"projectId": "sidecar-uuid-1"}},
                "userSettings": {"permissionPreset": "AGENT_PERMISSION_PRESET_TURBO", "voiceName": "Fola"},
                "plugins": {"stale-agy-plugin": {"enabled": True}},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    agy.mcp_config_json.write_text(
        json.dumps({"mcpServers": {"old-agy-mcp": {"command": "echo", "args": ["old"]}}}),
        encoding="utf-8",
    )

    # Antigravity sidecar + real projects
    (agy.projects_dir / "sidecar-uuid-1.json").write_text(
        json.dumps(
            {
                "id": "sidecar-uuid-1",
                "name": "swarm/swarm_board",
                "projectResources": {},
                "sidecar": {"sidecarId": "swarm/swarm_board"},
            }
        ),
        encoding="utf-8",
    )
    (agy.projects_dir / "agy-proj-app.json").write_text(
        json.dumps(
            {
                "id": "agy-proj-app",
                "name": "core-app",
                "projectResources": {
                    "resources": [
                        {"gitFolder": {"folderUri": path_to_file_uri(str(ws_app)), "defaultBranch": "main"}}
                    ]
                },
                "permissionGrants": {"v2Migrated": True},
                "settings": {"customKeep": True},
            }
        ),
        encoding="utf-8",
    )
    (agy.projects_dir / "agy-proj-sync.json").write_text(
        json.dumps(
            {
                "id": "agy-proj-sync",
                "name": "agentsync-repo",
                "projectResources": {"resources": [{"folderUri": path_to_file_uri(str(ws_sync))}]},
                "permissionGrants": {"v2Migrated": True},
                "settings": {},
            }
        ),
        encoding="utf-8",
    )
    agy.app_storage_json.write_text(
        json.dumps(
            {
                "agy.onboarding.lastLoginUsername": "dev@example.com",
                "projectsOrder": json.dumps(["agy-proj-app", "agy-proj-sync"]),
            }
        ),
        encoding="utf-8",
    )

    # Codex global state + state_5.sqlite with 3 duplicate rows for ws_app
    codex.global_state_json.write_text(
        json.dumps(
            {
                "electron-main-window-bounds": {"width": 1200, "height": 800},
                "local-projects": {
                    "legacy-app-1": {
                        "id": "legacy-app-1",
                        "name": "CoreApp",
                        "rootPaths": [str(ws_app)],
                        "createdAt": 1794312343446,
                        "updatedAt": 1794312363769,
                    },
                    "legacy-docs-1": {
                        "id": "legacy-docs-1",
                        "name": "DocsPortal",
                        "rootPaths": [str(ws_docs)],
                        "createdAt": 1795350389571,
                        "updatedAt": 1795350389571,
                    },
                },
                "project-order": ["legacy-docs-1", "legacy-app-1"],
                "app-server-project-id-by-legacy-project-id-by-host": {
                    f"local:{to_native_path(str(codex_home))}": {
                        "legacy-app-1": "cx-proj-app-1",
                        "legacy-docs-1": "cx-proj-docs-1",
                    }
                },
                "thread-project-assignments": {},
                "thread-project-membership-host-ids": {},
                "projectless-thread-ids": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    sconn = codex._ensure_state_db()
    sconn.execute(
        "INSERT INTO projects(id, name, metadata, position, created_at_ms, updated_at_ms) VALUES('cx-proj-app-1', 'CoreApp', '{}', 1, 1000, 2000)"
    )
    sconn.execute("INSERT INTO project_roots(project_id, position, path) VALUES('cx-proj-app-1', 0, ?)", (str(ws_app),))
    sconn.execute(
        "INSERT INTO projects(id, name, metadata, position, created_at_ms, updated_at_ms) VALUES('cx-proj-app-dup2', 'coreapp', '{}', 2, 1100, 3000)"
    )
    sconn.execute(
        "INSERT INTO project_roots(project_id, position, path) VALUES('cx-proj-app-dup2', 0, ?)",
        (str(ws_app).lower().replace("\\", "/"),),
    )
    sconn.execute(
        "INSERT INTO projects(id, name, metadata, position, created_at_ms, updated_at_ms) VALUES('cx-proj-app-dup3', 'coreapp', '{}', 3, 1200, 4000)"
    )
    sconn.execute(
        "INSERT INTO project_roots(project_id, position, path) VALUES('cx-proj-app-dup3', 0, ?)",
        (f"\\\\?\\{ws_app}\\" if "\\" in str(ws_app) else str(ws_app) + "/",),
    )
    sconn.execute(
        "INSERT INTO projects(id, name, metadata, position, created_at_ms, updated_at_ms) VALUES('cx-proj-docs-1', 'DocsPortal', '{}', 0, 1500, 1500)"
    )
    sconn.execute("INSERT INTO project_roots(project_id, position, path) VALUES('cx-proj-docs-1', 0, ?)", (str(ws_docs),))
    sconn.commit()
    sconn.close()

    # Instructions
    codex.agents_md.write_text(
        "# Global Rules\n\n## Shared Section\n\nKeep code clean.\n\n## Codex Rule\n\nUse pytest.\n",
        encoding="utf-8",
    )
    codex.agents_override_md.write_text(
        "# Global Rules\n\n## Shared Section\n\nKeep code clean.\n\n## Override Rule\n\nAlways verify before completion.\n",
        encoding="utf-8",
    )
    (ws_app / "AGENTS.md").write_text(
        "# App Rules\n\n## Architecture\n\nUse adapter pattern.\n\n## Codex Note\n\nRun ruff.\n",
        encoding="utf-8",
    )
    (ws_app / "GEMINI.md").write_text(
        "# App Rules\n\n## Architecture\n\nUse adapter pattern.\n\n## Antigravity Note\n\nUse dry-run first.\n",
        encoding="utf-8",
    )

    # Seed sample chats (1 Codex-only, 1 AGY-only, 1 duplicate across both)
    projs_c = codex.read_projects()
    projs_a = agy.read_projects()
    app_key = next(k for k in projs_c if k.endswith("/core-app"))

    codex.write_chat(
        ChatSession(
            tool="codex",
            native_id="cx-chat-unique",
            title="Codex Exclusive Chat",
            project_key=app_key,
            created_at_ms=1_795_000_000_000,
            updated_at_ms=1_795_000_010_000,
            messages=[
                ChatMessage(role="user", text="How do we fix routing latency?", timestamp_ms=1_795_000_000_000),
                ChatMessage(role="assistant", text="Inspect the routing table and profile latency.", timestamp_ms=1_795_000_005_000),
            ],
        ).compute_hashes(),
        target_native_id="cx-chat-unique",
        projects_by_key=projs_c,
    )
    codex.write_chat(
        ChatSession(
            tool="codex",
            native_id="cx-chat-dup",
            title="Shared Duplicate Chat",
            project_key=app_key,
            created_at_ms=1_795_100_000_000,
            updated_at_ms=1_795_100_010_000,
            messages=[
                ChatMessage(role="user", text="Please debug the auth token refresh bug.", timestamp_ms=1_795_100_000_000),
                ChatMessage(role="assistant", text="Checking auth.py refresh flow now.", timestamp_ms=1_795_100_005_000),
            ],
        ).compute_hashes(),
        target_native_id="cx-chat-dup",
        projects_by_key=projs_c,
    )
    agy.write_chat(
        ChatSession(
            tool="antigravity",
            native_id="agy-chat-unique",
            title="Antigravity Exclusive Chat",
            project_key=None,
            created_at_ms=1_795_200_000_000,
            updated_at_ms=1_795_200_010_000,
            messages=[
                ChatMessage(role="user", text="Draft the v1.0 release checklist.", timestamp_ms=1_795_200_000_000),
                ChatMessage(role="assistant", text="Here is the step-by-step release checklist.", timestamp_ms=1_795_200_005_000),
            ],
        ).compute_hashes(),
        target_native_id="agy-chat-unique",
        projects_by_key=projs_a,
    )
    agy.write_chat(
        ChatSession(
            tool="antigravity",
            native_id="agy-chat-dup",
            title="Shared Duplicate Chat",
            project_key=app_key,
            created_at_ms=1_795_100_000_000,
            updated_at_ms=1_795_100_010_000,
            messages=[
                ChatMessage(role="user", text="Please debug the auth token refresh bug.", timestamp_ms=1_795_100_000_000),
                ChatMessage(role="assistant", text="Checking auth.py refresh flow now.", timestamp_ms=1_795_100_005_000),
            ],
        ).compute_hashes(),
        target_native_id="agy-chat-dup",
        projects_by_key=projs_a,
    )

    # Seed DeepSeek Harness starter zstd v4 session for adapter tests
    cctx = zstandard.ZstdCompressor()
    zstd_path = dsh_home / "sessions" / "--workspace--" / "session-dsh-1" / "session.v4.jsonl.zstd"
    dsh_events = "\n".join(
        [
            json.dumps(
                {
                    "type": "session",
                    "version": 4,
                    "id": "session-dsh-1",
                    "createdAt": 1795349981036,
                    "cwd": str(ws_app),
                    "isSeeded": False,
                    "delegationDepth": 0,
                    "agentPreset": "standard",
                }
            ),
            json.dumps(
                {
                    "type": "message/user",
                    "seq": 1,
                    "time": 1795349982000,
                    "data": {"role": "user", "text": "Inspect DeepSeek workspace"},
                }
            ),
        ]
    ) + "\n"
    zstd_path.write_bytes(cctx.compress(dsh_events.encode("utf-8")))

    return {
        "config": cfg,
        "config_file": config_file,
        "codex": codex,
        "antigravity": agy,
        "claude": claude,
        "opencode": opencode,
        "dsh": dsh,
        "workspaces": {"app": ws_app, "docs": ws_docs, "sync": ws_sync},
    }
