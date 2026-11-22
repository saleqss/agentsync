"""Zed Code / Zed Editor adapter (JSONC settings, context_servers MCP, tool_permissions, AGENTS.md/.rules, ~/.agents/skills)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from agentsync.adapters.base import ToolAdapter, ToolCapabilities
from agentsync.adapters.registry import register_adapter
from agentsync.config import ToolConfig
from agentsync.models.canonical import (
    DoctorIssue,
    InstructionDoc,
    McpServer,
    SkillItem,
    ensure_skill_frontmatter,
    parse_skill_md,
)
from agentsync.utils.atomic_io import atomic_write_text, remove_tree_if_exists, sync_directory_files
from agentsync.utils.env_vars import translate_env_vars_for_tool
from agentsync.utils.jsonc import loads_jsonc, patch_jsonc_file
from agentsync.utils.paths import normalize_path, path_dedup_key

_IGNORED_DIR_NAMES = {".git", "__pycache__", "node_modules", ".DS_Store"}


def _read_dir_files(root: Path) -> tuple[dict[str, bytes], int]:
    files: dict[str, bytes] = {}
    max_mtime_ms = 0
    for item in sorted(root.rglob("*")):
        if any(part in _IGNORED_DIR_NAMES for part in item.relative_to(root).parts):
            continue
        if item.is_file():
            files[item.relative_to(root).as_posix()] = item.read_bytes()
            max_mtime_ms = max(max_mtime_ms, int(item.stat().st_mtime * 1000))
    return files, max_mtime_ms


@register_adapter
class ZedAdapter(ToolAdapter):
    """Adapter for Zed Code / Zed Editor (`%APPDATA%\\Zed` or `~/.config/zed` + `~/.agents/skills`)."""

    tool_name = "zed"
    display_name = "Zed Editor"
    client_kind = "ide"
    capabilities = ToolCapabilities(
        skills=True,
        mcp=True,
        plugins=False,
        instructions=True,
        projects=False,
        chats=False,
        chat_write=False,
        plugin_write=False,
    )

    def __init__(self, tool_config: ToolConfig) -> None:
        super().__init__(tool_config)
        self.agents_skills_dir = self.extra_paths.get("skills_dir") or (Path.home() / ".agents" / "skills")
        self.global_agents_md = self.extra_paths.get("global_agents_md") or (self.root_path / "AGENTS.md")

    @property
    def settings_json(self) -> Path:
        return self.root_path / "settings.json"

    def backup_targets(self) -> tuple[list[tuple[Path, Literal["file", "sqlite"]]], list[Path]]:
        return (
            [(self.settings_json, "file"), (self.global_agents_md, "file")],
            [self.agents_skills_dir],
        )

    def watch_roots(self) -> list[Path]:
        return [p for p in (self.root_path, self.agents_skills_dir) if p.exists()]

    def clear_managed_for_bootstrap(self) -> dict[str, list[str]]:
        removed: dict[str, list[str]] = {"skills": [], "mcp": [], "plugins": []}
        if self.agents_skills_dir.exists():
            for item in sorted(self.agents_skills_dir.iterdir()):
                if not item.name.startswith(".") and remove_tree_if_exists(item):
                    removed["skills"].append(item.name)
        removed["mcp"] = sorted(self.read_mcp_servers().keys())
        self.write_mcp_servers({})
        return removed

    def _load_settings(self) -> tuple[dict[str, Any], int]:
        if not self.settings_json.exists():
            return {}, 0
        raw = self.settings_json.read_text(encoding="utf-8-sig")
        mtime_ms = int(self.settings_json.stat().st_mtime * 1000)
        parsed = loads_jsonc(raw)
        return (parsed if isinstance(parsed, dict) else {}), mtime_ms

    # --- 1. Skills (`~/.agents/skills/*/SKILL.md`) ---

    def read_skills(self) -> dict[str, SkillItem]:
        result: dict[str, SkillItem] = {}
        if not self.agents_skills_dir.exists():
            return result
        for sub in sorted(self.agents_skills_dir.iterdir()):
            if not sub.is_dir() or sub.name.startswith(".") or not (sub / "SKILL.md").exists():
                continue
            files, mtime_ms = _read_dir_files(sub)
            md = files["SKILL.md"].decode("utf-8", errors="replace")
            fm, _, had_fm = parse_skill_md(md, sub.name)
            result[sub.name] = SkillItem(
                name=sub.name,
                frontmatter_name=str(fm.get("name") or sub.name),
                description=str(fm.get("description") or ""),
                files=files,
                updated_at_ms=mtime_ms,
                source_tool=self.tool_name,
                synthesized_frontmatter=not had_fm,
            ).compute_hashes()
        return result

    def write_skill(self, skill: SkillItem) -> bool:
        out_files = dict(skill.files)
        if "SKILL.md" in out_files:
            md = out_files["SKILL.md"].decode("utf-8", errors="replace")
            out_files["SKILL.md"] = ensure_skill_frontmatter(md, skill.name).encode("utf-8")
        return sync_directory_files(self.agents_skills_dir / skill.name, out_files)

    def delete_skill(self, skill_name: str) -> bool:
        return remove_tree_if_exists(self.agents_skills_dir / skill_name)

    # --- 2. MCP Servers (`context_servers` in JSONC settings.json, preserving ACP/custom blocks & comments) ---

    def read_mcp_servers(self) -> dict[str, McpServer]:
        data, mtime_ms = self._load_settings()
        ctx_servers = data.get("context_servers") or {}
        if not isinstance(ctx_servers, dict):
            return {}
        result: dict[str, McpServer] = {}
        for name, entry in sorted(ctx_servers.items()):
            if not isinstance(entry, dict):
                continue
            # Zed supports both flat `{command, args, env}` and nested `{command: {path, args, env}}` or `{url}`
            url = entry.get("url")
            if url:
                transport = "sse" if str(url).rstrip("/").lower().endswith("/sse") else "http"
                headers = translate_env_vars_for_tool(
                    {str(k): str(v) for k, v in (entry.get("headers") or {}).items()},
                    self.tool_name,
                    to_canonical=True,
                )
                result[name] = McpServer(
                    name=name,
                    transport=transport,
                    url=str(url),
                    headers=headers,
                    disabled=entry.get("enabled") is False,
                    portable=True,
                    source_tool=self.tool_name,
                    updated_at_ms=mtime_ms,
                ).compute_hash()
                continue

            cmd_field = entry.get("command")
            if isinstance(cmd_field, dict):
                cmd = str(cmd_field.get("path") or cmd_field.get("command") or "")
                args = [str(a) for a in (cmd_field.get("args") or [])]
                env = {str(k): str(v) for k, v in (cmd_field.get("env") or {}).items()}
            elif isinstance(cmd_field, str):
                cmd = cmd_field
                args = [str(a) for a in (entry.get("args") or [])]
                env = {str(k): str(v) for k, v in (entry.get("env") or {}).items()}
            else:
                continue
            if not cmd:
                continue
            env = translate_env_vars_for_tool(env, self.tool_name, to_canonical=True)
            result[name] = McpServer(
                name=name,
                transport="stdio",
                command=cmd,
                args=args,
                env=env,
                disabled=entry.get("enabled") is False,
                portable=True,
                source_tool=self.tool_name,
                updated_at_ms=mtime_ms,
            ).compute_hash()
        return result

    def write_mcp_servers(self, servers: dict[str, McpServer]) -> tuple[bool, list[str]]:
        data, _ = self._load_settings()
        existing = data.get("context_servers") if isinstance(data.get("context_servers"), dict) else {}
        out: dict[str, Any] = {}
        # Preserve Zed-native ACP / non-MCP context_server blocks that have `source` == "extension"
        for k, v in existing.items():
            if isinstance(v, dict) and v.get("source") == "extension" and k not in servers:
                out[k] = v

        for name, srv in sorted(servers.items()):
            if not srv.portable:
                continue
            if srv.transport == "stdio":
                if not srv.command:
                    continue
                cmd_obj: dict[str, Any] = {"path": srv.command, "args": list(srv.args)}
                if srv.env:
                    cmd_obj["env"] = translate_env_vars_for_tool(dict(srv.env), self.tool_name)
                entry: dict[str, Any] = {"source": "custom", "command": cmd_obj}
                if srv.disabled:
                    entry["enabled"] = False
                out[name] = entry
            elif srv.url:
                entry = {"source": "custom", "url": srv.url}
                if srv.headers:
                    entry["headers"] = translate_env_vars_for_tool(dict(srv.headers), self.tool_name)
                if srv.disabled:
                    entry["enabled"] = False
                out[name] = entry

        # Comment-preserving surgical JSONC patch!
        changed = patch_jsonc_file(self.settings_json, {"context_servers": out})
        return changed, []

    def read_tool_permissions(self) -> dict[str, Any]:
        """Inspect Zed `agent.tool_permissions` for cross-host semantic mapping and audit."""
        data, _ = self._load_settings()
        agent = data.get("agent")
        if isinstance(agent, dict) and isinstance(agent.get("tool_permissions"), dict):
            return dict(agent["tool_permissions"])
        return {}

    # --- 4. Instructions (`AGENTS.md`, `.rules`, `CLAUDE.md`) ---

    def read_global_instructions(self) -> InstructionDoc | None:
        if not self.global_agents_md.exists():
            return None
        return InstructionDoc.from_markdown(
            raw_markdown=self.global_agents_md.read_text(encoding="utf-8-sig", errors="replace"),
            scope="global",
            source_tool=self.tool_name,
            updated_at_ms=int(self.global_agents_md.stat().st_mtime * 1000),
            source_files=[str(self.global_agents_md)],
        )

    def write_global_instructions(self, doc: InstructionDoc) -> tuple[bool, list[str]]:
        return atomic_write_text(self.global_agents_md, doc.raw_text), []

    def read_project_instructions(self, project_path: Path) -> InstructionDoc | None:
        for fname in (".rules", "AGENTS.md", "CLAUDE.md"):
            p = project_path / fname
            if p.exists():
                return InstructionDoc.from_markdown(
                    raw_markdown=p.read_text(encoding="utf-8-sig", errors="replace"),
                    scope="project",
                    source_tool=self.tool_name,
                    updated_at_ms=int(p.stat().st_mtime * 1000),
                    project_key=path_dedup_key(str(project_path)),
                    project_path=normalize_path(str(project_path)),
                    source_files=[str(p)],
                )
        return None

    def write_project_instructions(self, project_path: Path, doc: InstructionDoc) -> tuple[bool, list[str]]:
        if not project_path.exists():
            return False, [f"Project directory not found: {project_path}"]
        changed = atomic_write_text(project_path / ".rules", doc.raw_text)
        if atomic_write_text(project_path / "AGENTS.md", doc.raw_text):
            changed = True
        return changed, []

    # --- Doctor ---

    def doctor(self) -> list[DoctorIssue]:
        issues: list[DoctorIssue] = []
        if self.settings_json.exists():
            try:
                loads_jsonc(self.settings_json.read_text(encoding="utf-8-sig"))
            except (json.JSONDecodeError, ValueError) as exc:
                issues.append(
                    DoctorIssue(
                        tool=self.tool_name,
                        severity="error",
                        code="ZED_INVALID_SETTINGS_JSONC",
                        path=str(self.settings_json),
                        message=f"Invalid JSONC in Zed settings.json: {exc}",
                    )
                )
        return issues
