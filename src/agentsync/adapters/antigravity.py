"""Google Antigravity 2.0 adapter with full hub summary proto registration, doctor, and repair."""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

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
    McpVariable,
    PluginItem,
    ProjectItem,
    RepairResult,
    SkillItem,
    ensure_skill_frontmatter,
    parse_skill_md,
)
from agentsync.utils.atomic_io import (
    atomic_write_json,
    atomic_write_text,
    remove_tree_if_exists,
    sqlite_safe_connect,
    sync_directory_files,
)
from agentsync.utils.paths import (
    normalize_path,
    path_dedup_key,
    path_to_file_uri,
    to_native_path,
)
from agentsync.utils.proto_wire import (
    RUN_STATUS_IDLE,
    decode_step_text,
    decode_trajectory_metadata,
    encode_planner_response_step,
    encode_raw_summary,
    encode_trajectory_metadata,
    encode_user_input_step,
    first_varint,
    parse_wire,
    read_agyhub_summaries_pb,
    upsert_agyhub_summaries_pb,
)

AGY_RULE_MAX_BYTES = 24_000
_IGNORED_DIR_NAMES = {".git", "__pycache__", "node_modules", ".DS_Store"}

_SUMMARIES_DDL = """
CREATE TABLE IF NOT EXISTS conversation_summaries (
    conversation_id text PRIMARY KEY,
    title text NOT NULL DEFAULT "",
    preview text NOT NULL DEFAULT "",
    step_count integer NOT NULL DEFAULT 0,
    last_modified_time datetime NOT NULL,
    workspace_uris text NOT NULL,
    status text NOT NULL DEFAULT "",
    source text NOT NULL DEFAULT "",
    project_id text NOT NULL DEFAULT "",
    agent_name text NOT NULL DEFAULT "",
    parent_conversation_id text NOT NULL DEFAULT "",
    nesting_depth integer NOT NULL DEFAULT 0,
    battle_id text NOT NULL DEFAULT "",
    winning_conversation_id text NOT NULL DEFAULT "",
    not_fully_idle numeric NOT NULL DEFAULT false,
    killed numeric NOT NULL DEFAULT false,
    last_user_input_time datetime NOT NULL,
    last_user_input_step_index integer NOT NULL DEFAULT -1,
    app_data_dir text NOT NULL DEFAULT "",
    raw_summary blob,
    group_id text NOT NULL DEFAULT ""
);
CREATE INDEX IF NOT EXISTS idx_conversation_summaries_last_user_input_time
    ON conversation_summaries(last_user_input_time);
CREATE INDEX IF NOT EXISTS idx_conversation_summaries_last_modified_time
    ON conversation_summaries(last_modified_time);
CREATE TABLE IF NOT EXISTS search_schema_version (
    version INTEGER PRIMARY KEY
);
CREATE VIRTUAL TABLE IF NOT EXISTS conversations_fts USING fts5(
    conversation_id UNINDEXED,
    chunk_index UNINDEXED,
    chunk_first_step UNINDEXED,
    chunk_last_step UNINDEXED,
    last_modified_time UNINDEXED,
    step_count UNINDEXED,
    last_step_created_at UNINDEXED,
    archived UNINDEXED,
    user_step_byte_offsets UNINDEXED,
    agent_step_byte_offsets UNINDEXED,
    title,
    workspace,
    project,
    user_body,
    agent_body,
    tokenize = 'unicode61 remove_diacritics 2',
    prefix = '1 2 3'
);
CREATE TABLE IF NOT EXISTS conversation_parents (
    conversation_id TEXT PRIMARY KEY,
    parent_id TEXT NOT NULL
) WITHOUT ROWID;
"""

_CONV_DB_DDL = """
CREATE TABLE IF NOT EXISTS trajectory_meta (
    trajectory_id text PRIMARY KEY,
    cascade_id text,
    trajectory_type integer,
    source integer
);
CREATE TABLE IF NOT EXISTS steps (
    idx integer PRIMARY KEY,
    step_type integer NOT NULL DEFAULT 0,
    status integer NOT NULL DEFAULT 0,
    has_subtrajectory numeric NOT NULL DEFAULT false,
    metadata blob,
    error_details blob,
    permissions blob,
    task_details blob,
    render_info blob,
    step_payload blob,
    step_format integer NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS gen_metadata (
    idx integer PRIMARY KEY,
    data blob,
    size integer NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS executor_metadata (
    idx integer PRIMARY KEY,
    data blob
);
CREATE TABLE IF NOT EXISTS parent_references (
    idx integer PRIMARY KEY,
    data blob
);
CREATE TABLE IF NOT EXISTS trajectory_metadata_blob (
    id text DEFAULT "main" PRIMARY KEY,
    data blob
);
CREATE TABLE IF NOT EXISTS battle_mode_infos (
    idx integer PRIMARY KEY,
    data blob
);
"""


class _AntigravityMcpFileSchema(BaseModel):
    mcpServers: dict[str, dict[str, Any]] = Field(default_factory=dict)


class _AntigravityPluginManifestSchema(BaseModel):
    name: str
    displayName: str | None = None
    version: str | None = None
    description: str | None = None


def _read_dir_files(root: Path) -> tuple[dict[str, bytes], int]:
    files: dict[str, bytes] = {}
    max_mtime_ms = 0
    for item in sorted(root.rglob("*")):
        if any(part in _IGNORED_DIR_NAMES for part in item.relative_to(root).parts):
            continue
        if item.is_file():
            rel = item.relative_to(root).as_posix()
            files[rel] = item.read_bytes()
            max_mtime_ms = max(max_mtime_ms, int(item.stat().st_mtime * 1000))
    return files, max_mtime_ms


def _parse_agy_mcp_entry(name: str, entry: dict[str, Any], updated_at_ms: int) -> McpServer:
    url = entry.get("url") or entry.get("serverUrl")
    command = entry.get("command")
    transport: McpTransport
    if url:
        raw_type = str(entry.get("type") or "").lower()
        transport = "sse" if (raw_type == "sse" or str(url).rstrip("/").lower().endswith("/sse")) else "http"
    else:
        transport = "stdio"

    variables = [
        McpVariable(
            name=str(v.get("name", "")),
            title=str(v.get("title", "")),
            description=str(v.get("description", "")),
        )
        for v in (entry.get("variables") or [])
        if isinstance(v, dict) and v.get("name")
    ]
    return McpServer(
        name=name,
        transport=transport,
        command=str(command) if command else None,
        args=[str(a) for a in (entry.get("args") or [])],
        env={str(k): str(v) for k, v in (entry.get("env") or {}).items()},
        cwd=str(entry["cwd"]) if entry.get("cwd") else None,
        url=str(url) if url else None,
        headers={str(k): str(v) for k, v in (entry.get("headers") or {}).items()},
        disabled=bool(entry.get("disabled", False)),
        variables=variables,
        portable=True,
        source_tool="antigravity",
        updated_at_ms=updated_at_ms,
    ).compute_hash()


