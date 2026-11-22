"""Cursor IDE adapter (`~/.cursor/mcp.json`, `~/.cursor/skills`, `.cursor/rules`)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from agentsync.adapters.base import ToolAdapter, ToolCapabilities
from agentsync.adapters.registry import register_adapter
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


def _read_dir_files(root: Path) -> tuple[dict[str, bytes], int]:
    files: dict[str, bytes] = {}
    max_mtime_ms = 0
    for item in sorted(root.rglob("*")):
        if item.is_file() and ".git" not in item.parts:
            files[item.relative_to(root).as_posix()] = item.read_bytes()
            max_mtime_ms = max(max_mtime_ms, int(item.stat().st_mtime * 1000))
    return files, max_mtime_ms


@register_adapter
class CursorAdapter(ToolAdapter):
    """Adapter for Cursor IDE (`~/.cursor/mcp.json`, `~/.cursor/skills`, `.cursor/rules`)."""

    tool_name = "cursor"
    display_name = "Cursor"
    client_kind = "ide"
    capabilities = ToolCapabilities(
        skills=True,
        mcp=True,
        plugins=False,
        instructions=True,
        projects=False,
        chats=False,
    )

    @property
    def mcp_json(self) -> Path:
        return self.root_path / "mcp.json"

    @property
    def skills_dir(self) -> Path:
        return self.root_path / "skills"

    @property
    def global_rules_md(self) -> Path:
        return self.root_path / "rules" / "global.mdc"

    def backup_targets(self) -> tuple[list[tuple[Path, Literal["file", "sqlite"]]], list[Path]]:
        return ([(self.mcp_json, "file"), (self.global_rules_md, "file")], [self.skills_dir])

    def read_skills(self) -> dict[str, SkillItem]:
        out: dict[str, SkillItem] = {}
        if not self.skills_dir.exists():
            return out
        for sub in sorted(self.skills_dir.iterdir()):
            if not sub.is_dir() or sub.name.startswith(".") or not (sub / "SKILL.md").exists():
                continue
            files, mtime_ms = _read_dir_files(sub)
            fm, _, had_fm = parse_skill_md(files["SKILL.md"].decode("utf-8", errors="replace"), sub.name)
            out[sub.name] = SkillItem(
                name=sub.name,
                frontmatter_name=str(fm.get("name") or sub.name),
                description=str(fm.get("description") or ""),
                files=files,
                updated_at_ms=mtime_ms,
                source_tool=self.tool_name,
                synthesized_frontmatter=not had_fm,
            ).compute_hashes()
        return out

    def write_skill(self, skill: SkillItem) -> bool:
        files = dict(skill.files)
        if "SKILL.md" in files:
            files["SKILL.md"] = ensure_skill_frontmatter(
                files["SKILL.md"].decode("utf-8", errors="replace"), skill.name
            ).encode("utf-8")
        return sync_directory_files(self.skills_dir / skill.name, files)

    def delete_skill(self, skill_name: str) -> bool:
        return remove_tree_if_exists(self.skills_dir / skill_name)

    def read_mcp_servers(self) -> dict[str, McpServer]:
        if not self.mcp_json.exists():
            return {}
        data = loads_jsonc(self.mcp_json.read_text(encoding="utf-8-sig"))
        mtime_ms = int(self.mcp_json.stat().st_mtime * 1000)
        result: dict[str, McpServer] = {}
        for name, entry in sorted((data.get("mcpServers") or {}).items()):
            if not isinstance(entry, dict):
                continue
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
                    disabled=bool(entry.get("disabled", False)),
                    source_tool=self.tool_name,
                    updated_at_ms=mtime_ms,
                ).compute_hash()
            elif entry.get("command"):
                env = translate_env_vars_for_tool(
                    {str(k): str(v) for k, v in (entry.get("env") or {}).items()},
                    self.tool_name,
                    to_canonical=True,
                )
                result[name] = McpServer(
                    name=name,
                    transport="stdio",
                    command=str(entry["command"]),
                    args=[str(a) for a in (entry.get("args") or [])],
                    env=env,
                    disabled=bool(entry.get("disabled", False)),
                    source_tool=self.tool_name,
                    updated_at_ms=mtime_ms,
                ).compute_hash()
        return result

    def write_mcp_servers(self, servers: dict[str, McpServer]) -> tuple[bool, list[str]]:
        out: dict[str, Any] = {}
        for name, srv in sorted(servers.items()):
            if not srv.portable:
                continue
            if srv.transport == "stdio" and srv.command:
                entry: dict[str, Any] = {"command": srv.command, "args": list(srv.args)}
                if srv.env:
                    entry["env"] = translate_env_vars_for_tool(dict(srv.env), self.tool_name)
                if srv.disabled:
                    entry["disabled"] = True
                out[name] = entry
            elif srv.url:
                entry = {"url": srv.url}
                if srv.headers:
                    entry["headers"] = translate_env_vars_for_tool(dict(srv.headers), self.tool_name)
                if srv.disabled:
                    entry["disabled"] = True
                out[name] = entry
        return patch_jsonc_file(self.mcp_json, {"mcpServers": out}), []

    def read_global_instructions(self) -> InstructionDoc | None:
        if not self.global_rules_md.exists():
            return None
        return InstructionDoc.from_markdown(
            raw_markdown=self.global_rules_md.read_text(encoding="utf-8-sig", errors="replace"),
            scope="global",
            source_tool=self.tool_name,
            updated_at_ms=int(self.global_rules_md.stat().st_mtime * 1000),
            source_files=[str(self.global_rules_md)],
        )

    def write_global_instructions(self, doc: InstructionDoc) -> tuple[bool, list[str]]:
        wrapped = f"---\nalwaysApply: true\n---\n\n{doc.raw_text.lstrip()}"
        return atomic_write_text(self.global_rules_md, wrapped), []

    def read_project_instructions(self, project_path: Path) -> InstructionDoc | None:
        for cand in (project_path / ".cursor" / "rules" / "agentsync.mdc", project_path / ".cursorrules"):
            if cand.exists():
                return InstructionDoc.from_markdown(
                    raw_markdown=cand.read_text(encoding="utf-8-sig", errors="replace"),
                    scope="project",
                    source_tool=self.tool_name,
                    updated_at_ms=int(cand.stat().st_mtime * 1000),
                    project_key=path_dedup_key(str(project_path)),
                    project_path=normalize_path(str(project_path)),
                    source_files=[str(cand)],
                )
        return None

    def write_project_instructions(self, project_path: Path, doc: InstructionDoc) -> tuple[bool, list[str]]:
        if not project_path.exists():
            return False, [f"Project directory not found: {project_path}"]
        wrapped = f"---\nalwaysApply: true\n---\n\n{doc.raw_text.lstrip()}"
        return atomic_write_text(project_path / ".cursor" / "rules" / "agentsync.mdc", wrapped), []

    def doctor(self) -> list[DoctorIssue]:
        issues: list[DoctorIssue] = []
        if self.mcp_json.exists():
            try:
                loads_jsonc(self.mcp_json.read_text(encoding="utf-8-sig"))
            except (json.JSONDecodeError, ValueError) as exc:
                issues.append(
                    DoctorIssue(
                        tool=self.tool_name,
                        severity="error",
                        code="CURSOR_INVALID_MCP_JSON",
                        path=str(self.mcp_json),
                        message=f"Invalid Cursor mcp.json: {exc}",
                    )
                )
        return issues
