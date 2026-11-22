"""OpenAI Codex adapter with schema-conformant rollout JSONL, SQLite sync, doctor, and repair."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import time
import tomllib
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
    McpVariable,
    PluginItem,
    ProjectItem,
    RepairResult,
    SkillItem,
    parse_skill_md,
)
from agentsync.utils.atomic_io import (
    atomic_write_json,
    atomic_write_text,
    atomic_write_toml,
    remove_tree_if_exists,
    sqlite_safe_connect,
    sync_directory_files,
)
from agentsync.utils.paths import normalize_path, path_dedup_key, to_native_path

_CODEX_INTERNAL_MCP_NAMES = {"node_repl", "cua_repl", "codex_app"}
_CODEX_INTERNAL_MARKETPLACES = {"openai-bundled", "openai-primary-runtime"}
_CODEX_EXTRA_MCP_KEYS = (
    "startup_timeout_sec",
    "tool_timeout_sec",
    "env_vars",
    "enabled_tools",
    "disabled_tools",
    "required",
    "oauth_resource",
    "scopes",
)
_BEARER_VAR_RE = re.compile(r"^Bearer\s+\$\{([A-Za-z_][A-Za-z0-9_]*)\}$", re.IGNORECASE)
_PLAIN_VAR_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_IGNORED_DIR_NAMES = {".git", "__pycache__", "node_modules", ".DS_Store"}

# Required keys in Codex Rust SessionMetaLine (line 1 of rollout-*.jsonl)
REQUIRED_SESSION_META_KEYS = ("id", "timestamp", "cwd", "originator", "cli_version", "source")

_STATE_5_MIN_DDL = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    position INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS project_roots (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    path TEXT NOT NULL,
    PRIMARY KEY (project_id, position)
);
CREATE TABLE IF NOT EXISTS project_idempotency_keys (
    key TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY,
    rollout_path TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    source TEXT NOT NULL,
    model_provider TEXT NOT NULL,
    cwd TEXT NOT NULL,
    title TEXT NOT NULL,
    sandbox_policy TEXT NOT NULL,
    approval_mode TEXT NOT NULL,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    has_user_event INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    archived_at INTEGER,
    git_sha TEXT,
    git_branch TEXT,
    git_origin_url TEXT,
    cli_version TEXT NOT NULL DEFAULT '',
    first_user_message TEXT NOT NULL DEFAULT '',
    agent_nickname TEXT,
    agent_role TEXT,
    memory_mode TEXT NOT NULL DEFAULT 'enabled',
    model TEXT,
    reasoning_effort TEXT,
    agent_path TEXT,
    created_at_ms INTEGER,
    updated_at_ms INTEGER,
    thread_source TEXT,
    preview TEXT NOT NULL DEFAULT '',
    recency_at INTEGER NOT NULL DEFAULT 0,
    recency_at_ms INTEGER NOT NULL DEFAULT 0,
    history_mode TEXT NOT NULL DEFAULT 'paginated',
    name TEXT,
    is_pinned INTEGER NOT NULL DEFAULT 0,
    thread_section_id TEXT,
    section_position INTEGER,
    section_entered_at_ms INTEGER,
    project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
    originator TEXT,
    daybreak_enabled BOOLEAN,
    creator_user_id TEXT,
    creator_account_id TEXT,
    attached_thread_parent_id TEXT
);
CREATE TABLE IF NOT EXISTS thread_spawn_edges (
    parent_thread_id TEXT NOT NULL,
    child_thread_id TEXT NOT NULL PRIMARY KEY,
    status TEXT NOT NULL
);
"""

_THREAD_HISTORY_MIN_DDL = """
CREATE TABLE IF NOT EXISTS thread_turns (
    thread_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    rollout_ordinal INTEGER NOT NULL,
    status TEXT NOT NULL,
    error_json TEXT,
    started_at INTEGER,
    completed_at INTEGER,
    duration_ms INTEGER,
    first_user_item_id TEXT,
    final_agent_item_id TEXT,
    rollout_byte_offset INTEGER,
    rollout_end_ordinal INTEGER,
    rollout_end_byte_offset INTEGER,
    root_turn_id TEXT,
    PRIMARY KEY (thread_id, turn_id)
);
CREATE TABLE IF NOT EXISTS thread_items (
    thread_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    rollout_ordinal INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL,
    item_json TEXT NOT NULL,
    item_type TEXT NOT NULL DEFAULT '',
    updated_at_ordinal INTEGER NOT NULL DEFAULT 0,
    started_at_ms INTEGER,
    completed_at_ms INTEGER,
    PRIMARY KEY (thread_id, turn_id, item_id)
);
CREATE TABLE IF NOT EXISTS thread_history_projection_state (
    thread_id TEXT PRIMARY KEY,
    next_rollout_byte_offset INTEGER NOT NULL,
    next_rollout_ordinal INTEGER NOT NULL
);
"""


def _iso_utc_ms(ts_ms: int) -> str:
    dt = datetime.fromtimestamp(max(0, ts_ms) / 1000, tz=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


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


def _is_codex_runtime_mcp(name: str, entry: dict[str, Any]) -> bool:
    if name in _CODEX_INTERNAL_MCP_NAMES:
        return True
    cmd = str(entry.get("command") or "").lower()
    args_str = " ".join(str(a) for a in (entry.get("args") or [])).lower()
    env_str = " ".join(f"{k}={v}" for k, v in (entry.get("env") or {}).items()).lower()
    blob = f"{cmd} {args_str} {env_str}"
    markers = (
        "\\pipe\\codex-computer-use",
        "openai\\codex\\runtimes",
        "openai/codex/runtimes",
        "openai-bundled",
        "codex-runtimes",
    )
    return any(m in blob for m in markers)


def _parse_codex_mcp_entry(name: str, entry: dict[str, Any], updated_at_ms: int) -> McpServer:
    internal = _is_codex_runtime_mcp(name, entry)
    url = entry.get("url")
    command = entry.get("command")
    transport: McpTransport
    if url:
        raw_type = str(entry.get("type") or "").lower()
        transport = "sse" if (raw_type == "sse" or str(url).rstrip("/").lower().endswith("/sse")) else "http"
    else:
        transport = "stdio"

    headers: dict[str, str] = {
        str(k): str(v) for k, v in (entry.get("http_headers") or entry.get("headers") or {}).items()
    }
    variables: list[McpVariable] = []
    seen_vars: set[str] = set()

    def _add_var(vname: str, desc: str) -> None:
        if vname and vname not in seen_vars:
            seen_vars.add(vname)
            variables.append(
                McpVariable(name=vname, title=vname.replace("_", " ").title(), description=desc)
            )

    bearer_var = entry.get("bearer_token_env_var")
    if bearer_var:
        bv = str(bearer_var)
        headers["Authorization"] = f"Bearer ${{{bv}}}"
        _add_var(bv, f"Bearer token environment variable ({bv}) migrated from Codex.")

    for hdr_name, env_name in (entry.get("env_http_headers") or {}).items():
        ev = str(env_name)
        headers[str(hdr_name)] = f"${{{ev}}}"
        _add_var(ev, f"Header {hdr_name} environment variable ({ev}) migrated from Codex.")

    extra = {k: entry[k] for k in _CODEX_EXTRA_MCP_KEYS if k in entry}
    disabled = entry.get("enabled") is False or entry.get("disabled") is True

    return McpServer(
        name=name,
        transport=transport,
        command=str(command) if command else None,
        args=[str(a) for a in (entry.get("args") or [])],
        env={str(k): str(v) for k, v in (entry.get("env") or {}).items()},
        cwd=str(entry["cwd"]) if entry.get("cwd") else None,
        url=str(url) if url else None,
        headers=headers,
        disabled=disabled,
        variables=variables,
        extra_fields=extra,
        portable=not internal,
        skip_reason="codex_internal_runtime" if internal else None,
        source_tool="codex",
        updated_at_ms=updated_at_ms,
    ).compute_hash()


def _serialize_codex_mcp_entry(server: McpServer) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if server.transport == "stdio":
        if server.command:
            out["command"] = server.command
        out["args"] = list(server.args)
        if server.env:
            out["env"] = dict(server.env)
        if server.cwd:
            out["cwd"] = to_native_path(server.cwd)
    else:
        if server.url:
            out["url"] = server.url
        if server.transport == "sse":
            out["type"] = "sse"
        http_headers: dict[str, str] = {}
        env_http_headers: dict[str, str] = {}
        bearer_var: str | None = None
        for hk, hv in server.headers.items():
            bm = _BEARER_VAR_RE.match(hv.strip()) if hk.lower() == "authorization" else None
            pm = _PLAIN_VAR_RE.match(hv.strip())
            if bm:
                bearer_var = bm.group(1)
            elif pm:
                env_http_headers[hk] = pm.group(1)
            else:
                http_headers[hk] = hv
        if bearer_var:
            out["bearer_token_env_var"] = bearer_var
        if http_headers:
            out["http_headers"] = http_headers
        if env_http_headers:
            out["env_http_headers"] = env_http_headers

    if server.disabled:
        out["enabled"] = False
    for k, v in server.extra_fields.items():
        if k in _CODEX_EXTRA_MCP_KEYS and k not in out and v is not None:
            out[k] = v
    return out


def validate_codex_rollout_file(path: Path) -> list[str]:
    """Validate a Codex rollout JSONL file against native thread-store expectations."""
    errors: list[str] = []
    if not path.exists():
        return [f"Missing rollout file: {path}"]
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        errors.append("File starts with UTF-8 BOM (must be UTF-8 without BOM)")
    if not raw.strip():
        return ["Rollout file is empty"]
    lines = raw.decode("utf-8", errors="replace").split("\n")
    non_empty = [line for line in lines if line.strip()]
    if not non_empty:
        return ["Rollout file contains no JSONL lines"]

    # Validate Line 1 strictly as SessionMetaLine
    try:
        line1 = json.loads(non_empty[0])
    except json.JSONDecodeError as exc:
        return [f"Line 1 is not valid JSON: {exc}"]

    if not isinstance(line1, dict):
        errors.append("Line 1 must be a JSON object")
    else:
        if line1.get("type") != "session_meta":
            errors.append(f"Line 1 type is '{line1.get('type')}', expected 'session_meta'")
        if not line1.get("timestamp"):
            errors.append("Line 1 missing top-level 'timestamp'")
        payload = line1.get("payload")
        if not isinstance(payload, dict):
            errors.append("Line 1 missing object 'payload'")
        else:
            missing = [k for k in REQUIRED_SESSION_META_KEYS if not payload.get(k)]
            if missing:
                errors.append(f"Line 1 session_meta payload missing required keys: {', '.join(missing)}")

    # Validate every subsequent line is valid JSON with type + payload
    for idx, line in enumerate(non_empty[1:], start=2):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"Line {idx} invalid JSON: {exc}")
            break
        if not isinstance(obj, dict) or not obj.get("type") or not obj.get("timestamp"):
            errors.append(f"Line {idx} missing required envelope keys ('timestamp', 'type')")
            break

    return errors


