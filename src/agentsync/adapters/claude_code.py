"""Claude Code adapter (`~/.claude` and `~/.claude.json`)."""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from agentsync.adapters.base import ToolAdapter, ToolCapabilities
from agentsync.adapters.registry import register_adapter
from agentsync.config import ToolConfig
from agentsync.models.canonical import (
    ChatMessage,
    ChatSession,
    DoctorIssue,
    InstructionDoc,
    McpServer,
    McpTransport,
    PluginItem,
    ProjectItem,
    SkillItem,
    ensure_skill_frontmatter,
    parse_skill_md,
)
from agentsync.utils.atomic_io import (
    atomic_write_json,
    atomic_write_text,
    remove_tree_if_exists,
    sync_directory_files,
)
from agentsync.utils.paths import normalize_path, path_dedup_key, to_native_path

_IGNORED_DIR_NAMES = {".git", "__pycache__", "node_modules", ".DS_Store"}


def _encode_claude_project_dir(path_str: str) -> str:
    """Encode a local path into Claude Code's project folder naming convention."""
    cleaned = to_native_path(path_str).replace("\\", "/").replace(":", "-")
    return re.sub(r"[^A-Za-z0-9._-]+", "-", cleaned).strip("-") or "default"


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


def _iso_utc(ts_ms: int) -> str:
    dt = datetime.fromtimestamp(max(0, ts_ms) / 1000, tz=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


@register_adapter
class ClaudeCodeAdapter(ToolAdapter):
    """Adapter for Anthropic Claude Code (`~/.claude` and `~/.claude.json`)."""

    tool_name = "claude-code"
    display_name = "Claude Code"
    capabilities = ToolCapabilities(
        skills=True,
        mcp=True,
        plugins=True,
        instructions=True,
        projects=True,
        chats=True,
        chat_write=True,
        plugin_write=True,
        project_names=False,
    )

    def __init__(self, tool_config: ToolConfig) -> None:
        super().__init__(tool_config)
        self.claude_json = self.extra_paths.get("claude_json") or (self.root_path.parent / ".claude.json")

    @property
    def skills_dir(self) -> Path:
        return self.root_path / "skills"

    @property
    def commands_dir(self) -> Path:
        return self.root_path / "commands"

    @property
    def plugins_dir(self) -> Path:
        return self.root_path / "plugins"

    @property
    def global_claude_md(self) -> Path:
        return self.root_path / "CLAUDE.md"

    @property
    def projects_dir(self) -> Path:
        return self.root_path / "projects"

    def backup_targets(
        self,
    ) -> tuple[list[tuple[Path, Literal["file", "sqlite"]]], list[Path]]:
        return (
            [(self.claude_json, "file"), (self.global_claude_md, "file")],
            [self.skills_dir, self.plugins_dir, self.projects_dir],
        )

    def watch_roots(self) -> list[Path]:
        roots = [self.root_path] if self.root_path.exists() else []
        return roots

    def _load_claude_json(self) -> tuple[dict[str, Any], int]:
        if not self.claude_json.exists():
            return {}, 0
        txt = self.claude_json.read_text(encoding="utf-8-sig").strip()
        mtime_ms = int(self.claude_json.stat().st_mtime * 1000)
        return (json.loads(txt) if txt else {}), mtime_ms

    def _save_claude_json(self, data: dict[str, Any]) -> bool:
        return atomic_write_json(self.claude_json, data)

    def clear_managed_for_bootstrap(self) -> dict[str, list[str]]:
        removed: dict[str, list[str]] = {"skills": [], "mcp": [], "plugins": []}
        if self.skills_dir.exists():
            for item in sorted(self.skills_dir.iterdir()):
                if not item.name.startswith(".") and remove_tree_if_exists(item):
                    removed["skills"].append(item.name)
        removed["mcp"] = sorted(self.read_mcp_servers().keys())
        self.write_mcp_servers({})
        if self.plugins_dir.exists():
            for item in sorted(self.plugins_dir.iterdir()):
                if not item.name.startswith(".") and remove_tree_if_exists(item):
                    removed["plugins"].append(item.name)
        return removed

    # --- 1. Skills ---

    def read_skills(self) -> dict[str, SkillItem]:
        result: dict[str, SkillItem] = {}
        if self.skills_dir.exists():
            for sub in sorted(self.skills_dir.iterdir()):
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

        # Also read flat ~/.claude/commands/*.md as skills when not shadowed by skills/
        if self.commands_dir.exists():
            for cf in sorted(self.commands_dir.glob("*.md")):
                sname = cf.stem
                if sname in result:
                    continue
                raw = cf.read_text(encoding="utf-8-sig", errors="replace")
                fixed = ensure_skill_frontmatter(raw, sname)
                fm, _, _ = parse_skill_md(fixed, sname)
                result[sname] = SkillItem(
                    name=sname,
                    frontmatter_name=str(fm.get("name") or sname),
                    description=str(fm.get("description") or ""),
                    files={"SKILL.md": fixed.encode("utf-8")},
                    updated_at_ms=int(cf.stat().st_mtime * 1000),
                    source_tool=self.tool_name,
                    synthesized_frontmatter=True,
                ).compute_hashes()
        return result

    def write_skill(self, skill: SkillItem) -> bool:
        return sync_directory_files(self.skills_dir / skill.name, skill.files)

    def delete_skill(self, skill_name: str) -> bool:
        return remove_tree_if_exists(self.skills_dir / skill_name)

    # --- 2. MCP Servers ---

    def read_mcp_servers(self) -> dict[str, McpServer]:
        data, mtime_ms = self._load_claude_json()
        raw_servers = data.get("mcpServers") or {}
        result: dict[str, McpServer] = {}
        for name, entry in sorted(raw_servers.items()):
            if not isinstance(entry, dict):
                continue
            url = entry.get("url")
            transport: McpTransport
            if url:
                rtype = str(entry.get("type") or "").lower()
                transport = "sse" if (rtype == "sse" or str(url).rstrip("/").lower().endswith("/sse")) else "http"
            else:
                transport = "stdio"
            result[name] = McpServer(
                name=name,
                transport=transport,
                command=str(entry["command"]) if entry.get("command") else None,
                args=[str(a) for a in (entry.get("args") or [])],
                env={str(k): str(v) for k, v in (entry.get("env") or {}).items()},
                cwd=str(entry["cwd"]) if entry.get("cwd") else None,
                url=str(url) if url else None,
                headers={str(k): str(v) for k, v in (entry.get("headers") or {}).items()},
                disabled=bool(entry.get("disabled", False)),
                portable=True,
                source_tool=self.tool_name,
                updated_at_ms=mtime_ms,
            ).compute_hash()
        return result

    def write_mcp_servers(self, servers: dict[str, McpServer]) -> tuple[bool, list[str]]:
        data, _ = self._load_claude_json()
        out_map: dict[str, dict[str, Any]] = {}
        for name, srv in sorted(servers.items()):
            if not srv.portable:
                continue
            entry: dict[str, Any] = {}
            if srv.transport == "stdio":
                if not srv.command:
                    continue
                entry["type"] = "stdio"
                entry["command"] = srv.command
                if srv.args:
                    entry["args"] = list(srv.args)
                if srv.env:
                    entry["env"] = dict(srv.env)
                if srv.cwd:
                    entry["cwd"] = to_native_path(srv.cwd)
            else:
                if not srv.url:
                    continue
                entry["type"] = "sse" if srv.transport == "sse" else "http"
                entry["url"] = srv.url
                if srv.headers:
                    entry["headers"] = dict(srv.headers)
            if srv.disabled:
                entry["disabled"] = True
            out_map[name] = entry

        data["mcpServers"] = out_map
        return self._save_claude_json(data), []

    # --- 3. Plugins ---

    def read_plugins(self) -> dict[str, PluginItem]:
        result: dict[str, PluginItem] = {}
        if not self.plugins_dir.exists():
            return result
        for sub in sorted(self.plugins_dir.iterdir()):
            if not sub.is_dir() or sub.name.startswith("."):
                continue
            manifest_path = sub / ".claude-plugin" / "plugin.json"
            if not manifest_path.exists():
                manifest_path = sub / "plugin.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            except Exception:
                continue

            pskills: dict[str, SkillItem] = {}
            sdir = sub / "skills"
            if sdir.exists():
                for sk in sorted(sdir.iterdir()):
                    if sk.is_dir() and (sk / "SKILL.md").exists():
                        files, m_ms = _read_dir_files(sk)
                        md = files["SKILL.md"].decode("utf-8", errors="replace")
                        fm, _, had_fm = parse_skill_md(md, sk.name)
                        pskills[sk.name] = SkillItem(
                            name=sk.name,
                            frontmatter_name=str(fm.get("name") or sk.name),
                            description=str(fm.get("description") or ""),
                            files=files,
                            updated_at_ms=m_ms,
                            source_tool=self.tool_name,
                            synthesized_frontmatter=not had_fm,
                        ).compute_hashes()

            pmcps: dict[str, McpServer] = {}
            mcp_p = sub / ".mcp.json"
            if mcp_p.exists():
                try:
                    mdata = json.loads(mcp_p.read_text(encoding="utf-8-sig"))
                    m_ms = int(mcp_p.stat().st_mtime * 1000)
                    for sname, sentry in (mdata.get("mcpServers") or {}).items():
                        if isinstance(sentry, dict):
                            url = sentry.get("url")
                            pmcps[sname] = McpServer(
                                name=sname,
                                transport="http" if url else "stdio",
                                command=sentry.get("command"),
                                args=list(sentry.get("args") or []),
                                env=dict(sentry.get("env") or {}),
                                url=url,
                                headers=dict(sentry.get("headers") or {}),
                                source_tool=self.tool_name,
                                updated_at_ms=m_ms,
                            ).compute_hash()
                except Exception:
                    pass

            mtime_ms = max(
                [int(manifest_path.stat().st_mtime * 1000), *(s.updated_at_ms for s in pskills.values())]
            )
            result[sub.name] = PluginItem(
                name=sub.name,
                display_name=str(manifest.get("displayName") or manifest.get("name") or sub.name),
                version=str(manifest.get("version") or "1.0.0"),
                description=str(manifest.get("description") or ""),
                enabled=not bool(manifest.get("disabled", False)),
                skills=pskills,
                mcp_servers=pmcps,
                portable=True,
                source_tool=self.tool_name,
                updated_at_ms=mtime_ms,
            ).compute_hash()
        return result

    def write_plugin(self, plugin: PluginItem) -> tuple[bool, list[str]]:
        if not plugin.portable:
            return False, [f"Plugin '{plugin.name}' skipped ({plugin.skip_reason})"]
        target = self.plugins_dir / plugin.name
        files: dict[str, bytes] = {}
        manifest = {
            "name": plugin.name,
            "version": plugin.version or "1.0.0",
            "description": plugin.description or f"Synced plugin {plugin.name}",
        }
        files[".claude-plugin/plugin.json"] = (
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
        ).encode("utf-8")
        portable_mcps = {k: v for k, v in plugin.mcp_servers.items() if v.portable}
        if portable_mcps:
            mcp_out: dict[str, Any] = {}
            for k, v in sorted(portable_mcps.items()):
                if v.transport == "stdio" and v.command:
                    mcp_out[k] = {"command": v.command, "args": v.args, "env": v.env}
                elif v.url:
                    mcp_out[k] = {"url": v.url, "headers": v.headers}
            files[".mcp.json"] = (
                json.dumps({"mcpServers": mcp_out}, indent=2, ensure_ascii=False) + "\n"
            ).encode("utf-8")
        for sname, sitem in sorted(plugin.skills.items()):
            for rel, data in sitem.files.items():
                files[f"skills/{sname}/{rel}"] = data
        return sync_directory_files(target, files), []

    def delete_plugin(self, plugin_name: str) -> bool:
        return remove_tree_if_exists(self.plugins_dir / plugin_name)

    # --- 4. Instructions ---

    def read_global_instructions(self) -> InstructionDoc | None:
        if not self.global_claude_md.exists():
            return None
        return InstructionDoc.from_markdown(
            raw_markdown=self.global_claude_md.read_text(encoding="utf-8-sig", errors="replace"),
            scope="global",
            source_tool=self.tool_name,
            updated_at_ms=int(self.global_claude_md.stat().st_mtime * 1000),
            source_files=[str(self.global_claude_md)],
        )

    def write_global_instructions(self, doc: InstructionDoc) -> tuple[bool, list[str]]:
        return atomic_write_text(self.global_claude_md, doc.raw_text), []

    def read_project_instructions(self, project_path: Path) -> InstructionDoc | None:
        p = project_path / "CLAUDE.md"
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

    def write_project_instructions(
        self, project_path: Path, doc: InstructionDoc
    ) -> tuple[bool, list[str]]:
        if not project_path.exists():
            return False, [f"Project directory not found: {project_path}"]
        return atomic_write_text(project_path / "CLAUDE.md", doc.raw_text), []

    # --- 5. Projects ---

    def read_projects(self) -> dict[str, ProjectItem]:
        projects: dict[str, ProjectItem] = {}
        data, mtime_ms = self._load_claude_json()
        for raw_p in (data.get("projects") or {}).keys():
            key = path_dedup_key(raw_p)
            if key and key not in projects:
                projects[key] = ProjectItem.from_path(
                    raw_path=raw_p,
                    native_ids_by_tool={self.tool_name: [_encode_claude_project_dir(raw_p)]},
                    updated_at_ms=mtime_ms,
                )

        global_fallback_key = path_dedup_key(str(self.root_path / "global"))
        if self.projects_dir.exists():
            for pdir in sorted(self.projects_dir.iterdir()):
                if not pdir.is_dir():
                    continue
                for jf in sorted(pdir.glob("*.jsonl")):
                    try:
                        for line in jf.read_text(encoding="utf-8-sig", errors="replace").splitlines()[:10]:
                            obj = json.loads(line)
                            cwd = obj.get("cwd")
                            if cwd:
                                key = path_dedup_key(cwd)
                                if key and key != global_fallback_key and key not in projects:
                                    projects[key] = ProjectItem.from_path(
                                        raw_path=cwd,
                                        native_ids_by_tool={self.tool_name: [pdir.name]},
                                        updated_at_ms=int(jf.stat().st_mtime * 1000),
                                    )
                                break
                    except Exception:
                        continue
        return projects

    def write_projects(
        self, projects: dict[str, ProjectItem]
    ) -> tuple[bool, dict[str, ProjectItem]]:
        data, _ = self._load_claude_json()
        proj_map = dict(data.get("projects") or {})
        existing_by_key = {path_dedup_key(k): k for k in proj_map}
        updated: dict[str, ProjectItem] = {}

        for key, item in sorted(projects.items()):
            copy_item = item.model_copy(deep=True)
            encoded = _encode_claude_project_dir(copy_item.display_path)
            copy_item.native_ids_by_tool[self.tool_name] = [encoded]
            if key not in existing_by_key:
                proj_map[to_native_path(copy_item.display_path)] = {"allowedTools": []}
            updated[key] = copy_item

        data["projects"] = proj_map
        changed = self._save_claude_json(data)
        return changed, updated

    # --- 6. Chats ---

    def _extract_claude_message_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                    parts.append(str(block["text"]))
                elif isinstance(block, str):
                    parts.append(block)
            return "\n".join(parts).strip()
        return ""

    def read_chats(
        self,
        projects_by_key: dict[str, ProjectItem],
        include_subagents: bool = False,
    ) -> list[ChatSession]:
        sessions: list[ChatSession] = []
        if not self.projects_dir.exists():
            return sessions
        for pdir in sorted(self.projects_dir.iterdir()):
            if not pdir.is_dir():
                continue
            for jf in sorted(pdir.glob("*.jsonl")):
                sid = jf.stem
                messages: list[ChatMessage] = []
                cwd = ""
                title = ""
                created_ms = 0
                updated_ms = int(jf.stat().st_mtime * 1000)
                is_subagent = False
                for line in jf.read_text(encoding="utf-8-sig", errors="replace").splitlines():
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if obj.get("isSidechain"):
                        is_subagent = True
                    if not cwd and obj.get("cwd"):
                        cwd = str(obj["cwd"])
                    if obj.get("type") == "summary" and obj.get("summary"):
                        title = str(obj["summary"]).strip()
                    if obj.get("type") in ("user", "assistant"):
                        msg_obj = obj.get("message") or {}
                        txt = self._extract_claude_message_text(msg_obj.get("content"))
                        ts_ms = 0
                        if obj.get("timestamp"):
                            try:
                                ts_ms = int(
                                    datetime.fromisoformat(
                                        str(obj["timestamp"]).replace("Z", "+00:00")
                                    ).timestamp()
                                    * 1000
                                )
                            except Exception:
                                pass
                        if txt:
                            if not created_ms and ts_ms:
                                created_ms = ts_ms
                            if ts_ms > updated_ms:
                                updated_ms = ts_ms
                            messages.append(
                                ChatMessage(role=obj["type"], text=txt, timestamp_ms=ts_ms)
                            )
                if is_subagent and not include_subagents:
                    continue
                if not messages and not title:
                    continue
                global_fallback_key = path_dedup_key(str(self.root_path / "global"))
                proj_key = path_dedup_key(cwd) if cwd else None
                if proj_key == global_fallback_key:
                    proj_key = None
                if proj_key and proj_key not in projects_by_key:
                    proj_path = normalize_path(cwd)
                elif proj_key:
                    proj_path = projects_by_key[proj_key].display_path
                else:
                    proj_path = None
                if not title and messages:
                    title = messages[0].text.splitlines()[0][:80]
                sessions.append(
                    ChatSession(
                        tool=self.tool_name,
                        native_id=sid,
                        title=title,
                        project_key=proj_key,
                        project_path=proj_path,
                        created_at_ms=created_ms or updated_ms,
                        updated_at_ms=updated_ms,
                        is_subagent=is_subagent,
                        messages=messages,
                    ).compute_hashes()
                )
        return sessions

    def write_chat(
        self,
        chat: ChatSession,
        target_native_id: str | None,
        projects_by_key: dict[str, ProjectItem],
    ) -> tuple[str, bool]:
        sid = target_native_id or str(uuid.uuid4())
        project = projects_by_key.get(chat.project_key) if chat.project_key else None
        cwd_native = to_native_path(project.display_path) if project else to_native_path(str(self.root_path / "global"))
        proj_folder = _encode_claude_project_dir(cwd_native)
        target = self.projects_dir / proj_folder / f"{sid}.jsonl"

        lines: list[str] = []
        if chat.title:
            lines.append(
                json.dumps(
                    {"type": "summary", "summary": chat.title, "sessionId": sid, "cwd": cwd_native},
                    ensure_ascii=False,
                )
            )
        parent_uuid: str | None = None
        base_ts = chat.created_at_ms or int(time.time() * 1000)
        for idx, msg in enumerate(chat.messages):
            if msg.role not in ("user", "assistant") or not msg.text.strip():
                continue
            muuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"claude-msg:{sid}:{idx}"))
            mts = msg.timestamp_ms or (base_ts + idx * 1000)
            entry = {
                "type": msg.role,
                "uuid": muuid,
                "parentUuid": parent_uuid,
                "sessionId": sid,
                "cwd": cwd_native,
                "timestamp": _iso_utc(mts),
                "isSidechain": False,
                "message": {
                    "role": msg.role,
                    "content": msg.text if msg.role == "user" else [{"type": "text", "text": msg.text}],
                },
            }
            lines.append(json.dumps(entry, ensure_ascii=False))
            parent_uuid = muuid

        changed = atomic_write_text(target, "\n".join(lines) + "\n")
        return sid, changed

    # --- Doctor ---

    def doctor(self) -> list[DoctorIssue]:
        issues: list[DoctorIssue] = []
        if self.claude_json.exists():
            try:
                json.loads(self.claude_json.read_text(encoding="utf-8-sig"))
            except Exception as exc:
                issues.append(
                    DoctorIssue(
                        tool=self.tool_name,
                        severity="error",
                        code="CLAUDE_INVALID_JSON",
                        path=str(self.claude_json),
                        message=f"Invalid JSON in .claude.json: {exc}",
                    )
                )
        if self.projects_dir.exists():
            for jf in sorted(self.projects_dir.rglob("*.jsonl")):
                raw = jf.read_bytes()
                if raw.startswith(b"\xef\xbb\xbf"):
                    issues.append(
                        DoctorIssue(
                            tool=self.tool_name,
                            severity="warning",
                            code="CLAUDE_SESSION_BOM",
                            path=str(jf),
                            message="Session JSONL starts with UTF-8 BOM",
                        )
                    )
                for idx, line in enumerate(raw.decode("utf-8-sig", errors="replace").splitlines(), 1):
                    if not line.strip():
                        continue
                    try:
                        json.loads(line)
                    except Exception as exc:
                        issues.append(
                            DoctorIssue(
                                tool=self.tool_name,
                                severity="error",
                                code="CLAUDE_BROKEN_SESSION",
                                path=str(jf),
                                message=f"Invalid JSON at line {idx}: {exc}",
                            )
                        )
                        break
        return issues