def _serialize_agy_mcp_entry(server: McpServer) -> tuple[dict[str, Any] | None, str | None]:
    if not server.portable:
        return None, f"MCP '{server.name}' skipped: {server.skip_reason or 'non_portable'}"
    if server.transport == "sse":
        return (
            None,
            f"MCP '{server.name}' skipped for Antigravity: legacy SSE transport is unsupported "
            "(use Streamable HTTP or an mcp-remote stdio bridge).",
        )

    out: dict[str, Any] = {}
    if server.transport == "stdio":
        if not server.command:
            return None, f"MCP '{server.name}' skipped: missing command for stdio transport"
        out["command"] = server.command
        if server.args:
            out["args"] = list(server.args)
        if server.env:
            out["env"] = dict(server.env)
        if server.cwd:
            out["cwd"] = to_native_path(server.cwd)
    else:
        if not server.url:
            return None, f"MCP '{server.name}' skipped: missing url for HTTP transport"
        out["url"] = server.url
        if server.headers:
            out["headers"] = dict(server.headers)
        if server.variables:
            out["variables"] = [v.model_dump() for v in server.variables]

    if server.disabled:
        out["disabled"] = True
    return out, None


def _parse_agy_datetime_ms(dt_str: str) -> int:
    if not dt_str:
        return 0
    s = dt_str.strip().replace(" ", "T")
    if "." in s:
        head, rest = s.split(".", 1)
        tz_part = ""
        for sep in ("+", "-", "Z"):
            if sep in rest:
                idx = rest.index(sep)
                tz_part = rest[idx:]
                rest = rest[:idx]
                break
        rest = rest[:6].ljust(6, "0")
        s = f"{head}.{rest}{tz_part}"
    s = s.replace("Z", "+00:00")
    try:
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except Exception:
        return 0


def _format_agy_datetime(ts_ms: int) -> str:
    dt = datetime.fromtimestamp(max(0, ts_ms) / 1000, tz=UTC)
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f+00:00")


