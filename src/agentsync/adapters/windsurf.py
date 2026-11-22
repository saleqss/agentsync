"""Windsurf IDE adapter (`~/.codeium/windsurf/mcp_config.json`, `.windsurfrules`)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from agentsync.adapters.base import ToolAdapter, ToolCapabilities
from agentsync.adapters.registry import register_adapter
from agentsync.models.canonical import DoctorIssue, InstructionDoc, McpServer
from agentsync.utils.atomic_io import atomic_write_text
from agentsync.utils.env_vars import translate_env_vars_for_tool
from agentsync.utils.jsonc import loads_jsonc, patch_jsonc_file
from agentsync.utils.paths import normalize_path, path_dedup_key


@register_adapter
class WindsurfAdapter(ToolAdapter):
    """Adapter for Codeium Windsurf (`~/.codeium/windsurf/mcp_config.json` and rules)."""

    tool_name = "windsurf"
    display_name = "Windsurf"
    client_kind = "ide"
    capabilities = ToolCapabilities(mcp=True, instructions=True)

    @property
    def mcp_config_json(self) -> Path:
        return self.root_path / "mcp_config.json"

    @property
    def global_rules_md(self) -> Path:
        return self.root_path / "memories" / "global_rules.md"

    def backup_targets(self) -> tuple[list[tuple[Path, Literal["file", "sqlite"]]], list[Path]]:
        return ([(self.mcp_config_json, "file"), (self.global_rules_md, "file")], [])

    def read_mcp_servers(self) -> dict[str, McpServer]:
        if not self.mcp_config_json.exists():
            return {}
        data = loads_jsonc(self.mcp_config_json.read_text(encoding="utf-8-sig"))
        mtime_ms = int(self.mcp_config_json.stat().st_mtime * 1000)
        result: dict[str, McpServer] = {}
        for name, entry in sorted((data.get("mcpServers") or {}).items()):
            if not isinstance(entry, dict):
                continue
            url = entry.get("serverUrl") or entry.get("url")
            if url:
                headers = translate_env_vars_for_tool(
                    {str(k): str(v) for k, v in (entry.get("headers") or {}).items()},
                    self.tool_name,
                    to_canonical=True,
                )
                result[name] = McpServer(
                    name=name,
                    transport="sse" if str(url).rstrip("/").lower().endswith("/sse") else "http",
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
                entry = {"serverUrl": srv.url}
                if srv.headers:
                    entry["headers"] = translate_env_vars_for_tool(dict(srv.headers), self.tool_name)
                out[name] = entry
        return patch_jsonc_file(self.mcp_config_json, {"mcpServers": out}), []

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
        return atomic_write_text(self.global_rules_md, doc.raw_text), []

    def read_project_instructions(self, project_path: Path) -> InstructionDoc | None:
        p = project_path / ".windsurfrules"
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
        return atomic_write_text(project_path / ".windsurfrules", doc.raw_text), []

    def doctor(self) -> list[DoctorIssue]:
        issues: list[DoctorIssue] = []
        if self.mcp_config_json.exists():
            try:
                loads_jsonc(self.mcp_config_json.read_text(encoding="utf-8-sig"))
            except (json.JSONDecodeError, ValueError) as exc:
                issues.append(
                    DoctorIssue(
                        tool=self.tool_name,
                        severity="error",
                        code="WINDSURF_INVALID_MCP_JSON",
                        path=str(self.mcp_config_json),
                        message=f"Invalid Windsurf mcp_config.json: {exc}",
                    )
                )
        return issues
