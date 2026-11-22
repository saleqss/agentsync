"""OpenCode adapter (`~/.config/opencode` and `~/.local/share/opencode/opencode.db`)."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
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
    ProjectItem,
    RepairResult,
    SkillItem,
    parse_skill_md,
)
from agentsync.utils.atomic_io import (
    atomic_write_json,
    atomic_write_text,
    remove_tree_if_exists,
    sqlite_safe_connect,
    sync_directory_files,
)
from agentsync.utils.env_vars import translate_env_vars_for_tool
from agentsync.utils.jsonc import loads_jsonc, patch_jsonc_file
from agentsync.utils.paths import normalize_path, path_dedup_key

_IGNORED_DIR_NAMES = {".git", "__pycache__", "node_modules", ".DS_Store"}

_OPENCODE_MIN_DDL = """
CREATE TABLE IF NOT EXISTS project (
    id text PRIMARY KEY,
    worktree text NOT NULL,
    vcs text,
    name text,
    time_created integer NOT NULL,
    time_updated integer NOT NULL
);
CREATE TABLE IF NOT EXISTS session_v2 (
    id text PRIMARY KEY,
    project_id text NOT NULL,
    workspace_id text,
    parent_id text,
    fork_session_id text,
    fork_boundary text,
    slug text NOT NULL,
    directory text NOT NULL,
    path text,
    title text,
    version text NOT NULL DEFAULT '2.1.4',
    time_created integer NOT NULL,
    time_updated integer NOT NULL
);
CREATE TABLE IF NOT EXISTS session_message (
    id text PRIMARY KEY,
    session_id text NOT NULL,
    type text NOT NULL,
    seq integer NOT NULL,
    time_created integer NOT NULL,
    time_updated integer NOT NULL,
    data text NOT NULL
);
"""


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
class OpenCodeAdapter(ToolAdapter):
    """Adapter for OpenCode (`~/.config/opencode` and `~/.local/share/opencode/opencode.db`)."""

    tool_name = "opencode"
    display_name = "OpenCode"
    capabilities = ToolCapabilities(
        skills=True,
        mcp=True,
        plugins=False,
        instructions=True,
        projects=True,
        chats=True,
        chat_write=True,
        plugin_write=False,
    )

    def __init__(self, tool_config: ToolConfig) -> None:
        super().__init__(tool_config)
        self.data_dir = self.extra_paths.get("data_dir") or (self.root_path / "data")

    @property
    def opencode_json(self) -> Path:
        return self.root_path / "opencode.json"

    @property
    def skills_dir(self) -> Path:
        return self.root_path / "skills"

    @property
    def agents_md(self) -> Path:
        return self.root_path / "AGENTS.md"

    @property
    def opencode_db(self) -> Path:
        return self.data_dir / "opencode.db"

    def backup_targets(
        self,
    ) -> tuple[list[tuple[Path, Literal["file", "sqlite"]]], list[Path]]:
        return (
            [(self.opencode_json, "file"), (self.agents_md, "file"), (self.opencode_db, "sqlite")],
            [self.skills_dir],
        )

    def watch_roots(self) -> list[Path]:
        return [p for p in (self.root_path, self.data_dir) if p.exists()]

    def _load_opencode_json(self) -> tuple[dict[str, Any], int]:
        cfg_path = self.opencode_json
        if not cfg_path.exists() and (self.root_path / "opencode.jsonc").exists():
            cfg_path = self.root_path / "opencode.jsonc"
        if not cfg_path.exists():
            return {}, 0
        txt = cfg_path.read_text(encoding="utf-8-sig").strip()
        mtime_ms = int(cfg_path.stat().st_mtime * 1000)
        parsed = loads_jsonc(txt) if txt else {}
        return (parsed if isinstance(parsed, dict) else {}), mtime_ms

    def _save_opencode_json(self, data: dict[str, Any], *, mcp_only: bool = False) -> bool:
        cfg_path = self.opencode_json
        if not cfg_path.exists() and (self.root_path / "opencode.jsonc").exists():
            cfg_path = self.root_path / "opencode.jsonc"
        if mcp_only and cfg_path.exists():
            return patch_jsonc_file(cfg_path, {"mcp": data.get("mcp", {})})
        return atomic_write_json(cfg_path, data)

    def _ensure_db(self) -> sqlite3.Connection:
        conn = sqlite_safe_connect(self.opencode_db, readonly=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(_OPENCODE_MIN_DDL)
        conn.commit()
        return conn

    def clear_managed_for_bootstrap(self) -> dict[str, list[str]]:
        removed: dict[str, list[str]] = {"skills": [], "mcp": [], "plugins": []}
        if self.skills_dir.exists():
            for item in sorted(self.skills_dir.iterdir()):
                if not item.name.startswith(".") and remove_tree_if_exists(item):
                    removed["skills"].append(item.name)
        removed["mcp"] = sorted(self.read_mcp_servers().keys())
        self.write_mcp_servers({})
        return removed

    # --- 1. Skills ---

    def read_skills(self) -> dict[str, SkillItem]:
        result: dict[str, SkillItem] = {}
        if not self.skills_dir.exists():
            return result
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
        return result

    def write_skill(self, skill: SkillItem) -> bool:
        return sync_directory_files(self.skills_dir / skill.name, skill.files)

    def delete_skill(self, skill_name: str) -> bool:
        return remove_tree_if_exists(self.skills_dir / skill_name)

    # --- 2. MCP Servers ---

    def _parse_oc_mcp(self, name: str, entry: dict[str, Any], mtime_ms: int) -> McpServer | None:
        mtype = str(entry.get("type") or "").lower()
        if mtype == "remote" or entry.get("url"):
            url = str(entry.get("url") or "")
            if not url:
                return None
            transport: McpTransport = "sse" if url.rstrip("/").lower().endswith("/sse") else "http"
            headers = translate_env_vars_for_tool(
                {str(k): str(v) for k, v in (entry.get("headers") or {}).items()},
                self.tool_name,
                to_canonical=True,
            )
            return McpServer(
                name=name,
                transport=transport,
                url=url,
                headers=headers,
                disabled=entry.get("enabled") is False,
                portable=True,
                source_tool=self.tool_name,
                updated_at_ms=mtime_ms,
            ).compute_hash()
        cmd_raw = entry.get("command")
        if isinstance(cmd_raw, list) and cmd_raw:
            cmd = str(cmd_raw[0])
            args = [str(a) for a in cmd_raw[1:]]
        elif isinstance(cmd_raw, str) and cmd_raw:
            cmd = cmd_raw
            args = [str(a) for a in (entry.get("args") or [])]
        else:
            return None
        env_raw = entry.get("environment") or entry.get("env") or {}
        env = translate_env_vars_for_tool(
            {str(k): str(v) for k, v in env_raw.items()},
            self.tool_name,
            to_canonical=True,
        )
        return McpServer(
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

    def read_mcp_servers(self) -> dict[str, McpServer]:
        data, mtime_ms = self._load_opencode_json()
        mcp_root = data.get("mcp") or {}
        result: dict[str, McpServer] = {}
        if not isinstance(mcp_root, dict):
            return result

        # OpenCode supports nested `mcp.servers.<name>` as well as top-level `mcp.<name>`
        servers_block = mcp_root.get("servers")
        if isinstance(servers_block, dict):
            for name, entry in sorted(servers_block.items()):
                if isinstance(entry, dict):
                    parsed = self._parse_oc_mcp(name, entry, mtime_ms)
                    if parsed:
                        result[name] = parsed

        for name, entry in sorted(mcp_root.items()):
            if name in ("servers", "timeout") or name in result:
                continue
            if isinstance(entry, dict) and ("command" in entry or "url" in entry):
                parsed = self._parse_oc_mcp(name, entry, mtime_ms)
                if parsed:
                    result[name] = parsed
        return result

    def write_mcp_servers(self, servers: dict[str, McpServer]) -> tuple[bool, list[str]]:
        data, _ = self._load_opencode_json()
        mcp_root = dict(data.get("mcp") or {})
        out_servers: dict[str, dict[str, Any]] = {}
        for name, srv in sorted(servers.items()):
            if not srv.portable:
                continue
            if srv.transport == "stdio":
                if not srv.command:
                    continue
                entry: dict[str, Any] = {
                    "type": "local",
                    "command": [srv.command, *srv.args],
                    "enabled": not srv.disabled,
                }
                if srv.env:
                    entry["environment"] = translate_env_vars_for_tool(dict(srv.env), self.tool_name)
                out_servers[name] = entry
            else:
                if not srv.url:
                    continue
                entry = {
                    "type": "remote",
                    "url": srv.url,
                    "enabled": not srv.disabled,
                }
                if srv.headers:
                    entry["headers"] = translate_env_vars_for_tool(dict(srv.headers), self.tool_name)
                out_servers[name] = entry

        mcp_root["servers"] = out_servers
        data["mcp"] = mcp_root
        return self._save_opencode_json(data, mcp_only=True), []

    # --- 4. Instructions ---

    def read_global_instructions(self) -> InstructionDoc | None:
        if not self.agents_md.exists():
            return None
        return InstructionDoc.from_markdown(
            raw_markdown=self.agents_md.read_text(encoding="utf-8-sig", errors="replace"),
            scope="global",
            source_tool=self.tool_name,
            updated_at_ms=int(self.agents_md.stat().st_mtime * 1000),
            source_files=[str(self.agents_md)],
        )

    def write_global_instructions(self, doc: InstructionDoc) -> tuple[bool, list[str]]:
        return atomic_write_text(self.agents_md, doc.raw_text), []

    def read_project_instructions(self, project_path: Path) -> InstructionDoc | None:
        p = project_path / "AGENTS.md"
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
        return atomic_write_text(project_path / "AGENTS.md", doc.raw_text), []

    # --- 5. Projects ---

    def read_projects(self) -> dict[str, ProjectItem]:
        projects: dict[str, ProjectItem] = {}
        if not self.opencode_db.exists():
            return projects
        conn = sqlite_safe_connect(self.opencode_db, readonly=True)
        conn.row_factory = sqlite3.Row
        try:
            tables = {
                r[0]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            if "project" not in tables:
                return projects
            for r in conn.execute("SELECT id, worktree, name, time_created, time_updated FROM project").fetchall():
                wt = r["worktree"]
                if not wt or wt == "/" or r["id"] == "global":
                    continue
                key = path_dedup_key(wt)
                if not key:
                    continue
                if key not in projects:
                    projects[key] = ProjectItem.from_path(
                        raw_path=wt,
                        name=r["name"] or None,
                        native_ids_by_tool={self.tool_name: [r["id"]]},
                        created_at_ms=int(r["time_created"] or 0),
                        updated_at_ms=int(r["time_updated"] or 0),
                    )
                else:
                    ids = projects[key].native_ids_by_tool.setdefault(self.tool_name, [])
                    if r["id"] not in ids:
                        ids.append(r["id"])
        finally:
            conn.close()
        return projects

    def write_projects(
        self, projects: dict[str, ProjectItem]
    ) -> tuple[bool, dict[str, ProjectItem]]:
        changed = False
        now = int(time.time() * 1000)
        updated: dict[str, ProjectItem] = {}
        conn = self._ensure_db()
        try:
            existing = {
                path_dedup_key(r["worktree"]): dict(r)
                for r in conn.execute("SELECT id, worktree, name FROM project").fetchall()
                if r["worktree"]
            }
            for key, item in sorted(projects.items()):
                copy_item = item.model_copy(deep=True)
                ex = existing.get(key)
                if ex:
                    pid = ex["id"]
                    if ex["name"] != copy_item.name:
                        conn.execute(
                            "UPDATE project SET name = ?, time_updated = ? WHERE id = ?",
                            (copy_item.name, copy_item.updated_at_ms or now, pid),
                        )
                        changed = True
                else:
                    pid = copy_item.primary_id(self.tool_name) or hashlib.sha1(key.encode("utf-8")).hexdigest()
                    conn.execute(
                        "INSERT OR IGNORE INTO project(id, worktree, name, time_created, time_updated) "
                        "VALUES(?, ?, ?, ?, ?)",
                        (
                            pid,
                            copy_item.display_path,
                            copy_item.name,
                            copy_item.created_at_ms or now,
                            copy_item.updated_at_ms or now,
                        ),
                    )
                    changed = True
                copy_item.native_ids_by_tool[self.tool_name] = [pid]
                updated[key] = copy_item
            if changed:
                conn.commit()
        finally:
            conn.close()
        return changed, updated

    # --- 6. Chats ---

    def _extract_oc_message(self, mtype: str, data_str: str, created_ms: int) -> ChatMessage | None:
        try:
            obj = json.loads(data_str)
        except Exception:
            return None
        if mtype == "user":
            txt = str(obj.get("text") or (obj.get("metadata") or {}).get("displayText") or "").strip()
            return ChatMessage(role="user", text=txt, timestamp_ms=created_ms) if txt else None
        if mtype == "assistant":
            parts = []
            for block in obj.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                    parts.append(str(block["text"]))
            txt = "\n".join(parts).strip()
            return ChatMessage(role="assistant", text=txt, timestamp_ms=created_ms) if txt else None
        return None

    def read_chats(
        self,
        projects_by_key: dict[str, ProjectItem],
        include_subagents: bool = False,
    ) -> list[ChatSession]:
        if not self.opencode_db.exists():
            return []
        conn = sqlite_safe_connect(self.opencode_db, readonly=True)
        conn.row_factory = sqlite3.Row
        try:
            tables = {
                r[0]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            if not {"session_v2", "session_message"}.issubset(tables):
                return []
            sess_rows = conn.execute("SELECT * FROM session_v2 ORDER BY time_updated DESC").fetchall()
            msg_rows = conn.execute(
                "SELECT session_id, type, time_created, data FROM session_message "
                "WHERE type IN ('user', 'assistant') ORDER BY session_id, seq ASC"
            ).fetchall()
        finally:
            conn.close()

        msgs_by_sid: dict[str, list[ChatMessage]] = {}
        for mr in msg_rows:
            msg = self._extract_oc_message(mr["type"], mr["data"], int(mr["time_created"] or 0))
            if msg:
                msgs_by_sid.setdefault(mr["session_id"], []).append(msg)

        sessions: list[ChatSession] = []
        for sr in sess_rows:
            is_subagent = bool(sr["parent_id"])
            if is_subagent and not include_subagents:
                continue
            sid = sr["id"]
            messages = msgs_by_sid.get(sid) or []
            raw_dir = sr["directory"] or ""
            is_global_chat = sr["project_id"] == "global"
            proj_key = None if is_global_chat else (path_dedup_key(raw_dir) if raw_dir else None)
            if proj_key and proj_key not in projects_by_key:
                proj_path = normalize_path(raw_dir)
            elif proj_key:
                proj_path = projects_by_key[proj_key].display_path
            else:
                proj_path = None
            title = (sr["title"] or "").strip()
            if not title and messages:
                title = messages[0].text.splitlines()[0][:80]
            sessions.append(
                ChatSession(
                    tool=self.tool_name,
                    native_id=sid,
                    title=title,
                    project_key=proj_key,
                    project_path=proj_path,
                    native_project_id=sr["project_id"],
                    created_at_ms=int(sr["time_created"] or 0),
                    updated_at_ms=int(sr["time_updated"] or 0),
                    is_subagent=is_subagent,
                    parent_id=sr["parent_id"],
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
        sid = target_native_id or f"ses_{uuid.uuid4().hex[:24]}"
        created_ms = chat.created_at_ms or int(time.time() * 1000)
        updated_ms = chat.updated_at_ms or created_ms
        project = projects_by_key.get(chat.project_key) if chat.project_key else None
        pid = (project.primary_id(self.tool_name) if project else None) or "global"
        directory = project.display_path if project else normalize_path(str(self.root_path))
        slug = re.sub(r"[^a-z0-9]+", "-", (chat.title or "synced-chat").lower()).strip("-")[:32] or "synced-chat"

        conn = self._ensure_db()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO project(id, worktree, name, time_created, time_updated) "
                "VALUES(?, ?, ?, ?, ?)",
                (pid, directory, project.name if project else "global", created_ms, updated_ms),
            )
            conn.execute(
                """
                INSERT INTO session_v2(
                    id, project_id, slug, directory, path, title, version, time_created, time_updated
                ) VALUES(?, ?, ?, ?, '', ?, '2.1.4', ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    project_id=excluded.project_id,
                    directory=excluded.directory,
                    title=excluded.title,
                    time_updated=excluded.time_updated
                """,
                (sid, pid, slug, directory, chat.title or "Synced Chat", created_ms, updated_ms),
            )
            conn.execute("DELETE FROM session_message WHERE session_id = ?", (sid,))
            for seq, msg in enumerate(chat.messages, start=1):
                if msg.role not in ("user", "assistant") or not msg.text.strip():
                    continue
                mid = f"msg_{uuid.uuid5(uuid.NAMESPACE_URL, f'oc-msg:{sid}:{seq}').hex[:24]}"
                mts = msg.timestamp_ms or (created_ms + seq * 1000)
                if msg.role == "user":
                    payload = {
                        "metadata": {"displayText": msg.text, "comments": [], "attachments": []},
                        "time": {"created": mts},
                        "text": msg.text,
                        "files": [],
                        "agents": [],
                    }
                else:
                    payload = {
                        "time": {"created": mts, "completed": mts},
                        "agent": "build",
                        "content": [{"type": "text", "text": msg.text}],
                        "finish": "stop",
                    }
                conn.execute(
                    """
                    INSERT INTO session_message(id, session_id, type, seq, time_created, time_updated, data)
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (mid, sid, msg.role, seq, mts, mts, json.dumps(payload, ensure_ascii=False)),
                )
            conn.commit()
        finally:
            conn.close()
        return sid, True

    # --- Doctor & Repair ---

    def doctor(self) -> list[DoctorIssue]:
        issues: list[DoctorIssue] = []
        if self.opencode_json.exists():
            raw = self.opencode_json.read_bytes()
            if raw.startswith(b"\xef\xbb\xbf"):
                issues.append(
                    DoctorIssue(
                        tool=self.tool_name,
                        severity="warning",
                        code="OPENCODE_JSON_BOM",
                        path=str(self.opencode_json),
                        message="opencode.json starts with a UTF-8 BOM (some strict JSON parsers reject BOM)",
                        fixable=True,
                    )
                )
            try:
                loads_jsonc(raw.decode("utf-8-sig"))
            except Exception as exc:
                issues.append(
                    DoctorIssue(
                        tool=self.tool_name,
                        severity="error",
                        code="OPENCODE_INVALID_JSON",
                        path=str(self.opencode_json),
                        message=f"Invalid JSON in opencode.json: {exc}",
                    )
                )
        if self.opencode_db.exists():
            try:
                conn = sqlite_safe_connect(self.opencode_db, readonly=True)
                res = conn.execute("PRAGMA quick_check").fetchone()
                conn.close()
                if res and res[0] != "ok":
                    issues.append(
                        DoctorIssue(
                            tool=self.tool_name,
                            severity="error",
                            code="OPENCODE_SQLITE_CORRUPT",
                            path=str(self.opencode_db),
                            message=f"SQLite quick_check failed: {res[0]}",
                        )
                    )
            except Exception as exc:
                issues.append(
                    DoctorIssue(
                        tool=self.tool_name,
                        severity="error",
                        code="OPENCODE_SQLITE_ERROR",
                        path=str(self.opencode_db),
                        message=f"Unable to read opencode.db: {exc}",
                    )
                )
        return issues

    def repair(self, quarantine_dir: Path, dry_run: bool = False) -> RepairResult:
        res = RepairResult(tool=self.tool_name)
        if self.opencode_json.exists():
            raw = self.opencode_json.read_bytes()
            if raw.startswith(b"\xef\xbb\xbf"):
                if dry_run:
                    res.fixed_count += 1
                    res.details.append("Would strip UTF-8 BOM from opencode.json")
                else:
                    parsed = json.loads(raw.decode("utf-8-sig"))
                    atomic_write_json(self.opencode_json, parsed)
                    res.fixed_count += 1
                    res.details.append("Stripped UTF-8 BOM from opencode.json")
        return res
