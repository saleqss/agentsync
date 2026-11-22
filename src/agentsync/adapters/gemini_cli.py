"""Gemini CLI adapter (`~/.gemini/settings.json`, `GEMINI.md`, `~/.gemini/skills`)."""

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
class GeminiCliAdapter(ToolAdapter):
    """Adapter for Google Gemini CLI (`~/.gemini/settings.json` and `GEMINI.md`)."""

    tool_name = "gemini-cli"
    display_name = "Gemini CLI"
    client_kind = "cli"
    capabilities = ToolCapabilities(
        skills=True,
        mcp=True,
        plugins=False,
        instructions=True,
        projects=False,
        chats=False,
    )

    @property
    def settings_json(self) -> Path:
        return self.root_path / "settings.json"

    @property
    def gemini_md(self) -> Path:
        return self.root_path / "GEMINI.md"

    @property
    def skills_dir(self) -> Path:
        return self.root_path / "skills"

    def backup_targets(self) -> tuple[list[tuple[Path, Literal["file", "sqlite"]]], list[Path]]:
        return ([(self.settings_json, "file"), (self.gemini_md, "file")], [self.skills_dir])

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
        if not self.settings_json.exists():
            return {}
        data = loads_jsonc(self.settings_json.read_text(encoding="utf-8-sig"))
        mtime_ms = int(self.settings_json.stat().st_mtime * 1000)
        raw = data.get("mcpServers") or {}
        result: dict[str, McpServer] = {}
        for name, entry in sorted(raw.items()):
            if not isinstance(entry, dict):
                continue
            url = entry.get("httpUrl") or entry.get("url")
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
                out[name] = entry
            elif srv.url:
                key = "url" if srv.transport == "sse" else "httpUrl"
                entry = {key: srv.url}
                if srv.headers:
                    entry["headers"] = translate_env_vars_for_tool(dict(srv.headers), self.tool_name)
                out[name] = entry
        return patch_jsonc_file(self.settings_json, {"mcpServers": out}), []

    def read_global_instructions(self) -> InstructionDoc | None:
        if not self.gemini_md.exists():
            return None
        return InstructionDoc.from_markdown(
            raw_markdown=self.gemini_md.read_text(encoding="utf-8-sig", errors="replace"),
            scope="global",
            source_tool=self.tool_name,
            updated_at_ms=int(self.gemini_md.stat().st_mtime * 1000),
            source_files=[str(self.gemini_md)],
        )

    def write_global_instructions(self, doc: InstructionDoc) -> tuple[bool, list[str]]:
        return atomic_write_text(self.gemini_md, doc.raw_text), []

    def read_project_instructions(self, project_path: Path) -> InstructionDoc | None:
        p = project_path / "GEMINI.md"
        if not p.exists():
            return None
        return InstructionDoc.from_markdown(
            raw_markdown=p.read_text(encoding="utf-8-sig", errors="replace"),
            scope="project",
            source_tool=self.tool_name,
            updated_at_ms=int(p.stat().st_mtime * 1000),
            project_key=path_dedup_key(str(project_path)),
            project_path=normalize_path(str(project_path)),
            source_files=[str(p)],
        )

    def write_project_instructions(self, project_path: Path, doc: InstructionDoc) -> tuple[bool, list[str]]:
        if not project_path.exists():
            return False, [f"Project directory not found: {project_path}"]
        return atomic_write_text(project_path / "GEMINI.md", doc.raw_text), []

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
                        code="GEMINI_CLI_INVALID_JSON",
                        path=str(self.settings_json),
                        message=f"Invalid settings.json: {exc}",
                    )
                )
        return issues