def _format_iso_utc(ts_ms: int) -> str:
    return datetime.fromtimestamp(max(0, ts_ms) / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@register_adapter
class AntigravityAdapter(ToolAdapter):
    """Adapter for Google Antigravity 2.0 (`~/.gemini` + `%APPDATA%/Antigravity`)."""

    tool_name = "antigravity"
    display_name = "Google Antigravity"
    client_kind = "desktop"
    capabilities = ToolCapabilities(
        skills=True,
        mcp=True,
        plugins=True,
        instructions=True,
        projects=True,
        chats=True,
        chat_write=True,
        plugin_write=True,
    )

    def __init__(self, tool_config: ToolConfig) -> None:
        super().__init__(tool_config)
        self.appdata_dir = self.extra_paths.get("appdata") or (self.root_path / "appdata")

    # --- Paths ---
    @property
    def config_dir(self) -> Path:
        return self.root_path / "config"

    @property
    def config_json(self) -> Path:
        return self.config_dir / "config.json"

    @property
    def mcp_config_json(self) -> Path:
        return self.config_dir / "mcp_config.json"

    @property
    def skills_dir(self) -> Path:
        return self.config_dir / "skills"

    @property
    def plugins_dir(self) -> Path:
        return self.config_dir / "plugins"

    @property
    def rules_dir(self) -> Path:
        return self.config_dir / "rules"

    @property
    def global_rule_md(self) -> Path:
        return self.rules_dir / "global-instructions.md"

    @property
    def legacy_gemini_md(self) -> Path:
        return self.root_path / "GEMINI.md"

    @property
    def projects_dir(self) -> Path:
        return self.config_dir / "projects"

    @property
    def data_dir(self) -> Path:
        return self.root_path / "antigravity"

    @property
    def summaries_db(self) -> Path:
        return self.data_dir / "conversation_summaries.db"

    @property
    def hub_summaries_pb(self) -> Path:
        return self.data_dir / "agyhub_summaries_proto.pb"

    @property
    def conversations_dir(self) -> Path:
        return self.data_dir / "conversations"

    @property
    def brain_dir(self) -> Path:
        return self.data_dir / "brain"

    @property
    def annotations_dir(self) -> Path:
        return self.data_dir / "annotations"

    @property
    def app_storage_json(self) -> Path:
        return self.appdata_dir / "app_storage.json"

    # --- Backup & Watch ---

    def backup_targets(
        self,
    ) -> tuple[list[tuple[Path, Literal["file", "sqlite"]]], list[Path]]:
        files: list[tuple[Path, Literal["file", "sqlite"]]] = [
            (self.config_json, "file"),
            (self.mcp_config_json, "file"),
            (self.global_rule_md, "file"),
            (self.legacy_gemini_md, "file"),
            (self.hub_summaries_pb, "file"),
            (self.summaries_db, "sqlite"),
            (self.app_storage_json, "file"),
        ]
        dirs = [
            self.skills_dir,
            self.plugins_dir,
            self.rules_dir,
            self.projects_dir,
            self.conversations_dir,
            self.annotations_dir,
            self.brain_dir,
        ]
        return files, dirs

    def should_skip_backup_file(self, root_dir: Path, file_path: Path) -> bool:
        if super().should_skip_backup_file(root_dir, file_path):
            return True
        if root_dir == self.brain_dir:
            rel_parts = file_path.relative_to(root_dir).parts
            if ".system_generated" not in rel_parts or "logs" not in rel_parts:
                return True
        return False

    def is_sqlite_backup_file(self, root_dir: Path, file_path: Path) -> bool:
        return root_dir == self.conversations_dir and file_path.suffix == ".db"

    def watch_roots(self) -> list[Path]:
        return [p for p in (self.config_dir, self.data_dir) if p.exists()]

    # --- Config helpers ---

    def _load_config_json(self) -> dict[str, Any]:
        if not self.config_json.exists():
            return {}
        txt = self.config_json.read_text(encoding="utf-8-sig").strip()
        return json.loads(txt) if txt else {}

    def _save_config_json(self, data: dict[str, Any]) -> bool:
        return atomic_write_json(self.config_json, data)

    def _load_app_storage(self) -> dict[str, Any]:
        if not self.app_storage_json.exists():
            return {}
        txt = self.app_storage_json.read_text(encoding="utf-8-sig").strip()
        return json.loads(txt) if txt else {}

    def _save_app_storage(self, data: dict[str, Any]) -> bool:
        return atomic_write_json(self.app_storage_json, data)

    def _ensure_summaries_db(self) -> sqlite3.Connection:
        conn = sqlite_safe_connect(self.summaries_db, readonly=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(_SUMMARIES_DDL)
        conn.execute("INSERT OR IGNORE INTO search_schema_version(version) VALUES(3)")
        conn.commit()
        return conn

    # --- Bootstrap clear (preserves auth, userSettings, sidecars, builtin!) ---

    def clear_managed_for_bootstrap(self) -> dict[str, list[str]]:
        removed: dict[str, list[str]] = {"skills": [], "mcp": [], "plugins": []}

        if self.skills_dir.exists():
            for item in sorted(self.skills_dir.iterdir()):
                if item.name.startswith("."):
                    continue
                if remove_tree_if_exists(item):
                    removed["skills"].append(item.name)

        existing_mcps = self.read_mcp_servers()
        removed["mcp"] = sorted(existing_mcps.keys())
        atomic_write_json(
            self.mcp_config_json,
            {"mcpServers": {}},
            schema_model=_AntigravityMcpFileSchema,
        )

        if self.plugins_dir.exists():
            for item in sorted(self.plugins_dir.iterdir()):
                if item.name.startswith("."):
                    continue
                if remove_tree_if_exists(item):
                    removed["plugins"].append(item.name)

        cfg = self._load_config_json()
        if "plugins" in cfg:
            cfg["plugins"] = {}
            self._save_config_json(cfg)

        return removed

    # --- 1. Skills ---

    def read_skills(self) -> dict[str, SkillItem]:
        result: dict[str, SkillItem] = {}
        if not self.skills_dir.exists():
            return result
        for sub in sorted(self.skills_dir.iterdir()):
            if not sub.is_dir() or sub.name.startswith("."):
                continue
            if not (sub / "SKILL.md").exists():
                continue
            files, mtime_ms = _read_dir_files(sub)
            md_text = files.get("SKILL.md", b"").decode("utf-8", errors="replace")
            fm, _, had_fm = parse_skill_md(md_text, sub.name)
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
            raw_md = out_files["SKILL.md"].decode("utf-8", errors="replace")
            out_files["SKILL.md"] = ensure_skill_frontmatter(raw_md, skill.name).encode("utf-8")
        return sync_directory_files(self.skills_dir / skill.name, out_files)

    def delete_skill(self, skill_name: str) -> bool:
        if skill_name.startswith("."):
            return False
        return remove_tree_if_exists(self.skills_dir / skill_name)

    # --- 2. MCP Servers ---

    def read_mcp_servers(self) -> dict[str, McpServer]:
        if not self.mcp_config_json.exists():
            return {}
        txt = self.mcp_config_json.read_text(encoding="utf-8-sig", errors="replace").strip()
        if not txt:
            return {}
        mtime_ms = int(self.mcp_config_json.stat().st_mtime * 1000)
        data = json.loads(txt)
        return {
            name: _parse_agy_mcp_entry(name, entry, mtime_ms)
            for name, entry in sorted((data.get("mcpServers") or {}).items())
            if isinstance(entry, dict)
        }

    def write_mcp_servers(self, servers: dict[str, McpServer]) -> tuple[bool, list[str]]:
        out_map: dict[str, dict[str, Any]] = {}
        skipped: list[str] = []
        for name, srv in sorted(servers.items()):
            serialized, skip_msg = _serialize_agy_mcp_entry(srv)
            if serialized is None:
                if skip_msg:
                    skipped.append(skip_msg)
                continue
            out_map[name] = serialized

        changed = atomic_write_json(
            self.mcp_config_json,
            {"mcpServers": out_map},
            schema_model=_AntigravityMcpFileSchema,
        )
        return changed, skipped

    # --- 3. Plugins ---

    def read_plugins(self) -> dict[str, PluginItem]:
        result: dict[str, PluginItem] = {}
        if not self.plugins_dir.exists():
            return result

        cfg = self._load_config_json()
        plugins_state = cfg.get("plugins") or {}

        for sub in sorted(self.plugins_dir.iterdir()):
            if not sub.is_dir() or sub.name.startswith("."):
                continue
            manifest_path = sub / "plugin.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            except Exception:
                manifest = {}

            pskills: dict[str, SkillItem] = {}
            sdir = sub / "skills"
            if sdir.exists() and sdir.is_dir():
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
            mcp_p = sub / "mcp_config.json"
            if mcp_p.exists():
                try:
                    mdata = json.loads(mcp_p.read_text(encoding="utf-8-sig"))
                    m_ms = int(mcp_p.stat().st_mtime * 1000)
                    for sname, sentry in (mdata.get("mcpServers") or {}).items():
                        if isinstance(sentry, dict):
                            pmcps[sname] = _parse_agy_mcp_entry(sname, sentry, m_ms)
                except Exception:
                    pass

            prules: dict[str, str] = {}
            rdir = sub / "rules"
            if rdir.exists() and rdir.is_dir():
                for rf in sorted(rdir.glob("*.md")):
                    prules[rf.name] = rf.read_text(encoding="utf-8", errors="replace")

            p_state = plugins_state.get(sub.name)
            enabled = (
                bool(p_state.get("enabled", True))
                if isinstance(p_state, dict)
                else (not bool(manifest.get("disabled", False)))
            )
            mtime_ms = max(
                [
                    int(manifest_path.stat().st_mtime * 1000),
                    *(s.updated_at_ms for s in pskills.values()),
                ]
            )
            result[sub.name] = PluginItem(
                name=sub.name,
                display_name=str(manifest.get("displayName") or manifest.get("name") or sub.name),
                version=str(manifest.get("version") or "1.0.0"),
                description=str(manifest.get("description") or ""),
                enabled=enabled,
                skills=pskills,
                mcp_servers=pmcps,
                rules=prules,
                portable=True,
                source_tool=self.tool_name,
                updated_at_ms=mtime_ms,
            ).compute_hash()
        return result

    def write_plugin(self, plugin: PluginItem) -> tuple[bool, list[str]]:
        warnings: list[str] = []
        if not plugin.portable:
            return False, [f"Plugin '{plugin.name}' skipped: {plugin.skip_reason}"]

        target = self.plugins_dir / plugin.name
        files: dict[str, bytes] = {}
        manifest_obj = _AntigravityPluginManifestSchema(
            name=plugin.name,
            displayName=plugin.display_name or plugin.name,
            version=plugin.version or "1.0.0",
            description=plugin.description or f"Synced plugin {plugin.name}",
        )
        files["plugin.json"] = (
            json.dumps(manifest_obj.model_dump(exclude_none=True), indent=2, ensure_ascii=False)
            + "\n"
        ).encode("utf-8")

        mcp_out: dict[str, dict[str, Any]] = {}
        for sname, srv in sorted(plugin.mcp_servers.items()):
            ser, skip_msg = _serialize_agy_mcp_entry(srv)
            if ser is not None:
                mcp_out[sname] = ser
            elif skip_msg:
                warnings.append(skip_msg)
        if mcp_out:
            _AntigravityMcpFileSchema.model_validate({"mcpServers": mcp_out})
            files["mcp_config.json"] = (
                json.dumps({"mcpServers": mcp_out}, indent=2, ensure_ascii=False) + "\n"
            ).encode("utf-8")

        for sname, sitem in sorted(plugin.skills.items()):
            for rel, data in sitem.files.items():
                if rel.lower() == "skill.md":
                    data = ensure_skill_frontmatter(
                        data.decode("utf-8", errors="replace"), sname
                    ).encode("utf-8")
                files[f"skills/{sname}/{rel}"] = data

        for rname, rtext in sorted(plugin.rules.items()):
            if not rtext.lstrip().startswith("---"):
                rtext = f"---\ntrigger: always_on\n---\n\n{rtext.lstrip()}"
            files[f"rules/{rname}"] = rtext.encode("utf-8")

        dir_changed = sync_directory_files(target, files)
        cfg = self._load_config_json()
        plugins_cfg = dict(cfg.get("plugins") or {})
        plugins_cfg[plugin.name] = {"enabled": plugin.enabled}
        cfg["plugins"] = plugins_cfg
        cfg_changed = self._save_config_json(cfg)
        return (dir_changed or cfg_changed), warnings

    def delete_plugin(self, plugin_name: str) -> bool:
        removed = remove_tree_if_exists(self.plugins_dir / plugin_name)
        cfg = self._load_config_json()
        plugins_cfg = dict(cfg.get("plugins") or {})
        if plugin_name in plugins_cfg:
            del plugins_cfg[plugin_name]
            cfg["plugins"] = plugins_cfg
            self._save_config_json(cfg)
            removed = True
        return removed

    # --- 4. Instructions & Rules ---

    def read_global_instructions(self) -> InstructionDoc | None:
        sources: list[Path] = []
        if self.global_rule_md.exists():
            sources.append(self.global_rule_md)
        if self.rules_dir.exists():
            for rf in sorted(self.rules_dir.glob("*.md")):
                if rf != self.global_rule_md:
                    sources.append(rf)
        if self.legacy_gemini_md.exists():
            sources.append(self.legacy_gemini_md)
        if not sources:
            return None

        from agentsync.engine.merge import merge_markdown_texts

        texts = [p.read_text(encoding="utf-8-sig", errors="replace") for p in sources]
        combined = texts[0]
        for extra in texts[1:]:
            combined, _ = merge_markdown_texts(combined, extra, primary_wins=True)
        mtime_ms = max(int(p.stat().st_mtime * 1000) for p in sources)
        return InstructionDoc.from_markdown(
            raw_markdown=combined,
            scope="global",
            source_tool=self.tool_name,
            updated_at_ms=mtime_ms,
            source_files=[str(p) for p in sources],
        )

    def write_global_instructions(self, doc: InstructionDoc) -> tuple[bool, list[str]]:
        warnings: list[str] = []
        wrapped = f"---\ntrigger: always_on\n---\n\n{doc.raw_text.lstrip()}"
        size_bytes = len(wrapped.encode("utf-8"))
        if size_bytes > AGY_RULE_MAX_BYTES:
            warnings.append(
                f"Antigravity global rules ({size_bytes} bytes) exceed the 24 KB per-file limit."
            )
        return atomic_write_text(self.global_rule_md, wrapped), warnings

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

    def write_project_instructions(
        self, project_path: Path, doc: InstructionDoc
    ) -> tuple[bool, list[str]]:
        if not project_path.exists():
            return False, [f"Project directory not found: {project_path}"]
        warnings: list[str] = []
        size_bytes = len(doc.raw_text.encode("utf-8"))
        if size_bytes > AGY_RULE_MAX_BYTES:
            warnings.append(
                f"Antigravity project rule {project_path.name}/GEMINI.md ({size_bytes} bytes) exceeds 24 KB."
            )
        return atomic_write_text(project_path / "GEMINI.md", doc.raw_text), warnings

    # --- 5. Projects / Workspaces ---

    def _extract_project_uri(self, pdata: dict[str, Any]) -> tuple[str, bool, str | None] | None:
        resources = (pdata.get("projectResources") or {}).get("resources") or []
        for res in resources:
            if not isinstance(res, dict):
                continue
            if "gitFolder" in res and isinstance(res["gitFolder"], dict):
                uri = res["gitFolder"].get("folderUri")
                if uri:
                    return str(uri), True, res["gitFolder"].get("defaultBranch")
            if res.get("folderUri"):
                return str(res["folderUri"]), False, None
        return None

    def read_projects(self) -> dict[str, ProjectItem]:
        projects: dict[str, ProjectItem] = {}
        if not self.projects_dir.exists():
            return projects
        for pf in sorted(self.projects_dir.glob("*.json")):
            try:
                data = json.loads(pf.read_text(encoding="utf-8-sig"))
            except Exception:
                continue
            if "sidecar" in data:
                continue
            extracted = self._extract_project_uri(data)
            if not extracted:
                continue
            uri, is_git, default_branch = extracted
            key = path_dedup_key(uri)
            if not key:
                continue
            mtime_ms = int(pf.stat().st_mtime * 1000)
            pid = str(data.get("id") or pf.stem)
            if key not in projects:
                projects[key] = ProjectItem.from_path(
                    raw_path=uri,
                    name=data.get("name"),
                    native_ids_by_tool={self.tool_name: [pid]},
                    is_git=is_git,
                    default_branch=default_branch,
                    created_at_ms=mtime_ms,
                    updated_at_ms=mtime_ms,
                )
            else:
                ids = projects[key].native_ids_by_tool.setdefault(self.tool_name, [])
                if pid not in ids:
                    ids.append(pid)
        return projects

    def write_projects(
        self, projects: dict[str, ProjectItem]
    ) -> tuple[bool, dict[str, ProjectItem]]:
        changed = False
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        updated: dict[str, ProjectItem] = {}

        existing_by_key: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
        for pf in sorted(self.projects_dir.glob("*.json")):
            try:
                data = json.loads(pf.read_text(encoding="utf-8-sig"))
            except Exception:
                continue
            if "sidecar" in data:
                continue
            extracted = self._extract_project_uri(data)
            if not extracted:
                continue
            k = path_dedup_key(extracted[0])
            if k:
                existing_by_key.setdefault(k, []).append((pf, data))

        for key, item in sorted(projects.items()):
            copy_item = item.model_copy(deep=True)
            existing_list = existing_by_key.get(key) or []
            ids = copy_item.native_ids_by_tool.setdefault(self.tool_name, [])
            if not ids:
                if existing_list:
                    ids.append(str(existing_list[0][1].get("id") or existing_list[0][0].stem))
                else:
                    ids.append(str(uuid.uuid5(uuid.NAMESPACE_URL, f"agy-proj:{key}")))
            pid = ids[0]
            copy_item.native_ids_by_tool[self.tool_name] = [pid]

            for dup_path, dup_data in existing_list:
                dup_id = str(dup_data.get("id") or dup_path.stem)
                if dup_id != pid:
                    dup_path.unlink(missing_ok=True)
                    changed = True

            target_file = self.projects_dir / f"{pid}.json"
            existing_data: dict[str, Any] = {}
            if target_file.exists():
                try:
                    existing_data = json.loads(target_file.read_text(encoding="utf-8-sig"))
                except Exception:
                    existing_data = {}

            folder_uri = path_to_file_uri(copy_item.display_path, encode_drive_colon=True)
            is_git = copy_item.is_git or (Path(to_native_path(copy_item.display_path)) / ".git").exists()
            resource = (
                {"gitFolder": {"folderUri": folder_uri, "defaultBranch": copy_item.default_branch or "main"}}
                if is_git
                else {"folderUri": folder_uri}
            )
            payload = {
                "id": pid,
                "name": copy_item.name,
                "projectResources": {"resources": [resource]},
                "permissionGrants": existing_data.get("permissionGrants") or {"v2Migrated": True},
                "settings": existing_data.get("settings") or {},
                "isWorkspaceOnly": bool(existing_data.get("isWorkspaceOnly", False)),
            }
            if atomic_write_json(target_file, payload):
                changed = True
            updated[key] = copy_item

        if self.app_storage_json.exists():
            storage = self._load_app_storage()
            try:
                cur_order = json.loads(storage.get("projectsOrder") or "[]")
                if not isinstance(cur_order, list):
                    cur_order = []
            except Exception:
                cur_order = []
            valid_ids = {
                p.primary_id(self.tool_name)
                for p in updated.values()
                if p.primary_id(self.tool_name)
            }
            new_order = [pid for pid in cur_order if pid in valid_ids]
            for pid in sorted(valid_ids):
                if pid not in new_order:
                    new_order.append(pid)
            rendered_order = json.dumps(new_order, separators=(",", ":"))
            if storage.get("projectsOrder") != rendered_order:
                storage["projectsOrder"] = rendered_order
                if self._save_app_storage(storage):
                    changed = True

        return changed, updated

    # --- 6. Chats / Sessions ---

    def _read_transcript_messages(self, conv_id: str) -> list[ChatMessage]:
        logs_dir = self.brain_dir / conv_id / ".system_generated" / "logs"
        tr_path = logs_dir / "transcript_full.jsonl"
        if not tr_path.exists():
            tr_path = logs_dir / "transcript.jsonl"
        if not tr_path.exists():
            return []

        messages: list[ChatMessage] = []
        for line in tr_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                step = json.loads(line)
            except Exception:
                continue
            stype = step.get("type")
            content = str(step.get("content") or "").strip()
            if not content:
                continue
            ts_ms = _parse_agy_datetime_ms(str(step.get("created_at") or ""))
            if stype == "USER_INPUT":
                messages.append(ChatMessage(role="user", text=content, timestamp_ms=ts_ms))
            elif stype == "PLANNER_RESPONSE":
                messages.append(ChatMessage(role="assistant", text=content, timestamp_ms=ts_ms))
        return messages

    def _read_conv_db_messages(self, conv_id: str) -> tuple[list[ChatMessage], int]:
        cdb = self.conversations_dir / f"{conv_id}.db"
        if not cdb.exists():
            return [], 0
        messages: list[ChatMessage] = []
        created_ms = 0
        conn = sqlite_safe_connect(cdb, readonly=True)
        try:
            tmb = conn.execute("SELECT data FROM trajectory_metadata_blob WHERE id = 'main'").fetchone()
            if tmb and tmb[0]:
                try:
                    created_ms = int(decode_trajectory_metadata(tmb[0]).get("created_at_ms") or 0)
                except Exception:
                    pass
            rows = conn.execute(
                "SELECT step_type, step_payload FROM steps WHERE step_type IN (14, 15) ORDER BY idx ASC"
            ).fetchall()
            for stype, payload in rows:
                if not payload:
                    continue
                try:
                    txt, ts_ms = decode_step_text(int(stype), payload)
                except Exception:
                    continue
                txt = txt.strip()
                if txt:
                    role = "user" if int(stype) == 14 else "assistant"
                    messages.append(ChatMessage(role=role, text=txt, timestamp_ms=ts_ms))
        finally:
            conn.close()
        return messages, created_ms

    def read_chats(
        self,
        projects_by_key: dict[str, ProjectItem],
        include_subagents: bool = False,
    ) -> list[ChatSession]:
        if not self.summaries_db.exists():
            return []

        pid_to_proj: dict[str, tuple[str, str]] = {
            pid: (k, p.display_path)
            for k, p in projects_by_key.items()
            for pid in p.native_ids_by_tool.get(self.tool_name, [])
        }

        conn = sqlite_safe_connect(self.summaries_db, readonly=True)
        conn.row_factory = sqlite3.Row
        try:
            tables = {
                r[0]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            if "conversation_summaries" not in tables:
                return []
            parents_map: dict[str, str] = {}
            if "conversation_parents" in tables:
                for pr in conn.execute("SELECT conversation_id, parent_id FROM conversation_parents").fetchall():
                    parents_map[pr["conversation_id"]] = pr["parent_id"]
            rows = conn.execute("SELECT * FROM conversation_summaries ORDER BY last_modified_time DESC").fetchall()
        finally:
            conn.close()

        sessions: list[ChatSession] = []
        for row in rows:
            cid = row["conversation_id"]
            parent_id = (row["parent_conversation_id"] or "").strip() or parents_map.get(cid)
            is_subagent = bool(parent_id) or int(row["nesting_depth"] or 0) > 0
            if is_subagent and not include_subagents:
                continue

            proj_key: str | None = None
            proj_path: str | None = None
            native_pid = (row["project_id"] or "").strip()
            wuris_raw = (row["workspace_uris"] or "").strip()
            if wuris_raw:
                try:
                    wuris = json.loads(wuris_raw)
                    if isinstance(wuris, list) and wuris:
                        proj_key = path_dedup_key(str(wuris[0]))
                        proj_path = normalize_path(str(wuris[0]))
                except Exception:
                    pass
            if not proj_key and native_pid and native_pid in pid_to_proj:
                proj_key, proj_path = pid_to_proj[native_pid]

            messages = self._read_transcript_messages(cid)
            created_ms = 0
            if not messages:
                messages, created_ms = self._read_conv_db_messages(cid)

            updated_ms = _parse_agy_datetime_ms(str(row["last_modified_time"] or ""))
            if not created_ms:
                if messages and messages[0].timestamp_ms:
                    created_ms = messages[0].timestamp_ms
                else:
                    created_ms = _parse_agy_datetime_ms(str(row["last_user_input_time"] or "")) or updated_ms

            title = (row["title"] or row["preview"] or "").strip()
            if not title and messages:
                title = messages[0].text.splitlines()[0][:80]

            sessions.append(
                ChatSession(
                    tool=self.tool_name,
                    native_id=cid,
                    title=title,
                    project_key=proj_key,
                    project_path=proj_path,
                    native_project_id=native_pid if native_pid not in ("", "outside-of-project") else None,
                    created_at_ms=created_ms,
                    updated_at_ms=updated_ms,
                    is_subagent=is_subagent,
                    parent_id=parent_id,
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
        cid = target_native_id or str(uuid.uuid4())
        trajectory_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"agy-traj:{cid}"))
        created_ms = chat.created_at_ms or int(time.time() * 1000)
        updated_ms = chat.updated_at_ms or created_ms

        project = projects_by_key.get(chat.project_key) if chat.project_key else None
        agy_pid = (
            project.primary_id(self.tool_name)
            if (project and project.primary_id(self.tool_name))
            else "outside-of-project"
        )
        ws_uri = path_to_file_uri(project.display_path, encode_drive_colon=True) if project else ""
        ws_uris_json = json.dumps([ws_uri], separators=(",", ":")) if ws_uri else ""

        filtered_msgs = [
            m for m in chat.messages if m.role in ("user", "assistant") and m.text.strip()
        ]
        title = (
            chat.title or (filtered_msgs[0].text.splitlines()[0][:80] if filtered_msgs else "Synced Chat")
        ).strip()

        # 1. Write per-conversation SQLite DB with PRAGMA user_version = 1 (required by Antigravity!)
        cdb_path = self.conversations_dir / f"{cid}.db"
        conn = sqlite_safe_connect(cdb_path, readonly=False)
        try:
            conn.executescript(_CONV_DB_DDL)
            conn.execute("PRAGMA user_version = 1")
            conn.execute("DELETE FROM trajectory_meta")
            conn.execute(
                "INSERT INTO trajectory_meta(trajectory_id, cascade_id, trajectory_type, source) "
                "VALUES(?, ?, 4, 1)",
                (trajectory_id, cid),
            )
            tmb = encode_trajectory_metadata(
                cascade_id=cid,
                created_at_ms=created_ms,
                workspace_uri=ws_uri,
                project_id=agy_pid,
            )
            conn.execute(
                "INSERT OR REPLACE INTO trajectory_metadata_blob(id, data) VALUES('main', ?)",
                (tmb,),
            )
            conn.execute("DELETE FROM steps")

            last_user_idx = -1
            last_user_ms = created_ms
            transcript_lines: list[str] = []
            user_body_parts: list[str] = []
            agent_body_parts: list[str] = []

            for idx, msg in enumerate(filtered_msgs):
                mts = msg.timestamp_ms or (created_ms + idx * 1000)
                turn_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"agy-turn:{cid}:{idx}"))
                if msg.role == "user":
                    step_type = 14
                    meta_blob, payload_blob = encode_user_input_step(
                        text=msg.text,
                        timestamp_ms=mts,
                        trajectory_id=trajectory_id,
                        cascade_id=cid,
                        step_idx=idx,
                        turn_uuid=turn_uuid,
                    )
                    last_user_idx = idx
                    last_user_ms = mts
                    user_body_parts.append(msg.text)
                    tr_entry = {
                        "step_index": idx,
                        "source": "USER_EXPLICIT",
                        "type": "USER_INPUT",
                        "status": "DONE",
                        "created_at": _format_iso_utc(mts),
                        "content": msg.text,
                    }
                else:
                    step_type = 15
                    meta_blob, payload_blob = encode_planner_response_step(
                        text=msg.text,
                        timestamp_ms=mts,
                        trajectory_id=trajectory_id,
                        cascade_id=cid,
                        step_idx=idx,
                        turn_uuid=turn_uuid,
                    )
                    agent_body_parts.append(msg.text)
                    tr_entry = {
                        "step_index": idx,
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "created_at": _format_iso_utc(mts),
                        "content": msg.text,
                    }
                conn.execute(
                    """
                    INSERT INTO steps(
                        idx, step_type, status, has_subtrajectory, metadata, step_payload, step_format
                    ) VALUES(?, ?, 3, 0, ?, ?, 0)
                    """,
                    (idx, step_type, meta_blob, payload_blob),
                )
                transcript_lines.append(json.dumps(tr_entry, ensure_ascii=False))
            conn.commit()
        finally:
            conn.close()

        # 2. Write brain transcripts
        logs_dir = self.brain_dir / cid / ".system_generated" / "logs"
        tr_text = ("\n".join(transcript_lines) + "\n") if transcript_lines else ""
        atomic_write_text(logs_dir / "transcript.jsonl", tr_text)
        atomic_write_text(logs_dir / "transcript_full.jsonl", tr_text)

        # 3. Write annotation pbtxt
        escaped_title = title.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
        secs = updated_ms // 1000
        nanos = (updated_ms % 1000) * 1_000_000
        ann_text = f'title:"{escaped_title}" last_user_view_time:{{seconds:{secs} nanos:{nanos}}}\n'
        atomic_write_text(self.annotations_dir / f"{cid}.pbtxt", ann_text)

        # 4. Encode native raw_summary (field 5 = 1 IDLE, field 16 = last_user_idx, field 28 = 4)
        raw_summary = encode_raw_summary(
            title=title,
            step_count=len(filtered_msgs),
            created_at_ms=created_ms,
            last_modified_ms=updated_ms,
            last_user_input_ms=last_user_ms,
            trajectory_id=trajectory_id,
            cascade_id=cid,
            workspace_uri=ws_uri,
            project_id=agy_pid,
            last_user_input_step_index=max(0, last_user_idx),
            run_status=RUN_STATUS_IDLE,
        )

        # 5. Upsert conversation_summaries.db + FTS5
        sconn = self._ensure_summaries_db()
        try:
            sconn.execute(
                """
                INSERT INTO conversation_summaries(
                    conversation_id, title, preview, step_count, last_modified_time,
                    workspace_uris, status, source, project_id, agent_name,
                    parent_conversation_id, nesting_depth, battle_id, winning_conversation_id,
                    not_fully_idle, killed, last_user_input_time, last_user_input_step_index,
                    app_data_dir, raw_summary, group_id
                ) VALUES(
                    ?, ?, ?, ?, ?,
                    ?, 'CASCADE_RUN_STATUS_IDLE', '', ?, '',
                    '', 0, '', '',
                    0, 0, ?, ?,
                    'antigravity', ?, ''
                )
                ON CONFLICT(conversation_id) DO UPDATE SET
                    title=excluded.title,
                    preview=excluded.preview,
                    step_count=excluded.step_count,
                    last_modified_time=excluded.last_modified_time,
                    workspace_uris=excluded.workspace_uris,
                    project_id=excluded.project_id,
                    last_user_input_time=excluded.last_user_input_time,
                    last_user_input_step_index=excluded.last_user_input_step_index,
                    raw_summary=excluded.raw_summary
                """,
                (
                    cid,
                    title,
                    title,
                    len(filtered_msgs),
                    _format_agy_datetime(updated_ms),
                    ws_uris_json,
                    agy_pid,
                    _format_agy_datetime(last_user_ms),
                    last_user_idx,
                    raw_summary,
                ),
            )
            sconn.execute("DELETE FROM conversations_fts WHERE conversation_id = ?", (cid,))
            sconn.execute(
                """
                INSERT INTO conversations_fts(
                    conversation_id, chunk_index, chunk_first_step, chunk_last_step,
                    last_modified_time, step_count, last_step_created_at, archived,
                    user_step_byte_offsets, agent_step_byte_offsets,
                    title, workspace, project, user_body, agent_body
                ) VALUES(?, -1, 0, 0, ?, ?, ?, 0, '', '', ?, ?, ?, '', '')
                """,
                (
                    cid,
                    updated_ms * 1_000_000,
                    len(filtered_msgs),
                    updated_ms * 1_000_000,
                    title,
                    project.display_path if project else "",
                    project.name if project else "",
                ),
            )
            if user_body_parts or agent_body_parts:
                sconn.execute(
                    """
                    INSERT INTO conversations_fts(
                        conversation_id, chunk_index, chunk_first_step, chunk_last_step,
                        last_modified_time, step_count, last_step_created_at, archived,
                        user_step_byte_offsets, agent_step_byte_offsets,
                        title, workspace, project, user_body, agent_body
                    ) VALUES(?, 0, 0, ?, 0, 0, 0, 0, '', '', '', '', '', ?, ?)
                    """,
                    (
                        cid,
                        max(0, len(filtered_msgs) - 1),
                        "\n\n".join(user_body_parts),
                        "\n\n".join(agent_body_parts),
                    ),
                )
            sconn.commit()
        finally:
            sconn.close()

        # 6. CRITICAL: Register summary in agyhub_summaries_proto.pb so Antigravity Hub UI displays the chat!
        upsert_agyhub_summaries_pb(self.hub_summaries_pb, {cid: raw_summary})

        return cid, True

    # --- Diagnostics & Repair ---

    def doctor(self) -> list[DoctorIssue]:
        issues: list[DoctorIssue] = []

        # 1. Validate JSON configs
        for p, code in (
            (self.config_json, "AGY_INVALID_CONFIG_JSON"),
            (self.mcp_config_json, "AGY_INVALID_MCP_JSON"),
        ):
            if p.exists():
                txt = p.read_text(encoding="utf-8-sig", errors="replace").strip()
                if txt:
                    try:
                        json.loads(txt)
                    except Exception as exc:
                        issues.append(
                            DoctorIssue(
                                tool=self.tool_name,
                                severity="error",
                                code=code,
                                path=str(p),
                                message=f"Invalid JSON: {exc}",
                                fixable=False,
                            )
                        )

        # 2. Check agyhub_summaries_proto.pb vs conversation_summaries.db synchronization
        pb_entries = read_agyhub_summaries_pb(self.hub_summaries_pb)
        if self.summaries_db.exists():
            conn = sqlite_safe_connect(self.summaries_db, readonly=True)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT conversation_id, title, status, raw_summary FROM conversation_summaries"
                ).fetchall()
                missing_from_pb: list[str] = []
                bad_status_enum: list[str] = []
                for r in rows:
                    cid = r["conversation_id"]
                    if cid not in pb_entries:
                        missing_from_pb.append(cid)
                    raw_s = r["raw_summary"]
                    if raw_s and r["status"] == "CASCADE_RUN_STATUS_IDLE":
                        try:
                            f5 = first_varint(parse_wire(raw_s), 5)
                            if f5 != RUN_STATUS_IDLE:
                                bad_status_enum.append(cid)
                        except Exception:
                            bad_status_enum.append(cid)
                if missing_from_pb:
                    issues.append(
                        DoctorIssue(
                            tool=self.tool_name,
                            severity="error",
                            code="AGY_MISSING_HUB_SUMMARY_PB",
                            path=str(self.hub_summaries_pb),
                            message=(
                                f"{len(missing_from_pb)} conversation(s) in conversation_summaries.db "
                                "are missing from agyhub_summaries_proto.pb (invisible in Antigravity Hub UI)"
                            ),
                            fixable=True,
                        )
                    )
                if bad_status_enum:
                    issues.append(
                        DoctorIssue(
                            tool=self.tool_name,
                            severity="warning",
                            code="AGY_RAW_SUMMARY_STATUS_MISMATCH",
                            path=str(self.summaries_db),
                            message=(
                                f"{len(bad_status_enum)} idle conversation(s) have raw_summary field 5 != 1 (IDLE)"
                            ),
                            fixable=True,
                        )
                    )
            except sqlite3.OperationalError:
                pass
            finally:
                conn.close()

        # 3. Check PRAGMA user_version == 1 on all conversations/*.db
        if self.conversations_dir.exists():
            bad_uv: list[str] = []
            for db in sorted(self.conversations_dir.glob("*.db")):
                c = sqlite_safe_connect(db, readonly=True)
                try:
                    uv = c.execute("PRAGMA user_version").fetchone()[0]
                    if uv != 1:
                        bad_uv.append(db.name)
                except Exception:
                    bad_uv.append(db.name)
                finally:
                    c.close()
            if bad_uv:
                issues.append(
                    DoctorIssue(
                        tool=self.tool_name,
                        severity="error",
                        code="AGY_CONV_DB_USER_VERSION",
                        path=str(self.conversations_dir),
                        message=f"{len(bad_uv)} conversation DB(s) have PRAGMA user_version != 1 (expected 1)",
                        fixable=True,
                    )
                )

        return issues

    def repair(self, quarantine_dir: Path, dry_run: bool = False) -> RepairResult:
        result = RepairResult(tool=self.tool_name)
        if not self.summaries_db.exists():
            return result

        pb_entries = read_agyhub_summaries_pb(self.hub_summaries_pb)
        projs = self.read_projects()

        conn = sqlite_safe_connect(self.summaries_db, readonly=dry_run)
        conn.row_factory = sqlite3.Row
        pb_updates: dict[str, bytes] = {}
        try:
            rows = conn.execute("SELECT * FROM conversation_summaries").fetchall()
            for r in rows:
                cid = r["conversation_id"]
                cdb = self.conversations_dir / f"{cid}.db"
                needs_uv_fix = False
                if cdb.exists():
                    cc = sqlite_safe_connect(cdb, readonly=True)
                    try:
                        needs_uv_fix = cc.execute("PRAGMA user_version").fetchone()[0] != 1
                    finally:
                        cc.close()

                raw_s = r["raw_summary"]
                needs_raw_fix = False
                if raw_s and r["status"] == "CASCADE_RUN_STATUS_IDLE":
                    try:
                        if first_varint(parse_wire(raw_s), 5) != RUN_STATUS_IDLE:
                            needs_raw_fix = True
                    except Exception:
                        needs_raw_fix = True
                elif not raw_s:
                    needs_raw_fix = True

                missing_pb = cid not in pb_entries
                if not (needs_uv_fix or needs_raw_fix or missing_pb):
                    continue

                if dry_run:
                    result.fixed_count += 1
                    result.details.append(
                        f"Would repair Antigravity chat {cid[:8]} "
                        f"(uv_fix={needs_uv_fix}, raw_fix={needs_raw_fix}, pb_add={missing_pb})"
                    )
                    continue

                if needs_uv_fix and cdb.exists():
                    wc = sqlite_safe_connect(cdb, readonly=False)
                    try:
                        wc.execute("PRAGMA user_version = 1")
                        wc.commit()
                    finally:
                        wc.close()

                if needs_raw_fix:
                    traj_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"agy-traj:{cid}"))
                    if cdb.exists():
                        tc = sqlite_safe_connect(cdb, readonly=True)
                        try:
                            tm = tc.execute("SELECT trajectory_id FROM trajectory_meta LIMIT 1").fetchone()
                            if tm and tm[0]:
                                traj_id = tm[0]
                        finally:
                            tc.close()
                    ws_uri = ""
                    if r["workspace_uris"]:
                        try:
                            parsed_w = json.loads(r["workspace_uris"])
                            if isinstance(parsed_w, list) and parsed_w:
                                ws_uri = str(parsed_w[0])
                        except Exception:
                            pass
                    mod_ms = _parse_agy_datetime_ms(str(r["last_modified_time"] or "")) or int(time.time() * 1000)
                    usr_ms = _parse_agy_datetime_ms(str(r["last_user_input_time"] or "")) or mod_ms
                    raw_s = encode_raw_summary(
                        title=r["title"] or "",
                        step_count=int(r["step_count"] or 0),
                        created_at_ms=usr_ms,
                        last_modified_ms=mod_ms,
                        last_user_input_ms=usr_ms,
                        trajectory_id=traj_id,
                        cascade_id=cid,
                        workspace_uri=ws_uri,
                        project_id=r["project_id"] or "outside-of-project",
                        last_user_input_step_index=max(0, int(r["last_user_input_step_index"] or 0)),
                        run_status=RUN_STATUS_IDLE,
                    )
                    conn.execute(
                        "UPDATE conversation_summaries SET raw_summary = ? WHERE conversation_id = ?",
                        (raw_s, cid),
                    )

                if raw_s and (missing_pb or needs_raw_fix):
                    pb_updates[cid] = raw_s
                result.fixed_count += 1
                result.details.append(f"Repaired Antigravity chat {cid[:8]} and registered in hub summary pb")

            if not dry_run:
                conn.commit()
        finally:
            conn.close()

        if pb_updates and not dry_run:
            if self.hub_summaries_pb.exists():
                ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
                q_root = quarantine_dir / f"{ts}_antigravity"
                q_root.mkdir(parents=True, exist_ok=True)
                q_dst = q_root / self.hub_summaries_pb.name
                shutil.copy2(self.hub_summaries_pb, q_dst)
                result.quarantined_files.append(str(q_dst))
            upsert_agyhub_summaries_pb(self.hub_summaries_pb, pb_updates)

        _ = projs  # keep local binding clean
        return result