@register_adapter
class CodexAdapter(ToolAdapter):
    """Adapter for OpenAI Codex Desktop / CLI (`~/.codex`)."""

    tool_name = "codex"
    display_name = "OpenAI Codex"
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

    # --- Paths ---
    @property
    def config_toml(self) -> Path:
        return self.root_path / "config.toml"

    @property
    def skills_dir(self) -> Path:
        return self.root_path / "skills"

    @property
    def plugins_cache_dir(self) -> Path:
        return self.root_path / "plugins" / "cache"

    @property
    def agents_md(self) -> Path:
        return self.root_path / "AGENTS.md"

    @property
    def agents_override_md(self) -> Path:
        return self.root_path / "AGENTS.override.md"

    @property
    def global_state_json(self) -> Path:
        return self.root_path / ".codex-global-state.json"

    @property
    def state_db(self) -> Path:
        return self.root_path / "state_5.sqlite"

    @property
    def thread_history_db(self) -> Path:
        return self.root_path / "thread_history_1.sqlite"

    @property
    def sessions_dir(self) -> Path:
        return self.root_path / "sessions"

    @property
    def session_index_jsonl(self) -> Path:
        return self.root_path / "session_index.jsonl"

    # --- Backup & Watch ---

    def backup_targets(
        self,
    ) -> tuple[list[tuple[Path, Literal["file", "sqlite"]]], list[Path]]:
        files: list[tuple[Path, Literal["file", "sqlite"]]] = [
            (self.config_toml, "file"),
            (self.agents_md, "file"),
            (self.agents_override_md, "file"),
            (self.global_state_json, "file"),
            (self.session_index_jsonl, "file"),
            (self.state_db, "sqlite"),
            (self.thread_history_db, "sqlite"),
        ]
        dirs = [
            self.skills_dir,
            self.plugins_cache_dir / "agentsync-synced",
            self.sessions_dir,
        ]
        return files, dirs

    def should_skip_backup_file(self, root_dir: Path, file_path: Path) -> bool:
        if super().should_skip_backup_file(root_dir, file_path):
            return True
        if root_dir == self.skills_dir:
            rel_parts = file_path.relative_to(root_dir).parts
            if rel_parts and rel_parts[0] == ".system":
                return True
        return False

    # --- Helpers ---

    def _load_config_toml(self) -> tuple[dict[str, Any], int]:
        if not self.config_toml.exists():
            return {}, 0
        raw = self.config_toml.read_text(encoding="utf-8-sig").strip()
        mtime_ms = int(self.config_toml.stat().st_mtime * 1000)
        return (tomllib.loads(raw) if raw else {}), mtime_ms

    def _save_config_toml(self, data: dict[str, Any]) -> bool:
        return atomic_write_toml(self.config_toml, data)

    def _load_global_state(self) -> dict[str, Any]:
        if not self.global_state_json.exists():
            return {}
        txt = self.global_state_json.read_text(encoding="utf-8-sig").strip()
        return json.loads(txt) if txt else {}

    def _save_global_state(self, data: dict[str, Any]) -> bool:
        return atomic_write_json(self.global_state_json, data)

    def _host_key(self) -> str:
        return f"local:{to_native_path(str(self.root_path))}"

    def _ensure_state_db(self) -> sqlite3.Connection:
        conn = sqlite_safe_connect(self.state_db, readonly=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(_STATE_5_MIN_DDL)
        conn.commit()
        return conn

    def _ensure_history_db(self) -> sqlite3.Connection:
        conn = sqlite_safe_connect(self.thread_history_db, readonly=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(_THREAD_HISTORY_MIN_DDL)
        conn.commit()
        return conn

    def _detect_native_thread_defaults(self) -> dict[str, str | None]:
        """Read native Codex cli_version, model, creator_user_id, and creator_account_id if available."""
        cfg, _ = self._load_config_toml()
        defaults: dict[str, str | None] = {
            "cli_version": "0.167.0",
            "model": str(cfg.get("model") or "gpt-5"),
            "reasoning_effort": str(cfg.get("model_reasoning_effort") or "medium"),
            "creator_user_id": None,
            "creator_account_id": None,
        }
        if self.state_db.exists():
            conn = sqlite_safe_connect(self.state_db, readonly=True)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    """
                    SELECT cli_version, model, reasoning_effort, creator_user_id, creator_account_id
                    FROM threads
                    WHERE cli_version != '' AND cli_version != 'agy-codex-sync'
                      AND cli_version != 'agentsync'
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """
                ).fetchone()
                if row:
                    if row["cli_version"]:
                        defaults["cli_version"] = row["cli_version"]
                    if row["creator_user_id"]:
                        defaults["creator_user_id"] = row["creator_user_id"]
                    if row["creator_account_id"]:
                        defaults["creator_account_id"] = row["creator_account_id"]
                    if row["model"] and not cfg.get("model"):
                        defaults["model"] = row["model"]
            except sqlite3.OperationalError:
                pass
            finally:
                conn.close()
        return defaults

    def clear_managed_for_bootstrap(self) -> dict[str, list[str]]:
        removed: dict[str, list[str]] = {"skills": [], "mcp": [], "plugins": []}
        if self.skills_dir.exists():
            for item in sorted(self.skills_dir.iterdir()):
                if item.name.startswith("."):
                    continue
                if remove_tree_if_exists(item):
                    removed["skills"].append(item.name)

        existing_mcps = self.read_mcp_servers()
        removed["mcp"] = [k for k, v in existing_mcps.items() if v.portable]
        self.write_mcp_servers({})

        synced_mp = self.plugins_cache_dir / "agentsync-synced"
        if synced_mp.exists():
            for p in sorted(synced_mp.iterdir()):
                if remove_tree_if_exists(p):
                    removed["plugins"].append(p.name)
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
        return sync_directory_files(self.skills_dir / skill.name, skill.files)

    def delete_skill(self, skill_name: str) -> bool:
        if skill_name.startswith("."):
            return False
        return remove_tree_if_exists(self.skills_dir / skill_name)

    # --- 2. MCP Servers ---

    def read_mcp_servers(self) -> dict[str, McpServer]:
        cfg, mtime_ms = self._load_config_toml()
        raw_servers = cfg.get("mcp_servers") or {}
        return {
            name: _parse_codex_mcp_entry(name, entry, mtime_ms)
            for name, entry in sorted(raw_servers.items())
            if isinstance(entry, dict)
        }

    def write_mcp_servers(self, servers: dict[str, McpServer]) -> tuple[bool, list[str]]:
        cfg, _ = self._load_config_toml()
        existing_raw = dict(cfg.get("mcp_servers") or {})
        new_raw: dict[str, Any] = {}

        # Preserve internal Codex runtime MCP servers untouched
        for ename, eentry in existing_raw.items():
            if isinstance(eentry, dict) and _is_codex_runtime_mcp(ename, eentry):
                new_raw[ename] = eentry

        for name, srv in sorted(servers.items()):
            if not srv.portable:
                if name in existing_raw:
                    new_raw[name] = existing_raw[name]
                continue
            new_raw[name] = _serialize_codex_mcp_entry(srv)

        if new_raw:
            cfg["mcp_servers"] = new_raw
        elif "mcp_servers" in cfg:
            del cfg["mcp_servers"]

        return self._save_config_toml(cfg), []

    # --- 3. Plugins ---

    def _find_plugin_skills(self, plugin_root: Path) -> dict[str, SkillItem]:
        found: dict[str, SkillItem] = {}
        skills_root = plugin_root / "skills"
        if not skills_root.exists() or not skills_root.is_dir():
            return found
        for skill_md in sorted(skills_root.rglob("SKILL.md")):
            rel_parts = skill_md.relative_to(skills_root).parts
            if any(p in _IGNORED_DIR_NAMES or p == "deprecated" for p in rel_parts):
                continue
            sdir = skill_md.parent
            sname = sdir.name
            if sname in found:
                continue
            files, mtime_ms = _read_dir_files(sdir)
            md_text = files.get("SKILL.md", b"").decode("utf-8", errors="replace")
            fm, _, had_fm = parse_skill_md(md_text, sname)
            found[sname] = SkillItem(
                name=sname,
                frontmatter_name=str(fm.get("name") or sname),
                description=str(fm.get("description") or ""),
                files=files,
                updated_at_ms=mtime_ms,
                source_tool=self.tool_name,
                synthesized_frontmatter=not had_fm,
            ).compute_hashes()
        return found

    def read_plugins(self) -> dict[str, PluginItem]:
        cfg, cfg_mtime_ms = self._load_config_toml()
        cfg_plugins: dict[str, dict[str, Any]] = cfg.get("plugins") or {}
        result: dict[str, PluginItem] = {}

        enabled_map: dict[tuple[str, str], bool] = {}
        for full_key, pcfg in cfg_plugins.items():
            pname, mp = full_key.split("@", 1) if "@" in full_key else (full_key, "local")
            enabled_map[(mp, pname)] = bool((pcfg or {}).get("enabled", True))

        if not self.plugins_cache_dir.exists():
            return result

        for mp_dir in sorted(self.plugins_cache_dir.iterdir()):
            if not mp_dir.is_dir() or mp_dir.name.startswith("."):
                continue
            mp_name = mp_dir.name
            for pl_dir in sorted(mp_dir.iterdir()):
                if not pl_dir.is_dir() or pl_dir.name.startswith("."):
                    continue
                pl_name = pl_dir.name
                ver_dirs = sorted(
                    [d for d in pl_dir.iterdir() if d.is_dir() and not d.name.startswith(".")],
                    key=lambda d: d.name,
                )
                if not ver_dirs:
                    continue
                vdir = ver_dirs[-1]
                ver = vdir.name
                is_enabled = enabled_map.get((mp_name, pl_name), (mp_name, pl_name) not in enabled_map)

                if mp_name in _CODEX_INTERNAL_MARKETPLACES:
                    key = f"{pl_name}@{mp_name}"
                    result[key] = PluginItem(
                        name=pl_name,
                        display_name=pl_name,
                        version=ver,
                        description=f"Codex bundled plugin ({mp_name})",
                        enabled=is_enabled,
                        marketplace=mp_name,
                        portable=False,
                        skip_reason="codex_bundled_runtime",
                        source_tool=self.tool_name,
                        updated_at_ms=cfg_mtime_ms,
                    ).compute_hash()
                    continue

                manifest: dict[str, Any] = {}
                for cand in (
                    vdir / ".codex-plugin" / "plugin.json",
                    vdir / ".claude-plugin" / "plugin.json",
                    vdir / "plugin.json",
                ):
                    if cand.exists():
                        try:
                            manifest = json.loads(cand.read_text(encoding="utf-8-sig"))
                        except Exception:
                            manifest = {}
                        break

                plugin_mcps: dict[str, McpServer] = {}
                for mc_name in (".mcp.json", "mcp_config.json", "mcp.json"):
                    mcp_file = vdir / mc_name
                    if mcp_file.exists():
                        try:
                            mdata = json.loads(mcp_file.read_text(encoding="utf-8-sig"))
                            for sname, sentry in (mdata.get("mcpServers") or {}).items():
                                if isinstance(sentry, dict):
                                    plugin_mcps[sname] = _parse_codex_mcp_entry(sname, sentry, cfg_mtime_ms)
                        except Exception:
                            pass
                        break

                portable_mcps = {k: v for k, v in plugin_mcps.items() if v.portable}
                plugin_skills = self._find_plugin_skills(vdir)

                rules: dict[str, str] = {}
                rules_dir = vdir / "rules"
                if rules_dir.exists() and rules_dir.is_dir():
                    for rf in sorted(rules_dir.glob("*.md")):
                        rules[rf.name] = rf.read_text(encoding="utf-8", errors="replace")

                is_connector_only = (
                    mp_name == "openai-curated-remote"
                    and not portable_mcps
                    and not plugin_skills
                    and not rules
                )
                key = pl_name if pl_name not in result else f"{pl_name}@{mp_name}"
                mtime_ms = max(
                    [
                        cfg_mtime_ms,
                        int(vdir.stat().st_mtime * 1000),
                        *(s.updated_at_ms for s in plugin_skills.values()),
                    ]
                )
                if is_connector_only:
                    result[key] = PluginItem(
                        name=pl_name,
                        display_name=str(manifest.get("displayName") or manifest.get("name") or pl_name),
                        version=str(manifest.get("version") or ver),
                        description=str(manifest.get("description") or ""),
                        enabled=is_enabled,
                        marketplace=mp_name,
                        portable=False,
                        skip_reason="codex_remote_connector_only",
                        source_tool=self.tool_name,
                        updated_at_ms=mtime_ms,
                    ).compute_hash()
                    continue

                result[key] = PluginItem(
                    name=pl_name,
                    display_name=str(manifest.get("displayName") or manifest.get("name") or pl_name),
                    version=str(manifest.get("version") or ver),
                    description=str(manifest.get("description") or ""),
                    enabled=is_enabled,
                    marketplace=mp_name,
                    skills=plugin_skills,
                    mcp_servers=portable_mcps,
                    rules=rules,
                    portable=True,
                    source_tool=self.tool_name,
                    updated_at_ms=mtime_ms,
                ).compute_hash()

        return result

    def write_plugin(self, plugin: PluginItem) -> tuple[bool, list[str]]:
        if not plugin.portable:
            return False, [f"Plugin '{plugin.name}' skipped ({plugin.skip_reason})"]

        mp_name = "agentsync-synced"
        mp_root = self.plugins_cache_dir / mp_name
        plugin_dir = mp_root / plugin.name / (plugin.version or "1.0.0")
        files: dict[str, bytes] = {}

        manifest: dict[str, Any] = {
            "name": plugin.name,
            "displayName": plugin.display_name or plugin.name,
            "version": plugin.version or "1.0.0",
            "description": plugin.description or f"Synced plugin {plugin.name}",
        }
        portable_mcps = {k: v for k, v in plugin.mcp_servers.items() if v.portable}
        if portable_mcps:
            manifest["mcpServers"] = "./.mcp.json"
            mcp_payload = {
                "mcpServers": {
                    k: _serialize_codex_mcp_entry(v) for k, v in sorted(portable_mcps.items())
                }
            }
            files[".mcp.json"] = (json.dumps(mcp_payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

        files[".codex-plugin/plugin.json"] = (
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
        ).encode("utf-8")

        for sname, sitem in sorted(plugin.skills.items()):
            for rel, data in sitem.files.items():
                files[f"skills/{sname}/{rel}"] = data

        for rname, rbody in sorted(plugin.rules.items()):
            files[f"rules/{rname}"] = rbody.encode("utf-8")

        dir_changed = sync_directory_files(plugin_dir, files)

        cfg, _ = self._load_config_toml()
        marketplaces = dict(cfg.get("marketplaces") or {})
        marketplaces[mp_name] = {
            "source_type": "local",
            "source": to_native_path(str(mp_root)),
        }
        cfg["marketplaces"] = marketplaces

        plugins_cfg = dict(cfg.get("plugins") or {})
        plugins_cfg[f"{plugin.name}@{mp_name}"] = {"enabled": plugin.enabled}
        cfg["plugins"] = plugins_cfg

        cfg_changed = self._save_config_toml(cfg)
        return (dir_changed or cfg_changed), []

    def delete_plugin(self, plugin_name: str) -> bool:
        mp_name = "agentsync-synced"
        removed = remove_tree_if_exists(self.plugins_cache_dir / mp_name / plugin_name)
        cfg, _ = self._load_config_toml()
        plugins_cfg = dict(cfg.get("plugins") or {})
        key = f"{plugin_name}@{mp_name}"
        if key in plugins_cfg:
            del plugins_cfg[key]
            cfg["plugins"] = plugins_cfg
            self._save_config_toml(cfg)
            removed = True
        return removed

    # --- 4. Instructions & Rules ---

    def read_global_instructions(self) -> InstructionDoc | None:
        sources: list[Path] = []
        if self.agents_override_md.exists():
            sources.append(self.agents_override_md)
        if self.agents_md.exists():
            sources.append(self.agents_md)
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
        changed = atomic_write_text(self.agents_md, doc.raw_text)
        if self.agents_override_md.exists():
            if atomic_write_text(self.agents_override_md, doc.raw_text):
                changed = True
        return changed, []

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

    # --- 5. Projects / Workspaces ---

    def read_projects(self) -> dict[str, ProjectItem]:
        projects: dict[str, ProjectItem] = {}

        def _register(
            raw_path: str,
            name: str | None = None,
            codex_id: str | None = None,
            legacy_id: str | None = None,
            created_ms: int = 0,
            updated_ms: int = 0,
            authoritative_name: bool = False,
        ) -> None:
            key = path_dedup_key(raw_path)
            if not key:
                return
            display = normalize_path(raw_path)
            existing = projects.get(key)
            if existing is None:
                item = ProjectItem.from_path(
                    raw_path=display,
                    name=name,
                    native_ids_by_tool={self.tool_name: [codex_id]} if codex_id else {},
                    secondary_ids_by_tool={self.tool_name: [legacy_id]} if legacy_id else {},
                    is_git=(Path(to_native_path(display)) / ".git").exists(),
                    created_at_ms=created_ms,
                    updated_at_ms=updated_ms,
                )
                projects[key] = item
            else:
                if codex_id:
                    ids = existing.native_ids_by_tool.setdefault(self.tool_name, [])
                    if codex_id not in ids:
                        ids.append(codex_id)
                if legacy_id:
                    lids = existing.secondary_ids_by_tool.setdefault(self.tool_name, [])
                    if legacy_id not in lids:
                        lids.append(legacy_id)
                if authoritative_name and name:
                    existing.name = name
                elif updated_ms > existing.updated_at_ms:
                    existing.updated_at_ms = updated_ms
                    if name and not existing.secondary_ids_by_tool.get(self.tool_name):
                        existing.name = name
                if created_ms and (not existing.created_at_ms or created_ms < existing.created_at_ms):
                    existing.created_at_ms = created_ms

        gs = self._load_global_state()
        legacy_to_app = (
            gs.get("app-server-project-id-by-legacy-project-id-by-host") or {}
        ).get(self._host_key()) or {}
        for lid, pdata in (gs.get("local-projects") or {}).items():
            if not isinstance(pdata, dict):
                continue
            roots = pdata.get("rootPaths") or []
            if not roots:
                continue
            _register(
                raw_path=str(roots[0]),
                name=pdata.get("name"),
                codex_id=legacy_to_app.get(lid),
                legacy_id=lid,
                created_ms=int(pdata.get("createdAt") or 0),
                updated_ms=int(pdata.get("updatedAt") or 0),
                authoritative_name=True,
            )

        if self.state_db.exists():
            conn = sqlite_safe_connect(self.state_db, readonly=True)
            conn.row_factory = sqlite3.Row
            try:
                tables = {
                    r[0]
                    for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                }
                if {"projects", "project_roots"}.issubset(tables):
                    rows = conn.execute(
                        """
                        SELECT p.id, p.name, p.created_at_ms, p.updated_at_ms, r.path
                        FROM projects p
                        JOIN project_roots r ON r.project_id = p.id AND r.position = 0
                        ORDER BY p.position ASC, p.created_at_ms ASC
                        """
                    ).fetchall()
                    for row in rows:
                        _register(
                            raw_path=row["path"],
                            name=row["name"],
                            codex_id=row["id"],
                            created_ms=int(row["created_at_ms"] or 0),
                            updated_ms=int(row["updated_at_ms"] or 0),
                        )
            finally:
                conn.close()

        cfg, cfg_mtime_ms = self._load_config_toml()
        for raw_p in (cfg.get("projects") or {}).keys():
            _register(raw_path=raw_p, updated_ms=cfg_mtime_ms)

        return projects

    def write_projects(
        self, projects: dict[str, ProjectItem]
    ) -> tuple[bool, dict[str, ProjectItem]]:
        changed = False
        now = int(time.time() * 1000)
        updated: dict[str, ProjectItem] = {}

        for key, item in sorted(projects.items()):
            copy_item = item.model_copy(deep=True)
            ids = copy_item.native_ids_by_tool.setdefault(self.tool_name, [])
            if not ids:
                ids.append(str(uuid.uuid5(uuid.NAMESPACE_URL, f"codex-proj:{key}")))
            lids = copy_item.secondary_ids_by_tool.setdefault(self.tool_name, [])
            if not lids:
                lids.append(str(uuid.uuid5(uuid.NAMESPACE_URL, f"codex-legacy:{key}")))
            if not copy_item.created_at_ms:
                copy_item.created_at_ms = now
            if not copy_item.updated_at_ms:
                copy_item.updated_at_ms = copy_item.created_at_ms
            updated[key] = copy_item

        conn = self._ensure_state_db()
        try:
            existing_rows = conn.execute(
                """
                SELECT p.id, p.name, r.path
                FROM projects p
                LEFT JOIN project_roots r ON r.project_id = p.id AND r.position = 0
                """
            ).fetchall()
            existing_by_id = {r["id"]: (r["name"], r["path"]) for r in existing_rows}

            dup_remap: dict[str, str] = {}
            for r in existing_rows:
                if r["path"]:
                    k = path_dedup_key(r["path"])
                    if k in updated:
                        primary_id = updated[k].native_ids_by_tool[self.tool_name][0]
                        if r["id"] != primary_id:
                            dup_remap[r["id"]] = primary_id

            db_dirty = False
            for old_id, new_id in dup_remap.items():
                if new_id not in existing_by_id:
                    target_proj = next(
                        p for p in updated.values() if p.native_ids_by_tool[self.tool_name][0] == new_id
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO projects(id, name, metadata, position, created_at_ms, updated_at_ms) "
                        "VALUES(?, ?, '{}', 0, ?, ?)",
                        (new_id, target_proj.name, target_proj.created_at_ms, target_proj.updated_at_ms),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO project_roots(project_id, position, path) VALUES(?, 0, ?)",
                        (new_id, to_native_path(target_proj.display_path)),
                    )
                    existing_by_id[new_id] = (target_proj.name, to_native_path(target_proj.display_path))
                conn.execute("UPDATE threads SET project_id = ? WHERE project_id = ?", (new_id, old_id))
                conn.execute("DELETE FROM project_idempotency_keys WHERE project_id = ?", (old_id,))
                conn.execute("DELETE FROM projects WHERE id = ?", (old_id,))
                db_dirty = True

            for pos, (key, item) in enumerate(sorted(updated.items())):
                pid = item.native_ids_by_tool[self.tool_name][0]
                lid = item.secondary_ids_by_tool[self.tool_name][0]
                native_path = to_native_path(item.display_path)
                cur = existing_by_id.get(pid)
                if cur is None:
                    conn.execute(
                        "INSERT INTO projects(id, name, metadata, position, created_at_ms, updated_at_ms) "
                        "VALUES(?, ?, '{}', ?, ?, ?)",
                        (pid, item.name, pos, item.created_at_ms, item.updated_at_ms),
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO project_roots(project_id, position, path) VALUES(?, 0, ?)",
                        (pid, native_path),
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO project_idempotency_keys(key, project_id, created_at_ms) VALUES(?, ?, ?)",
                        (lid, pid, item.created_at_ms),
                    )
                    db_dirty = True
                else:
                    cur_name, cur_path = cur
                    if cur_name != item.name or path_dedup_key(cur_path or "") != key or cur_path != native_path:
                        conn.execute(
                            "UPDATE projects SET name = ?, updated_at_ms = ? WHERE id = ?",
                            (item.name, item.updated_at_ms, pid),
                        )
                        conn.execute(
                            "INSERT OR REPLACE INTO project_roots(project_id, position, path) VALUES(?, 0, ?)",
                            (pid, native_path),
                        )
                        db_dirty = True

            if db_dirty:
                conn.commit()
                changed = True
        finally:
            conn.close()

        # Sync .codex-global-state.json
        gs = self._load_global_state()
        local_projects = dict(gs.get("local-projects") or {})
        legacy_dup_remap: dict[str, str] = {}
        new_local_projects: dict[str, Any] = {}

        for old_lid, lp in local_projects.items():
            roots = (lp or {}).get("rootPaths") or []
            if roots:
                k = path_dedup_key(str(roots[0]))
                if k in updated:
                    primary_lid = updated[k].secondary_ids_by_tool[self.tool_name][0]
                    if old_lid != primary_lid:
                        legacy_dup_remap[old_lid] = primary_lid
                    continue
            new_local_projects[old_lid] = lp

        for _key, item in sorted(updated.items()):
            lid = item.secondary_ids_by_tool[self.tool_name][0]
            native_path = to_native_path(item.display_path)
            prev = local_projects.get(lid) or {}
            new_local_projects[lid] = {
                "id": lid,
                "name": item.name,
                "rootPaths": [native_path],
                "createdAt": int(prev.get("createdAt") or item.created_at_ms),
                "updatedAt": int(prev.get("updatedAt") or item.updated_at_ms),
            }

        gs["local-projects"] = new_local_projects
        old_order = list(gs.get("project-order") or [])
        new_order: list[str] = []
        for lid in old_order:
            mlid = legacy_dup_remap.get(lid, lid)
            if mlid in new_local_projects and mlid not in new_order:
                new_order.append(mlid)
        for lid in new_local_projects:
            if lid not in new_order:
                new_order.append(lid)
        gs["project-order"] = new_order

        host_map = dict(gs.get("app-server-project-id-by-legacy-project-id-by-host") or {})
        hk = self._host_key()
        inner = dict(host_map.get(hk) or {})
        for old_lid in legacy_dup_remap:
            inner.pop(old_lid, None)
        for item in updated.values():
            inner[item.secondary_ids_by_tool[self.tool_name][0]] = item.native_ids_by_tool[self.tool_name][0]
        host_map[hk] = inner
        gs["app-server-project-id-by-legacy-project-id-by-host"] = host_map

        tpa = dict(gs.get("thread-project-assignments") or {})
        for assign in tpa.values():
            if isinstance(assign, dict) and assign.get("projectId") in legacy_dup_remap:
                assign["projectId"] = legacy_dup_remap[assign["projectId"]]
        gs["thread-project-assignments"] = tpa

        if self._save_global_state(gs):
            changed = True

        # Deduplicate config.toml [projects]
        cfg, _ = self._load_config_toml()
        old_cfg_projects = dict(cfg.get("projects") or {})
        new_cfg_projects: dict[str, Any] = {}
        seen_keys: set[str] = set()
        for raw_p, pval in old_cfg_projects.items():
            k = path_dedup_key(raw_p)
            if k in updated:
                if k not in seen_keys:
                    new_cfg_projects[to_native_path(updated[k].display_path)] = (
                        pval if isinstance(pval, dict) else {"trust_level": "trusted"}
                    )
                    seen_keys.add(k)
            elif k not in seen_keys:
                new_cfg_projects[to_native_path(raw_p)] = pval
                seen_keys.add(k)
        for k, item in sorted(updated.items()):
            if k not in seen_keys:
                new_cfg_projects[to_native_path(item.display_path)] = {"trust_level": "trusted"}
                seen_keys.add(k)
        cfg["projects"] = new_cfg_projects
        if self._save_config_toml(cfg):
            changed = True

        for item in updated.values():
            item.native_ids_by_tool[self.tool_name] = [item.native_ids_by_tool[self.tool_name][0]]
            item.secondary_ids_by_tool[self.tool_name] = [item.secondary_ids_by_tool[self.tool_name][0]]

        return changed, updated

    # --- 6. Chats / Sessions ---

    def _extract_codex_item_message(
        self, item_type: str, item_json_str: str, created_at_ms: int, turn_id: str
    ) -> ChatMessage | None:
        try:
            obj = json.loads(item_json_str)
        except Exception:
            return None
        if item_type == "userMessage":
            parts: list[str] = []
            content = obj.get("content")
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("text"):
                        parts.append(str(c["text"]))
                    elif isinstance(c, str):
                        parts.append(c)
            elif isinstance(content, str):
                parts.append(content)
            if not parts and obj.get("text"):
                parts.append(str(obj["text"]))
            text = "\n".join(parts).strip()
            if text:
                return ChatMessage(role="user", text=text, timestamp_ms=created_at_ms, turn_id=turn_id)
        elif item_type == "agentMessage":
            text = str(obj.get("text") or "").strip()
            if text:
                return ChatMessage(role="assistant", text=text, timestamp_ms=created_at_ms, turn_id=turn_id)
        return None

    def _read_rollout_messages(self, rollout_path: Path) -> list[ChatMessage]:
        messages: list[ChatMessage] = []
        if not rollout_path.exists():
            return messages
        for line in rollout_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("type") == "response_item":
                payload = obj.get("payload") or {}
                if payload.get("type") == "message" and payload.get("role") in ("user", "assistant"):
                    parts = [
                        str(c["text"]) if isinstance(c, dict) and c.get("text") else (c if isinstance(c, str) else "")
                        for c in (payload.get("content") or [])
                    ]
                    txt = "\n".join(p for p in parts if p).strip()
                    if txt:
                        ts_ms = 0
                        ts_str = obj.get("timestamp")
                        if ts_str:
                            try:
                                ts_ms = int(
                                    datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp() * 1000
                                )
                            except Exception:
                                pass
                        messages.append(ChatMessage(role=payload["role"], text=txt, timestamp_ms=ts_ms))
        return messages

    def read_chats(
        self,
        projects_by_key: dict[str, ProjectItem],
        include_subagents: bool = False,
    ) -> list[ChatSession]:
        if not self.state_db.exists():
            return []

        gs = self._load_global_state()
        tpa = gs.get("thread-project-assignments") or {}
        local_projects = gs.get("local-projects") or {}
        projectless_ids = set(gs.get("projectless-thread-ids") or [])

        pid_to_proj: dict[str, tuple[str, str]] = {}
        for k, p in projects_by_key.items():
            for cid in p.native_ids_by_tool.get(self.tool_name, []):
                pid_to_proj[cid] = (k, p.display_path)
            for lid in p.secondary_ids_by_tool.get(self.tool_name, []):
                pid_to_proj[lid] = (k, p.display_path)

        conn = sqlite_safe_connect(self.state_db, readonly=True)
        conn.row_factory = sqlite3.Row
        try:
            tables = {
                r[0]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            if "threads" not in tables:
                return []

            spawn_parent: dict[str, str] = {}
            if "thread_spawn_edges" in tables:
                for edge in conn.execute(
                    "SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges"
                ).fetchall():
                    spawn_parent[edge["child_thread_id"]] = edge["parent_thread_id"]

            if "project_roots" in tables:
                for r in conn.execute(
                    "SELECT project_id, path FROM project_roots WHERE position = 0"
                ).fetchall():
                    k = path_dedup_key(r["path"])
                    if k:
                        pid_to_proj.setdefault(r["project_id"], (k, normalize_path(r["path"])))

            thread_rows = conn.execute("SELECT * FROM threads ORDER BY updated_at DESC").fetchall()
        finally:
            conn.close()

        msgs_by_thread: dict[str, list[ChatMessage]] = {}
        if self.thread_history_db.exists():
            hconn = sqlite_safe_connect(self.thread_history_db, readonly=True)
            hconn.row_factory = sqlite3.Row
            try:
                htables = {
                    r[0]
                    for r in hconn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                }
                if "thread_items" in htables:
                    rows = hconn.execute(
                        """
                        SELECT thread_id, turn_id, item_type, item_json, created_at_ms
                        FROM thread_items
                        WHERE item_type IN ('userMessage', 'agentMessage')
                        ORDER BY thread_id, rollout_ordinal ASC
                        """
                    ).fetchall()
                    for r in rows:
                        msg = self._extract_codex_item_message(
                            r["item_type"], r["item_json"], int(r["created_at_ms"] or 0), r["turn_id"]
                        )
                        if msg:
                            msgs_by_thread.setdefault(r["thread_id"], []).append(msg)
            finally:
                hconn.close()

        sessions: list[ChatSession] = []
        for row in thread_rows:
            tid = row["id"]
            parent_id = spawn_parent.get(tid) or row["attached_thread_parent_id"]
            is_subagent = (row["thread_source"] == "subagent") or bool(parent_id)
            if is_subagent and not include_subagents:
                continue

            proj_key: str | None = None
            proj_path: str | None = None
            native_pid: str | None = row["project_id"]

            if native_pid and native_pid in pid_to_proj:
                proj_key, proj_path = pid_to_proj[native_pid]
            elif tid in tpa and isinstance(tpa[tid], dict):
                lid = tpa[tid].get("projectId")
                if lid and lid in pid_to_proj:
                    proj_key, proj_path = pid_to_proj[lid]
                    native_pid = lid
                elif lid and lid in local_projects:
                    roots = local_projects[lid].get("rootPaths") or []
                    if roots:
                        proj_key = path_dedup_key(roots[0])
                        proj_path = normalize_path(roots[0])
                        native_pid = lid
            elif tid not in projectless_ids:
                norm_cwd = normalize_path(row["cwd"] or "")
                if norm_cwd and "/documents/codex/" not in norm_cwd.lower():
                    ck = path_dedup_key(norm_cwd)
                    if ck in projects_by_key:
                        proj_key = ck
                        proj_path = projects_by_key[ck].display_path

            messages = msgs_by_thread.get(tid) or []
            if not messages and row["rollout_path"]:
                messages = self._read_rollout_messages(Path(to_native_path(row["rollout_path"])))
            if not messages and row["first_user_message"]:
                messages = [
                    ChatMessage(
                        role="user",
                        text=row["first_user_message"],
                        timestamp_ms=int(row["created_at_ms"] or (row["created_at"] * 1000)),
                    )
                ]

            created_ms = int(row["created_at_ms"] or (row["created_at"] * 1000))
            updated_ms = int(row["updated_at_ms"] or (row["updated_at"] * 1000))
            title = (row["name"] or row["title"] or "").strip()
            if not title and messages:
                title = messages[0].text.splitlines()[0][:80]

            sessions.append(
                ChatSession(
                    tool=self.tool_name,
                    native_id=tid,
                    title=title,
                    project_key=proj_key,
                    project_path=proj_path,
                    native_project_id=native_pid,
                    created_at_ms=created_ms,
                    updated_at_ms=updated_ms,
                    is_subagent=is_subagent,
                    parent_id=parent_id,
                    messages=messages,
                ).compute_hashes()
            )
        return sessions

    def _build_conformant_rollout(
        self,
        tid: str,
        cwd_native: str,
        created_ms: int,
        updated_ms: int,
        messages: list[ChatMessage],
        defaults: dict[str, str | None],
    ) -> tuple[str, list[dict[str, Any]], int, int]:
        """Build native-conformant Codex rollout JSONL + structured turn metadata with exact byte offsets."""
        lines: list[str] = []
        byte_offset = 0
        ordinal = 0

        def _append(obj: dict[str, Any]) -> tuple[int, int]:
            nonlocal byte_offset, ordinal
            obj_ord = ordinal
            obj["ordinal"] = obj_ord
            line = json.dumps(obj, ensure_ascii=False, separators=(", ", ": "))
            start_b = byte_offset
            lines.append(line)
            byte_offset += len((line + "\n").encode("utf-8"))
            ordinal += 1
            return obj_ord, start_b

        created_iso = _iso_utc_ms(created_ms)
        session_meta_payload: dict[str, Any] = {
            "id": tid,
            "session_id": tid,
            "timestamp": created_iso,
            "cwd": cwd_native,
            "runtime_workspace_roots": [cwd_native],
            "originator": "Codex Desktop",
            "cli_version": defaults["cli_version"] or "0.167.0",
            "source": "vscode",
            "thread_source": "user",
            "model_provider": "openai",
            "base_instructions": {"text": ""},
            "history_mode": "paginated",
            "context_window": {
                "window_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"codex-ctx:{tid}"))
            },
        }
        if defaults.get("creator_user_id"):
            session_meta_payload["creator_user_id"] = defaults["creator_user_id"]
        if defaults.get("creator_account_id"):
            session_meta_payload["creator_account_id"] = defaults["creator_account_id"]

        _append(
            {
                "timestamp": created_iso,
                "type": "session_meta",
                "payload": session_meta_payload,
            }
        )

        # Group user/assistant messages into turns
        turns_msgs: list[list[ChatMessage]] = []
        for m in messages:
            if m.role not in ("user", "assistant") or not m.text.strip():
                continue
            if m.role == "user" or not turns_msgs:
                turns_msgs.append([m])
            else:
                turns_msgs[-1].append(m)

        turn_records: list[dict[str, Any]] = []
        model_name = defaults.get("model") or "gpt-5"

        for t_idx, t_msgs in enumerate(turns_msgs):
            turn_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"codex-turn:{tid}:{t_idx}"))
            t_start_ms = t_msgs[0].timestamp_ms or (created_ms + t_idx * 1000)
            t_end_ms = t_msgs[-1].timestamp_ms or max(t_start_ms, updated_ms)
            t_start_iso = _iso_utc_ms(t_start_ms)
            t_end_iso = _iso_utc_ms(t_end_ms)

            # 1. task_started event
            turn_start_ord, turn_start_byte = _append(
                {
                    "timestamp": t_start_iso,
                    "type": "event_msg",
                    "payload": {
                        "type": "task_started",
                        "turn_id": turn_id,
                        "root_turn_id": turn_id,
                        "started_at": t_start_ms // 1000,
                        "turn_attribution": {
                            "turn_id": turn_id,
                            "turn_trigger": "composer",
                            "parent_turn_id": None,
                            "initiating_agent_path": None,
                            "root_turn_id": turn_id,
                        },
                    },
                }
            )

            # 2. turn_context
            _append(
                {
                    "timestamp": t_start_iso,
                    "type": "turn_context",
                    "payload": {
                        "turn_id": turn_id,
                        "root_turn_id": turn_id,
                        "cwd": cwd_native,
                        "approval_policy": "never",
                        "approvals_reviewer": "user",
                        "sandbox_policy": {"type": "danger-full-access"},
                        "model": model_name,
                        "effort": defaults.get("reasoning_effort") or "medium",
                    },
                }
            )

            first_u_id: str | None = None
            final_a_id: str | None = None
            last_agent_text = ""
            items_for_db: list[tuple[str, int, int, str, str]] = []

            for m_idx, msg in enumerate(t_msgs):
                mts = msg.timestamp_ms or (t_start_ms + m_idx * 500)
                mts_iso = _iso_utc_ms(mts)
                raw_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"codex-item:{tid}:{t_idx}:{m_idx}"))

                if msg.role == "user":
                    item_id = raw_uuid
                    if first_u_id is None:
                        first_u_id = item_id
                    _append(
                        {
                            "timestamp": mts_iso,
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "id": f"msg_{item_id}",
                                "role": "user",
                                "content": [{"type": "input_text", "text": msg.text}],
                            },
                        }
                    )
                    ic_ord, _ = _append(
                        {
                            "timestamp": mts_iso,
                            "type": "event_msg",
                            "payload": {
                                "type": "item_completed",
                                "thread_id": tid,
                                "turn_id": turn_id,
                                "started_at_ms": mts,
                                "completed_at_ms": mts,
                                "item": {
                                    "type": "UserMessage",
                                    "id": item_id,
                                    "client_id": item_id,
                                    "content": [{"type": "text", "text": msg.text, "text_elements": []}],
                                },
                            },
                        }
                    )
                    db_json = json.dumps(
                        {
                            "type": "userMessage",
                            "id": item_id,
                            "clientId": item_id,
                            "content": [{"type": "text", "text": msg.text, "text_elements": []}],
                        },
                        ensure_ascii=False,
                    )
                    items_for_db.append((item_id, ic_ord, mts, "userMessage", db_json))
                else:
                    item_id = f"msg_{raw_uuid}"
                    final_a_id = item_id
                    last_agent_text = msg.text
                    ic_ord, _ = _append(
                        {
                            "timestamp": mts_iso,
                            "type": "event_msg",
                            "payload": {
                                "type": "item_completed",
                                "thread_id": tid,
                                "turn_id": turn_id,
                                "started_at_ms": mts,
                                "completed_at_ms": mts,
                                "item": {
                                    "type": "AgentMessage",
                                    "id": item_id,
                                    "content": [{"type": "Text", "text": msg.text}],
                                },
                            },
                        }
                    )
                    _append(
                        {
                            "timestamp": mts_iso,
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "status": "completed",
                                "id": item_id,
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "output_text",
                                        "text": msg.text,
                                        "annotations": [],
                                        "logprobs": [],
                                    }
                                ],
                                "phase": "final_answer",
                            },
                        }
                    )
                    db_json = json.dumps(
                        {
                            "type": "agentMessage",
                            "id": item_id,
                            "text": msg.text,
                            "phase": "final_answer",
                            "memoryCitation": None,
                            "delivery": None,
                            "questions": None,
                        },
                        ensure_ascii=False,
                    )
                    items_for_db.append((item_id, ic_ord, mts, "agentMessage", db_json))

            # 3. task_complete event
            end_ord, _ = _append(
                {
                    "timestamp": t_end_iso,
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": turn_id,
                        "root_turn_id": turn_id,
                        "last_agent_message": last_agent_text,
                    },
                }
            )
            turn_records.append(
                {
                    "turn_id": turn_id,
                    "start_ord": turn_start_ord,
                    "end_ord": end_ord,
                    "start_byte": turn_start_byte,
                    "end_byte": byte_offset,
                    "started_at": t_start_ms // 1000,
                    "completed_at": t_end_ms // 1000,
                    "duration_ms": max(0, t_end_ms - t_start_ms),
                    "first_user_item_id": first_u_id,
                    "final_agent_item_id": final_a_id,
                    "items": items_for_db,
                }
            )

        rollout_text = "\n".join(lines) + "\n"
        return rollout_text, turn_records, byte_offset, ordinal

    def write_chat(
        self,
        chat: ChatSession,
        target_native_id: str | None,
        projects_by_key: dict[str, ProjectItem],
    ) -> tuple[str, bool]:
        tid = target_native_id or str(uuid.uuid4())
        created_ms = chat.created_at_ms or int(time.time() * 1000)
        updated_ms = chat.updated_at_ms or created_ms
        created_sec = created_ms // 1000
        updated_sec = updated_ms // 1000

        project = projects_by_key.get(chat.project_key) if chat.project_key else None
        codex_pid = project.primary_id(self.tool_name) if project else None
        codex_lid = project.secondary_id(self.tool_name) if project else None
        cwd_native = (
            to_native_path(project.display_path)
            if project
            else to_native_path(str(self.root_path / "synced-chats"))
        )
        defaults = self._detect_native_thread_defaults()

        dt = datetime.fromtimestamp(created_sec, tz=UTC)
        rel_dir = self.sessions_dir / f"{dt.year:04d}" / f"{dt.month:02d}" / f"{dt.day:02d}"
        rollout_file = rel_dir / f"rollout-{dt.strftime('%Y-%m-%dT%H-%M-%S')}-{tid}.jsonl"

        sconn = self._ensure_state_db()
        try:
            existing_row = sconn.execute(
                "SELECT rollout_path FROM threads WHERE id = ?", (tid,)
            ).fetchone()
            if existing_row and existing_row["rollout_path"]:
                existing_rp = Path(to_native_path(existing_row["rollout_path"]))
                if str(self.sessions_dir).lower() in str(existing_rp).lower():
                    rollout_file = existing_rp

            rollout_text, turn_records, total_bytes, next_ordinal = self._build_conformant_rollout(
                tid=tid,
                cwd_native=cwd_native,
                created_ms=created_ms,
                updated_ms=updated_ms,
                messages=chat.messages,
                defaults=defaults,
            )

            # Validate generated JSONL strictly before atomic replace
            def _validate_jsonl(text: str) -> None:
                raw_lines = [ln for ln in text.split("\n") if ln.strip()]
                if not raw_lines:
                    raise ValueError("Empty Codex rollout JSONL")
                first = json.loads(raw_lines[0])
                if first.get("type") != "session_meta":
                    raise ValueError("Codex rollout line 1 is not session_meta")
                pl = first.get("payload") or {}
                for req in REQUIRED_SESSION_META_KEYS:
                    if not pl.get(req):
                        raise ValueError(f"Codex session_meta missing required field '{req}'")
                for ln in raw_lines[1:]:
                    json.loads(ln)

            changed = atomic_write_text(rollout_file, rollout_text, validator=_validate_jsonl)

            first_user_msg = next(
                (m.text for m in chat.messages if m.role == "user" and m.text.strip()),
                "",
            )
            title = (
                chat.title or (first_user_msg.splitlines()[0][:80] if first_user_msg else "Synced Chat")
            ).strip()

            sconn.execute(
                """
                INSERT INTO threads(
                    id, rollout_path, created_at, updated_at, source, model_provider,
                    cwd, title, sandbox_policy, approval_mode, tokens_used, has_user_event,
                    archived, cli_version, first_user_message, memory_mode,
                    model, reasoning_effort,
                    created_at_ms, updated_at_ms, thread_source, preview,
                    recency_at, recency_at_ms, history_mode, name, is_pinned,
                    project_id, originator, creator_user_id, creator_account_id
                ) VALUES(
                    ?, ?, ?, ?, 'vscode', 'openai',
                    ?, ?, '{"type":"disabled"}', 'never', 0, 1,
                    0, ?, ?, 'enabled',
                    ?, ?,
                    ?, ?, 'user', ?,
                    ?, ?, 'paginated', ?, 0,
                    ?, 'Codex Desktop', ?, ?
                )
                ON CONFLICT(id) DO UPDATE SET
                    rollout_path=excluded.rollout_path,
                    updated_at=excluded.updated_at,
                    cwd=excluded.cwd,
                    title=excluded.title,
                    cli_version=excluded.cli_version,
                    first_user_message=excluded.first_user_message,
                    model=COALESCE(threads.model, excluded.model),
                    reasoning_effort=COALESCE(threads.reasoning_effort, excluded.reasoning_effort),
                    updated_at_ms=excluded.updated_at_ms,
                    preview=excluded.preview,
                    recency_at=excluded.recency_at,
                    recency_at_ms=excluded.recency_at_ms,
                    name=excluded.name,
                    project_id=excluded.project_id,
                    creator_user_id=COALESCE(threads.creator_user_id, excluded.creator_user_id),
                    creator_account_id=COALESCE(threads.creator_account_id, excluded.creator_account_id)
                """,
                (
                    tid,
                    to_native_path(str(rollout_file)),
                    created_sec,
                    updated_sec,
                    cwd_native,
                    title,
                    defaults["cli_version"] or "0.167.0",
                    first_user_msg,
                    defaults["model"],
                    defaults["reasoning_effort"],
                    created_ms,
                    updated_ms,
                    first_user_msg[:400],
                    updated_sec,
                    updated_ms,
                    title,
                    codex_pid,
                    defaults.get("creator_user_id"),
                    defaults.get("creator_account_id"),
                ),
            )
            sconn.commit()
            changed = True
        finally:
            sconn.close()

        hconn = self._ensure_history_db()
        try:
            hconn.execute("DELETE FROM thread_items WHERE thread_id = ?", (tid,))
            hconn.execute("DELETE FROM thread_turns WHERE thread_id = ?", (tid,))
            for tr in turn_records:
                hconn.execute(
                    """
                    INSERT INTO thread_turns(
                        thread_id, turn_id, rollout_ordinal, status,
                        started_at, completed_at, duration_ms,
                        first_user_item_id, final_agent_item_id,
                        rollout_byte_offset, rollout_end_ordinal, rollout_end_byte_offset, root_turn_id
                    ) VALUES(?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tid,
                        tr["turn_id"],
                        tr["start_ord"],
                        tr["started_at"],
                        tr["completed_at"],
                        tr["duration_ms"],
                        tr["first_user_item_id"],
                        tr["final_agent_item_id"],
                        tr["start_byte"],
                        tr["end_ord"],
                        tr["end_byte"],
                        tr["turn_id"],
                    ),
                )
                for item_id, ord_i, mts, item_type, item_json in tr["items"]:
                    hconn.execute(
                        """
                        INSERT INTO thread_items(
                            thread_id, turn_id, item_id, rollout_ordinal,
                            created_at_ms, item_json, item_type, updated_at_ordinal,
                            started_at_ms, completed_at_ms
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (tid, tr["turn_id"], item_id, ord_i, mts, item_json, item_type, ord_i, mts, mts),
                    )
            hconn.execute(
                """
                INSERT OR REPLACE INTO thread_history_projection_state(
                    thread_id, next_rollout_byte_offset, next_rollout_ordinal
                ) VALUES(?, ?, ?)
                """,
                (tid, total_bytes, next_ordinal),
            )
            hconn.commit()
        finally:
            hconn.close()

        # Upsert session_index.jsonl
        si_entries: dict[str, dict[str, Any]] = {}
        if self.session_index_jsonl.exists():
            for line in self.session_index_jsonl.read_text(encoding="utf-8-sig", errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                    if obj.get("id"):
                        si_entries[obj["id"]] = obj
                except Exception:
                    continue
        si_entries[tid] = {
            "id": tid,
            "thread_name": title,
            "updated_at": _iso_utc_ms(updated_ms),
        }
        atomic_write_text(
            self.session_index_jsonl,
            "\n".join(json.dumps(v, ensure_ascii=False) for v in si_entries.values()) + "\n",
        )

        # Update .codex-global-state.json thread assignment
        gs = self._load_global_state()
        tpa = dict(gs.get("thread-project-assignments") or {})
        tpm = dict(gs.get("thread-project-membership-host-ids") or {})
        projectless = list(gs.get("projectless-thread-ids") or [])
        if codex_lid:
            tpa[tid] = {"projectKind": "local", "projectId": codex_lid}
            tpm[tid] = "local"
            projectless = [x for x in projectless if x != tid]
        else:
            tpa.pop(tid, None)
            tpm.pop(tid, None)
            if tid not in projectless:
                projectless.append(tid)
        gs["thread-project-assignments"] = tpa
        gs["thread-project-membership-host-ids"] = tpm
        gs["projectless-thread-ids"] = projectless
        self._save_global_state(gs)

        return tid, changed

    # --- Diagnostics & Repair ---

    def doctor(self) -> list[DoctorIssue]:
        issues: list[DoctorIssue] = []

        # 1. Validate config.toml
        if self.config_toml.exists():
            try:
                tomllib.loads(self.config_toml.read_text(encoding="utf-8-sig"))
            except Exception as exc:
                issues.append(
                    DoctorIssue(
                        tool=self.tool_name,
                        severity="error",
                        code="CODEX_INVALID_TOML",
                        path=str(self.config_toml),
                        message=f"Invalid TOML syntax: {exc}",
                        fixable=False,
                    )
                )

        # 2. Validate .codex-global-state.json
        if self.global_state_json.exists():
            try:
                json.loads(self.global_state_json.read_text(encoding="utf-8-sig"))
            except Exception as exc:
                issues.append(
                    DoctorIssue(
                        tool=self.tool_name,
                        severity="error",
                        code="CODEX_INVALID_GLOBAL_STATE",
                        path=str(self.global_state_json),
                        message=f"Invalid JSON syntax: {exc}",
                        fixable=False,
                    )
                )

        # 3. Validate all rollout JSONL files in sessions/
        if self.sessions_dir.exists():
            for rf in sorted(self.sessions_dir.rglob("*.jsonl")):
                errs = validate_codex_rollout_file(rf)
                if errs:
                    issues.append(
                        DoctorIssue(
                            tool=self.tool_name,
                            severity="error",
                            code="CODEX_BROKEN_ROLLOUT",
                            path=str(rf),
                            message="; ".join(errs),
                            fixable=True,
                        )
                    )

        # 4. Check duplicate projects in state_5.sqlite
        projs = self.read_projects()
        for p in projs.values():
            dups = p.duplicate_count(self.tool_name)
            if dups > 0:
                issues.append(
                    DoctorIssue(
                        tool=self.tool_name,
                        severity="warning",
                        code="CODEX_DUPLICATE_PROJECT",
                        path=p.display_path,
                        message=f"Project '{p.name}' has {dups + 1} duplicate rows in state_5.sqlite",
                        fixable=True,
                    )
                )

        # 5. Check duplicate skills between ~/.codex/skills and ~/.agents/skills (anti-duplication trap)
        agents_skills_dir = self.root_path.parent / ".agents" / "skills"
        if self.skills_dir.exists() and agents_skills_dir.exists():
            codex_skill_names = {
                d.name
                for d in self.skills_dir.iterdir()
                if d.is_dir() and not d.name.startswith(".") and (d / "SKILL.md").exists()
            }
            agent_skill_names = {
                d.name
                for d in agents_skills_dir.iterdir()
                if d.is_dir() and not d.name.startswith(".") and (d / "SKILL.md").exists()
            }
            overlap = sorted(codex_skill_names & agent_skill_names)
            if overlap:
                issues.append(
                    DoctorIssue(
                        tool=self.tool_name,
                        severity="warning",
                        code="CODEX_DUPLICATE_AGENT_SKILL",
                        path=str(agents_skills_dir),
                        message=(
                            f"{len(overlap)} skill(s) exist in both ~/.codex/skills and ~/.agents/skills "
                            f"({', '.join(overlap[:5])}{'...' if len(overlap) > 5 else ''}), "
                            "which causes duplicate skill loading in Codex."
                        ),
                        fixable=True,
                    )
                )

        # 6. Check broken symlinks inside ~/.codex/skills
        if self.skills_dir.exists():
            for entry in sorted(self.skills_dir.iterdir()):
                if entry.is_symlink() and not entry.exists():
                    issues.append(
                        DoctorIssue(
                            tool=self.tool_name,
                            severity="error",
                            code="CODEX_BROKEN_SKILL_SYMLINK",
                            path=str(entry),
                            message=f"Broken skill symlink '{entry.name}' points to missing target.",
                            fixable=True,
                        )
                    )

        return issues

    def repair(self, quarantine_dir: Path, dry_run: bool = False) -> RepairResult:
        result = RepairResult(tool=self.tool_name)
        # Deduplicate projects in state_5.sqlite if duplicates exist
        projs = self.read_projects()
        has_dup_projects = any(p.duplicate_count(self.tool_name) > 0 for p in projs.values())
        if has_dup_projects:
            if dry_run:
                dup_cnt = sum(p.duplicate_count(self.tool_name) for p in projs.values())
                result.fixed_count += dup_cnt
                result.details.append(f"Would collapse {dup_cnt} duplicate project row(s) in state_5.sqlite")
            else:
                changed, projs = self.write_projects(projs)
                if changed:
                    result.fixed_count += 1
                    result.details.append("Collapsed duplicate project rows in state_5.sqlite")

        # Clean broken symlinks inside ~/.codex/skills
        if self.skills_dir.exists():
            for entry in sorted(self.skills_dir.iterdir()):
                if entry.is_symlink() and not entry.exists():
                    if dry_run:
                        result.fixed_count += 1
                        result.details.append(f"Would remove dangling Codex skill symlink '{entry.name}'")
                    else:
                        entry.unlink(missing_ok=True)
                        result.fixed_count += 1
                        result.details.append(f"Removed dangling Codex skill symlink '{entry.name}'")

        # Deduplicate overlapping skills between ~/.codex/skills and ~/.agents/skills
        agents_skills_dir = self.root_path.parent / ".agents" / "skills"
        if self.skills_dir.exists() and agents_skills_dir.exists():
            codex_names = {
                d.name
                for d in self.skills_dir.iterdir()
                if d.is_dir() and not d.name.startswith(".") and (d / "SKILL.md").exists()
            }
            for adir in sorted(agents_skills_dir.iterdir()):
                if adir.is_dir() and adir.name in codex_names and (adir / "SKILL.md").exists():
                    if dry_run:
                        result.fixed_count += 1
                        result.details.append(
                            f"Would quarantine duplicate ~/.agents/skills/{adir.name} (already in ~/.codex/skills)"
                        )
                    else:
                        ts_dup = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
                        q_dup = quarantine_dir / f"{ts_dup}_agents_skills_dup" / adir.name
                        q_dup.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copytree(adir, q_dup, dirs_exist_ok=True)
                        remove_tree_if_exists(adir)
                        result.quarantined_files.append(str(q_dup))
                        result.fixed_count += 1
                        result.details.append(
                            f"Quarantined duplicate ~/.agents/skills/{adir.name} to prevent double-loading in Codex"
                        )

        if not self.sessions_dir.exists():
            return result

        chats_by_id = {c.native_id: c for c in self.read_chats(projs, include_subagents=True)}

        # Map rollout_path -> thread_id from state_5.sqlite
        path_to_tid: dict[str, str] = {}
        if self.state_db.exists():
            conn = sqlite_safe_connect(self.state_db, readonly=True)
            try:
                for tid, rp in conn.execute("SELECT id, rollout_path FROM threads").fetchall():
                    if rp:
                        path_to_tid[path_dedup_key(rp)] = tid
            except sqlite3.OperationalError:
                pass
            finally:
                conn.close()

        ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        q_root = quarantine_dir / f"{ts}_codex"

        for rf in sorted(self.sessions_dir.rglob("*.jsonl")):
            errs = validate_codex_rollout_file(rf)
            if not errs:
                continue

            tid = path_to_tid.get(path_dedup_key(str(rf)))
            if not tid:
                # Fallback: extract session_id from broken line 1
                try:
                    first = json.loads(rf.read_text(encoding="utf-8-sig", errors="replace").splitlines()[0])
                    pl = first.get("payload") or {}
                    tid = pl.get("id") or pl.get("session_id")
                except Exception:
                    tid = None

            if dry_run:
                result.fixed_count += 1
                result.details.append(f"Would quarantine and regenerate {rf.name} (errors: {'; '.join(errs)})")
                continue

            # Quarantine the broken file first
            q_root.mkdir(parents=True, exist_ok=True)
            q_dst = q_root / rf.name
            shutil.copy2(rf, q_dst)
            result.quarantined_files.append(str(q_dst))

            chat = chats_by_id.get(tid) if tid else None
            if chat is None:
                # Reconstruct minimal ChatSession from broken rollout if unindexed
                msgs = self._read_rollout_messages(rf)
                if msgs and tid:
                    chat = ChatSession(
                        tool=self.tool_name,
                        native_id=tid,
                        title=msgs[0].text.splitlines()[0][:80],
                        created_at_ms=msgs[0].timestamp_ms or int(rf.stat().st_mtime * 1000),
                        updated_at_ms=msgs[-1].timestamp_ms or int(rf.stat().st_mtime * 1000),
                        messages=msgs,
                    ).compute_hashes()

            if chat is not None:
                self.write_chat(chat, target_native_id=chat.native_id, projects_by_key=projs)
                # If write_chat wrote to a canonical path different from rf, remove the old quarantined file
                if rf.exists() and validate_codex_rollout_file(rf):
                    rf.unlink(missing_ok=True)
                result.fixed_count += 1
                result.details.append(f"Quarantined and regenerated conformant rollout: {rf.name}")
            else:
                # Unrecoverable orphan broken file -> remove after quarantining
                rf.unlink(missing_ok=True)
                result.fixed_count += 1
                result.details.append(f"Quarantined unindexed broken rollout: {rf.name}")

        return result
