"""DeepSeek Harness adapter (`~/.dsh`) verified against local desktop storage."""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
import zstandard

from agentsync.adapters.base import ToolAdapter, ToolCapabilities
from agentsync.adapters.registry import register_adapter
from agentsync.config import ToolConfig
from agentsync.models.canonical import (
    ChatMessage,
    ChatSession,
    DoctorIssue,
    InstructionDoc,
    ProjectItem,
)
from agentsync.utils.atomic_io import atomic_write_json, atomic_write_text
from agentsync.utils.paths import normalize_path, path_dedup_key, to_native_path


def _iso_utc_ms(ts_ms: int) -> str:
    dt = datetime.fromtimestamp(max(0, ts_ms) / 1000, tz=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _parse_iso_ms(val: Any) -> int:
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str) and val.strip():
        try:
            return int(datetime.fromisoformat(val.replace("Z", "+00:00")).timestamp() * 1000)
        except Exception:
            return 0
    return 0


@register_adapter
class DeepSeekHarnessAdapter(ToolAdapter):
    """Adapter for DeepSeek Harness (`~/.dsh`).

    Supports full read/write for workspaces (`storages/workspace.json`) and project `AGENTS.md`,
    plus read-only inspection and validation of zstd-compressed v4 session logs.
    Chat writes are explicitly disabled (`chat_write=False`) until a multi-turn native v4
    message schema is observed, adhering to strict zero-guessing schema discipline.
    """

    tool_name = "deepseek-harness"
    display_name = "DeepSeek Harness"
    client_kind = "desktop"
    capabilities = ToolCapabilities(
        skills=False,
        mcp=False,
        plugins=False,
        instructions=True,
        projects=True,
        chats=True,
        chat_write=False,
        plugin_write=False,
    )

    def __init__(self, tool_config: ToolConfig) -> None:
        super().__init__(tool_config)

    @property
    def workspace_json(self) -> Path:
        return self.root_path / "storages" / "workspace.json"

    @property
    def sessions_dir(self) -> Path:
        return self.root_path / "sessions"

    @property
    def projcache_sessions_dir(self) -> Path:
        return self.root_path / "storages" / "session_projcache" / "sessions"

    @property
    def profile_patch_yml(self) -> Path:
        return self.root_path / "profiles" / "desktop" / "cordis.patch.yml"

    def backup_targets(
        self,
    ) -> tuple[list[tuple[Path, Literal["file", "sqlite"]]], list[Path]]:
        return ([(self.workspace_json, "file")], [])

    def _load_workspace_json(self) -> dict[str, Any]:
        if not self.workspace_json.exists():
            return {
                "unit": {"name": "workspace", "version": 2},
                "global": {
                    "initialized": True,
                    "workspaceIds": [],
                    "archivedSessionIds": [],
                    "pinnedSessionIds": [],
                    "defaultWorkspaceId": None,
                },
                "tables": {"workspaces": {}},
            }
        txt = self.workspace_json.read_text(encoding="utf-8-sig").strip()
        return json.loads(txt) if txt else {"unit": {"name": "workspace", "version": 2}, "global": {"workspaceIds": []}, "tables": {"workspaces": {}}}

    # --- Instructions (project-level AGENTS.md) ---

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

    # --- Projects / Workspaces (`storages/workspace.json`) ---

    def read_projects(self) -> dict[str, ProjectItem]:
        projects: dict[str, ProjectItem] = {}
        if not self.workspace_json.exists():
            return projects
        data = self._load_workspace_json()
        workspaces = (data.get("tables") or {}).get("workspaces") or {}
        for wid, wdata in workspaces.items():
            if not isinstance(wdata, dict) or not wdata.get("path"):
                continue
            raw_p = str(wdata["path"])
            key = path_dedup_key(raw_p)
            if not key:
                continue
            if key not in projects:
                projects[key] = ProjectItem.from_path(
                    raw_path=raw_p,
                    name=wdata.get("title") or None,
                    native_ids_by_tool={self.tool_name: [str(wid)]},
                    created_at_ms=_parse_iso_ms(wdata.get("createdAt")),
                    updated_at_ms=_parse_iso_ms(wdata.get("updatedAt")),
                )
            else:
                ids = projects[key].native_ids_by_tool.setdefault(self.tool_name, [])
                if str(wid) not in ids:
                    ids.append(str(wid))
        return projects

    def write_projects(
        self, projects: dict[str, ProjectItem]
    ) -> tuple[bool, dict[str, ProjectItem]]:
        data = self._load_workspace_json()
        tables = dict(data.get("tables") or {})
        workspaces = dict(tables.get("workspaces") or {})
        glob = dict(data.get("global") or {})
        ws_ids = list(glob.get("workspaceIds") or [])
        now = int(time.time() * 1000)

        existing_by_key: dict[str, tuple[str, dict[str, Any]]] = {}
        for wid, wdata in workspaces.items():
            if isinstance(wdata, dict) and wdata.get("path"):
                k = path_dedup_key(str(wdata["path"]))
                if k and k not in existing_by_key:
                    existing_by_key[k] = (wid, wdata)

        updated: dict[str, ProjectItem] = {}
        for key, item in sorted(projects.items()):
            copy_item = item.model_copy(deep=True)
            if key in existing_by_key:
                wid, prev = existing_by_key[key]
                workspaces[wid] = {
                    **prev,
                    "path": to_native_path(copy_item.display_path),
                    "title": copy_item.name,
                    "updatedAt": _iso_utc_ms(copy_item.updated_at_ms or now),
                }
            else:
                wid = copy_item.primary_id(self.tool_name) or str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"dsh-ws:{key}")
                )
                workspaces[wid] = {
                    "path": to_native_path(copy_item.display_path),
                    "title": copy_item.name,
                    "sessionIds": [],
                    "createdAt": _iso_utc_ms(copy_item.created_at_ms or now),
                    "updatedAt": _iso_utc_ms(copy_item.updated_at_ms or now),
                }
                if wid not in ws_ids:
                    ws_ids.append(wid)
            copy_item.native_ids_by_tool[self.tool_name] = [wid]
            updated[key] = copy_item

        tables["workspaces"] = workspaces
        glob["workspaceIds"] = ws_ids
        data["tables"] = tables
        data["global"] = glob
        changed = atomic_write_json(self.workspace_json, data)
        return changed, updated

    # --- Chats (Read-Only zstd v4 session inspection) ---

    def _decompress_v4_session(self, zstd_path: Path) -> list[dict[str, Any]]:
        dctx = zstandard.ZstdDecompressor()
        with zstd_path.open("rb") as fh:
            with dctx.stream_reader(fh) as reader:
                text = reader.read().decode("utf-8", errors="replace")
        events: list[dict[str, Any]] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            events.append(json.loads(line))
        return events

    def read_chats(
        self,
        projects_by_key: dict[str, ProjectItem],
        include_subagents: bool = False,
    ) -> list[ChatSession]:
        sessions: list[ChatSession] = []
        if not self.sessions_dir.exists():
            return sessions
        for sf in sorted(self.sessions_dir.rglob("session.v4.jsonl.zstd")):
            try:
                events = self._decompress_v4_session(sf)
            except Exception:
                continue
            if not events:
                continue
            header = events[0] if events[0].get("type") == "session" else {}
            sid = str(header.get("id") or sf.parent.name)
            cwd = str(header.get("cwd") or "")
            created_ms = int(header.get("createdAt") or int(sf.stat().st_mtime * 1000))
            updated_ms = created_ms
            if int(header.get("delegationDepth") or 0) > 0 and not include_subagents:
                continue

            # Extract any user/assistant messages present in event stream
            messages: list[ChatMessage] = []
            for ev in events[1:]:
                et = str(ev.get("type") or "").lower()
                ev_time = int(ev.get("time") or created_ms)
                updated_ms = max(updated_ms, ev_time)
                data = ev.get("data") or {}
                if isinstance(data, dict):
                    role = data.get("role")
                    text = data.get("text") or data.get("content")
                    if role in ("user", "assistant") and isinstance(text, str) and text.strip():
                        messages.append(ChatMessage(role=role, text=text.strip(), timestamp_ms=ev_time))
                    elif "user" in et and isinstance(text, str) and text.strip():
                        messages.append(ChatMessage(role="user", text=text.strip(), timestamp_ms=ev_time))
                    elif "assistant" in et and isinstance(text, str) and text.strip():
                        messages.append(ChatMessage(role="assistant", text=text.strip(), timestamp_ms=ev_time))

            # Skip blank starter sessions that have no messages and no title
            title = ""
            cache_file = self.projcache_sessions_dir / f"{sid}.json"
            if cache_file.exists():
                try:
                    cdata = json.loads(cache_file.read_text(encoding="utf-8-sig"))
                    rows = (cdata.get("record") or {}).get("rows") or {}
                    tval = (rows.get("title") or {}).get("val")
                    if isinstance(tval, str) and tval.strip():
                        title = tval.strip()
                except Exception:
                    pass
            if not messages and not title:
                continue

            proj_key = path_dedup_key(cwd) if cwd else None
            proj_path = (
                projects_by_key[proj_key].display_path
                if (proj_key and proj_key in projects_by_key)
                else (normalize_path(cwd) if cwd else None)
            )
            sessions.append(
                ChatSession(
                    tool=self.tool_name,
                    native_id=sid,
                    title=title or (messages[0].text.splitlines()[0][:80] if messages else sid),
                    project_key=proj_key,
                    project_path=proj_path,
                    created_at_ms=created_ms,
                    updated_at_ms=updated_ms,
                    messages=messages,
                ).compute_hashes()
            )
        return sessions

    # --- Doctor ---

    def doctor(self) -> list[DoctorIssue]:
        issues: list[DoctorIssue] = []
        if self.workspace_json.exists():
            try:
                json.loads(self.workspace_json.read_text(encoding="utf-8-sig"))
            except Exception as exc:
                issues.append(
                    DoctorIssue(
                        tool=self.tool_name,
                        severity="error",
                        code="DSH_INVALID_WORKSPACE_JSON",
                        path=str(self.workspace_json),
                        message=f"Invalid JSON in workspace.json: {exc}",
                    )
                )
        if self.profile_patch_yml.exists():
            try:
                yaml.safe_load(self.profile_patch_yml.read_text(encoding="utf-8-sig"))
            except Exception as exc:
                issues.append(
                    DoctorIssue(
                        tool=self.tool_name,
                        severity="warning",
                        code="DSH_INVALID_PROFILE_YAML",
                        path=str(self.profile_patch_yml),
                        message=f"Could not parse cordis.patch.yml with standard YAML loader: {exc}",
                    )
                )
        if self.sessions_dir.exists():
            for sf in sorted(self.sessions_dir.rglob("session.v4.jsonl.zstd")):
                try:
                    events = self._decompress_v4_session(sf)
                    if not events or events[0].get("type") != "session":
                        issues.append(
                            DoctorIssue(
                                tool=self.tool_name,
                                severity="error",
                                code="DSH_BROKEN_SESSION",
                                path=str(sf),
                                message="Decompressed session.v4.jsonl.zstd missing 'session' header on line 1",
                            )
                        )
                except Exception as exc:
                    issues.append(
                        DoctorIssue(
                            tool=self.tool_name,
                            severity="error",
                            code="DSH_ZSTD_CORRUPT",
                            path=str(sf),
                            message=f"Failed to decompress/parse session.v4.jsonl.zstd: {exc}",
                        )
                    )
        return issues
